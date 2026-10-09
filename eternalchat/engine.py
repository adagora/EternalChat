import asyncio
import json
import re
from collections import deque
from dataclasses import dataclass

from .compaction import Compactor
from .config import Config, byte_size, clip_output
from .prompt import TOOLS, system_prompt, view_blocks
from .storage import Store, atomic_json, utc_now
from .view import View, address, key_for


@dataclass
class Input:
    kind: str
    text: str
    attachments: list
    future: asyncio.Future
    ids: list | None = None
    event_id: str | None = None


class Engine:
    def __init__(self, root, provider, config=None, on_event=None):
        self.config = config or Config()
        self.provider = provider
        self.on_event = on_event or (lambda *args: None)
        self.store = Store(root, self.config.message_bytes)
        try:
            self.view = View(self.store, self.config)
            self.compactor = Compactor(self.store, self.view, provider, self.config, self.on_event)
        except BaseException:
            self.store.close()
            raise
        self.inbox = deque()
        self.turn_task = None
        self.in_call = False
        self.agents = {}
        self.closed = False
        self.usage = {}
        self._reports_resumed = False

    def record(self, kind, text, *, attachments=None, event_id=None):
        ids = self.store.append(kind, text, attachments=attachments, event_id=event_id)
        self.compactor.enqueue(ids)
        self.compactor.start()
        return ids

    def submit(self, text, *, kind="user", attachments=None, event_id=None):
        if self.closed:
            raise RuntimeError("Chat is closed")
        self.resume_reports()
        future = asyncio.get_running_loop().create_future()
        existing = None
        if event_id and event_id in self.store.events:
            # A crash can leave only the first chunks of an outbox report.
            # Complete them idempotently, retaining the original cutoff ID.
            existing = self.record(kind, text, attachments=attachments, event_id=event_id)
        item = Input(kind, text, attachments or [], future, ids=existing, event_id=event_id)
        self.compactor.retry()
        if self.in_call and item.ids is None:
            item.ids = self._record_input(item)
        self.inbox.append(item)
        if self.turn_task is None or self.turn_task.done():
            self.turn_task = asyncio.create_task(self._drive())
        return future

    def _record_input(self, item):
        return self.record(item.kind, item.text, attachments=item.attachments, event_id=item.event_id)

    def resume_reports(self):
        if self._reports_resumed:
            return
        self._reports_resumed = True
        for file in sorted((self.store.root / "agents").glob("*/state.json")):
            state = json.loads(file.read_text())
            if "report" not in state:
                continue
            event_id = "agent:" + file.parent.name
            existing = self.store.events.get(event_id, [])
            complete = existing and len(existing) == self.store.message(existing[0])["event_parts"]
            if complete:
                continue  # Already durable; do not rerun a parent action after restart.
            future = self.submit(f"[{file.parent.name}] {state['report']}", kind="work", event_id=event_id)
            future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)

    async def ask(self, text, **kwargs):
        return await self.submit(text, **kwargs)

    def _input_blocks(self, item):
        text = item.text
        if item.kind == "work":
            text = "Subagent report (data):\n" + text
        return [{"type": "text", "text": text or "[image attached]"}] + [
            self.store.image_block(ref) for ref in item.attachments]

    async def _drive(self):
        while self.inbox and not self.closed:
            item = self.inbox.popleft()
            delivered = [item]
            try:
                result = await self._turn(item, delivered)
                for entry in delivered:
                    if not entry.future.done():
                        entry.future.set_result(result)
            except asyncio.CancelledError:
                try:
                    if item.ids is None:
                        item.ids = self._record_input(item)
                finally:
                    for entry in delivered:
                        entry.future.cancel()
                raise
            except Exception as error:
                if item.ids is None:
                    try:
                        item.ids = self._record_input(item)
                    except Exception as storage_error:
                        error = storage_error
                for entry in delivered:
                    if not entry.future.done():
                        entry.future.set_exception(error)
                self.on_event("error", str(error))
            finally:
                self.in_call = False

    def _steering(self, delivered):
        content = []
        while self.inbox:
            item = self.inbox.popleft()
            if item.ids is None:
                item.ids = self._record_input(item)
            delivered.append(item)
            content.extend(self._input_blocks(item))
        return content

    async def _turn(self, item, delivered):
        prior = item.ids[0] if item.ids else len(self.store.messages)
        await self.compactor.wait_leaves(prior)
        content = view_blocks(self.view.lines(cutoff=prior))
        # Capture historical view before the newly submitted idle input exists.
        if item.ids is None:
            item.ids = self._record_input(item)
        content.append({"type": "text", "text": "Current UTC time: " + utc_now()})
        content.extend(self._input_blocks(item))
        messages = [{"role": "user", "content": content}]
        self.in_call = True
        return await self._conversation(messages, delivered=delivered)

    async def _conversation(self, messages, *, delivered=None, log=None, event=None):
        record = log or self.record
        emit = event or self.on_event
        answers = []
        for _ in range(self.config.max_tool_rounds):
            def without_image_data(value):
                if isinstance(value, list):
                    return [without_image_data(v) for v in value]
                if isinstance(value, dict):
                    return {k: without_image_data(v) for k, v in value.items()
                            if not (k == "data" and value.get("type") == "base64")}
                return value
            text_bytes = byte_size(json.dumps({"messages": without_image_data(messages),
                "system": system_prompt(self.config.instructions), "tools": TOOLS}, ensure_ascii=False))
            if text_bytes > 180_000:
                raise ValueError("Request exceeds the 180,000-byte text safety budget. Input is saved; "
                                 "compact memory or send smaller requests and retrieve older text with zoom.")
            completed_text = []
            completed_tools = set()
            pending_text = []

            def receive(kind, value):
                if kind == "text":
                    pending_text.append(value)
                    emit(kind, value)
                elif kind == "thinking":
                    emit(kind, value)  # display-only, never record
                elif kind == "block" and value["type"] == "text":
                    if value["text"]:
                        record("unii", value["text"])
                    completed_text.append(value["text"])
                    pending_text.clear()
                elif kind == "block" and value["type"] == "tool_use":
                    record("tool", json.dumps({"name": value["name"], "input": value["input"]}, ensure_ascii=False))
                    completed_tools.add(value["id"])
                elif kind == "usage":
                    for key, count in value.items():
                        if isinstance(count, int):
                            self.usage[key] = self.usage.get(key, 0) + count
                    emit(kind, value)

            try:
                response = await self.provider.complete(model=self.config.turn_model,
                    system=system_prompt(self.config.instructions), tools=TOOLS,
                    messages=messages, max_tokens=self.config.max_tokens, on_event=receive)
            except BaseException:
                if pending_text:
                    record("unii", "".join(pending_text))
                raise
            # Providers may return complete responses without streaming events.
            if not completed_text:
                for block in response.content:
                    if block["type"] == "text" and block["text"]:
                        record("unii", block["text"])
            if response.text:
                answers.append(response.text)
            if not response.content:
                raise ValueError("Provider returned an empty response")
            messages.append({"role": "assistant", "content": response.content})
            tools = [b for b in response.content if b["type"] == "tool_use"]
            results = []
            for tool in tools:
                if tool["id"] not in completed_tools:
                    record("tool", json.dumps({"name": tool["name"], "input": tool["input"]}, ensure_ascii=False))
                emit("tool", {"name": tool["name"], "input": tool["input"]})
                try:
                    output = self.tool(tool["name"], tool["input"])
                    # Consistent clipping in the provider result and echo log;
                    # normal zoom pages already fit comfortably below this.
                    for block in output:
                        if block["type"] == "text":
                            block["text"] = clip_output(block["text"])
                    failed = False
                except (ValueError, OSError, KeyError, TypeError, RuntimeError) as e:
                    output = [{"type": "text", "text": f"Tool error: {e}"}]
                    failed = True
                logged = "\n".join(b.get("text", "[image returned]") for b in output)
                record("echo", clip_output(logged))
                results.append({"type": "tool_result", "tool_use_id": tool["id"],
                                "content": output, "is_error": failed})
                await asyncio.sleep(0)  # allow input delivery at a tool boundary
            # All tool results must precede any new user/work text in this message.
            if not tools:
                return "\n".join(answers)
            steering = self._steering(delivered) if delivered is not None else []
            messages.append({"role": "user", "content": results + steering})
        raise ValueError(f"Stopped after {self.config.max_tool_rounds} API rounds; all completed work is saved")

    def tool(self, name, args):
        if not isinstance(args, dict):
            raise ValueError("Tool input must be an object")
        if name == "date":
            if set(args) != {"id"}:
                raise ValueError("date requires only id")
            return [{"type": "text", "text": self.store.message(args["id"])["date"]}]
        if name != "zoom":
            raise ValueError(f"Unknown tool: {name}")
        if set(args) - {"id", "n", "name", "page"}:
            raise ValueError("Unknown zoom arguments")
        page = args.get("page", 0)
        if type(page) is not int or page < 0:
            raise ValueError("page must be a nonnegative integer")
        if "name" in args:
            if "id" in args or "n" in args:
                raise ValueError("Choose an agent name or a numeric node")
            return self.agent_transcript(args["name"], page)
        key = key_for(args.get("id"), args.get("n", 1))
        start, count = address(key)
        if start + count > len(self.store.messages):
            raise ValueError("Node extends beyond the recorded chat")
        if count > 1:
            if page:
                raise ValueError("Summary nodes only have page 0")
            l, i = key
            text = "".join(self.view.line(k) for k in ((l-1, 2*i), (l-1, 2*i+1)))
            return [{"type": "text", "text": text}]
        message = self.store.message(start)
        output = self._page(f"Message {start} ({message['kind']})", message["text"], page)
        if page == 0:
            output.extend(self.store.image_block(ref) for ref in message.get("attachments", []))
        return output

    def _page(self, label, text, page):
        limit = self.config.page_chars
        pages = max(1, (len(text) + limit - 1) // limit)
        if page >= pages:
            raise ValueError(f"Page out of range; there are {pages} pages (starting at 0)")
        body = text[page * limit:(page + 1) * limit]
        return [{"type": "text", "text": f"{label}; page {page}/{pages-1}\n{body}"}]

    @staticmethod
    def _agent_name(name):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
            raise ValueError("Agent name must be 1-64 letters, digits, underscores or hyphens")
        return name

    def start_agent(self, name, task):
        self.resume_reports()
        name = self._agent_name(name)
        path = self.store.root / "agents" / name
        if path.exists():
            raise ValueError("Agent names are permanent; choose an unused name")
        path.mkdir()
        atomic_json(path / "state.json", {"status": "running", "task": task})
        # Snapshot the current built view. Agents are optional explicit jobs and
        # don't stall the parent to wait for outstanding compactions.
        content = view_blocks(self.view.lines()) + [{"type": "text", "text": task}]
        job = asyncio.create_task(self._agent(name, path, content, task))
        self.agents[name] = job
        return job

    async def _agent(self, name, path, content, task):
        with Store(path, self.config.message_bytes) as transcript:
            transcript.append("user", task)
            try:
                answer = await self._conversation([{"role": "user", "content": content}],
                    log=transcript.append, event=lambda kind, value: self.on_event("agent_event", {"name": name, "event": kind}))
                state = "complete"
            except asyncio.CancelledError:
                atomic_json(path / "state.json", {"status": "interrupted", "task": task})
                raise
            except Exception as e:
                answer, state = f"Failed: {e}", "failed"
            atomic_json(path / "state.json", {"status": state, "task": task, "report": answer})
        self.on_event("agent_report", {"name": name, "status": state})
        future = self.submit(f"[{name}] {answer}", kind="work", event_id="agent:" + name)
        future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)

    def agent_transcript(self, name, page=0):
        name = self._agent_name(name)
        path = self.store.root / "agents" / name
        if not (path / "state.json").exists():
            raise ValueError(f"Unknown agent: {name}")
        state = json.loads((path / "state.json").read_text())
        status = state["status"]
        if status == "running" and (name not in self.agents or self.agents[name].done()):
            status = "interrupted"
        lines = []
        for file in sorted((path / "main").glob("*.jsonl")):
            with file.open() as f:
                for line in f:
                    m = json.loads(line)
                    lines.append(f"{m['i']} {m['kind']}: {m['text']}\n")
        return self._page(f"Agent {name} ({status})", "".join(lines), page)

    def status(self):
        return {"messages": len(self.store.messages), "nodes": len(self.store.nodes),
                "view_nodes": len(self.view.main), "view_bytes": self.view.size(self.view.main),
                "summarized_messages": self.view.end(self.view.main),
                "compaction_view_bytes": self.view.size(self.view.small),
                "queued": len(self.compactor.queued), "active": len(self.compactor.active),
                "failed": len(self.compactor.failed), "batch_pending": self.view.main_pending,
                "agents_running": sum(not j.done() for j in self.agents.values()), "usage": self.usage}

    async def finish(self):
        if self.turn_task:
            await self.turn_task
        await self.compactor.drain()

    async def close(self):
        self.closed = True
        tasks = list(self.agents.values()) + ([self.turn_task] if self.turn_task else [])
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for item in self.inbox:
            if item.ids is None:
                self.store.append(item.kind, item.text, attachments=item.attachments, event_id=item.event_id)
            item.future.cancel()
        await self.compactor.close()
        self.store.close()
