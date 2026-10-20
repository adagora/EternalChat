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
import urllib.parse
import webbrowser
from argparse import Namespace
from importlib import resources
from pathlib import Path

from . import __version__, history, settings
from .config import MAX_IMAGE_BYTES, Config

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
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
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
    limit = MAX_IMAGE_BYTES if url.path == "/api/attach" else MAX_BODY
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
        self.found = {}  # past chats the last import scan found, by session key
        self.hosts = set()
        self.any_ip = False  # also admit Host names that are IP addresses (listening beyond loopback)
        self.lan = None  # this computer's advertised .local name, when listening beyond loopback
        self.server = None
        self.engine = self._open(args)

    def _open(self, args):
        engine = self.open_engine(args, self.publish)
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
        await self.engine.close()

    # Events ------------------------------------------------------------------------

    def publish(self, kind, value):
        """An engine event, to every open page. Problems also go to the terminal."""
        if kind in ("error", "compaction_error"):
            self.out.line(f"{kind}: {value}", err=True)
        event = json.dumps({"kind": kind, "value": value}, ensure_ascii=False, default=str)
        for queue in list(self.streams):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self.streams.discard(queue)  # far behind: its page reconnects and reloads

    async def stream(self, writer):
        queue = asyncio.Queue(BACKLOG)
        self.streams.add(queue)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\n"
                     b"Connection: close\r\nX-Content-Type-Options: nosniff\r\n\r\n")
        try:
            while queue in self.streams:
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
                elif not hmac.compare_digest(headers.get("x-eternalchat-token", "").encode(), self.token.encode()):
                    raise HTTPError(403, "Open the link that 'eternalchat web' printed")
                elif (method, path) == ("GET", "/api/events"):
                    return await self.stream(writer)
                else:
                    writer.write(reply(200, *await self.api(method, path, query, body)))
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
            writer.close()

    async def api(self, method, path, query, body):
        """(payload, content type) for an API call."""
        engine = self.engine
        if method == "GET":
            if path == "/api/state":
                return self.state(),
            if path == "/api/messages":
                start = int(query.get("from", 0))
                stop = start + max(1, min(int(query.get("limit", 200)), 500))
                return {"messages": engine.messages(start, stop)},
            if path == "/api/view":
                return {"lines": lines(engine.render_view())},
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
            if path.startswith("/api/image/"):
                ref = {"sha256": path.rsplit("/", 1)[1], "media_type": query.get("type", "")}
                return engine.image(ref), ref["media_type"]
            raise HTTPError(404, "Not found")
        if method != "POST":
            raise HTTPError(404, "Not found")
        if path == "/api/attach":
            with tempfile.TemporaryDirectory(prefix="eternalchat-upload-") as folder:
                upload = Path(folder, "image")
                upload.write_bytes(body)
                return {"ref": engine.attach(upload)},
        data = json.loads(body or b"{}")
        if not isinstance(data, dict):
            raise HTTPError(400, "Send a JSON object")
        if path == "/api/send":
            text, refs = data.get("text", ""), data.get("attachments", [])
            if not isinstance(text, str) or not isinstance(refs, list):
                raise HTTPError(400, "text must be a string and attachments a list")
            if not text.strip() and not refs:
                raise HTTPError(400, "Type a message or attach an image")
            engine.submit(text, attachments=refs).add_done_callback(_settled)
            return {"ok": True},
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
        machine = self.engine.machine
        return {"connection": args.connection, "cli": args.cli, "model": args.model or "",
                "summary_model": args.summary_model or "", "reasoning_effort": args.reasoning_effort or "",
                "tools": getattr(args, "tools", "off"), "work": str(machine.work) if machine else "",
                "computer": machine.name if machine else "",
                "demo": bool(args.demo), "chat": args.data_dir,
                "model_default": "the CLI's default" if subscription else Config.turn_model,
                "summary_default": "the same as replies" if subscription else Config.summary_model}

    def state(self):
        return {"settings": self.current(), "status": self.engine.status(), "version": __version__,
                "agents": self.engine.agent_jobs(), "view_limit": self.engine.config.view_high}

    async def switch(self, data):
        """Reopen the chat on another connection or model, and save the choice."""
        changes = {}
        for key in ("connection", "cli", "model", "summary_model", "reasoning_effort", "tools", "work"):
            if key in data:
                if not isinstance(data[key], str):
                    raise HTTPError(400, f"{key} must be text")
                changes[key] = data[key].strip() or None
        if changes.get("connection") == "api":
            changes["reasoning_effort"] = None  # an API connection takes none
        if changes.get("tools") not in (None, "auto", "off"):
            raise HTTPError(400, "tools must be auto or off")
        status = self.engine.status()
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
        webbrowser.open(url)
    try:
        await chat.server.serve_forever()
    finally:
        await chat.close()
    return 0
