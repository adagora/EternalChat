"""Opt-in real subscription acceptance, beyond subscription_smoke.py.

Consumes the signed-in CLI account's allowance. All file work uses disposable
folders. No API billing fallback, communications, purchases or external deletion.
Evidence contains tool names, observed model IDs, timing and native usage, not
prompts, account identifiers, screenshots or raw client errors.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eternalchat.app import open_chat
from eternalchat.cli_provider import CLIProvider
from eternalchat.config import Config
from eternalchat.devices import Devices
from eternalchat.errors import ProviderError
from eternalchat.machine import Machine
from eternalchat.prompt import GOAL_NO, GOAL_SYSTEM
from eternalchat.web import WebChat, device


class Observed(CLIProvider):
    def __init__(self, client, max_calls):
        super().__init__(client)
        self.calls, self.max_calls, self.phase = [], max_calls, None
        self.checkpoint = None

    async def complete(self, **kw):
        if len(self.calls) >= self.max_calls:
            raise ProviderError("Acceptance request budget reached")
        record = {"phase": self.phase, "purpose": "goal_check" if kw.get("system") == GOAL_SYSTEM else kw.get("purpose", "turn"),
                  "requested_model": kw["model"] or None, "observed_model": None,
                  "requested_effort": kw.get("effort") or self.reasoning_effort,
                  "usage": {}, "tools": [], "ok": False}
        self.calls.append(record)
        sink = kw.get("on_event")
        started = time.monotonic()

        def observe(kind, value):
            if kind == "model":
                record["observed_model"] = value
            if kind == "usage":
                record["usage"] = value
            if sink:
                sink(kind, value)

        try:
            response = await super().complete(**{**kw, "on_event": observe})
            record["usage"] = response.usage
            record["tools"] = [c["name"] for c in response.tool_calls]
            record["ok"] = True
            return response
        except BaseException as error:
            record["error_type"] = type(error).__name__
            record["error_category"] = (self.last_error or {}).get("category")
            raise
        finally:
            record["seconds"] = round(time.monotonic() - started, 3)
            record["usage_windows"] = self.rate_limits
            if self.checkpoint:
                self.checkpoint()


class Quiet:
    def line(self, text, **kwargs):
        pass  # the device's bearer link and raw network errors stay out of evidence


async def version(executable):
    process = await asyncio.create_subprocess_exec(executable, "--version", stdout=asyncio.subprocess.PIPE,
                                                  stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(process.communicate(), 20)
        text = out.decode("utf-8", errors="replace").strip()
        return text if len(text) <= 100 and re.fullmatch(r"[\w .()+-]+", text) else None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def tools(engine, start):
    return [json.loads(m["text"]) for m in engine.messages(start) if m["kind"] == "tool"]


async def settle(engine):
    while True:
        pending = [job for job in engine.agents.values() if not job.done()]
        if pending:
            await asyncio.gather(*pending)
        await engine.finish()
        if not any(not job.done() for job in engine.agents.values()):
            return


async def run(args):
    provider = Observed(args.client, args.max_calls)
    config = Config(turn_model=args.model or "", summary_model=args.model or "", instructions=(
        "This is an isolated acceptance test. Use only the assigned temporary working folders. "
        "No communications, purchases or deletions. Use only the tools explicitly requested. "
        "A completed agent report needs only a short acknowledgement; do not start new work from it."))
    report = {"client": args.client, "version": await version(provider.executable),
              "runner_max_calls": args.max_calls, "phase_timeout_seconds": args.phase_timeout,
              "started_utc": datetime.now(timezone.utc).isoformat(), "phases": [], "calls": provider.calls,
              "actual_subscription_charge": None, "api_equivalent_estimate": None,
              "scope": "Live model and tools in disposable data; baseline memory/images use subscription_smoke.py"}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")

    provider.checkpoint = save

    with tempfile.TemporaryDirectory(prefix="eternalchat-acceptance-") as temporary:
        root = Path(temporary)
        machine = Devices(Machine(root / "work", name="acceptance-host"))
        engine = open_chat(root / "chat", provider, config, machine=machine)
        try:
            for phase in args.phase or ("machine", "search", "agents", "goal", "device", "screen"):
                provider.phase = phase
                start = time.monotonic()
                first = engine.store.message_count
                before = len(provider.calls)
                evidence = {"phase": phase, "ok": False}
                try:
                    async with asyncio.timeout(args.phase_timeout):
                        # Grow the real memory and measure its summary overhead too.
                        engine.note(f"Acceptance phase {phase}. " + "Use the current test instructions; earlier phases are completed evidence. " * 10)
                        await engine.finish()
                        if phase == "machine":
                            await engine.ask("Use write to create acceptance.txt containing exactly alpha-42 followed by a newline. "
                                "Then read it, use edit to replace alpha with omega, then use shell to run cat acceptance.txt. Report its output.")
                            assert (machine.folder("main") / "acceptance.txt").read_text() == "omega-42\n", "file result differs"
                            assert {"write", "read", "edit", "shell"} <= {c["name"] for c in tools(engine, first)}, "missing tool workflow"
                        elif phase == "search":
                            await engine.ask("Use search with query Python asyncio documentation and count 3. "
                                "Give the first result's title and URL as a Markdown link. Do not use shell for search.")
                            calls = tools(engine, first)
                            assert any(c["name"] == "search" for c in calls), "no actual search call"
                            echoes = [json.loads(m["text"]) for m in engine.messages(first)
                                      if m["kind"] == "echo" and m["text"].startswith('{"backend"')]
                            assert echoes and echoes[0]["results"], "search returned no verified sources"
                            sources = echoes[0]["results"]
                            replies = " ".join(m["text"] for m in engine.messages(first) if m["kind"] == "unii")
                            assert any(r["url"] in replies for r in sources), "reply omitted returned URLs"
                            evidence["source_urls"] = [r["url"] for r in sources]
                        elif phase == "agents":
                            await engine.ask("Explicitly start exactly one subagent using spawn. Its task is: report the words "
                                "agent-first-42, with no other work and no tools. Return immediately; its report arrives by itself.")
                            await settle(engine)
                            assert any(c["name"] == "spawn" for c in tools(engine, first)), "no spawn call"
                            names = list(engine.agents)
                            assert len(names) == 1, "expected exactly one worker"
                            name = names[0]
                            assert "agent-first-42" in engine.store.agent_state(name).get("report", ""), "first report missing"
                            await engine.ask(f"Use tell to ask {name} to report agent-second-42, with no other work and no tools.")
                            await settle(engine)
                            assert any(c["name"] == "tell" for c in tools(engine, first)), "no tell call"
                            assert "agent-second-42" in engine.store.agent_state(name).get("report", ""), "continued report missing"
                            evidence["worker_reports"] = 2
                        elif phase == "goal":
                            await engine.set_goal("Work in two turns. In the first reply only say 'Plan ready', with no tools. "
                                "On the next turn, use write to create goal.txt containing goal-done-42 and use read to verify it. "
                                "The goal is met only after that file has been written and read; a plan alone does not meet it.")
                            await engine.finish()
                            assert any(m["kind"] == "user" and m["text"] == GOAL_NO for m in engine.messages(first)), "no automatic continuation"
                            assert engine.goal is None, "goal still active"
                            assert (machine.folder("main") / "goal.txt").read_text().strip() == "goal-done-42", "goal file missing"
                            assert {"write", "read"} <= {c["name"] for c in tools(engine, first)}, "goal was not verified with tools"
                        elif phase == "device":
                            # The remote client and server communicate over authenticated loopback HTTP/SSE.
                            options = SimpleNamespace(connection="subscription", cli=args.client, model=args.model,
                                summary_model=args.model, demo=False, tools="auto", data_dir=str(root / "chat"), reasoning_effort=None)
                            await engine.close()

                            def opened(options, event):
                                return open_chat(root / "chat", provider, config, event,
                                    machine=Devices(Machine(root / "work", name="acceptance-host")))

                            server = WebChat(options, opened, Quiet(), "acceptance-token-" + "x" * 32)
                            url = await server.start(0)
                            engine, machine = server.engine, server.engine.machine
                            client = asyncio.create_task(device(url, "acceptance-device", root / "remote", Quiet()))
                            try:
                                while "acceptance-device" not in server.remotes:
                                    await asyncio.sleep(.05)
                                await engine.ask("Use write on device acceptance-device to create remote.txt containing remote-done-42. "
                                    "Then use read on that same device to verify it. Do not run this on acceptance-host.")
                                assert (root / "remote" / "main" / "remote.txt").read_text().strip() == "remote-done-42", "remote file missing"
                                calls = [c for c in tools(engine, first) if c["name"] in {"write", "read"}]
                                assert len(calls) >= 2 and all(c["input"].get("device") == "acceptance-device" for c in calls), "wrong target"
                                evidence["transport"] = "authenticated loopback HTTP/SSE"
                                await engine.finish()
                            finally:
                                client.cancel()
                                await asyncio.gather(client, return_exceptions=True)
                                await server.close()
                                engine = opened(options, None)
                                machine = engine.machine
                        elif phase == "screen":
                            if not machine.local.computer_supported:
                                evidence["pending"] = "A Mac with screen control is required"
                            else:
                                previous = set(engine.agents)
                                await engine.ask("Use computer to start a background task on acceptance-host: take one screenshot "
                                    "and report its dimensions. Do not click, type, move the mouse or change anything. "
                                    "If screen permission is missing, report that and stop the task. Return without waiting for its report.")
                                await settle(engine)
                                names = set(engine.agents) - previous
                                assert len(names) == 1, "expected exactly one screen worker"
                                name = names.pop()
                                log = engine.agent_log(name)
                                assert log["role"]["kind"] == "screen", "worker role differs"
                                echoes = [m for m in log["records"] if m["kind"] == "echo"]
                                if any(m.get("attachments") for m in echoes):
                                    evidence["screen_image_observed"] = True
                                elif any("Screen Recording" in m["text"] for m in echoes):
                                    evidence["pending"] = "macOS Screen Recording permission is missing"
                                else:
                                    raise AssertionError("No screenshot or expected permission error")
                        await engine.finish()
                        evidence["ok"] = "pending" not in evidence
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    evidence["error_type"] = type(error).__name__
                    evidence["error_category"] = (provider.last_error or {}).get("category")
                    if "Acceptance request budget reached" in str(error):
                        evidence["pending"] = "Acceptance runner call budget reached"
                    if isinstance(error, AssertionError):
                        evidence["assertion"] = str(error)
                    engine.stop()
                    if engine.goal:
                        engine.halt()
                evidence["seconds"] = round(time.monotonic() - start, 3)
                evidence["calls"] = len(provider.calls) - before
                report["phases"].append(evidence)
                save()
                print(json.dumps({"client": args.client, **evidence}), flush=True)
                if (provider.last_error or {}).get("category") in {"limit", "login", "access"}:
                    break  # do not keep spending requests against an external account wall
        finally:
            await engine.close()
            report["finished_utc"] = datetime.now(timezone.utc).isoformat()
            report["ok"] = all(p["ok"] for p in report["phases"]) and bool(report["phases"])
            save()
    return report["ok"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client", choices=("codex", "claudecode"))
    parser.add_argument("--output", required=True)
    parser.add_argument("--model")
    parser.add_argument("--phase", action="append", choices=("machine", "search", "agents", "goal", "device", "screen"))
    parser.add_argument("--max-calls", type=int, default=80)
    parser.add_argument("--phase-timeout", type=float, default=240)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(run(args)) else 1)
