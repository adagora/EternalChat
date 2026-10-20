"""Past AI chats, read for import: Claude Code's and Codex's logs on this
computer, and the Claude and ChatGPT websites' exports (their zip, or its
conversations.json).

Each session becomes a note naming it, then its messages with their own
dates: the person's words as user, the other AI's replies as unii, and, only
when asked, its tool calls and their results as tool and echo; never its
thinking. Secrets that look like keys or tokens are redacted first: the chat
keeps what it is given for good. A session imported before brings only what is
newer (Engine.import_chats keeps each session's last date). Reading follows
UniiChat's Import.ts (Apache-2.0; see THIRD_PARTY_NOTICES.md).
"""
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import byte_size, clip_output

SUMMARY_SOURCE = 512  # a message longer than this (as "kind: text") costs a summary call
TOOL_CHARS = 4_000  # what an imported tool call or result keeps, head and tail
INJECTED = re.compile(r"^(<[a-z_]+[ >]|# AGENTS\.md)")  # what a client put in a person's message
SKIP = re.compile(r"^(<command-|<local-command|\[Request interrupted)")
SECRETS = [re.compile(p) for p in (
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    r"\bsk-ant-[A-Za-z0-9_\-]{20,}", r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}",
    r"\bgh[pousr]_[A-Za-z0-9]{30,}", r"\bgithub_pat_[A-Za-z0-9_]{30,}", r"\bAKIA[0-9A-Z]{16}\b",
    r"\bxox[abprs]-[A-Za-z0-9\-]{10,}", r"\bBearer\s+[A-Za-z0-9._~+/\-]{20,}=*",
)]
NAMED_SECRET = re.compile(r"(?i)\b((?:api[_-]?key|access[_-]?token|auth[_-]?token|secret|password|passwd)"
                          r"[\"']?\s*[:=]\s*[\"']?)([^\s\"',;]{8,})")


@dataclass
class Session:
    app: str  # Claude Code, Codex, Claude, ChatGPT
    where: str  # "session in <folder>", or 'chat "<title>"'
    start: str  # its first message's date
    messages: list = field(default_factory=list)  # (kind, text, date), as wanted

    @property
    def key(self):
        """The session's name among those imported: its start and where."""
        return f"{self.start} {self.where}"

    @property
    def note(self):
        return f"{self.app} {self.where}, from {self.start[:16].replace('T', ' ')} UTC (imported)"

    @property
    def calls(self):
        """Summary calls its messages need (a short message is kept as it is)."""
        return sum(byte_size(f"{kind}: {text}") > SUMMARY_SOURCE for kind, text, _ in self.messages)


def default_paths():
    home = Path.home()
    return [home / ".claude" / "projects", home / ".codex" / "sessions", home / ".codex" / "archived_sessions"]


def find(paths=None, *, since="", tools=False, done=None):
    """The sessions in `paths` (folders of logs, exports, or log files) with
    messages to import: from `since` (YYYY-MM-DD) on, newer than `done`'s date
    for the session, with tool calls if `tools`. Newest first."""
    done = done or {}
    found = []
    for path in [Path(p).expanduser() for p in (paths or default_paths())]:
        if path.is_dir():
            for log in sorted(path.rglob("*.jsonl")):
                if "subagents" in log.parts or (since and _day(log.stat().st_mtime) < since):
                    continue  # an agent's log is no person's chat; a file last written before since has nothing newer
                found.append(read_log(log, tools))
        elif path.suffix in (".zip", ".json"):
            found.extend(read_export(path, tools))
        elif path.suffix == ".jsonl":
            found.append(read_log(path, tools))
    sessions = []
    for session in filter(None, found):
        last = done.get(session.key, "")
        session.messages = [m for m in session.messages if m[2] >= since and m[2] > last]
        if session.messages:
            sessions.append(session)
    return sorted(sessions, key=lambda s: s.start, reverse=True)


def redact(text):
    """The text with what looks like a secret replaced by [redacted]."""
    for pattern in SECRETS:
        text = pattern.sub("[redacted]", text)
    return NAMED_SECRET.sub(lambda m: m.group(1) + "[redacted]", text)


# Logs ----------------------------------------------------------------------------

def read_log(path, tools):
    """A Claude Code or Codex session log as a Session; None if it holds no chat."""
    session = Session(app="", where="", start="")
    date = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as lines:
            for line in lines:
                try:
                    j = json.loads(line)
                except ValueError:
                    continue  # one odd line never fails its session
                if not isinstance(j, dict):
                    continue
                if not session.app:
                    session.app = "Codex" if j.get("type") == "session_meta" else "Claude Code"
                date = _date(j.get("timestamp"), date)
                found = _codex(session, j, date) if session.app == "Codex" else _claude(session, j, date)
                session.messages.extend(m for m in found if tools or m[0] in ("user", "unii"))
    except OSError:
        return None
    if not session.messages or not session.where:
        return None
    session.start = session.messages[0][2]
    return session


def _claude(session, j, date):
    if j.get("cwd") and not session.where:
        session.where = "session in " + str(j["cwd"])
    if j.get("type") == "attachment" and (j.get("attachment") or {}).get("type") == "queued_command":
        return _messages("user", str(j["attachment"].get("prompt", "")), date)
    own = j.get("type") in ("user", "assistant") and not any(j.get(k) for k in ("isMeta", "isSidechain",
                                                                                     "isCompactSummary"))
    if not own:
        return []
    content = (j.get("message") or {}).get("content")
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content if isinstance(
        content, list) else []
    return _blocks("user" if j["type"] == "user" else "unii", blocks, date)


def _codex(session, j, date):
    payload = j.get("payload") or {}
    if j.get("type") == "session_meta":
        source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
        session.where = "" if source.get("subagent") or source.get("internal") else (
            "session in " + str(payload.get("cwd", "")))
    if j.get("type") != "response_item" or not session.where:
        return []
    kind = payload.get("type")
    if kind == "message":
        role = {"user": "user", "assistant": "unii"}.get(payload.get("role"))
        parts = [p for p in payload.get("content") or [] if not INJECTED.match(str((p or {}).get("text", "")))]
        return _messages(role, _text(parts), date) if role else []
    if kind == "function_call":
        return _messages("tool", f"{payload.get('name')} {payload.get('arguments')}", date)
    if kind == "custom_tool_call":
        return _messages("tool", f"{payload.get('name')} {json.dumps({'input': payload.get('input')})}", date)
    if kind == "local_shell_call":
        command = (payload.get("action") or {}).get("command")
        return _messages("tool", "shell " + json.dumps({"command": command}), date)
    if kind in ("function_call_output", "custom_tool_call_output"):
        return _messages("echo", _text(payload.get("output")), date)
    return []


# Exports -------------------------------------------------------------------------

def read_export(path, tools):
    """A website's export (its zip, or conversations.json) as Sessions, a chat each."""
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            name = next((n for n in archive.namelist() if n.endswith("conversations.json")), None)
            if name is None:
                raise ValueError(f"{path.name}: no conversations.json in the zip")
            chats = json.loads(archive.read(name))
    else:
        chats = json.loads(path.read_bytes())
    if not isinstance(chats, list) or not all(isinstance(c, dict) and ("mapping" in c or isinstance(
            c.get("chat_messages"), list)) for c in chats):
        raise ValueError(f"{path.name} is not a Claude or ChatGPT export")
    sessions = []
    for chat in chats:
        app, found = ("ChatGPT", _chatgpt(chat)) if "mapping" in chat else ("Claude", _claude_site(chat))
        messages = [m for m in found if tools or m[0] in ("user", "unii")]
        if messages:
            title = str(chat.get("title") or chat.get("name") or "untitled")
            sessions.append(Session(app=app, where=f'chat "{title}"', start=messages[0][2], messages=messages))
    return sessions


def _chatgpt(chat):
    """A ChatGPT chat's thread shown (current_node up to the root), never thoughts."""
    nodes, seen = [], set()
    node = chat.get("current_node")
    mapping = chat.get("mapping") or {}
    while node and node not in seen and node in mapping:
        seen.add(node)
        nodes.append((mapping[node] or {}).get("message"))
        node = mapping[node].get("parent")
    date = _date(chat.get("create_time"), "")
    out = []
    for m in reversed(nodes):
        if not isinstance(m, dict):
            continue
        role = (m.get("author") or {}).get("role")
        content = m.get("content") or {}
        kind = {"user": "user", "tool": "echo"}.get(role) or (
            ("unii" if m.get("recipient") == "all" else "tool") if role == "assistant" else None)
        date = _date(m.get("create_time"), date)
        hidden = (m.get("metadata") or {}).get("is_visually_hidden_from_conversation") is True
        if kind is None or hidden or content.get("content_type") in ("thoughts", "reasoning_recap"):
            continue
        text = _text(content.get("parts") or [content.get("text", "")])
        out.extend(_messages(kind, (f"{m.get('recipient')} " if kind == "tool" else "") + text, date))
    return out


def _claude_site(chat):
    date = _date(chat.get("created_at"), "")
    out = []
    for m in chat.get("chat_messages") or []:
        date = _date(m.get("created_at"), date)
        content = m.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": str(m.get("text", ""))}]
        files = [{"type": "text", "text": f"[file {f.get('file_name')}]"}
                 for f in (m.get("attachments") or []) + (m.get("files") or []) if isinstance(f, dict)]
        out.extend(_blocks("user" if m.get("sender") == "human" else "unii", blocks + files, date))
    return out


# Messages ------------------------------------------------------------------------

def _blocks(kind, blocks, date):
    """Claude-shaped blocks: the texts as one message, then each tool call and result."""
    blocks = [b for b in blocks if isinstance(b, dict)]
    said = [b for b in blocks if b.get("type") == "image" or (b.get("type") == "text" and not SKIP.match(
        str(b.get("text", ""))))]
    out = _messages(kind, _text(said), date)
    for b in blocks:
        if b.get("type") == "tool_use":
            out += _messages("tool", f"{b.get('name')} {json.dumps(b.get('input'), ensure_ascii=False)}", date)
        elif b.get("type") == "tool_result":
            out += _messages("echo", _text(b.get("content")), date)
    return out


def _messages(kind, text, date):
    text = text.strip()
    if not text or not date:
        return []
    if kind in ("tool", "echo"):
        text = clip_output(text, TOOL_CHARS)
    return [(kind, redact(text), date)]


def _text(content):
    """Content as text: a string, or parts of text and images."""
    if isinstance(content, str):
        return content
    out = []
    for part in content if isinstance(content, list) else []:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, dict) and isinstance(part.get("text"), str):
            out.append(part["text"])
        elif isinstance(part, dict) and "image" in str(part.get("type") or part.get("content_type")):
            out.append("[image]")
    return "\n".join(out)


def _date(value, last):
    """An ISO date in UTC: a log's string or seconds; none or bad: `last`."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            moment = datetime.fromtimestamp(value, timezone.utc)
        elif isinstance(value, str) and value:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            moment = moment.replace(tzinfo=moment.tzinfo or timezone.utc).astimezone(timezone.utc)
        else:
            return last
    except (ValueError, OverflowError, OSError):
        return last
    return moment.isoformat(timespec="seconds")


def _day(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%d")
