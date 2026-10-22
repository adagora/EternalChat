"""A terminal view of a server-owned chat; never opens a store or model client.

Only GET event streams reconnect automatically. A failed POST is never replayed:
the server may have accepted it even if its acknowledgement was lost.
"""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import shlex
import ssl
from urllib.parse import urlencode, urlsplit, unquote

from .config import MAX_IMAGE_BYTES

MAX_RESPONSE = 16_000_000


class RemoteError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Body:
    """Bounded HTTP/1.1 body reader, including reverse proxies' chunked SSE."""

    def __init__(self, reader, headers):
        self.reader = reader
        self.chunked = headers.get("transfer-encoding", "").lower() == "chunked"
        encoding = headers.get("transfer-encoding", "")
        if encoding and not self.chunked:
            raise RemoteError("Unsupported server transfer encoding")
        self.remaining = int(headers["content-length"]) if "content-length" in headers else None
        if self.remaining is not None and self.remaining < 0:
            raise RemoteError("Invalid server response length")
        self.chunk = 0
        self.done = False

    async def read(self):
        if self.done:
            return b""
        if self.chunked:
            if not self.chunk:
                line = await self.reader.readline()
                try:
                    self.chunk = int(line.split(b";", 1)[0], 16)
                except ValueError:
                    raise RemoteError("Invalid server chunk") from None
                if self.chunk < 0:
                    raise RemoteError("Invalid server chunk")
                if not self.chunk:
                    self.done = True
                    return b""
            data = await self.reader.readexactly(min(self.chunk, 65536))
            self.chunk -= len(data)
            if not self.chunk and await self.reader.readexactly(2) != b"\r\n":
                raise RemoteError("Invalid server chunk ending")
            return data
        if self.remaining == 0:
            return b""
        data = await self.reader.read(min(self.remaining, 65536) if self.remaining is not None else 65536)
        if self.remaining is not None:
            if not data and self.remaining:
                raise ConnectionError("Incomplete server response")
            self.remaining -= len(data)
        return data


class Client:
    def __init__(self, url):
        parts = urlsplit(url)
        token = unquote(parts.fragment)
        if (parts.scheme not in ("http", "https") or not parts.hostname or not token
                or parts.username is not None or parts.password is not None
                or parts.path not in ("", "/") or parts.query):
            raise RemoteError("Use the server's full http(s) link, including #token")
        if not token.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in token):
            raise RemoteError("Invalid server token")
        self.host, self.port = parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
        self.authority = parts.netloc
        if any(ord(c) <= 32 or ord(c) == 127 for c in self.authority):
            raise RemoteError("Invalid server address")
        self.tls = ssl.create_default_context() if parts.scheme == "https" else None
        self.token = token
        self.extra_headers = {}

    @asynccontextmanager
    async def response(self, method, path, data=b""):
        writer = None
        try:
            async with asyncio.timeout(30):
                reader, writer = await asyncio.open_connection(self.host, self.port, ssl=self.tls,
                                                               limit=65536)
                head = (f"{method} {path} HTTP/1.1\r\nHost: {self.authority}\r\n"
                        f"X-EternalChat-Token: {self.token}\r\nContent-Type: application/json\r\n"
                        + "".join(f"{key}: {value}\r\n" for key, value in self.extra_headers.items()) +
                        f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n")
                writer.write(head.encode("ascii") + data)
                await writer.drain()
                raw = await reader.readuntil(b"\r\n\r\n")
            lines = raw.decode("latin-1").split("\r\n")
            status = int(lines[0].split()[1])
            headers = {k.lower(): v.strip() for line in lines[1:] if ":" in line
                       for k, v in [line.split(":", 1)]}
            # In particular, never forward the secret to a redirect target.
            if status in (401, 403):
                raise RemoteError("Server refused access; check the link token and allowed host", status)
            if 300 <= status < 400:
                raise RemoteError("Server redirected the request; use its direct chat link")
            body = Body(reader, headers)
            if status != 200:
                raise RemoteError(f"Server rejected the request (HTTP {status})", status)
            yield body
        finally:
            if writer is not None:
                writer.close()
                # Closing the transport also interrupts a pending stream read.

    async def request(self, method, path, value=None, *, raw=None):
        data = raw if raw is not None else json.dumps(value).encode() if value is not None else b""
        async with asyncio.timeout(30):
            async with self.response(method, path, data) as body:
                result = bytearray()
                while chunk := await body.read():
                    result.extend(chunk)
                    if len(result) > MAX_RESPONSE:
                        raise RemoteError("Server response exceeds the terminal's size limit")
                return json.loads(result)

    async def events(self, path="/api/events?snapshot=1"):
        async with self.response("GET", path) as body:
            pending = b""
            while True:
                async with asyncio.timeout(50):
                    chunk = await body.read()
                if not chunk:
                    raise ConnectionError("Server event stream closed")
                pending += chunk
                if len(pending) > MAX_RESPONSE:
                    raise RemoteError("Server event exceeds the terminal's size limit")
                while b"\n\n" in pending:
                    frame, pending = pending.split(b"\n\n", 1)
                    data = [line[6:] for line in frame.splitlines() if line.startswith(b"data: ")]
                    if data:
                        yield json.loads(b"\n".join(data))


HELP = """Type to send; // sends a leading slash.
/history [FROM]    saved messages (up to 200)
/status           server status
/settings [JSON]   inspect or change server settings, e.g. /settings {"model":"..."}
/view             memory view
/zoom ID [N] [PAGE] or /zoom NAME [PAGE]
/date ID          message timestamp
/attach PATH      upload an image for your next message
/note TEXT        save a note
/agent NAME TASK  start an agent
/stop [NAME]      stop the reply or a named agent
/goal TEXT        set a goal
/halt             drop the goal
/compact          retry summaries
/quit             detach; server work continues"""


class RemoteSession:
    def __init__(self, client, out):
        self.client, self.out = client, out
        self.images = []
        self.cursor = 0
        self.streamed = False
        self.ready = asyncio.Event()
        self.online = False

    def show(self, messages, *, catchup=False):
        for message in messages:
            kind = message["kind"]
            if kind == "unii" and self.streamed and not catchup:
                self.out.end_line()
                self.streamed = False
            elif kind in ("user", "unii", "note", "work"):
                self.out.line(f"[{message['i']} {kind}] {message['text']}")
            self.cursor = max(self.cursor, message["i"] + 1)

    async def watch(self):
        delay = 1
        while True:
            try:
                async for event in self.client.events():
                    kind, value = event["kind"], event["value"]
                    if kind == "snapshot":
                        self.streamed = False
                        messages = [m for m in value["messages"] if m["i"] >= self.cursor]
                        if messages and messages[0]["i"] > self.cursor:
                            self.out.line("Showing recent history; /history FROM opens older messages.")
                        self.show(messages, catchup=True)
                        self.cursor = value["status"]["messages"]
                        self.out.line("Connected. /help lists commands; /quit detaches.")
                        if value.get("partial"):
                            self.out.event("text", value["partial"])
                            self.streamed = True
                        self.online = True
                        self.ready.set()
                        delay = 1
                    elif kind == "message":
                        # Fetch once per notification, advancing over every returned
                        # record (including tool records). Inputs are never replayed.
                        stop = max(value["ids"]) + 1
                        while self.cursor < stop:
                            result = await self.client.request("GET", "/api/messages?" + urlencode(
                                {"from": self.cursor, "limit": min(200, stop - self.cursor)}))
                            self.show(result["messages"])
                            if not result["messages"]:
                                break
                    elif kind == "text":
                        self.streamed = True
                        self.out.event(kind, value)
                    elif kind == "settings":
                        self.out.line("Server settings changed:")
                        self.out.json(value)
                    else:
                        self.out.event(kind, value)
            except (OSError, TimeoutError, asyncio.IncompleteReadError):
                self.online = False
                self.out.line("Connection lost; reconnecting. Submitted messages will not be resent.", err=True)
                await asyncio.sleep(delay)
                delay = min(10, delay * 2)

    async def handle(self, line):
        from .cli import zoom_request
        if not line.strip():
            return
        if line in ("/quit", "/exit"):
            return False
        if line == "/help":
            self.out.line(HELP)
            return
        if not self.online:
            raise RemoteError("Disconnected; wait for reconnection before submitting work")
        command, _, rest = line.partition(" ")
        rest = rest.strip()
        if not line.startswith("/") or line.startswith("//"):
            await self.client.request("POST", "/api/send", {
                "text": line[1:] if line.startswith("//") else line, "attachments": self.images})
            self.images = []
        elif command == "/attach":
            parts = shlex.split(rest)
            if len(parts) != 1:
                raise ValueError("Usage: /attach PATH")
            with Path(parts[0]).expanduser().open("rb") as source:
                data = source.read(MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError("Image exceeds the 5 MB limit")
            result = await self.client.request("POST", "/api/attach", raw=data)
            self.images.append(result["ref"])
            self.out.line("Image attached to your next message.")
        elif command == "/zoom":
            parts = shlex.split(rest)
            if not 1 <= len(parts) <= 3:
                raise ValueError("Usage: /zoom ID [N] [PAGE] or /zoom NAME [PAGE]")
            numeric = parts[0].isascii() and parts[0].isdigit()
            if not numeric and len(parts) > 2:
                raise ValueError("Usage: /zoom NAME [PAGE]")
            request = zoom_request(parts[0], int(parts[1]) if numeric and len(parts) > 1 else None,
                                   int(parts[2]) if numeric and len(parts) > 2 else
                                   int(parts[1]) if not numeric and len(parts) > 1 else 0)
            self.out.json(await self.client.request("GET", "/api/zoom?" + urlencode(request)))
        elif command == "/history":
            start = int(rest) if rest else max(0, self.cursor - 200)
            if start < 0:
                raise ValueError("History starts at a nonnegative message ID")
            result = await self.client.request("GET", "/api/messages?" + urlencode({"from": start}))
            for message in result["messages"]:
                self.out.line(f"[{message['i']} {message['kind']}] {message['text']}")
        elif command == "/settings":
            if rest:
                changes = json.loads(rest)
                if not isinstance(changes, dict) or not changes or set(changes) - {
                        "connection", "cli", "model", "summary_model", "agent_model", "reasoning_effort", "tools", "work",
                        "api_provider", "api_speed", "service_tier", "after_turn"}:
                    raise ValueError("Supply a JSON object of server settings")
                result = await self.client.request("POST", "/api/settings", changes)
            else:
                result = await self.client.request("GET", "/api/state")
            self.out.json(result["settings"])
        elif command in ("/note", "/goal", "/agent"):
            if not rest:
                raise ValueError(f"{command} needs text")
            data = {"text": rest}
            if command == "/agent":
                name, _, task = rest.partition(" ")
                if not task.strip():
                    raise ValueError("Usage: /agent NAME TASK")
                data = {"name": name, "task": task.strip()}
            await self.client.request("POST", "/api" + command, data)
        elif command == "/stop":
            parts = shlex.split(rest)
            if len(parts) > 1:
                raise ValueError("Usage: /stop [NAME]")
            await self.client.request("POST", "/api/stop", {"name": parts[0]} if parts else {})
        elif command == "/date":
            self.out.json(await self.client.request("GET", "/api/date?" + urlencode({"id": int(rest)})))
        elif command in ("/halt", "/compact", "/view", "/status"):
            if rest:
                raise ValueError(f"{command} takes no arguments")
            if command == "/status":
                self.out.json((await self.client.request("GET", "/api/state"))["status"])
            elif command == "/view":
                self.out.json(await self.client.request("GET", "/api/view"))
            else:
                await self.client.request("POST", "/api" + command, {})
        else:
            raise ValueError("Unknown command; /help lists commands")

    async def run(self, source):
        watcher = asyncio.create_task(self.watch())

        async def interact():
            await self.ready.wait()
            async for line in source:
                try:
                    if await self.handle(line) is False:
                        return
                except (OSError, TimeoutError, asyncio.IncompleteReadError):
                    self.out.line("Request lost its connection; acceptance is unknown. Check history before resending.",
                                  err=True)
                except (ValueError, RuntimeError) as error:
                    self.out.line(f"Error: {error}", err=True)

        inputs = asyncio.create_task(interact())
        try:
            done, _ = await asyncio.wait((watcher, inputs), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        finally:
            watcher.cancel()
            inputs.cancel()
            await asyncio.gather(watcher, inputs, return_exceptions=True)
            self.out.end_line()
            if self.images:
                self.out.line(f"Discarded {len(self.images)} unsent image attachment(s).", err=True)


async def connect(url, out):
    from .cli import stdin_lines
    await RemoteSession(Client(url), out).run(stdin_lines())
    return 0
