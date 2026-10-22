# Architecture

EternalChat has three layers. Dependencies point inward, toward the rules that
change least; `tests/test_architecture.py` enforces it.

```text
 composition    cli.py ──► app.py (open_chat), settings.py (saved options), web.py (browser page)
                  │          │
 adapters         │   storage.py   provider.py   demo.py   machine.py   files, network, fakes, this computer
                  │          │          │           │
 core             ▼          ▼          ▼           ▼
                engine.py ─► compaction.py ─► view.py ─► tree.py
                   │              │             │
                   └──► prompt.py, llm.py, config.py, errors.py
```

| Layer | Module | Knows about |
| --- | --- | --- |
| core | `errors` | the error types every layer raises; a refusal is a `ProviderError` the compactor tells apart |
| | `config` | limits, defaults, clock, UTF-8 sizing, splitting and clipping |
| | `tree` | node addressing and the merge choice; pure functions |
| | `llm` | the provider boundary: `Response` and the `Provider` protocol |
| | `prompt` | model-facing text and shapes: system prompt, tools, view blocks, content blocks, request size |
| | `view` | the two saved partitions, their byte budgets and batched merging |
| | `compaction` | which summary to build next, with which context, and retries |
| | `engine` | turns, tools, steering, subagents, status; the API user interfaces call |
| | `screen` | exclusive task leases and action callbacks bound to a device connection, without IO |
| adapters | `storage` | every file name, record format, lock, fsync and the order of writes |
| | `provider` | Anthropic HTTPS/SSE, cache marks, same-prefix waiting, error hygiene |
| | `openai_provider` | Responses HTTPS/SSE, images and native function calls, transient reasoning continuation, token normalization |
| | `push` | optional VAPID/aes128gcm Web Push and APNs ES256 signing, fixed/validated HTTPS endpoints and cancellable curl |
| | `audio` | bounded recorded-file transcription through cancellable curl, independent OpenAI API credentials |
| | `hooks` | serialized local after-turn commands, stripped credentials and process cleanup |
| | `cli_provider` | subscription CLI subprocesses, native image input, response protocol, timeout and cancellation |
| | `cli_protocol` | JSON reply text decoding, native stream frames and token normalization; a helper of the CLI adapter |
| | `demo` | the deterministic offline provider |
| | `machine` | shell, read, write, edit and computer, each caller in a working folder of its own, a process group per command |
| | `computer` | Mac mouse/keyboard actions and screenshots, a lazily compiled Swift helper, serialized desktop actions and cancellation |
| | `devices` | the computers the device tools reach: this one (a `Machine`) and those joined with `eternalchat device`, each a queue of jobs |
| | `history` | past chats read for import: Claude Code and Codex logs, Claude and ChatGPT exports, secrets redacted |
| | `search` | Brave public HTML fetched through cancellable curl, bounded organic results, no API credentials or backend fallback |
| | `device_state` | private enrolled-device credentials, execution receipts, byte/count bounds and single-process file lock |
| composition | `app` | `open_chat()`: builds a `Store` and `Search` and gives them, with a provider and optionally a `Machine`, to `Engine` |
| | `settings` | `~/.eternalchat/config.json`: the options `setup` saved, below the environment and command line |
| | `cli` | argument parsing, the interactive session and printing |
| | `web` | `eternalchat web`: the chat over HTTP on 127.0.0.1 for one browser page (`web.html`), with a token and Host check |
| | `remote` | `eternalchat connect`: authenticated HTTP/SSE terminal view, without a local store or provider |
| | `web_sessions` | hashed browser/computer credentials, single-use pairing, expiry, revocation and owned push subscriptions |
| | `web_imports` | bounded browser-local upload previews, import tasks/progress/cancellation, and session selection |
| | `device_client` | device receipt/authorization/execution/result loop with conservative crash recovery |

## Rules

- **Core does no IO.** No core module imports `os`, `pathlib`, `urllib`,
  `threading` or an adapter. `Engine`, `Compactor` and `View` receive the store
  and provider objects they use; tests pass scripted providers and real stores
  in temporary folders.
- **Adapters depend inward.** `storage` imports `config`, `errors` and `tree`.
  `provider` and `demo` import `llm` and `errors`. Neither knows about `Engine`.
  `cli_provider` also imports its pure wire-decoding helper `cli_protocol`.
  `machine` imports `config` and its `computer` helper; the engine awaits it for the device tools in
  every connection mode, so the CLIs' own tools stay off.
- **One composition root.** Only `app.open_chat` (used by the CLI and the
  tests) chooses concrete classes and keeps `Store.message_bytes` equal to
  `Config.message_bytes`.

## Boundaries

**Store** (`storage.Store`) hides its offset indices and file layout behind a
small surface:
- reading: `message_count`, `node_count`, `has_node`, `node_text`, `node_bytes`,
  `message`, `node`, `node_keys` (startup only), `event_complete`, `image_bytes`,
  `read_state`;
- writing: `append`, `add_node`, `add_image`, `add_image_data`, `write_state`;
- agents: `create_agent`, `agent_state`, `save_agent_state`, `agent_reports`,
  `open_agent_log`, `agent_records`.

`View` names its partitions (`"view"`, `"compaction-view"`, `"view-state"`) and
the store maps them to files.

**Provider** (`llm.Provider`) has one method, `complete(...) -> Response`. It
raises `ProviderError` rather than return partial output, and streams `text`,
`thinking`, `block` and `usage` events to `on_event`. Messages use the Anthropic
content-block shape, the single wire format; `prompt` builds those blocks, so
neither storage nor the UI constructs them.
The optional `purpose` argument identifies a reply (`turn`), an original-message
summary (`memory`), an `agent`, or a background summary merge (`summary`). API and
demo adapters ignore scheduling priority; the subscription adapter uses it for
admission to its bounded queue. Its optional `foreground()` scope reserves one
slot throughout a main conversation, including time spent executing memory
tools, so queued merges cannot jump into the gap between two model calls.

`CLIProvider` extracts images from those blocks, including nested memory tool
results, and replaces their positions in the text envelope with numbered
references. Codex app-server receives temporary image files through `localImage`, and Claude Code
receives native image blocks through JSON stdin. The images stay in encounter order. Temporary
inputs live through the subprocess call and are removed on success, failure,
timeout or cancellation. Codex uses a fresh ephemeral app-server thread, with
ChatGPT authentication checked before model work; Claude Code uses its native
Messages event stream, and receives the prompt split into text blocks with a
one-hour cache mark on the view so later calls reuse it. Only the top-level EternalChat `text` string is
decoded incrementally, including escaped Unicode. Completed native messages and
terminal results reconcile against that prefix to avoid replaying output or
committing a changed reply. Tool calls are emitted only after successful final
validation. Native reasoning goes to transient events; token counts are normalized
to uncached input, cache read/write, output and optional reasoning counters, and
emitted once per call. Raw CLI errors and account identifiers are never echoed.
Claude's output budget is passed as `CLAUDE_CODE_MAX_OUTPUT_TOKENS` in its child
environment, overriding an inherited value without changing the caller's shell.
Codex receives a soft `response_token_budget` in the text envelope.

The OpenAI adapter translates the shared content blocks into Responses inputs.
`Response.message()` carries native output, encrypted reasoning and assistant
phase only within a live tool loop; this continuation never enters saved memory
and cannot be replayed through another model/provider. Responses use `store:false`.
Explicit agent models override the reply model for background agents. API effort
and speed/tier settings apply to replies and agents; summaries keep model defaults.
Unsupported requests fail instead of changing providers. The optional after-turn
adapter serializes local commands, limits each to 30 seconds, and reports hook
failures separately from chat replies. Engine shutdown cancels and reaps hooks.

**Engine** is what user interfaces call:

| Purpose | Methods |
| --- | --- |
| Input | `submit`, `ask`, `note`, `attach`, `start_agent`, `compact` |
| Reading | `zoom`, `date`, `render_view`, `messages`, `image`, `status` (each returns plain data; `zoom` returns a `Retrieval` of text plus image references) |
| Events | the `on_event` callback: text and thinking deltas, status, tool calls, usage, summaries built or failed, agent reports, and `message` with the IDs of each record logged |
| Lifetime | `stop` (the running reply), `finish`, `close` |
| Model-facing | `tool`, which converts a `Retrieval` into API content blocks |

The CLI parses what the user typed and prints what these return; the web page
(`web.py`) serves the same methods over HTTP and passes the events to the
browser. Neither reaches into the store, view or compactor, nor re-derives a rule. For
example, "a numeric zoom target is a message" holds because the storage layer
requires agent names to start with a letter.

Screen tasks use `Engine.start_screen(task, device=...)`. Chat's `computer`
schema starts a task; only the worker's schema accepts actions. Ordinary agents
receive neither schema. `Machine` and `Devices` reserve a `screen.Screen` before
the job is scheduled and return a lease tied to the selected object. The engine
keeps it through the whole worker run and releases it on success, failure or
cancellation, including cancellation before the coroutine starts. Screen workers
receive their task and transcript with a separate system prompt, no memory view,
and only `computer` actions. Runtime dispatch rejects unoffered tools and attempts
to override the selected device. Role and advertised screen identity persist for
`tell`; its accepted messages are appended before returning, even before the job
starts. `stop_agent(name)` stops one worker through the terminal or authenticated
web API. Device joins advertise their screen identity so aliases in this server
share a lease. Remote job cancellation acknowledgements and reconnect replay are
still tracked separately in the parity plan.

**Search** is independent of the model connection and computer tools. The
composition root supplies `Search.run({query, count})` to writable engines;
chat and ordinary agents receive the same schema even without a `Machine`.
The engine logs its JSON results as an echo and supplies that exact text to the
model. The adapter extracts titles, URLs and excerpts from organic web snippets,
in page order, excluding duplicate URLs and non-HTTP links. It runs at most four
requests at once, caps HTML at 2 MB and result text at 24,000 characters, and
kills/reaps curl on stop, timeout or a size failure. It does not read `.curlrc`,
follow redirects or pass model credentials to curl. No alternate backend runs
when the public page fails or requires browser verification. Compactions share
the tool schema for cache consistency but never execute tools.

## Read-only opening

Enrolled device sessions bind the server's persistent name registry to a random
identity and secret hash; the client keeps the secret in a private work-folder
journal. Job IDs and deadlines survive socket replacement in the same `Remote`.
Each stream owns a generation so an old socket cannot expire its successor.
The device persists receipt, obtains an idempotent authorization ACK, then
persists `started` before executing. It persists results before delivery, retries
lost result acknowledgements and ignores duplicate/reordered frames. A started
receipt recovered after a process crash becomes an uncertain failure, never a
repeated action. Stop tombstones replay before jobs and require acknowledgement.
Link loss has a 60-second grace; each job has a 10-minute deadline. Deadlines use
the server's remaining authorization budget less the receipt round trip.
Explicit leave cancels local tasks and ends remote waiters. Server restarts
preserve enrollment but end old waiters; orphaned results remain local receipts
and are not executed again. Journals cap source data at 128 MB and total records
at 4,096, retaining at most 1,024 completed receipts. Legacy clients without
enrollment retain their previous disconnect-fails-jobs semantics.

Browser-local uploads are parsed through `history.read_upload` in a serialized
background thread; client-provided paths are never used for extraction. The web
layer bounds raw upload, aggregate text and message counts and keeps at most four
temporary selections. Import tasks iterate `Engine.import_steps` on the owning
loop, yielding after 20 records, with progress and explicit cancellation. Stable
record event IDs make partial session retries safe even before their session
checkpoint advances. `Store.backup` snapshots only chat state, logs, agents and
assets synchronously on that same loop, so an active model cannot interleave a
write between file copies. It rejects linked folders/files and bounds source
bytes; restored backups need no lock file from the original instance.

`connect URL` uses the server's existing API. Its event subscription requests a
snapshot after stream registration, synchronously on the engine's loop, with the
latest 200 records, status/settings and any partial reply block. Live message
notifications retrieve records only through that notification's last ID, so a
fast model cannot cause saved replies to appear ahead of their streamed deltas.
The terminal reconnects read streams and catches up from saved record IDs; it
never replays a mutation whose acknowledgement may have been lost. Requests use
verified TLS for HTTPS and refuse redirects rather than forward the token.
Cancellation closes network transports, not the server's engine. The transport
handles HTTP chunk framing for SSE behind reverse proxies and bounds responses.

`open_chat(root, readonly=True)` needs no provider. It takes a shared lock, so
readers run alongside each other but not alongside a writer. It never creates a
folder or rewrites a view, and refuses writes with `StoreError`. The CLI uses it
for `view`, `zoom`, `date` and `status`.

Recorded audio uses a separate authenticated `/api/transcribe` request. It never
submits a chat message or stores a recording in memory. The browser reviews the
returned text as a draft. The adapter requires an explicit server OpenAI key,
keeps at most two requests active, bounds audio to 24 MB and responses to 256 KB,
and kills/reaps curl on cancellation, timeout or server shutdown. Temporary audio
lives in a private directory until curl ends. API error bodies are never echoed;
there is no retry, redirect or subscription fallback. Browser disconnect cancels
the request; cancellation does not establish that the vendor stopped billing.

Web sessions use the engine's private `web_auth`/`save_web_auth` state boundary.
Only credential hashes persist, with a private-mode atomic state file excluded
from memory exports. Each request authenticates anew; revocation closes its active
connections. Browser SSE rechecks expiry. The server bootstrap token remains an
administrator credential for local terminal/legacy compatibility and recovery.
Computer-scoped credentials are bound to one name and only its transport routes.

Successful logical turns emit `turn_complete`; the web composition schedules a
generic push independently of browser SSE. Subscriptions belong to sessions and
are removed on revocation. Push is optional, has a coalescing queue and bounded
active sends, and never blocks chat on network delivery. Web Push uses maintained
crypto libraries for aes128gcm/VAPID; APNs uses P-256 ES256 with server-owned team,
key ID, environment and topic. Both use the same cancellable TLS transport, no
redirects or automatic retries, and no model credentials in the curl environment.
The worker opens only the current chat origin and ignores arbitrary payload text
or links. The native client pins its server origin, refuses API redirects and
keeps its session in Keychain. Native code is presently source/shared-code verified;
UIKit compilation and physical-device delivery require external prerequisites.

The native iOS prototype and its Xcode/signing workflow are outside the required
project scope. The supported mobile interface is the browser/PWA; native source
and historical checks remain available for reference.
