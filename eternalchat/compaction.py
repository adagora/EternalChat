"""Event-driven summary building: bounded workers, child readiness, no per-turn tree scan."""
import asyncio
from collections import deque

from .config import byte_prefix, byte_size
from .errors import CompactionError, ProviderError
from .prompt import TOOLS, system_prompt, text_block, view_blocks
from .tree import address, children, parent, sibling


class Compactor:
    def __init__(self, store, view, provider, config, on_event=None):
        self.store, self.view, self.provider, self.config = store, view, provider, config
        self.on_event = on_event or (lambda *args: None)
        self._leaves, self._parents = deque(), deque()
        self._queued, self._active, self._failed = set(), {}, {}
        self.checkpoint_error = None  # why the last view save failed, until one succeeds
        self._wake = asyncio.Event()
        self._changed = asyncio.Event()
        self._closed = False
        self._runner = None
        self.built = 0
        # Startup is O(N). Every later notification touches just the new node
        # and its immediate parent, never the historical tree.
        self.enqueue(range(store.message_count))
        for key in store.node_keys():
            self._parent_ready(key)

    # Scheduling ------------------------------------------------------------------

    def start(self):
        if self.provider is None:
            raise RuntimeError("This chat is open read-only; summaries cannot be built")
        if self._runner is None:
            self._runner = asyncio.create_task(self._run())
        self._wake.set()

    def _queue(self, key):
        if (not self.store.has_node(key) and key not in self._queued
                and key not in self._active and key not in self._failed):
            self._queued.add(key)
            (self._leaves if key[0] == 0 else self._parents).append(key)

    def enqueue(self, ids):
        for i in ids:
            self._queue((0, i))
        self._wake.set()

    def retry(self):
        """Requeue failed summaries and resave a view checkpoint that failed.

        Never raises: a save that fails again is reported and kept for later.
        """
        self._save_view(self.view.flush)
        failed = sorted(self._failed)
        self._failed.clear()
        for key in failed:
            self._queue(key)
        self._leaves = deque(sorted(self._leaves, key=lambda k: k[1]))
        self._wake.set()

    def _parent_ready(self, key):
        if self.store.has_node(sibling(key)):
            self._queue(parent(key))

    def _earlier_unfinished_leaves(self, i):
        return (sum(k[0] == 0 and k[1] < i for k in self._active) +
                sum(k[0] == 0 and k[1] < i for k in self._failed))

    def _next(self):
        # A leaf starts while fewer than `concurrency` earlier leaves are unbuilt.
        if self._leaves and self._earlier_unfinished_leaves(self._leaves[0][1]) < self.config.concurrency:
            return self._leaves.popleft()
        if self._parents:
            return self._parents.popleft()
        return None

    async def _run(self):
        while not self._closed:
            await self._wake.wait()
            self._wake.clear()
            while not self._closed and len(self._active) < self.config.concurrency:
                key = self._next()
                if key is None:
                    break
                self._queued.remove(key)
                task = asyncio.create_task(self._build(key))
                self._active[key] = task
                task.add_done_callback(lambda task, key=key: self._done(key, task))
            self._changed.set()

    def _save_view(self, save):
        try:
            save()
        except (OSError, RuntimeError) as error:
            self.checkpoint_error = f"{type(error).__name__}: {error}"
            self.on_event("compaction_error", {"view": "not saved", "error": self.checkpoint_error})
            return False
        self.checkpoint_error = None
        return True

    def _done(self, key, task):
        self._active.pop(key, None)
        if task.cancelled():
            return
        error = task.exception()
        if self.store.has_node(key):
            # The node is durable; only the view save after it can have failed,
            # and _build already reported that. Its parent must not wait.
            self.built += 1
            self._parent_ready(key)
            self.on_event("compaction", {"node": address(key), "built": self.built})
        elif error:
            self._failed[key] = f"{type(error).__name__}: {error}"
            self.on_event("compaction_error", {"node": address(key), "error": self._failed[key]})
        self._wake.set()
        self._changed.set()

    # Building --------------------------------------------------------------------

    def _task(self, key):
        """(source text, task text, context cutoff) for one node."""
        l, i = key
        start, count = address(key)
        limit = self.config.summary_bytes
        if l == 0:
            message = self.store.message(i)
            source = f"{message['kind']}: {message['text']}"
            if message.get("attachments"):
                source += "\n[images attached; zoom original message to view]"
            return source, f"Compaction: compress message {i} into one line of at most {limit} bytes", i
        a, b = children(key)
        source = self.store.node_text(a) + "\n" + self.store.node_text(b)
        task = (f"Compaction: merge lines {start}+{count//2} and {start+count//2}+{count//2}, "
                f"adjacent, into one line of at most {limit} bytes. "
                f"<chat> may hold messages {start} to {start+count-1} in more detail; use their details too")
        return source, task, start + count

    async def _build(self, key):
        summary = await self._summarize(key)
        # Commit the node before any view reference. Only this event-loop thread
        # writes storage; concurrent network workers never touch the log.
        self.store.add_node(key, summary)
        self._save_view(self.view.advance)

    async def _summarize(self, key):
        source, task, cutoff = self._task(key)
        limit = self.config.summary_bytes
        if byte_size(source) <= limit:
            return source  # short sources skip the model
        task += f", the length of this ruler:\n{'-' * limit}\n<input>\n{source}\n</input>"
        content = view_blocks(self.view.lines(compact=True, cutoff=cutoff)) + [text_block(task)]
        messages = [{"role": "user", "content": content}]
        shortest = None
        for _ in range(self.config.attempts):
            try:
                response = await self.provider.complete(
                    model=self.config.summary_model,
                    system=system_prompt(self.config.instructions, limit), tools=TOOLS,
                    messages=messages, max_tokens=self.config.max_tokens, on_event=self._provider_event)
                if response.tool_calls or not response.text.strip():
                    raise ProviderError("Compaction must return nonempty text without tool calls")
            except ProviderError:
                if shortest is None:
                    raise
                break  # a failed retry must not discard the usable line already written
            text = response.text.strip().replace("\r", " ").replace("\n", " ")
            if shortest is None or byte_size(text) < byte_size(shortest):
                shortest = text
            if byte_size(text) <= limit:
                break
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": [text_block(
                f"Too long: your line is {byte_size(text)} bytes, over the {limit}-byte limit. "
                "Write the whole line again for the same <input>, cutting just enough of the least "
                "valuable items to fit before this cut:\n" + byte_prefix(text, limit) + "| ← LIMIT")]})
        return shortest

    def _provider_event(self, kind, value):
        if kind == "usage":
            self.on_event("usage", value)

    # Waiting ---------------------------------------------------------------------

    def stats(self):
        return {"queued": len(self._queued), "active": len(self._active), "failed": len(self._failed),
                "view_save_error": self.checkpoint_error}

    async def wait_leaves(self, end):
        """Wait until the main view summarizes messages 0..end-1."""
        if self.view.covered >= end:
            return
        self.start()
        while self.view.covered < end:
            self._changed.clear()
            failed = [(k, e) for k, e in self._failed.items() if k[0] == 0 and k[1] < end]
            if failed:
                key, error = min(failed)
                raise CompactionError(f"Cannot start turn: message {key[1]} is unsummarized ({error}). "
                                      "Fix the provider and retry, or run compact.")
            await self._changed.wait()

    async def drain(self):
        """Wait until nothing is queued or running; raise if anything failed."""
        if not self._queued and not self._active and not self._failed and not self.checkpoint_error:
            return
        self.start()
        while self._queued or self._active:
            self._changed.clear()
            # Failed early leaves can intentionally block later work.
            if not self._active and self._failed and not self._parents and self._leaves:
                if self._earlier_unfinished_leaves(self._leaves[0][1]) >= self.config.concurrency:
                    break
            await self._changed.wait()
        if self._failed:
            raise CompactionError(f"{len(self._failed)} compaction(s) failed; run compact to retry")
        if self.checkpoint_error:
            raise CompactionError(f"Saving the memory view failed ({self.checkpoint_error}); "
                                  "run compact to retry. Restart also recovers it.")

    async def close(self):
        self._closed = True
        if self._runner:
            self._runner.cancel()
        tasks = list(self._active.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, *([self._runner] if self._runner else []), return_exceptions=True)
