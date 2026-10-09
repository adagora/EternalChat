"""Append-only records. In-memory indices contain offsets, not chat contents."""
import base64
import fcntl
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .config import byte_size, clip_output, split_text

KINDS = {"user", "unii", "tool", "echo", "work", "note"}


class StoreError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("wb") as f:
        f.write(encode(value) + b"\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)
    sync_dir(path.parent)


class Store:
    def __init__(self, root, message_bytes=30_000):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.message_bytes = message_bytes
        self._lock = (self.root / ".lock").open("a+b")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise StoreError(f"Chat is already open in another process: {self.root}") from None
        self.messages = {}
        self.nodes = {}
        self.events = {}
        self._verified_assets = set()
        self._files = {}
        self._poisoned = False
        try:
            for name in ("main", "tree", "assets", "agents"):
                (self.root / name).mkdir(exist_ok=True)
            self._load("main", self.messages)
            self._load("tree", self.nodes)
        except BaseException:
            self.close()
            raise

    def _load(self, kind, index):
        for path in sorted((self.root / kind).glob("*.jsonl")):
            with path.open("rb") as f:
                while True:
                    offset = f.tell()
                    line = f.readline()
                    if not line:
                        break
                    try:
                        if not line.endswith(b"\n"):
                            raise ValueError("incomplete final record")
                        record = json.loads(line)
                        if record["size"] != byte_size(record["text"]):
                            raise ValueError("incorrect UTF-8 size")
                        if kind == "main":
                            key = record["i"]
                            if type(key) is not int or key != len(index):
                                raise ValueError("non-contiguous message id")
                            if record["kind"] not in KINDS:
                                raise ValueError("unknown message kind")
                            datetime.fromisoformat(record["date"])
                            for ref in record.get("attachments", []):
                                if ref["sha256"] not in self._verified_assets:
                                    self.image_block(ref)
                                    self._verified_assets.add(ref["sha256"])
                            if "event_id" in record:
                                prior = self.events.setdefault(record["event_id"], [])
                                if record["event_part"] != len(prior) or record["event_parts"] <= len(prior):
                                    raise ValueError("invalid durable event sequence")
                                prior.append(key)
                        else:
                            key = (record["l"], record["i"])
                            l, i = key
                            if any(type(x) is not int or x < 0 for x in key):
                                raise ValueError("invalid node address")
                            if (i + 1) * (1 << l) > len(self.messages):
                                raise ValueError("node covers absent messages")
                            if l and ((l-1, 2*i) not in index or (l-1, 2*i+1) not in index):
                                raise ValueError("tree parent has a missing child")
                        if key in index:
                            raise ValueError("duplicate record")
                    except (ValueError, KeyError, TypeError, OSError, StoreError) as e:
                        raise StoreError(f"Corrupt {path.name} at byte {offset}: {e}. "
                                         "Restore a verified backup; original bytes were not changed.") from e
                    index[key] = (path, offset)

    def _append(self, kind, record):
        if self._poisoned or self._lock.closed:
            raise StoreError("Storage is closed or had an uncertain write. Reopen and validate before writing again.")
        day = record.get("date", utc_now())[:10]
        # If the clock moves backwards, keep appending to the latest shard so
        # lexicographic startup order still preserves permanent message IDs.
        if not hasattr(self, "_latest"):
            self._latest = {k: max((p.stem for p in (self.root / k).glob("*.jsonl")),
                                   default="") for k in ("main", "tree")}
        day = max(day, self._latest[kind])
        self._latest[kind] = day
        path = self.root / kind / f"{day}.jsonl"
        try:
            if path not in self._files:
                existed = path.exists()
                self._files[path] = path.open("ab")
                if not existed:
                    sync_dir(path.parent)
            f = self._files[path]
            offset = f.tell()
            f.write(encode(record) + b"\n")
            f.flush()
            os.fsync(f.fileno())
            return path, offset
        except OSError as e:
            self._poisoned = True
            raise StoreError("Uncertain log write; further writes are disabled. Reopen and validate the chat.") from e

    @staticmethod
    def _read(location):
        path, offset = location
        with path.open("rb") as f:
            f.seek(offset)
            return json.loads(f.readline())

    def message(self, i):
        if type(i) is not int or i not in self.messages:
            raise ValueError(f"Unknown message id: {i}")
        return self._read(self.messages[i])

    def node(self, key):
        if key not in self.nodes:
            raise ValueError(f"Summary is not built: {key}")
        return self._read(self.nodes[key])

    def append(self, kind, text, *, date=None, attachments=None, event_id=None):
        if kind not in KINDS:
            raise ValueError(f"Unsupported message kind: {kind}")
        if kind == "echo":
            text = clip_output(text)
        ids = []
        date = date or utc_now()
        parts = list(split_text(text, self.message_bytes))
        existing = self.events.get(event_id, []) if event_id else []
        for part_index, part in enumerate(parts):
            if part_index < len(existing):
                previous = self.message(existing[part_index])
                if previous["text"] != part or previous["event_parts"] != len(parts):
                    raise StoreError("Durable event was retried with different contents")
                ids.append(previous["i"])
                continue
            i = len(self.messages)
            record = dict(i=i, kind=kind, text=part, size=byte_size(part), date=date)
            if attachments and not ids:
                record["attachments"] = attachments
            if event_id:
                record.update(event_id=event_id, event_part=part_index, event_parts=len(parts))
            self.messages[i] = self._append("main", record)
            if event_id:
                self.events.setdefault(event_id, []).append(i)
            ids.append(i)
        return ids

    def add_node(self, key, text):
        if key in self.nodes:
            raise StoreError(f"Node already exists: {key}")
        l, i = key
        if l < 0 or i < 0 or (i + 1) * (1 << l) > len(self.messages):
            raise ValueError("Node is outside the chat")
        if l and ((l - 1, 2 * i) not in self.nodes or (l - 1, 2 * i + 1) not in self.nodes):
            raise ValueError("Both children must exist")
        self.nodes[key] = self._append("tree", dict(l=l, i=i, text=text, size=byte_size(text)))

    def add_image(self, filename):
        data = Path(filename).read_bytes()
        if len(data) > 5_000_000:
            raise ValueError("Image must be at most 5 MB")
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif data.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif data.startswith((b"GIF87a", b"GIF89a")):
            mime = "image/gif"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            mime = "image/webp"
        else:
            raise ValueError("Use a PNG, JPEG, GIF or WebP image")
        digest = hashlib.sha256(data).hexdigest()
        path = self.root / "assets" / digest
        if not path.exists():
            with path.open("xb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            sync_dir(path.parent)
        return {"sha256": digest, "media_type": mime}

    def image_block(self, ref):
        if not re.fullmatch(r"[0-9a-f]{64}", ref["sha256"]):
            raise StoreError("Invalid attachment reference")
        data = (self.root / "assets" / ref["sha256"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != ref["sha256"]:
            raise StoreError("Attachment checksum mismatch")
        return {"type": "image", "source": {"type": "base64",
                "media_type": ref["media_type"], "data": base64.b64encode(data).decode()}}

    def close(self):
        try:
            for f in self._files.values():
                f.close()
        finally:
            self._files.clear()
            if not self._lock.closed:
                fcntl.flock(self._lock, fcntl.LOCK_UN)
                self._lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
