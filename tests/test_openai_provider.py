"""Responses API wire acceptance through real local HTTP and the normal engine."""
import asyncio
import copy
import io
import json
import threading
from pathlib import Path
from unittest.mock import patch
import urllib.error

from eternalchat.app import open_chat
from eternalchat.config import Config
from eternalchat.errors import ProviderError, RefusalError
from eternalchat.openai_provider import OpenAIProvider, request_body, StreamDecoder, usage
from eternalchat.prompt import TOOLS, image_block, text_block
from tests.test_engine import EngineFixture
from tests.test_web import PNG


def message(text, identity="message-1", phase="final"):
    return {"type": "message", "id": identity, "status": "completed", "role": "assistant", "phase": phase,
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def stream(output, *, status="completed", native_usage=None):
    events = [{"type": "response.created", "response": {"id": "response-1"}}]
    for index, item in enumerate(output):
        if item["type"] == "message":
            for part_index, part in enumerate(item["content"]):
                if part["type"] == "output_text":
                    events.append({"type": "response.output_text.delta", "output_index": index,
                                   "content_index": part_index, "delta": part["text"]})
        events.append({"type": "response.output_item.done", "output_index": index, "item": item})
    events.append({"type": "response." + status, "response": {"status": status, "model": "observed-model",
        "service_tier": "default", "output": output, "usage": native_usage or {
        "input_tokens": 100, "input_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 10},
        "output_tokens": 20, "output_tokens_details": {"reasoning_tokens": 7}}}})
    return b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)


class OpenAITests(EngineFixture):
    async def serve(self, handler):
        self.requests = []
        self.server_errors = []

        async def http(reader, writer):
            try:
                head = (await reader.readuntil(b"\r\n\r\n")).decode()
                headers = {k.lower(): v.strip() for line in head.split("\r\n")[1:] if ":" in line for k, v in [line.split(":", 1)]}
                body = json.loads(await reader.readexactly(int(headers["content-length"])))
                self.requests.append((headers, body))
                response = handler(body, len(self.requests))
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: "
                             + str(len(response)).encode() + b"\r\n\r\n" + response)
                await writer.drain()
            except Exception as error:
                self.server_errors.append(error)
            finally:
                writer.close()

        server = await asyncio.start_server(http, "127.0.0.1", 0)
        self.addCleanup(server.close)
        return f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1/responses"

    def arguments(self):
        return dict(model="gpt-6.1-sol", system="system", tools=TOOLS,
                    messages=[{"role": "user", "content": [text_block("hello")]}])

    async def test_real_http_zoom_continuation_usage_phase_and_no_saved_reasoning(self):
        reasoning = {"type": "reasoning", "id": "reason-1", "summary": [], "encrypted_content": "opaque-private-state"}
        call = {"type": "function_call", "id": "native-call", "call_id": "zoom-call", "name": "zoom", "arguments": '{"id":0,"n":1}'}

        def answer(body, count):
            return stream([reasoning, message("Looking up memory", "comment", "commentary"), call]) if count == 1 else stream([message("Remembered amber-42")])

        endpoint = await self.serve(answer)
        events = []
        provider = OpenAIProvider("test-secret", endpoint=endpoint, reasoning_effort="high", service_tier="priority")
        engine = open_chat(Path(self.temp.name, "chat"), provider, Config(turn_model="gpt-6.1-sol"), lambda *e: events.append(e))
        self.engines.append(engine)
        engine.note("amber-42")
        self.assertIn("Remembered", await engine.ask("recall the note"))
        first, second = self.requests
        self.assertEqual(first[0]["authorization"], "Bearer test-secret")
        self.assertEqual(first[1]["store"], False)
        self.assertEqual(first[1]["reasoning"], {"context": "all_turns", "effort": "high"})
        self.assertEqual(first[1]["service_tier"], "priority")
        self.assertTrue(all(t["strict"] is False for t in first[1]["tools"]))
        items = second[1]["input"]
        self.assertIn(reasoning, items)
        self.assertIn("commentary", [i.get("phase") for i in items])
        output = next(i for i in items if i.get("type") == "function_call_output")
        self.assertEqual(output["call_id"], "zoom-call")
        self.assertIn("amber-42", json.dumps(output))
        self.assertNotIn("opaque-private-state", json.dumps(engine.messages()))
        self.assertEqual(provider.stats()["last_model"], "observed-model")
        self.assertEqual(provider.stats()["service_tier"], "default")
        self.assertEqual(engine.status()["usage"]["turns"]["input_tokens"], 100)
        self.assertFalse(self.server_errors)

    async def test_images_and_function_results_convert_without_mutating_input(self):
        args = self.arguments()
        picture = image_block("image/png", PNG)
        args["messages"][0]["content"].append(picture)
        args["messages"].extend([
            {"role": "assistant", "content": [{"type": "tool_use", "id": "screen", "name": "computer", "input": {"action": "screenshot"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "screen", "content": [text_block("screen"), picture]}, text_block("correction")]}
        ])
        saved = copy.deepcopy(args)
        body = request_body(**args, max_tokens=8192)
        self.assertEqual(args, saved)
        user_image = body["input"][0]["content"][-1]
        self.assertTrue(user_image["image_url"].startswith("data:image/png;base64,"))
        output = next(i for i in body["input"] if i.get("type") == "function_call_output")
        self.assertEqual(output["output"][1], user_image)
        self.assertEqual(body["input"][-1]["content"][0]["text"], "correction")

    async def test_summary_retry_carries_native_state_and_uses_model_defaults(self):
        native = {"type": "reasoning", "id": "summary-reason", "summary": [], "encrypted_content": "summary-private"}
        endpoint = await self.serve(lambda body, count: stream([native, message("x" * 600)]) if count == 1 else stream([message("short summary")]))
        provider = OpenAIProvider("test-secret", endpoint=endpoint, reasoning_effort="max", service_tier="ultrafast")
        engine = open_chat(Path(self.temp.name, "chat"), provider, Config(summary_model="summary-model"))
        self.engines.append(engine)
        engine.note("long source " * 100)
        await engine.finish()
        self.assertEqual(len(self.requests), 2)
        for _, body in self.requests:
            self.assertNotIn("reasoning", body)
            self.assertNotIn("service_tier", body)
        self.assertIn(native, self.requests[1][1]["input"])
        self.assertNotIn("summary-private", engine.render_view())

    async def test_malformed_incomplete_refused_and_empty_are_not_retried(self):
        cases = [b"", stream([message("partial")], status="incomplete"), stream([]),
                 stream([{**message(""), "content": [{"type": "refusal", "refusal": "no"}]}]),
                 b'data: {"type":"response.output_text.delta","delta":5}\n\n']
        for payload in cases:
            with self.subTest(payload=payload[:50]):
                provider = OpenAIProvider("test-secret")
                with patch("eternalchat.openai_provider.urlopen", return_value=io.BytesIO(payload)) as call:
                    with self.assertRaises(ProviderError):
                        await provider.complete(**self.arguments())
                    self.assertEqual(call.call_count, 1)

    async def test_partial_failure_remains_in_memory_marked_interrupted(self):
        endpoint = await self.serve(lambda body, count: stream([message("partial answer")], status="incomplete"))
        engine = open_chat(Path(self.temp.name, "chat"), OpenAIProvider("secret", endpoint=endpoint), Config(turn_model="gpt-6.1-sol"))
        self.engines.append(engine)
        with self.assertRaises(ProviderError):
            await engine.ask("answer")
        text = json.dumps(engine.messages())
        self.assertIn("partial answer", text)
        self.assertIn("interrupted", text)
        self.assertEqual(len(self.requests), 1)

    async def test_error_redaction_timeout_and_usage_absence(self):
        provider = OpenAIProvider("test-secret", timeout=0.05)
        error = urllib.error.HTTPError("https://api.openai.com/v1/responses", 429, "test-secret", {}, io.BytesIO(b"test-secret"))
        with patch("eternalchat.openai_provider.urlopen", side_effect=error) as request:
            with self.assertRaisesRegex(ProviderError, "HTTP 429") as caught:
                await provider.complete(**self.arguments())
            self.assertNotIn("test-secret", str(caught.exception))
            self.assertEqual(request.call_count, 1)
        self.assertEqual(usage({}), {})
        self.assertEqual(usage({"input_tokens": 10, "input_tokens_details": {"cached_tokens": 3}}),
                         {"input_tokens": 7, "cache_read_input_tokens": 3})

    async def test_cancel_and_timeout_release_caller_without_late_events_or_retry(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                entered, release = threading.Event(), threading.Event()
                events = []

                class Slow:
                    def __enter__(self):
                        return self

                    def __exit__(self, *args):
                        pass

                    def __iter__(self):
                        entered.set()
                        release.wait(2)
                        yield from io.BytesIO(stream([message("too late")]))

                provider = OpenAIProvider("secret", timeout=0.05 if not cancel else 5)
                with patch("eternalchat.openai_provider.urlopen", return_value=Slow()) as request:
                    task = asyncio.create_task(provider.complete(**self.arguments(), on_event=lambda *e: events.append(e)))
                    try:
                        await asyncio.to_thread(entered.wait, 1)
                        if cancel:
                            task.cancel()
                        with self.assertRaises(asyncio.CancelledError if cancel else ProviderError):
                            await asyncio.wait_for(task, 1)
                    finally:
                        release.set()
                    await asyncio.sleep(0.02)
                    self.assertFalse(events)
                    self.assertEqual(request.call_count, 1)
