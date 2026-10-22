"""Reconnect-safe device execution: durable receipt before any side effect."""
import asyncio
import time
from urllib.parse import urlencode

from .device_state import DeviceState
from .remote import Client, RemoteError


class DeviceClient:
    def __init__(self, url, machine, out):
        self.client, self.machine, self.out = Client(url), machine, out
        self.name = machine.name.strip()
        self.state = DeviceState(machine.work, self.client.authority, self.name)
        self.client.extra_headers = {"X-EternalChat-Device": self.state.credentials["identity"],
            "X-EternalChat-Device-Token": self.state.credentials["session"]}
        self.running = {}
        self.failure = asyncio.get_running_loop().create_future()
        # An earlier process might have acted before crashing. Never replay it.
        try:
            for job, record in list(self.state.records.items()):
                if record["state"] == "started":
                    self.result(job, record, "Device process stopped during execution; side effects are unknown. Inspect before retrying.", True)
        except BaseException:
            self.state.close()
            raise

    async def post(self, path, data):
        return await self.client.request("POST", path, {"name": self.name, **data})

    def result(self, job, record, value, failed):
        self.state.save(job, {"state": "result", "digest": record.get("digest"),
                             "value": value, "failed": failed})

    async def execute(self, job):
        identity = job["id"]
        record = self.state.records[identity]
        try:
            # Receipt ACK is idempotent and has no side effects. Never start a
            # command whose server authorization expired during a disconnect.
            while True:
                if job["expires_at"] <= time.time():
                    self.result(identity, record, "Device job expired before execution.", True)
                    return
                try:
                    before = asyncio.get_running_loop().time()
                    reply = await self.post("/api/devices/ack", {"id": identity, "kind": "received"})
                    # Deduct the whole round trip, conservatively, rather than
                    # trusting matching wall clocks on the two computers.
                    deadline = before + reply["expires_in"]
                    break
                except (OSError, TimeoutError, asyncio.IncompleteReadError):
                    await asyncio.sleep(1)
            self.state.save(identity, {**record, "state": "started"})
            remaining = min(job["expires_at"] - time.time(), deadline - asyncio.get_running_loop().time())
            if remaining <= 0:
                self.result(identity, record, "Device job expired before execution.", True)
                return
            shown = job["input"].get("command") or job["input"].get("path") or job["tool"]
            self.out.line(f"↳ {job['tool']}: {shown}"[:300], err=True)
            async with asyncio.timeout(remaining):
                value, failed = await self.machine.run(job["tool"], job["input"], job["caller"])
            self.result(identity, record, value, failed)
        except asyncio.CancelledError:
            self.result(identity, record, "Device job cancelled; any completed side effects remain.", True)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
            self.result(identity, record, f"Device job failed: {type(error).__name__}; inspect side effects before retrying.", True)
        finally:
            self.running.pop(identity, None)

    def receive(self, job):
        identity = job["id"]
        digest = self.state.digest(job)
        previous = self.state.records.get(identity)
        if previous:
            if previous.get("digest") not in (None, digest):
                raise RemoteError("Server replayed a device job ID with different contents")
            if previous["state"] != "received" or identity in self.running:
                return
        else:
            self.state.save(identity, {"state": "received", "digest": digest, "job": job})
        task = asyncio.create_task(self.execute(job))
        self.running[identity] = task
        task.add_done_callback(self.finished)

    def finished(self, task):
        if not task.cancelled() and (error := task.exception()) is not None and not self.failure.done():
            self.failure.set_exception(error)

    async def stop(self, identity):
        task = self.running.get(identity)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        previous = self.state.records.get(identity, {})
        self.state.save(identity, {"state": "stopped", "digest": previous.get("digest")})
        await self.post("/api/devices/ack", {"id": identity, "kind": "stop"})
        self.state.acked(identity)

    async def deliver(self):
        while True:
            for identity, record in list(self.state.records.items()):
                if record["state"] == "received" and record["job"]["expires_at"] <= time.time():
                    self.result(identity, record, "Device job expired before execution.", True)
                    record = self.state.records[identity]
                if record["state"] not in ("result", "stopped"):
                    continue
                try:
                    if record["state"] == "stopped":
                        await self.post("/api/devices/ack", {"id": identity, "kind": "stop"})
                    else:
                        value = record["value"]
                        await self.post("/api/devices/result", {"id": identity, "failed": record["failed"],
                            **({"text": value} if isinstance(value, str) else {"content": value})})
                    self.state.acked(identity)
                except RemoteError as error:
                    if error.status in (404, 409):
                        # The old server job no longer waits. Keep a receipt so
                        # a late frame cannot execute its side effects again.
                        self.state.save(identity, {**record, "state": "orphaned"})
                        self.state.prune()
                        self.out.line(f"Job {identity} is no longer awaited by the server; its receipt is in "
                                      f"{self.state.root}. Inspect side effects before retrying.", err=True)
                    else:
                        raise
                except (OSError, TimeoutError, asyncio.IncompleteReadError):
                    break
            await asyncio.sleep(0.2)

    async def listen(self):
        while True:
            try:
                await self.post("/api/devices/hello", {**self.state.credentials,
                    "work": str(self.machine.work), "computer": self.machine.computer_supported,
                    "screen_id": self.machine.screen.identity})
                self.out.line(f"{self.name} joined {self.client.authority}. Device jobs may run here. Ctrl-C leaves.")
                async for item in self.client.events("/api/devices/jobs?" + urlencode({"name": self.name})):
                    if "job" in item:
                        self.receive(item["job"])
                    elif "stop" in item:
                        await self.stop(item["stop"])
            except (OSError, TimeoutError, asyncio.IncompleteReadError):
                self.out.line("Device link lost; reconnecting without restarting running jobs.", err=True)
                await asyncio.sleep(1)

    async def run(self):
        tasks = [asyncio.create_task(self.listen()), asyncio.create_task(self.deliver()), self.failure]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        finally:
            for task in [*tasks, *self.running.values()]:
                task.cancel()
            await asyncio.gather(*tasks, *self.running.values(), return_exceptions=True)
            try:
                async with asyncio.timeout(2):
                    await self.post("/api/devices/leave", {})
            except (OSError, RuntimeError, TimeoutError):
                pass
            self.machine.close()
            self.state.close()
