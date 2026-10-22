"""Recorded audio: actual multipart transport, cancellation and authenticated draft-only API."""
import asyncio
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from eternalchat import audio
from tests.test_engine import EngineFixture
from tests.test_web import WebFixture


class AudioTests(EngineFixture):
    async def fixture(self, payload=b'{"text":"A useful draft"}', status=200, delay=0):
        self.calls = []
        self.received = asyncio.Event()
        self.released = asyncio.Event()

        async def handle(reader, writer):
            try:
                head = await reader.readuntil(b'\r\n\r\n')
                size = int(next(line.split(b':', 1)[1] for line in head.split(b'\r\n') if line.lower().startswith(b'content-length:')))
                # curl can use Expect: 100-continue for larger payloads.
                if b'100-continue' in head:
                    writer.write(b'HTTP/1.1 100 Continue\r\n\r\n'); await writer.drain()
                body = await reader.readexactly(size)
                self.calls.append((head, body)); self.received.set()
                if delay:
                    await reader.read()
                else:
                    writer.write(f'HTTP/1.1 {status} Fixture\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n'.encode()+payload)
                    await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writer.close(); self.released.set()
        server = await asyncio.start_server(handle, '127.0.0.1', 0)
        self.addCleanup(server.close)
        endpoint = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1/audio/transcriptions'
        self.enterContext(patch.object(audio, 'ENDPOINT', endpoint))
        self.enterContext(patch.dict(os.environ, OPENAI_API_KEY='test-recording-secret'))
        transcriber = audio.Transcriber()
        self.addAsyncCleanup(transcriber.close)
        return transcriber

    async def test_real_multipart_no_chat_payload_and_private_file_cleanup(self):
        transcriber = await self.fixture()
        folders = []
        original = tempfile.TemporaryDirectory
        def directory(*args, **kwargs):
            temp = original(*args, **kwargs)
            folders.append(Path(temp.name))
            self.assertEqual(Path(temp.name).stat().st_mode & 0o777, 0o700)
            return temp
        with patch.object(audio.tempfile, 'TemporaryDirectory', directory):
            result = await transcriber.transcribe(b'audio-content', 'audio/webm;codecs=opus')
        self.assertTrue(folders)
        self.assertTrue(all(not folder.exists() for folder in folders))
        self.assertEqual(result['text'], 'A useful draft')
        head, body = self.calls[0]
        self.assertIn(b'Authorization: Bearer test-recording-secret', head)
        self.assertIn(b'name="model"\r\n\r\ngpt-transcribe', body)
        self.assertIn(b'filename="recording.webm"', body)
        self.assertIn(b'audio-content', body)
        self.assertNotIn(b'chat', body)

    async def test_failure_redaction_no_retries_and_invalid_json(self):
        for payload, status in [(b'{"error":"test-recording-secret"}',401), (b'not json',200), (b'{"text":[]}',200)]:
            t = await self.fixture(payload, status)
            with self.assertRaises(ValueError) as error:
                await t.transcribe(b'recording', 'audio/mp4')
            self.assertNotIn('test-recording-secret', str(error.exception))
            self.assertEqual(len(self.calls), 1)

    async def test_cancel_timeout_and_server_close_reap_transport(self):
        for mode in ['cancel', 'timeout', 'close']:
            t = await self.fixture(delay=1)
            with patch.object(audio, 'TIMEOUT', .15 if mode == 'timeout' else 90):
                task = asyncio.create_task(t.transcribe(b'clip', 'audio/ogg'))
                await asyncio.wait_for(self.received.wait(), 2)
                if mode == 'cancel': task.cancel()
                if mode == 'close': await t.close()
                with self.assertRaises(ValueError if mode == 'timeout' else asyncio.CancelledError): await task
                await asyncio.wait_for(self.released.wait(), 2)
                self.assertEqual(t.tasks, set())

    async def test_missing_credentials_size_type_and_concurrency(self):
        t = await self.fixture(delay=1)
        for content, kind in [(b'', 'audio/webm'), (b'a', 'text/plain')]:
            with self.assertRaises(ValueError): await t.transcribe(content, kind)
        with patch.object(audio, 'MAX_AUDIO', 2):
            with self.assertRaises(ValueError): await t.transcribe(b'abc', 'audio/webm')
        with patch.dict(os.environ, OPENAI_API_KEY=''):
            self.assertFalse(t.configured())
            with self.assertRaisesRegex(ValueError, 'OPENAI_API_KEY'): await t.transcribe(b'a', 'audio/webm')
        jobs = [asyncio.create_task(t.transcribe(b'a', 'audio/webm')) for _ in range(2)]
        await self.received.wait()
        with self.assertRaisesRegex(ValueError, 'busy'): await t.transcribe(b'a', 'audio/webm')
        await t.close()
        await asyncio.gather(*jobs, return_exceptions=True)


class AudioWebTests(WebFixture):
    async def test_auth_demo_guard_and_draft_only(self):
        await self.serve()
        self.assertFalse((await self.request('GET', '/api/state'))[1]['audio']['configured'])
        self.assertEqual((await self.request('POST', '/api/transcribe', b'clip', token=None))[0], 403)
        self.assertEqual((await self.request('POST', '/api/transcribe', b'clip'))[0], 400)
        self.chat.args.demo = False
        async def transcribe(data, kind):
            self.assertEqual((data, kind), (b'clip', 'audio/webm'))
            return {'text':'Review this draft'}
        with patch.object(self.chat.transcriber, 'transcribe', transcribe):
            status, result, _ = await self.request('POST', '/api/transcribe', b'clip', content_type='audio/webm')
        self.assertEqual((status, result), (200, {'text':'Review this draft'}))
        self.assertEqual(self.chat.engine.status()['messages'], 0)

    async def test_disconnecting_upload_cancels_transcription(self):
        await self.serve()
        self.chat.args.demo = False
        started, stopped = asyncio.Event(), asyncio.Event()
        async def transcribe(data, kind):
            started.set()
            try: await asyncio.Future()
            finally: stopped.set()
        with patch.object(self.chat.transcriber, 'transcribe', transcribe):
            reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
            writer.write(f'POST /api/transcribe HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nX-EternalChat-Token: {"t"*43}\r\nContent-Length: 4\r\nContent-Type: audio/webm\r\n\r\nclip'.encode())
            await writer.drain(); await started.wait()
            writer.close(); await writer.wait_closed()
            await asyncio.wait_for(stopped.wait(), 2)
