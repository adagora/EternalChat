# Design decisions and source coverage

Source: [UniiChat gist](https://gist.github.com/VictorTaelin/91837951a5ce5b38f341ec1ba1df6449/3c190e06f34aba0c69f49042c526093269604935),
revision `3c190e06f34aba0c69f49042c526093269604935`, retrieved 2026-10-08.
Python 3.11+ standard library, macOS/Linux; terminal app and importable `Engine`
(open one with `eternalchat.app.open_chat`). Module layers: [architecture](architecture.md).

| Gist section | Implementation | Verification |
| --- | --- | --- |
| 1. Log, kinds, one owner, flush, splitting/clipping | `storage.py`, `config.py` | locks, process exit, fsync failure, Unicode, rollover, corruption |
| 2. Tree, combined-source bypass, one-time nodes | `tree.py`, `compaction.py`, `view.py` | addressing, threshold, node counts, restart |
| 3. Saved view, rollback merge order, byte budgets | `view.py` | 20,001-prefix equivalence, T=10, pending batches, suffix recovery |
| 3.3 Stable prefixes, four-line blocks, cache marks | `prompt.py`, `provider.py` | immutable requests, prefix gating, usage decoding |
| 4. Smaller context, eight workers, cutoff, retries | `compaction.py` | concurrency, no future context, UTF-8 retries, rejected tools |
| 5. Shared prompt, zoom discipline, truthful summaries | `prompt.py` | equal system/tool snapshots for turns and summaries |
| 6. Fresh calls, dynamic state after view, steering | `engine.py` | input exclusion, multiple tool results, end-turn boundary |
| 6. Zoom/date, originals/images, optional agents | `engine.py`, `storage.py` | pagination, attachments, isolated transcripts, outbox recovery |
| 7. Failure modes | all modules | invariant tests, CLI smoke, independent review |

## Resolved ambiguities

1. **No placeholders.** The source prompt mentions them, but turn/compaction
   sections prohibit partial history. Only completed summaries reach models.
2. **512-byte target.** Short-source bypass is strict. Model output gets a ruler
   and measured retry feedback. After five attempts retain the shortest nonempty
   text, counting its real bytes even when oversized, as the source prescribes.
3. **Compaction lag.** A parent must exist before merging. A saved pending flag
   retains the low target until reachable; no placeholders or arbitrary cuts.
4. **Queued input.** Log every input on arrival; its first ID is the historical
   cutoff of the view it is answered from, so it never appears in its own view.
   Input that arrives while a turn waits for summaries joins that turn's first
   request, since nothing was replied yet. During a call, inject it after all
   tool results. If the response ends, or the
   tool-round limit stops the turn, start a fresh call with that cutoff. Later
   logged replies remain retrievable and enter subsequent views.
5. **Cache scope.** Different model IDs do not share entries. Equal tools/system
   permit sharing only with equal models/accounts. Complete four-line blocks are
   stable; opening/closing chat tags are separate content blocks. Real cache
   efficiency remains unmeasured; the source simulation is not a benchmark here.
6. **Provider settings.** Use API keys and the public Messages API, not Claude
   Code subscription credentials. Do not send unsupported `xhigh`. Signed
   thinking stays in volatile within-turn messages only. No automatic retry of
   a failed/ambiguous turn or tool execution; failed summaries retry on input or
   explicit compact. No keep-alive requests. Same-prefix requests wait for the
   first to start its response, then proceed together. Errors carry the HTTP
   status and API error type only.
7. **Optional tools.** Include explicit user-invoked named subagents, but no model
   spawning, remote-device, shell or computer-use tools. An agent waits, inside
   its own job, for summaries of every message before its request. Images are
   durable assets with summary clues; zoom returns the originals.
8. **Input limits.** Preserve raw non-tool text in consecutive records; send
   complete user input subject to a conservative request budget. Oversized
   requests fail visibly after logging, with guidance to send smaller requests.

## Durable commit order

1. Lock and validate records, children and assets; index their offsets.
2. Append and fsync messages with permanent sequential IDs. An uncertain or
   interrupted write makes storage fail-stop, preventing reuse of an ID already
   on disk. Attachment references are validated first; assets are written
   atomically under their SHA-256.
3. Append/fsync each node once before publishing view references. A node that
   exists counts as built even if the following view save fails.
4. Save pending merge intent, atomically replace partitions and fsync directory,
   then clear completed intent, only once no partition is behind memory. A
   failed save leaves the partition dirty and is reported; the next update,
   input or compact retries it. Restart can finish interrupted checkpoints.
5. Restore saved partitions, append only uncheckpointed built suffix leaves,
   reuse saved nodes, and queue missing work. Never refit an existing view.
6. Save agent reports before delivery. Event/part metadata on main-log records
   permits idempotent chunk resumption. Already logged reports become historical
   context; parent actions are not rerun automatically after a crash.

Corrupt records, missing snapshots or assets require a verified complete backup.
Automatic destructive repair would violate the append-only and stable-view
contracts. Model summary quality and discovery of omitted facts remain outside
what offline verification can establish.
