# EternalChat

A local terminal chat whose conversation is its memory. Append-only message logs
feed a background binary tree of summaries. Every fresh turn receives a saved,
size-budgeted view; `zoom` retrieves details down to the original message.

Implements the [UniiChat design](https://gist.github.com/VictorTaelin/91837951a5ce5b38f341ec1ba1df6449),
revision `3c190e06f34aba0c69f49042c526093269604935` (2026-10-08).
[OptMem](https://github.com/VictorTaelin/OptMem) is the related memory-tool example.

## Run

Requires **Python 3.11+ on macOS or Linux**, with no runtime dependencies.
From this checkout:

```sh
# Offline demonstration: no credentials or model charges.
python3 -m eternalchat --data-dir /tmp/eternalchat-demo --demo ask 'Remember amber-42.'
python3 -m eternalchat --data-dir /tmp/eternalchat-demo --demo ask '/recall 0'
python3 -m eternalchat --data-dir /tmp/eternalchat-demo status

# Real conversation.
export ANTHROPIC_API_KEY='your-key'
python3 -m eternalchat chat
```

Demo replies and summaries are deterministic placeholders, not an LLM. They
exercise persistence and tool use without establishing memory quality. Keep demo
and real conversations in separate data directories.

Optional installation:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/eternalchat --demo ask 'Hello'
```

Global options go **before** the command. Data defaults to `.eternalchat/` in the
current directory; `--data-dir` or `ETERNALCHAT_DIR` selects another chat. Reuse
that directory to continue it. One process owns a chat at a time, including
inspection commands; use `/status`, `/view`, and `/zoom` within an open session.

## Commands

| Command | Purpose |
| --- | --- |
| `chat` | Interactive chat; input stays responsive during model work |
| `ask 'message'` | One turn, then finish compaction and exit |
| `ask 'describe this' --image picture.png` | Send text and an image |
| `note 'memory'` | Import a memory without asking for a reply |
| `import notes.txt` | Import UTF-8 text as lossless consecutive note records |
| `view` | Print the persisted summary view |
| `zoom 40 8` | Open summary `40+8` into its two children |
| `zoom 40 --page 1` | Read page 1 of original message 40 |
| `zoom Research --page 0` | Read a named subagent's separate transcript |
| `date 40` | Read a message's timestamp |
| `status` | JSON storage, compaction, view and process-local usage metrics |
| `compact` | Retry and finish outstanding summaries |

Inside `chat`: `/help`, `/status`, `/view`, `/zoom ID [N] [PAGE]`, `/date ID`,
`/note TEXT`, `/compact`, `/attach PATH`, `/agent NAME TASK`, `/zoom NAME [PAGE]`,
and `/quit`. Quote paths containing spaces. `/attach` adds an image to your next
ordinary message. Offline `/recall ID` exercises the actual model-tool-result loop.

Input received during a call is logged immediately and delivered after its tool
results. If the response ends without another tool boundary, queued input starts
a fresh turn using a view ending before its ID. Output logged after that queued
input is available to subsequent views, outside this earlier historical cutoff.

Subagents start only through explicit `/agent` requests. Their steps stay in
separate transcripts; a final `[Name]` report enters the main chat automatically.
Names are permanent. EOF and `/quit` finish submitted turns and compaction, then
cancel unfinished agent jobs. Ctrl-C preserves received input and completed output
records. Interrupted jobs are never automatically rerun; completed but undelivered
reports recover when the next `chat` or `ask` session starts.

## Models and instructions

Defaults: `claude-sonnet-4-6` for turns, `claude-haiku-4-5-20251001` for summaries.

```sh
python3 -m eternalchat --model claude-sonnet-4-6 \
  --summary-model claude-haiku-4-5-20251001 \
  --instructions preferences.txt --verbose chat
```

Environment alternatives: `ETERNALCHAT_MODEL`, `ETERNALCHAT_SUMMARY_MODEL`.
Instructions load once at startup. The tools and system prompt stay constant;
changing instructions or models changes cached prefixes. Credentials remain in
the environment. `--show-thinking` displays thinking events if emitted, without
persisting them. This app does not request extended thinking or send `xhigh`.

The Anthropic Messages adapter streams text/tools and uses five-minute cache
marks on the static system prompt, last whole four-line view block, and request
end. Concurrent requests for an identical model/view prefix wait until the first
response begins. No keep-alive calls are sent. **Distinct models have distinct
caches**; select the same turn/summary model to permit sharing. Actual cache hit
rates and minimum cacheable lengths are provider-controlled; `--verbose` displays
returned usage. References: [caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching),
[streaming](https://platform.claude.com/docs/en/build-with-claude/streaming).

## Storage and recovery

```text
.eternalchat/
  main/YYYY-MM-DD.jsonl  {i, kind, text, size, date, ...optional metadata}
  tree/YYYY-MM-DD.jsonl  {l, i, text, size}
  view.json             chronological [l, i] pairs
  compaction-view.json  smaller summary-context partition
  view-state.json       pending-batch flags
  assets/<sha256>       original image bytes
  agents/<Name>/        isolated transcript and durable report outbox
  .lock                 OS advisory lock
```

- Sizes are UTF-8 bytes. Node `(l,i)` covers `2**l` messages from `i * 2**l`.
  `zoom(40,8)` opens node `(3,5)`. Short sources, including kind labels, bypass
  the model; child texts are joined only if their combined size fits 512 bytes.
- Above 128,000 rendered bytes, the main view merges built siblings toward
  64,000, ordered by `(T - last_message) / child_size`, oldest first on ties.
  Missing parents leave a persisted pending batch. The smaller compaction view
  uses 16,000/32,000 bytes and is coarsened anew when the main view merges.
- Up to eight queued compactions run concurrently, seeing only built context
  up to their source boundary. Oversized summaries get five attempts and the
  shortest nonempty result is retained, even if over 512 bytes. Actual byte
  sizes govern merging. Empty/tool-using compactions are rejected.
- Fresh turns wait for all prior leaves; no placeholders enter model input.
  Provider failures surface explicitly. New input or `compact` retries failed
  summaries. View targets can be exceeded while compaction lags. A conservative
  180,000-byte turn text budget rejects excessive requests visibly after saving
  input; split large requests instead of relying on silent truncation.
- Non-tool text is losslessly split into consecutive records of at most 30,000
  bytes. Tool output is clipped to head/tail, at most 30,000 **characters** with
  a marker. Original text retrieval uses 12,000-character pages. PNG/JPEG/GIF/WebP
  images up to 5 MB are separate durable assets, returned on page 0.
- Appends are flushed and fsynced; nodes precede references to them. Views use
  atomic replacement plus directory fsync. Merge intent precedes partition
  updates. Uncertain log writes disable further writes until reopening.
- Startup validates IDs, sizes, child references and assets, then indexes file
  offsets. Live summary work uses readiness queues, never repeated full-tree
  scans. Indices grow with history; original message content remains on disk.

Restart loads the saved view exactly, appending only built suffix leaves whose
checkpoint was interrupted. It reuses existing nodes and queues missing work.
Agent-report event IDs make interrupted chunk delivery idempotent.

Back up the **entire data directory while the chat is closed**, including views,
state, assets and agent transcripts. Torn/malformed records, missing snapshots,
children or assets fail visibly: restore a verified complete backup. The app does
not silently truncate logs or refit a lost view. A leftover `.lock` filename is
harmless; the OS releases its lock when the process exits.

Summaries are lossy and can omit retrieval clues: durable originals alone do not
guarantee perfect recall. This release includes memory tools and a terminal UI;
remote devices, shell execution and computer-use tools are not included.

## Development

```sh
python3 -m unittest discover -v
python3 scripts/smoke.py
```

Tests use scripted models and a local HTTP/SSE server, with no paid API calls.
They cover crashes, locking, images, Unicode, 20,001-prefix rollback equivalence,
long-history budgets/queues, streaming failures, tool ordering with live input,
fresh turns, agent-report recovery and cross-process CLI persistence. The HTTP
test needs permission to bind to `127.0.0.1`. Smoke prints structured phase evidence.
Live model quality, cost and cache hit rates are not established by these tests.

See [design decisions and source coverage](docs/design.md). Work is tracked in
`.beads/`; `br list --all`, `br dep cycles`, and `br sync --flush-only` inspect/export it.
