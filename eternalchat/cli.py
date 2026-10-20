"""Terminal interface: argument parsing, the interactive session and printing.

Everything shown comes from Engine methods; this module only parses what the
user types and formats what the engine returns.
"""
import argparse
import asyncio
import codecs
import json
import math
import os
import shlex
import shutil
import stat
import sys
from pathlib import Path

from . import __version__, history, settings, web
from .app import open_chat
from .config import MAX_IMAGE_BYTES, Config
from .demo import DemoProvider
from .machine import Machine
from .prompt import DEVICE_NAMES
from .provider import AnthropicProvider
from .cli_provider import CLIENTS, CLIProvider

INSPECT = frozenset({"view", "status", "zoom", "date"})  # read-only commands

DESCRIPTION = """\
One terminal chat that never ends. Every message is kept on disk, a background
tree of short summaries keeps all of history in a bounded view, and the model
zooms into that tree to recall details."""

EPILOG = """\
examples:
  eternalchat setup                             once: choose connection, models, chat folder
  eternalchat                                   talk (the same as 'chat'; the first run
                                               starts setup); type /help inside
  eternalchat web                               the same chat in your browser
  eternalchat --demo ask 'Remember amber-42.'   try it offline: no API key, no charges
  eternalchat --demo ask '/recall 0'            the demo model zooms into message 0
  eternalchat --connection subscription --cli codex chat
                                               another connection for this run only
  eternalchat zoom 40 8                         open summary 40+8 into its two halves
  eternalchat zoom 40 --page 1                  read page 1 of message 40
  eternalchat status --data-dir ~/work-chat     inspect another chat

Options override the environment, which overrides the settings 'setup' saved.
The chat lives in the saved folder (default: ~/.eternalchat/chat). Options may go
before or after the command. One process writes to a chat at a time; view,
zoom, date and status only read. 'eternalchat COMMAND -h' shows details.
Exit status: 0 ok, 1 error, 2 usage error, 130 interrupted."""

# One table for /help and 'chat -h'. /recall is added in demo mode only.
SLASH = (
    ("/help", "", "list these commands"),
    ("/status", "", "counts, summary progress and token usage"),
    ("/view", "", "the saved memory view"),
    ("/zoom", "ID [N] [PAGE]", "open summary ID+N, or read message ID"),
    ("/zoom", "NAME [PAGE]", "read subagent NAME's transcript"),
    ("/date", "ID", "when message ID was recorded"),
    ("/note", "TEXT", "save a memory without asking for a reply"),
    ("/attach", "PATH", "add an image to your next message"),
    ("/agent", "NAME TASK", "start a background subagent; its report arrives by itself"),
    ("/goal", "TEXT", "send TEXT and keep working until a check finds it done"),
    ("/halt", "", "drop the goal; a reply in progress goes on"),
    ("/compact", "", "retry failed summaries"),
    ("/quit", "", "finish submitted turns and summaries, then exit (also /exit, Ctrl-D)"),
)
DEMO_SLASH = (("/recall", "ID", "demo only: the offline model calls zoom on message ID"),)
SLASH_NOTES = """\
Type while the model works to steer it: your line is saved at once and reaches
the model after its next tool results. Start a line with // to send text that
begins with /. Ctrl-C stops at once; everything already saved stays saved."""


def slash_help(demo=False):
    rows = SLASH + (DEMO_SLASH if demo else ())
    width = max(len(f"{name} {args}".rstrip()) for name, args, _ in rows)
    lines = [f"  {f'{name} {args}'.rstrip():<{width}}  {text}" for name, args, text in rows]
    return "\n".join(lines) + "\n\n" + SLASH_NOTES


GLOBAL_NOTE = ("Global options (--data-dir, --demo, --model, --verbose, ...) also work here;\n"
               "see 'eternalchat -h'.")


class UsageError(ValueError):
    pass


# Argument parsing --------------------------------------------------------------------

class Formatter(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog):
        super().__init__(prog, max_help_position=24, width=min(100, max(60, _columns())))

    def _format_action(self, action):
        # List commands directly, without argparse's extra "COMMAND" header line.
        # Uses argparse internals; if they ever change, the default layout remains.
        if isinstance(action, getattr(argparse, "_SubParsersAction", ())) and hasattr(action, "_get_subactions"):
            return "".join(self._format_action(choice) for choice in action._get_subactions())
        return super()._format_action(action)


def _columns():
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 80


def _global_options(p, suppress, saved):
    """Options every command accepts, before or after its name. Subcommand copies
    use SUPPRESS defaults, so they never reset an option given before the
    command, and hidden help, so each command's -h stays about that command."""
    def default(value):
        return argparse.SUPPRESS if suppress else value

    def initial(env, key, fallback=None):
        # Below an option given on the command line: its environment variable,
        # then the saved setting, then the built-in default.
        return default((os.environ.get(env) if env else None) or saved.get(key) or fallback)

    def describe(text):
        return argparse.SUPPRESS if suppress else text

    chat = p.add_argument_group("chat")
    chat.add_argument("--data-dir", metavar="DIR", default=initial("ETERNALCHAT_DIR", "data_dir"),
                      help=describe("chat folder (default: the saved one, else ~/.eternalchat/chat; env ETERNALCHAT_DIR)"))
    chat.add_argument("--demo", action="store_true", default=default(False),
                      help=describe("offline demo with placeholder replies: no API key, no charges"))
    connection = p.add_argument_group("connection")
    connection.add_argument("--connection", choices=("api", "subscription"),
                           default=initial("ETERNALCHAT_CONNECTION", "connection", "api"),
                           help=describe("API key or an installed CLI subscription login "
                                         "(default: the saved one, else api; env ETERNALCHAT_CONNECTION)"))
    connection.add_argument("--cli", choices=("codex", "claudecode"),
                           default=initial("ETERNALCHAT_CLI", "cli", "codex"),
                           help=describe("subscription client (default: the saved one, else codex; env ETERNALCHAT_CLI)"))
    connection.add_argument("--cli-executable", default=default(None), metavar="PATH",
                           help=describe("override the selected CLI executable"))
    connection.add_argument("--cli-timeout", type=float, default=default(300), metavar="SECONDS",
                           help=describe("per-call subscription timeout (default: 300)"))
    connection.add_argument("--cli-concurrency", type=int, default=default(1), metavar="N",
                           help=describe("simultaneous subscription calls, 1–8 (default: 1; replies have priority)"))
    models = p.add_argument_group("models")
    models.add_argument("--model", metavar="ID", default=initial("ETERNALCHAT_MODEL", "model"),
                        help=describe(f"model for replies (subscription: CLI default; API: {Config.turn_model}; env ETERNALCHAT_MODEL)"))
    models.add_argument("--summary-model", metavar="ID",
                        default=initial("ETERNALCHAT_SUMMARY_MODEL", "summary_model"),
                        help=describe(f"model for summaries (subscription: reply model; API: {Config.summary_model}; env ETERNALCHAT_SUMMARY_MODEL)"))
    computer = p.add_argument_group("computer")
    computer.add_argument("--tools", choices=("auto", "off"), default=initial("ETERNALCHAT_TOOLS", "tools", "auto"),
                          help=describe("shell, read, write and edit on this computer: auto runs them as the model "
                                        "asks, off leaves only memory tools (default: the saved one, else auto; "
                                        "env ETERNALCHAT_TOOLS)"))
    computer.add_argument("--work", metavar="DIR", default=initial("ETERNALCHAT_WORK", "work"),
                          help=describe("where commands run and relative paths point, a folder per caller "
                                        "(default: the saved one, else ~/.eternalchat/work; env ETERNALCHAT_WORK)"))
    models.add_argument("--reasoning-effort", default=initial(None, "reasoning_effort"), metavar="LEVEL",
                        help=describe("subscription reasoning effort supported by the chosen client/model"))
    models.add_argument("--max-tokens", type=int, default=default(Config.max_tokens), metavar="N",
                        help=describe("token budget (API: enforced; Claude CLI: native; Codex: prompt budget)"))
    models.add_argument("--instructions", metavar="FILE", type=Path, default=initial(None, "instructions"),
                        help=describe("UTF-8 file of standing preferences, read once at startup"))
    output = p.add_argument_group("output")
    output.add_argument("--verbose", action="store_true", default=default(False),
                        help=describe("also print summary progress, tool calls and token usage"))
    output.add_argument("--show-thinking", action="store_true", default=default(False),
                        help=describe("print model thinking to stderr (never saved)"))


def parser(saved=None):
    """The argument parser; `saved` holds the settings 'setup' saved (none: built-in defaults)."""
    saved = saved or {}
    common = argparse.ArgumentParser(add_help=False)
    _global_options(common, suppress=True, saved=saved)
    p = argparse.ArgumentParser(prog="eternalchat", usage="%(prog)s [options] [COMMAND] [ARGS]",
                                description=DESCRIPTION, epilog=EPILOG, formatter_class=Formatter)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", title="commands", metavar="COMMAND")
    _global_options(p, suppress=False, saved=saved)

    def command(name, summary, description, epilog=None):
        epilog = (epilog + "\n\n" if epilog else "") + GLOBAL_NOTE
        return sub.add_parser(name, help=summary, description=description, epilog=epilog,
                              parents=[common], formatter_class=Formatter)

    command("chat", "talk interactively (type /help inside)",
            "Talk interactively. Input stays responsive while the model works.",
            "in-chat commands:\n" + slash_help(demo=True))
    page = command("web", "open the chat in your browser",
                   "Serve the chat to your browser, on this computer only (127.0.0.1), and open\n"
                   "it. The page is a view of the chat, like 'chat' in a terminal; Ctrl-C here\n"
                   "stops it, and everything already saved stays saved. Its link carries a secret\n"
                   "kept in ~/.eternalchat/web-token: anyone with the link can use your chat.",
                   "other devices at home:\n"
                   "  tailscale serve --bg 8765        HTTPS for your phones and tablets, then\n"
                   "  eternalchat web --allow-host NAME.TAILNET.ts.net\n"
                   "  eternalchat web --host 0.0.0.0   or plain HTTP on a trusted network only")
    page.add_argument("--port", metavar="PORT", type=int, default=8765, help="port to serve on (default: 8765)")
    page.add_argument("--host", metavar="ADDR", default="127.0.0.1",
                      help="address to listen on (default: 127.0.0.1, this computer only; 0.0.0.0: every network)")
    page.add_argument("--allow-host", metavar="NAME", action="append", default=[],
                      help="also answer requests for this host name, e.g. an HTTPS proxy's (repeatable)")
    page.add_argument("--no-browser", action="store_true", help="print the link without opening a browser")
    command("setup", "choose the connection, models and chat folder, and save them",
            "Ask a few questions and save the answers in ~/.eternalchat/config.json\n"
            "($ETERNALCHAT_HOME/config.json), so a plain 'eternalchat' needs no options.\n"
            "Options given with setup are saved too. API keys are never saved: keep\n"
            "ANTHROPIC_API_KEY in your shell profile.")
    ask = command("ask", "send one message, print the reply, then exit",
                  "Send one message and print the reply. Background summaries finish before\n"
                  "exiting, so the next run starts at once.",
                  "examples:\n  eternalchat ask 'What did we decide about the cache?'\n"
                  "  eternalchat ask 'Describe this' --image photo.jpg")
    ask.add_argument("text", metavar="TEXT", help="the message")
    ask.add_argument("--image", metavar="FILE", type=Path, action="append", default=[],
                     help=f"attach a PNG, JPEG, GIF or WebP image up to {MAX_IMAGE_BYTES // 1_000_000} MB (repeatable)")
    note = command("note", "save a memory without asking for a reply",
                   "Save TEXT as a note message. No turn starts; it is summarized like any message.")
    note.add_argument("text", metavar="TEXT", help="the memory to save")
    imp = command("import", "save a UTF-8 text file as note messages",
                  "Save a UTF-8 text file as notes. Nothing is cut: long text becomes consecutive\n"
                  f"messages of at most {Config.message_bytes:,} bytes.")
    imp.add_argument("file", metavar="FILE", type=Path, help="UTF-8 text file")
    past = command("import-chats", "bring past Claude Code, Codex, Claude and ChatGPT chats into memory",
                   "Read past chats and add them to this one, each session as a note naming it, then\n"
                   "its messages with their own dates. By default it reads Claude Code's logs\n"
                   "(~/.claude/projects) and Codex's (~/.codex/sessions); give PATHs for other\n"
                   "folders or for a website's export (its .zip or conversations.json). It shows what\n"
                   "it found and the summary calls that will take, and asks before adding anything:\n"
                   "the next reply waits until they are summarized. Only your words and the replies\n"
                   "come in, unless --with-tools; never thinking; keys and tokens are redacted. Importing\n"
                   "again brings only what is new.",
                   "examples:\n  eternalchat import-chats --since 2026-10-01 --dry-run\n"
                   "  eternalchat import-chats ~/Downloads/chatgpt-export.zip")
    past.add_argument("paths", metavar="PATH", nargs="*", type=Path, help="folders of logs, log files or exports")
    past.add_argument("--since", metavar="YYYY-MM-DD", default="", help="only messages from this day on")
    past.add_argument("--with-tools", action="store_true", help="also the tool calls and their results (many more)")
    past.add_argument("--dry-run", action="store_true", help="show what would come in, and stop")
    past.add_argument("--yes", action="store_true", help="import without asking")
    command("view", "print the saved memory view",
            "Print the saved view: what the model sees at the start of each turn. Each line\n"
            "is ID+N|summary, covering N messages from message ID, oldest first.")
    zoom = command("zoom", "open a summary, a message or an agent transcript",
                   "Open part of the memory, as the model's zoom tool does:\n"
                   "  zoom ID N   open summary ID+N into its two halves\n"
                   "              (N a power of two, ID a multiple of N)\n"
                   "  zoom ID     read message ID; long messages have pages from 0\n"
                   "  zoom NAME   read the transcript of subagent NAME",
                   "examples:\n  eternalchat zoom 40 8\n  eternalchat zoom 40 --page 1\n"
                   "  eternalchat zoom Research")
    zoom.add_argument("target", metavar="ID|NAME", help="message ID, or agent name")
    zoom.add_argument("n", metavar="N", nargs="?", type=int, help="messages covered by the summary (default: 1)")
    zoom.add_argument("--page", metavar="P", type=int, default=0, help="page of a long message or transcript (default: 0)")
    date = command("date", "print when a message was recorded",
                   "Print the UTC date and time when message ID was recorded.")
    date.add_argument("id", metavar="ID", type=int, help="message ID")
    command("status", "print counts, summary progress and token usage as JSON",
            "Print JSON: messages and summaries stored, view sizes, queued/active/failed\n"
            "summaries, running agents, and token usage of this process.")
    command("compact", "retry failed summaries and wait until all are built",
            "Retry failed summaries and wait until every summary is built, then print status.")
    return p


def zoom_request(target, n=None, page=0):
    """Keyword arguments for Engine.zoom. Agent names start with a letter, so a
    target made of digits is always a message ID."""
    if target.isascii() and target.isdigit():
        return {"id": int(target), "n": 1 if n is None else n, "page": page}
    if n is not None:
        raise UsageError("N applies to a message ID, not to an agent name")
    return {"name": target, "page": page}


# Output ------------------------------------------------------------------------------

class Terminal:
    """Prints engine events and results, starting status lines on a fresh line."""

    def __init__(self, verbose=False, show_thinking=False):
        self.verbose, self.show_thinking = verbose, show_thinking
        self.mid_line = False

    def event(self, kind, value):
        if kind == "text":
            print(value, end="", flush=True)
            self.mid_line = not value.endswith("\n")
        elif kind == "thinking":
            if self.show_thinking:
                print(value, end="", flush=True, file=sys.stderr)
        elif kind in ("error", "compaction_error"):
            self.line(f"{kind}: {value}", err=True)
        elif kind == "status" and (self.verbose or value.get("phase") == "retry" or
                                    value.get("phase") in ("queued", "memory") and value.get("purpose") == "turn"):
            self.line(value.get("message", ""), err=True)
        elif kind == "tool" and value.get("name") in DEVICE_NAMES:
            # What touches the computer always shows, whatever the verbosity.
            shown = value["input"].get("command") or value["input"].get("path") or json.dumps(
                value["input"], ensure_ascii=False)
            self.line(f"↳ {value['name']}: {shown}"[:300], err=True)
        elif kind == "agent_report" or self.verbose and kind in ("compaction", "tool", "usage"):
            self.line(f"{kind}: {json.dumps(value, ensure_ascii=False)}", err=True)

    def end_line(self):
        if self.mid_line:
            print(flush=True)
            self.mid_line = False

    def line(self, text, *, err=False):
        self.end_line()
        print(text, file=sys.stderr if err else sys.stdout, flush=True)

    def json(self, value):
        self.line(json.dumps(value, indent=2, ensure_ascii=False))

    def retrieval(self, found):
        self.line(found.text)
        for ref in found.images:
            self.line(f"[{ref['media_type']} image attached to this message]")


def saved(ids):
    return f"Saved as message {ids[0]}" if len(ids) == 1 else f"Saved as messages {ids[0]}..{ids[-1]}"


# Interactive session -----------------------------------------------------------------

async def stdin_lines():
    """Lines from stdin without a blocked executor thread, so Ctrl-C ends a session at once."""
    fd = sys.stdin.fileno()
    if stat.S_ISREG(os.fstat(fd).st_mode):
        for line in sys.stdin:
            yield line.rstrip("\r\n")
            await asyncio.sleep(0)
        return
    queue = asyncio.Queue()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buffer = ""
    loop = asyncio.get_running_loop()

    def ready():
        nonlocal buffer
        data = os.read(fd, 4096)
        if not data:
            loop.remove_reader(fd)
            buffer += decoder.decode(b"", final=True)
            if buffer:
                queue.put_nowait(buffer.rstrip("\r"))
            queue.put_nowait(None)
            return
        buffer += decoder.decode(data)
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            queue.put_nowait(line.rstrip("\r"))

    loop.add_reader(fd, ready)
    try:
        while (line := await queue.get()) is not None:
            yield line
    finally:
        loop.remove_reader(fd)


class Session:
    QUIT = object()

    def __init__(self, engine, out, demo=False):
        self.engine, self.out, self.demo = engine, out, demo
        self.images = []
        self.futures = set()  # replies still running; finished ones are released
        self.handlers = {
            "/help": self.help, "/status": self.status, "/view": self.view, "/zoom": self.zoom,
            "/date": self.date, "/note": self.note, "/attach": self.attach, "/agent": self.agent,
            "/compact": self.compact, "/goal": self.goal, "/halt": self.halt,
            "/quit": self.quit, "/exit": self.quit,
        }
        if demo:
            self.handlers["/recall"] = self.recall

    async def run(self):
        self.out.line("EternalChat" + (" · OFFLINE DEMO" if self.demo else "") +
                      ". Type a message; /help lists commands.")
        self.engine.resume_reports()  # reports a crash kept out of the chat arrive now
        async for line in stdin_lines():
            if not line.strip():
                continue
            try:
                if self.handle(line) is self.QUIT:
                    break
            except (ValueError, OSError, RuntimeError) as e:
                self.out.line(f"Error: {e}", err=True)
            await asyncio.sleep(0)
        if self.images:
            self.out.line(f"Discarded {len(self.images)} image(s) attached to no message.", err=True)
        await asyncio.gather(*self.futures, return_exceptions=True)
        await self.engine.finish()

    def handle(self, line):
        if line.startswith("//"):
            return self.send(line[1:])
        if not line.startswith("/"):
            return self.send(line)
        name, _, rest = line.partition(" ")
        handler = self.handlers.get(name)
        if handler is None:
            raise UsageError(f"Unknown command {name}. /help lists commands; "
                             "// sends a line that starts with /.")
        return handler(rest.strip())

    def send(self, text):
        future = self.engine.submit(text, attachments=self.images)
        self.images = []
        future.add_done_callback(self._finished)
        self.futures.add(future)

    def _finished(self, future):
        self.futures.discard(future)
        if not future.cancelled():
            future.exception()  # the engine already reported it as an error event
        self.out.end_line()

    @staticmethod
    def _no_arguments(rest, name):
        if rest:
            raise UsageError(f"{name} takes no arguments")

    def help(self, rest):
        self._no_arguments(rest, "/help")
        self.out.line(slash_help(self.demo))

    def status(self, rest):
        self._no_arguments(rest, "/status")
        self.out.json(self.engine.status())

    def view(self, rest):
        self._no_arguments(rest, "/view")
        self.out.line(self.engine.render_view())

    def zoom(self, rest):
        usage = UsageError("Usage: /zoom ID [N] [PAGE] or /zoom NAME [PAGE]")
        parts = shlex.split(rest)
        if not parts or len(parts) > 3:
            raise usage
        try:
            numbers = [int(part) for part in parts[1:]]
        except ValueError:
            raise usage from None
        target = parts[0]
        if target.isascii() and target.isdigit():
            request = zoom_request(target, *numbers[:1], *numbers[1:2])
        elif len(numbers) > 1:
            raise usage
        else:
            request = zoom_request(target, None, *numbers)
        self.out.retrieval(self.engine.zoom(**request))

    def date(self, rest):
        try:
            message_id = int(rest)
        except ValueError:
            raise UsageError("Usage: /date ID") from None
        self.out.line(self.engine.date(message_id))

    def note(self, rest):
        if not rest:
            raise UsageError("Usage: /note TEXT")
        self.out.line(saved(self.engine.note(rest)))

    def attach(self, rest):
        parts = shlex.split(rest)
        if len(parts) != 1:
            raise UsageError("Usage: /attach PATH (quote a path containing spaces)")
        self.images.append(self.engine.attach(parts[0]))
        self.out.line(f"Image attached to your next message ({len(self.images)} pending).")

    def agent(self, rest):
        name, _, task = rest.partition(" ")
        if not name or not task.strip():
            raise UsageError("Usage: /agent NAME TASK")
        self.engine.start_agent(name, task.strip())
        self.out.line(f"Started [{name}]. Its report will arrive by itself.")

    def goal(self, rest):
        if not rest:
            raise UsageError("Usage: /goal TEXT")
        future = self.engine.set_goal(rest)
        future.add_done_callback(self._finished)
        self.futures.add(future)

    def halt(self, rest):
        self._no_arguments(rest, "/halt")
        self.engine.halt()
        self.out.line("Goal dropped.")

    def compact(self, rest):
        self._no_arguments(rest, "/compact")
        self.engine.compact()
        self.out.line("Retrying failed summaries; /status shows progress.")

    def recall(self, rest):
        if not (rest.isascii() and rest.isdigit()):
            raise UsageError("Usage: /recall ID")
        return self.send(f"/recall {rest}")

    def quit(self, rest):
        self._no_arguments(rest, "/quit")
        return self.QUIT


# Setup -------------------------------------------------------------------------------

CONNECTIONS = (("api", None, "API key (ANTHROPIC_API_KEY), billed per use"),
               ("subscription", "claudecode", "Claude Code subscription (claude)"),
               ("subscription", "codex", "Codex subscription (codex)"))
LOGIN = {"claudecode": "claude auth login", "codex": "codex login"}


def default_chat_dir():
    """The chat folder when none is chosen: a chat in ./.eternalchat (the default
    before settings, a chat per working folder), else ~/.eternalchat/chat."""
    legacy = settings.LEGACY_CHAT
    return legacy if (legacy / "main").is_dir() else settings.default_chat()


def _ask(read, question, value, default=None):
    """An answer: Enter keeps `value`, and 'default' clears it; an empty value
    shows what applies instead, `default`."""
    answer = read(f"{question} [{value or default}]: ").strip()
    return "" if answer.lower() == "default" else answer or value


def setup(args, read=None, write=print):
    """Ask for the connection, models and chat folder, prefilled with what applies
    now, and save them. Returns False if cancelled, with nothing saved."""
    read = read or input
    write(f"EternalChat setup. Answers are saved in {settings.path()}.\n"
          "Enter keeps the value in [brackets]; 'default' clears it.")
    try:
        write("\nHow should EternalChat reach the model?")
        for k, (_, _, label) in enumerate(CONNECTIONS, 1):
            write(f"  {k}  {label}")
        now = next((k for k, (connection, cli, _) in enumerate(CONNECTIONS, 1)
                    if connection == args.connection and cli in (None, args.cli)), 1)
        while (choice := _ask(read, f"Choose 1-{len(CONNECTIONS)}", str(now))) not in \
                [str(k) for k in range(1, len(CONNECTIONS) + 1)]:
            write("Type one of the numbers above.")
        connection, cli, _ = CONNECTIONS[int(choice) - 1]
        subscription = connection == "subscription"
        if not subscription and not os.environ.get("ANTHROPIC_API_KEY"):
            write("  ANTHROPIC_API_KEY is not set in this shell. Add this line to your shell\n"
                  "  profile (~/.zshrc), then open a new terminal; EternalChat never saves keys:\n"
                  "    export ANTHROPIC_API_KEY='your-key'")
        elif subscription:
            found = args.cli_executable or shutil.which(CLIENTS[cli])
            write(f"  Found {found}. Sign in once if you haven't: {LOGIN[cli]}" if found else
                  f"  {CLIENTS[cli]} is not installed or not on PATH. Install it, then sign in: {LOGIN[cli]}")
        # Models chosen for another connection rarely fit this one: start over.
        same = connection == args.connection and (not subscription or cli == args.cli)
        model = _ask(read, "\nModel for replies", (args.model or "") if same else "",
                     "the CLI's default" if subscription else Config.turn_model)
        if subscription:
            write("Summaries are many small, mechanical calls: a smaller model saves allowance.")
        summary = _ask(read, "Model for summaries",
                       (args.summary_model or "") if same else ("haiku" if cli == "claudecode" else ""),
                       "the same as replies" if subscription else Config.summary_model)
        folder = _ask(read, "\nChat folder", str(Path(args.data_dir or default_chat_dir()).expanduser().resolve()))
        folder = Path(folder or settings.default_chat()).expanduser().resolve()
        write(f"  Continues the chat in {folder}." if (folder / "main").is_dir() else
              f"  A new chat starts in {folder}.")
    except (EOFError, KeyboardInterrupt):
        write("\nSetup cancelled; nothing was saved.")
        return False
    target = settings.save({
        "connection": connection, "cli": cli, "model": model, "summary_model": summary, "data_dir": folder,
        "reasoning_effort": args.reasoning_effort if subscription else None,
        "instructions": args.instructions.expanduser().resolve() if args.instructions else None})
    write(f"\nSaved in {target}. Type 'eternalchat' to chat; 'eternalchat setup' changes these.")
    return True


# Commands ----------------------------------------------------------------------------

async def cmd_chat(engine, args, out):
    await Session(engine, out, args.demo).run()
    return 0


async def cmd_ask(engine, args, out):
    images = [engine.attach(path) for path in args.image]
    future = engine.submit(args.text, attachments=images)
    try:
        await future
    except Exception:
        return 1  # the engine already printed it as an error event
    out.end_line()
    await engine.finish()
    return 0


async def cmd_note(engine, args, out):
    text = args.text if args.command == "note" else args.file.read_bytes().decode("utf-8")
    out.line(saved(engine.note(text)))
    await engine.finish()
    return 0


async def cmd_import_chats(engine, args, out):
    if args.since and not (len(args.since) == 10 and args.since[4] == args.since[7] == "-"):
        raise UsageError("--since takes a day as YYYY-MM-DD")
    sessions = await asyncio.to_thread(history.find, args.paths or None, since=args.since, tools=args.with_tools,
                                       done=engine.imported())
    if not sessions:
        out.line("Nothing new to import.")
        return 0
    for s in sessions:
        out.line(f"  {s.start[:10]}  {s.app:<11} {len(s.messages):>6,}  {s.where[:70]}")
    messages, calls = sum(len(s.messages) for s in sessions), sum(s.calls for s in sessions)
    out.line(f"{len(sessions):,} sessions, {messages:,} messages: about {calls:,} summary calls, more as memory "
             "merges, on this chat's connection. The next reply waits until they are summarized.")
    if args.dry_run:
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            raise UsageError("add --yes to import without asking")
        if input("Import them? [y/N] ").strip().lower() not in ("y", "yes"):
            out.line("Nothing imported.")
            return 0
    count = engine.import_chats(sessions)
    out.line(f"Imported {count:,} messages. Summarizing them; Ctrl-C stops, and the next start goes on.")
    progress = asyncio.create_task(_summary_progress(engine, out))
    try:
        await engine.finish()
    finally:
        progress.cancel()
    out.line("Done: ask about them any time.")
    return 0


async def _summary_progress(engine, out):
    while True:
        await asyncio.sleep(10)
        status = engine.status()
        out.line(f"  summaries: {status['active'] + status['queued']:,} to go, {status['failed']:,} failed", err=True)


async def cmd_view(engine, args, out):
    out.line(engine.render_view())
    return 0


async def cmd_status(engine, args, out):
    out.json(engine.status())
    return 0


async def cmd_zoom(engine, args, out):
    out.retrieval(engine.zoom(**zoom_request(args.target, args.n, args.page)))
    return 0


async def cmd_date(engine, args, out):
    out.line(engine.date(args.id))
    return 0


async def cmd_compact(engine, args, out):
    engine.compact()
    await engine.finish()
    out.json(engine.status())
    return 0


COMMANDS = {"chat": cmd_chat, "ask": cmd_ask, "note": cmd_note, "import": cmd_note, "import-chats": cmd_import_chats,
            "view": cmd_view, "status": cmd_status, "zoom": cmd_zoom, "date": cmd_date,
            "compact": cmd_compact}


def chat_dir(args, out):
    """The chat folder the options select. One found in the working folder (the
    default before settings) is named, so it never stands in silently."""
    if args.data_dir is not None:
        return args.data_dir
    data_dir = default_chat_dir()
    if data_dir == settings.LEGACY_CHAT:
        out.line(f"Using the chat in {data_dir.resolve()}, found in this folder. "
                 "Run 'eternalchat setup' to use one chat folder everywhere.", err=True)
    return data_dir


def open_from(args, on_event, out):
    """Check the options and open the chat they select, reporting to on_event."""
    instructions = args.instructions.read_text(encoding="utf-8") if args.instructions else ""
    if args.connection not in ("api", "subscription"):
        raise UsageError("the connection (ETERNALCHAT_CONNECTION or saved setting) must be api or subscription")
    if args.cli not in ("codex", "claudecode"):
        raise UsageError("the CLI (ETERNALCHAT_CLI or saved setting) must be codex or claudecode" +
                         ("; Grok support was removed" if args.cli == "grok" else ""))
    subscription = args.connection == "subscription"
    if args.cli_timeout <= 0 or not math.isfinite(args.cli_timeout):
        raise UsageError("--cli-timeout must be a positive finite number")
    if not 1 <= args.cli_concurrency <= 8:
        raise UsageError("--cli-concurrency must be between 1 and 8")
    if args.reasoning_effort is not None and not subscription and not args.demo:
        raise UsageError("--reasoning-effort currently requires --connection subscription")
    model = args.model or ("" if subscription else Config.turn_model)
    summary_model = args.summary_model or (model if subscription else Config.summary_model)
    config = Config(turn_model=model, summary_model=summary_model, instructions=instructions,
                    max_tokens=args.max_tokens,
                    concurrency=args.cli_concurrency if subscription else Config.concurrency)
    data_dir = chat_dir(args, out)
    readonly = args.command in INSPECT
    if readonly:
        provider = None
    elif args.demo:
        provider = DemoProvider()
    elif subscription:
        provider = CLIProvider(args.cli, executable=args.cli_executable, timeout=args.cli_timeout,
                               concurrency=args.cli_concurrency, reasoning_effort=args.reasoning_effort)
    else:
        provider = AnthropicProvider(os.environ.get("ANTHROPIC_API_KEY"), timeout=config.timeout)
    machine = Machine(args.work or settings.default_work()) if args.tools == "auto" else None
    return open_chat(data_dir, provider, config, on_event, readonly=readonly, machine=machine)


async def run(args):
    out = Terminal(args.verbose, args.show_thinking)
    if args.command == "web":
        # Settle the folder once: switching connection in the page reopens this chat.
        args.data_dir = str(chat_dir(args, out))
        return await web.serve(args, lambda options, on_event: open_from(options, on_event, out), out)
    engine = open_from(args, out.event, out)
    try:
        return await COMMANDS[args.command](engine, args, out)
    finally:
        await engine.close()


def main(argv=None):
    try:
        saved, broken = settings.load(), None
    except ValueError as e:
        saved, broken = {}, e  # setup can still replace the broken file
    p = parser(saved)
    args = p.parse_args(argv)
    # A plain 'eternalchat' chats. The very first one, or 'web', at a terminal sets up first.
    first = (args.command in (None, "web") and not args.demo and not settings.path().exists()
             and sys.stdin.isatty() and sys.stdout.isatty())
    args.command = args.command or "chat"
    try:
        if args.command == "setup" or first:
            if not setup(args):
                return 130
            if args.command == "setup":
                return 0
            command = args.command
            args = parser(settings.load()).parse_args(argv)
            args.command = command
        elif broken:
            raise broken
        return asyncio.run(run(args))
    except UsageError as e:
        p.exit(2, f"{p.prog} {args.command}: error: {e}\n")
    except KeyboardInterrupt:
        print("\nStopped. Completed records are saved.", file=sys.stderr)
        return 130
    except (RuntimeError, ValueError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
