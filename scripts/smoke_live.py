"""Optional real CLI smoke test. Invokes each configured agent once and uses account quota."""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import replace
from pathlib import Path

from agent_team.config import load_config
from agent_team.engine import Room
from agent_team.store import Store


async def main():
    config = load_config(Path("team.toml"))
    config = replace(config, workflow="discussion", turn_timeout=90, turn_delay=0)
    finished = asyncio.Event()
    errors = []
    speakers = set()

    def on_event(event):
        kind = event["type"]
        if kind == "message":
            print(f"{event['speaker']}: {event['text']}", flush=True)
            if event["role"] == "agent":
                speakers.add(event["speaker"])
                if len(speakers) == len(config.agents):
                    room.control("pause")
                    finished.set()
        elif kind == "floor.granted":
            print(
                f"[Floor: {event['speaker']}; context through #{event['context_through']}]",
                flush=True,
            )
        elif kind == "error":
            errors.append(event["text"])
            print(f"[error] {event['text']}", flush=True)
        elif kind == "state" and event["reason"] in {"error", "all_passed"}:
            if event["active"] is None:
                finished.set()

    with tempfile.TemporaryDirectory(prefix="agent-team-live-") as directory:
        store = Store(Path(directory) / "events.sqlite3")
        room = Room(config, store, on_event)
        room.start()
        try:
            room.say(
                "tester",
                "Integration test: introduce yourself as a team member in one sentence, "
                "and acknowledge anyone who has already spoken. Do not use tools.",
            )
            async with asyncio.timeout(200):
                await finished.wait()
            if errors:
                raise SystemExit(1)
            replies = [m for m in room.messages if m["role"] == "agent"]
            if len(replies) != len(config.agents):
                raise SystemExit("Some agents did not reply")
            print(
                f"[passed] {len(replies)} real CLIs completed the shared-context conversation.",
                flush=True,
            )
        finally:
            await room.close()
            store.close()


if __name__ == "__main__":
    asyncio.run(main())
