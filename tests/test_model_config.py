"""Provider selection, agent model routing, API controls and after-turn hooks."""
import asyncio
import io
import json
import os
from pathlib import Path
from unittest.mock import patch

from eternalchat import settings
from eternalchat.app import open_chat
from eternalchat.cli import parser, run, setup
from eternalchat.config import Config
from eternalchat.demo import DemoProvider
from eternalchat.errors import ProviderError
from eternalchat.hooks import AfterTurn
from eternalchat.provider import AnthropicProvider
from tests.test_engine import EngineFixture, Scripted, text_response
from tests.test_provider_cli import sse, stream_events


class ModelConfigTests(EngineFixture):
    async def test_api_selection_is_explicit_and_never_falls_back(self):
        folder = str(Path(self.temp.name, "chat"))
        args = parser().parse_args(["--connection", "api", "--api-provider", "openai", "--data-dir", folder,
            "--tools", "off", "--reasoning-effort", "high", "--service-tier", "priority", "ask", "hello"])
        with patch.dict(os.environ, {"OPENAI_API_KEY": "api-secret", "ANTHROPIC_API_KEY": "other-secret"}):
            with patch("eternalchat.cli.OpenAIProvider", return_value=DemoProvider()) as openai, \
                 patch("eternalchat.cli.AnthropicProvider", side_effect=AssertionError("wrong billing provider")), \
                 patch("eternalchat.cli.CLIProvider", side_effect=AssertionError("wrong auth mode")):
                self.assertEqual(await run(args), 0)
                self.assertEqual(openai.call_args.args, ("api-secret",))
                self.assertEqual(openai.call_args.kwargs["reasoning_effort"], "high")
                self.assertEqual(openai.call_args.kwargs["service_tier"], "priority")
            with patch("eternalchat.cli.OpenAIProvider", side_effect=ProviderError("no credit")), \
                 patch("eternalchat.cli.AnthropicProvider") as alternate:
                with self.assertRaisesRegex(ProviderError, "no credit"):
                    await run(args)
                alternate.assert_not_called()

    async def test_agent_and_screen_model_inherits_or_uses_its_own_pick(self):
        async def answer(kw):
            return text_response("agent answer" if kw["purpose"] == "agent" else "reply")
        for chosen in ("", "agent-model"):
            provider = Scripted(answer)
            engine = open_chat(Path(self.temp.name, chosen or "inherit"), provider,
                               Config(turn_model="reply-model", agent_model=chosen))
            self.engines.append(engine)
            await engine.start_agent("Scout", "work")
            await engine.finish()
            agents = [c for c in provider.calls if c["purpose"] == "agent"]
            self.assertEqual(agents[0]["model"], chosen or "reply-model")
            self.assertTrue(all(c["model"] == "reply-model" for c in provider.calls if c["purpose"] == "turn"))

    async def test_anthropic_effort_fast_beta_and_summary_defaults(self):
        provider = AnthropicProvider("api-secret", reasoning_effort="high", speed="fast")
        args = dict(model="claude-opus-5", system="system", tools=[],
                    messages=[{"role": "user", "content": [{"type": "text", "text": "task"}]}])
        sent = []

        def response(request, timeout):
            sent.append(request)
            return io.BytesIO(sse(stream_events()))

        with patch("eternalchat.provider.urlopen", side_effect=response):
            await provider.complete(**args)
            await provider.complete(**args, purpose="memory")
        first, second = [json.loads(request.data) for request in sent]
        self.assertEqual(first["thinking"], {"type": "adaptive"})
        self.assertEqual(first["output_config"], {"effort": "high"})
        self.assertEqual(first["speed"], "fast")
        self.assertEqual(sent[0].get_header("Anthropic-beta"), "fast-mode-2026-02-01")
        self.assertNotIn("thinking", second)
        self.assertNotIn("speed", second)
        with self.assertRaises(ProviderError):
            await provider.complete(**{**args, "model": "claude-opus-4-6"})

    async def test_hooks_run_serially_after_turns_with_tools_off_and_no_model_keys(self):
        root = Path(self.temp.name)
        output = root / "hooks.txt"
        command = f"sleep 0.02; printf 'x' >> '{output}'; test -z \"$OPENAI_API_KEY\" && test -z \"$ANTHROPIC_API_KEY\""
        events = []
        hook = AfterTurn(command, root / "work", lambda *e: events.append(e))
        engine = open_chat(root / "chat", DemoProvider(), after_turn=hook)
        self.engines.append(engine)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret", "ANTHROPIC_API_KEY": "secret"}):
            await engine.ask("first")
            await engine.ask("second")
            await engine.finish()
        self.assertEqual(output.read_text(), "xx")
        self.assertFalse(events)
        self.assertIsNone(engine.machine)

    async def test_hook_failure_does_not_turn_a_saved_reply_into_an_error(self):
        events = []
        hook = AfterTurn("exit 7", Path(self.temp.name, "work"), lambda *e: events.append(e))
        engine = open_chat(Path(self.temp.name, "chat"), DemoProvider(), after_turn=hook)
        self.engines.append(engine)
        self.assertIn("Saved", await engine.ask("hello"))
        await engine.finish()
        self.assertEqual(events[0][0], "hook_error")
        self.assertEqual(len(engine.messages()), 2)

    async def test_hook_timeout_reaps_its_process(self):
        events = []
        hook = AfterTurn("sleep 30", Path(self.temp.name, "work"), lambda *e: events.append(e))
        self.addAsyncCleanup(hook.close)
        with patch("eternalchat.hooks.HOOK_TIMEOUT", 0.05):
            hook.submit()
            await asyncio.wait_for(hook.wait(), 3)
        self.assertFalse(hook.machine._jobs)
        self.assertEqual(events[0][0], "hook_error")
        self.assertIn("TimeoutError", events[0][1])

    async def test_hook_on_stopped_turn_and_close_cleans_process(self):
        reached = asyncio.Event()

        async def blocked(kw):
            if kw["purpose"] in ("memory", "summary"):
                return text_response("memory")
            reached.set()
            await asyncio.Event().wait()

        events = []
        hook = AfterTurn("sleep 30", Path(self.temp.name, "work"), lambda *e: events.append(e))
        engine = open_chat(Path(self.temp.name, "chat"), Scripted(blocked), after_turn=hook)
        self.engines.append(engine)
        future = engine.submit("work")
        await asyncio.wait_for(reached.wait(), 1)
        engine.stop()
        await asyncio.gather(future, return_exceptions=True)
        async with asyncio.timeout(2):
            while not hook.machine._jobs:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(engine.close(), 4)
        self.assertFalse(hook.machine._jobs)

    async def test_setup_openai_and_saved_agent_flags(self):
        with patch.dict(os.environ, {"ETERNALCHAT_HOME": self.temp.name}):
            args = parser().parse_args(["setup", "--api-provider", "openai", "--agent-model", "agent",
                                       "--reasoning-effort", "high", "--service-tier", "priority", "--after-turn", "true"])
            answers = iter(["4", "", "", ""])
            setup(args, read=lambda prompt: next(answers), write=lambda *a: None)
            saved = settings.load()
            self.assertEqual(saved["api_provider"], "openai")
            self.assertEqual(saved["agent_model"], "agent")
            self.assertEqual(saved["reasoning_effort"], "high")
            self.assertEqual(saved["service_tier"], "priority")
            self.assertEqual(saved["after_turn"], "true")
            self.assertNotIn("api_speed", saved)
