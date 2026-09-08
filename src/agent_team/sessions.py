"""Private sessions are disposable caches; the public event log is authoritative."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict

from .config import AgentConfig, TeamConfig
from .store import Store

# Rebuild older private prompts that assigned backend-specific perspectives.
CONTEXT_PROTOCOL = 2


def session_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Backend session ID must be a UUID string")
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise ValueError("Backend session ID must be a UUID string") from exc


class Sessions:
    def __init__(self, config: TeamConfig, store: Store) -> None:
        self.config, self.store = config, store

    def fingerprint(self, agent: AgentConfig) -> str:
        data = {
            "agent": asdict(agent),
            "workspace": str(self.config.workspace.resolve()),
            "workflow": self.config.workflow,
            "context_protocol": CONTEXT_PROTOCOL,
            "context_mode": self.config.context_mode,
            "permission_mode": self.config.permission_mode,
            "interaction_mode": self.config.interaction_mode,
            "provider_state_root": os.environ.get(
                "CODEX_HOME" if agent.backend == "codex" else "CLAUDE_CONFIG_DIR", ""
            ),
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def plan(self, agent: AgentConfig, through: int, message_ids: set[int]) -> dict:
        """Only acknowledged, compatible sessions may receive an incremental prompt."""
        previous = self.store.sessions().get(agent.name, {})
        fingerprint = self.fingerprint(agent)
        reason = None
        if not previous.get("session_id"):
            reason = previous.get("reason") or "No saved session"
        elif previous.get("fingerprint") != fingerprint:
            reason = "Agent configuration or context protocol changed"
        elif previous.get("dirty"):
            reason = "Previous invocation did not commit"
        elif (
            type(previous.get("synced_through")) is not int
            or previous["synced_through"] not in message_ids
            or previous["synced_through"] > through
        ):
            reason = "Saved public-message cursor cannot be verified"
        else:
            try:
                session_id(previous["session_id"])
            except ValueError:
                reason = "Invalid saved backend session ID"
        if reason:
            return {
                "session_id": None,
                "synced_through": 0,
                "generation": previous.get("generation", 0) + 1,
                "fingerprint": fingerprint,
                "reason": reason,
            }
        return {
            **previous,
            "session_id": session_id(previous["session_id"]),
            "reason": None,
        }

    def begin(self, speaker: str, plan: dict, turn_id: str, through: int) -> None:
        # Persist the uncertain state BEFORE the CLI can append to its own transcript.
        self.store.set_session(
            speaker,
            {
                **plan,
                "dirty": True,
                "turn_id": turn_id,
                "input_through": through,
            },
        )

    def completed(
        self, speaker: str, turn_id: str, result_id: str, *, concurrent: bool = False
    ) -> tuple[str, dict]:
        result_id = session_id(result_id)
        states = self.store.sessions()
        state = states[speaker]
        if not state.get("dirty") or state.get("turn_id") != turn_id:
            raise ValueError("Private-session completion belongs to a revoked invocation")
        if state.get("session_id") and state["session_id"] != result_id:
            raise ValueError("Backend resumed a different private session")
        if any(s != speaker and v.get("session_id") == result_id for s, v in states.items()):
            raise ValueError("Two agents cannot share the same private session")
        return speaker, {
            **state,
            "session_id": result_id,
            "dirty": False,
            "turn_id": None,
            "synced_through": state["input_through"],
            "cursor_mode": "input" if concurrent else "reply",
            "reason": None,
        }

    def invalidate(self, speaker: str, reason: str) -> None:
        state = self.store.sessions().get(speaker)
        if state:
            # Leave provider transcript files intact, but never resume uncertain history.
            self.store.set_session(
                speaker,
                {
                    **state,
                    "session_id": None,
                    "synced_through": 0,
                    "dirty": False,
                    "turn_id": None,
                    "reason": reason,
                },
            )
