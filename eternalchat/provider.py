"""Anthropic Messages adapter: HTTPS and server-sent events with only the standard library."""
import asyncio
import copy
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request

from .errors import ProviderError
from .llm import Response

API_VERSION = "2023-06-01"
# Ephemeral cache entries live five minutes from last use; stay safely inside.
CACHE_SECONDS = 270
ERROR_TYPE = re.compile(r"[a-z_]{1,64}")


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: urllib would resend x-api-key to the new host.
    Returning None makes the redirect surface as an HTTPError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_RefuseRedirects)


def urlopen(request, timeout):
    return _OPENER.open(request, timeout=timeout)


def request_body(model, system, tools, messages, max_tokens):
    """The streamed request: cache marks on the system prompt, the view block that
    view_blocks marked, and the end of this request. The caller's messages stay unchanged."""
    messages = copy.deepcopy(messages)
    messages[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}
    return {"model": model, "max_tokens": max_tokens, "stream": True,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "tools": tools, "messages": messages}


def prefix_key(body):
    """Identity of the reusable cached prefix: model, tools, system and the first
    message's blocks up to its last cache mark before the request end."""
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


def error_type(payload):
    """The API's error type (e.g. overloaded_error), and nothing else from the body:
    messages and bodies may echo request content or credentials."""
    try:
        kind = payload["error"]["type"]
    except (KeyError, TypeError):
        return None
    return kind if isinstance(kind, str) and ERROR_TYPE.fullmatch(kind) else None


def _http_error_type(error):
    try:
        return error_type(json.loads(error.read(65_536)))
    except Exception:
        return None


class StreamDecoder:
    """Folds Anthropic stream events into a Response.

    feed() returns the (kind, value) events to show; it raises ValueError,
    KeyError or TypeError on malformed events and ProviderError on API errors.
    """

    def __init__(self):
        self.started = False
        self._blocks, self._json, self._usage = {}, {}, {}
        self._open_blocks = set()
        self._stop_reason = None
        self._finished = False

    def feed(self, event):
        if not isinstance(event, dict) or self._finished:
            raise ValueError("invalid event or event after message_stop")
        kind = event["type"]
        if kind in ("content_block_start", "content_block_delta", "content_block_stop",
                    "message_delta", "message_stop") and not self.started:
            raise ValueError("missing message_start")
        if kind in ("content_block_start", "content_block_delta", "content_block_stop"):
            i = event["index"]
            if type(i) is not int or i < 0 or self._stop_reason is not None:
                raise ValueError("invalid content block index or order")
        if kind == "message_start":
            if self.started or not isinstance(event["message"], dict):
                raise ValueError("invalid message_start")
            self.started = True
            self._usage.update(event["message"].get("usage", {}))
        elif kind == "content_block_start":
            block = event["content_block"]
            if i in self._blocks or not isinstance(block, dict) or not isinstance(block.get("type"), str):
                raise ValueError("invalid or repeated content block")
            self._blocks[i] = block.copy()
            self._json[i] = ""
            self._open_blocks.add(i)
            field = {"text": "text", "thinking": "thinking"}.get(block["type"])
            if field:
                if not isinstance(block.get(field), str):
                    raise ValueError("invalid content block text")
                if block[field]:
                    return [(field, block[field])]
        elif kind == "content_block_delta":
            if i not in self._open_blocks:
                raise ValueError("delta for a missing or finished block")
            return self._delta(i, event["delta"])
        elif kind == "content_block_stop":
            if i not in self._open_blocks:
                raise ValueError("stop for a missing or finished block")
            block = self._blocks[i]
            if block["type"] == "tool_use":
                if self._json[i]:
                    block["input"] = json.loads(self._json[i])
                if (not isinstance(block.get("input"), dict)
                        or not isinstance(block.get("id"), str) or not block["id"]
                        or not isinstance(block.get("name"), str) or not block["name"]):
                    raise ValueError("invalid tool call")
            self._open_blocks.remove(i)
            return [("block", block.copy())]
        elif kind == "message_delta":
            if self._open_blocks or not isinstance(event["delta"], dict):
                raise ValueError("message ended before its blocks")
            reason = event["delta"].get("stop_reason")
            if reason is not None:
                self._stop_reason = reason
            self._usage.update(event.get("usage", {}))
        elif kind == "message_stop":
            self._finished = True
        elif kind == "error":
            detail = error_type(event)
            raise ProviderError("Anthropic stream error" + (f" ({detail})" if detail else "") +
                                "; partial output retained, request was not retried")
        return []

    def _delta(self, i, delta):
        block, kind = self._blocks[i], delta["type"]
        expected = {"text_delta": "text", "thinking_delta": "thinking",
                    "signature_delta": "thinking", "input_json_delta": "tool_use"}
        if kind in expected and block["type"] != expected[kind]:
            raise ValueError("delta type does not match its block")
        if kind == "text_delta":
            block["text"] = block.get("text", "") + delta["text"]
            return [("text", delta["text"])]
        if kind == "thinking_delta":
            block["thinking"] = block.get("thinking", "") + delta["thinking"]
            return [("thinking", delta["thinking"])]
        if kind == "signature_delta":
            block["signature"] = block.get("signature", "") + delta["signature"]
        elif kind == "input_json_delta":
            self._json[i] += delta["partial_json"]
        return []

    def response(self):
        if not self.started or not self._finished or not self._stop_reason or self._open_blocks or not self._blocks:
            raise ProviderError("Incomplete Anthropic stream; request was not retried")
        if self._stop_reason in ("max_tokens", "refusal", "model_context_window_exceeded"):
            raise ProviderError(f"Anthropic stopped with {self._stop_reason}; partial output retained")
        if self._stop_reason not in ("end_turn", "tool_use"):
            raise ProviderError(f"Unsupported Anthropic stop reason: {self._stop_reason}")
        return Response([self._blocks[i] for i in sorted(self._blocks)], self._stop_reason, self._usage)

    @property
    def usage(self):
        return dict(self._usage)

    @property
    def finished(self):
        return self._finished


class _Gate:
    """Open once the first request with a prefix starts its response (cache written)."""

    def __init__(self):
        self.event = asyncio.Event()
        self.warmed = False


class AnthropicProvider:
    def __init__(self, api_key, *, endpoint="https://api.anthropic.com/v1/messages", timeout=120):
        if not api_key:
            raise ProviderError("Set ANTHROPIC_API_KEY, select --connection subscription, or use --demo for the offline demonstration.")
        self._api_key = api_key
        self.endpoint, self.timeout = endpoint, timeout
        self._writing = {}  # prefix key -> _Gate of the request writing that cache entry
        self._warm = {}  # prefix key -> monotonic time until which it is assumed cached

    async def _enter(self, key):
        """Wait while another request writes this prefix's cache.

        Returns a gate this call must open, or None when it can go at once: the
        prefix is warm, or the writer it waited for has started its response.
        """
        while True:
            if self._warm.get(key, 0) > time.monotonic():
                return None
            gate = self._writing.get(key)
            if gate is None:
                gate = self._writing[key] = _Gate()
                return gate
            await gate.event.wait()
            if gate.warmed:
                return None  # every waiter proceeds together, not one by one

    def _open(self, key, gate, warmed):
        if self._writing.get(key) is gate:
            del self._writing[key]
        if warmed:
            now = time.monotonic()
            self._warm = {k: t for k, t in self._warm.items() if t > now}
            self._warm[key] = now + CACHE_SECONDS
        gate.warmed = warmed
        gate.event.set()

    async def complete(self, *, model, system, tools, messages, max_tokens=8192, on_event=None, purpose="turn"):
        body = request_body(model, system, tools, messages, max_tokens)
        key = prefix_key(body)
        gate = await self._enter(key)
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
                headers={"x-api-key": self._api_key, "anthropic-version": API_VERSION,
                         "content-type": "application/json", "accept": "text/event-stream"})
            try:
                with urlopen(req, timeout=self.timeout) as response:
                    data = []
                    for raw in response:
                        if stopped.is_set():
                            break
                        line = raw.decode("utf-8").rstrip("\r\n")
                        if not line:
                            if data:
                                event = json.loads("\n".join(data))
                                post(event)
                                data = []
                                if isinstance(event, dict) and event.get("type") == "message_stop":
                                    return  # completion does not depend on the socket closing
                        elif line.startswith("data:"):
                            data.append(line[5:].lstrip())
                    if data:
                        post(json.loads("\n".join(data)))
            except urllib.error.HTTPError as e:
                # Report only the status and error type: bodies, headers and
                # messages may reflect credentials or request content.
                detail = _http_error_type(e)
                post({"type": "transport_error", "message": f"Anthropic HTTP {e.code}" +
                      (f" ({detail})" if detail else "") + "; request was not retried"})
                e.close()
            except Exception as e:
                post({"type": "transport_error",
                      "message": f"Anthropic transport failed ({type(e).__name__}); request was not retried"})
            finally:
                post({"type": "eof"})

        decoder = StreamDecoder()
        try:
            # urllib blocks. A daemon reader keeps asyncio.run() from waiting on
            # a stalled socket after Ctrl-C. It owns no storage, and post() is
            # disabled once this call ends.
            threading.Thread(target=read_stream, name="eternalchat-http", daemon=True).start()
            while True:
                event = await events.get()
                if not isinstance(event, dict):
                    raise ProviderError("Malformed Anthropic stream; request was not retried")
                if event.get("type") == "eof":
                    break
                if event.get("type") == "transport_error":
                    raise ProviderError(event["message"])
                try:
                    shown = decoder.feed(event)
                except (ValueError, KeyError, TypeError) as e:
                    raise ProviderError("Malformed Anthropic stream; request was not retried") from e
                if gate and decoder.started:
                    self._open(key, gate, warmed=True)
                    gate = None
                if on_event:
                    for kind, value in shown:
                        on_event(kind, value)
                if decoder.finished:
                    break
            response = decoder.response()
            if on_event:
                on_event("usage", decoder.usage)
            return response
        finally:
            if gate:
                self._open(key, gate, warmed=False)  # the next waiter becomes the writer
            stopped.set()
