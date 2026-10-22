"""Private local enrollment and execution receipts for a device connection."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time
import uuid

from .storage import atomic_json

JOURNAL_BYTES = 128_000_000
RECEIPT_TTL = 1200


class DeviceState:
    def __init__(self, work, server, name):
        key = hashlib.sha256(f"{server}\n{name}".encode()).hexdigest()
        self.root = Path(work) / ".eternalchat-devices" / key
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = os.open(self.root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.lock)
            self.lock = None
            raise RuntimeError("This device connection is already running for this server/name/work folder") from None
        try:
            path = self.root / "identity.json"
            if path.exists():
                self.credentials = json.loads(path.read_bytes())
            else:
                self.credentials = {"identity": uuid.uuid4().hex, "session": secrets.token_urlsafe(32)}
                atomic_json(path, self.credentials)
                path.chmod(0o600)
            if (not isinstance(self.credentials, dict)
                    or not re.fullmatch(r"[0-9a-f]{32}", str(self.credentials.get("identity", "")))
                    or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", str(self.credentials.get("session", "")))):
                raise RuntimeError("Invalid saved device enrollment; restore its private identity file")
            self.records = {}
            for path in self.root.glob("job-*.json"):
                record = json.loads(path.read_bytes())
                if record.get("state") in ("acked", "orphaned") and record.get("touched", 0) + RECEIPT_TTL < time.time():
                    path.unlink()
                    continue
                self.records[path.stem[4:]] = record
            self.prune()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def digest(job):
        return hashlib.sha256(json.dumps(job, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def save(self, job, record):
        if not isinstance(job, str) or not re.fullmatch(r"[0-9a-f]{32}", job):
            raise ValueError("Invalid device job identity")
        record = {**record, "touched": time.time()}
        path = self.root / f"job-{job}.json"
        encoded = json.dumps(record).encode()
        total = sum(p.stat().st_size for p in self.root.glob("job-*.json") if p != path)
        if total + len(encoded) > JOURNAL_BYTES or job not in self.records and len(self.records) >= 4096:
            raise RuntimeError("Device receipt journal is full; reconnect to acknowledge pending results")
        atomic_json(path, record)
        path.chmod(0o600)
        self.records[job] = record

    def acked(self, job):
        previous = self.records[job]
        self.save(job, {"state": "acked", "digest": previous.get("digest")})
        self.prune()

    def prune(self):
        receipts = sorted((r.get("touched", 0), job) for job, r in self.records.items() if r["state"] in ("acked", "orphaned"))
        for _, job in receipts[:-1024]:
            (self.root / f"job-{job}.json").unlink()
            del self.records[job]

    def close(self):
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None
