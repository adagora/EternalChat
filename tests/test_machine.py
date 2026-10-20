"""The computer's tools: commands, files, stops, and their place in a turn."""
import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from eternalchat.app import open_chat
from eternalchat.config import Config
from eternalchat.llm import Response
from eternalchat.machine import Machine
from eternalchat.prompt import BOSS_NAMES, DEVICE_NAMES, tools_for
from tests.test_engine import EngineFixture, Scripted, text_response


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def until_gone(pid, timeout=5):
    deadline = time.monotonic() + timeout
    while alive(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    return not alive(pid)


class MachineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.machine = Machine(Path(self.temp.name, "work"), name="testbox")
        self.addCleanup(self.machine.close)
        self.main = Path(self.temp.name, "work", "main")

    async def test_shell_answers_output_then_exit_and_runs_in_the_callers_folder(self):
        text, failed = await self.machine.run("shell", {"command": "pwd; echo hi; echo oops >&2"}, "main")
        self.assertFalse(failed)
        lines = text.splitlines()
        self.assertEqual(Path(lines[0]).resolve(), self.main.resolve())
        self.assertEqual(lines[1:3], ["hi", "oops"])
        self.assertRegex(lines[-1], r"^\(exit 0 in \d+\.\d s\)$")
        text, failed = await self.machine.run("shell", {"command": "exit 3"}, "Scout")
        self.assertTrue(failed)
        self.assertIn("(exit 3 in", text)
        self.assertTrue(Path(self.temp.name, "work", "Scout").is_dir())

    async def test_shell_keeps_head_and_tail_of_long_output_and_no_secrets(self):
        text, _ = await self.machine.run("shell", {"command": "seq 1 200000"}, "main")
        self.assertIn("clipped", text)
        self.assertTrue(text.startswith("1\n2\n"))
        self.assertIn("200000\n(exit 0", text)
        self.assertLess(len(text), 40_000)
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-secret", "ETERNALCHAT_HOME": "/x"}):
            text, _ = await self.machine.run(
                "shell", {"command": "echo ${ANTHROPIC_API_KEY:-none} ${ETERNALCHAT_HOME:-none}"}, "main")
        self.assertTrue(text.startswith("none none\n"))
        text, failed = await self.machine.run("shell", {"command": "read line; echo got:$line"}, "main")
        self.assertIn("got:", text)  # no input: a read gets end of file, never waits

    async def test_a_stopped_command_ends_with_everything_it_started(self):
        script = "echo $$ > shell.pid; sleep 30 & echo $! > child.pid; wait"
        task = asyncio.create_task(self.machine.run("shell", {"command": script}, "main"))
        for _ in range(100):
            if (self.main / "child.pid").exists() and (self.main / "child.pid").read_text().strip():
                break
            await asyncio.sleep(0.05)
        shell, child = (int((self.main / name).read_text()) for name in ("shell.pid", "child.pid"))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(await until_gone(shell))
        self.assertTrue(await until_gone(child))

    async def test_closing_ends_running_commands(self):
        task = asyncio.create_task(self.machine.run("shell", {"command": "echo $$ > s.pid; sleep 30"}, "main"))
        for _ in range(100):
            if (self.main / "s.pid").exists() and (self.main / "s.pid").read_text().strip():
                break
            await asyncio.sleep(0.05)
        pid = int((self.main / "s.pid").read_text())
        self.machine.close()
        self.assertTrue(await until_gone(pid))
        text, failed = await task
        self.assertTrue(failed)

    async def test_read_numbers_lines_and_says_where_to_go_on(self):
        self.main.mkdir(parents=True)
        (self.main / "f.txt").write_text("".join(f"line {k}\n" for k in range(1, 16)))
        text, failed = await self.machine.run("read", {"path": "f.txt", "offset": 4, "limit": 4}, "main")
        self.assertFalse(failed)
        self.assertEqual(text.splitlines()[0], "     4\tline 4")
        self.assertTrue(text.endswith("[lines 4-7 of 15]"))
        long = self.main / "long.txt"
        long.write_text("".join(f"{'x' * 90} {k}\n" for k in range(2000)))
        text, _ = await self.machine.run("read", {"path": str(long)}, "main")
        self.assertRegex(text.splitlines()[-1], r"^\[lines 1-\d+ of 2000; read on with offset=\d+\]$")
        (self.main / "b.bin").write_bytes(b"\0\1\2")
        text, _ = await self.machine.run("read", {"path": "b.bin"}, "main")
        self.assertIn("binary file of 3 bytes", text)
        with self.assertRaises(FileNotFoundError):
            await self.machine.run("read", {"path": "missing.txt"}, "main")
        with self.assertRaises(ValueError):
            await self.machine.run("read", {"path": 5}, "main")

    async def test_write_makes_folders_and_answers_a_diff(self):
        text, failed = await self.machine.run("write", {"path": "a/b/new.txt", "content": "one\ntwo\n"}, "main")
        self.assertFalse(failed)
        self.assertEqual((self.main / "a/b/new.txt").read_text(), "one\ntwo\n")
        self.assertIn("+one", text)
        self.assertIn("+++ b/a/b/new.txt", text)
        text, _ = await self.machine.run("write", {"path": "a/b/new.txt", "content": "one\n2\n"}, "main")
        self.assertIn("-two", text)
        self.assertIn("+2", text)

    async def test_edit_is_all_or_nothing(self):
        self.main.mkdir(parents=True)
        first, second = self.main / "one.py", self.main / "two.py"
        first.write_text("x = 1\ny = 1\n")
        second.write_text("z = 1\n")
        text, failed = await self.machine.run("edit", {"edits": [
            {"path": "one.py", "old": "x = 1", "new": "x = 2"},
            {"path": "two.py", "old": "missing", "new": "q"}]}, "main")
        self.assertTrue(failed)
        self.assertIn("edit 2", text)
        self.assertEqual(first.read_text(), "x = 1\ny = 1\n")
        text, failed = await self.machine.run("edit", {"edits": [{"path": "one.py", "old": "= 1", "new": "= 3"}]},
                                              "main")
        self.assertTrue(failed)
        self.assertIn("2 times", text)
        text, failed = await self.machine.run("edit", {"edits": [
            {"path": "one.py", "old": "= 1", "new": "= 3", "all": True},
            {"path": "one.py", "old": "y = 3", "new": "y = 4"},
            {"path": "two.py", "old": "z", "new": "w"}]}, "main")
        self.assertFalse(failed)
        self.assertEqual(first.read_text(), "x = 3\ny = 4\n")
        self.assertEqual(second.read_text(), "w = 1\n")
        self.assertIn("+y = 4", text)
        self.assertIn("+++ b/two.py", text)
        with self.assertRaises(ValueError):
            await self.machine.run("edit", {"edits": [{"path": "one.py", "old": "x"}]}, "main")


class TurnTests(EngineFixture):
    def chat(self, provider, events=None):
        machine = Machine(Path(self.temp.name, "work"), name="testbox")
        engine = open_chat(Path(self.temp.name, "chat"), provider, Config(), events, machine=machine)
        self.engines.append(engine)
        return engine

    async def test_a_turn_runs_a_command_and_logs_call_and_result(self):
        async def handler(kw):
            if kw["messages"][0]["content"][-1]["text"].startswith("Compaction:"):
                return text_response("summary")
            if len(kw["messages"]) == 1:
                return Response([{"type": "tool_use", "id": "t1", "name": "shell",
                                  "input": {"command": "echo machine-ok"}}], "tool_use")
            return text_response("done")
        provider = Scripted(handler)
        engine = self.chat(provider)
        self.assertEqual(await engine.ask("run it " + "please " * 120), "done")  # long: its summary asks the model
        await engine.finish()
        kinds = [engine.store.message(i)["kind"] for i in range(engine.store.message_count)]
        self.assertEqual(kinds, ["user", "tool", "echo", "unii"])
        self.assertIn("machine-ok", engine.store.message(2)["text"])
        turns = [c for c in provider.calls if not c["messages"][0]["content"][-1]["text"].startswith("Compaction:")]
        names = [tool["name"] for tool in turns[0]["tools"]]
        self.assertEqual(set(names), {"zoom", "date"} | BOSS_NAMES | DEVICE_NAMES)
        dynamic = turns[0]["messages"][0]["content"][-2]["text"]
        self.assertIn("Computer: testbox; your working folder:", dynamic)
        summaries = [c for c in provider.calls if c not in turns]
        self.assertTrue(summaries)
        self.assertTrue(all(c["tools"] == turns[0]["tools"] for c in summaries))  # one cached prefix

    async def test_stop_ends_a_running_command(self):
        started = asyncio.Event()

        async def handler(kw):
            if kw["messages"][0]["content"][-1]["text"].startswith("Compaction:"):
                return text_response("summary")
            started.set()
            return Response([{"type": "tool_use", "id": "t1", "name": "shell",
                              "input": {"command": "echo $$ > s.pid; sleep 30"}}], "tool_use")
        engine = self.chat(Scripted(handler))
        reply = engine.submit("sleep")
        pid_file = Path(self.temp.name, "work", "main", "s.pid")
        for _ in range(200):
            if pid_file.exists() and pid_file.read_text().strip():
                break
            await asyncio.sleep(0.05)
        pid = int(pid_file.read_text())
        engine.stop()
        with self.assertRaises(asyncio.CancelledError):
            await reply
        self.assertTrue(await until_gone(pid))

    async def test_without_a_machine_only_memory_tools_are_offered(self):
        provider = Scripted(lambda kw: asyncio.sleep(0, text_response("hi")))
        engine = self.engine(provider)
        await engine.ask("hello")
        self.assertEqual(provider.calls[0]["tools"], tools_for(False, boss=True))
        self.assertNotIn("Computer:", str(provider.calls[0]["messages"]))

    async def test_without_a_machine_a_device_call_fails_as_an_unknown_tool(self):
        tool = Response([{"type": "tool_use", "id": "t", "name": "shell", "input": {"command": "true"}}], "tool_use")

        async def handler(kw):
            if kw["messages"][0]["content"][-1]["text"].startswith("Compaction:"):
                return text_response("summary")
            return tool if len(kw["messages"]) == 1 else text_response("no shell")
        engine = self.engine(Scripted(handler))
        self.assertEqual(await engine.ask("try"), "no shell")
        echo = [engine.store.message(i) for i in range(engine.store.message_count)
                if engine.store.message(i)["kind"] == "echo"]
        self.assertIn("Unknown tool: shell", echo[0]["text"])
