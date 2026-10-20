# EternalChat

A local terminal chat whose conversation is its memory. Append-only message logs
feed a background binary tree of summaries. Every fresh turn receives a saved,
size-budgeted view; `zoom` retrieves details down to the original message.

Implements the [UniiChat design](https://gist.github.com/VictorTaelin/91837951a5ce5b38f341ec1ba1df6449),
revision `3c190e06f34aba0c69f49042c526093269604935` (2026-10-08).
[OptMem](https://github.com/VictorTaelin/OptMem) is the related memory-tool example.

## Run

Requires **Python 3.11+ on macOS or Linux**, with no runtime dependencies.
Install once from this checkout, so `eternalchat` works in any terminal:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
ln -s "$PWD/.venv/bin/eternalchat" ~/.local/bin/eternalchat   # any folder on your PATH
```

Then:

```sh
eternalchat setup     # once: connection, models and chat folder, saved for every run
eternalchat           # chat in the terminal; the first run starts setup by itself
eternalchat web       # or chat in your browser
```

`setup` asks how to reach the model (an API key, or a Claude Code or Codex
subscription), checks that the key or CLI is there, and saves your answers in
`~/.eternalchat/config.json` (`ETERNALCHAT_HOME` moves it). API keys are never
saved: keep `export ANTHROPIC_API_KEY=...` in your shell profile. Run `setup`
again to change anything; Enter keeps each current answer.

### In your browser

```sh
eternalchat web       # the same chat as a page in your browser
```

`eternalchat web` serves the chat on this computer only (`127.0.0.1:8765`;
`--port` changes it, `--no-browser` only prints the link) and opens it.

The log is the memory itself, as the model reads it. Old stretches appear as
summary slips, stacked by how many messages they hold. Click one and it opens
into its two halves, and those open further, down to the original words, with
a bar at the top that folds them again. Recent messages, and any not yet
summarized, show in full, with day markers between them. Each message has
Copy and **Memory**, which shows the one line the model keeps for it. The
model's look-ups (`zoom`, `date`) fold into one "Looked back" line that opens
to show what it read.

While it replies, the page streams its thinking (never saved; afterwards it
folds into "Thought for 8s"), then the reply as it is written. The send button
turns into Stop. Anything you type meanwhile joins the reply in progress.
Images can be attached with +, by paste, or by drag and drop. Dictation uses
the browser's own speech recognition, where one exists.

Typing `/` lists the commands: `/note`, `/agent Name task`, `/zoom 12` (opens
the summaries that hold message 12 and shows it), `/date`, `/memory`,
`/settings`, `/stop`, `/compact`. Each agent you start gets a tab with its live
transcript, and its report arrives in the chat. **Memory** (the layers icon)
shows the whole chat as a band from oldest to newest, how much of the 128 KB
view it fills, summary progress, and this session's tokens, cache reads
included. **Settings** switches between the API, Claude Code and Codex, along
with the models and reasoning effort, and saves the choice. It also holds the
light, dark or system theme, reply notifications, and a QR code that opens the
chat on your phone.

The page is a view of the chat, like `chat` in a terminal, and the one-writer
lock still applies: close a terminal session before opening the page, or the
other way round. Its link carries a secret from `~/.eternalchat/web-token`
(readable only by you): anyone with the link can use your chat, so keep it
private, and delete the file to make a new one. Each browser keeps the secret
until Settings → Forget this device. Requests must also name this server as
their host, so other websites open in your browser can neither read the chat
nor post to it.

**Phones, tablets and other computers at home:** serve it over HTTPS with
Tailscale and install it as an app. [docs/home-server.md](docs/home-server.md)
walks through it, including keeping it running as a LaunchAgent:

```sh
tailscale serve --bg 8765
eternalchat web --no-browser --allow-host HOST.TAILNET.ts.net
```

**Anywhere, without a computer left on:** run it on Fly.io. It stops when no
page is open and starts again on the next visit, and it works with an API key or
with your Codex or Claude Code subscription. [docs/fly.md](docs/fly.md) has the
three steps:

```sh
deploy/fly.sh up eternalchat-yourname ams    # create, build, deploy; prints your link
deploy/fly.sh login codex                    # or: login claude
deploy/fly.sh doctor                         # check every part
```

### Offline demo

Try it offline first, in a chat of its own; nothing is charged:

```sh
eternalchat --data-dir /tmp/eternalchat-demo --demo ask 'Remember amber-42.'
eternalchat --data-dir /tmp/eternalchat-demo --demo ask '/recall 0'
eternalchat --data-dir /tmp/eternalchat-demo status
```

Demo replies and summaries are deterministic placeholders, not an LLM. They
exercise persistence and tool use without establishing memory quality. Keep demo
and real conversations in separate data directories. Without installing,
`python3 -m eternalchat` runs the same program from this checkout.

`eternalchat -h` lists commands, options and examples; `eternalchat COMMAND -h`
explains one command. Options may go before or after the command. An option
overrides its environment variable (`ETERNALCHAT_CONNECTION`, `ETERNALCHAT_CLI`,
`ETERNALCHAT_MODEL`, `ETERNALCHAT_SUMMARY_MODEL`, `ETERNALCHAT_DIR`), which
overrides the saved setting. The chat lives in the saved folder, by default
`~/.eternalchat/chat`, from any working directory; `--data-dir` selects another
chat. A chat in `./.eternalchat`, the default of earlier versions, is still used
when found in the working folder and nothing else is chosen; `setup` offers to
keep it as the one chat folder.

One process writes to a chat at a time. `view`, `zoom`, `date` and `status` open
it read-only: they never write, may run alongside each other, and fail rather
than create a folder that holds no chat. While a session is open, use `/status`,
`/view` and `/zoom` inside it.

## Connection options

The default is **API** (`--connection api`), using `ANTHROPIC_API_KEY`.
The second option is **Subscription**, using an installed CLI and its existing
account login:

```sh
# Sign in once with the client you want to use:
codex login
claude auth login

# Then choose it in EternalChat:
python3 -m eternalchat --connection subscription --cli codex chat
python3 -m eternalchat --connection subscription --cli claudecode chat
```

Use the official Codex CLI or Claude Code (`claude`). Grok Build is no longer
supported: measured with Grok Build 1.0.62, it reused nothing of EternalChat's
request from cache, not even of an identical one, so every call paid for the
whole view.
Your account must have access to the selected CLI. CLI usage consumes its account
allowance; EternalChat does not supply a subscription. It leaves login and token
refresh to the CLI and removes API-key environment variables from child calls.
Codex is forced to use ChatGPT login. Check Claude account configuration in
Claude Code; locally configured providers and billing remain CLI-controlled.
See [Claude Code headless mode](https://code.claude.com/docs/en/headless).

Set `ETERNALCHAT_CONNECTION=subscription` and `ETERNALCHAT_CLI=codex` (or
`claudecode`) to save your choice in your shell environment. API remains
the default when no connection is selected. `--demo` still runs offline.

Subscription mode uses the CLI's default model unless `--model ID` is supplied;
`--summary-model ID` optionally selects a different summary model. Otherwise
summaries use the reply model. `ETERNALCHAT_MODEL` and
`ETERNALCHAT_SUMMARY_MODEL` apply in either mode: clear API-specific values when
switching clients. `--cli-executable PATH` overrides executable discovery, and
`--cli-timeout SECONDS` changes the 300-second per-call timeout.
`--reasoning-effort LEVEL` selects a level supported by that client and model
(for example `medium` or `high`); `--show-thinking` displays the reasoning events
the client actually provides. Displaying them does not change the reasoning
effort, and they are never saved to chat history.

Replies, summaries, and explicitly requested EternalChat agents all use the
selected client. Each request starts a fresh CLI process in a temporary folder,
with EternalChat's saved view and conversation; the app executes `zoom` and `date`
calls and supplies their results on the next request. Requests default to one
at a time. `--cli-concurrency N` allows 1–8 simultaneous calls within your account
limits. Replies take priority, followed by summaries of original messages needed
for memory, agents, and merges of older summaries. A conversation reserves one
slot across its memory-tool loop; active calls finish normally. Memory needed
for a waiting reply also moves ahead of queued older merges while its context
fits the configured budget. No coding-tool
approval bypass is used.
CLI settings may still impose organization policies or run client integrations.

Subscription mode supports images with both clients. Use `ask --image`
(repeatable) or `/attach PATH` inside `chat`; PNG, JPEG, GIF and WebP files up to
5 MB each use the same durable storage as API mode. `zoom` returns the original
images to the selected client, including after restarting or switching
connections. Select a model that supports image input.

```sh
python3 -m eternalchat --connection subscription --cli codex \
  ask 'Describe this picture' --image 'photo with spaces.png'
```

The adapter sends actual images through each CLI's native input:
[Codex app-server image inputs](https://learn.chatgpt.com/docs/app-server)
and [Claude Code streaming image blocks](https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode).
Numbered references preserve the images' positions in the conversation and tool
results. Temporary input files are removed when the call ends, fails, or is
cancelled; image data is never placed on the command line.

Replies stream while the model is generating them. The adapter decodes just the
reply text from its JSON contract; tool arguments are validated and executed only
after the final result succeeds. CLI reasoning events are available with
`--show-thinking`. Native token and cache counts feed `/status` and `--verbose`,
counted once per call; missing counters stay absent. Codex also reports its
subscription usage windows and reset timestamps in `/status`. Counters describe
the client's model requests, including its own instructions, and are not a bill.

`--max-tokens N` is enforced by API mode. Claude Code receives it through its
native [`CLAUDE_CODE_MAX_OUTPUT_TOKENS`](https://code.claude.com/docs/en/env-vars)
setting, which applies to most requests and respects the model's cap. Codex
uses a prompt budget because its current adapter interface does not expose an
equivalent per-request setting. Caching is the client's, with one exception:
Claude Code receives the request as text blocks split before each message block,
with a one-hour cache mark where API mode marks the view. Claude Code marks
later blocks for an hour, and the API rejects a five-minute mark before them.
Measured with Claude Code 2.1.304 on a 47 KB view, the next call with four more
lines read 20,896 tokens from cache and wrote 1,155; as one block it read 2,348
and wrote 19,715. Codex's cache reuse is not yet measured. Login, quota, model/configuration, protocol and timeout errors have
distinct messages. An interrupted stream saves the text received so far marked
as incomplete; reasoning is never saved. Failures surface without
silently switching to API billing. Completed history stays in the same data
folder when switching connections. CLI-native retry and extra-usage settings
remain controlled by the client/account; the adapter does not retry model calls.

```sh
python3 -m eternalchat --connection subscription --cli codex \
  --show-thinking --verbose --reasoning-effort medium chat
```

An optional live verification uses a temporary chat and your account allowance:

```sh
python3 scripts/subscription_smoke.py codex
python3 scripts/subscription_smoke.py claudecode
# Also verify image understanding and zoom retrieval after restarting:
python3 scripts/subscription_smoke.py codex --images --streaming
python3 scripts/subscription_smoke.py claudecode --images --streaming
```

## The computer

The model can work on the computer that runs EternalChat, as UniiChat's does:
it runs commands (`shell`), reads files (`read`) and writes or edits them
(`write`, `edit`), in every connection mode. Each caller works in its own
folder under `~/.eternalchat/work` (`--work` moves it): `main` for the chat, an
agent's name for an agent. Relative paths point there; any path works.

```sh
eternalchat --tools auto   # the default: the model runs commands as it works
eternalchat --tools off    # memory only: nothing runs on the computer
```

The page shows each command and its output, every file read, and every change
as a diff; the terminal prints each call as it starts. Stop ends a running
command and everything it started. Settings → *Commands and files* switches
it, and the choice is saved.

**What this means for safety.** Commands run with your account's permissions,
and nothing asks first: like UniiChat, it is no sandbox. Anyone who has the
page's link can make the model run any command on this computer, so keep the
link private, prefer Tailscale's HTTPS address to plain HTTP on your network
(the page warns when commands are on and it isn't), and choose Off when you
only want to talk.

## Goals

`/goal TEXT` (in the page or the terminal chat) sends TEXT and keeps the chat
working on it: after each reply, a short check without tools asks the model
whether the chat shows the goal fully met. If not, the message "The goal isn't
satisfied yet: keep working." starts another turn; once it is, the note "Goal
met." ends the work. After 30 such turns in a row the goal waits for your next
message instead of spending more of your account. `/halt` drops the goal (a
reply in progress goes on); the page shows the goal at the top, with Halt. The
goal is saved with the chat.

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
ordinary message. A wrong or unknown command prints its usage and sends nothing;
start a line with `//` to send text that begins with `/`. With `--demo`,
`/recall ID` exercises the actual model-tool-result loop.

Every input is logged the moment it arrives, and its first ID is the historical
cutoff of the view it is answered from. Input received while a turn waits for
summaries joins that turn's first request. Input received during a call is
delivered after its tool results. If the response ends without another tool boundary, or
the tool-round limit stops the turn, queued input starts a fresh turn using a view
ending before its ID. Output logged after that queued input is available to
subsequent views, outside this earlier historical cutoff.

Subagents start when you ask for them: with `/agent Name task`, or in words
("send two agents to compare X and Y"), when the model starts them with its
`spawn` tool, naming each (FireAnt) and choosing its effort where the connection
allows. Each works alone in the background with the chat's memory and the
computer's tools, in its own working folder; at most eight work at once. The
model's `tell` sends one a message: a working agent reads it between its tool
calls, and a finished one goes on from its transcript, its reply arriving as a
`[Name]` message. Inside its own job, an agent first waits until every message
before its request is summarized, so the parent never stalls and the agent never
works from a stale view. Its steps stay in separate transcripts; its report enters
the main chat automatically, once, even after a crash.
Names are permanent and start with a letter, so a numeric `zoom` target is always
a message (agents saved by 0.1.0 under other names stay readable). EOF and `/quit` finish submitted turns and compaction, then cancel
unfinished agent jobs. Ctrl-C keeps all logged input and completed output; a reply
cut off mid-stream is saved marked `[interrupted: this reply did not finish]`.
Interrupted jobs are never automatically rerun; completed but undelivered reports
recover when the next `chat` or `ask` session starts.

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
of them begins its response, then all proceed together; for 270 seconds after
any response with that prefix begins (a cache read restarts the provider's five
minutes, as a write does) the prefix counts as cached and calls go at once. No keep-alive calls are
sent. Redirects are refused, so the API key is only ever sent to the configured
endpoint. Errors report the HTTP status and API error type only, never bodies or
messages, which may echo request content. **Distinct models have distinct
caches**; select the same turn/summary model to permit sharing. Actual cache hit
rates and minimum cacheable lengths are provider-controlled; `--verbose` displays
returned usage; `status` totals it separately for turns, agents and summaries. References: [caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching),
[streaming](https://platform.claude.com/docs/en/build-with-claude/streaming).

## Storage and recovery

```text
~/.eternalchat/
  config.json           saved settings (eternalchat setup)
~/.eternalchat/chat/    the chat folder (--data-dir)
  main/YYYY-MM-DD.jsonl  {i, kind, text, size, date, ...optional metadata}
  tree/YYYY-MM-DD.jsonl  {l, i, text, size}
  view.json             chronological [l, i] pairs
  compaction-view.json  smaller summary-context partition
  view-state.json       pending-batch flags
  assets/<sha256>       original image bytes
  agents/<Name>/        state.json (status, task, report outbox) and main/ transcript
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
  up to their source boundary. Ready parent summaries run before more leaves,
  so bulk imports can shrink their context as they progress.
  Oversized summaries get five attempts and the
  shortest nonempty result is retained, even if over 512 bytes. Actual byte
  sizes govern merging. Empty or tool-using answers are never a summary: the
  same request is asked again, within the five attempts. If the model refuses,
  or never answers with a line, the node keeps its source (children on one
  line each) cut to 512 bytes, as UniiChat does, and reports
  `compaction_error`. Asked again it would fail alike, and every later turn
  would wait on that node.
- Fresh turns wait for all prior leaves; no placeholders enter model input.
  Provider failures surface explicitly. New input or `compact` retries failed
  summaries. A failed retry keeps the shortest line already written. View targets
  can be exceeded while compaction lags. A conservative 180,000-byte request text
  budget (`Config.request_bytes`) applies to turns and every summary attempt.
  Excessive requests fail visibly with the source saved; a summary retry that
  exceeds the budget keeps its shortest usable result. Split large requests
  instead of relying on silent truncation.
- Non-tool text is losslessly split into consecutive records of at most 30,000
  bytes. Tool output is clipped to head/tail, at most 30,000 **characters** with
  a marker. Original text retrieval uses 12,000-character pages. PNG/JPEG/GIF/WebP
  images up to 5 MB are separate durable assets, returned on page 0. Assets are
  written atomically under their SHA-256; a corrupt copy is replaced with the
  verified bytes when the same image is added again. Only regular files are read,
  without blocking. Attachment references are validated (digest, and a media
  type matching the bytes) before any record mentions them.
- Appends are flushed and fsynced; nodes precede references to them. Views use
  atomic replacement plus directory fsync. Merge intent precedes partition
  updates, and pending flags are cleared only after both partitions are saved. A
  view save that fails is reported (`status` shows `view_saved` and
  `view_save_error`) and retried by the next update, `compact` or input; the node
  built before it still counts, so its parent proceeds.
  Uncertain log writes, including ones interrupted by Ctrl-C, disable further
  writes until reopening, so no message ID is ever handed out twice.
- Startup validates IDs, field types, sizes, child references and assets, then
  indexes file offsets and sizes; view byte budgets are computed from the index
  without reading files. Live summary work uses readiness queues, never repeated
  full-tree scans. Indices grow with history; original message content remains
  on disk.

Restart loads the saved view exactly, appending only built suffix leaves whose
checkpoint was interrupted. It reuses existing nodes and queues missing work.
Agent-report event IDs make interrupted chunk delivery idempotent.

Back up the **entire data directory while the chat is closed**, including views,
state, assets and agent transcripts. Torn/malformed records, missing snapshots,
children or assets fail visibly: restore a verified complete backup. The app does
not silently truncate logs or refit a lost view. A leftover `.lock` filename is
harmless; the OS releases its lock when the process exits. Writers lock both
`.lock` and the folder itself, so readers exclude them even in a restored copy
that lacks `.lock`.

Summaries are lossy and can omit retrieval clues: durable originals alone do not
guarantee perfect recall. This release includes memory tools and a terminal UI;
remote devices, shell execution and computer-use tools are not included.

## Development

```sh
python3 -m unittest discover -v
python3 scripts/smoke.py
```

Tests use scripted models and a local HTTP/SSE server, with no paid API calls.
`tests/test_architecture.py` enforces the dependency rule described in
[the architecture notes](docs/architecture.md).
They cover crashes, locking, images, Unicode, 20,001-prefix rollback equivalence,
long-history budgets/queues, streaming failures, tool ordering with live input,
fresh turns, agent-report recovery, read-only inspection, CLI usage errors and
cross-process CLI persistence. The HTTP
test needs permission to bind to `127.0.0.1`. Smoke prints structured phase evidence.
Live model quality, cost and cache hit rates are not established by these tests.

The web page is one file, `eternalchat/web.html`: plain HTML, CSS and
JavaScript, with no build step and no outside requests. Its install files
(manifest, service worker, icons) are in `eternalchat/static/`, and
`python3 scripts/make_icons.py` redraws the icons from one geometry.

See [design decisions and source coverage](docs/design.md). Work is tracked in
`.beads/`; `br list --all`, `br dep cycles`, and `br sync --flush-only` inspect/export it.
