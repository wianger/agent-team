"""Real idea → consensus → implementation → review → checks test, in a temporary workspace."""

from __future__ import annotations

import asyncio
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from agent_team.config import load_config
from agent_team.engine import Room
from agent_team.store import Store


async def main():
    finished = asyncio.Event()
    errors = []

    def on_event(event):
        kind = event["type"]
        if kind == "floor.granted":
            print(f"[{event['phase']}] {event['speaker']}", flush=True)
        elif kind == "message":
            print(f"{event['speaker']}: {event['text']}", flush=True)
        elif kind == "workflow.changed":
            print(f"[workflow] {event['text']}", flush=True)
        elif kind == "error":
            errors.append(event["text"])
            print(f"[error] {event['text']}", flush=True)
        elif (
            kind == "state"
            and event["active"] is None
            and event["reason"]
            in {
                "completed",
                "error",
                "blocked",
                "no_consensus",
            }
        ):
            finished.set()

    with tempfile.TemporaryDirectory(prefix="agent-team-workflow-") as directory:
        config = replace(
            load_config(Path("team.toml")),
            workspace=Path(directory),
            workflow="build",
            turn_timeout=90,
            work_timeout=150,
            turn_delay=0,
        )
        store = Store(Path(directory) / "events.sqlite3")
        room = Room(config, store, on_event)
        room.start()
        try:
            room.say(
                "tester",
                (
                    "Agree on and build a minimal Python addition module together. "
                    "Create calc.py with add(a, b) and test_calc.py using unittest to check "
                    "positive, negative and zero inputs. Use shared milestones: code, then tests, "
                    "not private assignments. Submit an implementation checkpoint and judge it "
                    "before continuing; both members should write and judge peer work. "
                    "Only these two files are needed, with no dependencies, git operations, "
                    "or extra documentation. "
                    f"Checks: [{sys.executable!r}, '-m', 'unittest', '-v', 'test_calc']. "
                    "First propose the plan, then each member explicitly approves the same version."
                ),
            )
            async with asyncio.timeout(600):
                await finished.wait()
            if errors or room.reason != "completed":
                raise SystemExit(f"Workflow did not complete: {room.reason}")
            contributions = [
                c for task in room.workflow.data["proposal"]["tasks"] for c in task["contributions"]
            ]
            assert {c["author"] for c in contributions} == {a.name for a in config.agents}
            assert all(c["judgments"] for c in contributions)
            assert (config.workspace / "calc.py").is_file()
            assert (config.workspace / "test_calc.py").is_file()
            print(
                "[passed] Real CLIs agreed, built shared files, judged peers, and passed checks.",
                flush=True,
            )
        finally:
            await room.close()
            store.close()


if __name__ == "__main__":
    asyncio.run(main())
