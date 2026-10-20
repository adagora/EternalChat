"""The tools that touch this computer: shell, read, write and edit.

The engine never runs anything itself; it awaits Machine.run for these tools,
in every connection mode (the CLIs' own tools stay off). Each caller works in
a folder of its own under the work folder: `main` for the chat, an agent's
name for an agent. It is no sandbox: like the user's own shell, a command may
reach anything the user's account can. Every shell is a process group of its
own, so a stopped call, and closing the chat, ends it and what it started.
A file changed answers as a unified diff. The tools' contract follows
UniiChat's Machine.ts (Apache-2.0; see THIRD_PARTY_NOTICES.md).
"""
import asyncio
import difflib
import os
import signal
import socket
import time
from pathlib import Path

from .config import clip_output

OUTPUT_CHARS = 30_000  # what a result keeps, head and tail (config.clip_output)
KEEP_BYTES = 4 * OUTPUT_CHARS  # bytes of a command's output held while it runs, head and tail
CONTEXT = 3  # lines kept around each change in a diff
# What a command must not inherit: the keys this program reaches models with.
SECRETS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY",
           "CLAUDE_CODE_OAUTH_TOKEN")
NAMES = ("shell", "read", "write", "edit")


class Machine:
    """This computer, for the device tools. `work` holds each caller's folder."""

    def __init__(self, work, name=None):
        self.work = Path(work).expanduser()
        self.name = name or socket.gethostname().split(".")[0]
        self._groups = set()  # process groups started here, until they end

    def folder(self, caller):
        folder = self.work / caller
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def path(self, caller, given):
        if not isinstance(given, str) or not given:
            raise ValueError("path must be a nonempty string")
        path = Path(given).expanduser()
        return path if path.is_absolute() else self.folder(caller) / path

    async def run(self, tool, args, caller):
        """(text, failed) of one call; raises ValueError for malformed input."""
        if not isinstance(args, dict):
            raise ValueError("Tool input must be an object")
        if tool == "shell":
            return await self.shell(_need(args, "command", str), caller)
        if tool == "read":
            return self.read(caller, _need(args, "path", str), offset=_int(args, "offset", 1),
                             limit=_int(args, "limit", None), at=_int(args, "at", 0)), False
        if tool == "write":
            return self.write(caller, _need(args, "path", str), _need(args, "content", str)), False
        if tool == "edit":
            return self.edit(caller, _need(args, "edits", list))
        raise ValueError(f"Unknown tool: {tool}")

    # Shell -----------------------------------------------------------------------

    async def shell(self, command, caller):
        env = {k: v for k, v in os.environ.items() if k not in SECRETS and not k.startswith("ETERNALCHAT_")}
        env.update(PAGER="cat", GIT_PAGER="cat", TERM="dumb")
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            os.environ.get("SHELL") or "/bin/sh", "-c", command, cwd=self.folder(caller), env=env,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True)
        self._groups.add(process.pid)
        try:
            output = await _read_all(process.stdout)
            code = await process.wait()
        except asyncio.CancelledError:
            await _end_group(process)
            raise
        finally:
            self._groups.discard(process.pid)
        seconds = time.monotonic() - started
        text = clip_output(output, OUTPUT_CHARS).rstrip("\n")
        return (text + "\n" if text else "") + f"(exit {code} in {seconds:.1f} s)", code != 0

    def close(self):
        """End every shell still running, with whatever it left in its group."""
        for pid in list(self._groups):
            _kill_group(pid, signal.SIGKILL)
        self._groups.clear()

    # Files -----------------------------------------------------------------------

    def read(self, caller, given, *, offset=1, limit=None, at=0):
        path = self.path(caller, given)
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            return f"{given} is a binary file of {len(data):,} bytes."
        lines = data.decode("utf-8", errors="replace").splitlines()
        if offset < 1 or (limit is not None and limit < 1) or at < 0:
            raise ValueError("offset and limit start at 1; at at 0")
        first = min(offset, len(lines) + 1)
        last = len(lines) if limit is None else min(len(lines), first + limit - 1)
        shown, size = [], 0
        for number in range(first, last + 1):
            text = lines[number - 1]
            if number == first and at:
                text = text[at:]
            row = f"{number:>6}\t{text}"
            if shown and size + len(row) + 1 > OUTPUT_CHARS:
                last = number - 1
                break
            shown.append(row[:OUTPUT_CHARS])
            size += len(row) + 1
        if not lines:
            return f"{given} is empty."
        more = f"; read on with offset={last + 1}" if last < len(lines) and limit is None else ""
        return "\n".join(shown) + f"\n[lines {first}-{last} of {len(lines)}{more}]"

    def write(self, caller, given, content):
        path = self.path(caller, given)
        old = _text(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return _diff(given, old, content)

    def edit(self, caller, edits):
        """Apply every edit in order, or none: each sees its file as the edits
        before it left it. Answers each changed file's diff."""
        if not edits:
            raise ValueError("edits must list at least one edit")
        before, after, order = {}, {}, []
        for k, edit in enumerate(edits, 1):
            if not isinstance(edit, dict) or set(edit) - {"path", "old", "new", "all"}:
                raise ValueError(f"edit {k} must have path, old, new and optionally all")
            given, old, new = _need(edit, "path", str), _need(edit, "old", str), _need(edit, "new", str)
            path = self.path(caller, given)
            if path not in after:
                text = _text(path)
                if text is None:
                    return f"No file changed: edit {k}: {given} does not exist.", True
                before[path] = after[path] = text
                order.append((path, given))
            if not old:
                return f"No file changed: edit {k}: old must not be empty.", True
            found = after[path].count(old)
            if found == 0:
                return f"No file changed: edit {k}: its old text is not in {given}.", True
            if found > 1 and not edit.get("all"):
                return (f"No file changed: edit {k}: its old text is in {given} {found} times; "
                        "give more of the text around it, or all: true."), True
            after[path] = after[path].replace(old, new)
        for path, _ in order:
            path.write_text(after[path], encoding="utf-8")
        return "\n".join(_diff(given, before[path], after[path]) for path, given in order), False


def _need(args, key, kind):
    value = args.get(key)
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise ValueError(f"{key} must be a {kind.__name__}")
    return value


def _int(args, key, default):
    if args.get(key) is None:
        return default
    return _need(args, key, int)


def _text(path):
    try:
        return path.read_bytes().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return None


def _diff(given, old, new):
    """A unified diff of a file's change; a new file is all lines added."""
    lines = difflib.unified_diff((old or "").splitlines(), new.splitlines(), fromfile="a/" + given,
                                 tofile="b/" + given, n=CONTEXT, lineterm="")
    body = "\n".join(lines)
    if old is None:
        return body or f"Created {given} (empty)."
    return body or f"{given} is unchanged."


async def _read_all(stream):
    """A command's output, its head and tail kept when it is long."""
    head, tail, total = bytearray(), bytearray(), 0
    while chunk := await stream.read(65536):
        total += len(chunk)
        if len(head) < KEEP_BYTES:
            room = KEEP_BYTES - len(head)
            head += chunk[:room]
            chunk = chunk[room:]
        tail += chunk
        del tail[:-KEEP_BYTES]
    text = head.decode("utf-8", errors="replace")
    if tail:
        gap = "\n[… output clipped …]\n" if total > len(head) + len(tail) else ""
        text += gap + tail.decode("utf-8", errors="replace")
    return text


def _kill_group(pid, sig):
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


async def _end_group(process):
    """Stop a shell and its group: politely, then for good after two seconds."""
    _kill_group(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), 2)
    except (TimeoutError, asyncio.TimeoutError):
        pass
    _kill_group(process.pid, signal.SIGKILL)
