"""Subscription adapters using the installed, signed-in coding CLIs.

EternalChat owns history and executes memory tools. Each subprocess is a fresh,
noninteractive call with a JSON reply contract, never a resumed coding session.
"""
import asyncio
import base64
import binascii
import json
import math
import heapq
from contextlib import asynccontextmanager
import os
from pathlib import Path
import shutil
import signal
import tempfile

from .errors import ProviderError
from .config import MAX_IMAGE_BYTES
from .cli_protocol import MessageStream, ReplyStream, decode_reply, normalize_usage

CLIENTS = {"codex": "codex", "claudecode": "claude"}
IMAGE_SUFFIXES = {"image/png": ".png", "image/jpeg": ".jpg",
                  "image/gif": ".gif", "image/webp": ".webp"}
CONTRACT = """You are a model backend for EternalChat. Follow the supplied system instructions
and respond to the supplied conversation. Do not use the coding CLI's own tools.
Return ONLY a JSON object with exactly two fields:
{"text": "your reply", "tool_calls": [{"name": "zoom", "arguments": {"id": 0}}]}.
Use an empty tool_calls array for a final answer or compaction. To retrieve memory,
return calls to the supplied tools; EternalChat will execute them and call you
again with the results. Never invent tool results. Tool arguments must be objects.
The text field contains the actual user-facing reply or summary, not JSON prose.
"""
IMAGE_CONTRACT = """Numbered EternalChat image references in the conversation refer to the
actual images attached to this request, in the same numbered order. They may
appear in earlier messages or inside memory tool results. Use the image at each
reference's position in the conversation, along with the surrounding text.
"""


def _clean(value, images):
    if isinstance(value, list):
        return [_clean(item, images) for item in value]
    if isinstance(value, dict):
        if value.get("type") == "image":
            source = value.get("source")
            if (not isinstance(source, dict) or source.get("type") != "base64"
                    or not isinstance(source.get("media_type"), str)
                    or source["media_type"] not in IMAGE_SUFFIXES
                    or not isinstance(source.get("data"), str) or not source["data"]):
                raise ProviderError("Subscription images require base64 PNG, JPEG, GIF or WebP content")
            if len(source["data"]) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
                raise ProviderError("Subscription images must be at most 5 MB each")
            try:
                data = base64.b64decode(source["data"], validate=True)
                if not data:
                    raise ValueError()
            except (ValueError, binascii.Error) as exc:
                raise ProviderError("Subscription image contains invalid base64 data") from exc
            if len(data) > MAX_IMAGE_BYTES:
                raise ProviderError("Subscription images must be at most 5 MB each")
            images.append({"type": "image", "source": {
                "type": "base64", "media_type": source["media_type"], "data": source["data"]}})
            return {"type": "text", "text": f"[EternalChat image {len(images)}]"}
        return {key: _clean(item, images) for key, item in value.items() if key != "cache_control"}
    return value


def _last_mark(messages):
    """(message, block) of the last top-level content block carrying a cache mark."""
    found = None
    for m, message in enumerate(messages):
        content = message.get("content") if isinstance(message, dict) else None
        for b, block in enumerate(content if isinstance(content, list) else []):
            if isinstance(block, dict) and "cache_control" in block:
                found = m, b
    return found


def _prompt_blocks(head, request, mark):
    """The prompt as text blocks: `head` and the request's JSON, split before each
    message content block, so consecutive requests share whole leading blocks.
    Joined, they are exactly head + json.dumps(request). The block holding
    `mark` (message, block) carries a cache mark: one hour, because Claude Code
    marks later blocks for an hour and the API rejects a shorter mark before them.
    Measured with Claude Code 2.1.304, a second call with a grown view then read
    the view from cache instead of writing it again."""
    dump = lambda value: json.dumps(value, ensure_ascii=False)  # noqa: E731
    texts, marked = [head + "{"], None
    for k, (key, value) in enumerate(request.items()):
        texts[-1] += (", " if k else "") + dump(key) + ": "
        if key != "messages":
            texts[-1] += dump(value)
            continue
        texts[-1] += "["
        for m, message in enumerate(value):
            texts[-1] += (", " if m else "") + "{"
            for j, (field, item) in enumerate(message.items()):
                texts[-1] += (", " if j else "") + dump(field) + ": "
                if field != "content" or not isinstance(item, list):
                    texts[-1] += dump(item)
                    continue
                texts[-1] += "["
                for b, block in enumerate(item):
                    texts.append((", " if b else "") + dump(block))
                    if (m, b) == mark:
                        marked = len(texts) - 1
                texts[-1] += "]"
            texts[-1] += "}"
        texts[-1] += "]"
    texts[-1] += "}"
    blocks = [{"type": "text", "text": text} for text in texts]
    if marked is not None:
        blocks[marked]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    return blocks


def _image_content(blocks, images):
    content = list(blocks)
    for number, block in enumerate(images, 1):
        content.extend([{"type": "text", "text": f"[EternalChat image {number}]"}, block])
    return content


class _PriorityGate:
    def __init__(self, limit):
        self.limit, self.active, self.sequence = limit, 0, 0
        self.waiters = []
        self.foreground = 0
        self.foreground_active = 0

    def _admit(self):
        while self.active < self.limit and self.waiters:
            if (self.foreground and not self.foreground_active and self.waiters[0][0] > 0 and
                    self.active >= self.limit - 1):
                break  # reserve a slot across memory-tool boundaries too
            priority, _, future = heapq.heappop(self.waiters)
            if future.cancelled():
                continue
            self.active += 1
            if priority == 0:
                self.foreground_active += 1
            future.set_result(None)

    @asynccontextmanager
    async def enter(self, priority):
        future = asyncio.get_running_loop().create_future()
        self.sequence += 1
        heapq.heappush(self.waiters, (priority, self.sequence, future))
        self._admit()
        try:
            await future
        except BaseException:
            if future.done() and not future.cancelled():
                self.active -= 1
                if priority == 0:
                    self.foreground_active -= 1
            future.cancel()
            self._admit()
            raise
        try:
            yield
        finally:
            self.active -= 1
            if priority == 0:
                self.foreground_active -= 1
            self._admit()


class CLIProvider:
    def __init__(self, client, *, executable=None, timeout=300, concurrency=1, reasoning_effort=None):
        if client not in CLIENTS:
            raise ValueError("Unknown subscription CLI")
        if timeout <= 0 or not math.isfinite(timeout):
            raise ValueError("CLI timeout must be a positive finite number")
        if type(concurrency) is not int or not 1 <= concurrency <= 8:
            raise ValueError("CLI concurrency must be between 1 and 8")
        if reasoning_effort is not None and (not isinstance(reasoning_effort, str) or not reasoning_effort.strip()):
            raise ValueError("Reasoning effort must be a nonempty client-supported level")
        self.client = client
        self.executable = shutil.which(executable or CLIENTS[client])
        if not self.executable:
            raise ProviderError(f"Install {CLIENTS[client]} and sign in before using --cli {client}.")
        # Child calls run in a temporary directory. Preserve the caller's
        # relative executable/PATH lookup while keeping executable symlinks.
        self.executable = os.path.abspath(self.executable)
        self.timeout, self.reasoning_effort = timeout, reasoning_effort
        self._gate = _PriorityGate(concurrency)
        self.last_error = None
        self.rate_limits = None

    def stats(self):
        return {"mode": "subscription", "client": self.client, "concurrency": self._gate.limit,
                "active": self._gate.active,
                "waiting": sum(not future.cancelled() for _, _, future in self._gate.waiters),
                "last_error": self.last_error, "rate_limits": self.rate_limits}

    @asynccontextmanager
    async def foreground(self):
        """Reserve reply capacity throughout a conversation's tool loop."""
        self._gate.foreground += 1
        try:
            yield
        finally:
            self._gate.foreground -= 1
            self._gate._admit()

    def _error(self, detail="", *, category=None):
        # Never echo stderr, credentials, prompts or arbitrary server error bodies.
        detail = str(detail).lower()
        if category is None:
            if any(term in detail for term in ("usage limit", "usage_limit", "usagelimitexceeded", "weekly limit",
                    "rate limit", "rate_limit", "rate_limited", "quota", "usage_pool_exhausted",
                    "429", "402", "hit your", "balance exhausted")):
                category = "limit"
            elif any(term in detail for term in ("unauthor", "not signed in", "not authenticated", "authentication", "login", "401")):
                category = "login"
            elif any(term in detail for term in ("forbidden", "403")):
                category = "access"
            elif any(term in detail for term in ("contextwindowexceeded", "context window", "context length")):
                category = "context"
            elif any(term in detail for term in ("unexpected argument", "unknown option", "unrecognized option")):
                category = "version"
            elif any(term in detail for term in ("model", "effort", "invalid params")):
                category = "configuration"
            else:
                category = "request"
        messages = {
            "limit": "account usage limit reached; check the client's usage/reset time or wait for the allowance to reset",
            "login": f"subscription login required; run {CLIENTS[self.client]} " + ("auth login" if self.client == "claudecode" else "login"),
            "access": "account access denied; check the selected model, plan and organization permissions",
            "context": "model context limit reached; send a smaller request or retrieve fewer memory pages",
            "version": "installed CLI does not support the required protocol; update the CLI",
            "configuration": "model or reasoning setting rejected; select a model and effort supported by this client",
            "timeout": f"CLI timed out after {self.timeout:g}s; increase --cli-timeout if needed",
            "output": "model output limit reached; partial reply retained",
            "request": "CLI request failed; check the client's account and connectivity in a terminal",
        }
        message = f"{self.client}: {messages[category]}; no API fallback or adapter retry"
        self.last_error = {"category": category, "message": message}
        return ProviderError(message)

    per_call_effort = True  # an agent may think harder or less than the chat

    def command(self, model, effort=None):
        if self.client == "codex":
            cmd = [self.executable, "app-server", "--stdio"]
            overrides = {'forced_login_method': '"chatgpt"', 'model_provider': '"openai"',
                         'sandbox_mode': '"read-only"', 'approval_policy': '"never"',
                         'web_search': '"disabled"', 'mcp_servers': '{}', 'notify': '[]',
                         'project_doc_max_bytes': '0'}
            for name in ("shell_tool", "hooks", "plugins", "apps", "multi_agent", "browser_use",
                         "computer_use", "image_generation", "view_image", "memories", "code_mode",
                         "code_mode_host", "code_mode_tools_only", "code_mode_tool_search", "code_mode_prewarm"):
                overrides['features.' + name] = 'false'
            for key, value in overrides.items():
                cmd += ["-c", key + "=" + value]
            return cmd
        cmd = [self.executable, "--print", "--output-format", "stream-json", "--input-format", "stream-json",
               "--verbose", "--include-partial-messages", "--tools", "", "--safe-mode",
               "--strict-mcp-config", "--no-session-persistence", "--permission-mode", "dontAsk"]
        if model:
            cmd += ["--model", model]
        if effort or self.reasoning_effort:
            cmd += ["--effort", effort or self.reasoning_effort]
        return cmd

    def _input(self, directory, blocks, images):
        if self.client == "codex":
            prompt = "".join(block["text"] for block in blocks)
            inputs = [{"type": "text", "text": prompt}]
            for number, block in enumerate(images, 1):
                source = block["source"]
                path = directory / f"image-{number}{IMAGE_SUFFIXES[source['media_type']]}"
                path.write_bytes(base64.b64decode(source["data"], validate=True))
                inputs.extend([{"type": "text", "text": f"[EternalChat image {number}]"},
                               {"type": "localImage", "path": str(path)}])
            return inputs
        content = _image_content(blocks, images)
        return (json.dumps({"type": "user", "message": {"role": "user", "content": content},
                           "parent_tool_use_id": None}, ensure_ascii=False) + "\n").encode("utf-8")

    async def _stderr(self, process, tail):
        while chunk := await process.stderr.read(4096):
            tail[:] = (tail + chunk)[-16_384:]

    async def _line(self, process):
        raw = await process.stdout.readline()
        if not raw:
            return None
        try:
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError()
            return event
        except (ValueError, UnicodeError) as exc:
            if any(term in raw.lower() for term in (b"limit", b"unauthor", b"login", b"not signed")):
                raise self._error(raw.decode("utf-8", errors="replace")) from exc
            raise ProviderError("CLI returned malformed JSON events; partial reply retained") from exc

    async def _messages(self, process, payload, tools, emit):
        async def write():
            try:
                process.stdin.write(payload)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass  # the reader reports the client's terminal error
            finally:
                process.stdin.close()
        writer = asyncio.create_task(write())
        stream = MessageStream(emit)
        try:
            while (event := await self._line(process)) is not None:
                if event.get("type") == "error":
                    raise self._error(event.get("message", ""))
                if event.get("error"):
                    raise self._error(json.dumps(event["error"]))
                # Claude can report a quota wall as a plain-text assistant
                # message before its error result. Never treat that as a reply.
                text = ""
                if event.get("type") == "assistant":
                    text = "".join(b.get("text", "") for b in event.get("message", {}).get("content", [])
                                   if b.get("type") == "text")
                elif event.get("type") == "stream_event":
                    text = event.get("event", {}).get("delta", {}).get("text", "")
                if not stream.reply.raw.strip() and not text.lstrip().startswith("{") and any(
                        term in text.lower() for term in ("hit your", "usage limit", "weekly limit", "not signed in")):
                    raise self._error(text)
                if stream.result is not None:
                    raise ProviderError("CLI returned events after its terminal result")
                stream.feed(event)
            code = await process.wait()
            result = stream.result or {}
            if stream.usage:
                emit("usage", stream.usage)
            if code or result.get("is_error") or result.get("subtype", "success") != "success":
                detail = json.dumps({key: result.get(key) for key in ("result", "errors", "error", "subtype")})
                error = self._error(detail + f" exit status {code}")
                if code:
                    error.args = (str(error) + f" (exit status {code})",)
                raise error
            if stream.stop_reason in ("max_tokens", "max_turn_requests"):
                raise self._error(category="output")
            return stream.finish(tools)
        finally:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)

    async def _codex(self, process, inputs, directory, model, tools, emit, effort=None):
        reply = ReplyStream(emit)
        usage, final, thread_id, completed = {}, None, None, None
        next_id = 0

        def limits(snapshot):
            if not isinstance(snapshot, dict):
                return
            self.rate_limits = {name: {key: window[key] for key in ("usedPercent", "resetsAt", "windowDurationMins")
                                      if type(window.get(key)) is int}
                                for name in ("primary", "secondary")
                                if isinstance(window := snapshot.get(name), dict)}

        async def send(value):
            process.stdin.write((json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"))
            await process.stdin.drain()

        async def event():
            nonlocal usage, final, completed
            frame = await self._line(process)
            if frame is None:
                raise ProviderError("Codex stream ended before its turn completed; partial reply retained")
            method, params = frame.get("method"), frame.get("params", {})
            if method and "id" in frame:
                await send({"id": frame["id"], "error": {"code": -32601, "message": "No coding tools or approvals in EternalChat"}})
                raise ProviderError("Codex requested an unsupported tool or approval; partial reply retained")
            if method == "account/rateLimits/updated":
                limits(params.get("rateLimits"))
            if method and method.startswith("item/"):
                if thread_id and params.get("threadId") != thread_id:
                    return frame
                if method == "item/agentMessage/delta":
                    reply.feed(params.get("delta"))
                elif method == "item/reasoning/summaryTextDelta":
                    emit("thinking", params.get("delta", ""))
                elif method == "item/completed" and params.get("item", {}).get("type") == "agentMessage":
                    final = params["item"].get("text")
                elif method == "item/started" and params.get("item", {}).get("type") in (
                        "commandExecution", "fileChange", "mcpToolCall", "webSearch", "imageView", "collabToolCall"):
                    raise ProviderError("Codex attempted a coding tool; only EternalChat memory tools are supported")
            elif method == "thread/tokenUsage/updated":
                usage = normalize_usage(params.get("tokenUsage", {}).get("total", {}), codex=True)
            elif method == "turn/completed" and params.get("threadId") == thread_id:
                completed = params.get("turn")
            elif method == "error":
                if params.get("willRetry"):
                    emit("status", {"phase": "retry", "message": "Client is retrying a model request"})
                else:
                    raise self._error(json.dumps(params.get("error", {})))
            return frame

        async def request(method, params):
            nonlocal next_id
            next_id += 1
            identifier = next_id
            await send({"id": identifier, "method": method, "params": params})
            while True:
                frame = await event()
                if frame.get("id") == identifier:
                    if "error" in frame:
                        raise self._error(json.dumps(frame["error"]))
                    return frame.get("result", {})

        await request("initialize", {"clientInfo": {"name": "eternalchat", "title": "EternalChat", "version": "0.1.0"}})
        await send({"method": "initialized", "params": {}})
        account = await request("account/read", {"refreshToken": True})
        if (account.get("account") or {}).get("type") != "chatgpt":
            raise self._error(category="login")
        rate = await request("account/rateLimits/read", {})
        limits(rate.get("rateLimits"))
        params = {"cwd": str(directory), "ephemeral": True, "modelProvider": "openai",
                  "approvalPolicy": "never", "sandbox": "read-only",
                  "baseInstructions": CONTRACT, "developerInstructions": ""}
        if model:
            params["model"] = model
        started = await request("thread/start", params)
        thread_id = started["thread"]["id"]
        turn = {"threadId": thread_id, "input": inputs, "summary": "auto"}
        if effort or self.reasoning_effort:
            turn["effort"] = effort or self.reasoning_effort
        await request("turn/start", turn)
        while completed is None:
            await event()
        if usage:
            emit("usage", usage)
        if completed.get("status") != "completed":
            raise self._error(json.dumps(completed.get("error", {})))
        if not isinstance(final, str):
            raise ProviderError("Codex did not produce a final reply")
        return reply.finish(final, tools, usage)

    async def complete(self, *, model, system, tools, messages, max_tokens=8192, on_event=None, purpose="turn",
                       effort=None):
        priorities = {"turn": 0, "memory": 1, "agent": 2, "summary": 3}
        if purpose not in priorities:
            raise ValueError("Unknown model request purpose")
        sink = on_event or (lambda *event: None)
        def emit(kind, value):
            if kind == "status":
                value = {**value, "client": self.client, "purpose": purpose}
            sink(kind, value)
        if self._gate.active >= self._gate.limit:
            emit("status", {"phase": "queued", "message": "Waiting for an active model request to finish"})
        async with self._gate.enter(priorities[purpose]):
            self.last_error = None
            with tempfile.TemporaryDirectory(prefix="eternalchat-cli-") as temporary:
                directory = Path(temporary)
                images = []
                conversation = _clean(messages, images)
                request = {"system": system, "tools": tools, "messages": conversation}
                if self.client == "codex":
                    request["response_token_budget"] = max_tokens
                head = CONTRACT + (IMAGE_CONTRACT if images else "") + "\n"
                payload = self._input(directory, _prompt_blocks(head, request, _last_mark(messages)), images)
                env = os.environ.copy()
                for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "OPENAI_API_KEY",
                            "CODEX_API_KEY", "OPENAI_BASE_URL",
                            "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
                    env.pop(key, None)
                if self.client == "claudecode":
                    env['CLAUDE_CODE_MAX_OUTPUT_TOKENS'] = str(max_tokens)
                try:
                    process = await asyncio.create_subprocess_exec(*self.command(model, effort), cwd=directory,
                        env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=32_000_000)
                except OSError as exc:
                    raise ProviderError(f"Could not start {self.client} CLI ({type(exc).__name__})") from exc
                tail = bytearray()
                stderr = asyncio.create_task(self._stderr(process, tail))
                try:
                    async with asyncio.timeout(self.timeout):
                        emit("status", {"phase": "started", "message": "Model request started"})
                        if self.client == "codex":
                            response = await self._codex(process, payload, directory, model, tools, emit, effort)
                        else:
                            response = await self._messages(process, payload, tools, emit)
                        self.last_error = None
                        return response
                except TimeoutError as exc:
                    raise self._error(category="timeout") from exc
                except ProviderError as exc:
                    detail = tail.decode("utf-8", errors="replace")
                    if any(term in detail.lower() for term in ("limit", "login", "unauthor", "unexpected argument")):
                        raise self._error(detail) from exc
                    if self.last_error is None:
                        self.last_error = {"category": "protocol", "message": str(exc)}
                    raise
                finally:
                    process.stdin.close()
                    # Give clients a chance to shut down on EOF. On macOS a
                    # just-exited process group can report EPERM, not ESRCH.
                    try:
                        await asyncio.wait_for(process.wait(), .2)
                    except TimeoutError:
                        pass
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        if process.returncode is None:
                            process.kill()
                    await process.wait()
                    stderr.cancel()
                    await asyncio.gather(stderr, return_exceptions=True)

