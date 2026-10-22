"""Dropped transports must not repeat side effects or switch target identity."""
import asyncio
import json
from pathlib import Path
import stat
import time
from unittest.mock import patch

from eternalchat.app import open_chat
from eternalchat.cli import parser
from eternalchat.config import Config
from eternalchat.demo import DemoProvider
from eternalchat.device_client import DeviceClient
from eternalchat.device_state import DeviceState
from eternalchat.devices import Devices, Remote
from eternalchat.machine import Machine
from eternalchat.remote import RemoteError
from eternalchat.web import WebChat
from tests.test_engine import EngineFixture
from tests.test_web import Quiet


class ReconnectTests(EngineFixture):
    async def serve(self):
        root = Path(self.temp.name)
        args = parser().parse_args(["--demo", "--data-dir", str(root / "chat"), "web", "--no-browser"])

        def opened(options, events):
            engine = open_chat(root / "chat", DemoProvider(), Config(), events,
                               machine=Devices(Machine(root / "server", name="server")))
            self.engines.append(engine)
            return engine

        self.chat = WebChat(args, opened, Quiet(), "t" * 43)
        self.url = await self.chat.start(0)
        self.addAsyncCleanup(self.chat.close)
        return self.chat

    async def wait_for(self, condition):
        async with asyncio.timeout(5):
            while not condition():
                await asyncio.sleep(0.01)

    async def stop_task(self, task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def start(self, machine=None, *, drop=False):
        machine = machine or Machine(Path(self.temp.name, "laptop"), name="laptop")
        client = DeviceClient(self.url, machine, Quiet())
        if drop:
            client.dropped = self.drop_one_stream(client)
        task = asyncio.create_task(client.run())
        self.addAsyncCleanup(self.stop_task, task)
        await self.wait_for(lambda: "laptop" in self.chat.remotes)
        return client, task

    def drop_one_stream(self, client):
        original = client.client.events
        dropped = asyncio.Event()

        async def events(path):
            stream = original(path)
            try:
                async for item in stream:
                    yield item
                    if "job" in item and not dropped.is_set():
                        dropped.set()
                        raise ConnectionError("simulated link loss after job frame")
            finally:
                await stream.aclose()

        client.client.events = events
        return dropped

    async def test_drop_and_replay_job_plus_lost_result_ack_executes_shell_once(self):
        await self.serve()
        with patch("eternalchat.web.PING", 0.03):
            client, _ = await self.start(drop=True)
            remote = self.chat.remotes["laptop"]
            dropped = client.dropped
            original = client.post
            hellos, accepted, results = 0, 0, []

            async def post(path, data):
                nonlocal hellos, accepted
                if path.endswith("hello"):
                    hellos += 1
                if path.endswith("result"):
                    results.append(data["id"])
                    if not hellos:
                        raise ConnectionError("results unavailable until reconnect")
                    answer = await original(path, data)
                    accepted += 1
                    if accepted == 1:
                        raise ConnectionError("result accepted but acknowledgement dropped")
                    return answer
                return await original(path, data)

            client.post = post
            job = asyncio.create_task(remote.run("shell", {
                "command": "printf x >> counter.txt; sleep 0.15; cat counter.txt"}, "main"))
            self.addAsyncCleanup(self.stop_task, job)
            await asyncio.wait_for(dropped.wait(), 2)
            await self.wait_for(lambda: not remote.connected)
            self.assertFalse(job.done())
            value, failed = await asyncio.wait_for(job, 5)
            self.assertFalse(failed, value)
            self.assertEqual(Path(client.machine.work, "main", "counter.txt").read_text(), "x")
            self.assertIs(self.chat.remotes["laptop"], remote)
            await self.wait_for(lambda: any(r["state"] == "acked" for r in client.state.records.values()))
            self.assertGreaterEqual(len(results), 2)
            self.assertEqual(len(set(results)), 1)

    async def test_stop_while_offline_cancels_original_work_and_acks(self):
        await self.serve()
        started, cancelled = asyncio.Event(), asyncio.Event()

        class BlockingMachine(Machine):
            calls = 0

            async def run(self, *args):
                self.calls += 1
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        with patch("eternalchat.web.PING", 0.03):
            machine = BlockingMachine(Path(self.temp.name, "laptop"), name="laptop")
            client, _ = await self.start(machine, drop=True)
            dropped = client.dropped
            remote = self.chat.remotes["laptop"]
            job = asyncio.create_task(remote.run("shell", {"command": "wait"}, "main"))
            await asyncio.wait_for(started.wait(), 2)
            await asyncio.wait_for(dropped.wait(), 2)
            await self.wait_for(lambda: not remote.connected)
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
            self.assertTrue(remote.stops)
            await asyncio.wait_for(cancelled.wait(), 5)
            await self.wait_for(lambda: not remote.stops)
            self.assertEqual(machine.calls, 1)
            self.assertTrue(all(r["state"] == "acked" for r in client.state.records.values()))

    async def test_lost_receipt_ack_does_not_start_until_authorization_confirmed(self):
        await self.serve()
        client, _ = await self.start()
        original = client.post
        dropped = asyncio.Event()
        count = 0

        async def post(path, data):
            nonlocal count
            result = await original(path, data)
            if path.endswith("ack") and data["kind"] == "received":
                count += 1
                if count == 1:
                    dropped.set()
                    raise ConnectionError("receipt acknowledgement lost")
            return result

        client.post = post
        remote = self.chat.remotes["laptop"]
        task = asyncio.create_task(remote.run("shell", {"command": "printf x >> receipt.txt; cat receipt.txt"}, "main"))
        self.addAsyncCleanup(self.stop_task, task)
        await asyncio.wait_for(dropped.wait(), 2)
        self.assertFalse(Path(client.machine.work, "main", "receipt.txt").exists())
        value, failed = await asyncio.wait_for(task, 4)
        self.assertFalse(failed, value)
        self.assertEqual(Path(client.machine.work, "main", "receipt.txt").read_text(), "x")
        self.assertEqual(count, 2)

    async def test_crashed_started_receipt_reports_uncertainty_without_execution(self):
        await self.serve()
        machine = Machine(Path(self.temp.name, "laptop"), name="laptop")
        old = DeviceClient(self.url, machine, Quiet())
        await old.post("/api/devices/hello", {**old.state.credentials, "work": str(machine.work),
            "computer": machine.computer_supported, "screen_id": machine.screen.identity})
        remote = self.chat.remotes["laptop"]
        job = asyncio.create_task(remote.run("shell", {"command": "never replay"}, "main"))
        descriptor = (await remote.outbox.get())["job"]
        old.state.save(descriptor["id"], {"state": "started", "digest": old.state.digest(descriptor), "job": descriptor})
        old.state.close()
        machine.close()
        new_machine = Machine(machine.work, name="laptop")
        with patch.object(new_machine, "run", side_effect=AssertionError("executed twice")) as called:
            client, _ = await self.start(new_machine)
            value, failed = await asyncio.wait_for(job, 3)
            self.assertTrue(failed)
            self.assertIn("side effects are unknown", value)
            called.assert_not_called()

    async def test_identity_is_durable_private_and_rejects_same_name_takeover(self):
        await self.serve()
        client, task = await self.start()
        credentials = dict(client.state.credentials)
        stored = self.chat.engine.store.read_state("devices")
        self.assertNotIn(credentials["session"], json.dumps(stored))
        self.assertEqual(stat.S_IMODE((client.state.root / "identity.json").stat().st_mode), 0o600)
        attacker = DeviceClient(self.url, Machine(Path(self.temp.name, "other"), name="laptop"), Quiet())
        try:
            with self.assertRaises(RemoteError):
                await attacker.post("/api/devices/hello", {**attacker.state.credentials, "work": str(attacker.machine.work),
                    "computer": False, "screen_id": attacker.machine.screen.identity})
            with self.assertRaises(RemoteError) as denied:
                await attacker.client.request("POST", "/api/devices/ack", {"name": "laptop", "id": "0" * 32, "kind": "stop"})
            self.assertEqual(denied.exception.status, 403)
        finally:
            attacker.state.close()
            attacker.machine.close()
        await self.stop_task(task)
        reopened = DeviceState(client.machine.work, client.client.authority, "laptop")
        try:
            self.assertEqual(reopened.credentials, credentials)
            with self.assertRaisesRegex(RuntimeError, "already running"):
                DeviceState(client.machine.work, client.client.authority, "laptop")
        finally:
            reopened.close()

    async def test_server_restart_keeps_enrollment_and_never_replays_old_side_effects(self):
        await self.serve()
        with patch("eternalchat.web.PING", 0.03):
            client, _ = await self.start()
            remote = self.chat.remotes["laptop"]
            call = asyncio.create_task(remote.run("shell", {
                "command": "printf x >> restart.txt; sleep 0.2; cat restart.txt"}, "main"))
            self.addAsyncCleanup(self.stop_task, call)
            await self.wait_for(lambda: any(r["state"] == "started" for r in client.state.records.values()))
            old = self.chat
            port = old.port
            await old.close()
            await old.server.wait_closed()
            value, failed = await asyncio.wait_for(call, 2)
            self.assertTrue(failed)
            self.assertIn("side effects are unknown", value)
            restarted = WebChat(old.args, old.open_engine, Quiet(), old.token)
            await restarted.start(port)
            self.chat = restarted
            self.addAsyncCleanup(restarted.close)
            await self.wait_for(lambda: "laptop" in restarted.remotes)
            await self.wait_for(lambda: any(r["state"] == "orphaned" for r in client.state.records.values()))
            value, failed = await asyncio.wait_for(restarted.remotes["laptop"].run("shell", {
                "command": "printf y >> restart.txt; cat restart.txt"}, "main"), 3)
            self.assertFalse(failed, value)
            self.assertEqual(Path(client.machine.work, "main", "restart.txt").read_text(), "xy")
            self.assertEqual(restarted.engine.store.read_state("devices")["laptop"]["identity"],
                             client.state.credentials["identity"])

    async def test_stop_before_job_and_changed_replay_cannot_execute(self):
        await self.serve()
        client, _ = await self.start()
        identity = "a" * 32
        await client.stop(identity)
        job = {"id": identity, "tool": "shell", "input": {"command": "no"}, "caller": "main", "expires_at": time.time() + 30}
        with patch.object(client.machine, "run", side_effect=AssertionError("stopped job executed")):
            client.receive(job)
            await asyncio.sleep(0)
        identity = "b" * 32
        client.state.save(identity, {"state": "acked", "digest": client.state.digest(job)})
        with self.assertRaisesRegex(RemoteError, "different contents"):
            client.receive({**job, "id": identity, "input": {"command": "changed"}})

    async def test_grace_and_job_expiry_release_waiters_without_retargeting(self):
        remote = Remote("laptop", "/work", identity="a" * 32)
        task = asyncio.create_task(remote.run("write", {"path": "x", "text": "x"}, "main"))
        await remote.outbox.get()
        with patch("eternalchat.devices.RECONNECT_GRACE", 0.02):
            epoch, _ = remote.attach()
            remote.detach(epoch)
            value, failed = await asyncio.wait_for(task, 1)
        self.assertTrue(failed)
        self.assertIn("side effects are unknown", value)
        self.assertTrue(remote.gone)
        remote = Remote("other", "/work", identity="b" * 32)
        with patch("eternalchat.devices.JOB_TTL", 0.02):
            value, failed = await remote.run("read", {"path": "x"}, "main")
        self.assertTrue(failed)
        self.assertIn("expired", value)
        remote.leave()

    async def test_new_stream_generation_preserves_screen_lease(self):
        await self.serve()
        devices = self.chat.engine.machine
        identity = "c" * 32
        remote = devices.join("screen", "/work", True, "physical-screen", identity)
        lease = remote.reserve_screen("Scout")
        epoch, _ = remote.attach()
        remote.detach(epoch)
        same = devices.join("screen", "/work", True, "physical-screen", identity)
        current, _ = same.attach()
        remote.detach(epoch)  # an older socket's finally must not expire the new one
        self.assertTrue(remote.connected)
        self.assertIsNone(remote.expiry)
        self.assertIs(remote, same)
        with self.assertRaises(ValueError):
            remote.reserve_screen("Other")
        lease.release()
        remote.leave()
