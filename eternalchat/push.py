"""Optional standards-based Web Push; encrypted generic alerts, cancellable IO."""
import asyncio
import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from urllib.parse import urlsplit

HOSTS = {"fcm.googleapis.com", "updates.push.services.mozilla.com", "web.push.apple.com"}
TIMEOUT = 15


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(value, length):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", value):
        raise ValueError("Invalid push subscription key")
    try:
        decoded = base64.urlsafe_b64decode(value.rstrip("=") + "=" * (-len(value.rstrip("=")) % 4))
    except ValueError:
        raise ValueError("Invalid push subscription key") from None
    if len(decoded) != length:
        raise ValueError("Invalid push subscription key length")
    return decoded


def subscription(value):
    if not isinstance(value, dict):
        raise ValueError("Send a browser push subscription")
    endpoint, keys = value.get("endpoint"), value.get("keys")
    if not isinstance(endpoint, str) or len(endpoint) > 2048 or not endpoint.isascii() or any(ord(c) < 33 for c in endpoint):
        raise ValueError("Invalid push endpoint")
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "https" or parsed.hostname not in HOSTS or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.fragment or not parsed.path.startswith("/")):
        raise ValueError("Push endpoint must use a supported browser push service over HTTPS")
    if not isinstance(keys, dict) or unb64(keys.get("p256dh"), 65)[0] != 4:
        raise ValueError("Invalid push public key")
    unb64(keys.get("auth"), 16)
    return {"endpoint": endpoint, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}}


class Push:
    def __init__(self, recipients, expired):
        self.recipients, self.expired = recipients, expired
        self.queue = asyncio.Queue(1)
        self.worker = None
        self.last_error = None
        self.closed = False
        self.active = set()
        self._key = None
        self._configuration = None
        self._native_key = None
        self._native_config = None
        self._native_jwt = None

    def configuration(self):
        path, subject = os.environ.get("ETERNALCHAT_PUSH_KEY", ""), os.environ.get("ETERNALCHAT_PUSH_SUBJECT", "")
        if not path or not subject:
            return None
        if (not subject.startswith(("mailto:", "https://")) or len(subject) > 256
                or any(ord(c) < 33 for c in subject)):
            raise ValueError("Set ETERNALCHAT_PUSH_SUBJECT to a mailto: or HTTPS contact")
        if not importlib.util.find_spec("pywebpush"):
            raise ValueError("Install eternalchat[push] to enable background notifications")
        if self._configuration != (path, subject):
            try:
                from py_vapid import Vapid
                self._key = Vapid.from_file(path)
                self._configuration = path, subject
            except Exception:
                raise ValueError("Cannot load the server's VAPID key; check ETERNALCHAT_PUSH_KEY") from None
        return self._key, subject

    def native_status(self):
        try:
            config = self.native_configuration()
            return {"configured": bool(config), "topic": config[2] if config else "", "sandbox": config[3] if config else False}
        except ValueError as error:
            return {"configured": False, "reason": str(error)}

    def native_configuration(self):
        path = os.environ.get("ETERNALCHAT_APNS_KEY", "")
        if not path:
            return None
        team, kid, topic = (os.environ.get(name, "") for name in ("ETERNALCHAT_APNS_TEAM", "ETERNALCHAT_APNS_KEY_ID", "ETERNALCHAT_APNS_TOPIC"))
        sandbox = os.environ.get("ETERNALCHAT_APNS_SANDBOX", "0")
        if not re.fullmatch(r"[A-Z0-9]{10}", team) or not re.fullmatch(r"[A-Z0-9]{10}", kid) or not re.fullmatch(r"[A-Za-z0-9.-]{3,200}",topic) or sandbox not in ("0","1"):
            raise ValueError("Configure the APNs team, key ID, bundle topic and sandbox environment on the server")
        config = (path,team,kid,topic,sandbox)
        if self._native_config != config:
            try:
                from cryptography.hazmat.primitives.serialization import load_pem_private_key
                from cryptography.hazmat.primitives.asymmetric import ec
                key = load_pem_private_key(Path(path).read_bytes(),password=None)
                if not isinstance(key,ec.EllipticCurvePrivateKey) or not isinstance(key.curve,ec.SECP256R1):
                    raise ValueError
                self._native_key = key; self._native_config = config; self._native_jwt = None
            except Exception:
                raise ValueError("Cannot load the server's APNs P-256 key; install eternalchat[push] and check the private key file") from None
        return team,kid,topic,sandbox == "1"

    async def native_send(self, saved, identity):
        config = self.native_configuration()
        if not config:
            raise ValueError("Native push is not configured")
        team,kid,topic,sandbox = config
        if not isinstance(saved.get("token"),str) or not re.fullmatch(r"[0-9a-f]{64,512}",saved["token"]):
            raise ValueError("Invalid APNs device token")
        now = int(time.time())
        if not self._native_jwt or not 0 <= now - self._native_jwt[0] < 3000:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import ec, utils
            head = b64(json.dumps({"alg":"ES256","kid":kid},separators=(",",":")).encode())
            claims = b64(json.dumps({"iss":team,"iat":now},separators=(",",":")).encode())
            signing = f"{head}.{claims}"
            r,s = utils.decode_dss_signature(self._native_key.sign(signing.encode(),ec.ECDSA(hashes.SHA256())))
            self._native_jwt = now, signing + "." + b64(r.to_bytes(32,"big") + s.to_bytes(32,"big"))
        host = "api.sandbox.push.apple.com" if sandbox else "api.push.apple.com"
        headers = {"authorization":"bearer " + self._native_jwt[1], "apns-topic":topic,
                   "apns-push-type":"alert", "apns-priority":"10", "apns-expiration":str(now+300),
                   "apns-collapse-id":"eternalchat-reply", "Content-Type":"application/json"}
        payload = json.dumps({"aps":{"alert":{"title":"EternalChat","body":"A reply is ready."},"sound":"default"}}).encode()
        return await post(f"https://{host}/3/device/{saved['token']}", headers, payload, http2=True)

    def status(self):
        try:
            config = self.configuration()
            if not config:
                return {"configured": False, "reason": "Set up a VAPID key on the server for closed-page notifications."}
            from cryptography.hazmat.primitives import serialization
            public = config[0].public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
            return {"configured": True, "public_key": b64(public), "last_error": self.last_error}
        except ValueError as error:
            return {"configured": False, "reason": str(error)}

    def submit(self, identity):
        if self.closed or not (self.status()["configured"] or self.native_status()["configured"]):
            return
        if self.queue.full():
            self.queue.get_nowait()  # coalesce old generic alerts; never block a chat reply
        self.queue.put_nowait(identity)
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self.run())  # ubs:ignore — close() cancels and awaits the owned worker.

    async def run(self):
        while not self.queue.empty() and not self.closed:
            identity = self.queue.get_nowait()
            for session, saved in self.recipients():
                if (session, saved) not in self.recipients():
                    continue
                try:
                    code = await self.send(saved, identity)
                    if code in (404, 410):
                        self.expired(session, saved)
                    elif code not in (200, 201, 202):
                        self.last_error = f"Push service HTTP {code}; no automatic retry was sent."
                    else:
                        self.last_error = None
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.last_error = "Push delivery failed; check the server configuration and network. No retry was sent."

    async def close(self):
        self.closed = True
        tasks = self.active | ({self.worker} if self.worker else set())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def send(self, saved, identity):
        if self.closed or len(self.active) >= 4:
            raise ValueError("Push delivery is busy or shutting down")
        task = asyncio.current_task()
        self.active.add(task)
        try:
            return await (self.native_send(saved, identity) if saved.get("kind") == "apns" else self._send(saved, identity))
        finally:
            self.active.discard(task)

    async def _send(self, saved, identity):
        from pywebpush import WebPusher
        saved = subscription(saved)  # validate again before every network send
        key, subject = self.configuration()
        endpoint = urlsplit(saved["endpoint"])
        payload = json.dumps({"title": "EternalChat", "body": "A reply is ready.", "id": str(identity)[:80]}).encode()
        try:
            encrypted = WebPusher(saved).encode(payload, "aes128gcm")["body"]
            authorization = key.sign({"aud": f"https://{endpoint.hostname}", "sub": subject, "exp": int(time.time()) + 3600})
        except Exception:
            raise ValueError("Invalid push encryption configuration or browser key") from None
        headers = {**authorization, "Content-Type": "application/octet-stream", "Content-Encoding": "aes128gcm",
                   "TTL": "300", "Topic": "eternalchat-reply"}
        return await post(saved["endpoint"], headers, encrypted)


async def post(endpoint, headers, payload, *, http2=False):
    executable = shutil.which("curl")
    if not executable:
        raise ValueError("Push requires curl on the server")
    env = {k: v for k, v in os.environ.items() if not k.startswith("ETERNALCHAT_") and k not in {
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}}
    with tempfile.TemporaryDirectory(prefix="eternalchat-push-") as folder:
        path = Path(folder, "payload")
        path.write_bytes(payload)
        config = f'url = {json.dumps(endpoint)}\ndata-binary = {json.dumps("@" + str(path))}\n'
        config += "".join("header = " + json.dumps(f"{k}: {v}") + "\n" for k, v in headers.items())
        process = await asyncio.create_subprocess_exec(executable, "--disable", *(["--http2"] if http2 else []), "--silent", "--proto", "=https",
            "--max-time", str(TIMEOUT), "--connect-timeout", "5", "--output", os.devnull,
            "--max-filesize", "4096", "--write-out", "%{response_code}", "--config", "-", env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            async with asyncio.timeout(TIMEOUT + 1):
                process.stdin.write(config.encode()); await process.stdin.drain(); process.stdin.close()
                status = await process.stdout.read(4)
                code = await process.wait()
                if code or not status.isdigit():
                    raise ValueError("Push transport failed")
                return int(status)
        finally:  # ubs:ignore — cleanup has no return/break; cancellation must still reap curl.
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()


def generate(path):
    """Create a private P-256 key without overwriting an existing identity."""
    from py_vapid import Vapid
    key = Vapid(); key.generate_keys()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # ubs:ignore — fdopen context below owns and closes this binary descriptor.
    with os.fdopen(fd, "wb") as target:
        target.write(key.private_pem())


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Create EternalChat's persistent private Web Push key")
    parser.add_argument("key_file")
    generate(parser.parse_args().key_file)
    print("Private push key created. Set ETERNALCHAT_PUSH_KEY to that file and ETERNALCHAT_PUSH_SUBJECT to your mailto: contact.")
