# Architecture

EternalChat has three layers. Dependencies point inward, toward the rules that
change least; `tests/test_architecture.py` enforces it.

```text
 composition    cli.py ──► app.py (open_chat)
                  │          │
 adapters         │   storage.py   provider.py   demo.py      files, network, fakes
                  │          │          │           │
 core             ▼          ▼          ▼           ▼
                engine.py ─► compaction.py ─► view.py ─► tree.py
                   │              │             │
                   └──► prompt.py, llm.py, config.py, errors.py
```

| Layer | Module | Knows about |
| --- | --- | --- |
| core | `errors` | the three error types every layer raises |
| | `config` | limits, defaults, clock, UTF-8 sizing, splitting and clipping |
| | `tree` | node addressing and the merge choice; pure functions |
| | `llm` | the provider boundary: `Response` and the `Provider` protocol |
| | `prompt` | model-facing text and shapes: system prompt, tools, view blocks, content blocks, request size |
| | `view` | the two saved partitions, their byte budgets and batched merging |
| | `compaction` | which summary to build next, with which context, and retries |
| | `engine` | turns, tools, steering, subagents, status; the API user interfaces call |
| adapters | `storage` | every file name, record format, lock, fsync and the order of writes |
| | `provider` | Anthropic HTTPS/SSE, cache marks, same-prefix waiting, error hygiene |
| | `demo` | the deterministic offline provider |
| composition | `app` | `open_chat()`: builds a `Store` and gives it, with a provider, to `Engine` |
| | `cli` | argument parsing, the interactive session and printing |

## Rules

- **Core does no IO.** No core module imports `os`, `pathlib`, `urllib`,
  `threading` or an adapter. `Engine`, `Compactor` and `View` receive the store
  and provider objects they use; tests pass scripted providers and real stores
  in temporary folders.
- **Adapters depend inward.** `storage` imports `config`, `errors` and `tree`.
  `provider` and `demo` import `llm` and `errors`. Neither knows about `Engine`.
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

**Engine** is what user interfaces call:

| Purpose | Methods |
| --- | --- |
| Input | `submit`, `ask`, `note`, `attach`, `start_agent`, `compact` |
| Reading | `zoom`, `date`, `render_view`, `status` (each returns plain data; `zoom` returns a `Retrieval` of text plus image references) |
| Lifetime | `finish`, `close` |
| Model-facing | `tool`, which converts a `Retrieval` into API content blocks |

The CLI parses what the user typed and prints what these return. It never
reaches into the store, view or compactor, and never re-derives a rule. For
example, "a numeric zoom target is a message" holds because the storage layer
requires agent names to start with a letter.

## Read-only opening

`open_chat(root, readonly=True)` needs no provider. It takes a shared lock, so
readers run alongside each other but not alongside a writer. It never creates a
folder or rewrites a view, and refuses writes with `StoreError`. The CLI uses it
for `view`, `zoom`, `date` and `status`.
