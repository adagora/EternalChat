"""Subscription wire decoding. Only user-facing text is streamed from the JSON reply."""
import json
import uuid

from .errors import ProviderError
from .llm import Response


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def decode_reply(raw, tools):
    try:
        value = json.loads(raw, object_pairs_hook=_object)
        if not isinstance(value, dict) or set(value) != {"text", "tool_calls"}:
            raise ValueError()
        if not isinstance(value["text"], str) or not isinstance(value["tool_calls"], list):
            raise ValueError()
        value["text"].encode("utf-8")
        names = {tool["name"] for tool in tools}
        content = []
        if value["text"]:
            content.append({"type": "text", "text": value["text"]})
        for call in value["tool_calls"]:
            if (not isinstance(call, dict) or set(call) != {"name", "arguments"}
                    or not isinstance(call["name"], str) or call["name"] not in names
                    or not isinstance(call["arguments"], dict)):
                raise ValueError()
            content.append({"type": "tool_use", "id": "cli_" + uuid.uuid4().hex,
                            "name": call["name"], "input": call["arguments"]})
        if not content:
            raise ValueError()
        return Response(content, "tool_use" if value["tool_calls"] else "end_turn")
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise ProviderError("CLI returned an invalid EternalChat response; request was not retried") from exc


class ReplyStream:
    """Incrementally decode just the top-level `text` string, including split escapes.

    Tool calls are validated and executed only after the entire reply succeeds.
    The final native result is checked against the text already shown to the user.
    """
    def __init__(self, emit):
        self.emit = emit
        self.raw = ""
        self.text = ""
        self.depth = 0
        self.phase = "start"
        self.target = None
        self.key = ""
        self.keys = set()
        self.escape = False
        self.digits = None
        self.surrogate = None

    def feed(self, delta):
        if not isinstance(delta, str):
            raise ProviderError("CLI returned malformed text events")
        self.raw += delta
        output = []

        def char(value):
            if self.target == "key":
                self.key += value
            elif self.target == "text":
                number = ord(value)
                if self.surrogate is not None:
                    if not 0xDC00 <= number <= 0xDFFF:
                        raise ProviderError("CLI returned invalid Unicode in its reply")
                    value = chr(0x10000 + ((self.surrogate - 0xD800) << 10) + number - 0xDC00)
                    self.surrogate = None
                elif 0xD800 <= number <= 0xDBFF:
                    self.surrogate = number
                    return
                elif 0xDC00 <= number <= 0xDFFF:
                    raise ProviderError("CLI returned invalid Unicode in its reply")
                output.append(value)

        for c in delta:
            if self.target is not None:
                if self.digits is not None:
                    if c not in "0123456789abcdefABCDEF":
                        raise ProviderError("CLI returned invalid JSON escapes")
                    self.digits += c
                    if len(self.digits) == 4:
                        char(chr(int(self.digits, 16)))
                        self.digits = None
                elif self.escape:
                    self.escape = False
                    if c == "u":
                        self.digits = ""
                    elif c in '"\\/bfnrt':
                        char({'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t'}.get(c, c))
                    else:
                        raise ProviderError("CLI returned invalid JSON escapes")
                elif c == "\\":
                    self.escape = True
                elif c == '"':
                    if self.surrogate is not None:
                        raise ProviderError("CLI returned invalid Unicode in its reply")
                    if self.target == "key":
                        if self.key in self.keys:
                            raise ProviderError("CLI returned duplicate response fields")
                        self.keys.add(self.key)
                        self.phase = "colon"
                    elif self.depth == 1:
                        self.phase = "comma"
                    self.target = None
                elif ord(c) < 32:
                    raise ProviderError("CLI returned invalid JSON text")
                else:
                    char(c)
                continue
            if c.isspace():
                continue
            if self.phase == "start" and c != "{":
                raise ProviderError("CLI returned an invalid EternalChat response; request was not retried")
            if c == '"':
                self.target = "skip"
                if self.depth == 1:
                    if self.phase == "key":
                        self.target, self.key = "key", ""
                    elif self.phase == "value" and self.key == "text":
                        self.target = "text"
            elif c in "{[":
                self.depth += 1
                if self.depth == 1:
                    self.phase = "key"
            elif c in "}]":
                self.depth -= 1
                if self.depth <= 1:
                    self.phase = "comma"
            elif self.depth == 1 and c == ":":
                self.phase = "value"
            elif self.depth == 1 and c == ",":
                self.phase = "key"
        if output:
            delta = "".join(output)
            self.text += delta
            self.emit("text", delta)

    def finish(self, raw, tools, usage):
        response = decode_reply(raw, tools)
        if not response.text.startswith(self.text):
            raise ProviderError("CLI changed a reply that was already streamed; partial reply retained")
        rest = response.text[len(self.text):]
        if rest:
            self.text += rest
            self.emit("text", rest)
        response.usage = usage
        for block in response.content:
            self.emit("block", block)
        return response


def normalize_usage(value, *, codex=False):
    if not isinstance(value, dict):
        return {}
    if codex:
        mapping = {"inputTokens": "input_tokens", "outputTokens": "output_tokens",
                   "cachedInputTokens": "cache_read_input_tokens", "cacheWriteInputTokens": "cache_creation_input_tokens",
                   "reasoningOutputTokens": "reasoning_tokens"}
    else:
        mapping = {key: key for key in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                   "cache_creation_input_tokens", "reasoning_tokens")}
    result = {target: value[key] for key, target in mapping.items()
              if type(value.get(key)) is int and value[key] >= 0}
    if codex and "input_tokens" in result:
        # Separate cache reads/writes from uncached input, as Anthropic does.
        result["input_tokens"] = max(0, result["input_tokens"] - result.get("cache_read_input_tokens", 0)
                                     - result.get("cache_creation_input_tokens", 0))
    return result


class MessageStream:
    """Claude Code's Messages-compatible event stream."""
    def __init__(self, emit):
        self.emit = emit
        self.reply = ReplyStream(emit)
        self.result = None
        self.raw = ""
        self.usage = {}
        self.stop_reason = None
        self.thinking_streamed = False
        self.message_usage = {}
        self.message_id = None

    def feed(self, frame):
        if not isinstance(frame, dict):
            raise ProviderError("CLI returned malformed stream events")
        kind = frame.get("type")
        if kind == "stream_event":
            event = frame.get("event", {})
            if not isinstance(event, dict):
                raise ProviderError("CLI returned malformed stream events")
            kind = event.get("type")
            if kind == "message_start":
                message = event.get("message", {})
                self.message_id = message.get("id", str(len(self.message_usage)))
                self.message_usage[self.message_id] = normalize_usage(message.get("usage"))
            elif kind == "content_block_start":
                block = event.get("content_block", {})
                if block.get("type") == "text" and block.get("text"):
                    self.reply.feed(block["text"])
            elif kind == "content_block_delta":
                delta = event.get("delta", {})
                if not isinstance(delta, dict):
                    raise ProviderError("CLI returned malformed stream events")
                if delta.get("type") == "text_delta":
                    self.reply.feed(delta.get("text"))
                elif delta.get("type") == "thinking_delta":
                    self.thinking_streamed = True
                    self.emit("thinking", delta.get("thinking", ""))
            elif kind == "message_delta":
                self.stop_reason = event.get("delta", {}).get("stop_reason")
                if self.message_id is not None:
                    self.message_usage[self.message_id].update(normalize_usage(event.get("usage")))
        elif kind == "assistant":
            message = frame.get("message", {})
            blocks = message.get("content", [])
            identifier = message.get("id", self.message_id or "0")
            self.message_usage.setdefault(identifier, {}).update(normalize_usage(message.get("usage")))
            raw = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
            if raw:
                if raw.startswith(self.reply.raw):
                    self.reply.feed(raw[len(self.reply.raw):])
                self.raw = raw
            if not self.thinking_streamed:
                for block in blocks:
                    if block.get("type") == "thinking":
                        self.emit("thinking", block.get("thinking", ""))
            self.stop_reason = frame.get("message", {}).get("stop_reason", self.stop_reason)
        elif kind == "result":
            if self.result is not None:
                raise ProviderError("CLI returned multiple terminal results")
            self.result = frame
            self.usage = normalize_usage(frame.get("usage"))
            if not self.usage:
                for values in self.message_usage.values():
                    for key, count in values.items():
                        self.usage[key] = self.usage.get(key, 0) + count
            self.stop_reason = frame.get("stop_reason", self.stop_reason)
        elif kind == "system" and frame.get("subtype") == "api_retry":
            self.emit("status", {"phase": "retry", "message": "Client is retrying a model request"})

    def finish(self, tools):
        if self.result is None:
            raise ProviderError("CLI stream ended without a terminal result; partial reply retained")
        raw = self.result.get("result", self.raw)
        if not isinstance(raw, str) or not raw:
            raise ProviderError("CLI did not produce a final reply")
        return self.reply.finish(raw, tools, self.usage)
