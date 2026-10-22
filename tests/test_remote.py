"""Real HTTP acceptance of terminal views sharing a single locked server store."""
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from eternalchat.cli import parser, run
from eternalchat.remote import Body, Client, RemoteError, RemoteSession
from eternalchat.config import Config
from eternalchat.web import WebChat
from tests.test_engine import Scripted, text_response
from tests.test_web import WebFixture


class Output:
    def __init__(self):
        self.lines, self.events = [], []

    def line(self, value, **kwargs):
        self.lines.append(str(value))

    def json(self, value):
        self.line(json.dumps(value))

    def event(self, kind, value):
        self.events.append((kind, value))

    def end_line(self):
        pass


class RemoteTests(WebFixture):
    async def client(self):
        await self.serve()
        return Client(f"http://127.0.0.1:{self.port}/#{'t' * 43}")

    async def watching(self, client):
        out = Output()
        session = RemoteSession(client, out)
        task = asyncio.create_task(session.watch())
        self.addAsyncCleanup(self.cancel, task)
        await asyncio.wait_for(session.ready.wait(), 3)
        return session, out, task

    async def cancel(self, task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def wait_for(self, check):
        async with asyncio.timeout(5):
            while not check():
                await asyncio.sleep(0.01)

    async def test_two_views_share_history_streams_and_never_open_local_store(self):
        client = await self.client()
        first, out1, _ = await self.watching(client)
        second, out2, _ = await self.watching(client)
        with patch("eternalchat.cli.open_from", side_effect=AssertionError("local store opened")):
            with patch("eternalchat.remote.connect", return_value=0) as connected:
                self.assertEqual(await run(parser().parse_args(["connect",
                    f"http://127.0.0.1:{self.port}/#{'t' * 43}"])), 0)
                connected.assert_awaited_once()
        await first.handle("Remember amber-42.")
        await self.wait_for(lambda: first.cursor >= 2 and second.cursor >= 2)
        for out in (out1, out2):
            self.assertTrue(any("Remember amber-42." in line for line in out.lines))
            text = "".join(v for k, v in out.events if k == "text")
            self.assertTrue(text)
            # Saved replies are not printed a second time after their deltas.
            self.assertFalse(any(" unii]" in line for line in out.lines))
        await self.request("POST", "/api/send", {"text": "hello from browser"})
        await self.wait_for(lambda: first.cursor >= 4 and second.cursor >= 4)
        self.assertTrue(any("hello from browser" in line for line in out1.lines))
        await second.handle("/view")
        await second.handle("/zoom 0")
        await second.handle("/date 0")
        await second.handle("/status")
        await second.handle("/settings")
        await second.handle("/history 0")
        self.assertTrue(any("amber-42" in line for line in out2.lines))

    async def test_reconnect_catches_saved_messages_without_resending_input(self):
        client = await self.client()
        session, out, task = await self.watching(client)
        await session.handle("Remember amber-42.")
        await self.wait_for(lambda: session.cursor >= 2)
        await self.cancel(task)
        await self.request("POST", "/api/note", {"text": "while disconnected"})
        task = asyncio.create_task(session.watch())
        self.addAsyncCleanup(self.cancel, task)
        await self.wait_for(lambda: session.cursor == 3)
        self.assertEqual(self.chat.engine.status()["messages"], 3)
        self.assertEqual(sum("while disconnected" in line for line in out.lines), 1)
        self.assertEqual(sum("Remember amber-42." in line for line in out.lines), 1)

    async def test_snapshot_contains_partial_reply_and_settings_reopen_keeps_client(self):
        client = await self.client()
        self.chat.publish("text", "partial reply")
        session, out, _ = await self.watching(client)
        self.assertIn(("text", "partial reply"), out.events)
        await self.chat.engine.finish()
        with patch("eternalchat.web.settings.save"), patch("eternalchat.web.settings.load", return_value={}):
            await session.handle('/settings {"model":"changed"}')
        await session.handle("after switch")
        await self.wait_for(lambda: session.cursor >= 2)
        self.assertEqual(self.chat.args.model, "changed")
        self.assertEqual(len(self.opened), 2)

    async def test_auth_failure_is_fatal_and_does_not_display_secret(self):
        await self.client()
        secret = "wrong-secret"
        client = Client(f"http://127.0.0.1:{self.port}/#{secret}")
        with self.assertRaises(RemoteError) as caught:
            await client.request("GET", "/api/state")
        self.assertNotIn(secret, str(caught.exception))
        session = RemoteSession(client, Output())

        async def no_input():
            yield "/quit"

        with self.assertRaises(RemoteError):
            await asyncio.wait_for(session.run(no_input()), 2)

    async def test_quit_and_cancel_detach_without_stopping_server(self):
        client = await self.client()
        session = RemoteSession(client, Output())

        async def quit_input():
            yield "/quit"

        await asyncio.wait_for(session.run(quit_input()), 2)
        self.assertFalse(self.chat.engine.closed)

        async def blocked_input():
            await asyncio.Event().wait()
            yield "never"

        task = asyncio.create_task(session.run(blocked_input()))
        await asyncio.sleep(0.05)
        await self.cancel(task)
        self.assertFalse(self.chat.engine.closed)
        self.assertEqual((await client.request("GET", "/api/state"))["status"]["messages"], 0)

    async def test_uncertain_post_is_not_replayed_or_cleared(self):
        client = await self.client()
        session = RemoteSession(client, Output())
        session.online = True
        session.images = [{"sha256": "x"}]
        with patch.object(client, "request", side_effect=ConnectionError("lost")) as request:
            with self.assertRaises(ConnectionError):
                await session.handle("one submission")
            self.assertEqual(request.call_count, 1)
        self.assertEqual(len(session.images), 1)

    async def test_steering_stop_goal_agents_and_detach_during_work(self):
        started = asyncio.Event()

        async def blocked(kw):
            if kw["model"] == Config.summary_model:
                return text_response("summary")
            started.set()
            await asyncio.Event().wait()

        await self.serve(Scripted(blocked))
        client = Client(f"http://127.0.0.1:{self.port}/#{'t' * 43}")
        session, _, task = await self.watching(client)
        await session.handle("original request")
        await asyncio.wait_for(started.wait(), 2)
        await self.request("POST", "/api/send", {"text": "browser correction"})
        await session.handle("terminal correction")
        self.assertEqual([m["text"] for m in self.chat.engine.messages()],
                         ["original request", "browser correction", "terminal correction"])
        await self.cancel(task)
        self.assertTrue(self.chat.engine.status()["replying"])
        await session.handle("/stop")
        await self.wait_for(lambda: not self.chat.engine.status()["replying"])
        await session.handle("/agent Scout inspect")
        await self.wait_for(lambda: self.chat.engine.status()["agents_running"] == 1)
        await session.handle("/stop Scout")
        await self.wait_for(lambda: self.chat.engine.status()["agents_running"] == 0)
        await session.handle("/goal finish this")
        self.assertEqual(self.chat.engine.status()["goal"], "finish this")
        await session.handle("/halt")
        self.assertIsNone(self.chat.engine.status()["goal"])
        await session.handle("/stop")
        await session.handle("/compact")

    async def test_automatic_reconnect_after_server_restart(self):
        client = await self.client()
        with patch("eternalchat.web.PING", 0.03):
            session, out, _ = await self.watching(client)
            await session.handle("Remember amber-42.")
            await self.wait_for(lambda: session.cursor == 2)
            await self.chat.engine.finish()
            await self.chat.close()
            await self.chat.server.wait_closed()
            restarted = WebChat(self.chat.args, self.chat.open_engine, Output(), "t" * 43)
            await restarted.start(self.port)
            self.addAsyncCleanup(restarted.close)
            restarted.engine.note("saved after restart")
            await self.wait_for(lambda: session.cursor == 3)
            self.assertTrue(any("saved after restart" in line for line in out.lines))
            self.assertEqual([m["text"] for m in restarted.engine.messages() if m["kind"] == "user"],
                             ["Remember amber-42."])

    async def test_chunked_events_and_refused_redirect(self):
        reader = asyncio.StreamReader()
        reader.feed_data(b"3\r\nabc\r\n2;ext=yes\r\nde\r\n0\r\n\r\n")
        reader.feed_eof()
        body = Body(reader, {"transfer-encoding": "chunked"})
        self.assertEqual(await body.read(), b"abc")
        self.assertEqual(await body.read(), b"de")
        self.assertEqual(await body.read(), b"")

        async def redirect(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 302 Found\r\nLocation: https://example.org/\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(redirect, "127.0.0.1", 0)
        self.addCleanup(server.close)
        port = server.sockets[0].getsockname()[1]
        with self.assertRaisesRegex(RemoteError, "redirected"):
            await Client(f"http://127.0.0.1:{port}/#secret").request("GET", "/api/state")

    async def test_reject_invalid_link_and_commands_before_request(self):
        for url in ("http://localhost/", "http://user:pass@localhost/#s",
                    "http://localhost/#abc%0d%0aInjected", "https://localhost/other#s"):
            with self.subTest(url=url), self.assertRaises(RemoteError):
                Client(url)
        client = await self.client()
        session, _, _ = await self.watching(client)
        for command in ("/stop a b", "/halt extra", "/settings []", "/settings {\"unknown\":1}",
                        "/agent name", "/history -1", "/unknown"):
            with self.subTest(command=command), self.assertRaises((ValueError, RemoteError)):
                await session.handle(command)
