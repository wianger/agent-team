"""Read arbitrary-length lines without StreamReader.readline's frame limit."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable


async def iter_lines(
    reader: asyncio.StreamReader, *, on_activity: Callable[[], None] | None = None
) -> AsyncIterator[bytes]:
    """Observe incoming chunks, including partial frames, without limiting line length."""
    parts = []
    while chunk := await reader.read(65_536):
        if on_activity:
            on_activity()
        start = 0
        while (end := chunk.find(b"\n", start)) != -1:
            parts.append(chunk[start : end + 1])
            yield b"".join(parts)
            parts.clear()
            start = end + 1
        if start < len(chunk):
            parts.append(chunk[start:])
    if parts:
        yield b"".join(parts)


async def readline(reader: asyncio.StreamReader) -> bytes:
    parts = []
    while True:
        try:
            parts.append(await reader.readuntil(b"\n"))
            return b"".join(parts)
        except asyncio.LimitOverrunError as exc:
            # Consume the safe prefix and continue; the buffer size is not a line cap.
            parts.append(await reader.readexactly(exc.consumed))
        except asyncio.IncompleteReadError as exc:
            parts.append(exc.partial)
            return b"".join(parts)
