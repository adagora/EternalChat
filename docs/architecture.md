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
| adapters | `storage` | every file name, record format, lock, fsync and the order of writes |
| | `provider` | Anthropic HTTPS/SSE, cache marks, same-prefix waiting, error hygiene |
| | `cli_provider` | subscription CLI subprocesses, native image input, response protocol, timeout and cancellation |
| | `cli_protocol` | JSON reply text decoding, native stream frames and token normalization; a helper of the CLI adapter |
| | `demo` | the deterministic offline provider |
| | `machine` | the computer's tools for the model: shell, read, write and edit, each caller in a working folder of its own, a process group per command |
| composition | `app` | `open_chat()`: builds a `Store` and gives it, with a provider and optionally a `Machine`, to `Engine` |
| | `settings` | `~/.eternalchat/config.json`: the options `setup` saved, below the environment and command line |
| | `cli` | argument parsing, the interactive session and printing |
| | `web` | `eternalchat web`: the chat over HTTP on 127.0.0.1 for one browser page (`web.html`), with a token and Host check |

## Rules

- **Core does no IO.** No core module imports `os`, `pathlib`, `urllib`,
  `threading` or an adapter. `Engine`, `Compactor` and `View` receive the store
  and provider objects they use; tests pass scripted providers and real stores
  in temporary folders.
- **Adapters depend inward.** `storage` imports `config`, `errors` and `tree`.
  `provider` and `demo` import `llm` and `errors`. Neither knows about `Engine`.
  `cli_provider` also imports its pure wire-decoding helper `cli_protocol`.
  `machine` imports only `config`; the engine awaits it for the device tools in
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
- writing: `append`, `add_node`, `add_image`, `write_state`;
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

## Read-only opening

`open_chat(root, readonly=True)` needs no provider. It takes a shared lock, so
readers run alongside each other but not alongside a writer. It never creates a
folder or rewrites a view, and refuses writes with `StoreError`. The CLI uses it
for `view`, `zoom`, `date` and `status`.
