"""Bounded browser upload previews and cancellable imports on the engine loop."""
import asyncio
import secrets
import time

from . import history

MAX_UPLOAD = 16_000_000
MAX_BATCH = 64_000_000
MAX_MESSAGES = 50_000
TTL = 1800


class Imports:
    def __init__(self, engine):
        self.engine = engine
        self.batches = {}
        self.parse_lock = asyncio.Lock()

    def create(self, since="", tools=False):
        now = time.monotonic()
        for identity, batch in list(self.batches.items()):
            if (batch["task"] is None or batch["task"].done()) and (
                    batch["state"] in ("completed", "cancelled", "error") or
                    now - batch["touched"] > TTL and batch["state"] not in ("running", "parsing")):
                del self.batches[identity]
        if len(self.batches) >= 4:
            raise ValueError("Finish or cancel an existing upload before starting another")
        identity = secrets.token_urlsafe(24)
        self.batches[identity] = {"id": identity, "state": "ready", "sessions": {}, "bytes": 0,
            "files": 0, "expanded": 0, "messages": 0, "processed": 0, "imported": 0,
            "since": since, "tools": tools, "touched": now, "task": None, "error": None}
        return self.status(identity)

    def get(self, identity):
        if not isinstance(identity, str) or identity not in self.batches:
            raise ValueError("Upload expired or server restarted; select the files again")
        batch = self.batches[identity]
        batch["touched"] = time.monotonic()
        return batch

    def status(self, identity):
        batch = self.get(identity)
        return {key: batch[key] for key in ("id", "state", "files", "bytes", "messages", "processed",
                                            "imported", "error")} | {
            "sessions": [{"key": s.key, "app": s.app, "where": s.where, "start": s.start,
                          "messages": len(s.messages), "calls": s.calls} for s in batch["sessions"].values()]}

    async def upload(self, identity, name, data):
        batch = self.get(identity)
        if batch["state"] != "ready":
            raise ValueError("This upload is busy or has finished")
        if len(data) > MAX_UPLOAD or batch["bytes"] + len(data) > MAX_BATCH or batch["files"] >= 500:
            raise ValueError("Upload limit: 16 MB per file, 64 MB and 500 files per selection")
        batch["state"] = "parsing"
        try:
            async with self.parse_lock:
                sessions = await asyncio.to_thread(history.read_upload, name, data, tools=batch["tools"])
            if batch["state"] == "cancelled":
                return self.status(identity)
            candidate = {k: history.Session(s.app, s.where, s.start, list(s.messages))
                         for k, s in batch["sessions"].items()}
            done = self.engine().imported()
            for session in sessions:
                session.messages = [m for m in session.messages if m[2] >= batch["since"]
                                    and m[2] > done.get(session.key, "")]
                if session.messages:
                    # Same session in duplicate files is one preview, never two imports.
                    previous = candidate.get(session.key)
                    if previous:
                        from collections import Counter
                        seen = Counter(previous.messages)
                        for message in session.messages:
                            if seen[message]:
                                seen[message] -= 1
                            else:
                                previous.messages.append(message)
                        previous.messages.sort(key=lambda m: m[2])
                    else:
                        candidate[session.key] = session
            count = sum(len(s.messages) for s in candidate.values())
            expanded = sum(len(t.encode()) for s in candidate.values() for _, t, _ in s.messages)
            if count > MAX_MESSAGES or expanded > MAX_BATCH:
                raise ValueError("Selection expands beyond 50,000 messages or 64 MB; select fewer files")
            batch.update(sessions=candidate, messages=count, expanded=expanded,
                         files=batch["files"] + 1, bytes=batch["bytes"] + len(data))
            batch["state"] = "ready"
            return self.status(identity)
        finally:
            if batch["state"] == "parsing":
                batch["state"] = "ready"

    def start(self, identity, keys):
        batch = self.get(identity)
        if batch["state"] != "ready":
            raise ValueError("This selection is busy or has finished")
        if not isinstance(keys, list) or not keys or not all(isinstance(k, str) and k in batch["sessions"] for k in keys):
            raise ValueError("Choose sessions from this upload's preview")
        if any(b["state"] == "running" for b in self.batches.values()):
            raise ValueError("Wait for the current import or cancel it")
        sessions = [batch["sessions"][key] for key in dict.fromkeys(keys)]
        batch["state"] = "running"
        batch["messages"] = sum(len(s.messages) + 1 for s in sessions)
        batch["task"] = asyncio.create_task(self._run(batch, sessions))
        return self.status(identity)

    async def _run(self, batch, sessions):
        engine = self.engine()
        pending = []
        try:
            for ids in engine.import_steps(sessions):
                pending.extend(ids)
                batch["processed"] += 1
                batch["imported"] += len(ids)
                if batch["processed"] % 20 == 0:
                    engine.import_progress(pending)
                    pending = []
                    await asyncio.sleep(0)
            batch["state"] = "completed"
        except asyncio.CancelledError:
            batch["state"] = "cancelled"
        except Exception as error:
            batch["state"], batch["error"] = "error", str(error)
        finally:
            engine.import_progress(pending)
            # Completed data is on disk; previews need not retain all its text.
            batch["sessions"] = {}

    def cancel(self, identity):
        batch = self.get(identity)
        if batch["task"] is not None and not batch["task"].done():
            batch["task"].cancel()
        batch["state"] = "cancelled"
        batch["sessions"] = {}
        return self.status(identity)

    async def close(self):
        tasks = [b["task"] for b in self.batches.values() if b["task"] is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @property
    def running(self):
        return any(b["task"] is not None and not b["task"].done() for b in self.batches.values())
