"""Saved settings: what `eternalchat setup` stores, so a plain `eternalchat` needs no flags.

They live in $ETERNALCHAT_HOME/config.json (default ~/.eternalchat), beside the
default chat folder. A command-line option wins over an environment variable,
which wins over a saved setting. Credentials are never saved here.
"""
import json
import os
from pathlib import Path

KEYS = ("data_dir", "connection", "cli", "model", "summary_model", "reasoning_effort", "instructions",
        "tools", "work")
LEGACY_CHAT = Path(".eternalchat")  # the default before settings: a chat per working folder


def home():
    return Path(os.environ.get("ETERNALCHAT_HOME") or Path.home() / ".eternalchat")


def path():
    return home() / "config.json"


def default_chat():
    return home() / "chat"


def default_work():
    return home() / "work"


def load():
    """The saved settings, or {} when none are saved. Raises ValueError for a broken file."""
    try:
        raw = path().read_bytes()
    except FileNotFoundError:
        return {}
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or not all(isinstance(value[k], str) for k in KEYS if k in value):
            raise ValueError
    except ValueError:
        raise ValueError(f"{path()} is not a valid settings file; fix it or run 'eternalchat setup'") from None
    return {k: value[k] for k in KEYS if value.get(k)}


def save(values):
    """Replace the settings file, readable only by its owner; returns its path.
    Empty values are left out, so they fall back to the defaults."""
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    data = json.dumps({k: str(values[k]) for k in KEYS if values.get(k)}, indent=2, ensure_ascii=False)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, target)
    return target
