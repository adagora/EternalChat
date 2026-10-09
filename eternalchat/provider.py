"""Async provider boundary and a dependency-free Anthropic SSE transport."""
import asyncio
import copy
import hashlib
import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .config import byte_prefix


class ProviderError(RuntimeError):
    pass


@dataclass
class Response:
    content: list
    stop_reason: str = "end_turn"
    usage: dict = field(default_factory=dict)

    @property
    def text(self):
        return "".join(b["text"] for b in self.content if b["type"] == "text")


def request_body(model, system, tools, messages, max_tokens):
    messages = copy.deepcopy(messages)
    # Retain the view breakpoint, but move the per-request end breakpoint.
    for message in messages:
        for block in message["content"]:
            if block.pop("_request_end", False):
                block.pop("cache_control", None)
    last = messages[-1]["content"][-1]
    last["cache_control"] = {"type": "ephemeral"}
    return {"model": model, "max_tokens": max_tokens, "stream": True,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "tools": tools, "messages": messages}


def prefix_key(body):
    # The first message contains the persisted view and fresh task. Its last
    # whole four-line block is the reusable view prefix (exclude request end).
    blocks = body["messages"][0]["content"]
    reusable = []
    prefix = []
    for block in blocks[:-1]:
        prefix.append({k: v for k, v in block.items() if k != "cache_control"})
        if "cache_control" in block:
            reusable = prefix.copy()
    raw = json.dumps([body["model"], body["tools"], body["system"], reusable],
                     sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


class AnthropicProvider:
    def __init__(self, api_key, *, endpoint="https://api.anthropic.com/v1/messages", timeout=120):
        if not api_key:
            raise ProviderError("Set ANTHROPIC_API_KEY, or use --demo for the offline demonstration.")
        self._api_key = api_key
        self.endpoint, self.timeout = endpoint, timeout
        self._writing = {}

    async def complete(self, *, model, system, tools, messages, max_tokens=8192, on_event=None):
        body = request_body(model, system, tools, messages, max_tokens)
        key = prefix_key(body)
        while key in self._writing:
            await self._writing[key].wait()
        started = asyncio.Event()
        self._writing[key] = started

        def release():
            if self._writing.get(key) is started:
                self._writing.pop(key)
            started.set()

        loop = asyncio.get_running_loop()
        events = asyncio.Queue()
        stopped = threading.Event()

        def post(event):
            if not stopped.is_set():
                try:
                    loop.call_soon_threadsafe(events.put_nowait, event)
                except RuntimeError:
                    pass  # the owning loop may have closed during cancellation

        def read_stream():
            req = urllib.request.Request(self.endpoint, data=json.dumps(body).encode(),
                headers={"x-api-key": self._api_key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json", "accept": "text/event-stream"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    data = []
                    for raw in response:
                        if stopped.is_set():
                            break
                        line = raw.decode("utf-8").rstrip("\r\n")
                        if not line:
                            if data:
                                post(json.loads("\n".join(data)))
                                data = []
                        elif line.startswith("data:"):
                            data.append(line[5:].lstrip())
                    if data:
                        post(json.loads("\n".join(data)))
            except urllib.error.HTTPError as e:
                # Don't dump bodies/headers: gateways may reflect credentials.
                post({"type": "transport_error", "message": f"Anthropic HTTP {e.code}; request was not retried"})
                e.close()
            except Exception as e:
                post({"type": "transport_error", "message": f"Anthropic transport failed ({type(e).__name__}); request was not retried"})
            finally:
                post({"type": "eof"})

        # urllib is blocking. A daemon reader prevents asyncio.run() from waiting
        # on its default executor until a stalled socket times out after Ctrl-C.
        # The reader owns no storage and post() is disabled when this call ends.
        threading.Thread(target=read_stream, name="eternalchat-http", daemon=True).start()
        blocks, partial_json, usage = {}, {}, {}
        stop_reason = None
        finished = False
        try:
            while True:
                event = await events.get()
                kind = event["type"]
                if kind == "message_start":
                    release()
                    usage.update(event["message"].get("usage", {}))
                elif kind == "content_block_start":
                    i = event["index"]
                    blocks[i] = event["content_block"].copy()
                    partial_json[i] = ""
                elif kind == "content_block_delta":
                    i, delta = event["index"], event["delta"]
                    delta_type = delta["type"]
                    if delta_type == "text_delta":
                        blocks[i]["text"] = blocks[i].get("text", "") + delta["text"]
                        if on_event:
                            on_event("text", delta["text"])
                    elif delta_type == "thinking_delta":
                        blocks[i]["thinking"] = blocks[i].get("thinking", "") + delta["thinking"]
                        if on_event:
                            on_event("thinking", delta["thinking"])
                    elif delta_type == "signature_delta":
                        blocks[i]["signature"] = blocks[i].get("signature", "") + delta["signature"]
                    elif delta_type == "input_json_delta":
                        partial_json[i] += delta["partial_json"]
                elif kind == "content_block_stop":
                    i = event["index"]
                    if blocks[i]["type"] == "tool_use" and partial_json[i]:
                        blocks[i]["input"] = json.loads(partial_json[i])
                    if on_event:
                        on_event("block", blocks[i].copy())
                elif kind == "message_delta":
                    stop_reason = event["delta"].get("stop_reason")
                    usage.update(event.get("usage", {}))
                elif kind == "message_stop":
                    finished = True
                elif kind in ("error", "transport_error"):
                    if kind == "transport_error":
                        raise ProviderError(event["message"])
                    raise ProviderError("Anthropic stream error; partial output retained, request was not retried")
                elif kind == "eof":
                    break
            if not finished or not stop_reason:
                raise ProviderError("Incomplete Anthropic stream; request was not retried")
            if stop_reason in ("max_tokens", "refusal", "model_context_window_exceeded"):
                raise ProviderError(f"Anthropic stopped with {stop_reason}; partial output retained")
            if stop_reason not in ("end_turn", "tool_use"):
                raise ProviderError(f"Unsupported Anthropic stop reason: {stop_reason}")
            if on_event:
                on_event("usage", usage)
            return Response([blocks[i] for i in sorted(blocks)], stop_reason, usage)
        except (ValueError, KeyError, TypeError) as e:
            raise ProviderError("Malformed Anthropic stream; request was not retried") from e
        finally:
            release()
            stopped.set()
            # On cancellation urllib exits at the next frame or socket timeout.
            # It owns no store handles; queued events can no longer mutate chat.


class DemoProvider:
    """Deterministic offline exercise of the engine; deliberately not an LLM."""
    async def complete(self, *, model, system, tools, messages, max_tokens=8192, on_event=None):
        await asyncio.sleep(0)
        blocks = messages[-1]["content"]
        text = next((b["text"] for b in reversed(blocks) if b["type"] == "text"), "")
        if "Compaction:" in text or text.startswith("Too long:"):
            source = text.split("<input>\n", 1)[-1].split("\n</input>", 1)[0]
            result = byte_prefix(source.replace("\n", " "), 480)
        elif any(b["type"] == "tool_result" for b in blocks):
            result = "[Offline demo] Retrieved: " + " ".join(
                c.get("text", "") for b in blocks if b["type"] == "tool_result" for c in b["content"])
        elif text.strip().startswith("/recall "):
            i = int(text.strip().split()[-1])
            block = {"type": "tool_use", "id": "demo_zoom", "name": "zoom", "input": {"id": i, "n": 1}}
            if on_event:
                on_event("block", block)
            return Response([block], "tool_use")
        else:
            result = "[Offline demo] Saved your message: " + text.split("\n\n", 1)[-1]
        block = {"type": "text", "text": result}
        if on_event:
            on_event("text", result)
            on_event("block", block)
        return Response([block])
