"""Composition root: wire the filesystem store and a provider into an Engine."""
from .config import Config
from .engine import Engine
from .storage import Store
from .search import Search


def open_chat(root, provider=None, config=None, on_event=None, *, readonly=False, machine=None, search=None, after_turn=None):
    """Open the chat folder `root`.

    A writable chat needs a provider and takes the exclusive lock. A read-only
    chat needs no provider, shares its lock with other readers, never writes
    and fails if the folder holds no chat. A machine (machine.Machine) gives
    the model the computer's tools; a read-only chat never gets one.
    Writable chats also get a search adapter, independently of computer access;
    callers can supply one for testing or embedding.
    """
    config = config or Config()
    if provider is None and not readonly:
        raise ValueError("A writable chat needs a provider")
    store = Store(root, config.message_bytes, readonly=readonly)
    return Engine(store, provider, config, on_event, None if readonly else machine,
                  search=None if readonly else search if search is not None else Search(),
                  after_turn=None if readonly else after_turn)
