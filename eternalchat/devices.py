"""The computers the device tools reach: the one EternalChat runs on (a
Machine), and any that joined with `eternalchat device`, each by its name.

A joined computer is a queue of jobs here: web.py streams them to it and
hands back its answers; the call waits for its answer, and a stop sends it a
stop for that job. A computer that leaves fails what it was running, so no
call waits for good. The engine sees one machine: `run` takes the tool's
optional `device` field, and `describe` lists the computers for the turn.
"""
import asyncio
import uuid

from .prompt import DEVICE_NAMES as TOOL_NAMES


class Remote:
    """A joined computer: its name, its work folder, its jobs waiting and running."""

    def __init__(self, name, work):
        self.name, self.work = name, work
        self.outbox = asyncio.Queue()  # what to send it: {"job": ...} or {"stop": id}
        self.pending = {}  # job id -> future of (text, failed)
        self.gone = False

    async def run(self, tool, args, caller):
        job = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[job] = future
        self.outbox.put_nowait({"job": {"id": job, "tool": tool, "input": args, "caller": caller}})
        try:
            return await future
        except asyncio.CancelledError:
            if not self.gone:
                self.outbox.put_nowait({"stop": job})
            raise
        finally:
            self.pending.pop(job, None)

    def answer(self, job, text, failed):
        future = self.pending.get(job)
        if future is not None and not future.done():
            future.set_result((text, bool(failed)))

    def leave(self):
        self.gone = True
        for future in self.pending.values():
            if not future.done():
                future.set_result((f"The computer {self.name} left before this finished.", True))


class Devices:
    """One machine for the engine: this computer, and the joined ones by name."""

    def __init__(self, local):
        self.local = local
        self.remote = {}

    @property
    def name(self):
        return self.local.name

    @property
    def work(self):
        return self.local.work

    def folder(self, caller):
        return self.local.folder(caller)

    def join(self, name, work):
        """A computer joins (again): one already joined by that name leaves."""
        if not isinstance(name, str) or not name.strip() or len(name) > 64:
            raise ValueError("A computer needs a name of at most 64 characters")
        name = name.strip()
        if name == self.local.name:
            raise ValueError(f"{name} is this computer's own name; join with --name another")
        old = self.remote.pop(name, None)
        if old is not None:
            old.leave()
        self.remote[name] = Remote(name, str(work))
        return self.remote[name]

    def leave(self, remote):
        if self.remote.get(remote.name) is remote:
            del self.remote[remote.name]
        remote.leave()

    def listing(self):
        return [{"name": self.local.name, "work": str(self.local.work), "here": True}] + [
            {"name": r.name, "work": r.work, "here": False, "running": len(r.pending)} for r in self.remote.values()]

    def describe(self, caller):
        """The computers, for the text after the view."""
        rows = [f"{self.local.name} (this one; your working folder: {self.local.folder(caller)})"]
        rows += [f"{r.name} (your working folder: {r.work}/{caller})" for r in self.remote.values()]
        return "\nComputers for shell, read, write and edit (device; default the first): " + "; ".join(rows)

    async def run(self, tool, args, caller):
        if tool not in TOOL_NAMES:
            raise ValueError(f"Unknown tool: {tool}")
        if not isinstance(args, dict):
            raise ValueError("Tool input must be an object")
        args = dict(args)
        device = args.pop("device", None)
        if device in (None, "", self.local.name):
            return await self.local.run(tool, args, caller)
        remote = self.remote.get(device)
        if remote is None:
            names = ", ".join([self.local.name, *self.remote]) or self.local.name
            return f"No computer named {device} is connected; these are: {names}.", True
        return await remote.run(tool, args, caller)

    def close(self):
        self.local.close()
        for remote in list(self.remote.values()):
            self.leave(remote)
