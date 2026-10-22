"""Screen actions without touching a real desktop, plus model/image round trips."""
import asyncio
import base64
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from eternalchat.app import open_chat
from eternalchat.computer import Computer
from eternalchat.config import Config, MAX_IMAGE_BYTES
from eternalchat.devices import Devices
from eternalchat.llm import Response
from eternalchat.machine import Machine
from eternalchat.prompt import image_block, text_block, tool_content, tools_for
from tests.test_engine import EngineFixture, Scripted, text_response

JPEG = b"\xff\xd8\xff" + b"screen" * 100


def screenshot():
    return [text_block("[image 1280x800]"), image_block("image/jpeg", JPEG)]


class FakeComputer(Computer):
    @property
    def supported(self):
        return True

    async def hands(self, name, args):
        self.events.append((name, args))
        return {"w": 1280, "h": 800, "scale": 2, "text": "OK"}

    async def shot(self, options, w, h):
        self.events.append(("shot", options, w, h))
        return screenshot(), False


class ComputerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.computer = FakeComputer(self.temp.name)
        self.computer.events = []
        self.addCleanup(self.computer.close)
        self.settle = patch("eternalchat.computer.SETTLE", 0)
        self.settle.start()
        self.addCleanup(self.settle.stop)

    async def test_click_uses_full_screenshot_coordinates_and_returns_image(self):
        content, failed = await self.computer.run({"action": "left_click", "coordinate": [700, 400]})
        self.assertFalse(failed)
        self.assertEqual(self.computer.events, [("screenshot", {}),
                         ("left_click", {"action": "left_click", "coordinate": [700, 400]}),
                         ("shot", [], 1280, 800)])
        self.assertEqual(base64.b64decode(content[1]["source"]["data"]), JPEG)
        self.assertIn("OK", content[0]["text"])

    async def test_zoom_scales_region_to_screen_points(self):
        await self.computer.run({"action": "zoom", "region": [100, 50, 500, 250]})
        self.assertEqual(self.computer.events[-1], ("shot", ["-R", "200,100,800,400"], 1280, 640))

    async def test_bad_input_never_posts_an_action(self):
        for args in ({"action": "press"}, {"action": "key"}, {"action": "type", "text": 42},
                     {"action": "mouse_move", "coordinate": [True, 2]},
                     {"action": "wait", "duration": float("nan")}, {"action": "wait", "duration": 101},
                     {"action": "wait", "duration": 10 ** 1000},
                     {"action": "zoom", "region": [1, 2, 1, 5]},
                     {"action": "scroll", "scroll_direction": "other", "scroll_amount": 1},
                     {"action": "scroll", "scroll_direction": "up", "scroll_amount": 2 ** 32},
                     {"action": "screenshot", "unknown": True}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                await self.computer.run(args)
        self.assertEqual(self.computer.events, [])
        for args in ({"action": "mouse_move", "coordinate": [1280, 4]},
                     {"action": "zoom", "region": [0, 0, 1281, 400]}):
            self.computer.events.clear()
            with self.assertRaises(ValueError):
                await self.computer.run(args)
            self.assertEqual(self.computer.events, [("screenshot", {})])

    async def test_cancel_releases_a_held_key_and_an_interrupted_drag(self):
        started = asyncio.Event()
        original = self.computer.hands

        async def hands(name, args):
            result = await original(name, args)
            if name in ("press", "left_click_drag"):
                started.set()
                await asyncio.Event().wait()
            return result
        self.computer.hands = hands
        for action in ({"action": "hold_key", "text": "cmd+a", "duration": 100},
                       {"action": "left_click_drag", "start_coordinate": [1, 2], "coordinate": [10, 20]}):
            started.clear()
            task = asyncio.create_task(self.computer.run(action))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(self.computer.events[-1][0], "release" if action["action"] == "hold_key" else "reset")

    async def test_desktop_calls_serialize_and_cancelled_wait_unlocks(self):
        first = asyncio.create_task(self.computer.run({"action": "wait", "duration": 100}))
        while not self.computer.events:
            await asyncio.sleep(0)
        second = asyncio.create_task(self.computer.run({"action": "screenshot"}))
        await asyncio.sleep(0)
        self.assertEqual(self.computer.events, [("screenshot", {})])
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        await second
        self.assertEqual(len([e for e in self.computer.events if e[0] == "shot"]), 1)

    async def test_native_command_timeout_and_cancel_reap_the_child(self):
        for cancel in (False, True):
            task = asyncio.create_task(self.computer.execute(sys.executable, "-c", "import time; time.sleep(30)",
                                                             timeout=30 if cancel else 0.05))
            while not self.computer.processes:
                await asyncio.sleep(0)
            process = next(iter(self.computer.processes))
            if cancel:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            else:
                with self.assertRaisesRegex(RuntimeError, "timed out"):
                    await task
            self.assertIsNotNone(process.returncode)
            self.assertFalse(self.computer.processes)

    async def test_capture_temp_files_are_removed_on_success_and_failure(self):
        computer = Computer(self.temp.name)
        seen = []

        async def execute(*argv):
            seen.append(argv)
            if "screencapture" in argv[0]:
                Path(argv[-1]).write_bytes(JPEG)
            return ""
        computer.execute = execute
        self.assertEqual((await computer.shot([], 800, 600))[0][1], screenshot()[1])
        self.assertIn("-m", seen[0])
        self.assertFalse(Path(seen[0][-1]).exists())
        computer.execute = AsyncMock(side_effect=RuntimeError("no capture"))
        with self.assertRaisesRegex(RuntimeError, "Screen Recording"):
            await computer.shot([], 800, 600)

    async def test_non_mac_and_bad_result_content_fail_explicitly(self):
        with patch("eternalchat.computer.sys.platform", "linux"):
            with self.assertRaisesRegex(RuntimeError, "needs a Mac"):
                await Computer(self.temp.name).run({"action": "screenshot"})
        for value in ([{"type": "tool_use"}], [{"type": "image", "source": {}}],
                      screenshot() + [text_block("extra")],
                      [image_block("image/jpeg", b"x" * (MAX_IMAGE_BYTES + 1))],
                      [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "%%%"}}]):
            with self.assertRaises(ValueError):
                tool_content(value)


class ScreenTurnTests(EngineFixture):
    async def test_tools_off_and_remote_screen_capabilities(self):
        self.assertNotIn("computer", [tool["name"] for tool in tools_for(False)])
        self.assertIn("computer", [tool["name"] for tool in tools_for(True, boss=True)])
        self.assertNotIn("computer", [tool["name"] for tool in tools_for(True)])
        devices = Devices(Machine(Path(self.temp.name, "work")))
        self.addCleanup(devices.close)
        devices.join("linux", "/work")
        output, failed = await devices.run("computer", {"action": "screenshot", "device": "linux"}, "main")
        self.assertTrue(failed)
        self.assertIn("does not offer", output)
        remote = devices.join("mac", "/work", computer=True)
        self.assertIn("mac (your working folder: /work/main) [screen control]", devices.describe("main"))
        task = asyncio.create_task(devices.run("computer", {"action": "screenshot", "device": "mac"}, "main"))
        job = (await remote.outbox.get())["job"]
        remote.answer(job["id"], screenshot(), False)
        self.assertEqual(await task, (screenshot(), False))
