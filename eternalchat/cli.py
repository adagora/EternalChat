"""Terminal interface: argument parsing, the interactive session and printing.

Everything shown comes from Engine methods; this module only parses what the
user types and formats what the engine returns.
"""
import argparse
import asyncio
import codecs
import json
import os
import shlex
import stat
import sys
from pathlib import Path

from . import __version__
from .app import open_chat
from .config import MAX_IMAGE_BYTES, Config
from .demo import DemoProvider
from .provider import AnthropicProvider

INSPECT = frozenset({"view", "status", "zoom", "date"})  # read-only commands

DESCRIPTION = """\
One terminal chat that never ends. Every message is kept on disk, a background
tree of short summaries keeps all of history in a bounded view, and the model
zooms into that tree to recall details."""

EPILOG = """\
examples:
  eternalchat --demo ask 'Remember amber-42.'   try it offline: no API key, no charges
  eternalchat --demo ask '/recall 0'            the demo model zooms into message 0
  export ANTHROPIC_API_KEY=sk-ant-...
  eternalchat chat                              talk; type /help for in-chat commands
  eternalchat zoom 40 8                         open summary 40+8 into its two halves
  eternalchat zoom 40 --page 1                  read page 1 of message 40
  eternalchat status --data-dir ~/work-chat     inspect another chat

The chat lives in --data-dir; reuse the folder to continue it. Options may go
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


def _global_options(p, suppress):
    """Options every command accepts, before or after its name. Subcommand copies
    use SUPPRESS defaults, so they never reset an option given before the
    command, and hidden help, so each command's -h stays about that command."""
    def default(value):
        return argparse.SUPPRESS if suppress else value

    def describe(text):
        return argparse.SUPPRESS if suppress else text

    chat = p.add_argument_group("chat")
    chat.add_argument("--data-dir", metavar="DIR", default=default(os.environ.get("ETERNALCHAT_DIR", ".eternalchat")),
                      help=describe("chat folder (default: ./.eternalchat; env ETERNALCHAT_DIR)"))
    chat.add_argument("--demo", action="store_true", default=default(False),
                      help=describe("offline demo with placeholder replies: no API key, no charges"))
    models = p.add_argument_group("models")
    models.add_argument("--model", metavar="ID", default=default(os.environ.get("ETERNALCHAT_MODEL", Config.turn_model)),
                        help=describe(f"model for replies (default: {Config.turn_model}; env ETERNALCHAT_MODEL)"))
    models.add_argument("--summary-model", metavar="ID",
                        default=default(os.environ.get("ETERNALCHAT_SUMMARY_MODEL", Config.summary_model)),
                        help=describe(f"model for summaries (default: {Config.summary_model}; env ETERNALCHAT_SUMMARY_MODEL)"))
    models.add_argument("--instructions", metavar="FILE", type=Path, default=default(None),
                        help=describe("UTF-8 file of standing preferences, read once at startup"))
    output = p.add_argument_group("output")
    output.add_argument("--verbose", action="store_true", default=default(False),
                        help=describe("also print summary progress, tool calls and token usage"))
    output.add_argument("--show-thinking", action="store_true", default=default(False),
                        help=describe("print model thinking to stderr (never saved)"))


def parser():
    common = argparse.ArgumentParser(add_help=False)
    _global_options(common, suppress=True)
    p = argparse.ArgumentParser(prog="eternalchat", usage="%(prog)s [options] COMMAND [ARGS]",
                                description=DESCRIPTION, epilog=EPILOG, formatter_class=Formatter)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True, title="commands", metavar="COMMAND")
    _global_options(p, suppress=False)

    def command(name, summary, description, epilog=None):
        epilog = (epilog + "\n\n" if epilog else "") + GLOBAL_NOTE
        return sub.add_parser(name, help=summary, description=description, epilog=epilog,
                              parents=[common], formatter_class=Formatter)

    command("chat", "talk interactively (type /help inside)",
            "Talk interactively. Input stays responsive while the model works.",
            "in-chat commands:\n" + slash_help(demo=True))
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
            "/compact": self.compact, "/quit": self.quit, "/exit": self.quit,
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


COMMANDS = {"chat": cmd_chat, "ask": cmd_ask, "note": cmd_note, "import": cmd_note,
            "view": cmd_view, "status": cmd_status, "zoom": cmd_zoom, "date": cmd_date,
            "compact": cmd_compact}


async def run(args):
    instructions = args.instructions.read_text(encoding="utf-8") if args.instructions else ""
    config = Config(turn_model=args.model, summary_model=args.summary_model, instructions=instructions)
    out = Terminal(args.verbose, args.show_thinking)
    readonly = args.command in INSPECT
    if readonly:
        provider = None
    elif args.demo:
        provider = DemoProvider()
    else:
        provider = AnthropicProvider(os.environ.get("ANTHROPIC_API_KEY"), timeout=config.timeout)
    engine = open_chat(args.data_dir, provider, config, out.event, readonly=readonly)
    try:
        return await COMMANDS[args.command](engine, args, out)
    finally:
        await engine.close()


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except UsageError as e:
        p.exit(2, f"{p.prog} {args.command}: error: {e}\n")
    except KeyboardInterrupt:
        print("\nStopped. Completed records are saved.", file=sys.stderr)
        return 130
    except (RuntimeError, ValueError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
