"""Event-driven compaction: bounded workers, child readiness, no per-turn tree scan."""
import asyncio
from collections import deque

from .config import byte_prefix, byte_size
from .prompt import TOOLS, system_prompt, view_blocks
from .provider import ProviderError
from .view import address


class CompactionError(RuntimeError):
    pass


class Compactor:
    def __init__(self, store, view, provider, config, on_event=None):
        self.store, self.view, self.provider, self.config = store, view, provider, config
        self.on_event = on_event or (lambda *args: None)
        self.leaves, self.parents = deque(), deque()
        self.queued, self.active, self.failed = set(), {}, {}
        self.wake = asyncio.Event()
        self.changed = asyncio.Event()
        self.closed = False
        self.runner = None
        self.built = 0
        # Startup is O(N). Every later notification touches just the new leaf
        # and its immediate parent, never the historical tree.
        self.enqueue(range(len(store.messages)))
        for key in store.nodes:
            self._parent_ready(key)
        self.view.advance()

    def start(self):
        if self.runner is None:
            self.runner = asyncio.create_task(self._run())
        self.wake.set()

    def _queue(self, key):
        if key not in self.store.nodes and key not in self.queued and key not in self.active and key not in self.failed:
            self.queued.add(key)
            (self.leaves if key[0] == 0 else self.parents).append(key)

    def enqueue(self, ids):
        for i in ids:
            self._queue((0, i))
        self.wake.set()

    def retry(self):
        failed = sorted(self.failed)
        self.failed.clear()
        for key in failed:
            self._queue(key)
        self.leaves = deque(sorted(self.leaves, key=lambda k: k[1]))
        self.wake.set()

    def _parent_ready(self, key):
        l, i = key
        if (l, i ^ 1) in self.store.nodes:
            self._queue((l + 1, i // 2))

    def _next(self):
        if self.leaves:
            key = self.leaves[0]
            preceding = sum(k[0] == 0 and k[1] < key[1] for k in self.active)
            preceding += sum(k[0] == 0 and k[1] < key[1] for k in self.failed)
            if preceding < self.config.concurrency:
                return self.leaves.popleft()
        if self.parents:
            return self.parents.popleft()
        return None

    async def _run(self):
        while not self.closed:
            await self.wake.wait()
            self.wake.clear()
            while not self.closed and len(self.active) < self.config.concurrency:
                key = self._next()
                if key is None:
                    break
                self.queued.remove(key)
                task = asyncio.create_task(self._build(key))
                self.active[key] = task
                task.add_done_callback(lambda task, key=key: self._done(key, task))
            self.changed.set()

    def _done(self, key, task):
        self.active.pop(key, None)
        if task.cancelled():
            return
        error = task.exception()
        if error:
            self.failed[key] = f"{type(error).__name__}: {error}"
            self.on_event("compaction_error", {"node": address(key), "error": self.failed[key]})
        else:
            self.built += 1
            self._parent_ready(key)
            self.on_event("compaction", {"node": address(key), "built": self.built})
        self.wake.set()
        self.changed.set()

    async def _build(self, key):
        l, i = key
        start, count = address(key)
        if l == 0:
            message = self.store.message(i)
            source = f"{message['kind']}: {message['text']}"
            if message.get("attachments"):
                source += "\n[images attached; zoom original message to view]"
            task = f"Compaction: compress message {i} into one line of at most {self.config.summary_bytes} bytes"
            cutoff = i
        else:
            a, b = (l - 1, 2 * i), (l - 1, 2 * i + 1)
            source = self.store.node(a)["text"] + "\n" + self.store.node(b)["text"]
            task = (f"Compaction: merge lines {start}+{count//2} and {start+count//2}+{count//2}, "
                    f"adjacent, into one line of at most {self.config.summary_bytes} bytes. "
                    f"<chat> may hold messages {start} to {start+count-1} in more detail; use their details too")
            cutoff = start + count
        if byte_size(source) <= self.config.summary_bytes:
            summary = source
        else:
            task += (", the length of this ruler:\n" + "-" * self.config.summary_bytes +
                     f"\n<input>\n{source}\n</input>")
            content = view_blocks(self.view.lines(compact=True, cutoff=cutoff))
            content.append({"type": "text", "text": task})
            messages = [{"role": "user", "content": content}]
            shortest = None
            for attempt in range(self.config.attempts):
                response = await self.provider.complete(model=self.config.summary_model,
                    system=system_prompt(self.config.instructions), tools=TOOLS,
                    messages=messages, max_tokens=self.config.max_tokens)
                if any(b["type"] == "tool_use" for b in response.content) or not response.text.strip():
                    raise ProviderError("Compaction must return nonempty text without tool calls")
                text = response.text.strip().replace("\r", " ").replace("\n", " ")
                if shortest is None or byte_size(text) < byte_size(shortest):
                    shortest = text
                if byte_size(text) <= self.config.summary_bytes:
                    break
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": [{"type": "text", "text":
                    f"Too long: your line is {byte_size(text)} bytes, over the {self.config.summary_bytes}-byte limit. "
                    "Write the whole line again for the same <input>, cutting just enough of the least "
                    "valuable items to fit before this cut:\n" + byte_prefix(text, self.config.summary_bytes) + "| ← LIMIT"}]})
            summary = shortest
        # Commit node before any view reference. Only this event-loop thread
        # writes storage; concurrent network workers never touch the log.
        self.store.add_node(key, summary)
        self.view.advance()

    async def wait_leaves(self, end):
        self.start()
        while self.view.end(self.view.main) < end:
            self.changed.clear()
            failed = [(k, e) for k, e in self.failed.items() if k[0] == 0 and k[1] < end]
            if failed:
                key, error = min(failed)
                raise CompactionError(f"Cannot start turn: message {key[1]} is unsummarized ({error}). "
                                      "Fix the provider and retry, or run compact.")
            await self.changed.wait()

    async def drain(self):
        self.start()
        while self.queued or self.active:
            self.changed.clear()
            # Failed early leaves can intentionally block later work.
            if not self.active and self.failed and not self.parents and self.leaves:
                key = self.leaves[0]
                if sum(k[0] == 0 and k[1] < key[1] for k in self.failed) >= self.config.concurrency:
                    break
            await self.changed.wait()
        if self.failed:
            raise CompactionError(f"{len(self.failed)} compaction(s) failed; run compact to retry")

    async def close(self):
        self.closed = True
        if self.runner:
            self.runner.cancel()
        tasks = list(self.active.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, *([self.runner] if self.runner else []), return_exceptions=True)
