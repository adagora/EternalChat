"""Composition root: wire the filesystem store and a provider into an Engine."""
from .config import Config
from .engine import Engine
from .storage import Store


def open_chat(root, provider=None, config=None, on_event=None, *, readonly=False):
    """Open the chat folder `root`.

    A writable chat needs a provider and takes the exclusive lock. A read-only
    chat needs no provider, shares its lock with other readers, never writes
    and fails if the folder holds no chat.
    """
    config = config or Config()
    if provider is None and not readonly:
        raise ValueError("A writable chat needs a provider")
    store = Store(root, config.message_bytes, readonly=readonly)
    return Engine(store, provider, config, on_event)
