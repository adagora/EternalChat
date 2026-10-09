import argparse
import asyncio
import codecs
import json
import os
import shlex
import stat
import sys
from pathlib import Path

from .config import Config
from .engine import Engine
from .provider import AnthropicProvider, DemoProvider


def parser():
    p = argparse.ArgumentParser(description="One persistent chat with retrievable tree memory.")
    p.add_argument("--data-dir", default=os.environ.get("ETERNALCHAT_DIR", ".eternalchat"))
    p.add_argument("--demo", action="store_true", help="offline deterministic demo, no model calls")
    p.add_argument("--model", default=os.environ.get("ETERNALCHAT_MODEL", Config.turn_model))
    p.add_argument("--summary-model", default=os.environ.get("ETERNALCHAT_SUMMARY_MODEL", Config.summary_model))
    p.add_argument("--instructions", type=Path, help="UTF-8 file of stable personal instructions")
    p.add_argument("--show-thinking", action="store_true", help="display provider thinking without saving it")
    p.add_argument("--verbose", action="store_true", help="print compaction and cache-usage events")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("chat", help="interactive chat; input while working steers the active turn")
    ask = sub.add_parser("ask", help="send one message and exit after compaction")
    ask.add_argument("text")
    ask.add_argument("--image", type=Path, action="append", default=[])
    note = sub.add_parser("note", help="import a memory without starting a turn")
    note.add_argument("text")
    imp = sub.add_parser("import", help="import a UTF-8 text file as lossless note records")
    imp.add_argument("file", type=Path)
    sub.add_parser("view", help="print the saved memory view")
    sub.add_parser("status", help="print storage, queue and cache metrics as JSON")
    sub.add_parser("compact", help="retry and finish outstanding compactions")
    zoom = sub.add_parser("zoom", help="open a node or an agent transcript")
    zoom.add_argument("id", help="numeric first message ID or agent name")
    zoom.add_argument("n", nargs="?", type=int, default=1)
    zoom.add_argument("--page", type=int, default=0)
    date = sub.add_parser("date")
    date.add_argument("id", type=int)
    return p


def print_blocks(blocks):
    for block in blocks:
        print(block.get("text", "[image available in the original message]"))


async def stdin_lines():
    """No blocked executor thread when Ctrl-C cancels an interactive session."""
    fd = sys.stdin.fileno()
    if stat.S_ISREG(os.fstat(fd).st_mode):
        for line in sys.stdin:
            yield line.rstrip("\n")
            await asyncio.sleep(0)
        return
    queue = asyncio.Queue()
    decoder = codecs.getincrementaldecoder("utf-8")()
    buffer = ""
    loop = asyncio.get_running_loop()

    def ready():
        nonlocal buffer
        data = os.read(fd, 4096)
        if not data:
            loop.remove_reader(fd)
            buffer += decoder.decode(b"", final=True)
            if buffer:
                queue.put_nowait(buffer)
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


async def interactive(engine, args):
    engine.resume_reports()
    print("EternalChat" + (" · OFFLINE DEMO" if args.demo else ""))
    print("Type a message. /help lists commands. You can send corrections while a turn runs.")
    futures = []
    images = []

    def finished(future):
        if not future.cancelled():
            future.exception()  # error already displayed by the engine
        print("\n", end="", flush=True)

    async for line in stdin_lines():
        if not line.strip():
            continue
        try:
            if line in ("/quit", "/exit"):
                break
            if line == "/help":
                print("/status · /view · /zoom ID [N] [PAGE] · /date ID · /note TEXT\n"
                      "/agent NAME TASK · /zoom NAME · /attach PATH · /compact · /quit\n"
                      "Demo only: /recall ID exercises model tool use. EOF finishes submitted turns.\n"
                      "/quit cancels outstanding agent jobs after finishing submitted turns.")
            elif line == "/status":
                print(json.dumps(engine.status(), indent=2))
            elif line == "/view":
                print(engine.view.render())
            elif line.startswith("/zoom "):
                parts = shlex.split(line)[1:]
                if parts[0].isdigit():
                    values = {"id": int(parts[0]), "n": int(parts[1]) if len(parts) > 1 else 1,
                              "page": int(parts[2]) if len(parts) > 2 else 0}
                else:
                    values = {"name": parts[0], "page": int(parts[1]) if len(parts) > 1 else 0}
                print_blocks(engine.tool("zoom", values))
            elif line.startswith("/date "):
                print_blocks(engine.tool("date", {"id": int(line[6:])}))
            elif line.startswith("/note "):
                print("Saved note:", engine.record("note", line[6:]))
            elif line.startswith("/attach "):
                parts = shlex.split(line)
                if len(parts) != 2:
                    raise ValueError("Use /attach PATH; quote a path containing spaces")
                images.append(engine.store.add_image(parts[1]))
                print("Image attached to your next message.")
            elif line.startswith("/agent "):
                _, name, task = line.split(maxsplit=2)
                engine.start_agent(name, task)
                print(f"Started [{name}]. Its report will arrive automatically.")
            elif line == "/compact":
                engine.compactor.retry()
                engine.compactor.start()
                print("Compaction queued; /status shows progress.")
            else:
                future = engine.submit(line, attachments=images)
                images = []
                future.add_done_callback(finished)
                futures.append(future)
            await asyncio.sleep(0)
        except (ValueError, OSError) as e:
            print(f"Error: {e}", file=sys.stderr)
    if futures:
        await asyncio.gather(*futures, return_exceptions=True)
    await engine.finish()


async def run(args):
    readonly = args.command in {"view", "status", "zoom", "date"}
    config = Config(turn_model=args.model, summary_model=args.summary_model,
                    instructions=args.instructions.read_text() if args.instructions else "")
    provider = DemoProvider() if args.demo or readonly else AnthropicProvider(
        os.environ.get("ANTHROPIC_API_KEY"), timeout=config.timeout)

    def event(kind, value):
        if kind == "text":
            print(value, end="", flush=True)
        elif kind == "thinking" and args.show_thinking:
            print(value, end="", flush=True, file=sys.stderr)
        elif kind in {"error", "compaction_error"}:
            print(f"\n{kind}: {value}", file=sys.stderr)
        elif kind == "agent_report" or args.verbose and kind in {"compaction", "tool", "usage"}:
            print(f"\n{kind}: {json.dumps(value)}", file=sys.stderr)

    engine = Engine(args.data_dir, provider, config, event)
    try:
        if args.command == "chat":
            await interactive(engine, args)
        elif args.command == "ask":
            images = [engine.store.add_image(p) for p in args.image]
            await engine.ask(args.text, attachments=images)
            print()
            await engine.finish()
        elif args.command in {"note", "import"}:
            text = args.text if args.command == "note" else args.file.read_text()
            ids = engine.record("note", text)
            await engine.finish()
            print(f"Saved {len(ids)} note record(s): {ids[0]}..{ids[-1]}")
        elif args.command == "compact":
            engine.compactor.retry()
            await engine.finish()
            print(json.dumps(engine.status(), indent=2))
        elif args.command == "status":
            print(json.dumps(engine.status(), indent=2))
        elif args.command == "view":
            print(engine.view.render())
        elif args.command == "date":
            print_blocks(engine.tool("date", {"id": args.id}))
        elif args.command == "zoom":
            values = {"id": int(args.id), "n": args.n} if args.id.isdigit() else {"name": args.id}
            print_blocks(engine.tool("zoom", {**values, "page": args.page}))
    finally:
        await engine.close()


def main():
    args = parser().parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nStopped. Completed records are saved.", file=sys.stderr)
        return 130
    except (RuntimeError, ValueError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0
