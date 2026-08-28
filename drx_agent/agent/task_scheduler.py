"""Priority queue with per-target concurrency for sub-agent task scheduling."""

import asyncio
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional


class TaskPriority(IntEnum):
    EXPLOIT = 0
    RECON = 1
    LATERAL = 2
    PERSIST = 3
    REPORT = 4


@dataclass(order=True)
class ScheduledTask:
    priority: int
    task_id: str = field(compare=False)
    agent_type: str = field(compare=False)
    target: str = field(compare=False)
    description: str = field(compare=False)
    ttl: int = field(compare=False)


class TaskScheduler:
    """Priority queue scheduler with per-target concurrency limiting."""

    def __init__(self, max_concurrent_per_target: int = 3, global_qps: Optional[float] = None):
        self._queue: list[ScheduledTask] = []
        self._target_concurrency: dict[str, int] = {}
        self.max_concurrent_per_target = max_concurrent_per_target
        self.global_qps = global_qps
        self._last_slot_ts: Optional[float] = None

    async def wait_for_slot(self) -> None:
        qps = self.global_qps
        if not qps or qps <= 0:
            return
        interval = 1.0 / qps
        now = time.monotonic()
        if self._last_slot_ts is not None:
            wait = self._last_slot_ts + interval - now
            if wait > 0:
                await asyncio.sleep(wait)
        self._last_slot_ts = time.monotonic()

    def try_acquire(self, target: str) -> bool:
        """Take one in-flight slot for *target*. False if already at cap.

        Dispatch awaits the sub-agent inline, so there is no worker draining
        a queue. Capacity is a semaphore, not enqueue-then-dequeue (which
        leaked slots when dequeue popped a different task).
        """
        current = self._target_concurrency.get(target, 0)
        if current >= self.max_concurrent_per_target:
            return False
        self._target_concurrency[target] = current + 1
        return True

    def concurrency_for(self, target: str) -> int:
        """In-flight count for *target* (0 if none)."""
        return self._target_concurrency.get(target, 0)

    def enqueue(self, task: ScheduledTask) -> None:
        """Add a task to the queue. Tasks are sorted by priority after insertion."""
        self._queue.append(task)
        self._queue.sort(key=lambda t: t.priority)

    def dequeue(self) -> Optional[ScheduledTask]:
        """Pop the highest-priority task whose target has capacity."""
        for i, task in enumerate(self._queue):
            current = self._target_concurrency.get(task.target, 0)
            if current < self.max_concurrent_per_target:
                self._target_concurrency[task.target] = current + 1
                return self._queue.pop(i)
        return None

    def task_completed(self, target: str) -> None:
        """Release one concurrency slot for *target*."""
        current = self._target_concurrency.get(target, 0)
        if current > 0:
            self._target_concurrency[target] = current - 1

    @property
    def pending(self) -> int:
        """Return the number of tasks still in the queue."""
        return len(self._queue)

    def cancel_target(self, target: str) -> int:
        """Remove all queued tasks for *target*. Returns the number removed."""
        before = len(self._queue)
        self._queue = [t for t in self._queue if t.target != target]
        return before - len(self._queue)

