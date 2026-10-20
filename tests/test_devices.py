"""Other computers: joined with `eternalchat device`, reached by the device tools."""
import asyncio
from argparse import Namespace
from pathlib import Path

from eternalchat import web
from eternalchat.app import open_chat
from eternalchat.config import Config
from eternalchat.devices import Devices
from eternalchat.llm import Response
from eternalchat.machine import Machine
from eternalchat.web import WebChat
from tests.test_engine import EngineFixture, Scripted, text_response
from tests.test_web import Quiet, WebFixture


class DevicesTests(EngineFixture):
    def devices(self):
        devices = Devices(Machine(Path(self.temp.name, "here"), name="mac"))
        self.addCleanup(devices.close)
        return devices

    async def test_calls_go_to_the_computer_named_and_wait_for_its_answer(self):
        devices = self.devices()
        text, failed = await devices.run("shell", {"command": "echo here", "device": "mac"}, "main")
        self.assertTrue(text.startswith("here\n"))
        text, failed = await devices.run("shell", {"command": "echo x", "device": "pc"}, "main")
        self.assertTrue(failed)
        self.assertEqual(text, "No computer named pc is connected; these are: mac.")
        with self.assertRaises(ValueError):
            devices.join("mac", "/w")
        pc = devices.join("pc", "/home/me/work")
        call = asyncio.create_task(devices.run("read", {"path": "a.txt", "device": "pc"}, "Scout"))
        sent = await pc.outbox.get()
        self.assertEqual(sent["job"]["tool"], "read")
        self.assertEqual(sent["job"]["input"], {"path": "a.txt"})
        self.assertEqual(sent["job"]["caller"], "Scout")
        pc.answer(sent["job"]["id"], "     1\thello", False)
        self.assertEqual(await call, ("     1\thello", False))
        self.assertIn("pc (your working folder: /home/me/work/main)", devices.describe("main"))
        self.assertEqual([c["name"] for c in devices.listing()], ["mac", "pc"])

    async def test_a_stopped_call_tells_the_computer_and_a_leaving_one_fails_its_calls(self):
        devices = self.devices()
        pc = devices.join("pc", "/w")
        call = asyncio.create_task(devices.run("shell", {"command": "sleep 9", "device": "pc"}, "main"))
        job = (await pc.outbox.get())["job"]["id"]
        call.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await call
        self.assertEqual(await pc.outbox.get(), {"stop": job})
        call = asyncio.create_task(devices.run("shell", {"command": "sleep 9", "device": "pc"}, "main"))
        await pc.outbox.get()
        again = devices.join("pc", "/w")  # the same name joining again: the old one leaves
        self.assertEqual(await call, ("The computer pc left before this finished.", True))
        self.assertIs(devices.remote["pc"], again)


class JoinTests(WebFixture):
    async def test_a_computer_joins_over_http_and_runs_what_the_model_sends_it(self):
        async def handler(kw):
            if kw["messages"][0]["content"][-1]["text"].startswith("Compaction:"):
                return text_response("summary")
            if len(kw["messages"]) == 1:
                return Response([{"type": "tool_use", "id": "t1", "name": "shell",
                                  "input": {"command": "pwd", "device": "laptop"}}], "tool_use")
            return text_response("done")
        root = Path(self.temp.name)

        def open_engine(options, on_event):
            engine = open_chat(root / "chat", Scripted(handler), Config(), on_event,
                               machine=Devices(Machine(root / "server", name="server")))
            self.engines.append(engine)
            return engine
        args = Namespace(connection="api", cli="codex", model=None, summary_model=None, demo=True, tools="auto",
                         data_dir=str(root / "chat"), reasoning_effort=None)
        chat = WebChat(args, open_engine, Quiet(), "t" * 43)
        url = await chat.start(0)
        self.addAsyncCleanup(chat.close)
        lent = asyncio.create_task(web.device(url, "laptop", root / "laptop", Quiet()))
        self.addCleanup(lent.cancel)
        for _ in range(200):
            if "laptop" in chat.remotes:
                break
            await asyncio.sleep(0.02)
        self.assertEqual([c["name"] for c in chat.state()["computers"]], ["server", "laptop"])
        self.assertEqual(await chat.engine.ask("where are you?"), "done")
        echo = next(chat.engine.store.message(i)["text"] for i in range(chat.engine.store.message_count)
                    if chat.engine.store.message(i)["kind"] == "echo")
        self.assertEqual(Path(echo.splitlines()[0]).resolve(), (root / "laptop" / "main").resolve())
        with self.assertRaisesRegex(RuntimeError, "#token"):
            await web.device("https://example.com/", "x", root / "x", Quiet())
