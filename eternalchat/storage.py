"""Filesystem adapter: append-only logs, saved views, image assets and agent folders.

Only this module knows file names, record encoding, locking and fsync order.
In-memory indices hold file offsets and sizes, never chat contents.
"""
import fcntl
import hashlib
import json
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from .config import MAX_IMAGE_BYTES, byte_size, clip_output, split_text, utc_now
from .errors import StoreError
from .tree import children

KINDS = frozenset({"user", "unii", "tool", "echo", "work", "note"})
IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
STATE_FILES = {"view": "view.json", "compaction-view": "compaction-view.json", "goal": "goal.json",
               "view-state": "view-state.json"}
AGENT_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")  # new names: a numeric zoom target is a message
SAVED_AGENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")  # also readable: names saved by 0.1.0
SHA256 = re.compile(r"[0-9a-f]{64}")
RESTORE = "Restore a verified backup; original bytes were not changed."


class Location(NamedTuple):
    path: Path
    offset: int
    size: int  # UTF-8 bytes of the record's text


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_bytes(path, data):
    """Replace `path` with `data`: temp file, fsync, rename, directory fsync."""
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)
    sync_dir(path.parent)


def atomic_json(path, value):
    atomic_bytes(path, encode(value) + b"\n")


def image_type(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("Use a PNG, JPEG, GIF or WebP image")


def image_identity(ref):
    if (not isinstance(ref, dict) or not isinstance(ref.get("sha256"), str)
            or not SHA256.fullmatch(ref["sha256"]) or not isinstance(ref.get("media_type"), str)
            or ref["media_type"] not in IMAGE_TYPES):
        raise StoreError("Invalid attachment reference")
    return ref["sha256"], ref["media_type"]


def check_agent_name(name, *, new=False):
    pattern = AGENT_NAME if new else SAVED_AGENT_NAME
    if not isinstance(name, str) or not pattern.fullmatch(name):
        raise ValueError("Agent name must start with a letter, followed by up to 63 "
                         "letters, digits, underscores or hyphens")
    return name


def read_image_file(filename):
    """At most MAX_IMAGE_BYTES + 1 bytes of a regular file. Opening never blocks,
    so a FIFO or device path cannot stall the event loop."""
    fd = os.open(filename, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("Attach a regular image file")
        with os.fdopen(fd, "rb") as f:
            fd = None
            return f.read(MAX_IMAGE_BYTES + 1)
    finally:
        if fd is not None:
            os.close(fd)


class Store:
    """One chat folder. A writer holds an exclusive lock; readers share one."""

    def __init__(self, root, message_bytes=30_000, *, readonly=False):
        if type(message_bytes) is not int or message_bytes < 4:
            raise ValueError("message_bytes must fit a UTF-8 code point")
        self.root = Path(root)
        self.readonly = readonly
        self.message_bytes = message_bytes
        self._messages, self._nodes, self._events = {}, {}, {}
        self._files = {}
        self._verified = set()  # (digest, media type) references verified at startup
        self._poisoned = False
        self._lock = self._acquire_lock()
        try:
            if not readonly:
                for name in ("main", "tree", "assets", "agents"):
                    if not (self.root / name).is_dir():
                        (self.root / name).mkdir()
                        sync_dir(self.root)
            self._load("main", self._messages, self._check_message)
            self._load("tree", self._nodes, self._check_node)
            self._latest = {kind: max((p.stem for p in (self.root / kind).glob("*.jsonl")), default="")
                            for kind in ("main", "tree")}
        except BaseException:
            self.close()
            raise

    def _acquire_lock(self):
        """Lock the folder itself and its .lock file: shared for readers,
        exclusive for the one writer. The folder lock covers a restored copy
        without .lock; the file lock keeps excluding writers of version 0.1.0."""
        if self.readonly:
            if not (self.root / "main").is_dir():
                raise StoreError(f"No chat found at {self.root}")
            mode = fcntl.LOCK_SH
            busy = (f"Chat is open for writing in another process: {self.root}. "
                    "Use /status, /view or /zoom in that session.")
            paths = [(self.root, os.O_RDONLY)]
            if (self.root / ".lock").exists():
                paths.append((self.root / ".lock", os.O_RDONLY))
        else:
            if not self.root.exists():
                self.root.mkdir(parents=True)
                sync_dir(self.root.parent)
            mode = fcntl.LOCK_EX
            busy = f"Chat is already open in another process: {self.root}"
            paths = [(self.root, os.O_RDONLY), (self.root / ".lock", os.O_RDWR | os.O_CREAT)]
        fds = []
        try:
            for path, flags in paths:
                fds.append(os.open(path, flags, 0o644))
                fcntl.flock(fds[-1], mode | fcntl.LOCK_NB)
        except BaseException as error:
            for fd in fds:
                os.close(fd)
            if isinstance(error, BlockingIOError):
                raise StoreError(busy) from None
            raise
        return fds

    # Startup validation ----------------------------------------------------------

    def _load(self, kind, index, check):
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
                        if not isinstance(record, dict):
                            raise ValueError("record is not an object")
                        if (not isinstance(record["text"], str) or type(record["size"]) is not int
                                or record["size"] != byte_size(record["text"])):
                            raise ValueError("incorrect UTF-8 size")
                        key = check(record)
                        if key in index:
                            raise ValueError("duplicate record")
                    except (ValueError, KeyError, TypeError, AttributeError, OSError, StoreError) as e:
                        raise StoreError(f"Corrupt {path.name} at byte {offset}: {e}. {RESTORE}") from e
                    index[key] = Location(path, offset, record["size"])

    def _check_message(self, record):
        key = record["i"]
        if type(key) is not int or key != len(self._messages):
            raise ValueError("non-contiguous message id")
        if not isinstance(record["kind"], str) or record["kind"] not in KINDS:
            raise ValueError("unknown message kind")
        if not isinstance(record["date"], str):
            raise ValueError("invalid date")
        datetime.fromisoformat(record["date"])
        attachments = record.get("attachments", [])
        if not isinstance(attachments, list):
            raise ValueError("invalid attachments")
        for ref in attachments:
            identity = image_identity(ref)
            if identity not in self._verified:
                self.image_bytes(ref)
                self._verified.add(identity)
        if "event_id" in record:
            event_id, part, parts = record["event_id"], record["event_part"], record["event_parts"]
            if not isinstance(event_id, str) or type(part) is not int or type(parts) is not int:
                raise ValueError("invalid durable event fields")
            prior = self._events.setdefault(event_id, [])
            if (part != len(prior) or parts <= len(prior)
                    or prior and parts != self.message(prior[0])["event_parts"]):
                raise ValueError("invalid durable event sequence")
            prior.append(key)
        return key

    def _check_node(self, record):
        l, i = record["l"], record["i"]
        if type(l) is not int or type(i) is not int or l < 0 or i < 0:
            raise ValueError("invalid node address")
        # Bound the level before shifting, so a corrupt record can't allocate a huge int.
        if l > len(self._messages).bit_length() or (i + 1) << l > len(self._messages):
            raise ValueError("node covers absent messages")
        if l and any(child not in self._nodes for child in children((l, i))):
            raise ValueError("tree parent has a missing child")
        return l, i

    # Reading ---------------------------------------------------------------------

    @property
    def message_count(self):
        return len(self._messages)

    @property
    def node_count(self):
        return len(self._nodes)

    def has_node(self, key):
        return key in self._nodes

    def node_keys(self):
        """All built nodes. A full scan: for startup only."""
        return list(self._nodes)

    @staticmethod
    def _read(location):
        with location.path.open("rb") as f:
            f.seek(location.offset)
            return json.loads(f.readline())

    def message(self, i):
        if type(i) is not int or i not in self._messages:
            raise ValueError(f"Unknown message id: {i}")
        return self._read(self._messages[i])

    def node(self, key):
        if key not in self._nodes:
            raise ValueError(f"Summary is not built: {key}")
        return self._read(self._nodes[key])

    def node_text(self, key):
        return self.node(key)["text"]

    def node_bytes(self, key):
        """UTF-8 size of a node's text, from the index, without reading the file."""
        if key not in self._nodes:
            raise ValueError(f"Summary is not built: {key}")
        return self._nodes[key].size

    def event_complete(self, event_id):
        ids = self._events.get(event_id)
        return bool(ids) and len(ids) == self.message(ids[0])["event_parts"]

    def image_bytes(self, ref):
        """The verified bytes of an attachment reference {sha256, media_type}."""
        image_identity(ref)
        try:
            data = (self.root / "assets" / ref["sha256"]).read_bytes()
        except FileNotFoundError:
            raise StoreError(f"Missing attachment asset {ref['sha256']}") from None
        if hashlib.sha256(data).hexdigest() != ref["sha256"]:
            raise StoreError("Attachment checksum mismatch")
        try:
            matches = image_type(data) == ref["media_type"]
        except ValueError:
            matches = False
        if not matches:
            raise StoreError("Attachment media type does not match its bytes")
        return data

    def read_state(self, name, *, required=False):
        """A saved JSON state file, or None if absent. A required file may be
        absent only while the chat is still empty: refitting would change memory."""
        path = self.root / STATE_FILES[name]
        if not path.exists():
            if required and self._messages:
                raise StoreError(f"Missing {path.name} for existing chat; restore a verified backup. "
                                 "Automatic refitting would change the saved memory view.")
            return None
        try:
            value = json.loads(path.read_bytes())
            if value is None:
                raise ValueError("null is not a saved state")
            return value
        except ValueError as e:
            raise StoreError(f"Invalid {path.name}; restore a verified backup") from e

    # Writing ---------------------------------------------------------------------

    def _check_writable(self):
        if self.readonly:
            raise StoreError("This chat is open read-only")
        if self._poisoned or not self._lock:
            raise StoreError("Storage is closed or had an uncertain write. Reopen and validate before writing again.")

    def _append(self, kind, record):
        self._check_writable()
        data = encode(record) + b"\n"  # encoding errors happen before any write
        # If the clock moves backwards, keep appending to the latest shard so
        # lexicographic startup order still preserves permanent message IDs.
        day = max(record.get("date", utc_now())[:10], self._latest[kind])
        self._latest[kind] = day
        path = self.root / kind / f"{day}.jsonl"
        try:
            f = self._files.get(path)
            if f is None:
                existed = path.exists()
                f = self._files[path] = path.open("ab")
                if not existed:
                    sync_dir(path.parent)
            offset = f.tell()
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
            return Location(path, offset, record["size"])
        except OSError as e:
            self._poisoned = True
            raise StoreError("Uncertain log write; further writes are disabled. Reopen and validate the chat.") from e
        except BaseException:
            # Interrupted (Ctrl-C, cancellation) with the record possibly on disk:
            # never hand out its ID again in this process.
            self._poisoned = True
            raise

    def append(self, kind, text, *, date=None, attachments=None, event_id=None):
        """Log text as consecutive records; returns their permanent IDs.

        With an event_id, a retry after a crash completes the missing parts and
        returns the IDs of the parts already logged.
        """
        self._check_writable()
        if not isinstance(kind, str) or kind not in KINDS:
            raise ValueError(f"Unsupported message kind: {kind}")
        if not isinstance(text, str):
            raise ValueError("Message text must be a string")
        if event_id is not None and (not isinstance(event_id, str) or not event_id):
            raise ValueError("event_id must be a nonempty string")
        date = utc_now() if date is None else date
        if not isinstance(date, str):
            raise ValueError("Message date must be an ISO date string")
        datetime.fromisoformat(date)  # validate before appending an unreadable record
        if kind == "echo":
            text = clip_output(text)
        for ref in attachments or []:
            self.image_bytes(ref)
        # Store exactly the reference, never extra keys a caller passed along.
        attachments = [{"sha256": ref["sha256"], "media_type": ref["media_type"]} for ref in attachments or []]
        parts = list(split_text(text, self.message_bytes))
        existing = self._events.get(event_id, []) if event_id else []
        ids = []
        for part_index, part in enumerate(parts):
            if part_index < len(existing):
                previous = self.message(existing[part_index])
                if (previous["text"] != part or previous["event_parts"] != len(parts)
                        or previous["kind"] != kind
                        or previous.get("attachments", []) != (attachments if part_index == 0 else [])):
                    raise StoreError("Durable event was retried with different contents")
                ids.append(previous["i"])
                continue
            i = len(self._messages)
            record = dict(i=i, kind=kind, text=part, size=byte_size(part), date=date)
            if attachments and not ids:
                record["attachments"] = attachments
            if event_id:
                record.update(event_id=event_id, event_part=part_index, event_parts=len(parts))
            try:
                self._messages[i] = self._append("main", record)
                if event_id:
                    self._events.setdefault(event_id, []).append(i)
            except BaseException:
                # Interrupted between the write and the index update: the record
                # may be on disk, so its ID must never be handed out again.
                self._poisoned = True
                raise
            ids.append(i)
        return ids

    def add_node(self, key, text):
        if key in self._nodes:
            raise StoreError(f"Node already exists: {key}")
        l, i = key
        if l < 0 or i < 0 or l > len(self._messages).bit_length() or (i + 1) << l > len(self._messages):
            raise ValueError("Node is outside the chat")
        if l and any(child not in self._nodes for child in children(key)):
            raise ValueError("Both children must exist")
        try:
            self._nodes[key] = self._append("tree", dict(l=l, i=i, text=text, size=byte_size(text)))
        except BaseException:
            self._poisoned = True
            raise

    def add_image(self, filename):
        """Save an image as a content-addressed asset; returns its reference."""
        self._check_writable()
        data = read_image_file(filename)
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError(f"Image must be at most {MAX_IMAGE_BYTES // 1_000_000} MB")
        media_type = image_type(data)
        digest = hashlib.sha256(data).hexdigest()
        path = self.root / "assets" / digest
        ref = {"sha256": digest, "media_type": media_type}
        try:
            self.image_bytes(ref)
        except StoreError:
            # Absent, or torn by a crash: write the exact bytes its name commits to.
            atomic_bytes(path, data)
        return ref

    def write_state(self, name, value):
        self._check_writable()
        atomic_json(self.root / STATE_FILES[name], value)

    # Agents ----------------------------------------------------------------------

    def _agent_path(self, name, *, new=False):
        return self.root / "agents" / check_agent_name(name, new=new)

    def create_agent(self, name, task):
        """Reserve a permanent agent name and save its running state."""
        self._check_writable()
        path = self._agent_path(name, new=True)
        if path.exists():
            raise ValueError("Agent names are permanent; choose an unused name")
        path.mkdir()
        sync_dir(path.parent)
        atomic_json(path / "state.json", {"status": "running", "task": task})

    def agent_state(self, name):
        path = self._agent_path(name) / "state.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_bytes())
        except ValueError as e:
            raise StoreError(f"Invalid state for agent {name}; restore a verified backup") from e

    def save_agent_state(self, name, state):
        self._check_writable()
        atomic_json(self._agent_path(name) / "state.json", state)

    def agent_reports(self):
        """(name, report) for every agent whose final report is saved."""
        reports = []
        for path in sorted((self.root / "agents").glob("*/state.json")):
            if not SAVED_AGENT_NAME.fullmatch(path.parent.name):
                continue
            state = self.agent_state(path.parent.name)
            if isinstance(state, dict) and "report" in state:
                reports.append((path.parent.name, state["report"]))
        return reports

    def open_agent_log(self, name):
        """A separate writable Store for an agent's own transcript."""
        return Store(self._agent_path(name), self.message_bytes)

    def agent_records(self, name):
        """An agent transcript's records, read without taking its lock."""
        records = []
        for path in sorted((self._agent_path(name) / "main").glob("*.jsonl")):
            for line in path.read_bytes().splitlines(keepends=True):
                if not line.endswith(b"\n"):
                    raise StoreError(f"Corrupt transcript for agent {name}: incomplete final record")
                try:
                    records.append(json.loads(line))
                except ValueError as e:
                    raise StoreError(f"Corrupt transcript for agent {name}") from e
        return records

    # Lifetime --------------------------------------------------------------------

    def close(self):
        try:
            for f in self._files.values():
                f.close()
        finally:
            self._files.clear()
            for fd in self._lock:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            self._lock = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
