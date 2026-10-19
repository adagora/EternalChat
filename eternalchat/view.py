"""The saved memory view: two persisted partitions of the tree and their merge policy.

The main view feeds turns; the smaller compaction view gives summaries context.
Each is a chronological list of node keys covering a prefix of the chat exactly
once. Both are saved after every change and never refitted on restart.
Persistence goes through the store's read_state/write_state; no file IO here.
"""
from functools import lru_cache

from .config import byte_size
from .errors import StoreError
from .tree import address, candidate, children

CHAT_OPEN, CHAT_CLOSE = "<chat>\n", "</chat>"
FRAME_BYTES = byte_size(CHAT_OPEN + CHAT_CLOSE)


class _Built:
    """The store's built nodes as a container, for tree.candidate."""

    def __init__(self, store):
        self._has = store.has_node

    def __contains__(self, key):
        return self._has(key)


class View:
    def __init__(self, store, config):
        self._store, self._config = store, config
        self._built = _Built(store)
        self.line = lru_cache(maxsize=4096)(self._line)
        self._main = self._load("view")
        self._small = self._load("compaction-view")
        state = store.read_state("view-state")
        if state is None:
            flags = (False, False)
        else:
            try:
                flags = (state["main_pending"], state["small_pending"])
            except (KeyError, TypeError):
                flags = None
            if flags is None or not all(isinstance(flag, bool) for flag in flags):
                raise StoreError("Invalid view-state.json; restore a verified backup")
        self._main_pending, self._small_pending = flags
        self._saved_flags = flags if state is not None else None
        self._dirty = set()  # partitions whose saved copy is behind memory
        self._validate(self._main)
        self._validate(self._small)

    def _load(self, name):
        raw = self._store.read_state(name, required=True)
        if raw is None:
            if not self._store.readonly:
                self._store.write_state(name, [])  # an empty chat's view must exist on disk
            return []
        try:
            if not isinstance(raw, list):
                raise TypeError("not a list")
            return [tuple(k) for k in raw]
        except TypeError as e:
            raise StoreError(f"Invalid saved {name}; restore a verified backup") from e

    def _validate(self, nodes):
        end = 0
        for key in nodes:
            if len(key) != 2 or any(type(x) is not int or x < 0 for x in key):
                raise StoreError("Invalid node in persisted view")
            if not self._store.has_node(key) or address(key)[0] != end:
                raise StoreError("Persisted view contains a gap, overlap or missing node")
            end += address(key)[1]

    # Reading ---------------------------------------------------------------------

    @property
    def main(self):
        return tuple(self._main)

    @property
    def small(self):
        return tuple(self._small)

    @property
    def main_pending(self):
        return self._main_pending

    @property
    def covered(self):
        """Messages 0..covered-1 are summarized in the main view."""
        return self.end(self._main)

    @staticmethod
    def end(nodes):
        if not nodes:
            return 0
        start, count = address(nodes[-1])
        return start + count

    def _line(self, key):
        start, count = address(key)
        text = self._store.node_text(key).replace("\r", " ").replace("\n", " ")
        return f"{start}+{count}|{text}\n"

    def line_bytes(self, key):
        # Same as byte_size(self.line(key)): \r and \n become one-byte spaces.
        start, count = address(key)
        return len(f"{start}+{count}|") + self._store.node_bytes(key) + 1

    def size(self, nodes):
        return FRAME_BYTES + sum(self.line_bytes(k) for k in nodes)

    def lines(self, *, compact=False, cutoff=None):
        """View lines, optionally covering only messages before `cutoff`."""
        nodes = self._small if compact else self._main
        result = []

        def emit(key):
            start, count = address(key)
            if cutoff is None or start + count <= cutoff:
                result.append(self.line(key))
            elif start < cutoff and key[0]:
                # A persisted coarse node may straddle the cutoff. Open it down
                # to the boundary; never leak later messages.
                for child in children(key):
                    emit(child)

        for key in nodes:
            if cutoff is not None and address(key)[0] >= cutoff:
                break
            emit(key)
        return result

    def render(self, **kwargs):
        return CHAT_OPEN + "".join(self.lines(**kwargs)) + CHAT_CLOSE

    def stats(self):
        return {"view_nodes": len(self._main), "view_bytes": self.size(self._main),
                "summarized_messages": self.covered,
                "compaction_view_bytes": self.size(self._small),
                "batch_pending": self._main_pending, "view_saved": not self.dirty}

    # Updating --------------------------------------------------------------------

    def _shrink(self, nodes, low):
        changed = False
        current = self.size(nodes)
        # Age is relative to this view, not input still waiting for summaries.
        # Otherwise a bulk import changes merge order before its leaves arrive.
        end = self.end(nodes)
        while current > low:
            choice = candidate(nodes, end, self._built)
            if choice is None:
                break
            j, merged = choice
            current += self.line_bytes(merged) - sum(self.line_bytes(k) for k in nodes[j:j + 2])
            nodes[j:j + 2] = [merged]
            changed = True
        return changed

    def advance(self):
        """Append newly built leaves; merge a partition in one batch when it is over budget."""
        for name, nodes in (("view", self._main), ("compaction-view", self._small)):
            end = self.end(nodes)
            while self._store.has_node((0, end)):
                nodes.append((0, end))
                end += 1
                self._dirty.add(name)  # before any write that could fail
        old_main, old_small = list(self._main), list(self._small)
        if self.size(self._main) > self._config.view_high:
            self._main_pending = True
        if self._main_pending:
            self._small_pending = True
        if self.size(self._small) > self._config.compact_high:
            self._small_pending = True
        # Commit the intent before replacing either partition. Replaying a
        # pending batch after a crash cannot forget that its low target was due.
        self._save_intent()
        merged = False
        if self._main_pending:
            merged = self._shrink(self._main, self._config.view_low)
            self._main_pending = self.size(self._main) > self._config.view_low
        if merged:
            self._small = list(self._main)
            self._small_pending = True
        if self._small_pending:
            self._shrink(self._small, self._config.compact_low)
            self._small_pending = self.size(self._small) > self._config.compact_low
        if self._main != old_main:
            self._dirty.add("view")
        if self._small != old_small:
            self._dirty.add("compaction-view")
        self.flush()

    @property
    def dirty(self):
        """True while memory is ahead of the saved view."""
        return bool(self._dirty) or self._saved_flags != (self._main_pending, self._small_pending)

    def flush(self):
        """Save partitions whose saved copy is behind memory, then the pending flags.

        If a save fails, the partition stays dirty and the next advance or flush
        retries it; a restart recovers from the saved intent either way.
        """
        for name, nodes in (("view", self._main), ("compaction-view", self._small)):
            if name in self._dirty:
                self._store.write_state(name, nodes)
                self._dirty.discard(name)
        # Flags may be cleared only once no partition is behind memory.
        self._write_flags((self._main_pending, self._small_pending))

    def _save_intent(self):
        """Before replacing partitions, saved flags may only gain pending bits."""
        saved = self._saved_flags or (False, False)
        self._write_flags((saved[0] or self._main_pending, saved[1] or self._small_pending))

    def _write_flags(self, flags):
        if flags != self._saved_flags:
            self._store.write_state("view-state", {"main_pending": flags[0], "small_pending": flags[1]})
            self._saved_flags = flags
