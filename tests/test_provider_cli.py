import asyncio
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from eternalchat.prompt import TOOLS, system_prompt, view_blocks
from eternalchat.provider import AnthropicProvider, ProviderError, prefix_key, request_body


def stream_events(*, tool=False, thinking=False, complete=True, stop="end_turn"):
    events = [{"type": "message_start", "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 8}}}]
    i = 0
    if thinking:
        events += [
            {"type": "content_block_start", "index": i, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
            {"type": "content_block_delta", "index": i, "delta": {"type": "thinking_delta", "thinking": "ephemeral"}},
            {"type": "content_block_delta", "index": i, "delta": {"type": "signature_delta", "signature": "signed"}},
            {"type": "content_block_stop", "index": i}]
        i += 1
    events += [
        {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": "Hello 🦊"}},
        {"type": "content_block_stop", "index": i}]
    if tool:
        i += 1
        events += [
            {"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": "call1", "name": "zoom", "input": {}}},
            {"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": '{"id":'}},
            {"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": '0,"n":1}'}},
            {"type": "content_block_stop", "index": i}]
    if complete:
        events += [{"type": "message_delta", "delta": {"stop_reason": "tool_use" if tool else stop}, "usage": {"output_tokens": 4}},
                   {"type": "message_stop"}]
    return events


def sse(events):
    return b"".join(("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n").encode() for e in events)


def kwargs(lines=8):
    content = view_blocks([f"{i}+1|user: line {i}\n" for i in range(lines)])
    content.append({"type": "text", "text": "fresh input"})
    return dict(model="test-model", system=system_prompt(), tools=TOOLS,
                messages=[{"role": "user", "content": content}], max_tokens=1024)


class CacheTests(unittest.TestCase):
    def test_four_line_groups_cache_marks_and_model_scope(self):
        args = kwargs(9)
        original = copy.deepcopy(args)
        body = request_body(**args)
        self.assertEqual(args, original)
        blocks = body["messages"][0]["content"]
        marks = [i for i, b in enumerate(blocks) if "cache_control" in b]
        self.assertEqual(marks, [2, len(blocks)-1])
        self.assertEqual(len(blocks[1]["text"].splitlines()), 4)
        different = copy.deepcopy(body)
        different["model"] = "another-model"
        self.assertNotEqual(prefix_key(body), prefix_key(different))
        different = copy.deepcopy(body)
        different["messages"][0]["content"][-1]["text"] = "another task"
        self.assertEqual(prefix_key(body), prefix_key(different))

    def test_completed_blocks_preserve_byte_prefix(self):
        a = view_blocks([f"{i}+1|{i}\n" for i in range(8)])
        b = view_blocks([f"{i}+1|{i}\n" for i in range(9)])
        self.assertEqual(a[:-1], b[:-2])


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_thinking_signature_and_tool_json_stream(self):
        provider = AnthropicProvider("test-secret")
        events = []
        with patch("urllib.request.urlopen", return_value=io.BytesIO(sse(stream_events(tool=True, thinking=True)))):
            result = await provider.complete(**kwargs(), on_event=lambda kind, value: events.append((kind, value)))
        self.assertEqual(result.text, "Hello 🦊")
        self.assertEqual(result.content[0]["signature"], "signed")
        self.assertEqual(result.content[-1]["input"], {"id": 0, "n": 1})
        self.assertEqual(result.usage["cache_read_input_tokens"], 8)
        self.assertIn(("thinking", "ephemeral"), events)
        self.assertFalse(provider._writing)

    async def test_truncation_empty_and_token_limit_fail(self):
        for events in [[], stream_events(complete=False), stream_events(stop="max_tokens")]:
            provider = AnthropicProvider("test-secret")
            with patch("urllib.request.urlopen", return_value=io.BytesIO(sse(events))):
                with self.assertRaises(ProviderError):
                    await provider.complete(**kwargs())
            self.assertFalse(provider._writing)

    async def test_http_error_does_not_print_key_or_retry(self):
        error = urllib.error.HTTPError("https://example.test", 429, "test-secret", {}, io.BytesIO(b"test-secret"))
        provider = AnthropicProvider("test-secret")
        with patch("urllib.request.urlopen", side_effect=error) as call:
            with self.assertRaisesRegex(ProviderError, "HTTP 429") as raised:
                await provider.complete(**kwargs())
        self.assertNotIn("test-secret", str(raised.exception))
        self.assertEqual(call.call_count, 1)

    async def test_identical_prefix_waits_until_response_start(self):
        provider = AnthropicProvider("test-secret")
        first_started = threading.Event()
        first_release = threading.Event()
        first_start_sent = threading.Event()
        finish_release = threading.Event()
        opened = []

        class SlowStream:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def __iter__(self):
                first_started.set()
                first_release.wait(3)
                for line in sse(stream_events()[:1]).splitlines(keepends=True):
                    yield line
                first_start_sent.set()
                finish_release.wait(3)
                yield from sse(stream_events()[1:]).splitlines(keepends=True)

        def opening(*args, **kw):
            opened.append(1)
            return SlowStream() if len(opened) == 1 else io.BytesIO(sse(stream_events()))

        with patch("urllib.request.urlopen", side_effect=opening):
            a = asyncio.create_task(provider.complete(**kwargs()))
            await asyncio.to_thread(first_started.wait, 2)
            b = asyncio.create_task(provider.complete(**kwargs()))
            await asyncio.sleep(0.02)
            self.assertEqual(len(opened), 1)
            first_release.set()
            await asyncio.wait_for(b, 2)
            self.assertFalse(a.done())
            self.assertEqual(len(opened), 2)
            finish_release.set()
            await asyncio.wait_for(a, 2)

    async def test_real_local_http_transport(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path, self.headers.get("x-api-key"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(sse(stream_events()))
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            provider = AnthropicProvider("test-secret", endpoint=f"http://127.0.0.1:{server.server_port}/v1/messages")
            result = await provider.complete(**kwargs())
            self.assertEqual(result.text, "Hello 🦊")
            self.assertEqual(seen[0][0:2], ("/v1/messages", "test-secret"))
            self.assertTrue(seen[0][2]["stream"])
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join()


class CLITests(unittest.TestCase):
    def test_cancelled_transport_does_not_hold_process_open(self):
        source = '''
import asyncio, threading
from unittest.mock import patch
from eternalchat.provider import AnthropicProvider
async def run():
    entered = threading.Event()
    def stalled(*args, **kwargs):
        entered.set()
        threading.Event().wait(30)
        raise OSError("stopped")
    with patch("urllib.request.urlopen", side_effect=stalled):
        provider = AnthropicProvider("fake")
        task = asyncio.create_task(provider.complete(model="fake", system="stable", tools=[],
            messages=[{"role":"user","content":[{"type":"text","text":"hello"}]}]))
        while not entered.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert not provider._writing
asyncio.run(run())
'''
        result = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_two_process_demo_persistence_and_retrieval(self):
        with tempfile.TemporaryDirectory() as temp:
            base = [sys.executable, "-m", "eternalchat", "--data-dir", temp, "--demo"]
            def run(*args, input=None):
                result = subprocess.run(base + list(args), input=input, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout
            self.assertIn("amber-42", run("ask", "remember amber-42"))
            self.assertIn("amber-42", run("ask", "/recall 0"))
            status = json.loads(run("status"))
            self.assertEqual(status["summarized_messages"], status["messages"])
            self.assertEqual(status["failed"], 0)
            self.assertIn("amber-42", run("zoom", "0"))
            self.assertIn("<chat>", run("view"))
            run("chat", input="hello\ncorrection\n/quit\n")
            self.assertIn("Saved", run("note", "imported memory"))

    def test_no_key_fails_without_creating_chat(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp, "chat")
            env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
            result = subprocess.run([sys.executable, "-m", "eternalchat", "--data-dir", str(root), "ask", "hello"],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("ANTHROPIC_API_KEY", result.stderr)
            self.assertFalse(root.exists())
