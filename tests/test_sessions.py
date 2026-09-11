from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agent_team.adapters import (
    AdapterError,
    CLIAdapter,
    EventDecoder,
    SessionUnavailable,
    command_for,
)
from agent_team.client import describe_sessions, parse_input
from agent_team.config import AgentConfig, TeamConfig
from agent_team.context import DELTA_MARKER, SYNC_MARKER, TRANSCRIPT_MARKER
from agent_team.engine import Room
from agent_team.sessions import CONTEXT_PROTOCOL, Sessions
from agent_team.store import Store
from agent_team.workflow import action_reply


def public_messages(prompt):
    marker = DELTA_MARKER if DELTA_MARKER in prompt else TRANSCRIPT_MARKER
    return json.loads(prompt.split(marker, 1)[1])


class SessionBackend:
    supports_sessions = True
    supports_session_notifications = True

    def __init__(self, history=None):
        self.history = {} if history is None else history
        self.calls = []
        self.replies = []
        self.started = asyncio.Event()
        self.block = False
        self.ignore_cancel = False
        self.partial_failure = False
        self.override_id = None
        self.result_session_id = None

    async def stream(
        self, prompt, *, phase="discussion", persist_session=False, session_id=None, on_session=None
    ):
        self.result_session_id = None
        self.calls.append({"prompt": prompt, "session_id": session_id, "phase": phase})
        self.started.set()
        if session_id and session_id not in self.history:
            raise SessionUnavailable("No conversation found with this session ID")
        identifier = session_id or str(uuid.uuid4())
        self.history.setdefault(identifier, []).append(prompt)
        if persist_session and on_session:
            on_session(identifier)
        if self.block:
            yield "uncommitted work"
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if not self.ignore_cancel:
                    raise
        if self.partial_failure:
            yield "partial"
            raise SessionUnavailable("No session found")
        reply = self.replies.pop(0) if self.replies else f"contribution {len(self.calls)}"
        if isinstance(reply, Exception):
            raise reply
        yield reply
        self.result_session_id = self.override_id or identifier


class SessionEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.store = Store(self.workspace / "events.sqlite3")
        self.config = TeamConfig(
            workflow="discussion",
            workspace=self.workspace,
            turn_delay=0,
            agents=(AgentConfig("a", "codex"), AgentConfig("b", "claude")),
        )
        self.backends = {n: SessionBackend() for n in ("a", "b")}
        self.events = []
        self.room = Room(self.config, self.store, self.events.append, self.backends)
        self.room.start()
        self.room.control("pause")
        self.room.say("human", "initial idea")

    async def asyncTearDown(self):
        await self.room.close()
        self.store.close()
        self.temp.cleanup()

    async def idle(self):
        async with asyncio.timeout(3):
            while self.room.active is not None or not self.room.manual_paused:
                await asyncio.sleep(0.001)

    async def step(self, name):
        self.room.control("next", name)
        await self.idle()
        self.assertEqual(self.room.reason, "step_complete", self.room.messages[-1])

    async def restart(self, config=None):
        await self.room.close()
        self.room = Room(config or self.config, self.store, self.events.append, self.backends)
        self.room.start()
        self.assertTrue(self.room.manual_paused)

    async def test_agents_use_distinct_ids_and_only_missing_public_messages(self):
        self.backends["a"].replies = ["A's first reply", "A responds to B"]
        self.backends["b"].replies = ["B challenges A"]
        await self.step("a")
        a_message = self.room.messages[-1]
        a_id = self.store.sessions()["a"]["session_id"]
        self.assertEqual(self.store.sessions()["a"]["synced_through"], a_message["id"])
        await self.step("b")
        b_message = self.room.messages[-1]
        b_id = self.store.sessions()["b"]["session_id"]
        self.assertNotEqual(a_id, b_id)
        await self.step("a")
        call = self.backends["a"].calls[-1]
        self.assertEqual(call["session_id"], a_id)
        self.assertEqual([m["id"] for m in public_messages(call["prompt"])], [b_message["id"]])
        sync = json.loads(call["prompt"].split(SYNC_MARKER, 1)[1].split("\n", 1)[0])
        self.assertEqual(sync["after"], a_message["id"])
        self.assertEqual(sync["through"], b_message["id"])
        self.assertEqual(sync["message_count"], 1)
        self.assertIn("initial idea", call["prompt"])  # Authoritative human reminder.
        self.assertIn("re-read relevant files", call["prompt"])
        self.assertFalse(self.store.sessions()["a"]["dirty"])

    async def test_clean_restart_resumes_exact_session_without_replaying_history(self):
        await self.step("a")
        saved = self.store.sessions()["a"]
        await self.restart()
        await self.step("a")
        self.assertEqual(self.backends["a"].calls[-1]["session_id"], saved["session_id"])
        self.assertEqual(public_messages(self.backends["a"].calls[-1]["prompt"]), [])

    async def test_in_flight_state_is_durable_before_adapter_starts(self):
        backend = self.backends["a"]
        backend.block = True
        self.room.control("next", "a")
        await backend.started.wait()
        pending = self.store.sessions()["a"]
        self.assertTrue(pending["dirty"])
        self.assertEqual(pending["turn_id"], self.room.active["turn_id"])
        self.room.control("interrupt")
        await self.idle()
        self.assertEqual(self.store.sessions()["a"]["session_id"], pending["session_id"])
        self.assertIsNotNone(pending["session_id"])
        self.assertEqual(self.store.sessions()["a"]["synced_through"], 0)
        self.assertTrue(self.store.sessions()["a"]["dirty"])

    async def test_idle_notice_does_not_invalidate_or_advance_private_session(self):
        await self.step("a")
        saved = self.store.sessions()["a"]
        await self.restart(replace(self.config, idle_warning_seconds=0.02))
        backend = self.backends["a"]
        backend.started.clear()
        backend.block = True
        self.room.control("next", "a")
        await backend.started.wait()
        pending = self.store.sessions()["a"]
        async with asyncio.timeout(3):
            while not any(e["type"] == "turn.idle" for e in self.events):
                await asyncio.sleep(0.005)
        self.assertEqual(self.store.sessions()["a"], pending)
        self.assertEqual(pending["session_id"], saved["session_id"])
        self.assertEqual(pending["synced_through"], saved["synced_through"])
        self.assertTrue(pending["dirty"])
        self.assertFalse(self.room.manual_paused)
        self.assertEqual(len(self.room.messages), 2)
        self.room.control("interrupt")
        await self.idle()
        self.assertEqual(self.store.sessions()["a"]["session_id"], saved["session_id"])

    async def test_interrupted_session_resumes_but_stale_output_never_commits(self):
        await self.step("a")
        old_id = self.store.sessions()["a"]["session_id"]
        backend = self.backends["a"]
        backend.started.clear()
        backend.block = backend.ignore_cancel = True
        self.room.control("next", "a")
        await backend.started.wait()
        self.room.control("interrupt")
        self.room.say("human", "new direction")
        backend.block = False
        await self.idle()
        self.assertFalse(any(m["text"] == "uncommitted work" for m in self.room.messages))
        await self.step("a")
        call = backend.calls[-1]
        self.assertEqual(call["session_id"], old_id)
        self.assertIn(DELTA_MARKER, call["prompt"])
        self.assertIn("Session recovery:", call["prompt"])
        self.assertIn("new direction", call["prompt"])
        self.assertEqual(self.store.sessions()["a"]["session_id"], old_id)
        self.assertEqual([m["text"] for m in public_messages(call["prompt"])], ["new direction"])
        self.assertFalse(self.store.sessions()["a"]["dirty"])
        self.assertIn(old_id, backend.history)  # Provider transcripts are not deleted.

    async def test_crash_marker_resumes_with_reconciliation_on_restart(self):
        await self.step("a")
        state = self.store.sessions()["a"]
        self.store.set_session("a", {**state, "dirty": True, "turn_id": "crashed"})
        await self.restart()
        await self.step("a")
        self.assertEqual(self.backends["a"].calls[-1]["session_id"], state["session_id"])
        self.assertIn("Session recovery:", self.backends["a"].calls[-1]["prompt"])
        self.assertEqual(self.store.sessions()["a"]["generation"], 1)

    async def test_missing_session_retries_once_with_full_public_history(self):
        await self.step("a")
        backend = self.backends["a"]
        backend.history.clear()
        await self.step("a")
        self.assertEqual(len(backend.calls), 3)
        self.assertIsNotNone(backend.calls[1]["session_id"])
        self.assertIsNone(backend.calls[2]["session_id"])
        self.assertEqual(len(public_messages(backend.calls[2]["prompt"])), 2)
        self.assertEqual(len([e for e in self.events if e["type"] == "session.rebuilt"]), 1)
        self.assertEqual(len([m for m in self.room.messages if m["role"] == "member"]), 2)

    async def test_unknown_failure_pauses_and_next_explicit_turn_resumes(self):
        await self.step("a")
        saved = self.store.sessions()["a"].copy()
        backend = self.backends["a"]
        backend.replies = [AdapterError("network failure")]
        self.room.control("next", "a")
        await self.idle()
        self.assertEqual(self.room.reason, "error")
        self.assertEqual(len(backend.calls), 2)
        self.assertEqual(self.store.sessions()["a"]["session_id"], saved["session_id"])
        self.assertEqual(self.store.sessions()["a"]["synced_through"], saved["synced_through"])
        await self.step("a")
        self.assertEqual(backend.calls[-1]["session_id"], saved["session_id"])
        self.assertIn("Session recovery:", backend.calls[-1]["prompt"])

    async def test_missing_session_after_partial_output_is_not_automatically_retried(self):
        await self.step("a")
        self.backends["a"].partial_failure = True
        self.room.control("next", "a")
        await self.idle()
        self.assertEqual(self.room.reason, "error")
        self.assertEqual(len(self.backends["a"].calls), 2)
        self.assertEqual(len(self.room.messages), 2)

    async def test_invalid_workflow_action_does_not_acknowledge_session(self):
        await self.restart(replace(self.config, workflow="build"))
        self.backends["a"].replies = [
            action_reply("Wrong version", {"action": "approve", "version": 999})
        ]
        self.room.control("next", "a")
        await self.idle()
        # A first malformed action is the author's to correct, not the run's to die on.
        self.assertNotEqual(self.room.reason, "error")
        self.assertEqual(self.room.protocol_lapses.get("a"), 1)
        self.assertTrue(
            any("was not accepted" in m["text"] for m in self.room.messages),
            "the member must be told what to send instead",
        )
        # What matters regardless of policy: nothing was recorded or acknowledged.
        self.assertIsNotNone(self.store.sessions()["a"]["session_id"])
        self.assertEqual(self.store.sessions()["a"]["synced_through"], 0)
        self.assertTrue(self.store.sessions()["a"]["dirty"])
        # a's rejected turn commits nothing; the room stays alive and peers carry on.
        self.assertFalse(
            any(m["role"] == "member" and m["speaker"] == "a" for m in self.room.messages)
        )

    async def test_peer_session_id_collision_is_rejected(self):
        await self.step("a")
        self.backends["b"].override_id = self.store.sessions()["a"]["session_id"]
        self.room.control("next", "b")
        await self.idle()
        self.assertEqual(self.room.reason, "error")
        self.assertNotEqual(
            self.store.sessions()["b"]["session_id"], self.store.sessions()["a"]["session_id"]
        )
        self.assertTrue(self.store.sessions()["b"]["dirty"])
        self.assertEqual(len(self.room.messages), 2)

    async def test_human_reset_preserves_public_history_and_other_agent_session(self):
        await self.step("a")
        await self.step("b")
        b_id = self.store.sessions()["b"]["session_id"]
        before = list(self.room.messages)
        self.room.control("reset-session", "a")
        self.assertEqual(self.room.messages, before)
        self.assertEqual(self.store.sessions()["b"]["session_id"], b_id)
        await self.step("a")
        self.assertIsNone(self.backends["a"].calls[-1]["session_id"])
        self.assertEqual(len(public_messages(self.backends["a"].calls[-1]["prompt"])), 3)

    async def test_reset_rejects_invalid_target_and_active_turn_without_mutation(self):
        before = self.room.status()
        with self.assertRaises(ValueError):
            self.room.control("reset-session", "unknown")
        self.assertEqual(before, self.room.status())
        backend = self.backends["a"]
        backend.block = True
        self.room.control("next", "a")
        await backend.started.wait()
        before = self.store.sessions()
        with self.assertRaisesRegex(ValueError, "/interrupt"):
            self.room.control("reset-session", "a")
        self.assertEqual(before, self.store.sessions())
        self.room.control("interrupt")
        await self.idle()

    async def test_model_change_rebuilds_private_context(self):
        await self.step("a")
        changed = replace(
            self.config,
            agents=(replace(self.config.agents[0], model="new-model"), self.config.agents[1]),
        )
        await self.restart(changed)
        await self.step("a")
        self.assertIsNone(self.backends["a"].calls[-1]["session_id"])

    async def test_permission_mode_changes_rebuild_sessions_in_both_directions(self):
        await self.step("a")
        for mode in ("full_auto", "phase_scoped"):
            old_id = self.store.sessions()["a"]["session_id"]
            before = list(self.room.messages)
            await self.restart(replace(self.config, permission_mode=mode))
            await self.step("a")
            self.assertIsNone(self.backends["a"].calls[-1]["session_id"])
            self.assertNotEqual(self.store.sessions()["a"]["session_id"], old_id)
            self.assertEqual(self.room.messages[:-1], before)

    async def test_removing_old_role_rebuilds_private_context_and_keeps_public_history(self):
        focused = replace(
            self.config,
            agents=(
                replace(self.config.agents[0], role="Architecture focus"),
                self.config.agents[1],
            ),
        )
        await self.restart(focused)
        await self.step("a")
        old_id = self.store.sessions()["a"]["session_id"]
        public_before = list(self.room.messages)
        await self.restart(self.config)
        await self.step("a")
        latest = self.backends["a"].calls[-1]
        self.assertIsNone(latest["session_id"])
        self.assertNotEqual(self.store.sessions()["a"]["session_id"], old_id)
        self.assertNotIn("Optional additional focus:", latest["prompt"])
        self.assertEqual(self.room.messages[:-1], public_before)
        self.assertEqual(
            [m["id"] for m in public_messages(latest["prompt"])], [m["id"] for m in public_before]
        )

    async def test_old_prompt_protocol_rebuilds_even_when_agent_configuration_is_unchanged(self):
        with patch("agent_team.sessions.CONTEXT_PROTOCOL", CONTEXT_PROTOCOL - 1):
            await self.step("a")
        public_before = list(self.room.messages)
        await self.restart()
        await self.step("a")
        latest = self.backends["a"].calls[-1]
        self.assertIsNone(latest["session_id"])
        self.assertIn("All members share equal responsibility", latest["prompt"])
        self.assertEqual(self.room.messages[:-1], public_before)
        self.assertEqual(len(public_messages(latest["prompt"])), len(public_before))

    async def test_full_mode_never_resumes_a_previously_saved_private_session(self):
        await self.step("a")
        calls = []

        class FullBackend:
            supports_sessions = True

            async def stream(self, prompt, *, phase="discussion", **options):
                calls.append((prompt, options))
                yield "full-context reply"

        self.backends["a"] = FullBackend()
        await self.restart(replace(self.config, context_mode="full"))
        await self.step("a")
        await self.step("a")
        self.assertTrue(all(not options for _, options in calls))
        self.assertTrue(all(TRANSCRIPT_MARKER in prompt for prompt, _ in calls))
        self.assertEqual(len(public_messages(calls[-1][0])), 3)
        self.assertIsNone(self.store.sessions()["a"]["session_id"])

    async def test_timeout_keeps_session_without_acknowledging_partial_reply(self):
        await self.step("a")
        saved = self.store.sessions()["a"].copy()
        self.backends["a"].block = True
        await self.restart(replace(self.config, turn_timeout=0.02))
        self.room.control("next", "a")
        await self.idle()
        self.assertEqual(self.room.reason, "error")
        self.assertEqual(self.store.sessions()["a"]["session_id"], saved["session_id"])
        self.assertEqual(self.store.sessions()["a"]["synced_through"], saved["synced_through"])
        self.assertEqual(len(self.room.messages), 2)

    async def test_saved_cursor_outside_public_history_is_not_trusted(self):
        await self.step("a")
        state = self.store.sessions()["a"]
        self.store.set_session("a", {**state, "synced_through": 999999})
        await self.step("a")
        self.assertIsNone(self.backends["a"].calls[-1]["session_id"])

    async def test_pass_acknowledges_input_without_fabricating_public_message(self):
        self.backends["a"].replies = ["[[PASS]]"]
        await self.step("a")
        self.assertEqual(len(self.room.messages), 1)
        self.assertEqual(self.store.sessions()["a"]["synced_through"], self.room.messages[0]["id"])
        self.assertFalse(self.store.sessions()["a"]["dirty"])

    async def test_session_transaction_failure_cannot_advance_cursor_or_commit_message(self):
        await self.step("a")
        before = self.store.sessions()["a"]
        events_before = self.store.events()
        self.store.db.execute(
            "CREATE TEMP TRIGGER fail_session_update BEFORE UPDATE ON agent_sessions "
            "BEGIN SELECT RAISE(ABORT, 'test crash'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.append(
                "message",
                role="member",
                speaker="a",
                text="not committed",
                session_update=("a", {**before, "session_id": str(uuid.uuid4())}),
            )
        self.assertEqual(self.store.events(), events_before)
        self.assertEqual(self.store.sessions()["a"], before)
        self.store.db.execute("DROP TRIGGER fail_session_update")


class SessionProtocolTests(unittest.TestCase):
    def test_exact_cli_ids_and_permissions_are_set_on_every_resume(self):
        identifier = str(uuid.uuid4())
        for phase in ("planning", "implementation", "judging", "review", "discussion"):
            with self.subTest(phase=phase):
                codex = command_for(
                    AgentConfig("a", "codex"),
                    phase,
                    persist_session=True,
                    session_id=identifier,
                )
                self.assertEqual(codex[-3:], ["resume", identifier, "-"])
                self.assertNotIn("--last", codex)
                self.assertNotIn("--ephemeral", codex)
                self.assertEqual(
                    codex[codex.index("--sandbox") + 1],
                    "workspace-write" if phase == "implementation" else "read-only",
                )
                self.assertLess(codex.index("--sandbox"), codex.index("resume"))
                claude = command_for(
                    AgentConfig("b", "claude"),
                    phase,
                    persist_session=True,
                    session_id=identifier,
                )
                self.assertEqual(claude[claude.index("--resume") + 1], identifier)
                self.assertNotIn("--continue", claude)
                self.assertNotIn("--no-session-persistence", claude)
                self.assertEqual(
                    claude[claude.index("--permission-mode") + 1],
                    "acceptEdits" if phase == "implementation" else "dontAsk",
                )
                if phase != "implementation":
                    self.assertNotIn("Write", claude[claude.index("--tools") + 1])

    def test_new_claude_session_is_preallocated_and_full_mode_stays_ephemeral(self):
        identifier = str(uuid.uuid4())
        command = command_for(
            AgentConfig("a", "claude"),
            persist_session=True,
            new_session_id=identifier,
        )
        self.assertEqual(command[command.index("--session-id") + 1], identifier)
        self.assertIn("--ephemeral", command_for(AgentConfig("a", "codex")))
        self.assertIn("--no-session-persistence", command_for(AgentConfig("a", "claude")))

    def test_decoder_captures_ids_and_ignores_nested_claude_sessions(self):
        identifier = str(uuid.uuid4())
        decoder = EventDecoder("codex", identifier)
        decoder.feed({"type": "thread.started", "thread_id": identifier})
        self.assertEqual(decoder.session_id, identifier)
        decoder = EventDecoder("claude", identifier)
        decoder.feed({"type": "system", "subtype": "init", "session_id": identifier})
        decoder.feed(
            {
                "type": "assistant",
                "parent_tool_use_id": "child",
                "session_id": str(uuid.uuid4()),
                "message": {"content": []},
            }
        )
        decoder.feed({"type": "result", "result": "ok", "session_id": identifier})
        decoder.finish()
        self.assertEqual(decoder.session_id, identifier)

    def test_wrong_or_missing_format_session_ids_are_rejected(self):
        expected = str(uuid.uuid4())
        for actual in ("--last", str(uuid.uuid4())):
            with self.subTest(actual=actual), self.assertRaises(AdapterError):
                EventDecoder("codex", expected).feed(
                    {
                        "type": "thread.started",
                        "thread_id": actual,
                    }
                )
        with self.assertRaises(ValueError):
            command_for(AgentConfig("a", "codex"), persist_session=True, session_id="--last")

    def test_cli_commands_and_display(self):
        self.assertEqual(parse_input("/sessions"), {"type": "sessions"})
        self.assertEqual(
            parse_input("/reset-session a"),
            {
                "type": "control",
                "action": "reset-session",
                "target": "a",
            },
        )
        display = describe_sessions(
            {
                "context_mode": "incremental",
                "agents": [{"name": "a", "backend": "codex"}],
                "sessions": {"a": {"session_id": "example", "synced_through": 12, "dirty": True}},
            }
        )
        self.assertIn("through #12", display)
        self.assertIn("uncertain", display)
        with self.assertRaises(ValueError):
            TeamConfig(
                workflow="discussion",
                agents=(AgentConfig("a", "mock"),),
                context_mode="bad",
            )

    def test_existing_public_log_migrates_without_private_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.sqlite3"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute(
                "INSERT INTO events VALUES (1, ?)",
                (
                    json.dumps(
                        {
                            "type": "message",
                            "speaker": "human",
                            "role": "user",
                            "text": "preserved",
                        }
                    ),
                ),
            )
            db.commit()
            db.close()
            store = Store(path)
            self.addCleanup(store.close)
            self.assertEqual(store.sessions(), {})
            self.assertEqual(store.messages()[0]["text"], "preserved")
            config = TeamConfig(
                workflow="discussion",
                workspace=Path(directory),
                agents=(AgentConfig("a", "codex"),),
            )
            planned = Sessions(config, store).plan(config.agents[0], 1, {1})
            self.assertIsNone(planned["session_id"])


class SessionProcessTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, backend, events, *, identifier=None, stderr="", code=0, on_session=None):
        script = (
            "import json,sys; sys.stdin.read(); "
            f"[print(json.dumps(e), flush=True) for e in {events!r}]; "
            f"sys.stderr.write({stderr!r}); sys.exit({code})"
        )
        adapter = CLIAdapter(AgentConfig("native", backend), Path.cwd())
        with patch("agent_team.adapters.command_for", return_value=[sys.executable, "-c", script]):
            async with asyncio.timeout(3):
                text = "".join(
                    [
                        chunk
                        async for chunk in adapter.stream(
                            "prompt",
                            persist_session=True,
                            session_id=identifier,
                            on_session=on_session,
                        )
                    ]
                )
        return text, adapter.result_session_id

    async def test_real_process_success_exposes_session_id_only_after_completion(self):
        identifier = str(uuid.uuid4())
        result = await self.invoke(
            "codex",
            [
                {"type": "thread.started", "thread_id": identifier},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
                {"type": "turn.completed"},
            ],
            identifier=identifier,
        )
        self.assertEqual(result, ("done", identifier))
        result = await self.invoke(
            "claude",
            [
                {"type": "system", "subtype": "init", "session_id": identifier},
                {"type": "result", "result": "done", "session_id": identifier},
            ],
            identifier=identifier,
        )
        self.assertEqual(result, ("done", identifier))

    async def test_missing_session_before_activity_is_safe_to_rebuild(self):
        for backend in ("codex", "claude"):
            with self.subTest(backend=backend), self.assertRaises(SessionUnavailable):
                await self.invoke(
                    backend,
                    [],
                    identifier=str(uuid.uuid4()),
                    stderr="No conversation found with session ID: missing",
                    code=1,
                )

    async def test_native_serial_identity_is_reported_even_when_the_turn_fails(self):
        identifier = str(uuid.uuid4())
        for backend, event in (
            ("codex", {"type": "thread.started", "thread_id": identifier}),
            ("claude", {"type": "system", "subtype": "init", "session_id": identifier}),
        ):
            identities = []
            with self.subTest(backend=backend), self.assertRaises(AdapterError):
                await self.invoke(
                    backend,
                    [event],
                    identifier=identifier,
                    code=1,
                    stderr="Usage exhausted",
                    on_session=identities.append,
                )
            self.assertEqual(identities, [identifier])

    async def test_session_error_after_activity_is_not_classified_as_safe_retry(self):
        identifier = str(uuid.uuid4())
        with self.assertRaises(AdapterError) as raised:
            await self.invoke(
                "codex",
                [
                    {"type": "thread.started", "thread_id": identifier},
                    {"type": "item.started", "item": {"type": "command_execution"}},
                ],
                identifier=identifier,
                stderr="No session found",
                code=1,
            )
        self.assertNotIsInstance(raised.exception, SessionUnavailable)

    async def test_silent_resume_to_new_id_and_missing_id_fail_closed(self):
        expected = str(uuid.uuid4())
        cases = [
            [
                {"type": "thread.started", "thread_id": str(uuid.uuid4())},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "wrong"}},
                {"type": "turn.completed"},
            ],
            [
                {"type": "item.completed", "item": {"type": "agent_message", "text": "missing"}},
                {"type": "turn.completed"},
            ],
        ]
        for events in cases:
            with self.subTest(events=events), self.assertRaises(AdapterError):
                await self.invoke("codex", events, identifier=expected)
