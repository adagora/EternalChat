"""The local web interface: its guards, and the chat through its API."""
import asyncio
import json
import socket
from argparse import Namespace
from pathlib import Path
import unittest

from eternalchat import settings
from eternalchat.app import open_chat
from eternalchat.config import Config
from eternalchat.demo import DemoProvider
from eternalchat.llm import Response
from eternalchat.web import WebChat
from tests.test_engine import EngineFixture, Scripted, text_response

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 100


class Quiet:
    def __init__(self):
        self.lines = []

    def line(self, text, err=False):
        self.lines.append(text)


class WebFixture(EngineFixture):
    """A served chat and raw HTTP requests to it, for tests of the page's API."""
    async def serve(self, provider=None, args=None, **start):
        self.opened = []

        def open_engine(options, on_event):
            self.opened.append(options)
            engine = open_chat(Path(self.temp.name, "chat"), provider or DemoProvider(), Config(), on_event)
            self.engines.append(engine)
            return engine

        args = args or Namespace(connection="api", cli="codex", model=None, summary_model=None, demo=True,
                                 data_dir=str(Path(self.temp.name, "chat")), reasoning_effort=None)
        self.chat = WebChat(args, open_engine, Quiet(), "t" * 43)
        url = await self.chat.start(0, **start)
        self.port = int(url.split(":")[2].split("/")[0])
        self.addAsyncCleanup(self.chat.close)
        return self.chat

    async def request(self, method, path, body=None, *, token="t" * 43, host=None, content_type="application/json"):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        data = body if isinstance(body, bytes) else b"" if body is None else json.dumps(body).encode()
        head = [f"{method} {path} HTTP/1.1", f"Host: {host or f'127.0.0.1:{self.port}'}",
                f"Content-Length: {len(data)}", f"Content-Type: {content_type}"]
        if token:
            head.append(f"X-EternalChat-Token: {token}")
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + data)
        raw = await reader.read()
        writer.close()
        status_line, _, rest = raw.partition(b"\r\n")
        headers, _, payload = rest.partition(b"\r\n\r\n")
        status = int(status_line.split()[1])
        kind = next((line.split(b":", 1)[1].strip().decode() for line in headers.split(b"\r\n")
                     if line.lower().startswith(b"content-type:")), "")
        return status, (json.loads(payload) if kind == "application/json" else payload), headers.decode()

    async def until(self, check, timeout=5):
        async with asyncio.timeout(timeout):
            while not await check():
                await asyncio.sleep(0.02)


class WebTests(WebFixture):
    async def test_page_is_open_but_the_api_needs_token_and_host(self):
        await self.serve()
        status, body, headers = await self.request("GET", "/", token=None)
        self.assertEqual(status, 200)
        self.assertIn(b"EternalChat", body)
        self.assertIn("frame-ancestors 'none'", headers)
        for kwargs in ({"token": None}, {"token": "wrong"}, {"host": "evil.example"}):
            with self.subTest(**kwargs):
                status, body, _ = await self.request("GET", "/api/state", **kwargs)
                self.assertEqual(status, 403)
        status, _, _ = await self.request("GET", "/", host=f"rebound.example:{self.port}")
        self.assertEqual(status, 403)
        self.assertEqual((await self.request("GET", "/nothing"))[0], 404)

    async def test_send_reply_events_view_and_zoom(self):
        await self.serve()
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write((f"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                      f"X-EternalChat-Token: {'t' * 43}\r\n\r\n").encode())
        await reader.readuntil(b"\r\n\r\n")
        await asyncio.sleep(0.05)  # the stream is registered before anything is sent
        status, body, _ = await self.request("POST", "/api/send", {"text": "Remember amber-42."})
        self.assertEqual((status, body), (200, {"ok": True}))
        kinds = []
        async with asyncio.timeout(5):
            while "message" not in kinds or "text" not in kinds:
                frame = await reader.readuntil(b"\n\n")
                if frame.startswith(b"data: "):
                    kinds.append(json.loads(frame[6:])["kind"])
        writer.close()

        async def replied():
            return len((await self.request("GET", "/api/messages?from=0"))[1]["messages"]) == 2
        await self.until(replied)
        messages = (await self.request("GET", "/api/messages?from=0"))[1]["messages"]
        self.assertEqual([m["kind"] for m in messages], ["user", "unii"])
        await self.request("POST", "/api/note", {"text": "x" * 600})

        async def summarized():
            return (await self.request("GET", "/api/state"))[1]["status"]["summarized_messages"] == 3
        await self.until(summarized)
        lines = (await self.request("GET", "/api/view"))[1]["lines"]
        self.assertEqual([(line["id"], line["n"]) for line in lines], [(0, 1), (1, 1), (2, 1)])
        found = (await self.request("GET", "/api/zoom?id=0&n=2"))[1]
        self.assertEqual([(line["id"], line["n"]) for line in found["lines"]], [(0, 1), (1, 1)])
        found = (await self.request("GET", "/api/zoom?id=0&n=1"))[1]
        self.assertIn("Remember amber-42.", found["text"])
        self.assertEqual(found["lines"], [])
        status, body, _ = await self.request("GET", "/api/zoom?id=1&n=2")
        self.assertEqual(status, 400)  # not a node: the error is the engine's own

    async def test_images_go_up_and_come_back(self):
        await self.serve()
        status, body, _ = await self.request("POST", "/api/attach", PNG, content_type="image/png")
        self.assertEqual(status, 200)
        ref = body["ref"]
        await self.request("POST", "/api/send", {"text": "Look", "attachments": [ref]})
        status, data, headers = await self.request("GET", f"/api/image/{ref['sha256']}?type=image/png")
        self.assertEqual((status, data), (200, PNG))
        self.assertIn("Content-Type: image/png", headers)
        message = (await self.request("GET", "/api/messages?from=0"))[1]["messages"][0]
        self.assertEqual(message["attachments"], [ref])
        status, _, _ = await self.request("POST", "/api/attach", b"not an image", content_type="image/png")
        self.assertEqual(status, 400)
        status, _, _ = await self.request("POST", "/api/send", {"text": "x", "attachments": [{"sha256": "0" * 64,
                                                                                         "media_type": "image/png"}]})
        self.assertEqual(status, 400)

    async def test_stop_ends_a_running_reply_and_keeps_its_text(self):
        release = asyncio.Event()

        async def slow(kw):
            if kw["messages"][0]["content"][-1]["text"].startswith("Compaction:"):
                return text_response("summary")
            kw["on_event"]("text", "partial answer")
            await release.wait()
            return text_response("never")

        chat = await self.serve(Scripted(slow))
        await self.request("POST", "/api/send", {"text": "Think long"})

        async def replying():
            return (await self.request("GET", "/api/state"))[1]["status"]["replying"]
        await self.until(replying)
        status, body, _ = await self.request("POST", "/api/stop", {})
        self.assertEqual(status, 200)

        async def stopped():
            return not await replying()
        await self.until(stopped)
        messages = (await self.request("GET", "/api/messages?from=0"))[1]["messages"]
        self.assertEqual(messages[-1]["kind"], "unii")
        self.assertIn("interrupted", messages[-1]["text"])
        self.assertIn("partial answer", messages[-1]["text"])
        self.assertTrue(chat.engine.status()["messages"] >= 2)

    async def test_switching_connection_reopens_the_chat_and_saves_the_choice(self):
        chat = await self.serve()
        await self.request("POST", "/api/note", {"text": "kept"})
        first = chat.engine
        status, body, _ = await self.request("POST", "/api/settings",
                                             {"connection": "subscription", "cli": "claudecode", "model": "",
                                              "summary_model": "haiku"})
        self.assertEqual(status, 200)
        self.assertEqual(body["settings"]["connection"], "subscription")
        self.assertTrue(first.closed)
        self.assertIsNot(chat.engine, first)
        self.assertEqual((self.opened[-1].cli, self.opened[-1].summary_model), ("claudecode", "haiku"))
        self.assertEqual(settings.load()["summary_model"], "haiku")
        self.assertNotIn("model", settings.load())
        messages = (await self.request("GET", "/api/messages?from=0"))[1]["messages"]
        self.assertEqual(messages[0]["text"], "kept")

    async def test_switching_waits_for_the_reply(self):
        release = asyncio.Event()

        async def slow(kw):
            if kw["messages"][0]["content"][-1]["text"].startswith("Compaction:"):
                return text_response("summary")
            await release.wait()
            return Response([{"type": "text", "text": "done"}])

        chat = await self.serve(Scripted(slow))
        await self.request("POST", "/api/send", {"text": "Think"})
        await self.until(lambda: asyncio.sleep(0, chat.engine.status()["replying"]))
        status, body, _ = await self.request("POST", "/api/settings", {"connection": "api"})
        self.assertEqual(status, 409)
        release.set()

    async def test_the_app_files_are_public_and_the_page_may_use_them(self):
        await self.serve()
        for path, kind in (("/manifest.webmanifest", "application/manifest+json"), ("/sw.js", "text/javascript"),
                           ("/icon.svg", "image/svg+xml"), ("/icon-192.png", "image/png"),
                           ("/icon-512.png", "image/png"), ("/icon-maskable.png", "image/png"),
                           ("/apple-touch-icon.png", "image/png")):
            with self.subTest(path=path):
                status, body, headers = await self.request("GET", path, token=None)
                self.assertEqual(status, 200)
                self.assertIn(f"Content-Type: {kind}", headers)
                self.assertTrue(body)
        manifest = json.loads((await self.request("GET", "/manifest.webmanifest", token=None))[1])
        self.assertEqual(manifest["start_url"], "/")
        headers = (await self.request("GET", "/", token=None))[2]
        self.assertIn("manifest-src 'self'", headers)
        self.assertIn("worker-src 'self'", headers)
        status, _, _ = await self.request("GET", "/icon.svg", host=f"rebound.example:{self.port}")
        self.assertEqual(status, 403)

    async def test_other_devices_reach_it_only_by_names_it_was_given(self):
        chat = await self.serve(allow_hosts=["Mac.Tail1234.ts.net"])
        for host in ("mac.tail1234.ts.net", f"mac.tail1234.ts.net:{self.port}", f"localhost:{self.port}"):
            with self.subTest(host=host):
                self.assertEqual((await self.request("GET", "/api/state", host=host))[0], 200)
        for host in ("evil.ts.net", f"192.168.1.5:{self.port}", "mac.tail1234.ts.net.evil.example", ""):
            with self.subTest(host=host):
                self.assertEqual((await self.request("GET", "/api/state", host=host or " "))[0], 403)
        # Listening beyond this computer also admits IP addresses, which no attacker can rebind.
        chat.any_ip = True
        self.assertEqual((await self.request("GET", "/api/state", host=f"192.168.1.5:{self.port}"))[0], 200)
        self.assertEqual((await self.request("GET", "/api/state", host=f"[fe80::1]:{self.port}"))[0], 200)
        self.assertEqual((await self.request("GET", "/api/state", host="192.168.1.5:1"))[0], 403)
        self.assertEqual((await self.request("GET", "/api/state", host=f"rebound.example:{self.port}"))[0], 403)
        self.assertEqual((await self.request("GET", "/api/state", host="192.168.1.5:8765", token="wrong"))[0], 403)

    def test_this_computer_answers_to_its_network_names(self):
        from eternalchat.web import lan_names
        advertised, names = lan_names()
        here = socket.gethostname().lower()
        self.assertIn(here, names)
        self.assertIn(here.split(".")[0] + ".local", names)
        if advertised:
            self.assertTrue(advertised.endswith(".local"))
            self.assertEqual(names[0], advertised)

    async def test_agents_show_in_the_state_with_their_transcript(self):
        await self.serve()
        status, _, _ = await self.request("POST", "/api/agent", {"name": "Scout", "task": "Look around"})
        self.assertEqual(status, 200)

        async def reported():
            state = (await self.request("GET", "/api/state"))[1]
            return state["agents"] == {"Scout": False}
        await self.until(reported)
        status, log, _ = await self.request("GET", "/api/agent?name=Scout")
        self.assertEqual(status, 200)
        self.assertEqual((log["name"], log["status"], log["task"]), ("Scout", "complete", "Look around"))
        self.assertEqual(log["records"][0]["kind"], "user")
        self.assertIn("unii", [r["kind"] for r in log["records"]])
        for name in ("Nobody", "../etc"):
            with self.subTest(name=name):
                self.assertEqual((await self.request("GET", f"/api/agent?name={name}"))[0], 400)
        state = (await self.request("GET", "/api/state"))[1]
        self.assertEqual(state["view_limit"], Config().view_high)

    async def test_stop_can_target_a_named_agent_without_stopping_the_chat(self):
        entered = asyncio.Event()

        async def handler(kw):
            if kw["purpose"] == "agent":
                entered.set()
                await asyncio.Event().wait()
            return text_response("The chat still works")

        await self.serve(Scripted(handler))
        self.assertEqual((await self.request("POST", "/api/agent", {"name": "Worker", "task": "wait"}))[0], 200)
        await entered.wait()
        self.assertEqual((await self.request("POST", "/api/stop", {"name": "Worker"}, token=None))[0], 403)
        self.assertEqual((await self.request("POST", "/api/stop", {"name": "Worker"}))[0], 200)
        await asyncio.gather(self.chat.engine.agents["Worker"], return_exceptions=True)
        log = (await self.request("GET", "/api/agent?name=Worker"))[1]
        self.assertEqual(log["status"], "interrupted")
        self.assertEqual(await self.chat.engine.ask("hello"), "The chat still works")
        self.assertEqual((await self.request("POST", "/api/stop", {"name": 42}))[0], 400)

    async def test_switching_keeps_explicit_reasoning_effort_for_api(self):
        await self.serve()
        status, body, _ = await self.request("POST", "/api/settings", {"connection": "subscription", "cli": "codex",
                                                                       "reasoning_effort": "high"})
        self.assertEqual(status, 200)
        self.assertEqual(body["settings"]["reasoning_effort"], "high")
        self.assertEqual(self.opened[-1].reasoning_effort, "high")
        self.assertEqual(settings.load()["reasoning_effort"], "high")
        status, body, _ = await self.request("POST", "/api/settings", {"connection": "api", "reasoning_effort": "high"})
        self.assertEqual(status, 200)
        self.assertEqual(body["settings"]["reasoning_effort"], "high")
        self.assertEqual(settings.load()["reasoning_effort"], "high")
