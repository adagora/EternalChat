"""The computers the device tools reach: the one EternalChat runs on (a
Machine), and any that joined with `eternalchat device`, each by its name.

A joined computer is a queue of jobs here: web.py streams them to it and
hands back its answers; the call waits for its answer, and a stop sends it a
stop for that job. Enrolled computers retain jobs through brief disconnections;
explicit leave or bounded expiry ends waiters with uncertain-side-effect status.
Legacy unenrolled connections still fail immediately on disconnect.
The engine sees one machine: `run` takes the tool's
optional `device` field, and `describe` lists the computers for the turn.
"""
import asyncio
import time
import uuid

from .prompt import DEVICE_NAMES as TOOL_NAMES
from .screen import Screen

RECONNECT_GRACE = 60  # seconds a temporary link loss may last
JOB_TTL = 600  # jobs/results/cancellation tombstones have a bounded lifetime


class Remote:
    """A joined computer: its name, its work folder, its jobs waiting and running."""

    def __init__(self, name, work, computer=False, screen=None, identity=None):
        self.name, self.work = name, work
        self.computer_supported = computer
        self.outbox = asyncio.Queue()  # what to send it: {"job": ...} or {"stop": id}
        self.pending = {}  # job id -> future of (text, failed)
        self.gone = False
        self.screen = screen or Screen("remote:" + uuid.uuid4().hex)
        self.identity = identity
        self.jobs = {}
        self.stops = {}
        self.completed = {}
        self.epoch = 0
        self.connected = True
        self.expiry = None

    def attach(self):
        """A new authenticated stream supersedes its previous generation."""
        if self.expiry:
            self.expiry.cancel()
            self.expiry = None
        self.connected = True
        self.epoch += 1
        self.outbox = asyncio.Queue()
        now = time.time()
        self.stops = {job: expiry for job, expiry in self.stops.items() if expiry > now}
        self.completed = {job: expiry for job, expiry in self.completed.items() if expiry > now}
        for job in self.stops:
            self.outbox.put_nowait({"stop": job})
        for job in self.jobs.values():
            self.outbox.put_nowait({"job": job})
        return self.epoch, self.outbox

    def detach(self, epoch):
        if epoch != self.epoch or self.gone:
            return
        self.connected = False
        if self.expiry:
            self.expiry.cancel()
        self.expiry = asyncio.get_running_loop().call_later(RECONNECT_GRACE, self.leave)

    def acknowledge(self, job, kind):
        if kind == "stop":
            self.stops.pop(job, None)
            return
        if kind != "received" or job not in self.jobs or self.jobs[job]["expires_at"] <= time.time():
            raise ValueError("Device job is no longer authorized; do not execute it")

    def prune_receipts(self):
        now = time.time()
        for records in (self.completed, self.stops):
            for job, expiry in list(records.items()):
                if expiry <= now:
                    records.pop(job, None)
            while len(records) > 4096:
                records.pop(next(iter(records)))

    def reserve_screen(self, caller, device=None, identity=None):
        if self.gone:
            raise ValueError(f"The computer {self.name} left; reconnect it before continuing")
        if not self.computer_supported:
            raise ValueError(f"The computer {self.name} does not offer Mac screen control")
        if identity is not None and identity != self.screen.identity:
            raise ValueError("The screen device identity changed; start a new task for the intended computer")
        return self.screen.reserve(caller, self.name, self.run)

    async def run(self, tool, args, caller):
        if self.gone:
            return f"The computer {self.name} left; this task cannot act on a replacement connection.", True
        if tool == "computer":
            self.screen.check(caller)
        if self.identity:
            self.prune_receipts()
        job = uuid.uuid4().hex
        if len(self.pending) >= 64:
            return "Device has too many outstanding jobs; wait before submitting more.", True
        future = asyncio.get_running_loop().create_future()
        self.pending[job] = future
        descriptor = {"id": job, "tool": tool, "input": args, "caller": caller}
        if self.identity:
            descriptor["expires_at"] = time.time() + JOB_TTL
            self.jobs[job] = descriptor
        self.outbox.put_nowait({"job": descriptor})
        try:
            if self.identity:
                try:
                    return await asyncio.wait_for(asyncio.shield(future), JOB_TTL)
                except TimeoutError:
                    self.stops[job] = time.time() + JOB_TTL
                    self.outbox.put_nowait({"stop": job})
                    return "Device job expired; its side effects are unknown. Inspect before retrying.", True
            return await future
        except asyncio.CancelledError:
            if not self.gone:
                if self.identity:
                    self.stops[job] = time.time() + JOB_TTL
                self.outbox.put_nowait({"stop": job})
            raise
        finally:
            self.pending.pop(job, None)
            self.jobs.pop(job, None)
            if not future.done():
                future.cancel()

    def answer(self, job, text, failed):
        future = self.pending.get(job)
        if future is not None and not future.done():
            future.set_result((text, bool(failed)))
            if self.identity:
                self.prune_receipts()
                self.completed[job] = time.time() + JOB_TTL

    def leave(self):
        self.gone = True
        self.connected = False
        if self.expiry:
            self.expiry.cancel()
            self.expiry = None
        for future in self.pending.values():
            if not future.done():
                detail = " Its side effects are unknown; inspect before retrying." if self.identity else ""
                future.set_result((f"The computer {self.name} left before this finished." + detail, True))


class Devices:
    """One machine for the engine: this computer, and the joined ones by name."""

    def __init__(self, local):
        self.local = local
        self.remote = {}
        self._screens = {local.screen.identity: local.screen}

    def restore_remotes(self, remotes):
        self.remote = remotes
        self._screens.update((r.screen.identity, r.screen) for r in remotes.values())

    def reserve_screen(self, caller, device=None, identity=None):
        if device in (None, "", self.local.name):
            return self.local.reserve_screen(caller, identity=identity)
        if not isinstance(device, str) or device not in self.remote:
            raise ValueError(f"No computer named {device} is connected")
        return self.remote[device].reserve_screen(caller, identity=identity)

    @property
    def name(self):
        return self.local.name

    @property
    def work(self):
        return self.local.work

    def folder(self, caller):
        return self.local.folder(caller)

    def join(self, name, work, computer=False, screen_id=None, identity=None):
        """A computer joins (again): one already joined by that name leaves."""
        if not isinstance(name, str) or not name.strip() or len(name) > 64:
            raise ValueError("A computer needs a name of at most 64 characters")
        name = name.strip()
        if type(computer) is not bool:
            raise ValueError("computer capability must be a boolean")
        if screen_id is not None and (not isinstance(screen_id, str) or not screen_id.strip() or len(screen_id) > 256):
            raise ValueError("screen_id must be a nonempty device identity of at most 256 characters")
        if name == self.local.name:
            raise ValueError(f"{name} is this computer's own name; join with --name another")
        old = self.remote.get(name)
        if old is not None and old.identity and identity != old.identity:
            raise ValueError("This name belongs to another enrolled device")
        if old is not None and identity and old.identity == identity and not old.gone:
            if old.work != str(work) or old.computer_supported != computer or old.screen.identity != screen_id:
                raise ValueError("Device capabilities or work folder changed; reconnect the original device")
            return old
        self.remote.pop(name, None)
        if old is not None:
            old.leave()
        screen_identity = screen_id or "remote:" + uuid.uuid4().hex
        screen = self._screens.setdefault(screen_identity, Screen(screen_identity))
        self.remote[name] = Remote(name, str(work), computer, screen, identity)
        return self.remote[name]

    def leave(self, remote):
        if self.remote.get(remote.name) is remote:
            del self.remote[remote.name]
        remote.leave()

    def listing(self):
        return [{"name": self.local.name, "work": str(self.local.work), "here": True,
                 "computer": self.local.computer_supported}] + [
            {"name": r.name, "work": r.work, "here": False, "running": len(r.pending),
             "computer": r.computer_supported, "connected": r.connected} for r in self.remote.values() if not r.gone]

    def describe(self, caller):
        """The computers, for the text after the view."""
        rows = [f"{self.local.name} (this one; your working folder: {self.local.folder(caller)})"]
        if self.local.computer_supported:
            rows[0] += " [screen control]"
        rows += [f"{r.name} (your working folder: {r.work}/{caller})"
                 + (" [screen control]" if r.computer_supported else "")
                 + (" [reconnecting; existing jobs wait for the device]" if not r.connected else "")
                 for r in self.remote.values() if not r.gone]
        return "\nComputers for shell, read, write, edit and computer (device; default the first): " + "; ".join(rows)

    async def run(self, tool, args, caller):
        if tool not in TOOL_NAMES:
            raise ValueError(f"Unknown tool: {tool}")
        if not isinstance(args, dict):
            raise ValueError("Tool input must be an object")
        args = dict(args)
        device = args.pop("device", None)
        if device is not None and not isinstance(device, str):
            raise ValueError("device must name a computer")
        if device in (None, "", self.local.name):
            return await self.local.run(tool, args, caller)
        remote = self.remote.get(device)
        if remote is None:
            names = ", ".join([self.local.name, *self.remote]) or self.local.name
            return f"No computer named {device} is connected; these are: {names}.", True
        if tool == "computer" and not remote.computer_supported:
            return f"The computer {device} does not offer Mac screen control.", True
        return await remote.run(tool, args, caller)

    def close(self):
        self.local.close()
        for remote in list(self.remote.values()):
            self.leave(remote)
