import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eternalchat.app import open_chat
from eternalchat.config import Config, byte_size
from eternalchat.demo import DemoProvider
from eternalchat.errors import CompactionError, ProviderError
from eternalchat.llm import Response
from eternalchat.prompt import TOOLS, system_prompt, tools_for
from eternalchat.storage import atomic_json


class Scripted:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(copy.deepcopy({k: v for k, v in kwargs.items() if k != "on_event"}))
        return await self.handler(kwargs)


def text_response(text):
    return Response([{"type": "text", "text": text}])


class EngineFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engines = []

    def engine(self, provider=None, config=None, events=None):
        engine = open_chat(self.temp.name, provider or DemoProvider(), config, events)
        self.engines.append(engine)
        return engine

    async def asyncTearDown(self):
        for engine in self.engines:
            if not engine.closed:
                await engine.close()


class EngineTests(EngineFixture):
    async def test_queued_input_after_end_or_failure_starts_fresh(self):
        for fail in (False, True):
            called, release = asyncio.Event(), asyncio.Event()
            count = 0
            async def handler(kw):
                nonlocal count
                if kw["model"] == Config.summary_model:
                    return text_response("summary")
                count += 1
                if count == 1:
                    called.set()
                    await release.wait()
                    if fail:
                        raise ProviderError("interrupted")
                return text_response("answer")
            provider = Scripted(handler)
            engine = self.engine(provider)
            first = engine.submit("first input")
            await called.wait()
            sentinel = f"unique steering sentinel {fail}"
            second = engine.submit(sentinel)
            release.set()
            outcomes = await asyncio.gather(first, second, return_exceptions=True)
            self.assertEqual(isinstance(outcomes[0], ProviderError), fail)
            await engine.finish()
            turns = [c for c in provider.calls if c["model"] == Config.turn_model]
            self.assertEqual(len(turns[-1]["messages"]), 1)
            body = turns[-1]["messages"][0]["content"]
            self.assertNotIn(sentinel, json.dumps(body[:-1]))
            self.assertEqual(body[-1]["text"], sentinel)
            await engine.close()

    async def test_completed_agent_report_recovers_exactly_once(self):
        engine = self.engine()
        with patch.object(engine, "submit", side_effect=RuntimeError("simulated death before delivery")):
            with self.assertRaisesRegex(RuntimeError, "simulated death"):
                await engine.start_agent("Outbox", "research this")
        self.assertFalse(any(engine.store.message(i)["kind"] == "work" for i in range(engine.store.message_count)))
        await engine.close()
        engine = self.engine()
        engine.resume_reports()
        await engine.finish()
        work_ids = [i for i in range(engine.store.message_count) if engine.store.message(i)["kind"] == "work"]
        self.assertEqual(len(work_ids), 1)
        await engine.close()
        engine = self.engine()
        engine.resume_reports()
        await engine.finish()
        self.assertEqual([i for i in range(engine.store.message_count) if engine.store.message(i)["kind"] == "work"], work_ids)

    async def test_partial_report_delivery_recovers_missing_chunks(self):
        config = Config(message_bytes=10)
        engine = self.engine(config=config)
        path = engine.store.root / "agents" / "Partial"
        path.mkdir()
        report = "completed research result"
        payload = "[Partial] " + report
        atomic_json(path / "state.json", {"status": "complete", "task": "research", "report": report})
        append = engine.store._append
        def crash(kind, record):
            if record.get("event_part") == 1:
                raise OSError("simulated death")
            return append(kind, record)
        with patch.object(engine.store, "_append", side_effect=crash):
            with self.assertRaises(OSError):
                engine.store.append("work", payload, event_id="agent:Partial")
        await engine.close()
        engine = self.engine(config=config)
        engine.resume_reports()
        await engine.finish()
        work = [engine.store.message(i)["text"] for i in range(engine.store.message_count) if engine.store.message(i)["kind"] == "work"]
        self.assertEqual("".join(work), payload)

    async def test_cancel_while_waiting_keeps_input(self):
        called = asyncio.Event()
        async def handler(kw):
            called.set()
            await asyncio.Event().wait()
        engine = self.engine(Scripted(handler))
        engine.record("note", "x" * 600)
        future = engine.submit("do not lose this input")
        await called.wait()
        await engine.close()
        self.assertTrue(future.cancelled())
        messages = [engine.store.message(i)["text"] for i in range(engine.store.message_count)]
        self.assertIn("do not lose this input", messages)

    async def test_fresh_turn_and_restart_recall(self):
        provider = Scripted(lambda kw: DemoProvider().complete(**kw))
        engine = self.engine(provider)
        await engine.ask("code amber-42")
        await engine.finish()
        await engine.ask("what was it?")
        await engine.finish()
        turns = [c for c in provider.calls if c["model"] == Config.turn_model]
        self.assertEqual(len(turns), 2)
        self.assertEqual(len(turns[1]["messages"]), 1)
        before = turns[0]["messages"][0]["content"][:-1]
        self.assertNotIn("code amber-42", json.dumps(before))
        self.assertIn("code amber-42", json.dumps(turns[1]["messages"]))
        before_view = engine.view.render()
        count = engine.store.node_count
        await engine.close()
        engine = self.engine()
        self.assertEqual(engine.view.render(), before_view)
        self.assertEqual(engine.store.node_count, count)
        result = await engine.ask("/recall 0")
        self.assertIn("code amber-42", result)
        await engine.finish()
        kinds = [engine.store.message(i)["kind"] for i in range(engine.store.message_count)]
        self.assertIn("tool", kinds)
        self.assertIn("echo", kinds)

    async def test_parallel_tools_then_steering_order(self):
        called = asyncio.Event()
        release = asyncio.Event()
        count = 0

        async def handler(kw):
            nonlocal count
            if kw["model"] == Config.summary_model:
                return text_response("a compact memory")
            count += 1
            if count == 1:
                called.set()
                await release.wait()
                return Response([
                    {"type": "tool_use", "id": "a", "name": "zoom", "input": {"id": 0, "n": 1}},
                    {"type": "tool_use", "id": "b", "name": "date", "input": {"id": 0}}], "tool_use")
            return text_response("used your correction")

        provider = Scripted(handler)
        engine = self.engine(provider)
        first = engine.submit("original")
        await called.wait()
        second = engine.submit("correction: blue")
        release.set()
        await asyncio.gather(first, second)
        await engine.finish()
        call = [c for c in provider.calls if c["model"] == Config.turn_model][1]
        content = call["messages"][-1]["content"]
        self.assertEqual([b["type"] for b in content], ["tool_result", "tool_result", "text"])
        self.assertEqual([b["tool_use_id"] for b in content[:2]], ["a", "b"])
        self.assertEqual(content[-1]["text"], "correction: blue")
        self.assertEqual(engine.store.message(1)["text"], "correction: blue")

    async def test_partial_stream_persists_text_but_never_thinking(self):
        async def handler(kw):
            cb = kw["on_event"]
            cb("thinking", "secret internal reasoning")
            cb("text", "partial visible answer")
            raise ProviderError("connection lost")
        engine = self.engine(Scripted(handler))
        with self.assertRaisesRegex(ProviderError, "connection lost"):
            await engine.ask("hello")
        records = [engine.store.message(i) for i in range(engine.store.message_count)]
        self.assertEqual(records[-1]["text"], "partial visible answer\n[interrupted: this reply did not finish]")
        self.assertNotIn("secret internal", json.dumps(records))

    async def test_zoom_pagination_invalid_args_and_image(self):
        engine = self.engine(config=Config(page_chars=10))
        image = Path(self.temp.name, "tiny.png")
        image.write_bytes(b"\x89PNG\r\n\x1a\nimage")
        ref = engine.store.add_image(image)
        engine.record("note", "0123456789ABCDEFGHIJ", attachments=[ref])
        first = engine.tool("zoom", {"id": 0, "n": 1})
        second = engine.tool("zoom", {"id": 0, "n": 1, "page": 1})
        self.assertIn("0123456789", first[0]["text"])
        self.assertEqual(first[1]["type"], "image")
        self.assertIn("ABCDEFGHIJ", second[0]["text"])
        self.assertEqual(len(second), 1)
        for args in [{"id": 1, "n": 2}, {"id": 0, "page": 9}, {"name": "../escape"}, {"id": True}, {"id": 0, "oops": 2}]:
            with self.assertRaises(ValueError):
                engine.tool("zoom", args)

    async def test_explicit_agent_isolation_and_report(self):
        engine = self.engine()
        engine.record("note", "context")
        await engine.finish()
        await engine.start_agent("Research", "brief task")
        await engine.finish()
        kinds = [engine.store.message(i)["kind"] for i in range(engine.store.message_count)]
        self.assertEqual(kinds.count("work"), 1)
        report = next(engine.store.message(i)["text"] for i in range(engine.store.message_count) if engine.store.message(i)["kind"] == "work")
        self.assertTrue(report.startswith("[Research]"))
        transcript = engine.tool("zoom", {"name": "Research"})[0]["text"]
        self.assertIn("brief task", transcript)
        self.assertIn("complete", transcript)
        with self.assertRaises(ValueError):
            engine.start_agent("Research", "duplicate")
        with self.assertRaises(ValueError):
            engine.start_agent("../x", "escape")

    async def test_large_user_input_is_saved_and_rejected_visibly(self):
        engine = self.engine()
        value = "z" * (Config.request_bytes + 10_000)
        with self.assertRaisesRegex(ValueError, "Input is saved"):
            await engine.ask(value)
        self.assertEqual("".join(engine.store.message(i)["text"] for i in range(engine.store.message_count)), value)


class CompactionTests(EngineFixture):
    async def test_long_history_budgets_and_no_repeated_tree_scan(self):
        class IndexedNodes(dict):
            def __iter__(self):
                raise AssertionError("historical tree was scanned during live work")
        async def handler(kw):
            return text_response("useful summary " + "x" * 285)
        cfg = Config(view_low=3000, view_high=6000, compact_low=1800, compact_high=3600)
        engine = self.engine(Scripted(handler), cfg)
        engine.store._nodes = IndexedNodes(engine.store._nodes)
        for batch in range(16):
            for i in range(32):
                engine.record("note", f"message {batch*32+i}: " + "ż" * 400)
            await engine.finish()
            self.assertLessEqual(engine.view.size(engine.view.main), cfg.view_high)
            self.assertLessEqual(engine.view.size(engine.view.small), cfg.compact_high)
        self.assertEqual(engine.store.node_count, 1023)
        self.assertEqual(engine.view.end(engine.view.main), 512)
        self.assertIn("message 0:", engine.tool("zoom", {"id": 0})[0]["text"])
    async def test_short_source_and_pair_join_threshold(self):
        calls = []
        async def handler(kw):
            calls.append(kw)
            return text_response("merged")
        engine = self.engine(Scripted(handler))
        engine.record("user", "x" * 290)
        engine.record("user", "y" * 290)
        await engine.finish()
        self.assertEqual(len(calls), 1)
        self.assertEqual(engine.store.node((0, 0))["text"], "user: " + "x" * 290)
        self.assertEqual(engine.store.node((1, 0))["text"], "merged")
        self.assertEqual(calls[0]["system"], system_prompt())
        self.assertEqual(calls[0]["tools"], tools_for(False, boss=True))  # the turns' list: one cached prefix

    async def test_oversized_unicode_retry_retains_shortest(self):
        replies = ["🦊" * 160, "ż" * 270, "x" * 530, "y" * 540, "z" * 550]
        calls = []
        async def handler(kw):
            calls.append(copy.deepcopy({k: v for k, v in kw.items() if k != "on_event"}))
            return text_response(replies[len(calls)-1])
        engine = self.engine(Scripted(handler))
        engine.record("note", "source " * 200)
        await engine.finish()
        self.assertEqual(len(calls), 5)
        self.assertEqual(engine.store.node((0, 0))["text"], "x" * 530)
        self.assertIn("640 bytes", calls[1]["messages"][-1]["content"][0]["text"])
        self.assertIn("-" * 512, calls[0]["messages"][-1]["content"][-1]["text"])

    async def test_failed_summary_blocks_turn_then_retries(self):
        fail = True
        async def handler(kw):
            if fail:
                raise ProviderError("offline")
            return text_response("recovered")
        engine = self.engine(Scripted(handler))
        engine.record("note", "long " * 200)
        with self.assertRaises(CompactionError):
            await engine.finish()
        with self.assertRaises(CompactionError):
            await engine.ask("later")
        fail = False
        engine.compactor.retry()
        await engine.finish()
        self.assertEqual(engine.store.node((0, 0))["text"], "recovered")

    async def test_tool_or_empty_compaction_is_asked_again_then_clipped(self):
        for n, blocks in enumerate([[], [{"type": "tool_use", "id": "evil", "name": "date", "input": {"id": 0}}]]):
            async def handler(kw):
                return Response(blocks)
            provider, events = Scripted(handler), []
            engine = open_chat(Path(self.temp.name, str(n)), provider, None,
                               lambda kind, value: events.append(kind))
            self.engines.append(engine)
            engine.record("note", "line\n" + "x" * 700)
            await engine.finish()
            self.assertEqual(len(provider.calls), Config().attempts)
            # Never the tool call: the source, on one line, cut to the summary size.
            self.assertEqual(engine.store.node_text((0, 0)), "note: line " + "x" * 501)
            self.assertIn("compaction_error", events)
            await engine.close()

    async def test_concurrency_queue_and_no_future_context(self):
        active = peak = 0
        release = asyncio.Event()
        started = asyncio.Event()
        starts = []

        async def handler(kw):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            task = kw["messages"][0]["content"][-1]["text"]
            view = "".join(b["text"] for b in kw["messages"][0]["content"][:-1])
            if "compress message" in task:
                i = int(task.split("message ")[1].split()[0])
                starts.append(i)
                self.assertNotIn(f"future-{i+1}", view)
            if active == 8:
                started.set()
            await release.wait()
            active -= 1
            return text_response("a" * 300)

        engine = self.engine(Scripted(handler))
        for i in range(20):
            engine.record("note", f"future-{i} " + "x" * 700)
        await asyncio.wait_for(started.wait(), 2)
        self.assertEqual(starts, list(range(8)))
        self.assertEqual(peak, 8)
        release.set()
        await asyncio.wait_for(engine.finish(), 5)
        self.assertLessEqual(peak, 8)
        self.assertEqual(engine.store.node_count, 38)  # N + floor(N/2) + ...
        self.assertEqual(engine.compactor.stats()["queued"], 0)

    async def test_restart_builds_only_missing_nodes(self):
        engine = self.engine()
        engine.record("note", "unfinished " * 100)
        await engine.close()  # no background task has run yet
        provider = Scripted(lambda kw: DemoProvider().complete(**kw))
        engine = self.engine(provider)
        await engine.finish()
        self.assertEqual(len(provider.calls), 1)
        await engine.close()
        engine = self.engine(provider)
        await engine.finish()
        self.assertEqual(len(provider.calls), 1)
