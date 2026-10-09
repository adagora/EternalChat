"""Behavioral regressions from the fresh-eyes branch review."""
import asyncio
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from eternalchat.app import open_chat
from eternalchat.config import Config
from eternalchat.demo import DemoProvider
from eternalchat.errors import CompactionError, StoreError
from eternalchat.prompt import request_text_bytes
from eternalchat.storage import Store
from tests.test_engine import EngineFixture, Scripted, text_response


class ReviewFixTests(EngineFixture):
    async def test_bulk_import_keeps_summary_requests_bounded(self):
        sizes = []

        async def summarize(kw):
            size = request_text_bytes(kw["system"], kw["tools"], kw["messages"])
            sizes.append(size)
            self.assertLessEqual(size, Config.request_bytes)
            return text_response("s" * 500)

        engine = self.engine(Scripted(summarize))
        engine.note("x" * (30_000 * 420))
        await asyncio.wait_for(engine.finish(), 30)
        self.assertTrue(sizes)
        self.assertEqual(engine.status()["summarized_messages"], 420)
        self.assertEqual(engine.status()["failed"], 0)
        self.assertLessEqual(engine.status()["compaction_view_bytes"], Config.compact_high)

    async def test_summary_budget_rejects_before_provider_call(self):
        provider = Scripted(lambda kw: DemoProvider().complete(**kw))
        engine = self.engine(provider, Config(request_bytes=1000))
        engine.note("x" * 700)
        with self.assertRaises(CompactionError):
            await engine.finish()
        self.assertEqual(provider.calls, [])
        self.assertFalse(engine.store.has_node((0, 0)))
        self.assertEqual(engine.store.message(0)["text"], "x" * 700)

    async def test_summary_keeps_usable_result_when_retry_exceeds_budget(self):
        async def summarize(kw):
            return text_response("s" * 7000)

        provider = Scripted(summarize)
        engine = self.engine(provider, Config(request_bytes=10_000))
        engine.note("x" * 700)
        await engine.finish()
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(engine.store.node_text((0, 0)), "s" * 7000)

    async def test_compact_resumes_merge_after_intent_write_failure(self):
        cfg = Config(summary_bytes=32, view_low=120, view_high=240,
                     compact_low=60, compact_high=120)

        async def summarize(kw):
            return text_response("s" * 20)

        engine = self.engine(Scripted(summarize), cfg)
        write = engine.store.write_state

        def unavailable(name, value):
            if name == "view-state":
                raise OSError("simulated checkpoint failure")
            write(name, value)

        with patch.object(engine.store, "write_state", side_effect=unavailable):
            for i in range(64):
                engine.note(f"note {i} " + "x" * 50)
            with self.assertRaises(CompactionError):
                await engine.finish()
        self.assertEqual(engine.store.node_count, 127)
        engine.compact()
        await engine.finish()
        status = engine.status()
        self.assertTrue(status["view_saved"])
        self.assertFalse(status["batch_pending"])
        self.assertLessEqual(status["view_bytes"], cfg.view_low)
        self.assertLessEqual(status["compaction_view_bytes"], cfg.compact_low)

    async def test_null_snapshots_fail_without_rewriting_them(self):
        engine = self.engine()
        engine.note("keep the original partition")
        await engine.finish()
        await engine.close()
        root = Path(self.temp.name)
        for name in ("view.json", "compaction-view.json", "view-state.json"):
            path = root / name
            original = path.read_bytes()
            path.write_bytes(b"null\n")
            try:
                for readonly in (True, False):
                    with self.subTest(snapshot=name, readonly=readonly):
                        with self.assertRaisesRegex(StoreError, "Invalid"):
                            opened = open_chat(root, DemoProvider(), readonly=readonly)
                            await opened.close()
                        self.assertEqual(path.read_bytes(), b"null\n")
            finally:
                path.write_bytes(original)

    async def test_finish_waits_for_report_submitted_during_drain(self):
        summary_started, release_summary = asyncio.Event(), asyncio.Event()
        parent_started, release_parent = asyncio.Event(), asyncio.Event()

        async def respond(kw):
            task = kw["messages"][0]["content"][-1]["text"]
            if kw["model"] == Config.summary_model:
                summary_started.set()
                await release_summary.wait()
                return text_response("note summary")
            if task == "agent task":
                return text_response("agent report")
            parent_started.set()
            await release_parent.wait()
            return text_response("parent handled report")

        engine = self.engine(Scripted(respond))
        engine.note("x" * 700)
        engine.start_agent("Scout", "agent task")
        await asyncio.wait_for(summary_started.wait(), 2)
        finishing = asyncio.create_task(engine.finish())
        await asyncio.sleep(0)  # finish enters drain while the agent waits for context
        release_summary.set()
        await asyncio.wait_for(parent_started.wait(), 2)
        try:
            done, _ = await asyncio.wait({finishing}, timeout=0.05)
            self.assertFalse(done, "finish returned while the report turn was running")
        finally:
            release_parent.set()
            await asyncio.wait_for(finishing, 2)
        self.assertTrue(engine.turn_task.done())
        self.assertEqual(engine.status()["messages"], engine.status()["summarized_messages"])
        texts = [engine.store.message(i)["text"] for i in range(engine.store.message_count)]
        self.assertIn("parent handled report", texts)

    async def test_import_preserves_unicode_and_all_line_endings(self):
        source = Path(self.temp.name, "source.txt")
        original = ("one\r\ntwo\rthree\n🦊" * 3000).encode("utf-8")
        source.write_bytes(original)
        root = Path(self.temp.name, "chat")
        result = subprocess.run(
            [sys.executable, "-m", "eternalchat", "--demo", "--data-dir", str(root),
             "import", str(source)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        with Store(root, readonly=True) as store:
            self.assertGreater(store.message_count, 1)
            actual = "".join(store.message(i)["text"] for i in range(store.message_count))
        self.assertEqual(actual.encode("utf-8"), original)
