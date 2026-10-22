"""Browser-local uploads, durable cancellation/restart and restorable backups."""
import asyncio
import io
import json
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode
import zipfile

from eternalchat.app import open_chat
from eternalchat.config import Config
from eternalchat.demo import DemoProvider
from eternalchat.history import Session
from eternalchat.remote import Client
from eternalchat.web import HTTPError
from tests.test_history import CLAUDE, CHATGPT, KEY
from tests.test_web import WebFixture, PNG


class UploadTests(WebFixture):
    async def batch(self, **options):
        status, result, _ = await self.request("POST", "/api/import/batch", options)
        self.assertEqual(status, 200)
        return result["id"]

    async def upload(self, identity, name, data, expected=200):
        status, result, _ = await self.request("POST", "/api/import/upload?" + urlencode(
            {"batch": identity, "name": name}), data)
        self.assertEqual(status, expected, result)
        return result

    async def finished(self, identity):
        async def done():
            result = (await self.request("GET", "/api/import/batch?" + urlencode({"batch": identity})))[1]
            return result["state"] != "running"
        await self.until(done)
        return self.chat.imports.status(identity)

    async def test_browser_bytes_preview_redaction_duplicate_files_and_import(self):
        await self.serve()
        identity = await self.batch()
        preview = await self.upload(identity, "laptop/projects/log.jsonl", CLAUDE.encode())
        self.assertEqual(preview["state"], "ready")
        self.assertEqual(len(preview["sessions"]), 1)
        self.assertEqual(self.chat.engine.status()["messages"], 0)
        duplicate = await self.upload(identity, "same-log.jsonl", CLAUDE.encode())
        self.assertEqual(duplicate["messages"], preview["messages"])
        keys = [s["key"] for s in preview["sessions"]]
        self.assertEqual((await self.request("POST", "/api/import/start", {"batch": identity, "keys": keys}))[0], 200)
        final = await self.finished(identity)
        self.assertEqual(final["state"], "completed")
        text = json.dumps(self.chat.engine.messages())
        self.assertIn("[redacted]", text)
        self.assertNotIn(KEY, text)
        self.assertNotIn("secret thoughts", text)
        self.assertIn("2026-10-01T10:00:00+00:00", text)
        count = self.chat.engine.status()["messages"]
        again = await self.batch()
        preview = await self.upload(again, "log.jsonl", CLAUDE.encode())
        self.assertEqual(preview["sessions"], [])
        self.assertEqual(self.chat.engine.status()["messages"], count)

    async def test_zip_and_json_exports_since_filter_and_auth(self):
        await self.serve()
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("../../conversations.json", json.dumps(CHATGPT))
        identity = await self.batch()
        preview = await self.upload(identity, "export.zip", archive.getvalue())
        self.assertEqual(preview["sessions"][0]["app"], "ChatGPT")
        self.assertFalse(Path(self.temp.name, "conversations.json").exists())
        # ZIP members are read in memory, never extracted, regardless of names.
        preview = await self.upload(identity, "export.json", json.dumps(CHATGPT).encode())
        self.assertEqual(len(preview["sessions"]), 1)
        for path in ("/api/import/batch", "/api/backup"):
            self.assertEqual((await self.request("GET", path, token="wrong"))[0], 403)
        self.assertEqual((await self.request("POST", "/api/import/upload", b"private", token=None))[0], 403)
        recent = await self.batch(since="2099-01-01")
        self.assertEqual((await self.upload(recent, "log.jsonl", CLAUDE.encode()))["sessions"], [])

    async def test_malformed_oversized_and_cancelled_uploads(self):
        await self.serve()
        identity = await self.batch()
        for name, data in (("bad.zip", b"bad"), ("bad.json", b"[1]"),
                           ("../secret.json", b"[]"), ("wrong.exe", b""),
                           ("bad.jsonl", b'{"type":"session_meta","payload":[1]}\n')):
            await self.upload(identity, name, data, expected=400)
        with patch("eternalchat.web_imports.MAX_UPLOAD", 4):
            await self.upload(identity, "big.json", b"12345", expected=400)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
            zipped.writestr("conversations.json", b" " * 100)
        from eternalchat import history
        original = history.read_export
        with patch("eternalchat.history.read_export", side_effect=lambda p, t: original(p, t, max_bytes=50)):
            await self.upload(identity, "too-large.zip", archive.getvalue(), expected=400)
        self.assertEqual((await self.request("POST", "/api/import/cancel", {"batch": identity}))[0], 200)
        await self.upload(identity, "log.jsonl", CLAUDE.encode(), expected=400)
        # Cancelled previews release their slot and do not leave the UI stuck.
        for _ in range(6):
            next_id = await self.batch()
            await self.request("POST", "/api/import/cancel", {"batch": next_id})

    async def test_cancel_running_import_then_restart_is_exactly_once(self):
        await self.serve()
        session = Session("Claude", 'chat "long"', "2026-10-01T00:00:00+00:00", [
            ("user", f"record {i}", "2026-10-01T00:00:00+00:00") for i in range(200)])
        identity = await self.batch()
        self.chat.imports.get(identity)["sessions"] = {session.key: session}
        self.chat.imports.start(identity, [session.key])
        # The task yields after 20 durable records, before the session checkpoint.
        await asyncio.sleep(0)
        self.chat.imports.cancel(identity)
        await self.chat.imports.close()
        count = self.chat.engine.status()["messages"]
        self.assertGreater(count, 0)
        self.assertLess(count, 201)
        await self.chat.engine.close()
        engine = self.chat._open(self.chat.args)
        self.engines.append(engine)
        self.chat.engine = engine
        self.assertEqual(engine.import_chats([session]), 201 - count)
        self.assertEqual(engine.status()["messages"], 201)
        self.assertEqual(len(set(m["text"] for m in engine.messages())), 201)
        self.assertEqual(engine.import_chats([session]), 0)

    async def test_backup_reopens_with_assets_agents_memory_and_pending_work(self):
        await self.serve()
        engine = self.chat.engine
        image = Path(self.temp.name, "image.png")
        image.write_bytes(PNG)
        ref = engine.attach(image)
        engine.record("user", "photo", attachments=[ref])
        engine.store.create_agent("Scout", "look around")
        log = engine.store.open_agent_log("Scout")
        log.append("user", "agent instruction")
        log.close()
        engine.note("memory " * 200)
        client = Client(f"http://127.0.0.1:{self.port}/#{'t' * 43}")
        async with client.response("GET", "/api/backup") as body:
            data = bytearray()
            while part := await body.read():
                data.extend(part)
        restored = Path(self.temp.name, "restored")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertNotIn(".lock", archive.namelist())
            self.assertIn("agents/Scout/state.json", archive.namelist())
            archive.extractall(restored)
        opened = open_chat(restored, DemoProvider(), Config())
        self.engines.append(opened)
        self.assertEqual(opened.messages(), engine.messages())
        self.assertEqual(opened.image(ref), PNG)
        self.assertEqual(opened.store.agent_records("Scout")[0]["text"], "agent instruction")
        await opened.finish()
        self.assertIn("photo", (await opened.ask("/recall 0")))
        with patch.object(engine.store, "backup", side_effect=ValueError("limit")):
            self.assertEqual((await self.request("GET", "/api/backup"))[0], 400)

    async def test_backup_rejects_symlinks_and_size_overflow(self):
        await self.serve()
        engine = self.chat.engine
        engine.note("data")
        with self.assertRaisesRegex(RuntimeError, "128 MB"):
            engine.store.backup(max_bytes=1)
        target = engine.store.root / "assets"
        target.rename(engine.store.root / "empty-assets")
        target.symlink_to(Path(self.temp.name))
        with self.assertRaisesRegex(RuntimeError, "linked"):
            engine.export_backup()

    async def test_switch_blocked_during_import_even_after_cancel_request(self):
        await self.serve()
        identity = await self.batch()
        batch = self.chat.imports.get(identity)
        batch["task"] = asyncio.create_task(asyncio.sleep(10))
        batch["state"] = "running"
        self.chat.imports.cancel(identity)
        with self.assertRaises(HTTPError) as caught:
            await self.chat.switch({"model": "other"})
        self.assertEqual(caught.exception.status, 409)
