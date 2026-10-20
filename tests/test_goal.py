"""/goal: keep working until a check after each turn finds the goal met."""
import asyncio
from unittest.mock import patch

from eternalchat.errors import ProviderError
from eternalchat.prompt import GOAL_NO, GOAL_SYSTEM
from tests.test_engine import EngineFixture, Scripted, text_response


def is_summary(kw):
    return kw["messages"][0]["content"][-1]["text"].startswith("Compaction:")


class GoalTests(EngineFixture):
    def scripted(self, verdicts):
        """Turns answer "working"; checks answer the verdicts in order."""
        answers = iter(verdicts)

        async def handler(kw):
            if is_summary(kw):
                return text_response("summary")
            if kw["system"] == GOAL_SYSTEM:
                verdict = next(answers)
                if isinstance(verdict, Exception):
                    raise verdict
                return text_response(verdict)
            return text_response("working")
        return Scripted(handler)

    def log(self, engine):
        return [(engine.store.message(i)["kind"], engine.store.message(i)["text"])
                for i in range(engine.store.message_count)]

    async def settle(self, engine):
        while engine.turn_task and not engine.turn_task.done():
            await engine.finish()

    async def test_the_chat_works_until_a_check_finds_the_goal_met(self):
        provider = self.scripted(["no", "No.", "Yes"])
        engine = self.engine(provider)
        await engine.set_goal("make the tests pass")
        await self.settle(engine)
        self.assertEqual(self.log(engine), [
            ("user", "make the tests pass"), ("unii", "working"),
            ("user", GOAL_NO), ("unii", "working"),
            ("user", GOAL_NO), ("unii", "working"),
            ("note", "Goal met.")])
        self.assertIsNone(engine.goal)
        self.assertIsNone(engine.status()["goal"])
        checks = [c for c in provider.calls if c["system"] == GOAL_SYSTEM]
        self.assertEqual(len(checks), 3)
        self.assertTrue(all(c["tools"] == [] for c in checks))
        self.assertIn("The goal:\n\nmake the tests pass", checks[0]["messages"][0]["content"][-1]["text"])
        self.assertIn("|unii: working", checks[0]["messages"][0]["content"][-1]["text"])

    async def test_a_goal_never_met_stops_after_its_turns_until_the_user_writes(self):
        with patch("eternalchat.engine.GOAL_NUDGES", 2):
            engine = self.engine(self.scripted(["no"] * 10))
            await engine.set_goal("impossible")
            await self.settle(engine)
            kinds = [kind for kind, _ in self.log(engine)]
            self.assertEqual(kinds.count("user"), 3)  # the goal, then two nudges
            self.assertIn("not met after 2 more turns", self.log(engine)[-1][1])
            self.assertEqual(engine.goal, "impossible")  # it waits, not dropped
            await engine.ask("keep trying")
            await self.settle(engine)
            self.assertEqual([kind for kind, _ in self.log(engine)].count("user"), 6)

    async def test_halt_drops_the_goal_and_a_goal_survives_a_restart(self):
        release = asyncio.Event()

        async def handler(kw):
            if is_summary(kw):
                return text_response("summary")
            if kw["system"] == GOAL_SYSTEM:
                return text_response("no")
            await release.wait()
            return text_response("working")
        engine = self.engine(Scripted(handler))
        reply = engine.set_goal("tidy up")
        await asyncio.sleep(0.05)
        await engine.close()
        with self.assertRaises(asyncio.CancelledError):
            await reply
        engine = self.engine(Scripted(handler))
        self.assertEqual(engine.goal, "tidy up")
        engine.halt()
        release.set()
        await engine.ask("one more")
        await self.settle(engine)
        self.assertNotIn(GOAL_NO, [text for _, text in self.log(engine)])
        await engine.close()
        self.assertIsNone(self.engine(Scripted(handler)).goal)

    async def test_a_failed_check_ends_the_work_and_keeps_the_goal(self):
        engine = self.engine(self.scripted([ProviderError("model busy")]))
        await engine.set_goal("ship it")
        await self.settle(engine)
        self.assertEqual(self.log(engine)[-1], ("note", "No goal check: model busy. The goal stays; /halt drops it."))
        self.assertEqual(engine.goal, "ship it")
        with self.assertRaises(ValueError):
            engine.set_goal("  ")
