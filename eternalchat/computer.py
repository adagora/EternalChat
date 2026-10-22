"""Mac screen actions, serialized across callers, with a screenshot after each.

Adapted from UniiChat's Computer.ts/Computer.swift (THIRD_PARTY_NOTICES.md).
The Swift helper is compiled lazily; macOS tools capture/resize the main display.
Results use Messages content blocks so images reach every model connection.
"""
import asyncio
import hashlib
import json
import math
import os
import signal
import sys
import tempfile
from importlib import resources
from pathlib import Path

from .config import MAX_IMAGE_BYTES
from .prompt import image_block, text_block

WAIT = 100
SETTLE = 0.5
ACTIONS = frozenset({"screenshot", "zoom", "wait", "cursor_position", "key", "hold_key", "type",
                     "mouse_move", "left_mouse_down", "left_mouse_up", "left_click", "left_click_drag",
                     "right_click", "middle_click", "double_click", "triple_click", "scroll"})


def validate(args):
    """Reject malformed actions before posting any input to the desktop."""
    if not isinstance(args, dict) or not isinstance(args.get("action"), str) or args["action"] not in ACTIONS:
        raise ValueError("Unknown computer action")
    allowed = {"action", "coordinate", "start_coordinate", "text", "duration", "scroll_direction",
               "scroll_amount", "region", "repeat"}
    if set(args) - allowed:
        raise ValueError("Unknown computer arguments")
    action = args["action"]
    for field, length in (("coordinate", 2), ("start_coordinate", 2), ("region", 4)):
        if field in args:
            value = args[field]
            if not isinstance(value, list) or len(value) != length or any(type(n) is not int or n < 0 for n in value):
                raise ValueError(f"{field} must contain {length} nonnegative integer pixels")
    for field in ("text", "scroll_direction"):
        if field in args and not isinstance(args[field], str):
            raise ValueError(f"{field} must be a string")
    for field, low, high in (("scroll_amount", 0, 1000), ("repeat", 1, 100)):
        if field in args and (type(args[field]) is not int or not low <= args[field] <= high):
            raise ValueError(f"{field} must be an integer from {low} to {high}")
    if "duration" in args and (type(args["duration"]) not in (int, float) or not 0 <= args["duration"] <= WAIT):
        raise ValueError(f"duration must be from 0 to {WAIT} seconds")
    required = {"type": ("text",), "key": ("text",), "hold_key": ("text", "duration"),
                "wait": ("duration",), "mouse_move": ("coordinate",),
                "left_click_drag": ("start_coordinate", "coordinate"), "zoom": ("region",),
                "scroll": ("scroll_direction", "scroll_amount")}.get(action, ())
    if any(field not in args for field in required):
        raise ValueError(f"{action} requires {', '.join(required)}")
    if action in ("key", "hold_key") and not args["text"].strip():
        raise ValueError("text must name a key or key combination")
    if action == "scroll" and args["scroll_direction"] not in ("up", "down", "left", "right"):
        raise ValueError("scroll_direction must be up, down, left or right")
    if action == "zoom":
        x0, y0, x1, y1 = args["region"]
        if x1 <= x0 or y1 <= y0:
            raise ValueError("region must be [x0, y0, x1, y1], with x1 > x0 and y1 > y0")


class Computer:
    def __init__(self, cache):
        self.cache = Path(cache)
        self.lock = asyncio.Lock()  # one mouse/keyboard, even with several agents
        self.processes = set()

    @property
    def supported(self):
        return sys.platform == "darwin"

    async def run(self, args):
        validate(args)
        if not self.supported:
            raise RuntimeError("Screen control needs a Mac. Join one with eternalchat device and name it in device.")
        async with self.lock:
            action = args["action"]
            # Check coordinates and permissions before any input is posted.
            done = await self.hands("screenshot", {})
            w, h = done["w"], done["h"]
            for field in ("coordinate", "start_coordinate"):
                if field in args and (args[field][0] >= w or args[field][1] >= h):
                    raise ValueError(f"{field} must be inside the {w}x{h} screenshot")
            if action == "zoom":
                x0, y0, x1, y1 = args["region"]
                if x1 > w or y1 > h:
                    raise ValueError(f"region must be inside the {w}x{h} screenshot")
                scale = done["scale"]
                area = [round(x0 * scale), round(y0 * scale), round((x1 - x0) * scale), round((y1 - y0) * scale)]
                fit = min(w / area[2], h / area[3])
                return await self.shot(["-R", ",".join(map(str, area))],
                                       max(1, round(area[2] * fit)), max(1, round(area[3] * fit)))
            if action == "wait":
                await asyncio.sleep(args["duration"])
            elif action == "hold_key":
                # Even a stop during the key-down helper must send key-up.
                try:
                    done = await self.hands("press", args)
                    await asyncio.sleep(args["duration"])
                finally:
                    await self.hands("release", args)
            elif action != "screenshot":
                try:
                    done = await self.hands(action, args)
                except asyncio.CancelledError:
                    # A killed click/drag/type helper may have left an input held.
                    await self.hands("reset", args)
                    raise
            await asyncio.sleep(SETTLE)
            content, failed = await self.shot([], w, h)
            content[0]["text"] = done["text"] + "\n" + content[0]["text"]
            return content, failed

    async def hands(self, name, args):
        source = resources.files(__package__).joinpath("computer.swift").read_bytes()
        digest = hashlib.sha256(source).hexdigest()[:20]
        binary = self.cache / ("computer-" + digest)
        if not binary.is_file():
            self.cache.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="build-", dir=self.cache) as folder:
                src, built = Path(folder, "computer.swift"), Path(folder, "computer")
                src.write_bytes(source)
                try:
                    await self.execute("/usr/bin/swiftc", "-O", str(src), "-o", str(built), timeout=120)
                except FileNotFoundError:
                    raise RuntimeError("Screen control needs Apple's Command Line Tools: run xcode-select --install.") from None
                os.replace(built, binary)
        value = json.loads(await self.execute(str(binary), json.dumps({"name": name, "input": args})))
        if (not isinstance(value, dict) or type(value.get("w")) is not int or type(value.get("h")) is not int
                or not 1 <= value["w"] <= 1280 or not 1 <= value["h"] <= 800
                or type(value.get("scale")) not in (int, float) or not math.isfinite(value["scale"])
                or value["scale"] < 1 or not isinstance(value.get("text"), str)):
            raise RuntimeError("The screen helper returned invalid dimensions")
        return value

    async def shot(self, options, w, h):
        with tempfile.TemporaryDirectory(prefix="eternalchat-screen-") as folder:
            image = Path(folder, "screen.jpg")
            try:
                await self.execute("/usr/sbin/screencapture", "-x", "-t", "jpg", *options,
                                   *([] if options else ["-m"]), str(image))
                await self.execute("/usr/bin/sips", "-z", str(h), str(w), str(image))
            except RuntimeError as error:
                raise RuntimeError(f"Screenshot failed: {error}. Enable Screen Recording for the app running "
                                   "EternalChat in System Settings → Privacy & Security, then restart it.") from None
            with image.open("rb") as stream:
                data = stream.read(MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError("Screenshot exceeds the 5 MB image limit")
            if not data.startswith(b"\xff\xd8\xff"):
                raise ValueError("Screen capture did not return a JPEG")
            return [text_block(f"[image {w}x{h}]"), image_block("image/jpeg", data)], False

    async def execute(self, *argv, timeout=30):
        """A cancellable native command; arguments never go through a shell."""
        process = await asyncio.create_subprocess_exec(*argv, start_new_session=True, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.processes.add(process)
        try:
            try:
                out, err = await asyncio.wait_for(process.communicate(), timeout)
            except TimeoutError:
                raise RuntimeError(f"{Path(argv[0]).name} timed out after {timeout} seconds") from None
            if process.returncode:
                detail = (err or out).decode("utf-8", errors="replace").strip()
                raise RuntimeError(detail[:2000] or f"{Path(argv[0]).name}: exit {process.returncode}")
            return out.decode("utf-8", errors="replace").strip()
        finally:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
            self.processes.discard(process)

    def close(self):
        for process in self.processes:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
