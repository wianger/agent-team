"""Observe silence without putting a deadline on the work being observed."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress


@asynccontextmanager
async def observe_activity(
    warning_seconds: float,
    warn: Callable[[float], None],
    *,
    on_activity: Callable[[], None] | None = None,
) -> AsyncIterator[Callable[[], None]]:
    loop = asyncio.get_running_loop()
    last_activity = loop.time()
    last_report = None
    closed = False
    changed = asyncio.Event()

    def touch() -> None:
        nonlocal last_activity, last_report
        if closed:
            return
        last_activity = loop.time()
        changed.set()
        # Report real I/O, not a timer heartbeat. Bursts must not flood clients.
        if on_activity and (last_report is None or last_activity - last_report >= 1):
            last_report = last_activity
            on_activity()

    async def watch() -> None:
        nonlocal last_report
        while True:
            changed.clear()
            idle_seconds = loop.time() - last_activity
            remaining = warning_seconds - idle_seconds
            if remaining <= 0:
                warn(idle_seconds)
                last_report = None  # The next activity must clear the idle indicator immediately.
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
        closed = True
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
