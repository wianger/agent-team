from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_chatroom import Scripted, transcript
from test_collaboration import shared_plan

from agent_team.adapters import AdapterError, EventDecoder, QuotaExceeded, provider_error
from agent_team.chatroom import ChatRoom
from agent_team.config import AgentConfig, TeamConfig
from agent_team.engine import CLAUDE_QUOTA_RETRY_SECONDS, Room
from agent_team.store import Store
from agent_team.workflow import action_reply

REAL_TIME = time.time


class QuotaDecoderTests(unittest.TestCase):
    def test_only_explicit_provider_quota_errors_are_classified(self):
        for backend in ("claude", "codex"):
            for detail in (
                {"codexErrorInfo": "usageLimitExceeded", "message": "Try later"},
                {"code": "usage_limit_reached"},
                "You've hit your limit · resets 6pm",
                "You've reached your weekly limit",
                "Usage quota exhausted",
            ):
                with self.subTest(backend=backend, detail=detail):
                    self.assertIsInstance(provider_error(backend, detail), QuotaExceeded)
            for detail in (
                "Operation not permitted",
                "Invalid control character",
                "401 Unauthorized",
                {"type": "rate_limit_error", "message": "429 Too many requests"},
                {"codexErrorInfo": "ContextWindowExceeded"},
                "Connection closed",
            ):
                self.assertNotIsInstance(provider_error(backend, detail), QuotaExceeded)
        self.assertNotIsInstance(provider_error("command", "Quota exhausted"), QuotaExceeded)

    def test_codex_failed_turn_keeps_structured_quota_information(self):
        with self.assertRaises(QuotaExceeded):
            EventDecoder("codex").feed(
                {
                    "type": "turn.failed",
                    "error": {"codexErrorInfo": "UsageLimitExceeded"},
                }
            )

    def test_claude_native_error_uses_rejected_quota_metadata(self):
        decoder = EventDecoder("claude")
        decoder.feed(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour"},
            }
        )
        with self.assertRaises(QuotaExceeded):
            decoder.feed(
                {
                    "type": "assistant",
                    "error": "rate_limit",
                    "message": {"content": [{"type": "text", "text": "Try again later"}]},
                }
            )

    def test_quota_warnings_and_ordinary_agent_text_are_not_failures(self):
        for status in ("allowed", "allowed_warning", "rejected"):
            decoder = EventDecoder("claude")
            decoder.feed({"type": "rate_limit_event", "rate_limit_info": {"status": status}})
            decoder.feed(
                {
                    "type": "assistant",
                    "parent_tool_use_id": "child",
                    "error": "rate_limit",
                    "message": {"content": [{"type": "text", "text": "You've hit your limit"}]},
                }
            )
            text = "The test fixture says: You've hit your limit"
            decoder.feed({"type": "result", "result": text})
            decoder.finish()
            self.assertEqual(decoder.text, text)

    def test_native_capacity_errors_are_failures_but_not_five_hour_cooldowns(self):
        with self.assertRaises(AdapterError) as raised:
            EventDecoder("claude").feed(
                {
                    "type": "assistant",
                    "error": "rate_limit",
                    "message": {"content": [{"type": "text", "text": "Temporary capacity issue"}]},
                }
            )
        self.assertNotIsInstance(raised.exception, QuotaExceeded)
        with self.assertRaises(QuotaExceeded):
            EventDecoder("claude").feed(
                {
                    "type": "result",
                    "is_error": True,
                    "result": "You've hit your limit",
                }
            )


class QuotaRoomTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.rooms, self.stores = [], []
        self.now = 2_000_000_000.0
        self.clock = patch("agent_team.engine.time.time", side_effect=lambda: self.now)
        self.clock.start()

    async def asyncTearDown(self):
        for room in self.rooms:
            if not room.closed:
                await room.close()
        for store in self.stores:
            store.close()
        self.clock.stop()
        self.temp.cleanup()

    def start(self, claude, codex, mode="chatroom", *, build=False, store=None, start=True):
        if store is None:
            store = Store(Path(self.temp.name) / f"events-{len(self.stores)}.sqlite3")
            self.stores.append(store)
        config = TeamConfig(
            workspace=Path(self.temp.name),
            workflow="build" if build else "discussion",
            # Arbitrary names prove policy follows the backend, not the member's name.
            agents=(AgentConfig("short", "claude"), AgentConfig("long", "codex")),
            interaction_mode=mode,
            turn_delay=0,
        )
        cls = ChatRoom if mode == "chatroom" else Room
        room = cls(config, store, lambda event: None, {"short": claude, "long": codex})
        self.rooms.append(room)
        if start:
            room.start()
            if not room.messages:
                room.say("human", "Initial idea")
        return room

    async def until(self, predicate):
        async with asyncio.timeout(4):
            while not predicate():
                for room in self.rooms:
                    if room.runner and room.runner.done():
                        room.runner.result()
                await asyncio.sleep(0.005)

    async def test_claude_cools_down_and_codex_continues_in_both_modes(self):
        for mode in ("chatroom", "serial"):
            claude, codex = Scripted(QuotaExceeded("Quota exhausted")), Scripted("Keep researching")
            room = self.start(claude, codex, mode)
            await self.until(lambda room=room: "short" in room.quotas and not room.active)
            self.assertEqual(CLAUDE_QUOTA_RETRY_SECONDS, 18000)
            self.assertEqual(room.quotas["short"]["retry_at"], self.now + 18000)
            self.assertFalse(room.manual_paused)
            self.assertEqual(len(claude.calls), 1)
            self.assertTrue(any(m["text"] == "Keep researching" for m in room.messages))
            self.assertIsNotNone(room.quota_timer)
            calls = len(codex.calls)
            await asyncio.sleep(0.03)
            self.assertEqual(len(codex.calls), calls)
            await room.close()
            self.assertIsNone(room.quota_timer)

    async def test_claude_failure_does_not_cancel_an_active_codex_reply(self):
        gate = asyncio.Event()
        claude = Scripted(QuotaExceeded("Quota exhausted"))
        codex = Scripted((gate, "Still working"))
        room = self.start(claude, codex)
        await self.until(lambda: "short" in room.quotas and codex.calls)
        self.assertEqual(codex.cancelled, 0)
        self.assertEqual(room.revision, 0)
        gate.set()
        await self.until(lambda: not room.active)
        self.assertIn("Still working", [m["text"] for m in room.messages])

    async def test_retry_after_five_hours_rebuilds_context_then_rejoins(self):
        for mode in ("chatroom", "serial"):
            claude = Scripted(QuotaExceeded("Quota exhausted"), "Recovered")
            codex = Scripted("Progress while offline")
            room = self.start(claude, codex, mode)
            await self.until(lambda room=room: "short" in room.quotas and not room.active)
            self.now = room.quotas["short"]["retry_at"] - 1
            room.wake.set()
            await asyncio.sleep(0.02)
            self.assertEqual(len(claude.calls), 1)
            self.now += 1
            room.wake.set()
            await self.until(lambda room=room: not room.quotas and not room.active)
            self.assertIsNone(claude.calls[1]["session_id"])
            self.assertIn(
                "Progress while offline", [m["text"] for m in transcript(claude.calls[1]["prompt"])]
            )
            await room.close()

    async def test_repeated_limit_schedules_another_five_hours(self):
        claude = Scripted(QuotaExceeded("First limit"), QuotaExceeded("Still limited"))
        room = self.start(claude, Scripted())
        await self.until(lambda room=room: "short" in room.quotas and not room.active)
        self.now = room.quotas["short"]["retry_at"]
        room.wake.set()
        await self.until(lambda: len(claude.calls) == 2 and not room.active)
        self.assertEqual(room.quotas["short"]["retry_at"], self.now + 18000)
        self.assertFalse(room.manual_paused)
        self.assertFalse(room.quota_retries)

    async def test_real_timer_retries_without_messages_or_connected_humans(self):
        with (
            patch("agent_team.engine.time.time", side_effect=REAL_TIME),
            patch("agent_team.engine.CLAUDE_QUOTA_RETRY_SECONDS", 0.05),
        ):
            claude = Scripted(QuotaExceeded("Quota exhausted"), "Recovered automatically")
            room = self.start(claude, Scripted())
            await self.until(lambda: len(claude.calls) >= 2 and not room.quotas and not room.active)
            self.assertIn("Recovered automatically", [m["text"] for m in room.messages])
            await room.close()

    async def test_manual_pause_defers_due_retry_until_resume(self):
        for mode in ("chatroom", "serial"):
            claude = Scripted(QuotaExceeded("Quota exhausted"), "Recovered")
            room = self.start(claude, Scripted(), mode)
            await self.until(lambda room=room: "short" in room.quotas and not room.active)
            room.control("pause")
            self.now = room.quotas["short"]["retry_at"] + 1
            room.wake.set()
            await asyncio.sleep(0.02)
            self.assertTrue(room.manual_paused)
            self.assertIsNone(room.quota_timer)
            self.assertEqual(len(claude.calls), 1)
            room.control("resume")
            await self.until(lambda room=room: not room.quotas and not room.active)
            self.assertGreaterEqual(len(claude.calls), 2)
            await room.close()

    async def test_codex_limit_pauses_and_revokes_peers_until_explicit_resume(self):
        fail, peer = asyncio.Event(), asyncio.Event()
        claude = Scripted((peer, "Stale reply"), "Fresh reply", resist_cancel=True)
        codex = Scripted((fail, QuotaExceeded("Usage limit reached")), "Restored")
        room = self.start(claude, codex)
        await self.until(lambda: claude.calls and codex.calls)
        fail.set()
        await self.until(lambda: room.manual_paused and not room.active)
        self.assertEqual(claude.cancelled, 1)
        self.assertNotIn("Stale reply", [m["text"] for m in room.messages])
        self.assertIsNone(room.quotas["long"]["retry_at"])
        self.now += 7 * 24 * 3600
        room.say("human", "Context is not permission to resume")
        room.control("retry", "short")
        await asyncio.sleep(0.02)
        self.assertTrue(room.manual_paused)
        self.assertIsNone(room.quota_timer)
        self.assertEqual(len(codex.calls), 1)
        room.control("resume")
        await self.until(lambda: len(codex.calls) >= 2 and not room.active)
        self.assertIn("Restored", [m["text"] for m in room.messages])

    async def test_explicit_retry_can_restore_claude_early_without_cancelling_codex(self):
        gate = asyncio.Event()
        claude = Scripted(QuotaExceeded("Quota exhausted"), "Recovered early")
        codex = Scripted((gate, "Continued work"))
        room = self.start(claude, codex)
        await self.until(lambda: "short" in room.quotas and codex.calls)
        room.control("retry", "short")
        await self.until(lambda: len(claude.calls) >= 2)
        self.assertFalse(room.quotas)
        self.assertEqual(codex.cancelled, 0)
        gate.set()
        await self.until(lambda: not room.active)

    async def test_codex_pause_prevents_an_overdue_claude_retry(self):
        for mode in ("chatroom", "serial"):
            claude = Scripted(QuotaExceeded("Claude limit"), "Claude restored")
            codex = Scripted(QuotaExceeded("Codex limit"), "Codex restored")
            room = self.start(claude, codex, mode)
            await self.until(lambda room=room: len(room.quotas) == 2 and not room.active)
            self.now += 18001
            room.wake.set()
            await asyncio.sleep(0.02)
            self.assertTrue(room.manual_paused)
            self.assertEqual(len(claude.calls), 1)
            self.assertEqual(len(codex.calls), 1)
            room.control("resume")
            await self.until(lambda room=room: not room.quotas and not room.active)
            await room.close()

    async def test_nonquota_failure_on_retry_still_pauses_everyone(self):
        claude = Scripted(QuotaExceeded("Quota exhausted"), AdapterError("Operation not permitted"))
        room = self.start(claude, Scripted())
        await self.until(lambda room=room: "short" in room.quotas and not room.active)
        self.now += 18000
        room.wake.set()
        await self.until(lambda: room.reason == "error" and not room.active)
        self.assertFalse(room.quotas)
        self.assertEqual(room.members["short"].error, "Operation not permitted")
        self.assertIsNone(room.quota_timer)

    async def test_restart_preserves_deadline_and_still_requires_resume(self):
        for mode in ("chatroom", "serial"):
            room = self.start(Scripted(QuotaExceeded("Quota exhausted")), Scripted(), mode)
            await self.until(lambda room=room: "short" in room.quotas and not room.active)
            deadline = room.quotas["short"]["retry_at"]
            await room.close()
            claude = Scripted("Restored")
            recovered = self.start(claude, Scripted(), mode, store=room.store)
            self.assertEqual(recovered.quotas["short"]["retry_at"], deadline)
            self.assertTrue(recovered.manual_paused)
            recovered.control("resume")
            await asyncio.sleep(0.02)
            self.assertFalse(claude.calls)
            self.now = deadline
            recovered.wake.set()
            await self.until(
                lambda recovered=recovered: not recovered.quotas and not recovered.active
            )
            await recovered.close()
            again = self.start(Scripted(), Scripted(), mode, store=room.store)
            self.assertFalse(again.quotas)
            await again.close()

    async def test_claude_quota_never_waives_consensus_votes(self):
        claude = Scripted(QuotaExceeded("Quota exhausted"))
        codex = Scripted(action_reply("Agreed", {"action": "approve", "version": 1}))
        room = self.start(claude, codex, build=True, start=False)
        room.workflow.apply("long", shared_plan())
        room.start()
        room.say("human", "Review the proposal")
        await self.until(lambda room=room: "short" in room.quotas and not room.active)
        self.assertEqual(room.workflow.phase, "discussion")
        self.assertEqual(room.workflow.data["approvals"], ["long"])
        self.assertFalse(room.workflow.data["consensus_history"])
        self.assertFalse(room.manual_paused)

    async def test_unavailable_preferred_writer_is_skipped_without_self_approval(self):
        room = self.start(Scripted(), Scripted(), build=True, start=False)
        room.workflow.apply("long", shared_plan())
        for name in room.adapters:
            room.workflow.apply(name, {"action": "approve", "version": 1})
        room.workflow.data["next_writer"] = "short"
        room.record_quota("short", QuotaExceeded("Quota exhausted"), "fixture")
        self.assertEqual(room.choose_available(), "long")
        room.workflow.data.update(phase="judging", checkpoint={"author": "long", "approvals": []})
        self.assertIsNone(room.choose_available())

    async def test_codex_takes_over_approved_work_but_waits_for_claude_judgment(self):
        phases = []

        class CodexWriter:
            async def stream(inner, prompt, *, phase):
                phases.append(phase)
                if phase == "implementation":
                    (Path(self.temp.name) / "calc.py").write_text("def add(a, b): return a + b\n")
                    yield action_reply(
                        "Implemented",
                        {
                            "action": "contribute",
                            "version": 1,
                            "task_id": "code",
                            "ready": True,
                            "summary": "Implemented addition",
                            "files": ["calc.py"],
                            "tests": "Not run",
                        },
                    )
                else:
                    yield "[[PASS]]"

        claude = Scripted(QuotaExceeded("Quota exhausted"))
        room = self.start(claude, CodexWriter(), build=True, start=False)
        room.workflow.apply("long", shared_plan())
        for name in room.adapters:
            room.workflow.apply(name, {"action": "approve", "version": 1})
        room.workflow.confirm_consensus()
        room.workflow.data["next_writer"] = "short"
        room.start()
        room.publish_system("Implement the approved shared plan")
        await self.until(lambda: room.workflow.phase == "judging" and not room.active)
        self.assertIn("implementation", phases)
        self.assertEqual(claude.calls[0]["phase"], "implementation")
        self.assertEqual(room.workflow.data["checkpoint"]["author"], "long")
        self.assertEqual(room.workflow.data["checkpoint"]["approvals"], [])
        self.assertFalse(room.manual_paused)
        self.assertIsNotNone(room.quota_timer)
