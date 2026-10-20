"""Regressions found while comparing EternalChat with the UniiChat source."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from eternalchat.cli_provider import CLIProvider, _clean, _last_mark, _prompt_blocks
from eternalchat.config import Config
from eternalchat.errors import ProviderError, RefusalError, StoreError
from eternalchat.prompt import TOOLS, text_block, view_blocks
from eternalchat.provider import AnthropicProvider, prefix_key, request_body
from eternalchat.storage import Store
from eternalchat.view import View
from tests.subscription_fake import FAKE_CLI
from tests.test_engine import EngineFixture, Scripted, text_response
from tests.test_provider_cli import kwargs, sse, stream_events


class MemoryReviewTests(unittest.TestCase):
    def test_merge_order_depends_on_view_end_not_unsummarized_backlog(self):
        # The view's oldest pair is due before its two most recent leaves.
        # Logging more input must not change that age until the view reaches it.
        parts = [(1, 0), (1, 1), (1, 2), (0, 6), (0, 7)]
        expected = ((2, 0), (1, 2), (0, 6), (0, 7))
        config = Config(view_low=120, view_high=130, compact_low=60, compact_high=100)
        for backlog in (0, 32):
            with self.subTest(backlog=backlog), tempfile.TemporaryDirectory() as root:
                with Store(root) as store:
                    for i in range(8 + backlog):
                        store.append("note", str(i))
                    for level, count in ((0, 8), (1, 4), (2, 1)):
                        for i in range(count):
                            store.add_node((level, i), "s" * 20)
                    store.write_state("view", parts)
                    store.write_state("compaction-view", parts)
                    view = View(store, config)
                    view.advance()
                    self.assertEqual(view.main, expected)
                    self.assertEqual(view.covered, 8)
                with Store(root, readonly=True) as store:
                    self.assertEqual(View(store, config).main, expected)


class CompactionReviewTests(EngineFixture):
    async def test_refused_summary_keeps_its_source_clipped_and_turns_go_on(self):
        # A refusal is the input's doing: asked again it refuses alike, and every
        # later turn waits on this leaf. UniiChat keeps the source clipped.
        async def handler(kw):
            if kw["messages"][-1]["content"][-1]["text"].startswith("Compaction:"):
                raise RefusalError("Anthropic stopped with refusal")
            return text_response("reply")

        provider = Scripted(handler)
        engine = self.engine(provider)
        engine.record("note", "z" * 700)
        self.assertEqual(await engine.ask("hello"), "reply")
        self.assertEqual(engine.store.node_text((0, 0)), "note: " + "z" * 506)
        leaf = [c for c in provider.calls if "compress message 0" in c["messages"][0]["content"][-1]["text"]]
        self.assertEqual(len(leaf), 1)

    async def test_merge_input_holds_exactly_two_lines(self):
        tasks = []

        async def handler(kw):
            tasks.append(kw["messages"][0]["content"][-1]["text"])
            return text_response("merged")

        engine = self.engine(Scripted(handler))
        engine.record("user", "first\nsecond\n" + "a" * 280)
        engine.record("user", "third\r\n" + "b" * 280)
        await engine.finish()
        source = tasks[0].split("<input>\n", 1)[1].split("\n</input>", 1)[0]
        self.assertEqual(source.split("\n"), ["user: first second " + "a" * 280, "user: third  " + "b" * 280])


class SubscriptionCacheTests(unittest.TestCase):
    def test_claude_code_prompt_shares_whole_blocks_and_marks_the_view(self):
        # Claude Code got the request as one text block, so no later call could
        # reuse its view: it wrote ~20k tokens to the cache on every call.
        lines = [f"{i}+1|note: item {i} ü\n" for i in range(12)]
        calls = []
        for view, ask in ((lines[:8], "first"), (lines, "second")):
            messages = [{"role": "user", "content": view_blocks(view) + [text_block("time"), text_block(ask)]}]
            request = {"system": "s", "tools": TOOLS, "messages": _clean(messages, []),
                       "response_token_budget": 64}
            blocks = _prompt_blocks("head\n", request, _last_mark(messages))
            self.assertEqual("".join(b["text"] for b in blocks), "head\n" + json.dumps(request, ensure_ascii=False))
            marked = [k for k, b in enumerate(blocks) if "cache_control" in b]
            self.assertEqual(len(marked), 1)
            self.assertEqual(blocks[marked[0]]["cache_control"], {"type": "ephemeral", "ttl": "1h"})
            calls.append(([b["text"] for b in blocks], marked[0]))
        (first, mark), (second, later) = calls
        self.assertGreater(later, mark)  # the grown view's mark moves on
        self.assertEqual(first[:mark + 1], second[:mark + 1])  # and still meets the earlier entry


class StreamReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_refusal_is_told_apart_from_other_failures(self):
        provider = AnthropicProvider("test-secret")
        with patch("eternalchat.provider.urlopen", return_value=io.BytesIO(sse(stream_events(stop="refusal")))):
            with self.assertRaises(RefusalError):
                await provider.complete(**kwargs())
        self.assertFalse(provider._writing)

    async def test_cache_reads_keep_the_prefix_warm(self):
        # A read restarts the vendor's five minutes, as a write does. Counting
        # only from the write made a call lead, and others wait, every 270 s.
        provider = AnthropicProvider("test-secret")
        clock = [1000.0]
        with patch("eternalchat.provider.time", SimpleNamespace(monotonic=lambda: clock[0])), \
                patch("eternalchat.provider.urlopen", side_effect=lambda *a, **k: io.BytesIO(sse(stream_events()))):
            await provider.complete(**kwargs())
            clock[0] += 200
            await provider.complete(**kwargs())
            clock[0] += 200
            self.assertIsNone(await provider._enter(prefix_key(request_body(**kwargs()))))
        self.assertFalse(provider._writing)

    async def test_unfinished_and_duplicate_blocks_are_rejected(self):
        valid = stream_events(tool=True)
        cases = {
            "missing start": valid[1:],
            "unfinished text": [e for e in valid if not (
                e["type"] == "content_block_stop" and e["index"] == 0)],
            "unfinished tool": [e for e in valid if not (
                e["type"] == "content_block_stop" and e["index"] == 1)],
            "duplicate stop": valid[:4] + [valid[3]] + valid[4:],
        }
        for name, events in cases.items():
            with self.subTest(case=name):
                provider = AnthropicProvider("test-secret")
                with patch("eternalchat.provider.urlopen", return_value=io.BytesIO(sse(events))):
                    with self.assertRaises(ProviderError):
                        await provider.complete(**kwargs())
                self.assertFalse(provider._writing)

    async def test_tool_input_must_be_a_complete_object(self):
        events = stream_events(tool=True)
        for event in events:
            if event["type"] == "content_block_delta" and event["index"] == 1:
                event["delta"]["partial_json"] = "[]" if event["delta"]["partial_json"].endswith(":") else ""
        provider = AnthropicProvider("test-secret")
        with patch("eternalchat.provider.urlopen", return_value=io.BytesIO(sse(events))):
            with self.assertRaises(ProviderError):
                await provider.complete(**kwargs())

    async def test_terminal_message_finishes_without_reading_until_socket_eof(self):
        class Stream(io.BytesIO):
            def __iter__(self):
                yield from sse(stream_events()).splitlines(keepends=True)
                raise OSError("socket failed after the complete response")

        provider = AnthropicProvider("test-secret")
        with patch("eternalchat.provider.urlopen", return_value=Stream()):
            result = await provider.complete(**kwargs())
        self.assertEqual(result.text, "Hello 🦊")

    async def test_relative_cli_executable_survives_temporary_working_directory(self):
        with tempfile.TemporaryDirectory() as root:
            fake = Path(root, "fake-cli")
            fake.write_text(FAKE_CLI)
            fake.chmod(0o700)
            with contextlib.chdir(root):
                provider = CLIProvider("claudecode", executable="./fake-cli")
            response = await provider.complete(model="", system="Compaction", tools=[], messages=[])
            self.assertEqual(response.text, "summary")


class StorageReviewTests(unittest.TestCase):
    def test_repeated_image_still_validates_each_reference(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            image = root / "test.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\nimage")
            with Store(root) as store:
                ref = store.add_image(image)
                for text in ("one", "two"):
                    store.append("user", text, attachments=[ref])
            path = next((root / "main").glob("*.jsonl"))
            records = [json.loads(line) for line in path.read_bytes().splitlines()]
            records[1]["attachments"][0]["media_type"] = "image/jpeg"
            broken = b"".join(json.dumps(r).encode() + b"\n" for r in records)
            path.write_bytes(broken)
            with self.assertRaisesRegex(StoreError, "media type"):
                Store(root)
            self.assertEqual(path.read_bytes(), broken)

    def test_inconsistent_event_length_is_rejected_on_restart(self):
        with tempfile.TemporaryDirectory() as root:
            with Store(root, message_bytes=4) as store:
                store.append("work", "abcdefgh", event_id="agent:Scout")
            path = next(Path(root, "main").glob("*.jsonl"))
            records = [json.loads(line) for line in path.read_bytes().splitlines()]
            records[1]["event_parts"] = 3
            broken = b"".join(json.dumps(r).encode() + b"\n" for r in records)
            path.write_bytes(broken)
            with self.assertRaisesRegex(StoreError, "event"):
                Store(root)
            self.assertEqual(path.read_bytes(), broken)

    def test_invalid_message_metadata_is_rejected_before_writing(self):
        with tempfile.TemporaryDirectory() as root:
            with Store(root) as store:
                for fields in ({"date": "not-a-date"}, {"date": 123}, {"event_id": 123}):
                    with self.subTest(fields=fields), self.assertRaises(ValueError):
                        store.append("note", "bad", **fields)
                    self.assertEqual(store.message_count, 0)
                self.assertEqual(store.append("note", "good"), [0])
            with Store(root) as store:
                self.assertEqual(store.message(0)["text"], "good")

    def test_event_retry_checks_kind_and_attachments(self):
        with tempfile.TemporaryDirectory() as root:
            image = Path(root, "test.png")
            image.write_bytes(b"\x89PNG\r\n\x1a\nimage")
            with Store(root) as store:
                ref = store.add_image(image)
                store.append("user", "same", attachments=[ref], event_id="image-event")
                for kind, attachments in (("note", [ref]), ("user", [])):
                    with self.subTest(kind=kind, attachments=attachments):
                        with self.assertRaisesRegex(StoreError, "different contents"):
                            store.append(kind, "same", attachments=attachments, event_id="image-event")
                self.assertEqual(store.message_count, 1)
                self.assertEqual(store.append("user", "same", attachments=[ref], event_id="image-event"), [0])
