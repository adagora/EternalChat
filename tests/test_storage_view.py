import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eternalchat.config import Config, byte_size, clip_output, split_text
from eternalchat.errors import StoreError
from eternalchat.storage import Store, atomic_json
from eternalchat.tree import address, candidate, key_for
from eternalchat.view import View


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_unicode_chunks_and_tool_clipping(self):
        text = "A🦊ż中\n" * 100
        parts = list(split_text(text, 17))
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(byte_size(p) <= 17 for p in parts))
        with Store(self.root, 17) as store:
            ids = store.append("user", text)
            self.assertEqual("".join(store.message(i)["text"] for i in ids), text)
        with Store(self.root, 17) as store:
            self.assertEqual(store.message_count, len(ids))
            self.assertEqual(store.message(0)["size"], byte_size(parts[0]))
        output = clip_output("HEAD" + "x" * 40_000 + "TAIL")
        self.assertEqual(len(output), 30_000)
        self.assertTrue(output.startswith("HEAD") and output.endswith("TAIL"))

    def test_rollover_and_clock_regression(self):
        with Store(self.root) as store:
            for date in ["2026-10-08T23:59:59+00:00", "2026-10-09T00:00:00+00:00", "2026-10-07T00:00:00+00:00"]:
                store.append("user", date, date=date)
        with Store(self.root) as store:
            self.assertEqual(store.message_count, 3)
            self.assertEqual(store.message(2)["date"][:10], "2026-10-07")
        self.assertEqual(len(list((self.root / "main").glob("*.jsonl"))), 2)

    def test_competing_process_cannot_write(self):
        with Store(self.root):
            run = subprocess.run([sys.executable, "-c",
                "from eternalchat.storage import Store; import sys; Store(sys.argv[1])", str(self.root)],
                capture_output=True, text=True)
            self.assertNotEqual(run.returncode, 0)
            self.assertIn("already open", run.stderr)
        with Store(self.root):
            pass

    def test_abrupt_process_exit_preserves_committed_append(self):
        result = subprocess.run([sys.executable, "-c",
            "from eternalchat.storage import Store; import os,sys; s=Store(sys.argv[1]); s.append('user','durable'); os._exit(0)",
            str(self.root)], capture_output=True)
        self.assertEqual(result.returncode, 0)
        with Store(self.root) as store:
            self.assertEqual(store.message(0)["text"], "durable")

    def test_torn_record_fails_without_altering_bytes(self):
        with Store(self.root) as store:
            store.append("note", "safe")
        path = next((self.root / "main").glob("*.jsonl"))
        with path.open("ab") as f:
            f.write(b'{"i":1')
        before = path.read_bytes()
        with self.assertRaisesRegex(StoreError, "incomplete final record"):
            Store(self.root)
        self.assertEqual(path.read_bytes(), before)

    def test_duplicate_nodes_and_bad_message_ids(self):
        with Store(self.root) as store:
            store.append("note", "a")
            store.add_node((0, 0), "note: a")
            with self.assertRaises(StoreError):
                store.add_node((0, 0), "bad")
            with self.assertRaises(ValueError):
                store.message(True)
            with self.assertRaises(ValueError):
                store.append("thinking", "private")

    def test_uncertain_fsync_makes_writer_fail_stop(self):
        with Store(self.root) as store:
            store.append("user", "first")
            with patch("os.fsync", side_effect=OSError("simulated disk failure")):
                with self.assertRaisesRegex(StoreError, "Uncertain log write"):
                    store.append("user", "second")
            with self.assertRaisesRegex(StoreError, "uncertain write"):
                store.append("user", "must not reuse id")
        with Store(self.root) as store:
            self.assertEqual(store.message_count, 2)
            self.assertEqual(store.append("user", "third"), [2])

    def test_event_delivery_resumes_partial_chunks_once(self):
        with Store(self.root, 10) as store:
            original_append = store._append
            def fail_second(kind, record):
                if record.get("event_part") == 1:
                    raise OSError("simulated process death")
                return original_append(kind, record)
            with patch.object(store, "_append", side_effect=fail_second):
                with self.assertRaises(OSError):
                    store.append("work", "1234567890abcdefghij", event_id="agent:Test")
        with Store(self.root, 10) as store:
            ids = store.append("work", "1234567890abcdefghij", event_id="agent:Test")
            self.assertEqual(ids, [0, 1])
            self.assertEqual(store.append("work", "1234567890abcdefghij", event_id="agent:Test"), ids)
            self.assertEqual(store.message_count, 2)

    def test_startup_detects_missing_tree_child(self):
        with Store(self.root) as store:
            for value in ["a", "b"]:
                store.append("note", value)
            store.add_node((0, 0), "a")
            store.add_node((0, 1), "b")
            store.add_node((1, 0), "a and b")
        path = next((self.root / "tree").glob("*.jsonl"))
        lines = path.read_bytes().splitlines(keepends=True)
        path.write_bytes(b"".join(lines[1:]))
        with self.assertRaisesRegex(StoreError, "missing child"):
            Store(self.root)

    def test_attachment_round_trip_and_integrity(self):
        image = self.root / "test.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"test")
        with Store(self.root) as store:
            ref = store.add_image(image)
            store.append("user", "look", attachments=[ref])
        with Store(self.root) as store:
            self.assertEqual(store.image_bytes(store.message(0)["attachments"][0]), b"\x89PNG\r\n\x1a\n" + b"test")
            (self.root / "assets" / ref["sha256"]).write_bytes(b"corrupted")
            with self.assertRaisesRegex(StoreError, "checksum"):
                store.image_bytes(ref)
        with self.assertRaisesRegex(StoreError, "checksum"):
            Store(self.root)


class ViewTests(unittest.TestCase):
    def test_address_validation(self):
        self.assertEqual(address((3, 5)), (40, 8))
        self.assertEqual(key_for(40, 8), (3, 5))
        for a, n in [(1, 2), (0, 3), (-1, 1), (False, 1), (0, 0)]:
            with self.assertRaises(ValueError):
                key_for(a, n)

    def test_ten_message_regression(self):
        nodes = [(2, 0), (2, 1), (0, 8), (0, 9)]
        self.assertEqual(candidate(nodes, 10, {(3, 0), (1, 4)}), (2, (1, 4)))

    def test_rollback_equivalence_20001_prefixes(self):
        def push(state, states):
            if states is None:
                return 0, state, None
            keep, old, older = states
            if keep == 0:
                return 1, old, older
            return 0, state, push(old, older)

        class Built:
            def __contains__(self, key):
                return True

        states, nodes = None, []
        for tick in range(20_001):
            states = push(tick, states)
            boundaries, cursor = [], states
            while cursor is not None:
                _, start, cursor = cursor
                boundaries.append(start)
            nodes.append((0, tick))
            while len(nodes) > len(boundaries):
                j, parent = candidate(nodes, tick + 1, Built())
                nodes[j:j+2] = [parent]
            ordered = list(reversed(boundaries)) + [tick + 1]
            expected = [key_for(a, b-a) for a, b in zip(ordered, ordered[1:])]
            self.assertEqual(nodes, expected, f"mismatch at t={tick}")

    def test_pending_batch_restart_and_cutoff(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = Config(view_low=70, view_high=110, compact_low=50, compact_high=90)
            with Store(temp) as store:
                view = View(store, cfg)
                for i in range(8):
                    store.append("user", str(i))
                    store.add_node((0, i), f"user: item-{i}-" + "x" * 12)
                    view.advance()
                self.assertTrue(view.main_pending)
                saved = Path(temp, "view.json").read_bytes()
            with Store(temp) as store:
                view = View(store, cfg)
                self.assertEqual(Path(temp, "view.json").read_bytes(), saved)
                self.assertTrue(view.main_pending)
                for level in (1, 2, 3):
                    for i in range(8 >> level):
                        store.add_node((level, i), f"merged {level}/{i}")
                view.advance()
                self.assertFalse(view.main_pending)
                self.assertLessEqual(view.size(view.main), cfg.view_low)
                self.assertEqual(view.end(view.main), 8)
                cutoff = view.lines(compact=True, cutoff=3)
                for line in cutoff:
                    a, n = map(int, line.split("|", 1)[0].split("+"))
                    self.assertLessEqual(a+n, 3)
                before = view.render()
            with Store(temp) as store:
                view = View(store, cfg)
                view.advance()
                self.assertEqual(view.render(), before)

    def test_append_prefix_and_recover_uncheckpointed_suffix(self):
        with tempfile.TemporaryDirectory() as temp:
            with Store(temp) as store:
                view = View(store, Config())
                store.append("user", "a")
                store.add_node((0, 0), "user: a")
                view.advance()
                prefix = "".join(view.lines())
                store.append("user", "b")
                store.add_node((0, 1), "user: b")
                # Simulate death between the fsynced node and view checkpoint.
            with Store(temp) as store:
                view = View(store, Config())
                self.assertEqual(len(view.main), 1)
                view.advance()
                self.assertTrue("".join(view.lines()).startswith(prefix))
                self.assertEqual(view.end(view.main), 2)

    def test_missing_view_refuses_refit(self):
        with tempfile.TemporaryDirectory() as temp:
            with Store(temp) as store:
                store.append("user", "a")
                with self.assertRaisesRegex(StoreError, "Missing view.json"):
                    View(store, Config())

    def test_invalid_configuration(self):
        for kwargs in [{"view_low": 128000}, {"concurrency": 9}, {"attempts": 6}, {"message_bytes": 2}]:
            with self.assertRaises(ValueError):
                Config(**kwargs)
