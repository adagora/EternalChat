"""Local web interface: the chat as one page in your browser, served by this process.

`eternalchat web` opens the chat once and serves it on 127.0.0.1 only. Like the
terminal session, the page is a view of the chat: it sends what you type and
shows what the Engine publishes, through Engine's public methods alone. The
HTTP layer is a small asyncio server on the engine's own event loop, so no
request ever touches the chat from another thread.

Every API call must carry this computer's token (~/.eternalchat/web-token,
readable only by you; the page reads it from the link's #fragment, which
browsers never send) in a header, and every request must name this server as
its Host. Another website open in the same browser can then neither read the
chat (DNS rebinding) nor post to it: a custom header needs a CORS preflight,
which this server never grants.

Other devices at home reach it through an HTTPS proxy on this computer, such as
`tailscale serve`, whose name --allow-host admits; or, on a trusted network,
directly with --host 0.0.0.0, which also admits this computer's name and any IP
address (a name an attacker could rebind is never one of them). The page, its
icons and its service worker are public; the chat is only behind the API.
"""
import asyncio
import functools
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import webbrowser
from argparse import Namespace
from importlib import resources
from pathlib import Path

from . import __version__, history, settings
from .config import MAX_IMAGE_BYTES, Config
from .machine import Machine
from .prompt import tool_content
from .web_imports import Imports, MAX_UPLOAD
from .audio import Transcriber, MAX_AUDIO
from .web_sessions import Sessions
from .push import Push, subscription as push_subscription
from .openai_provider import DEFAULT_MODEL as OPENAI_MODEL

LINE = re.compile(r"(\d+)\+(\d+)\|(.*)")  # id+n|text, as the view and zoom print lines
MAX_BODY = 1_000_000
BACKLOG = 2_000  # events a page may fall behind before it is dropped and reloads
PING = 15  # seconds between keep-alive comments on an event stream
REASONS = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 409: "Conflict",
           413: "Content Too Large", 431: "Request Header Fields Too Large"}
PAGE_HEADERS = ("Content-Security-Policy: default-src 'none'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline'; img-src 'self' blob:; connect-src 'self'; manifest-src 'self'; "
                "worker-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",)
LOOPBACK = ("127.0.0.1", "localhost", "[::1]")
# The page's public files: what a browser needs to install it as an app.
STATIC = {"/manifest.webmanifest": "application/manifest+json", "/sw.js": "text/javascript; charset=utf-8",
          "/icon.svg": "image/svg+xml", "/icon-192.png": "image/png", "/icon-512.png": "image/png",
          "/icon-maskable.png": "image/png", "/apple-touch-icon.png": "image/png"}


class HTTPError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


@functools.cache
def page():
    return resources.files(__package__).joinpath("web.html").read_bytes()


@functools.cache
def static(path):
    return resources.files(__package__).joinpath("static", path.lstrip("/")).read_bytes()


def lan_names():
    """(the .local name mDNS advertises, or None; every name of this computer on
    its network). A Mac's Bonjour name is its own (scutil), not its host name,
    which the router may have chosen; on Linux, Avahi advertises the host name."""
    here = socket.gethostname().lower()
    advertised = None
    if sys.platform == "darwin":
        try:
            found = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=2)
            if found.returncode == 0 and found.stdout.strip():
                advertised = found.stdout.strip().lower() + ".local"
        except (OSError, subprocess.SubprocessError):
            pass
    elif os.path.exists("/run/avahi-daemon/socket"):
        advertised = here.split(".")[0] + ".local"
    names = [here, here.split(".")[0] + ".local"]
    return advertised, ([advertised] if advertised else []) + names


def is_ip(name):
    try:
        ipaddress.ip_address(name.strip("[]"))
    except ValueError:
        return False
    return True


def web_token():
    """This computer's token for the page: made once, readable only by its owner."""
    path = settings.home() / "web-token"
    try:
        token = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        token = ""
    if len(token) < 32:
        path.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # ubs:ignore — fdopen context below closes this descriptor.
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(token + "\n")
    return token


def lines(text):
    return [{"id": int(m[1]), "n": int(m[2]), "text": m[3]}
            for m in map(LINE.fullmatch, text.splitlines()) if m]


def reply(status, body=b"", content_type="application/json", headers=()):
    if not isinstance(body, bytes):
        body = json.dumps(body, ensure_ascii=False).encode()
    head = [f"HTTP/1.1 {status} {REASONS[status]}", f"Content-Type: {content_type}",
            f"Content-Length: {len(body)}", "Cache-Control: no-store", "Connection: close",
            "X-Content-Type-Options: nosniff", "Referrer-Policy: no-referrer", *headers]
    return ("\r\n".join(head) + "\r\n\r\n").encode() + body


async def read_request(reader):
    """(method, path, query, headers, body) of one request; one per connection."""
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except asyncio.LimitOverrunError:
        raise HTTPError(431, "Request headers are too large") from None
    request_line, *fields = head.decode("latin-1").split("\r\n")
    try:
        method, target, _ = request_line.split(" ")
    except ValueError:
        raise HTTPError(400, "Malformed request") from None
    headers = {}
    for field in fields:
        name, colon, value = field.partition(":")
        if colon:
            headers[name.strip().lower()] = value.strip()
    if "transfer-encoding" in headers:
        raise HTTPError(400, "Send the body with a Content-Length")
    url = urllib.parse.urlsplit(target)
    limit = (MAX_AUDIO if url.path == "/api/transcribe" else
             MAX_UPLOAD if url.path == "/api/import/upload" else
             MAX_IMAGE_BYTES if url.path == "/api/attach" else
             4 * ((MAX_IMAGE_BYTES + 2) // 3) + MAX_BODY if url.path == "/api/devices/result" else MAX_BODY)
    try:
        length = int(headers.get("content-length", "0"))
    except ValueError:
        raise HTTPError(400, "Malformed Content-Length") from None
    if not 0 <= length <= limit:
        raise HTTPError(413, f"The body may be at most {limit:,} bytes")
    body = await reader.readexactly(length)
    return method, url.path, dict(urllib.parse.parse_qsl(url.query)), headers, body


def _settled(future):
    """Replies report their errors as events; a page needs no answer to its send."""
    if not future.cancelled():
        future.exception()


class WebChat:
    """One chat served to pages: its engine, the pages' event streams and the token."""

    def __init__(self, args, open_engine, out, token):
        self.args, self.open_engine, self.out, self.token = args, open_engine, out, token
        self.streams = set()
        self.remotes = {}  # computers joined with `eternalchat device`, kept across reopening the chat
        self.found = {}  # past chats the last import scan found, by session key
        self.hosts = set()
        self.any_ip = False  # also admit Host names that are IP addresses (listening beyond loopback)
        self.lan = None  # this computer's advertised .local name, when listening beyond loopback
        self.server = None
        self.partial = ""  # current, not-yet-recorded reply block for new terminal views
        self.engine = self._open(args)
        self.imports = Imports(lambda: self.engine)
        self.transcriber = Transcriber()
        self.sessions = Sessions(lambda: self.engine, token)
        self.session_connections = {}
        self.push = Push(self.sessions.recipients, self.expire_push)

    def _open(self, args):
        engine = self.open_engine(args, self.publish)
        if hasattr(engine.machine, "remote"):
            engine.machine.restore_remotes(self.remotes)  # joined computers stay through a reopen
        try:
            engine.resume_reports()
            engine.compact()  # summaries an earlier run left unbuilt start now, unawaited
        except BaseException:
            asyncio.ensure_future(engine.close())
            raise
        return engine

    async def start(self, port, host="127.0.0.1", allow_hosts=()):
        """Listen on host (default 127.0.0.1 only); returns the page's link on this
        computer (port 0: any free port). allow_hosts: more names requests may give
        as their Host, with or without this port, such as an HTTPS proxy's."""
        self.server = await asyncio.start_server(self.handle, host, port)
        port = self.server.sockets[0].getsockname()[1]
        names = [*LOOPBACK, *(name.strip().lower() for name in allow_hosts if name.strip())]
        self.any_ip = host not in ("127.0.0.1", "::1", "localhost")
        if self.any_ip:
            self.lan, more = lan_names()
            names += more
        self.port = port
        self.hosts = {f"{name}:{port}" for name in names} | set(names[len(LOOPBACK):])
        return f"http://127.0.0.1:{port}/#{self.token}"

    def known_host(self, value):
        value = value.lower()
        if value in self.hosts:
            return True
        name, colon, port = value.rpartition(":")
        return bool(self.any_ip and colon and port == str(self.port) and is_ip(name))

    async def close(self):
        if self.server:
            self.server.close()
        self.streams.clear()
        for connections in list(self.session_connections.values()):
            for connection in list(connections):
                connection.close()
        await self.imports.close()
        await self.transcriber.close()
        await self.push.close()
        await self.engine.close()

    # Events ------------------------------------------------------------------------

    def publish(self, kind, value):
        """An engine event, to every open page. Problems also go to the terminal."""
        if kind == "turn_complete" and not self.args.demo and hasattr(self, "push"):
            self.push.submit(value["id"])
        if kind == "text":
            self.partial += value
        elif kind == "message" and value.get("kind") == "unii":
            self.partial = ""
        if kind in ("error", "compaction_error"):
            self.out.line(f"{kind}: {value}", err=True)
        event = json.dumps({"kind": kind, "value": value}, ensure_ascii=False, default=str)
        for queue in list(self.streams):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self.streams.discard(queue)  # far behind: its page reconnects and reloads

    async def stream(self, writer, snapshot=False, token=None):
        queue = asyncio.Queue(BACKLOG)
        self.streams.add(queue)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\n"
                     b"Connection: close\r\nX-Content-Type-Options: nosniff\r\n\r\n")
        if snapshot:
            # Register first, then take this synchronous snapshot: no message can
            # fall between the snapshot and this stream on the engine's event loop.
            state = self.state()
            count = state["status"]["messages"]
            initial = {"kind": "snapshot", "value": {
                **state, "messages": self.engine.messages(max(0, count - 200), count),
                "partial": self.partial}}
            writer.write(b"data: " + json.dumps(initial, ensure_ascii=False).encode() + b"\n\n")
        try:
            while queue in self.streams:
                if token is not None and not self.sessions.authenticate(token):
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), PING)
                    writer.write(b"data: " + event.encode() + b"\n\n")
                except TimeoutError:
                    writer.write(b": ping\n\n")
                await writer.drain()
        finally:
            self.streams.discard(queue)

    # Requests ----------------------------------------------------------------------

    async def handle(self, reader, writer):
        principal = None
        try:
            try:
                method, path, query, headers, body = await read_request(reader)
                if not self.known_host(headers.get("host", "")):
                    raise HTTPError(403, "Unknown host: open the link the terminal printed, or name "
                                         "this one with --allow-host")
                if (method, path) == ("GET", "/"):
                    writer.write(reply(200, page(), "text/html; charset=utf-8", PAGE_HEADERS))
                elif method == "GET" and path in STATIC:
                    writer.write(reply(200, static(path), STATIC[path]))
                elif not path.startswith("/api/"):
                    raise HTTPError(404, "Not found")
                elif (method, path) == ("POST", "/api/sessions/open"):
                    data = json.loads(body or b"{}")
                    if not isinstance(data, dict):
                        raise ValueError("Send a JSON object")
                    writer.write(reply(200, self.sessions.open(headers.get("x-eternalchat-token", ""), data.get("label", "Browser"))))  # ubs:ignore — session exchange, not a filesystem open.
                else:
                    token = headers.get("x-eternalchat-token", "")
                    principal = self.sessions.authenticate(token)
                    if principal is None:
                        raise HTTPError(403, "Session expired or revoked; open a new pairing link")
                    self.session_connections.setdefault(principal["id"], set()).add(writer)
                    if principal["scope"] == "device":
                        if path not in {"/api/devices/hello", "/api/devices/jobs", "/api/devices/result", "/api/devices/ack", "/api/devices/leave"}:
                            raise HTTPError(403, "Computer sessions can only use their own device transport")
                        data = json.loads(body or b"{}") if method == "POST" else query
                        if not isinstance(data, dict) or not isinstance(data.get("name"), str):
                            raise HTTPError(400, "Computer transport requires a text device name")
                        name = data["name"].strip()
                        if not name or principal.get("device_name", name) != name:
                            raise HTTPError(403, "Computer session belongs to another device")
                        if not principal.get("device_name"):
                            if (method, path) != ("POST", "/api/devices/hello"):
                                raise HTTPError(403, "Join this computer first")
                            self.sessions.bind_computer(principal["id"], name)
                    await self.authenticated(method, path, query, headers, body, reader, writer, principal, token)
            except PermissionError as error:
                writer.write(reply(403, {"error": str(error)}))
            except HTTPError as error:
                writer.write(reply(error.status, {"error": error.message}))
            except ConnectionError:
                raise
            except (ValueError, KeyError, TypeError, RuntimeError, OSError) as error:
                writer.write(reply(400, {"error": str(error) or type(error).__name__}))
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass  # the page went away
        finally:
            if principal:
                connections = self.session_connections.get(principal["id"], set())
                connections.discard(writer)
                if not connections:
                    self.session_connections.pop(principal["id"], None)
            writer.close()

    async def authenticated(self, method, path, query, headers, body, reader, writer, principal, token):
        if (method, path) == ("GET", "/api/events"):
            return await self.stream(writer, snapshot=query.get("snapshot") == "1", token=token)
        elif (method, path) == ("GET", "/api/devices/jobs"):
            return await self.device_jobs(writer, query.get("name", ""), headers)
        elif (method, path) == ("POST", "/api/transcribe"):
            work = asyncio.create_task(self.api(method, path, query, body, headers, principal))  # ubs:ignore — cancelled and gathered in finally below.
            disconnected = asyncio.create_task(reader.read(1))  # ubs:ignore — cancelled and gathered in finally below.
            try:
                done, _ = await asyncio.wait({work, disconnected}, return_when=asyncio.FIRST_COMPLETED)
                if disconnected in done:
                    return
                writer.write(reply(200, *await work))
            finally:  # ubs:ignore — cleanup has no control transfer; both child tasks are reaped.
                work.cancel()
                disconnected.cancel()
                await asyncio.gather(work, disconnected, return_exceptions=True)
        else:
            writer.write(reply(200, *await self.api(method, path, query, body, headers, principal)))

    async def api(self, method, path, query, body, headers=None, principal=None):
        """(payload, content type) for an API call."""
        engine = self.engine
        principal = principal or {"id": "master", "scope": "master"}
        if method == "GET":
            if path == "/api/native-push":
                return self.push.native_status(),
            if path == "/api/push":
                current = next((s for s in self.sessions.listing(principal["id"]) if s["current"]), {})
                return {**self.push.status(), "subscribed": current.get("push", False)},
            if path == "/api/sessions":
                return {"current": principal["id"], "sessions": self.sessions.listing(principal["id"])},
            if path == "/api/state":
                return self.state(),
            if path == "/api/messages":
                start = int(query.get("from", 0))
                stop = start + max(1, min(int(query.get("limit", 200)), 500))
                return {"messages": engine.messages(start, stop)},
            if path == "/api/view":
                return {"lines": lines(engine.render_view())},
            if path == "/api/date":
                return {"text": engine.date(int(query["id"]))},
            if path == "/api/zoom":
                page_number = int(query.get("page", 0))
                if "name" in query:
                    found, opened = engine.zoom(name=query["name"], page=page_number), False
                else:
                    n = int(query.get("n", 1))
                    found, opened = engine.zoom(int(query["id"]), n, page=page_number), n > 1
                return {"text": found.text, "lines": lines(found.text) if opened else [],
                        "images": list(found.images)},
            if path == "/api/agent":
                return engine.agent_log(query.get("name", "")),
            if path == "/api/import":
                return await self.scan(query),
            if path == "/api/import/batch":
                return self.imports.status(query.get("batch")),
            if path == "/api/backup":
                return engine.export_backup(), "application/zip"
            if path.startswith("/api/image/"):
                ref = {"sha256": path.rsplit("/", 1)[1], "media_type": query.get("type", "")}
                return engine.image(ref), ref["media_type"]
            raise HTTPError(404, "Not found")
        if method != "POST":
            raise HTTPError(404, "Not found")
        if path == "/api/transcribe":
            if self.args.demo:
                raise ValueError("Recorded transcription is disabled in offline demo mode")
            return await self.transcriber.transcribe(body, (headers or {}).get("content-type", "")),
        if path == "/api/attach":
            with tempfile.TemporaryDirectory(prefix="eternalchat-upload-") as folder:
                upload = Path(folder, "image")
                upload.write_bytes(body)
                return {"ref": engine.attach(upload)},
        if path == "/api/import/upload":
            return await self.imports.upload(query.get("batch"), query.get("name", ""), body),
        data = json.loads(body or b"{}")  # ubs:ignore — handle() catches ValueError and returns HTTP 400.
        if not isinstance(data, dict):
            raise HTTPError(400, "Send a JSON object")
        if path in {"/api/native-push/subscribe", "/api/push/subscribe", "/api/push/test"} and self.args.demo:
            raise ValueError("Network push is disabled in offline demo mode")
        if path == "/api/native-push/subscribe":
            if not self.push.native_status()["configured"]:
                raise ValueError("Native APNs push is not configured on this server")
            token = data.get("token")
            if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64,512}", token):
                raise ValueError("Invalid APNs device token")
            self.sessions.subscription(principal["id"], {"kind":"apns", "token":token})
            return {"ok":True},
        if path == "/api/push/subscribe":
            if not self.push.status()["configured"]:
                raise ValueError("Background push is not configured on this server")
            self.sessions.subscription(principal["id"], push_subscription(data))
            return {"ok": True},
        if path == "/api/push/unsubscribe":
            self.sessions.subscription(principal["id"], None)
            return {"ok": True},
        if path == "/api/push/test":
            if not any(s == principal["id"] for s, _ in self.sessions.recipients()):
                raise ValueError("Enable push on this device first")
            code = await self.push.send(next(v for s, v in self.sessions.recipients() if s == principal["id"]), "test")
            if code not in (200, 201, 202):
                raise ValueError(f"Push service HTTP {code}")
            return {"ok": True},
        if path == "/api/sessions/invite":
            return self.sessions.invite(),
        if path == "/api/sessions/computer":
            return self.sessions.computer(data.get("label", "Computer")),
        if path == "/api/sessions/revoke":
            removed = self.sessions.revoke(data.get("id"))
            for connection in list(self.session_connections.pop(data.get("id"), set())):
                if connection is not None:
                    asyncio.get_running_loop().call_later(.05, connection.close)
            if removed.get("scope") == "device" and removed.get("device_name") in self.remotes:
                self.remotes[removed["device_name"]].leave()
            return {"ok": True},
        if path == "/api/send":
            text, refs = data.get("text", ""), data.get("attachments", [])
            if not isinstance(text, str) or not isinstance(refs, list):
                raise HTTPError(400, "text must be a string and attachments a list")
            if not text.strip() and not refs:
                raise HTTPError(400, "Type a message or attach an image")
            engine.submit(text, attachments=refs).add_done_callback(_settled)
            return {"ok": True},
        if path == "/api/import/batch":
            since, tools = data.get("since", ""), data.get("tools", False)
            if not isinstance(since, str) or since and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
                raise HTTPError(400, "since takes a day as YYYY-MM-DD")
            if type(tools) is not bool:  # ubs:ignore — only literal JSON booleans are accepted, not integer truth values.
                raise HTTPError(400, "tools must be a boolean")
            return self.imports.create(since, tools),
        if path == "/api/import/start":
            return self.imports.start(data.get("batch"), data.get("keys")),
        if path == "/api/import/cancel":
            return self.imports.cancel(data.get("batch")),
        if path == "/api/note":
            if not isinstance(data.get("text"), str) or not data["text"].strip():
                raise HTTPError(400, "Type the note to save")
            return {"ids": engine.note(data["text"])},
        if path == "/api/agent":
            name, task = data.get("name"), data.get("task")
            if not isinstance(name, str) or not isinstance(task, str) or not task.strip():
                raise HTTPError(400, "An agent needs a name and a task")
            engine.start_agent(name, task.strip())
            return {"ok": True},
        if path == "/api/stop":
            if "name" in data:
                if not isinstance(data["name"], str) or not data["name"]:
                    raise HTTPError(400, "name must identify an agent to stop")
                engine.stop_agent(data["name"])
            else:
                engine.stop()
            return {"ok": True},
        if path == "/api/goal":
            if not isinstance(data.get("text"), str) or not data["text"].strip():
                raise HTTPError(400, "A goal needs its text")
            engine.set_goal(data["text"]).add_done_callback(_settled)
            return {"ok": True},
        if path == "/api/halt":
            engine.halt()
            return {"ok": True},
        if path == "/api/compact":
            engine.compact()
            return {"ok": True},
        if path == "/api/settings":
            return await self.switch(data),
        if path == "/api/devices/hello":
            if not hasattr(engine.machine, "join"):
                raise HTTPError(409, "Computer access is off on this chat: turn it on in Settings")
            if not isinstance(data.get("work"), str):
                raise HTTPError(400, "A computer joins with its name and work folder")
            name = data.get("name")
            if isinstance(name, str):
                name = name.strip()
            identity = None
            if "identity" in data:
                identity = engine.authenticate_device(name, data["identity"], data.get("session"), enroll=True)
            elif engine.has_enrolled_device(name):
                raise HTTPError(409, "This name belongs to an enrolled device; use its original work folder")
            remote = engine.machine.join(name, data["work"], data.get("computer", False), data.get("screen_id"), identity)
            self.publish("devices", engine.machine.listing())
            return {"ok": True, "name": remote.name},
        if path == "/api/devices/result":
            remote = self.remotes.get(data.get("name"))
            if remote is None:
                raise HTTPError(404, "No such computer or job")
            self.device_auth(remote, headers)
            if remote.identity and data.get("id") in remote.completed:
                return {"ok": True},  # acknowledgement lost: result was already committed
            if data.get("id") not in remote.pending:
                raise HTTPError(404, "No such computer or job")
            value = data.get("content", data.get("text"))
            content = tool_content(value)
            if type(data.get("failed")) is not bool:
                raise HTTPError(400, "failed must be a boolean")
            remote.answer(data["id"], value if isinstance(value, str) else content, data["failed"])
            return {"ok": True},
        if path == "/api/devices/ack":
            remote = self.remotes.get(data.get("name"))
            if remote is None or not remote.identity:
                raise HTTPError(404, "No such enrolled computer")
            self.device_auth(remote, headers)
            try:
                remote.acknowledge(data.get("id"), data.get("kind"))
            except ValueError as error:
                raise HTTPError(409, str(error)) from None
            if data.get("kind") == "received":
                return {"ok": True, "expires_in": max(0, remote.jobs[data["id"]]["expires_at"] - time.time())},
            return {"ok": True},
        if path == "/api/devices/leave":
            remote = self.remotes.get(data.get("name"))
            if remote is None:
                raise HTTPError(404, "No such computer")
            self.device_auth(remote, headers)
            remote.leave()
            self.publish("devices", engine.machine.listing())
            return {"ok": True},
        if path == "/api/import":
            keys = data.get("keys")
            if not isinstance(keys, list) or not keys or not all(k in self.found for k in keys):
                raise HTTPError(400, "Choose sessions from the last scan")
            return {"imported": engine.import_chats([self.found[k] for k in keys])},
        raise HTTPError(404, "Not found")

    # Past chats --------------------------------------------------------------------

    async def scan(self, query):
        """Past chats on this computer with something new, read off the event loop."""
        since = query.get("since", "")
        if since and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
            raise HTTPError(400, "since takes a day as YYYY-MM-DD")
        paths = [p for p in query.get("paths", "").split("\n") if p.strip()] or None
        sessions = await asyncio.to_thread(history.find, paths, since=since, tools=query.get("tools") == "1",
                                           done=self.engine.imported())
        self.found = {s.key: s for s in sessions}
        return {"sessions": [{"key": s.key, "app": s.app, "where": s.where, "start": s.start,
                              "messages": len(s.messages), "calls": s.calls} for s in sessions]}

    # Settings ----------------------------------------------------------------------

    def current(self):
        args = self.args
        subscription = args.connection == "subscription"
        vendor = getattr(args, "api_provider", "anthropic") or "anthropic"
        machine = self.engine.machine
        return {"connection": args.connection, "cli": args.cli, "model": args.model or "",
                "summary_model": args.summary_model or "", "reasoning_effort": args.reasoning_effort or "",
                "api_provider": vendor, "agent_model": getattr(args, "agent_model", None) or "",
                "api_speed": getattr(args, "api_speed", None) or "", "service_tier": getattr(args, "service_tier", None) or "",
                "after_turn": getattr(args, "after_turn", None) or "",
                "tools": getattr(args, "tools", "off"), "work": str(machine.work) if machine else "",
                "computer": machine.name if machine else "",
                "demo": bool(args.demo), "chat": args.data_dir,
                "model_default": "the CLI's default" if subscription else OPENAI_MODEL if vendor == "openai" else Config.turn_model,
                "summary_default": "the same as replies" if subscription or vendor == "openai" else Config.summary_model}

    def state(self):
        listing = getattr(self.engine.machine, "listing", None)
        return {"settings": self.current(), "status": self.engine.status(), "version": __version__,
                "agents": self.engine.agent_jobs(), "view_limit": self.engine.config.view_high,
                "computers": listing() if listing else [],
                "push": self.push.status(),
                "audio": {"configured": self.transcriber.configured() and not self.args.demo,
                          "provider": "openai", "model": "gpt-transcribe", "max_bytes": MAX_AUDIO}}

    def expire_push(self, identity, subscription):
        if (identity, subscription) in self.sessions.recipients():
            self.sessions.subscription(identity, None)

    def device_auth(self, remote, headers):
        if remote.identity:
            headers = headers or {}
            try:
                self.engine.authenticate_device(remote.name, headers.get("x-eternalchat-device"),
                                                headers.get("x-eternalchat-device-token"))
            except ValueError:
                raise HTTPError(403, "Device enrollment does not match") from None

    async def device_jobs(self, writer, name, headers=None):
        """A joined computer's jobs, as they come, while it stays connected."""
        remote = self.remotes.get(name)
        if remote is None:
            writer.write(reply(404, {"error": "Join first: POST /api/devices/hello"}))
            return
        self.device_auth(remote, headers)
        epoch, queue = remote.attach() if remote.identity else (remote.epoch, remote.outbox)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\n"
                     b"Connection: close\r\nX-Content-Type-Options: nosniff\r\n\r\n")
        try:
            while not remote.gone and remote.epoch == epoch:
                try:
                    item = await asyncio.wait_for(queue.get(), PING)
                    writer.write(b"data: " + json.dumps(item, ensure_ascii=False).encode() + b"\n\n")
                except TimeoutError:
                    writer.write(b": ping\n\n")
                await writer.drain()
        finally:
            machine = self.engine.machine
            if remote.identity:
                remote.detach(epoch)
                if hasattr(machine, "listing"):
                    self.publish("devices", machine.listing())
            elif not remote.gone and hasattr(machine, "leave"):
                machine.leave(remote)  # what it was running fails now, never waits for good
                self.publish("devices", machine.listing())

    async def switch(self, data):
        """Reopen the chat on another connection or model, and save the choice."""
        changes = {}
        for key in ("connection", "cli", "model", "summary_model", "agent_model", "reasoning_effort", "tools", "work",
                    "api_provider", "api_speed", "service_tier", "after_turn"):
            if key in data:
                if not isinstance(data[key], str):
                    raise HTTPError(400, f"{key} must be text")
                changes[key] = data[key].strip() or None
        connection = changes.get("connection", self.args.connection)
        vendor = changes.get("api_provider", getattr(self.args, "api_provider", "anthropic")) or "anthropic"
        if connection == "subscription":
            changes.update(api_speed=None, service_tier=None)
        elif vendor == "openai":
            changes["api_speed"] = None
        else:
            changes["service_tier"] = None
        if changes.get("tools") not in (None, "auto", "off"):
            raise HTTPError(400, "tools must be auto or off")
        status = self.engine.status()
        if self.imports.running:
            raise HTTPError(409, "Wait for the import or cancel it before switching settings")
        if status["replying"] or status["agents_running"]:
            raise HTTPError(409, "Wait for the reply and agents to finish, or stop them, before switching")
        options = Namespace(**{**vars(self.args), **changes})
        await self.engine.close()
        try:
            self.engine = self._open(options)
        except Exception as error:
            self.engine = self._open(self.args)  # the chat stays served as it was
            raise HTTPError(400, f"Could not switch: {error}") from None
        self.args = options
        try:
            saved = settings.load()
        except ValueError:
            saved = {}
        settings.save({**saved, **changes})
        self.publish("settings", self.current())
        return self.state()


async def device(url, name, work, out):
    """Lend this computer with durable enrollment and replay-safe job receipts."""
    from .device_client import DeviceClient
    machine = Machine(work, name=name)
    try:
        client = DeviceClient(url, machine, out)
    except BaseException:
        machine.close()
        raise
    await client.run()


async def serve(args, open_engine, out):
    """Serve the chat until interrupted. open_engine(options, on_event) opens it."""
    chat = WebChat(args, open_engine, out, web_token())
    host, allow = getattr(args, "host", "127.0.0.1"), getattr(args, "allow_host", None) or []
    try:
        url = await chat.start(args.port, host, allow)
    except OSError as error:
        await chat.close()
        raise RuntimeError(f"Cannot listen on {host}:{args.port} ({error.strerror or error}); "
                           "another program may be using it: choose another with --port") from None
    links = [f"https://{name}/#{chat.token}  (through its HTTPS proxy)" for name in allow if ":" not in name]
    if chat.lan:
        links.append(f"http://{chat.lan}:{chat.port}/#{chat.token}  (other devices on this network; plain HTTP)")
    also = "".join(f"\n  also {link}" for link in links)
    out.line(f"EternalChat is open at {url}{also}\n"
             "The link holds your secret token: keep it private. Ctrl-C stops; the chat stays saved.")
    if not args.no_browser:
        webbrowser.open(url)  # ubs:ignore — opens the requested UI, not a file descriptor.
    try:
        await chat.server.serve_forever()
    finally:  # ubs:ignore — cleanup has no return/break; server resources are awaited.
        await chat.close()
    return 0
