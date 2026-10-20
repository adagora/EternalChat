import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from eternalchat.app import open_chat
from eternalchat.cli_provider import CLIProvider, _PriorityGate
from eternalchat.cli_protocol import MessageStream, ReplyStream, normalize_usage
from eternalchat.config import Config
from eternalchat.engine import INTERRUPTED
from eternalchat.errors import ProviderError
from eternalchat.prompt import TOOLS
from eternalchat.storage import Store
from eternalchat.view import View
from tests.subscription_fake import FAKE_CLI
from tests.test_engine import Scripted, text_response


class DecoderTests(unittest.TestCase):
    def test_every_split_preserves_unicode_and_never_streams_tool_arguments(self):
        text = 'Zażółć 😀\n"quoted" \\ backslash\t⛄'
        call = {'name': 'zoom', 'arguments': {'id': 0, 'text': 'private tool arguments'}}
        for value in ({'text': text, 'tool_calls': [call]}, {'tool_calls': [call], 'text': text}):
            raw = json.dumps(value, ensure_ascii=True)
            for split in range(len(raw) + 1):
                events = []
                stream = ReplyStream(lambda *event: events.append(event))
                stream.feed(raw[:split])
                stream.feed(raw[split:])
                response = stream.finish(raw, TOOLS, {})
                self.assertEqual(''.join(v for kind, v in events if kind == 'text'), text)
                self.assertEqual(response.text, text)
                self.assertEqual(response.tool_calls[0]['input'], call['arguments'])
        events = []
        stream = ReplyStream(lambda *event: events.append(event))
        for c in raw:
            stream.feed(c)
        self.assertEqual(''.join(v for kind, v in events if kind == 'text'), text)

    def test_invalid_or_revised_reply_is_not_committed(self):
        for raw in ('{"text":"a","text":"b","tool_calls":[]}',
                    '{"text":"\\uD800!","tool_calls":[]}'):
            with self.assertRaises(ProviderError):
                stream = ReplyStream(lambda *event: None)
                stream.feed(raw)
                stream.finish(raw, [], {})
        stream = ReplyStream(lambda *event: None)
        stream.feed('{"text":"already shown')
        with self.assertRaisesRegex(ProviderError, 'already streamed'):
            stream.finish('{"text":"different", "tool_calls":[]}', [], {})

    def test_native_replays_and_usage_snapshots_are_not_counted_twice(self):
        events = []
        stream = MessageStream(lambda *event: events.append(event))
        raw = '{"text":"hello", "tool_calls":[]}'
        stream.feed({'type': 'stream_event', 'event': {'type': 'message_start', 'message': {
            'id': 'a', 'usage': {'input_tokens': 7, 'cache_read_input_tokens': 2}}}})
        stream.feed({'type': 'stream_event', 'event': {'type': 'content_block_delta',
            'delta': {'type': 'thinking_delta', 'thinking': 'private'}}})
        stream.feed({'type': 'stream_event', 'event': {'type': 'content_block_delta',
            'delta': {'type': 'text_delta', 'text': raw}}})
        stream.feed({'type': 'stream_event', 'event': {'type': 'message_delta', 'usage': {'output_tokens': 3}}})
        stream.feed({'type': 'assistant', 'message': {'id': 'a', 'content': [
            {'type': 'thinking', 'thinking': 'private'}, {'type': 'text', 'text': raw}],
            'usage': {'input_tokens': 7, 'output_tokens': 3}}})
        stream.feed({'type': 'result', 'result': raw})
        response = stream.finish([])
        self.assertEqual(response.usage, {'input_tokens': 7, 'cache_read_input_tokens': 2, 'output_tokens': 3})
        self.assertEqual([v for kind, v in events if kind == 'thinking'], ['private'])
        self.assertEqual([v for kind, v in events if kind == 'text'], ['hello'])
        self.assertEqual(normalize_usage({'inputTokens': 100, 'cachedInputTokens': 70,
            'cacheWriteInputTokens': 10, 'outputTokens': 10, 'reasoningOutputTokens': 5}, codex=True),
            {'input_tokens': 20, 'cache_read_input_tokens': 70, 'cache_creation_input_tokens': 10,
             'output_tokens': 10, 'reasoning_tokens': 5})


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_reply_arrives_before_exit_and_is_saved_on_cancel(self):
        for client in ('codex', 'claudecode'):
            with self.subTest(client=client), tempfile.TemporaryDirectory() as root:
                fake = Path(root, 'fake')
                source = FAKE_CLI.replace("import base64,", "import time\nimport base64,")
                source = source.replace("notify('item/completed'", "time.sleep(60)\n    notify('item/completed'")
                source = source.replace("    output({'type': 'assistant'", "    time.sleep(60)\n    output({'type': 'assistant'")
                fake.write_text(source)
                fake.chmod(0o700)
                received = asyncio.Event()
                events = []
                def emit(kind, value):
                    events.append((kind, value))
                    if kind == 'text':
                        received.set()
                provider = CLIProvider(client, executable=str(fake))
                engine = open_chat(Path(root, 'partial'), provider, Config(), emit)
                engine.note('amber-42')
                fake.write_text(source.replace("elif 'tool_result' in prompt:", "elif True:"))
                future = engine.submit('Answer now')
                await asyncio.wait_for(received.wait(), 5)
                self.assertFalse(future.done())
                self.assertTrue(any(kind == 'thinking' for kind, _ in events))
                self.assertFalse(any(kind == 'block' for kind, _ in events))
                await engine.close()
                self.assertTrue(future.cancelled())
                reader = open_chat(Path(root, 'partial'), readonly=True)
                try:
                    logged = '\n'.join(reader.store.message(i)['text'] for i in range(reader.store.message_count))
                    self.assertIn('Remembered', logged)
                    self.assertIn(INTERRUPTED.strip(), logged)
                    self.assertNotIn('private reasoning summary', logged)
                finally:
                    await reader.close()

    async def test_foreground_priority_and_cancelled_waiter_do_not_leak_capacity(self):
        gate = _PriorityGate(1)
        release = asyncio.Event()
        order = []
        async def work(name, priority, hold=False):
            async with gate.enter(priority):
                order.append(name)
                if hold:
                    await release.wait()
        first = asyncio.create_task(work('active', 3, True))
        await asyncio.sleep(0)
        cancelled = asyncio.create_task(work('cancelled', 0))
        old = asyncio.create_task(work('old merge', 3))
        memory = asyncio.create_task(work('required memory', 1))
        turn = asyncio.create_task(work('reply', 0))
        await asyncio.sleep(0)
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        release.set()
        await asyncio.gather(first, old, memory, turn)
        self.assertEqual(order, ['active', 'reply', 'required memory', 'old merge'])
        self.assertEqual(gate.active, 0)

    async def test_parallel_capacity_and_core_request_purposes(self):
        gate = _PriorityGate(2)
        release = asyncio.Event()
        active = []
        async def work():
            async with gate.enter(0):
                active.append(gate.active)
                await release.wait()
        tasks = [asyncio.create_task(work()) for _ in range(3)]
        await asyncio.sleep(0)
        self.assertEqual(len(active), 2)
        release.set()
        await asyncio.gather(*tasks)
        self.assertLessEqual(max(active), 2)
        with tempfile.TemporaryDirectory() as root:
            async def answer(kw):
                return text_response('s' * 25)
            provider = Scripted(answer)
            engine = open_chat(root, provider, Config(summary_bytes=30))
            try:
                engine.note('n' * 100)
                await engine.ask('q' * 100)
                await engine.finish()
                self.assertEqual({call['purpose'] for call in provider.calls}, {'turn', 'memory', 'summary'})
            finally:
                await engine.close()

    async def test_required_memory_runs_before_queued_old_merges(self):
        with tempfile.TemporaryDirectory() as root:
            config = Config(summary_bytes=50, concurrency=1)
            with Store(root) as store:
                view = View(store, config)
                for i in range(4):
                    store.append('note', f'original {i}')
                    store.add_node((0, i), 's' * 80)
                view.advance()
            started, release = asyncio.Event(), asyncio.Event()
            purposes = []
            async def answer(kw):
                purposes.append(kw['purpose'])
                if len(purposes) == 1:
                    started.set()
                    await release.wait()
                return text_response('summary')
            engine = open_chat(root, Scripted(answer), config)
            try:
                engine.compactor.start()
                await asyncio.wait_for(started.wait(), 5)
                engine.note('new original ' * 20)
                future = engine.submit('reply please')
                await asyncio.sleep(0)
                release.set()
                await asyncio.wait_for(future, 5)
                await engine.finish()
                self.assertEqual(purposes[:2], ['summary', 'memory'])
                self.assertEqual(engine.compactor._required, {})
                self.assertEqual(engine.status()['failed'], 0)
            finally:
                release.set()
                await engine.close()

    async def test_reply_capacity_is_reserved_between_tool_calls(self):
        gate = _PriorityGate(1)
        gate.foreground = 1
        order = []
        async def work(name, priority):
            async with gate.enter(priority):
                order.append(name)
        background = asyncio.create_task(work('merge', 3))
        await asyncio.sleep(0)
        await work('first model call', 0)
        await asyncio.sleep(0)  # an asynchronous memory tool runs here
        self.assertEqual(order, ['first model call'])
        await work('reply after memory tool', 0)
        gate.foreground = 0
        gate._admit()
        await background
        self.assertEqual(order, ['first model call', 'reply after memory tool', 'merge'])

    async def test_spare_capacity_is_available_while_reply_is_running(self):
        gate = _PriorityGate(2)
        gate.foreground = 1
        release = asyncio.Event()
        started = []
        async def work(name, priority):
            async with gate.enter(priority):
                started.append(name)
                await release.wait()
        turn = asyncio.create_task(work('reply', 0))
        await asyncio.sleep(0)
        background = asyncio.create_task(work('merge', 3))
        await asyncio.sleep(0)
        self.assertEqual(started, ['reply', 'merge'])
        release.set()
        await asyncio.gather(turn, background)
        self.assertEqual(gate.foreground_active, 0)

    async def test_known_cli_errors_are_classified_without_echoing_secrets(self):
        for reason, category in [('UsageLimitExceeded secret', 'limit'), ('Unauthorized secret', 'login'),
                                 ('ContextWindowExceeded secret', 'context'), ('unknown option secret', 'version')]:
            with tempfile.TemporaryDirectory() as root:
                fake = Path(root, 'fake')
                fake.write_text('#!/usr/bin/env python3\nimport json,sys\nsys.stdin.read()\n' +
                    f'print(json.dumps({{"type":"error", "message":{reason!r}}}))\n')
                fake.chmod(0o700)
                provider = CLIProvider('claudecode', executable=str(fake))
                with self.assertRaises(ProviderError) as caught:
                    await provider.complete(model='', system='', tools=[], messages=[])
                self.assertEqual(provider.stats()['last_error']['category'], category)
                self.assertNotIn('secret', str(caught.exception))

    async def test_plain_assistant_quota_wall_is_not_streamed_as_user_text(self):
        with tempfile.TemporaryDirectory() as root:
            fake = Path(root, 'fake')
            fake.write_text('#!/usr/bin/env python3\nimport json,sys\nsys.stdin.read()\n'
                'print(json.dumps({"type":"assistant", "message":{"content":[{"type":"text", '
                '"text":"You have hit your weekly limit secret"}]}}))\n')
            fake.chmod(0o700)
            events = []
            provider = CLIProvider('claudecode', executable=str(fake))
            with self.assertRaisesRegex(ProviderError, 'usage limit reached'):
                await provider.complete(model='', system='', tools=[], messages=[], on_event=lambda *event: events.append(event))
            self.assertFalse(any(kind == 'text' for kind, _ in events))
            self.assertEqual(provider.stats()['last_error']['category'], 'limit')

    async def test_codex_rejects_api_authentication_before_a_model_call(self):
        with tempfile.TemporaryDirectory() as root:
            fake = Path(root, 'fake')
            fake.write_text(FAKE_CLI.replace("{'type': 'chatgpt'}", "{'type': 'apiKey'}"))
            fake.chmod(0o700)
            events = []
            provider = CLIProvider('codex', executable=str(fake))
            with self.assertRaisesRegex(ProviderError, 'subscription login required'):
                await provider.complete(model='', system='', tools=[], messages=[], on_event=lambda *event: events.append(event))
            self.assertFalse(any(kind in ('text', 'thinking', 'usage', 'block') for kind, _ in events))
