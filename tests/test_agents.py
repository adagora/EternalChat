"""Agents the model starts itself (spawn) and talks to (tell)."""
import asyncio
import json
from unittest.mock import patch

from eternalchat.llm import Response
from eternalchat.prompt import BOSS_NAMES
from tests.test_engine import EngineFixture, Scripted, text_response


def is_summary(kw):
    return kw["messages"][0]["content"][-1]["text"].startswith("Compaction:")


def spawn(*tasks, **more):
    return Response([{"type": "tool_use", "id": "sp", "name": "spawn", "input": {"tasks": list(tasks), **more}}],
                    "tool_use")


def last_text(kw):
    return next(b["text"] for b in reversed(kw["messages"][-1]["content"]) if b["type"] == "text")


class AgentTests(EngineFixture):
    async def settle(self, engine):
        """Agents work in the background: wait for them, their reports' turns and summaries."""
        while any(not job.done() for job in engine.agents.values()):
            await asyncio.gather(*engine.agents.values(), return_exceptions=True)
            await engine.finish()
        await engine.finish()

    def work(self, engine):
        return [engine.store.message(i)["text"] for i in range(engine.store.message_count)
                if engine.store.message(i)["kind"] == "work"]

    async def test_spawn_starts_agents_in_parallel_and_each_reports(self):
        both = asyncio.Barrier(2)

        async def handler(kw):
            if is_summary(kw):
                return text_response("summary")
            if kw["purpose"] == "agent":
                await asyncio.wait_for(both.wait(), 5)  # both run at once
                return text_response("report on " + last_text(kw))
            if len(kw["messages"]) == 1 and "please" in last_text(kw):
                return spawn("count the birds", "count the trees")
            return text_response("ok")
        provider = Scripted(handler)
        engine = self.engine(provider)
        await engine.ask("please send two agents")
        await self.settle(engine)
        reports = self.work(engine)
        self.assertEqual(len(reports), 2)
        self.assertTrue(all(r.startswith("[") and "] report on count the " in r for r in reports))
        names = sorted(r[1:r.index("]")] for r in reports)
        self.assertTrue(all(name[0].isupper() and name.isalpha() for name in names))
        started = next(engine.store.message(i)["text"] for i in range(engine.store.message_count)
                       if engine.store.message(i)["kind"] == "echo")
        self.assertIn("Started", started)
        agent_calls = [c for c in provider.calls if c["purpose"] == "agent"]
        self.assertTrue(agent_calls)
        self.assertFalse({t["name"] for c in agent_calls for t in c["tools"]} & BOSS_NAMES)
        self.assertEqual(engine.agent_log(names[0])["status"], "complete")

    async def test_a_running_agent_reads_what_it_is_told_and_answers_it(self):
        release = asyncio.Event()

        async def handler(kw):
            if is_summary(kw):
                return text_response("summary")
            if "Eternal tells you" in last_text(kw):
                return text_response("also did: " + last_text(kw).split("\n", 1)[1])
            await release.wait()
            return text_response("first part done")
        engine = self.engine(Scripted(handler))
        engine.start_agent("Helper", "do the first part")
        await asyncio.sleep(0.05)
        self.assertIn("reads it between its tool calls", engine.tell("Helper", "the second part"))
        release.set()
        await self.settle(engine)
        report = self.work(engine)[0]
        self.assertIn("first part done", report)
        self.assertIn("also did: the second part", report)
        records = engine.agent_log("Helper")["records"]
        self.assertIn("the second part", [r["text"] for r in records if r["kind"] == "user"])

    async def test_a_finished_agent_goes_on_and_its_reply_arrives_once(self):
        async def handler(kw):
            if is_summary(kw):
                return text_response("summary")
            if "transcript so far" in json.dumps(kw["messages"]):
                return text_response("reply to " + last_text(kw))
            return text_response("first report")
        provider = Scripted(handler)
        engine = self.engine(provider)
        await engine.start_agent("Scout", "look around")
        await self.settle(engine)
        self.assertEqual(engine.tell("Scout", "look closer"), "Sent. Scout's reply will reach you as a message.")
        await self.settle(engine)
        self.assertEqual(self.work(engine), ["[Scout] first report", "[Scout] reply to look closer"])
        continued = [c for c in provider.calls if "transcript so far" in json.dumps(c["messages"])]
        self.assertIn("look around", json.dumps(continued[0]["messages"]))  # its own history comes back
        self.assertEqual(engine.store.agent_state("Scout")["replies"], 1)
        self.assertIn("No agent named Nobody", engine.tell("Nobody", "hi"))
        # A reply saved but lost before delivery arrives after a restart, once.
        with patch.object(engine, "submit", side_effect=RuntimeError("died before delivery")):
            engine.tell("Scout", "and again")
            with self.assertRaisesRegex(RuntimeError, "died before delivery"):
                await engine.agents["Scout"]
        await engine.close()
        for _ in range(2):
            engine = self.engine(provider)
            engine.resume_reports()
            await engine.finish()
            self.assertEqual(self.work(engine)[-1], "[Scout] reply to and again")
            self.assertEqual(len(self.work(engine)), 3)
            await engine.close()

    async def test_effort_reaches_providers_that_take_it_and_bad_spawns_fail(self):
        async def handler(kw):
            if is_summary(kw):
                return text_response("summary")
            if kw["purpose"] == "agent":
                return text_response("done")
            if len(kw["messages"]) == 1 and last_text(kw) == "go":
                return spawn("think hard", effort="high")
            return text_response("ok")
        provider = Scripted(handler)
        provider.per_call_effort = True
        engine = self.engine(provider)
        await engine.ask("go")
        await self.settle(engine)
        agents = [c for c in provider.calls if c["purpose"] == "agent"]
        self.assertEqual({c.get("effort") for c in agents}, {"high"})
        self.assertTrue(all("effort" not in c for c in provider.calls if c["purpose"] == "turn"))
        for args in ({"tasks": []}, {"tasks": ["x"] * 9}, {"tasks": [""]}, {"tasks": ["x"], "other": 1},
                     {"name": "Scout"}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                engine._boss("tell" if "name" in args else "spawn", args)


class EffortTests(EngineFixture):
    def test_a_call_may_ask_another_effort_than_the_saved_one(self):
        from eternalchat.cli_provider import CLIProvider
        claude = CLIProvider("claudecode", executable="/bin/sh", reasoning_effort="low")
        self.assertTrue(claude.per_call_effort)
        self.assertEqual(claude.command("sonnet")[-2:], ["--effort", "low"])
        self.assertEqual(claude.command("sonnet", "high")[-2:], ["--effort", "high"])
        self.assertNotIn("--effort", CLIProvider("claudecode", executable="/bin/sh").command("sonnet"))

    async def test_spawn_stops_at_the_most_agents_at_work(self):
        hold = asyncio.Event()

        async def handler(kw):
            if is_summary(kw):
                return text_response("summary")
            await hold.wait()
            return text_response("done")
        engine = self.engine(Scripted(handler))
        self.assertIn("Started", engine._boss("spawn", {"tasks": [f"task {k}" for k in range(8)]}))
        self.assertIn("Not started: 8 agents are at work", engine._boss("spawn", {"tasks": ["one more"]}))
        hold.set()
        await asyncio.gather(*engine.agents.values())
