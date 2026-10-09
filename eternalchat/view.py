"""Persisted dyadic partitions, with age-normalized, batched sibling merges."""
import json
from fractions import Fraction
from functools import lru_cache

from .config import byte_size
from .storage import StoreError, atomic_json


def address(key):
    l, i = key
    return i * (1 << l), 1 << l


def key_for(start, count):
    if type(start) is not int or type(count) is not int:
        raise ValueError("id and n must be integers")
    if start < 0 or count < 1 or count & (count - 1) or start % count:
        raise ValueError("n must be a power of two and id a nonnegative multiple of n")
    return count.bit_length() - 1, start // count


def candidate(nodes, total, available):
    best = None
    for j in range(len(nodes) - 1):
        l, i = nodes[j]
        if i % 2 or nodes[j + 1] != (l, i + 1):
            continue
        parent = (l + 1, i // 2)
        if parent not in available:
            continue
        first, size = address(nodes[j])
        last = first + size * 2 - 1
        score = (Fraction(total - last, size), -first)
        if best is None or score > best[0]:
            best = score, j, parent
    return None if best is None else (best[1], best[2])


class View:
    def __init__(self, store, config):
        self.store, self.config = store, config
        self.line = lru_cache(maxsize=4096)(self._line)
        self.path = store.root / "view.json"
        self.compact_path = store.root / "compaction-view.json"
        self.state_path = store.root / "view-state.json"
        self.main = self._load(self.path)
        self.small = self._load(self.compact_path)
        self.main_pending = False
        self.small_pending = False
        if self.state_path.exists():
            try:
                state = json.loads(self.state_path.read_text())
                self.main_pending = state["main_pending"]
                self.small_pending = state["small_pending"]
            except (ValueError, KeyError, TypeError) as e:
                raise StoreError("Invalid view-state.json; restore a verified backup") from e
        self._validate(self.main)
        self._validate(self.small)
        self.version = 0

    def _load(self, path):
        if not path.exists():
            if self.store.messages:
                raise StoreError(f"Missing {path.name} for existing chat; restore a verified backup. "
                                 "Automatic refitting would change the saved memory view.")
            atomic_json(path, [])
            return []
        try:
            raw = json.loads(path.read_text())
            if not isinstance(raw, list):
                raise ValueError("not a list")
            return [tuple(k) for k in raw]
        except (ValueError, TypeError) as e:
            raise StoreError(f"Invalid {path.name}; restore a verified backup") from e

    def _validate(self, nodes):
        end = 0
        for key in nodes:
            if len(key) != 2 or any(type(x) is not int or x < 0 for x in key):
                raise StoreError("Invalid node in persisted view")
            start, count = address(key)
            if start != end or key not in self.store.nodes:
                raise StoreError("Persisted view contains a gap, overlap or missing node")
            end += count

    def _line(self, key):
        start, count = address(key)
        text = self.store.node(key)["text"].replace("\r", " ").replace("\n", " ")
        return f"{start}+{count}|{text}\n"

    def size(self, nodes):
        return byte_size("<chat>\n</chat>") + sum(byte_size(self.line(k)) for k in nodes)

    @staticmethod
    def end(nodes):
        if not nodes:
            return 0
        start, count = address(nodes[-1])
        return start + count

    def _state(self):
        atomic_json(self.state_path, {"main_pending": self.main_pending,
                                     "small_pending": self.small_pending})

    def _shrink(self, nodes, low):
        changed = False
        current = self.size(nodes)
        while current > low:
            choice = candidate(nodes, len(self.store.messages), self.store.nodes)
            if choice is None:
                break
            j, parent = choice
            current += byte_size(self.line(parent)) - sum(byte_size(self.line(k)) for k in nodes[j:j+2])
            nodes[j:j+2] = [parent]
            changed = True
        return changed

    def advance(self):
        old_main, old_small = self.main.copy(), self.small.copy()
        for nodes in (self.main, self.small):
            end = self.end(nodes)
            while (0, end) in self.store.nodes:
                nodes.append((0, end))
                end += 1
        if self.size(self.main) > self.config.view_high:
            self.main_pending = True
        # Commit the intent before replacing either partition. Replaying a
        # pending batch after a crash cannot forget that its low target was due.
        if self.main_pending:
            self.small_pending = True
        if self.size(self.small) > self.config.compact_high:
            self.small_pending = True
        if self.main_pending or self.small_pending:
            self._state()
        merged = False
        if self.main_pending:
            merged = self._shrink(self.main, self.config.view_low)
            self.main_pending = self.size(self.main) > self.config.view_low
        if merged:
            self.small = self.main.copy()
            self.small_pending = True
        if self.small_pending:
            self._shrink(self.small, self.config.compact_low)
            self.small_pending = self.size(self.small) > self.config.compact_low
        if self.main != old_main:
            atomic_json(self.path, self.main)
            self.version += 1
        if self.small != old_small:
            atomic_json(self.compact_path, self.small)
        self._state()

    def lines(self, *, compact=False, cutoff=None):
        nodes = self.small if compact else self.main
        result = []

        def emit(key):
            start, count = address(key)
            if cutoff is None or start + count <= cutoff:
                result.append(self.line(key))
            elif start < cutoff and key[0]:
                # A persisted coarse node may straddle this job's cutoff.
                # Open it down to the boundary; never leak later messages.
                l, i = key
                emit((l - 1, 2 * i))
                emit((l - 1, 2 * i + 1))

        for key in nodes:
            if cutoff is not None and address(key)[0] >= cutoff:
                break
            emit(key)
        return result

    def render(self, **kwargs):
        return "<chat>\n" + "".join(self.lines(**kwargs)) + "</chat>"
