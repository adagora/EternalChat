# Design decisions and source coverage

Source: [UniiChat gist](https://gist.github.com/VictorTaelin/91837951a5ce5b38f341ec1ba1df6449/3c190e06f34aba0c69f49042c526093269604935),
revision `3c190e06f34aba0c69f49042c526093269604935`, retrieved 2026-10-08.
Python 3.11+ standard library, macOS/Linux; terminal app and importable `Engine`.

| Gist section | Implementation | Verification |
| --- | --- | --- |
| 1. Log, kinds, one owner, flush, splitting/clipping | `storage.py` | locks, process exit, fsync failure, Unicode, rollover, corruption |
| 2. Tree, combined-source bypass, one-time nodes | `compaction.py`, `view.py` | addressing, threshold, node counts, restart |
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
4. **Queued input.** Log immediately while working; inject after all tool results.
   If the response ends, start a fresh call with historical cutoff at that input's
   first ID. Later logged replies remain retrievable and enter subsequent views.
5. **Cache scope.** Different model IDs do not share entries. Equal tools/system
   permit sharing only with equal models/accounts. Complete four-line blocks are
   stable; opening/closing chat tags are separate content blocks. Real cache
   efficiency remains unmeasured; the source simulation is not a benchmark here.
6. **Provider settings.** Use API keys and the public Messages API, not Claude
   Code subscription credentials. Do not send unsupported `xhigh`. Signed
   thinking stays in volatile within-turn messages only. No automatic retry of
   a failed/ambiguous turn or tool execution; failed summaries retry on input or
   explicit compact. No keep-alive requests.
7. **Optional tools.** Include explicit user-invoked named subagents, but no model
   spawning, remote-device, shell or computer-use tools. Images are durable
   assets with summary clues; zoom returns the originals.
8. **Input limits.** Preserve raw non-tool text in consecutive records; send
   complete user input subject to a conservative request budget. Oversized
   requests fail visibly after logging, with guidance to send smaller requests.

## Durable commit order

1. Lock and validate records, children and assets; index their offsets.
2. Append and fsync messages with permanent sequential IDs. An uncertain write
   makes storage fail-stop, preventing reuse of an ID already on disk.
3. Append/fsync each node once before publishing view references.
4. Save pending merge intent, atomically replace partitions and fsync directory,
   then clear completed intent. Restart can finish interrupted checkpoints.
5. Restore saved partitions, append only uncheckpointed built suffix leaves,
   reuse saved nodes, and queue missing work. Never refit an existing view.
6. Save agent reports before delivery. Event/part metadata on main-log records
   permits idempotent chunk resumption. Already logged reports become historical
   context; parent actions are not rerun automatically after a crash.

Corrupt records, missing snapshots or assets require a verified complete backup.
Automatic destructive repair would violate the append-only and stable-view
contracts. Model summary quality and discovery of omitted facts remain outside
what offline verification can establish.
