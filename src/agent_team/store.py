from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class Store:
    """One ordered, durable event log; only committed messages enter model context."""

    def __init__(self, path: Path) -> None:
        # Set permissions before SQLite creates a WAL using the database's mode.
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        path.chmod(0o600)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS events "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS agent_sessions "
            "(speaker TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        self.db.commit()

    def append(
        self, kind: str, *, session_update: tuple[str, dict] | None = None, **data: Any
    ) -> dict:
        event = {"type": kind, "time": datetime.now(UTC).isoformat(), **data}
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO events(payload) VALUES (?)",
                (json.dumps(event, ensure_ascii=False),),
            )
            if session_update:
                speaker, state = session_update
                # The author already has its reply in the backend session. Bind its
                # public cursor to that reply's ID in the very same transaction.
                if kind == "message":
                    if state.get("cursor_mode") == "input":
                        # Concurrent peer messages may precede this reply without having
                        # reached its author. Never acknowledge those unseen messages.
                        state = {
                            **state,
                            "known_own_messages": [
                                i
                                for i in state.get("known_own_messages", [])
                                if i > state["synced_through"]
                            ]
                            + [cursor.lastrowid],
                        }
                    else:
                        state = {**state, "synced_through": cursor.lastrowid}
                self._write_session(speaker, state)
        return {"id": cursor.lastrowid, **event}

    def _write_session(self, speaker: str, state: dict) -> None:
        self.db.execute(
            "INSERT INTO agent_sessions(speaker, payload) VALUES (?, ?) "
            "ON CONFLICT(speaker) DO UPDATE SET payload=excluded.payload",
            (speaker, json.dumps(state, ensure_ascii=False)),
        )

    def set_session(self, speaker: str, state: dict) -> None:
        with self.db:
            self._write_session(speaker, state)

    def sessions(self) -> dict[str, dict]:
        return {
            row[0]: json.loads(row[1])
            for row in self.db.execute("SELECT speaker, payload FROM agent_sessions")
        }

    def events(self, after: int = 0) -> list[dict]:
        return [
            {"id": row[0], **json.loads(row[1])}
            for row in self.db.execute(
                "SELECT id, payload FROM events WHERE id > ? ORDER BY id", (after,)
            )
        ]

    def messages(self) -> list[dict]:
        return [event for event in self.events() if event["type"] == "message"]

    def close(self) -> None:
        self.db.close()


def read_events(path: Path) -> list[dict]:
    if not path.is_file():
        raise ValueError(f"Session history does not exist: {path}")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return [
            {"id": row[0], **json.loads(row[1])}
            for row in db.execute("SELECT id, payload FROM events ORDER BY id")
        ]
    finally:
        db.close()
