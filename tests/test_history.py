"""Past chats: reading Claude Code, Codex and website exports, and importing them."""
import json
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from eternalchat import history
from tests.test_engine import EngineFixture
from tests.test_web import WebFixture

KEY = "sk-ant-" + "a1B2" * 10


def lines(*records):
    return "".join(json.dumps(r) + "\n" for r in records) + "not json\n"


CLAUDE = lines(
    {"type": "user", "cwd": "/home/me/app", "timestamp": "2026-10-01T10:00:00Z",
     "message": {"content": f"Fix the login bug. My key is {KEY}"}},
    {"type": "user", "isMeta": True, "timestamp": "2026-10-01T10:00:01Z", "message": {"content": "meta"}},
    {"type": "assistant", "timestamp": "2026-10-01T10:00:02Z", "message": {"content": [
        {"type": "thinking", "thinking": "secret thoughts"},
        {"type": "text", "text": "Looking at the login code."},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "login.py"}}]}},
    {"type": "user", "timestamp": "2026-10-01T10:00:03Z", "message": {"content": [
        {"type": "tool_result", "content": "def login(): pass"}]}},
    {"type": "user", "timestamp": "2026-10-01T10:00:04Z", "message": {"content": "<command-name>/clear</command-name>"}},
    {"type": "assistant", "isSidechain": True, "timestamp": "2026-10-01T10:00:05Z",
     "message": {"content": [{"type": "text", "text": "side chain"}]}},
    {"type": "assistant", "timestamp": "2026-10-02T09:00:00Z",
     "message": {"content": [{"type": "text", "text": "Fixed: the session expired early."}]}},
)
CODEX = lines(
    {"type": "session_meta", "timestamp": "2026-10-03T08:00:00Z", "payload": {"cwd": "/home/me/site"}},
    {"type": "response_item", "timestamp": "2026-10-03T08:00:01Z", "payload": {"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "<environment_context>cwd</environment_context>"},
        {"type": "input_text", "text": "Make the header blue."}]}},
    {"type": "response_item", "timestamp": "2026-10-03T08:00:02Z",
     "payload": {"type": "function_call", "name": "shell", "arguments": "{\"command\": \"ls\"}"}},
    {"type": "response_item", "timestamp": "2026-10-03T08:00:03Z",
     "payload": {"type": "function_call_output", "output": "index.html"}},
    {"type": "response_item", "timestamp": "2026-10-03T08:00:04Z", "payload": {"type": "message", "role": "assistant",
                                                                               "content": [{"type": "output_text", "text": "Done: the header is blue."}]}},
)
SUBAGENT = lines(
    {"type": "session_meta", "timestamp": "2026-10-03T09:00:00Z", "payload": {"cwd": "/x", "source": {"subagent": "x"}}},
    {"type": "response_item", "timestamp": "2026-10-03T09:00:01Z",
     "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "internal"}]}},
)
CHATGPT = [{"title": "Trip", "create_time": 1759300000, "current_node": "c", "mapping": {
    "root": {"parent": None, "message": None},
    "a": {"parent": "root", "message": {"author": {"role": "user"}, "create_time": 1759300001,
                                        "content": {"content_type": "text", "parts": ["Plan a trip to Japan"]}}},
    "old": {"parent": "a", "message": {"author": {"role": "assistant"}, "recipient": "all", "create_time": 1759300002,
                                       "content": {"content_type": "text", "parts": ["an abandoned branch"]}}},
    "t": {"parent": "a", "message": {"author": {"role": "assistant"}, "recipient": "all", "create_time": 1759300003,
                                     "content": {"content_type": "thoughts", "thoughts": []}}},
    "h": {"parent": "t", "message": {"author": {"role": "system"}, "create_time": 1759300004,
                                     "metadata": {"is_visually_hidden_from_conversation": True},
                                     "content": {"content_type": "text", "parts": ["hidden"]}}},
    "c": {"parent": "h", "message": {"author": {"role": "assistant"}, "recipient": "all", "create_time": 1759300005,
                                     "content": {"content_type": "text", "parts": ["Two weeks: Tokyo, then Kanazawa."]}}},
}}]
CLAUDE_SITE = [{"name": "Garden", "created_at": "2026-09-01T12:00:00Z", "chat_messages": [
    {"sender": "human", "created_at": "2026-09-01T12:00:01Z", "text": "Why are the tomato leaves yellow?",
     "attachments": [{"file_name": "leaf.jpg"}]},
    {"sender": "assistant", "created_at": "2026-09-01T12:00:02Z",
     "content": [{"type": "text", "text": "Probably nitrogen."}]}]}]


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        projects = self.root / ".claude" / "projects" / "app"
        sessions = self.root / ".codex" / "sessions" / "2026" / "10"
        projects.mkdir(parents=True)
        (projects / "subagents").mkdir()
        sessions.mkdir(parents=True)
        (projects / "one.jsonl").write_text(CLAUDE)
        (projects / "subagents" / "agent.jsonl").write_text(CLAUDE)
        (sessions / "rollout-a.jsonl").write_text(CODEX)
        (sessions / "rollout-b.jsonl").write_text(SUBAGENT)
        self.paths = [self.root / ".claude" / "projects", self.root / ".codex" / "sessions"]

    def test_logs_bring_words_and_replies_never_thinking_meta_or_injected_context(self):
        claude, codex = sorted(history.find(self.paths), key=lambda s: s.app)
        self.assertEqual((claude.app, claude.where, claude.start), ("Claude Code", "session in /home/me/app",
                                                                    "2026-10-01T10:00:00+00:00"))
        self.assertEqual([(k, t) for k, t, _ in claude.messages], [
            ("user", "Fix the login bug. My key is [redacted]"), ("unii", "Looking at the login code."),
            ("unii", "Fixed: the session expired early.")])
        self.assertEqual([(k, t) for k, t, _ in codex.messages],
                         [("user", "Make the header blue."), ("unii", "Done: the header is blue.")])
        self.assertTrue(codex.note.startswith("Codex session in /home/me/site, from 2026-10-03 08:00 UTC"))
        self.assertNotIn("thoughts", json.dumps(claude.messages))

    def test_tool_calls_only_when_asked_and_since_and_done_filter(self):
        claude = next(s for s in history.find(self.paths, tools=True) if s.app == "Claude Code")
        kinds = [k for k, _, _ in claude.messages]
        self.assertEqual(kinds, ["user", "unii", "tool", "echo", "unii"])
        self.assertIn('Read {"file_path": "login.py"}', claude.messages[2][1])
        later = history.find(self.paths, since="2026-10-02")
        self.assertEqual([(s.app, len(s.messages)) for s in later], [("Codex", 2), ("Claude Code", 1)])
        done = {claude.key: "2026-10-01T23:59:59+00:00"}
        again = [s for s in history.find(self.paths, done=done) if s.app == "Claude Code"]
        self.assertEqual([t for _, t, _ in again[0].messages], ["Fixed: the session expired early."])
        old = time.time() - 40 * 86400
        for log in self.root.rglob("*.jsonl"):
            os.utime(log, (old, old))
        self.assertEqual(history.find(self.paths, since="2026-10-01"), [])  # files last written before since

    def test_website_exports_follow_the_shown_thread(self):
        export = self.root / "chatgpt.zip"
        with zipfile.ZipFile(export, "w") as archive:
            archive.writestr("export/conversations.json", json.dumps(CHATGPT))
        site = self.root / "conversations.json"
        site.write_text(json.dumps(CLAUDE_SITE))
        chats = history.find([export, site])
        trip = next(s for s in chats if s.app == "ChatGPT")
        self.assertEqual(trip.where, 'chat "Trip"')
        self.assertEqual([t for _, t, _ in trip.messages], ["Plan a trip to Japan", "Two weeks: Tokyo, then Kanazawa."])
        garden = next(s for s in chats if s.app == "Claude")
        self.assertEqual([t for _, t, _ in garden.messages],
                         ["Why are the tomato leaves yellow?\n[file leaf.jpg]", "Probably nitrogen."])
        bad = self.root / "other.json"
        bad.write_text("[1, 2]")
        with self.assertRaisesRegex(ValueError, "not a Claude or ChatGPT export"):
            history.find([bad])

    def test_redaction(self):
        text = ("ghp_" + "x" * 36 + " AKIAABCDEFGHIJKLMNOP password=hunter2hunter2 Bearer " + "t" * 30
                + "\n-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----")
        self.assertEqual(history.redact(text).count("[redacted]"), 5)
        self.assertIn("password=[redacted]", history.redact(text))
        self.assertEqual(history.redact("nothing secret here"), "nothing secret here")


class ImportTests(EngineFixture):
    async def test_sessions_land_as_a_note_and_dated_messages_once(self):
        temp = Path(self.temp.name, "logs")
        temp.mkdir()
        (temp / "a.jsonl").write_text(CODEX)
        events = []
        engine = self.engine(events=lambda kind, value: events.append((kind, value)))
        sessions = history.find([temp])
        self.assertEqual(engine.import_chats(sessions), 3)
        records = [engine.store.message(i) for i in range(engine.store.message_count)]
        self.assertEqual([r["kind"] for r in records], ["note", "user", "unii"])
        self.assertTrue(records[0]["text"].endswith("(imported)"))
        self.assertEqual(records[1]["date"], "2026-10-03T08:00:01+00:00")
        self.assertEqual([v for k, v in events if k == "message"], [{"ids": [0, 2], "imported": 3}])
        self.assertEqual(history.find([temp], done=engine.imported()), [])
        await engine.finish()
        self.assertEqual(engine.status()["summarized_messages"], 3)


class ImportWebTests(WebFixture):
    async def test_the_page_scans_then_imports_what_was_picked(self):
        await self.serve()
        logs = Path(self.temp.name, "logs")
        logs.mkdir()
        (logs / "a.jsonl").write_text(CODEX)
        (logs / "b.jsonl").write_text(CLAUDE)
        status, body, _ = await self.request("GET", f"/api/import?paths={logs}&since=2026-10-01")
        self.assertEqual(status, 200)
        found = {s["app"]: s for s in body["sessions"]}
        self.assertEqual((found["Codex"]["messages"], found["Claude Code"]["messages"]), (2, 3))
        status, body, _ = await self.request("POST", "/api/import", {"keys": [found["Codex"]["key"]]})
        self.assertEqual((status, body), (200, {"imported": 3}))
        self.assertEqual((await self.request("POST", "/api/import", {"keys": ["unknown"]}))[0], 400)
        self.assertEqual((await self.request("GET", "/api/import?since=yesterday"))[0], 400)
        status, body, _ = await self.request("GET", f"/api/import?paths={logs}")
        self.assertEqual([s["app"] for s in body["sessions"]], ["Claude Code"])  # Codex is in already
