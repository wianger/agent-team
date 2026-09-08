from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from .adapters import terminate_process


async def run_checks(
    commands: list[list[str]],
    workspace: Path,
    deadline_seconds: float,
    progress: Callable[[str], None],
    *,
    on_activity: Callable[[], None] | None = None,
) -> list[dict]:
    """Execute the unanimously accepted checks, retaining complete evidence and actual exits."""
    results = []
    for argv in commands:
        progress("Running acceptance check: " + repr(argv) + "\n")
        if on_activity:
            on_activity()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=workspace,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            results.append({"command": argv, "exit_code": -1, "output": str(exc)})
            break
        output = bytearray()

        async def drain(stdout=process.stdout, captured=output) -> None:
            while chunk := await stdout.read(4096):
                captured.extend(chunk)
                if on_activity:
                    on_activity()

        reader = asyncio.create_task(drain())
        code = -1
        try:
            try:
                async with asyncio.timeout(deadline_seconds or None):
                    await asyncio.shield(reader)
                    code = await process.wait()
            except TimeoutError:
                output.extend(b"\nCheck timed out.")
        finally:
            # Keep stdout draining while killing, including descendants holding the pipe open.
            await terminate_process(process)
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        result = {"command": argv, "exit_code": code, "output": output.decode(errors="replace")}
        results.append(result)
        progress(f"Exit code {code}\n")
        if on_activity:
            on_activity()
        if code:
            break
    return results
