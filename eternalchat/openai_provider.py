"""OpenAI Responses API with native functions, images and stateless reasoning.

Wire documentation checked 2026-10-21: official Responses/function-calling,
reasoning and computer-use integration guides. No billing/provider fallback.
"""
import asyncio
import copy
import json
import threading
import urllib.error
import urllib.request

from .errors import ProviderError, RefusalError
from .llm import Response
from .provider import urlopen

DEFAULT_MODEL = "gpt-6.1-sol"
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
TIERS = ("auto", "default", "flex", "priority", "fast", "ultrafast")
MAX_STREAM = 16_000_000


def content(block):
    if block["type"] == "text":
        part = {"type": "input_text", "text": block["text"]}
        if "cache_control" in block:
            part["prompt_cache_breakpoint"] = {"mode": "explicit"}
        return part
    if block["type"] == "image":
        source = block["source"]
        if source.get("type") != "base64" or source.get("media_type") not in (
                "image/png", "image/jpeg", "image/gif", "image/webp"):
            raise ProviderError("OpenAI images require supported base64 image content")
        return {"type": "input_image", "image_url": f"data:{source['media_type']};base64,{source['data']}",
                "detail": "auto"}
    raise ProviderError("Unsupported OpenAI input content")


def request_body(model, system, tools, messages, max_tokens, *, effort=None, service_tier=None):
    items = []
    for message in messages:
        state = message.get("provider_state")
        if state is not None:
            if state.get("provider") != "openai" or state.get("model") != model:
                raise ProviderError("Cannot replay provider state through another provider/model")
            items.extend(copy.deepcopy(state["output"]))
            continue
        pending = []

        def flush():
            if pending:
                items.append({"role": message["role"], "content": list(pending)})
                pending.clear()

        for block in message["content"]:
            if block["type"] == "tool_use":
                flush()
                items.append({"type": "function_call", "call_id": block["id"], "name": block["name"],
                              "arguments": json.dumps(block["input"], ensure_ascii=False)})
            elif block["type"] == "tool_result":
                flush()
                value = block.get("content", [])
                value = [{"type": "text", "text": value}] if isinstance(value, str) else value
                items.append({"type": "function_call_output", "call_id": block["tool_use_id"],
                              "output": [content(b) for b in value]})
            elif block["type"] == "text" and message["role"] == "assistant":
                pending.append({"type": "output_text", "text": block["text"], "annotations": []})
            else:
                pending.append(content(block))
        flush()
    body = {"model": model, "instructions": system, "input": items, "stream": True, "store": False,
            "max_output_tokens": max_tokens,
            "tools": [{"type": "function", "name": t["name"], "description": t["description"],
                       "parameters": copy.deepcopy(t["input_schema"]), "strict": False} for t in tools]}
    reasoning = {}
    if model.startswith(("gpt-6", "gpt-5.6")):
        reasoning["context"] = "all_turns"
    if effort:
        reasoning["effort"] = effort
    if reasoning:
        body["reasoning"] = reasoning
    if service_tier:
        body["service_tier"] = service_tier
    return body


def usage(value):
    result = {}
    details = value.get("input_tokens_details") or {}
    for native, canonical in (("cached_tokens", "cache_read_input_tokens"),
                              ("cache_write_tokens", "cache_creation_input_tokens")):
        if type(details.get(native)) is int and details[native] >= 0:
            result[canonical] = details[native]
    if type(value.get("input_tokens")) is int:
        result["input_tokens"] = max(0, value["input_tokens"] - result.get("cache_read_input_tokens", 0)
                                      - result.get("cache_creation_input_tokens", 0))
    if type(value.get("output_tokens")) is int:
        result["output_tokens"] = value["output_tokens"]
    reasoning = (value.get("output_tokens_details") or {}).get("reasoning_tokens")
    if type(reasoning) is int:
        result["reasoning_tokens"] = reasoning
    return result


class StreamDecoder:
    def __init__(self, model, on_event=None):
        self.model, self.on_event = model, on_event or (lambda *args: None)
        self.text = {}
        self.items = {}
        self.blocks = {}
        self.finished = False
        self.result = None
        self.last_model = None
        self.last_tier = None

    def item(self, index, item):
        if type(index) is not int or index < 0 or not isinstance(item, dict):
            raise ValueError("Invalid output item")
        if index in self.items:
            if item != self.items[index]:
                raise ValueError("Completed output item changed")
            return
        blocks = []
        if item["type"] == "message":
            for part_index, part in enumerate(item["content"]):
                if part["type"] == "refusal":
                    raise RefusalError("OpenAI refused the request; partial output retained")
                if part["type"] != "output_text" or not isinstance(part.get("text"), str):
                    raise ValueError("Unsupported output message part")
                value = part["text"]
                streamed = self.text.get((index, part_index), "")
                if not value.startswith(streamed):
                    raise ValueError("Final text differs from streamed prefix")
                if value[len(streamed):]:
                    self.on_event("text", value[len(streamed):])
                if value:
                    block = {"type": "text", "text": value}
                    blocks.append(block)
                    self.on_event("block", block)
        elif item["type"] == "function_call":
            args = json.loads(item["arguments"])
            if not isinstance(args, dict) or not all(isinstance(item.get(k), str) and item[k] for k in ("call_id", "name")):
                raise ValueError("Invalid function call")
            if any(b.get("id") == item["call_id"] for values in self.blocks.values() for b in values):
                raise ValueError("Duplicate function call identity")
            block = {"type": "tool_use", "id": item["call_id"], "name": item["name"], "input": args}
            blocks.append(block)
            self.on_event("block", block)
        elif item["type"] != "reasoning":
            raise ValueError("Unsupported output item")
        self.items[index], self.blocks[index] = copy.deepcopy(item), blocks

    def feed(self, event):
        if not isinstance(event, dict) or self.finished:
            raise ValueError("Malformed or late event")
        kind = event["type"]
        if kind == "response.output_text.delta":
            key = event["output_index"], event["content_index"]
            if not all(type(i) is int and i >= 0 for i in key) or not isinstance(event["delta"], str):
                raise ValueError("Invalid text delta")
            if key[0] in self.items:
                raise ValueError("Delta after completed item")
            self.text[key] = self.text.get(key, "") + event["delta"]
            self.on_event("text", event["delta"])
        elif kind == "response.reasoning_summary_text.delta":
            if not isinstance(event["delta"], str):
                raise ValueError("Invalid reasoning summary delta")
            self.on_event("thinking", event["delta"])
        elif kind == "response.output_item.done":
            self.item(event["output_index"], event["item"])
        elif kind in ("response.completed", "response.incomplete", "response.failed"):
            response = event["response"]
            if not isinstance(response.get("output"), list):
                raise ValueError("Missing response output")
            for index, item in enumerate(response["output"]):
                self.item(index, item)
            if len(self.items) != len(response["output"]) or any(
                    index not in self.items or self.items[index]["type"] != "message"
                    or part >= len(self.items[index]["content"])
                    or self.items[index]["content"][part]["type"] != "output_text"
                    for index, part in self.text):
                raise ValueError("Output did not finish")
            observed_usage = usage(response.get("usage") or {})
            self.on_event("usage", observed_usage)
            self.last_model, self.last_tier = response.get("model"), response.get("service_tier")
            if kind != "response.completed" or response.get("status") != "completed":
                raise ProviderError("OpenAI response did not complete; partial output retained; request was not retried")
            blocks = [b for i in sorted(self.blocks) for b in self.blocks[i]]
            if not blocks:
                raise ProviderError("OpenAI returned no text or function calls; request was not retried")
            self.result = Response(blocks, "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn",
                observed_usage, {"provider": "openai", "model": self.model, "output": copy.deepcopy(response["output"])})
            self.finished = True
        elif kind == "error":
            raise ProviderError("OpenAI stream error; partial output retained; request was not retried")


class OpenAIProvider:
    per_call_effort = True

    def __init__(self, api_key, *, endpoint="https://api.openai.com/v1/responses", timeout=120,
                 reasoning_effort=None, service_tier=None):
        if not api_key:
            raise ProviderError("Set OPENAI_API_KEY for the OpenAI API; subscription logins do not supply API credit")
        if reasoning_effort not in (None, *EFFORTS) or service_tier not in (None, *TIERS):
            raise ValueError("Unsupported OpenAI reasoning effort or service tier")
        self._api_key, self.endpoint, self.timeout = api_key, endpoint, timeout
        self.effort, self.tier = reasoning_effort, service_tier
        self.last_model, self.last_tier = None, None

    def stats(self):
        return {"provider": "openai", "last_model": self.last_model, "service_tier": self.last_tier}

    async def complete(self, *, model, system, tools, messages, max_tokens=8192, on_event=None, purpose="turn", effort=None):
        foreground = purpose not in ("memory", "summary")
        chosen = effort or (self.effort if foreground else None)
        if chosen not in (None, *EFFORTS):
            raise ValueError("Unsupported OpenAI reasoning effort")
        body = request_body(model, system, tools, messages, max_tokens, effort=chosen,
                            service_tier=self.tier if foreground else None)
        queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        stopped = threading.Event()

        def post(value):
            if not stopped.is_set():
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, value)
                except RuntimeError:
                    pass

        def read():
            request = urllib.request.Request(self.endpoint, data=json.dumps(body).encode(), headers={
                "Authorization": "Bearer " + self._api_key, "Content-Type": "application/json", "Accept": "text/event-stream"})
            try:
                with urlopen(request, timeout=self.timeout) as stream:
                    data, total = [], 0
                    for raw in stream:
                        if stopped.is_set():
                            return
                        total += len(raw)
                        if total > MAX_STREAM:
                            raise ValueError("Stream too large")
                        line = raw.decode("utf-8").rstrip("\r\n")
                        if line.startswith("data:"):
                            data.append(line[5:].lstrip())
                        elif not line and data:
                            event = json.loads("\n".join(data))
                            post(event)
                            data = []
                            if event.get("type") in ("response.completed", "response.incomplete", "response.failed", "error"):
                                return
                    if data:
                        post(json.loads("\n".join(data)))
            except urllib.error.HTTPError as error:
                post(ProviderError(f"OpenAI HTTP {error.code}; request was not retried"))
                error.close()
            except Exception as error:
                post(ProviderError(f"OpenAI transport failed ({type(error).__name__}); request was not retried"))
            finally:
                post(None)

        decoder = StreamDecoder(model, on_event)
        try:
            threading.Thread(target=read, name="eternalchat-openai", daemon=True).start()
            async with asyncio.timeout(self.timeout):
                while not decoder.finished:
                    event = await queue.get()
                    if event is None:
                        raise ProviderError("OpenAI stream ended before completion; request was not retried")
                    if isinstance(event, Exception):
                        raise event
                    try:
                        decoder.feed(event)
                    except (ValueError, KeyError, TypeError, AttributeError) as error:
                        raise ProviderError("Malformed OpenAI response; request was not retried") from error
            self.last_model, self.last_tier = decoder.last_model, decoder.last_tier
            return decoder.result
        except TimeoutError:
            raise ProviderError("OpenAI request timed out; partial output retained; request was not retried") from None
        finally:
            stopped.set()
