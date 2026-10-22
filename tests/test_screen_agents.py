"""Task-wide screen ownership, isolated agents and durable device-bound resumes."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from eternalchat.app import open_chat
from eternalchat.cli_provider import CLIProvider
from eternalchat.config import Config
from eternalchat.devices import Devices
from eternalchat.errors import ProviderError
from eternalchat.llm import Response
from eternalchat.machine import Machine
from eternalchat.prompt import SCREEN_TOOLS
from tests.test_computer import JPEG, screenshot
from tests.test_engine import EngineFixture, Scripted, text_response
from tests.subscription_fake import FAKE_CLI


def action(name="computer", **args):
    return Response([{"type": "tool_use", "id": "act", "name": name, "input": args}])


def is_summary(kw):
    return kw["messages"][0]["content"][-1].get("text", "").startswith("Compaction:")


class ScreenMachine(Machine):
    @property
    def computer_supported(self):
        return True


class ScreenAgentTests(EngineFixture):
    def chat(self, handler):
        machine = ScreenMachine(Path(self.temp.name, "work"), "here")
        machine.computer.run = AsyncMock(return_value=(screenshot(), False))
        provider = Scripted(handler)
        engine = open_chat(Path(self.temp.name, "chat"), provider, machine=Devices(machine))
        self.engines.append(engine)
        return engine, provider, machine

    async def settle(self, engine):
        await asyncio.gather(*engine.agents.values(), return_exceptions=True)
        await engine.finish()

    async def test_parent_stays_responsive_and_workers_have_only_task_and_screen(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(kw):
            if is_summary(kw):
                return text_response("summary")
            if kw["purpose"] == "agent":
                self.assertEqual(kw["tools"], SCREEN_TOOLS)
                self.assertNotIn("PRIVATE_CHAT_MEMORY", json.dumps([kw["system"], kw["messages"]]))
                self.assertNotIn("<chat>", json.dumps(kw["messages"]))
                self.assertNotIn("device", kw["tools"][0]["input_schema"]["properties"])
                entered.set()
                await release.wait()
                return text_response("Screen task done")
            if len(kw["messages"]) == 1 and kw["messages"][-1]["content"][-1].get("text") == "start":
                return action(task="Inspect the open page", device="here")
            return text_response("Chat is free")

        engine, _, machine = self.chat(handler)
        engine.note("PRIVATE_CHAT_MEMORY")
        self.assertEqual(await asyncio.wait_for(engine.ask("start"), 2), "Chat is free")
        await asyncio.wait_for(entered.wait(), 2)
        name = next(iter(engine.agents))
        self.assertFalse(engine.agents[name].done())
        self.assertEqual(await asyncio.wait_for(engine.ask("hello"), 2), "Chat is free")
        with self.assertRaisesRegex(ValueError, "one task at a time"):
            engine.start_screen("Competing work", device="here")
        with self.assertRaisesRegex(ValueError, name):
            await machine.run("computer", {"action": "screenshot"}, "OtherAgent")
        release.set()
        await self.settle(engine)
        self.assertIsNone(machine.screen.lease)
        self.assertEqual(engine.agent_log(name)["role"]["kind"], "screen")

    async def test_worker_cannot_escape_into_other_tools_or_devices(self):
        attempted = [action("shell", command="touch forbidden"), action("zoom", id=0),
                     action("search", query="forbidden"), action("spawn", tasks=["forbidden"]),
                     action(action="screenshot", device="other"), action(action="screenshot")]

        async def handler(kw):
            if is_summary(kw):
                return text_response("summary")
            if kw["purpose"] != "agent":
                return text_response("ack")
            if len(kw["messages"]) > 1:
                result = kw["messages"][-1]["content"][0]
                self.assertEqual(result["is_error"], len(attempted) > 0)
            return attempted.pop(0) if attempted else text_response("done")

        engine, _, machine = self.chat(handler)
        name = engine.start_screen("Inspect only")
        await self.settle(engine)
        machine.computer.run.assert_awaited_once_with({"action": "screenshot"})
        records = engine.agent_log(name)["records"]
        errors = [r for r in records if r["kind"] == "echo" and r["text"].startswith("Tool error:")]
        self.assertEqual(len(errors), 5)
        self.assertFalse((machine.work / name / "forbidden").exists())
        self.assertIsNone(machine.screen.lease)

    async def test_screenshots_and_role_survive_restart_and_tell_keeps_isolation(self):
        async def handler(kw):
            if is_summary(kw):
                return text_response("summary")
            if kw["purpose"] != "agent":
                return text_response("ack")
            if len(kw["messages"]) == 1:
                return action(action="screenshot")
            self.assertEqual(kw["messages"][-1]["content"][0]["content"][1], screenshot()[1])
            return text_response("Observed the screen")

        engine, provider, machine = self.chat(handler)
        name = engine.start_screen("Inspect the screen")
        await self.settle(engine)
        echo = next(m for m in engine.agent_log(name)["records"] if m["kind"] == "echo")
        ref = echo["attachments"][0]
        self.assertEqual(engine.image(ref), JPEG)
        with engine.store.open_agent_log(name) as transcript:
            self.assertEqual(transcript.image_bytes(ref), JPEG)
        await engine.close()
        machine = ScreenMachine(Path(self.temp.name, "work"), "here")
        machine.computer.run = AsyncMock(return_value=(screenshot(), False))
        reopened = open_chat(Path(self.temp.name, "chat"), provider, machine=Devices(machine))
        self.engines.append(reopened)
        reopened.tell(name, "Inspect again")
        await self.settle(reopened)
        resumed = [c for c in provider.calls if c["purpose"] == "agent" and "transcript so far" in json.dumps(c["messages"])]
        self.assertTrue(resumed)
        self.assertEqual(resumed[0]["tools"], SCREEN_TOOLS)
        self.assertIn(screenshot()[1], resumed[0]["messages"][0]["content"])
        self.assertEqual(reopened.store.agent_state(name)["role"]["device"], "here")
        self.assertEqual(reopened.store.agent_state(name)["replies"], 1)
        self.assertIsNone(machine.screen.lease)

    async def test_tell_during_a_model_call_reaches_the_screen_worker(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(kw):
            if kw["purpose"] != "agent" or is_summary(kw):
                return text_response("ack")
            if len(kw["messages"]) == 1:
                entered.set()
                await release.wait()
                return text_response("First part")
            self.assertIn("Inspect the other tab", json.dumps(kw["messages"][-1]))
            return text_response("Second part")

        engine, _, machine = self.chat(handler)
        name = engine.start_screen("Inspect one tab")
        await entered.wait()
        engine.tell(name, "Inspect the other tab")
        release.set()
        await self.settle(engine)
        self.assertIn("Second part", engine.store.agent_state(name)["report"])
        self.assertIsNone(machine.screen.lease)

    async def test_accepted_tells_are_durable_before_a_worker_can_read_them(self):
        entered = asyncio.Event()

        async def handler(kw):
            if kw["purpose"] != "agent":
                return text_response("ack")
            entered.set()
            await asyncio.Event().wait()

        engine, provider, machine = self.chat(handler)
        name = engine.start_screen("Inspect")
        engine.tell(name, "First correction")  # before the coroutine starts
        await entered.wait()
        engine.tell(name, "Second correction")  # while the provider call waits
        engine.stop_agent(name)
        await self.settle(engine)
        await engine.close()

        async def resumed(kw):
            if kw["purpose"] == "agent":
                text = json.dumps(kw["messages"])
                self.assertIn("First correction", text)
                self.assertIn("Second correction", text)
            return text_response("resumed")

        provider.handler = resumed
        reopened = open_chat(Path(self.temp.name, "chat"), provider, machine=machine)
        self.engines.append(reopened)
        reopened.tell(name, "Continue")
        await self.settle(reopened)
        texts = [r["text"] for r in reopened.agent_log(name)["records"] if r["kind"] == "user"]
        self.assertEqual(texts, ["Inspect", "First correction", "Second correction", "Continue"])
        self.assertIsNone(machine.screen.lease)

    async def test_continuing_a_worker_cannot_overwrite_an_undelivered_report(self):
        async def handler(kw):
            return text_response("report" if kw["purpose"] == "agent" else "ack")

        engine, _, machine = self.chat(handler)
        with patch.object(engine, "submit", side_effect=RuntimeError("delivery failed")):
            name = engine.start_screen("Inspect")
            with self.assertRaisesRegex(RuntimeError, "delivery failed"):
                await engine.agents[name]
        self.assertIsNone(machine.screen.lease)
        engine.tell(name, "Continue")
        await self.settle(engine)
        reports = [m["text"] for m in engine.messages() if m["kind"] == "work"]
        self.assertEqual(reports, [f"[{name}] report", f"[{name}] report"])

    async def test_failure_stop_immediate_cancel_and_setup_failure_release_ownership(self):
        entered = asyncio.Event()

        async def handler(kw):
            if kw["purpose"] != "agent" or is_summary(kw):
                return text_response("ack")
            entered.set()
            await asyncio.Event().wait()

        engine, provider, machine = self.chat(handler)
        name = engine.start_screen("First")
        engine.stop_agent(name)  # before its coroutine starts
        await self.settle(engine)
        self.assertEqual(engine.store.agent_state(name)["status"], "interrupted")
        self.assertIsNone(machine.screen.lease)
        name = engine.start_screen("Second")
        await entered.wait()
        self.assertTrue(engine.stop_agent(name))
        with self.assertRaisesRegex(ValueError, "stopping"):
            engine.tell(name, "Too early")
        await self.settle(engine)
        self.assertIsNone(machine.screen.lease)
        self.assertEqual(engine.store.agent_state(name)["status"], "interrupted")
        self.assertFalse(engine.stop_agent(name))
        with patch.object(engine.store, "create_agent", side_effect=RuntimeError("disk failed")):
            with self.assertRaisesRegex(RuntimeError, "disk failed"):
                engine.start_screen("Third")
        self.assertIsNone(machine.screen.lease)

        async def fail(kw):
            if kw["purpose"] == "agent":
                raise ProviderError("model failed")
            return text_response("ack")
        provider.handler = fail
        name = engine.start_screen("Fourth")
        await self.settle(engine)
        self.assertEqual(engine.store.agent_state(name)["status"], "failed")
        self.assertIsNone(machine.screen.lease)

    async def test_remote_binding_alias_ownership_and_replaced_connection_do_not_redirect_actions(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(kw):
            if kw["purpose"] != "agent" or is_summary(kw):
                return text_response("ack")
            if len(kw["messages"]) == 1:
                entered.set()
                await release.wait()
                return action(action="screenshot")
            self.assertTrue(kw["messages"][-1]["content"][0]["is_error"])
            return text_response("Device left; work remains")

        engine, _, machine = self.chat(handler)
        old = engine.machine.join("remote", "/work", True, "host:remote")
        engine.machine.join("alias", "/work", True, "host:remote")
        name = engine.start_screen("Inspect remote", device="remote")
        await entered.wait()
        with self.assertRaisesRegex(ValueError, "one task at a time"):
            engine.start_screen("Alias task", device="alias")
        replacement = engine.machine.join("remote", "/new", True, "host:different")
        release.set()
        await self.settle(engine)
        self.assertTrue(replacement.outbox.empty())
        self.assertTrue(old.outbox.empty())
        with self.assertRaisesRegex(ValueError, "identity changed"):
            engine.tell(name, "Continue")
        self.assertIsNone(old.screen.lease)
        engine.machine.join("local-alias", "/work", True, machine.screen.identity)
        name = engine.start_screen("Local task")
        with self.assertRaisesRegex(ValueError, "one task at a time"):
            engine.start_screen("Same physical screen", device="local-alias")
        engine.stop_agent(name)
        await self.settle(engine)

    async def test_remote_action_and_stop_route_to_the_original_device(self):
        async def handler(kw):
            if kw["purpose"] != "agent" or is_summary(kw):
                return text_response("ack")
            return action(action="screenshot") if len(kw["messages"]) == 1 else text_response("done")

        engine, _, _ = self.chat(handler)
        remote = engine.machine.join("remote", "/work", True)
        name = engine.start_screen("Inspect", device="remote")
        job = (await asyncio.wait_for(remote.outbox.get(), 2))["job"]
        self.assertEqual(job["caller"], name)
        self.assertEqual(job["input"], {"action": "screenshot"})
        remote.answer(job["id"], screenshot(), False)
        await self.settle(engine)
        self.assertIsNone(remote.screen.lease)
        engine.tell(name, "Inspect again")
        job = (await asyncio.wait_for(remote.outbox.get(), 2))["job"]
        engine.stop_agent(name)
        await self.settle(engine)
        self.assertEqual(await remote.outbox.get(), {"stop": job["id"]})
        self.assertFalse(remote.pending)
        self.assertIsNone(remote.screen.lease)

    async def test_unavailable_devices_and_ordinary_agent_actions_fail_without_starting_workers(self):
        async def handler(kw):
            if is_summary(kw) or kw["purpose"] != "agent":
                return text_response("ack")
            self.assertNotIn("computer", {t["name"] for t in kw["tools"]})
            return action(action="screenshot") if len(kw["messages"]) == 1 else text_response("No screen tool")

        engine, _, machine = self.chat(handler)
        engine.machine.join("linux", "/work", False)
        for task, device in (("", None), ("task", "missing"), ("task", "linux"), ("task", 42)):
            with self.assertRaises(ValueError):
                engine.start_screen(task, device=device)
        self.assertFalse(engine.agents)
        await engine.start_agent("Ordinary", "Try a screen action")
        await self.settle(engine)
        machine.computer.run.assert_not_awaited()
        self.assertTrue(any("Ordinary subagents have no screen tool" in m["text"] for m in engine.agent_log("Ordinary")["records"]))

    async def test_subscription_protocols_keep_screen_worker_images_and_tools_isolated(self):
        executable = Path(self.temp.name, "fake-cli")
        executable.write_text(FAKE_CLI)
        executable.chmod(0o700)
        for client in ("codex", "claudecode"):
            with self.subTest(client=client):
                machine = ScreenMachine(Path(self.temp.name, client, "work"), "here")
                machine.computer.run = AsyncMock(return_value=(screenshot(), False))
                engine = open_chat(Path(self.temp.name, client, "chat"),
                    CLIProvider(client, executable=str(executable)), Config(turn_model="screen-test"), machine=machine)
                self.engines.append(engine)
                self.assertEqual(await engine.ask("Start a screen task"), "Worker is in the background.")
                await self.settle(engine)
                name = next(iter(engine.agents))
                state = engine.store.agent_state(name)
                self.assertEqual(state["status"], "complete")
                self.assertIn("native image input", state["report"])
                echo = next(m for m in engine.agent_log(name)["records"] if m["kind"] == "echo")
                self.assertEqual(engine.image(echo["attachments"][0]), JPEG)
                machine.computer.run.assert_awaited_once_with({"action": "screenshot"})
                self.assertIsNone(machine.screen.lease)
                self.assertEqual(engine.provider.stats()["last_model"]["id"], "screen-test")
                await engine.close()
