"""Explicit local after-turn commands; serialized, bounded and cancellable."""
import asyncio

from .machine import Machine

HOOK_TIMEOUT = 30


class AfterTurn:
    def __init__(self, command, work, event):
        self.command, self.event = command, event
        self.machine = Machine(work)
        self.tasks = set()
        self.previous = None

    def submit(self):
        previous = self.previous

        async def run():
            if previous:
                await previous
            try:
                async with asyncio.timeout(HOOK_TIMEOUT):
                    output, failed = await self.machine.run("shell", {"command": self.command}, "after-turn")
                if failed:
                    self.event("hook_error", output)
            except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                self.event("hook_error", f"After-turn command failed ({type(error).__name__})")

        task = asyncio.create_task(run())
        self.previous = task
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def wait(self):
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await self.wait()
        self.machine.close()
