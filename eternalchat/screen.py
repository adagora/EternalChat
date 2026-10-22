"""Exclusive screen-task ownership, bound to a particular device connection.

Adapters supply the action callback. Holding a lease never reroutes by device
name: a replaced connection cannot inherit an earlier task's next click.
"""


class Screen:
    def __init__(self, identity):
        self.identity = identity
        self.lease = None

    def check(self, caller):
        if self.lease is not None and self.lease.owner != caller:
            raise ValueError(f"Screen task {self.lease.owner} is still working on {self.lease.name}; one task at a time")

    def reserve(self, owner, name, run):
        if self.lease is not None:
            raise ValueError(f"Screen task {self.lease.owner} is still working on {self.lease.name}; one task at a time")
        self.lease = ScreenLease(self, owner, name, run)
        return self.lease


class ScreenLease:
    def __init__(self, screen, owner, name, run):
        self.screen, self.owner, self.name = screen, owner, name
        self.identity, self._run = screen.identity, run

    async def run(self, args):
        if self.screen.lease is not self:
            raise ValueError("This screen task no longer owns the device")
        if not isinstance(args, dict) or "device" in args:
            raise ValueError("Screen actions are bound to this task's device; do not supply device")
        return await self._run("computer", dict(args), self.owner)

    def release(self):
        if self.screen.lease is self:
            self.screen.lease = None
