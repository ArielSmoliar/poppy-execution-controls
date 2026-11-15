"""Bounded FIFO ordering for eligible tool calls; budgets live in the ledger."""

import asyncio


class Dispatcher:
    def __init__(self, policy, capacity, max_pending):
        if policy != "fifo" or type(capacity) is not int or capacity < 1:
            raise ValueError("Use fifo scheduling with a positive integer capacity")
        if type(max_pending) is not int or max_pending < 1:
            raise ValueError("max_pending must be a positive integer")
        self.capacity, self.max_pending = capacity, max_pending
        self.active = 0
        self.waiters = []

    async def enqueue(self, job):
        # Arrival sequence is assigned by the engine before acquire; no second queue.
        pass

    def grant(self):
        self.waiters = [w for w in self.waiters if not w[1].cancelled()]
        while self.active < self.capacity and self.waiters:
            chosen = min(self.waiters, key=lambda w: w[0]["seq"])
            self.waiters.remove(chosen)
            self.active += 1
            chosen[1].set_result(None)

    async def acquire(self, job):
        if len(self.waiters) >= self.max_pending:
            raise RuntimeError("Execution queue is full")
        future = asyncio.get_running_loop().create_future()
        item = (job, future)
        self.waiters.append(item)
        self.grant()
        try:
            await future
        except asyncio.CancelledError:
            if item in self.waiters:
                self.waiters.remove(item)
            elif not future.cancelled():
                # A grant raced with cancellation before this coroutine resumed.
                self.active -= 1
            self.grant()
            raise

    async def release(self, job, handle):
        self.active -= 1
        self.grant()
