"""Regression tests for integrity, robustness and boundary fixes."""
import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from eternalchat.app import open_chat
from eternalchat.config import Config
from eternalchat.demo import DemoProvider
from eternalchat.errors import CompactionError, ProviderError, StoreError
from eternalchat.llm import Response
from eternalchat.provider import AnthropicProvider
from eternalchat.storage import Store
from eternalchat.view import View
from tests.test_engine import EngineFixture, Scripted, text_response
from tests.test_provider_cli import kwargs, sse, stream_events

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 100


class StoreHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name, "chat")

    def test_interrupted_append_never_reuses_an_id(self):
        with Store(self.root) as store:
            store.append("user", "first")
            with patch("os.fsync", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    store.append("user", "maybe on disk")
            with self.assertRaisesRegex(StoreError, "uncertain write"):
                store.append("user", "must not reuse id 1")
        with Store(self.root) as store:
            self.assertEqual(store.message_count, 2)
            self.assertEqual(store.message(1)["text"], "maybe on disk")

    def test_torn_asset_is_rewritten_from_verified_bytes(self):
        image = Path(self.temp.name, "picture.png")
        image.write_bytes(PNG)
        with Store(self.root) as store:
            ref = store.add_image(image)
            asset = self.root / "assets" / ref["sha256"]
            asset.write_bytes(PNG[:20])  # as if a crash tore the first write
            self.assertEqual(store.add_image(image), ref)
            self.assertEqual(asset.read_bytes(), PNG)
            store.append("user", "look", attachments=[ref])
        with Store(self.root) as store:
            self.assertEqual(store.image_bytes(store.message(0)["attachments"][0]), PNG)
        self.assertFalse(list((self.root / "assets").glob("*.tmp")))

    def test_image_reads_are_bounded_and_never_block(self):
        big = Path(self.temp.name, "big.png")
        big.write_bytes(PNG + b"0" * 5_000_000)
        with Store(self.root) as store:
            with self.assertRaisesRegex(ValueError, "5 MB"):
                store.add_image(big)
            with self.assertRaisesRegex(ValueError, "regular image file"):
                store.add_image("/dev/zero")
        # A FIFO with no writer blocks a plain open(); check in a child process
        # so a regression fails here instead of hanging the suite.
        fifo = Path(self.temp.name, "pipe")
        os.mkfifo(fifo)
        source = ("import sys; from eternalchat.storage import Store\n"
                  "with Store(sys.argv[1]) as s:\n"
                  "    try: s.add_image(sys.argv[2])\n"
                  "    except ValueError as e: print(e)\n")
        try:
            result = subprocess.run([sys.executable, "-c", source, str(self.root), str(fifo)],
                                    capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            self.fail("opening a FIFO blocked")
        self.assertIn("regular image file", result.stdout, result.stderr)

    def test_attachment_references_must_match_their_bytes(self):
        image = Path(self.temp.name, "picture.png")
        image.write_bytes(PNG)
        with Store(self.root) as store:
            ref = store.add_image(image)
            with self.assertRaisesRegex(StoreError, "media type"):
                store.append("user", "x", attachments=[{**ref, "media_type": "image/gif"}])
            store.append("user", "x", attachments=[{**ref, "extra": "z" * 1000}])
            self.assertEqual(store.message(0)["attachments"], [ref])

    def test_interrupt_after_write_before_index_update_poisons(self):
        with Store(self.root) as store:
            write = store._append

            def written_then_interrupted(kind, record):
                write(kind, record)
                raise KeyboardInterrupt

            with patch.object(store, "_append", side_effect=written_then_interrupted):
                with self.assertRaises(KeyboardInterrupt):
                    store.append("user", "on disk")
            with self.assertRaisesRegex(StoreError, "uncertain write"):
                store.append("user", "would reuse id 0")
        with Store(self.root) as store:
            self.assertEqual(store.message_count, 1)

    def test_reader_without_lock_file_still_excludes_writers(self):
        with Store(self.root) as store:
            store.append("note", "hello")
        (self.root / ".lock").unlink()  # e.g. restored from a backup without it
        with Store(self.root, readonly=True):
            with self.assertRaisesRegex(StoreError, "already open"):
                Store(self.root)
        with Store(self.root):
            pass

    def test_names_saved_by_older_versions_stay_readable(self):
        with Store(self.root) as store:
            path = self.root / "agents" / "2nd"
            path.mkdir()
            (path / "state.json").write_text(json.dumps({"status": "complete", "task": "t", "report": "r"}))
            self.assertEqual(store.agent_reports(), [("2nd", "r")])
            self.assertEqual(store.agent_state("2nd")["report"], "r")
            with self.assertRaises(ValueError):
                store.create_agent("3rd", "new names start with a letter")

    def test_invalid_attachment_reference_is_never_logged(self):
        with Store(self.root) as store:
            for ref in [{"sha256": "../../etc/passwd", "media_type": "image/png"},
                        {"sha256": "0" * 64, "media_type": "text/html"}, "not a dict"]:
                with self.assertRaises(StoreError):
                    store.append("user", "x", attachments=[ref])
            self.assertEqual(store.message_count, 0)

    def test_malformed_records_fail_with_restore_guidance(self):
        records = [
            ("main", b'{"i":0,"kind":"user","text":null,"size":0,"date":"2026-01-01T00:00:00+00:00"}\n'),
            ("main", b'{"i":0,"kind":"user","text":"a","size":true,"date":"2026-01-01T00:00:00+00:00"}\n'),
            ("main", b'{"i":0,"kind":["user"],"text":"a","size":1,"date":"2026-01-01T00:00:00+00:00"}\n'),
            ("main", b'[1,2,3]\n'),
            ("tree", b'{"l":4000000000,"i":0,"text":"x","size":1}\n'),
        ]
        for kind, line in records:
            with tempfile.TemporaryDirectory() as temp:
                with Store(temp) as store:
                    store.append("user", "seed")
                # A bad main record replaces the log; a bad node goes in a new tree shard.
                path = next(Path(temp, "main").glob("*.jsonl")) if kind == "main" else Path(temp, "tree", "2099-01-01.jsonl")
                path.write_bytes(line)
                with self.assertRaisesRegex(StoreError, "Restore a verified backup"):
                    Store(temp)

    def test_readonly_store_shares_lock_and_never_writes(self):
        with self.assertRaisesRegex(StoreError, "No chat found"):
            Store(self.root, readonly=True)
        self.assertFalse(self.root.exists())
        with Store(self.root) as store:
            store.append("note", "hello")
            with self.assertRaisesRegex(StoreError, "open for writing"):
                Store(self.root, readonly=True)
        before = sorted((p.relative_to(self.root), p.stat().st_mtime_ns) for p in self.root.rglob("*"))
        with Store(self.root, readonly=True) as a, Store(self.root, readonly=True) as b:
            self.assertEqual(a.message(0)["text"], b.message(0)["text"])
            with self.assertRaisesRegex(StoreError, "read-only"):
                a.append("note", "no")
        after = sorted((p.relative_to(self.root), p.stat().st_mtime_ns) for p in self.root.rglob("*"))
        self.assertEqual(before, after)

    def test_flags_are_cleared_only_after_partitions_are_saved(self):
        cfg = Config(view_low=70, view_high=110, compact_low=50, compact_high=90)
        with Store(self.root) as store:
            view = View(store, cfg)
            write = store.write_state
            order = []

            def recording(name, value):
                order.append((name, value))
                if name == "compaction-view" and recording.fail:
                    raise OSError("disk full")
                return write(name, value)

            recording.fail = False
            with patch.object(store, "write_state", side_effect=recording):
                for i in range(8):
                    store.append("user", str(i))
                    store.add_node((0, i), f"user: item-{i}-" + "x" * 12)
                    view.advance()
                for level in (1, 2, 3):
                    for i in range(8 >> level):
                        store.add_node((level, i), f"m{level}/{i}")
                recording.fail = True
                with self.assertRaises(OSError):
                    view.advance()  # merges; compaction-view fails to save
                order.clear()
                recording.fail = False
                view.advance()  # nothing new: must resave the partition before clearing flags
            names = [name for name, _ in order]
            self.assertLess(names.index("compaction-view"), names.index("view-state"))
            self.assertEqual(order[-1], ("view-state", {"main_pending": False, "small_pending": False}))
            self.assertFalse(view.dirty)

    def test_leaves_stay_dirty_when_the_intent_write_fails(self):
        with Store(self.root) as store:
            view = View(store, Config())
            store.append("user", "a")
            store.add_node((0, 0), "user: a")
            with patch.object(store, "write_state", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    view.advance()
            self.assertTrue(view.dirty)
            view.flush()
            self.assertEqual(json.loads((self.root / "view.json").read_text()), [[0, 0]])

    def test_agent_names_are_safe_and_unambiguous(self):
        with Store(self.root) as store:
            for name in ["123", "../x", "a/b", "", "x" * 65, ".hidden"]:
                with self.assertRaises(ValueError):
                    store.create_agent(name, "task")
            store.create_agent("Research-2", "task")
            self.assertEqual(store.agent_state("Research-2")["status"], "running")


class EngineHardeningTests(EngineFixture):
    async def test_view_checkpoint_failure_does_not_orphan_parent(self):
        engine = self.engine()
        engine.record("note", "a")
        await engine.finish()
        write_state = engine.store.write_state
        failures = []

        def flaky(name, value):
            if name == "view" and not failures:
                failures.append(name)
                raise OSError("simulated disk full")
            return write_state(name, value)

        with patch.object(engine.store, "write_state", side_effect=flaky):
            engine.record("note", "b")
            await engine.finish()  # the parent's own save succeeds and clears the error
        self.assertEqual(failures, ["view"])
        self.assertTrue(engine.store.has_node((1, 0)))  # parent built despite the failure
        self.assertEqual(engine.status()["failed"], 0)
        saved = json.loads(Path(self.temp.name, "view.json").read_text())
        self.assertEqual([tuple(k) for k in saved], list(engine.view.main))

    async def test_failed_view_save_is_retried_by_compact_alone(self):
        engine = self.engine()
        write_state = engine.store.write_state
        failing = [True]

        def flaky(name, value):
            if name == "view" and failing[0]:
                raise OSError("simulated disk full")
            return write_state(name, value)

        with patch.object(engine.store, "write_state", side_effect=flaky):
            engine.record("note", "only leaf")  # no sibling, so no later node resaves
            with self.assertRaisesRegex(CompactionError, "Saving the memory view failed"):
                await engine.finish()
            self.assertEqual(engine.status()["failed"], 0)  # the node itself is built
            self.assertFalse(engine.status()["view_saved"])
            failing[0] = False
            engine.compact()
            await engine.finish()
        self.assertTrue(engine.status()["view_saved"])
        saved = json.loads(Path(self.temp.name, "view.json").read_text())
        self.assertEqual([tuple(k) for k in saved], list(engine.view.main))

    async def test_input_is_logged_even_when_the_view_cannot_be_saved(self):
        engine = self.engine()
        engine.record("note", "x")
        await engine.finish()
        with patch.object(engine.view, "flush", side_effect=OSError("disk full")):
            engine.view._dirty.add("view")
            future = engine.submit("do not lose me")
        self.assertIn("do not lose me", [engine.store.message(i)["text"] for i in range(engine.store.message_count)])
        await future

    async def test_text_finished_before_a_token_limit_is_marked_interrupted(self):
        async def handler(kw):
            if kw["model"] == Config.summary_model:
                return text_response("s")
            kw["on_event"]("block", {"type": "text", "text": "a complete-looking sentence"})
            raise ProviderError("Anthropic stopped with max_tokens; partial output retained")

        engine = self.engine(Scripted(handler))
        with self.assertRaises(ProviderError):
            await engine.ask("hello")
        texts = [engine.store.message(i)["text"] for i in range(engine.store.message_count)]
        self.assertEqual(texts[-2:], ["a complete-looking sentence", "[interrupted: this reply did not finish]"])

    async def test_view_size_needs_no_file_reads(self):
        engine = self.engine()
        for i in range(50):
            engine.record("note", f"note {i}")
        await engine.finish()
        with patch.object(type(engine.store), "_read", side_effect=AssertionError("file read")):
            self.assertGreater(engine.view.size(engine.view.main), 0)

    async def test_failed_retry_keeps_the_shortest_line(self):
        replies = ["x" * 600, ProviderError("overloaded")]

        async def handler(kw):
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return text_response(reply)

        engine = self.engine(Scripted(handler))
        engine.record("note", "source " * 200)
        await engine.finish()
        self.assertEqual(engine.store.node((0, 0))["text"], "x" * 600)

    async def test_summary_and_turn_usage_are_counted_separately(self):
        async def handler(kw):
            kw["on_event"]("usage", {"input_tokens": 7 if kw["model"] == Config.summary_model else 3})
            return text_response("short")

        engine = self.engine(Scripted(handler))
        engine.record("note", "y" * 700)
        await engine.ask("hello")
        await engine.finish()
        usage = engine.status()["usage"]
        self.assertEqual(usage["summaries"]["input_tokens"], 7)  # only the long note needs a model
        self.assertEqual(usage["turns"]["input_tokens"], 3)

    async def test_input_is_logged_while_its_turn_waits_for_summaries(self):
        release = asyncio.Event()

        async def handler(kw):
            await release.wait()
            return text_response("summary")

        provider = Scripted(handler)
        engine = self.engine(provider)
        engine.record("note", "z" * 700)  # needs a model summary before any turn
        first = engine.submit("first")
        second = engine.submit("second")
        await asyncio.sleep(0.05)
        texts = [engine.store.message(i)["text"] for i in range(engine.store.message_count)]
        self.assertEqual(texts[1:], ["first", "second"])
        release.set()
        await asyncio.gather(first, second)
        # Both are answered in one turn: no turn starts from a view that lacks
        # the reply to "first", and "second" is never in the historical view.
        turns = [c for c in provider.calls if c["model"] == Config.turn_model]
        self.assertEqual(len(turns), 1)
        content = turns[0]["messages"][0]["content"]
        self.assertEqual([b["text"] for b in content[-2:]], ["first", "second"])
        self.assertNotIn("second", "".join(b["text"] for b in content[:-2]))

    async def test_round_limit_leaves_steering_for_its_own_turn(self):
        called, release = asyncio.Event(), asyncio.Event()
        turns = []

        async def handler(kw):
            if kw["model"] == Config.summary_model:
                return text_response("summary")
            turns.append(kw["messages"][-1]["content"][-1]["text"])
            if len(turns) == 1:
                called.set()
                await release.wait()
                return Response([{"type": "tool_use", "id": "a", "name": "date", "input": {"id": 0}}], "tool_use")
            return text_response("answered")

        engine = self.engine(Scripted(handler), Config(max_tool_rounds=1))
        first = engine.submit("first")
        await called.wait()
        second = engine.submit("steer me")
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        self.assertIsInstance(results[0], ValueError)
        self.assertEqual(results[1], "answered")
        self.assertEqual(turns[-1], "steer me")

    async def test_agent_waits_for_summaries_and_sees_recent_messages(self):
        seen = []

        async def handler(kw):
            if kw["model"] == Config.turn_model:
                seen.append("".join(b.get("text", "") for b in kw["messages"][0]["content"]))
            return text_response("done")

        engine = self.engine(Scripted(handler))
        engine.record("note", "fresh context")
        await engine.start_agent("Scout", "look around")  # before the note's summary exists
        await engine.finish()
        self.assertIn("note: fresh context", seen[0])

    async def test_readonly_engine_reads_without_writing(self):
        engine = self.engine()
        await engine.ask("remember amber-42")
        await engine.finish()
        await engine.close()
        root = Path(self.temp.name)
        before = sorted((p.relative_to(root), p.stat().st_mtime_ns) for p in root.rglob("*"))
        reader = open_chat(root, readonly=True)
        self.engines.append(reader)
        self.assertIn("amber-42", reader.render_view())
        self.assertIn("amber-42", reader.zoom(0).text)
        self.assertEqual(reader.status()["messages"], 2)
        with self.assertRaisesRegex(RuntimeError, "read-only"):
            reader.submit("no")
        await reader.close()
        after = sorted((p.relative_to(root), p.stat().st_mtime_ns) for p in root.rglob("*"))
        self.assertEqual(before, after)


class ProviderHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiters_proceed_together_once_the_first_response_starts(self):
        provider = AnthropicProvider("test-secret")
        first_started, first_release = threading.Event(), threading.Event()
        others_release = threading.Event()
        opened = []

        class Stream:
            def __init__(self, first):
                self.first = first

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def __iter__(self):
                if self.first:
                    first_started.set()
                    first_release.wait(3)
                    yield from sse(stream_events()).splitlines(keepends=True)
                else:
                    others_release.wait(3)
                    yield from sse(stream_events()).splitlines(keepends=True)

        def opening(*args, **kw):
            opened.append(1)
            return Stream(len(opened) == 1)

        with patch("eternalchat.provider.urlopen", side_effect=opening):
            a = asyncio.create_task(provider.complete(**kwargs()))
            await asyncio.to_thread(first_started.wait, 2)
            waiters = [asyncio.create_task(provider.complete(**kwargs())) for _ in range(3)]
            await asyncio.sleep(0.05)
            self.assertEqual(len(opened), 1)
            first_release.set()
            await a
            for _ in range(100):
                if len(opened) == 4:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(len(opened), 4)  # all three went at once, not one by one
            # The prefix is now warm: a new call doesn't wait either.
            late = asyncio.create_task(provider.complete(**kwargs()))
            for _ in range(100):
                if len(opened) == 5:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(len(opened), 5)
            others_release.set()
            await asyncio.gather(*waiters, late)
        self.assertFalse(provider._writing)

    async def test_error_type_is_reported_but_never_the_body(self):
        body = json.dumps({"type": "error", "error": {"type": "overloaded_error",
                                                      "message": "echo of test-secret"}}).encode()
        error = urllib.error.HTTPError("https://example.test", 529, "x", {}, io.BytesIO(body))
        provider = AnthropicProvider("test-secret")
        with patch("eternalchat.provider.urlopen", side_effect=error):
            with self.assertRaises(ProviderError) as raised:
                await provider.complete(**kwargs())
        self.assertIn("HTTP 529 (overloaded_error)", str(raised.exception))
        self.assertNotIn("test-secret", str(raised.exception))
        events = [{"type": "message_start", "message": {}},
                  {"type": "error", "error": {"type": "api_error", "message": "test-secret"}}]
        with patch("eternalchat.provider.urlopen", return_value=io.BytesIO(sse(events))):
            with self.assertRaisesRegex(ProviderError, r"stream error \(api_error\)") as raised:
                await provider.complete(**kwargs())
        self.assertNotIn("test-secret", str(raised.exception))

    async def test_failed_reader_start_releases_the_gate(self):
        provider = AnthropicProvider("test-secret")
        with patch("threading.Thread.start", side_effect=RuntimeError("can't start new thread")):
            with self.assertRaises(RuntimeError):
                await provider.complete(**kwargs())
        self.assertFalse(provider._writing)

    async def test_redirects_are_refused_so_the_key_stays_home(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        seen = []

        class Elsewhere(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("x-api-key"))
                self.send_response(200)
                self.end_headers()

            do_POST = do_GET

            def log_message(self, *args):
                pass

        other = ThreadingHTTPServer(("127.0.0.1", 0), Elsewhere)

        class Redirect(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(302)  # urllib follows 301-303 for POST, keeping x-api-key
                self.send_header("Location", f"http://127.0.0.1:{other.server_port}/steal")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (server, other)]
        for thread in threads:
            thread.start()
        try:
            provider = AnthropicProvider("test-secret", endpoint=f"http://127.0.0.1:{server.server_port}/v1/messages")
            with self.assertRaisesRegex(ProviderError, "HTTP 302"):
                await provider.complete(**kwargs())
            self.assertEqual(seen, [])
        finally:
            for s in (server, other):
                await asyncio.to_thread(s.shutdown)
                s.server_close()

    async def test_callback_errors_are_not_reported_as_malformed_streams(self):
        provider = AnthropicProvider("test-secret")

        def broken(kind, value):
            raise KeyError("ui bug")

        with patch("eternalchat.provider.urlopen", return_value=io.BytesIO(sse(stream_events()))):
            with self.assertRaises(KeyError):
                await provider.complete(**kwargs(), on_event=broken)
        self.assertFalse(provider._writing)


class CLIHardeningTests(unittest.TestCase):
    def run_cli(self, *args, input=None, env=None):
        return subprocess.run([sys.executable, "-m", "eternalchat", *args], input=input, env=env,
                              capture_output=True, text=True, timeout=30)

    def test_bad_slash_commands_report_usage_without_crashing(self):
        with tempfile.TemporaryDirectory() as temp:
            lines = ["hello", "/zoom ", "/zoom", "/zoom 0 x", "/agent Foo", "/date", "/bogus",
                     "/note", "/attach", "/help me", "/quit now", "//starts with slash", "/quit"]
            result = self.run_cli("chat", "--demo", "--data-dir", temp, input="\n".join(lines) + "\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertIn("Usage: /zoom", result.stderr)
            self.assertIn("Unknown command /bogus", result.stderr)
            self.assertIn("/quit takes no arguments", result.stderr)
            self.assertIn("Saved your message: /starts with slash", result.stdout)

    def test_options_after_command_and_inspection_of_missing_chat(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp, "chat")
            self.assertEqual(self.run_cli("ask", "hi", "--demo", "--data-dir", str(root)).returncode, 0)
            self.assertIn("hi", self.run_cli("zoom", "0", "--data-dir", str(root)).stdout)
            usage = self.run_cli("zoom", "Scout", "2", "--data-dir", str(root))
            self.assertEqual(usage.returncode, 2)
            self.assertIn("message ID, not to an agent name", usage.stderr)
            missing = Path(temp, "typo")
            result = self.run_cli("status", "--data-dir", str(missing))
            self.assertEqual(result.returncode, 1)
            self.assertIn("No chat found", result.stderr)
            self.assertFalse(missing.exists())

    def test_turn_error_is_printed_once(self):
        with tempfile.TemporaryDirectory() as temp:
            instructions = Path(temp, "big.txt")
            instructions.write_text("p" * 200_000, encoding="utf-8")
            result = self.run_cli("--demo", "--data-dir", str(Path(temp, "chat")),
                                  "--instructions", str(instructions), "ask", "hello")
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stderr.count("text budget"), 1, result.stderr)

    def test_chat_session_delivers_reports_left_by_a_crash(self):
        with tempfile.TemporaryDirectory() as temp:
            with Store(temp) as store:
                store.create_agent("Scout", "look")
                store.save_agent_state("Scout", {"status": "complete", "task": "look", "report": "found it"})
            result = self.run_cli("chat", "--demo", "--data-dir", temp, input="/quit\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            with Store(temp, readonly=True) as store:
                texts = [store.message(i)["text"] for i in range(store.message_count)]
            self.assertIn("[Scout] found it", texts)

    def test_help_explains_commands_and_examples(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0)
        for text in ("commands:", "examples:", "ETERNALCHAT_DIR", "zoom 40 8", "Exit status"):
            self.assertIn(text, result.stdout)
        chat = self.run_cli("chat", "-h").stdout
        self.assertIn("/agent NAME TASK", chat)
