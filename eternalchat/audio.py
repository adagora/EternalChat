"""Explicit OpenAI recorded-file transcription, independent of chat billing mode."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile

ENDPOINT = "https://api.openai.com/v1/audio/transcriptions"
MODEL = "gpt-transcribe"
MAX_AUDIO = 24_000_000
MAX_RESPONSE = 256_000
TIMEOUT = 90
FORMATS = {"audio/webm": "webm", "audio/mp4": "mp4", "audio/ogg": "ogg",
           "audio/wav": "wav", "audio/x-wav": "wav", "audio/mpeg": "mp3",
           "audio/flac": "flac", "audio/x-m4a": "m4a"}


class Transcriber:
    def __init__(self):
        self.tasks = set()
        self.closed = False

    def configured(self):
        return bool(os.environ.get("OPENAI_API_KEY"))

    async def close(self):
        self.closed = True
        pending = list(self.tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def transcribe(self, data, media_type):
        suffix = FORMATS.get(media_type.split(";", 1)[0].strip().lower())
        if not suffix or not data or len(data) > MAX_AUDIO:
            raise ValueError("Send a supported audio recording between 1 byte and 24 MB")
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise ValueError("Recorded audio needs OPENAI_API_KEY on the server; it uses the OpenAI API separately from chat")
        if not key.isascii() or any(ord(c) < 33 or c in '\\"' for c in key):
            raise ValueError("The server's OpenAI API key has an invalid format")
        if self.closed or len(self.tasks) >= 2:
            raise ValueError("Transcription is busy; try again shortly")
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            async with asyncio.timeout(TIMEOUT):
                return await self._request(data, suffix, key)
        except TimeoutError:
            raise ValueError("Transcription timed out; no retry was sent") from None
        finally:
            self.tasks.discard(task)

    async def _request(self, data, suffix, key):
        executable = shutil.which("curl")
        if not executable:
            raise ValueError("Recorded audio requires curl on the server")
        env = {k: v for k, v in os.environ.items() if not k.startswith("ETERNALCHAT_") and k not in {
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}}
        # Private transient file, removed after curl is reaped. No recording enters chat memory.
        with tempfile.TemporaryDirectory(prefix="eternalchat-audio-") as folder:
            audio = Path(folder, "recording." + suffix)
            audio.write_bytes(data)
            audio.chmod(0o600)
            try:
                process = await asyncio.create_subprocess_exec(executable, "--disable", "--silent",
                    "--proto", "=https,http" if ENDPOINT.startswith("http://127.0.0.1:") else "=https",
                    "--max-time", str(TIMEOUT), "--connect-timeout", "10", "--max-filesize", str(MAX_RESPONSE),
                    "--write-out", "%{stderr}%{response_code}", "--config", "-",
                    env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE)
            except OSError:
                raise ValueError("Could not start the transcription transport") from None
            readers = []

            async def bounded(stream, limit):
                value = bytearray()
                while chunk := await stream.read(16_384):
                    value += chunk
                    if len(value) > limit:
                        raise ValueError("Transcription response exceeds its size limit")
                return bytes(value)

            try:
                config = (f'url = {json.dumps(ENDPOINT)}\nheader = "Authorization: Bearer {key}"\n'
                          f'form = {json.dumps("file=@" + str(audio))}\nform = "model={MODEL}"\n')
                process.stdin.write(config.encode())
                await process.stdin.drain()
                process.stdin.close()
                readers = [asyncio.create_task(bounded(process.stdout, MAX_RESPONSE)),
                           asyncio.create_task(bounded(process.stderr, 4096))]
                payload, status = await asyncio.gather(*readers)
                code = await process.wait()
                if code or status != b"200":
                    http = f" HTTP {status.decode()}" if status.isdigit() else ""
                    raise ValueError(f"Transcription failed{http}; no retry or alternate backend was used")
                try:
                    result = json.loads(payload)
                    text = result["text"]
                    if not isinstance(text, str) or len(text) > 100_000:
                        raise ValueError
                except (ValueError, KeyError, TypeError):
                    raise ValueError("Transcription returned an invalid transcript") from None
                return {"text": text, "provider": "openai", "model": MODEL}
            except (BrokenPipeError, ConnectionResetError):
                raise ValueError("Transcription connection failed; no retry was sent") from None
            finally:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await process.wait()
                for reader in readers:
                    reader.cancel()
                await asyncio.gather(*readers, return_exceptions=True)
