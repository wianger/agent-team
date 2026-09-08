"""Observe silence without putting a deadline on the work being observed."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress


@asynccontextmanager
async def observe_activity(
    warning_seconds: float, warn: Callable[[float], None]
) -> AsyncIterator[Callable[[], None]]:
    loop = asyncio.get_running_loop()
    last_activity = loop.time()
    changed = asyncio.Event()

    def touch() -> None:
        nonlocal last_activity
        last_activity = loop.time()
        changed.set()

    async def watch() -> None:
        while True:
            changed.clear()
            idle_seconds = loop.time() - last_activity
            remaining = warning_seconds - idle_seconds
            if remaining <= 0:
                warn(idle_seconds)
                # One warning per continuous silent period, not a repeating alarm.
                await changed.wait()
            else:
                try:
                    # This deadline only wakes the observer; it never cancels the worker.
                    await asyncio.wait_for(changed.wait(), remaining)
                except TimeoutError:
                    pass

    task = asyncio.create_task(watch(), name="idle-observer") if warning_seconds else None
    try:
        yield touch
    finally:
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
