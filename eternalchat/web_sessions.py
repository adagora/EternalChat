"""Persistent, individually revocable credentials; no bearer secrets in saved state."""
import copy
import hashlib
import hmac
import re
import secrets
import time

SESSION_TTL = 90 * 86400
INVITE_TTL = 600
LIMIT = 64
TOKEN = re.compile(r"(ecs|eci)\.([0-9a-f]{32})\.([A-Za-z0-9_-]{43})")


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Sessions:
    def __init__(self, engine, master):
        self.engine, self.master = engine, master

    def load(self):
        state = self.engine().web_auth()
        if not hmac.compare_digest(str(state.get("authority", "")).encode(), digest(self.master).encode()):
            state = {"authority": digest(self.master), "sessions": {}, "invites": {}}
        now = time.time()
        for group in ("sessions", "invites"):
            state[group] = {k: v for k, v in state.get(group, {}).items() if v["expires"] > now}
        return state

    def save(self, state):
        self.engine().save_web_auth(state)

    def authenticate(self, token):
        if hmac.compare_digest(token.encode(), self.master.encode()):
            return {"id": "master", "scope": "master"}
        match = TOKEN.fullmatch(token)
        if not match or match[1] != "ecs":
            return None
        record = self.load()["sessions"].get(match[2])
        if not record or not hmac.compare_digest(record["digest"], digest(token)):
            return None
        return {"id": match[2], **copy.deepcopy(record)}

    def _new(self, scope, label):
        if scope not in {"browser", "device"}:
            raise ValueError("Choose a browser or computer session")
        if not isinstance(label, str) or not label.strip() or len(label) > 80 or any(ord(c) < 32 for c in label):
            raise ValueError("Device name must contain 1–80 printable characters")
        identity = secrets.token_hex(16)
        token = f"ecs.{identity}.{secrets.token_urlsafe(32)}"
        now = time.time()
        return identity, token, {"digest": digest(token), "scope": scope, "label": label.strip(),
                                 "created": now, "expires": now + SESSION_TTL}

    def open(self, credential, label):
        state = self.load()
        root = hmac.compare_digest(credential.encode(), self.master.encode())
        match = TOKEN.fullmatch(credential)
        invite = state["invites"].get(match[2]) if match and match[1] == "eci" else None
        if not root and (not invite or not hmac.compare_digest(invite["digest"], digest(credential))):
            raise PermissionError("Pairing link expired or already used; create another link")
        if len(state["sessions"]) >= LIMIT:
            raise ValueError("Device session limit reached; revoke an unused session")
        identity, token, record = self._new("browser", label)
        state["sessions"][identity] = record
        if invite:
            del state["invites"][match[2]]
        self.save(state)  # consume invitation and create credential in one durable write
        return {"token": token, "id": identity, "expires": record["expires"]}

    def invite(self):
        state = self.load()
        if len(state["invites"]) >= 16:
            raise ValueError("Too many pending pairing links; wait ten minutes or revoke them")
        identity = secrets.token_hex(16)
        token = f"eci.{identity}.{secrets.token_urlsafe(32)}"
        expiry = time.time() + INVITE_TTL
        state["invites"][identity] = {"digest": digest(token), "expires": expiry}
        self.save(state)
        return {"token": token, "expires": expiry}

    def computer(self, label):
        state = self.load()
        if len(state["sessions"]) >= LIMIT:
            raise ValueError("Device session limit reached")
        identity, token, record = self._new("device", label)
        state["sessions"][identity] = record
        self.save(state)
        return {"token": token, "id": identity, "expires": record["expires"]}

    def bind_computer(self, identity, name):
        state = self.load()
        record = state["sessions"].get(identity)
        if not record or record["scope"] != "device" or (record.get("device_name") not in (None, name)):
            raise PermissionError("Computer credential belongs to another device")
        record["device_name"] = name
        self.save(state)

    def listing(self, current):
        return [{"id": identity, "label": r["label"], "scope": r["scope"], "created": r["created"],
                 "expires": r["expires"], "current": identity == current, "push": bool(r.get("push")),
                 "device_name": r.get("device_name", "")}
                for identity, r in self.load()["sessions"].items()]

    def revoke(self, identity):
        state = self.load()
        record = state["sessions"].pop(identity, None)
        if not record:
            raise ValueError("Session not found")
        self.save(state)
        return record

    def subscription(self, identity, subscription):
        state = self.load()
        record = state["sessions"].get(identity)
        if not record or record["scope"] != "browser":
            raise ValueError("Open a browser session first")
        if subscription is None:
            record.pop("push", None)
        else:
            # One endpoint belongs to one session, including browser re-pairing.
            for other in state["sessions"].values():
                old = other.get("push", {})
                if old.get("kind", "web") == subscription.get("kind", "web") and (old.get("endpoint") or old.get("token")) == (subscription.get("endpoint") or subscription.get("token")):
                    other.pop("push", None)
            record["push"] = copy.deepcopy(subscription)
        self.save(state)

    def recipients(self):
        return [(identity, copy.deepcopy(r["push"])) for identity, r in self.load()["sessions"].items()
                if r.get("push")]
