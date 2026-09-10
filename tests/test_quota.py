from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_chatroom import Scripted, transcript
from test_collaboration import shared_plan

from agent_team.adapters import (
    AdapterError,
    CLIAdapter,
    EventDecoder,
    QuotaExceeded,
    codex_quota_reset,
    provider_error,
)
from agent_team.chatroom import ChatRoom
from agent_team.config import AgentConfig, TeamConfig
from agent_team.engine import QUOTA_PROBE_PROMPT, QUOTA_RESET_BUFFER_SECONDS, Room
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
                "rate_limit_info": {
                    "status": "rejected",
                    "rateLimitType": "five_hour",
                    "resetsAt": 2_000_000_600,
                },
            }
        )
        with self.assertRaises(QuotaExceeded) as raised:
            decoder.feed(
                {
                    "type": "assistant",
                    "error": "rate_limit",
                    "message": {"content": [{"type": "text", "text": "Try again later"}]},
                }
            )
        self.assertEqual(raised.exception.resets_at, 2_000_000_600)
        self.assertEqual(raised.exception.limit_type, "five_hour")

    def test_advisory_or_malformed_metadata_cannot_supply_a_retry_deadline(self):
        for info in (
            None,
            [],
            "rejected",
            {"status": "allowed", "resetsAt": 2_000_000_600},
            {"status": "allowed_warning", "resetsAt": 2_000_000_600},
        ):
            decoder = EventDecoder("claude")
            # A later event replaces, rather than merges with, the rejected window.
            decoder.feed(
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {
                        "status": "rejected",
                        "resetsAt": 2_000_000_600,
                    },
                }
            )
            decoder.feed({"type": "rate_limit_event", "rate_limit_info": info})
            with self.subTest(info=info), self.assertRaises(QuotaExceeded) as raised:
                decoder.feed({"type": "result", "is_error": True, "result": "Quota exhausted"})
            self.assertIsNone(raised.exception.resets_at)

    def test_rejected_metadata_does_not_reclassify_an_unrelated_failure(self):
        decoder = EventDecoder("claude")
        decoder.feed(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "status": "rejected",
                    "resetsAt": 2_000_000_600,
                },
            }
        )
        with self.assertRaises(AdapterError) as raised:
            decoder.feed({"type": "result", "is_error": True, "result": "Operation not permitted"})
        self.assertNotIsInstance(raised.exception, QuotaExceeded)

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

    def test_native_capacity_errors_are_failures_but_not_usage_quotas(self):
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


class CodexResetTests(unittest.TestCase):
    def window(self, used=100, reset=2_000_000_600):
        return {"usedPercent": used, "resetsAt": reset}

    def test_waits_for_the_latest_exhausted_window(self):
        self.assertEqual(
            codex_quota_reset(
                {
                    "rateLimits": {
                        "primary": self.window(),
                        "secondary": self.window(reset=2_000_086_400),
                        "rateLimitReachedType": "rate_limit_reached",
                    }
                }
            ),
            (2_000_086_400, "primary+secondary"),
        )
        self.assertEqual(
            codex_quota_reset(
                {
                    "rateLimits": {
                        "primary": self.window(99),
                        "secondary": self.window(),
                    }
                }
            ),
            (2_000_000_600, "secondary"),
        )
        self.assertEqual(
            codex_quota_reset(
                {
                    "rateLimits": {
                        "primary": self.window(),
                        "secondary": None,
                    }
                }
            ),
            (2_000_000_600, "primary"),
        )

    def test_missing_reset_on_any_exhausted_window_is_not_guessed(self):
        for value in (
            None,
            True,
            "2000000600",
            0,
            -1,
            float("inf"),
            float("nan"),
            2_000_000_600_000,
            [],
            {},
            10**400,
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    codex_quota_reset(
                        {
                            "rateLimits": {
                                "primary": self.window(),
                                "secondary": self.window(reset=value),
                            }
                        }
                    ),
                    (None, None),
                )

    def test_only_reliable_usage_windows_provide_reset_times(self):
        for snapshot in (
            {},
            None,
            [],
            "invalid",
            {"primary": self.window(99)},
            *(
                {"primary": self.window(value)}
                for value in (None, True, "100", [], {}, -1, float("inf"), float("nan"))
            ),
            *(
                {"primary": self.window(), "rateLimitReachedType": kind}
                for kind in (
                    "workspace_owner_credits_depleted",
                    "workspace_member_usage_limit_reached",
                    "unknown",
                    [],
                    {},
                )
            ),
            {"primary": self.window(), "spendControlReached": True},
            {"primary": self.window(), "secondary": []},
        ):
            with self.subTest(snapshot=snapshot):
                self.assertEqual(codex_quota_reset({"rateLimits": snapshot}), (None, None))
        for value in (None, [], True, "invalid"):
            self.assertEqual(codex_quota_reset(value), (None, None))

    def test_ambiguous_buckets_are_not_selected_by_model_name(self):
        snapshot = {"primary": self.window()}
        self.assertEqual(
            codex_quota_reset({"rateLimitsByLimitId": {"codex": snapshot}}),
            (2_000_000_600, "primary"),
        )
        buckets = {
            "codex": snapshot,
            "model-specific": {"primary": self.window(reset=2_000_000_900)},
        }
        self.assertEqual(codex_quota_reset({"rateLimitsByLimitId": buckets}), (None, None))
        self.assertEqual(
            codex_quota_reset({"rateLimits": snapshot, "rateLimitsByLimitId": buckets}),
            (None, None),
        )


class QuotaProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_serial_cli_preserves_reset_metadata_on_all_quota_error_paths(self):
        info = {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "rejected",
                "rateLimitType": "seven_day",
                "resetsAt": 2_000_086_400,
            },
        }
        for failure in (
            {
                "type": "assistant",
                "error": "rate_limit",
                "message": {"content": [{"type": "text", "text": "Try later"}]},
            },
            {"type": "result", "is_error": True, "result": "You've hit your weekly limit"},
            None,
        ):
            script = "import sys; sys.stdin.read(); " + f"print({json.dumps(info)!r}); "
            if failure:
                script += f"print({json.dumps(failure)!r})"
            else:
                script += "sys.stderr.write('Quota exhausted'); sys.exit(2)"
            adapter = CLIAdapter(AgentConfig("claude", "claude"), Path.cwd())
            with (
                self.subTest(failure=failure),
                patch(
                    "agent_team.adapters.command_for", return_value=[sys.executable, "-c", script]
                ),
                self.assertRaises(QuotaExceeded) as raised,
            ):
                _ = [text async for text in adapter.stream("An idea")]
            self.assertEqual(raised.exception.resets_at, 2_000_086_400)
            self.assertEqual(raised.exception.limit_type, "seven_day")


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

    async def test_either_backend_pauses_everyone_without_a_fixed_retry(self):
        for mode in ("chatroom", "serial"):
            for name in ("short", "long"):
                with self.subTest(mode=mode, name=name):
                    agents = {"short": Scripted(), "long": Scripted()}
                    agents[name] = Scripted(QuotaExceeded("Usage exhausted"), "[[PASS]]")
                    room = self.start(agents["short"], agents["long"], mode)
                    await self.until(
                        lambda room=room, name=name: name in room.quotas and not room.active
                    )
                    self.assertTrue(room.status()["paused"])
                    self.assertEqual(room.reason, "quota")
                    quota = room.quotas[name]
                    self.assertIsNone(quota["retry_at"])
                    self.assertIsNone(quota["resets_at"])
                    self.assertEqual(quota["retry_source"], "unknown")
                    self.assertIsNone(room.quota_timer)
                    counts = [len(a.calls) for a in agents.values()]
                    self.now += 8 * 86400
                    room.say("human", "Guidance is not permission to resume")
                    room.wake.set()
                    await asyncio.sleep(0.02)
                    self.assertEqual([len(a.calls) for a in agents.values()], counts)
                    self.assertEqual(room.reason, "quota")
                    room.control("resume")
                    await self.until(lambda room=room: not room.quotas and not room.active)
                    self.assertEqual(
                        agents[name].calls[counts[list(agents).index(name)]]["phase"], "recovery"
                    )
                    await room.close()

    async def test_either_quota_cancels_peers_and_discards_stale_output(self):
        for name in ("short", "long"):
            fail, peer = asyncio.Event(), asyncio.Event()
            limited = Scripted((fail, QuotaExceeded("Usage exhausted")))
            working = Scripted((peer, "Stale reply"), resist_cancel=True)
            agents = {"short": limited, "long": working}
            if name == "long":
                agents = {"short": working, "long": limited}
            room = self.start(agents["short"], agents["long"])
            await self.until(
                lambda limited=limited, working=working: limited.calls and working.calls
            )
            fail.set()
            await self.until(lambda room=room, name=name: name in room.quotas and not room.active)
            self.assertEqual(working.cancelled, 1)
            self.assertNotIn("Stale reply", [m["text"] for m in room.messages])
            self.assertTrue(room.status()["paused"])
            await room.close()

    async def test_provider_deadline_probes_in_isolation_before_resuming(self):
        self.assertEqual(QUOTA_RESET_BUFFER_SECONDS, 30)
        for mode in ("chatroom", "serial"):
            for name in ("short", "long"):
                reset = self.now + 600
                gate = asyncio.Event()
                limited = Scripted(
                    QuotaExceeded("Usage exhausted", resets_at=reset, limit_type="window"),
                    (gate, "PROBE OUTPUT MUST STAY PRIVATE"),
                    "Fresh discussion",
                )
                agents = {"short": Scripted(), "long": Scripted()}
                agents[name] = limited
                room = self.start(agents["short"], agents["long"], mode)
                events = []
                room.broadcast = events.append
                await self.until(
                    lambda room=room, name=name: name in room.quotas and not room.active
                )
                self.assertEqual(room.quotas[name]["retry_at"], reset + 30)
                self.assertEqual(room.quotas[name]["retry_source"], "provider")
                self.assertEqual(room.quotas[name]["limit_type"], "window")
                peer = agents["long" if name == "short" else "short"]
                count = len(peer.calls)
                for now in (reset, reset + 29):
                    self.now = now
                    room.wake.set()
                    await asyncio.sleep(0.02)
                    self.assertEqual(len(limited.calls), 1)
                self.now = reset + 30
                room.wake.set()
                await self.until(lambda limited=limited: len(limited.calls) == 2)
                self.assertEqual(limited.calls[1]["phase"], "recovery")
                self.assertEqual(limited.calls[1]["prompt"], QUOTA_PROBE_PROMPT)
                self.assertIsNone(limited.calls[1]["session_id"])
                self.assertTrue(room.status()["paused"])
                self.assertEqual(len(peer.calls), count)
                gate.set()
                await self.until(lambda room=room: not room.quotas and not room.active)
                self.assertGreaterEqual(len(limited.calls), 3)
                texts = [m["text"] for m in room.messages]
                self.assertIn("Fresh discussion", texts)
                self.assertNotIn("PROBE OUTPUT MUST STAY PRIVATE", texts)
                self.assertNotIn(
                    "PROBE OUTPUT MUST STAY PRIVATE",
                    [e.get("text") for e in events if e["type"] == "delta"],
                )
                self.assertIsNone(limited.calls[2]["session_id"])
                self.assertIn(
                    "Initial idea", [m["text"] for m in transcript(limited.calls[2]["prompt"])]
                )
                await room.close()

    async def test_all_limited_members_must_recover_before_any_normal_work(self):
        for mode in ("chatroom", "serial"):
            claude, codex = Scripted("[[PASS]]"), Scripted("[[PASS]]")
            room = self.start(claude, codex, mode, start=False)
            room.publish_system("Initial idea")
            for name, reset in (("short", self.now + 100), ("long", self.now + 200)):
                room.record_quota(name, QuotaExceeded("Usage exhausted", resets_at=reset), "t")
            room.start()
            self.now += 130
            room.wake.set()
            await self.until(lambda room=room: "short" not in room.quotas and not room.active)
            self.assertEqual([c["phase"] for c in claude.calls], ["recovery"])
            self.assertFalse(codex.calls)
            self.assertTrue(room.status()["paused"])
            self.now += 100
            room.wake.set()
            await self.until(
                lambda room=room, codex=codex: codex.calls and not room.quotas and not room.active
            )
            self.assertEqual(codex.calls[0]["phase"], "recovery")
            self.assertGreater(len(claude.calls), 1)
            self.assertGreater(len(codex.calls), 1)
            await room.close()

    async def test_known_reset_can_be_checked_but_unknown_quota_still_blocks_discussion(self):
        for mode in ("chatroom", "serial"):
            claude, codex = Scripted(), Scripted()
            room = self.start(claude, codex, mode, start=False)
            room.publish_system("Initial idea")
            room.record_quota("short", QuotaExceeded("Unknown reset"), "a")
            room.record_quota("long", QuotaExceeded("Known reset", resets_at=self.now + 100), "b")
            room.start()
            self.now += 130
            room.wake.set()
            await self.until(lambda room=room: "long" not in room.quotas and not room.active)
            self.assertFalse(claude.calls)
            self.assertEqual([c["phase"] for c in codex.calls], ["recovery"])
            self.assertIsNone(room.quota_timer)
            room.control("retry", "long")
            await asyncio.sleep(0.02)
            self.assertTrue(room.status()["paused"])
            self.assertEqual(len(codex.calls), 1)
            room.control("retry", "short")
            await self.until(
                lambda room=room, claude=claude: (
                    claude.calls and not room.quotas and not room.active
                )
            )
            self.assertEqual(claude.calls[0]["phase"], "recovery")
            await room.close()

    async def test_targeted_retry_does_not_clear_other_quotas(self):
        room = self.start(Scripted(), Scripted(), start=False)
        room.publish_system("Initial idea")
        for name in room.adapters:
            room.record_quota(name, QuotaExceeded("Usage exhausted"), "t")
        room.start()
        room.control("retry", "short")
        await self.until(lambda room=room: "short" not in room.quotas and not room.active)
        self.assertIn("long", room.quotas)
        self.assertTrue(room.status()["paused"])
        self.assertFalse(room.adapters["long"].calls)
        self.assertEqual([c["phase"] for c in room.adapters["short"].calls], ["recovery"])

    async def test_next_cannot_bypass_a_team_quota_pause(self):
        for mode in ("chatroom", "serial"):
            room = self.start(Scripted(), Scripted(), mode, start=False)
            room.publish_system("Initial idea")
            room.record_quota("short", QuotaExceeded("Usage exhausted"), "t")
            for target in (None, "short", "long"):
                with self.assertRaisesRegex(ValueError, "use /retry or /resume"):
                    room.control("next", target)
            self.assertFalse(room.quota_retries)
            await room.close()

    async def test_due_recovery_waits_for_writer_cancellation_cleanup(self):
        started, stopping, cleanup, fail, probe = (asyncio.Event() for _ in range(5))

        class Writer:
            async def stream(inner, prompt, *, phase):
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    stopping.set()
                    await cleanup.wait()
                yield "Stale checkpoint"

        codex = Scripted(
            (fail, QuotaExceeded("Usage exhausted", resets_at=self.now + 100)),
            (probe, "[[PASS]]"),
        )
        room = self.start(Writer(), codex, build=True, start=False)
        room.workflow.apply("long", shared_plan())
        for name in room.adapters:
            # "long" approved by proposing; approving twice would land after consensus.
            if name not in room.workflow.data["approvals"]:
                room.workflow.apply(name, {"action": "approve", "version": 1})
        room.workflow.confirm_consensus()
        room.workflow.data["next_writer"] = "short"
        room.start()
        room.publish_system("Implement the approved plan")
        try:
            await self.until(lambda: started.is_set() and codex.calls)
            self.assertEqual(room.writer, "short")
            fail.set()
            await self.until(stopping.is_set)
            self.now += 130
            room.wake.set()
            await self.until(lambda: "long" in room.quota_retries)
            self.assertEqual(room.writer, "short")
            self.assertEqual(len(codex.calls), 1)
            for action in ("retry", "resume"):
                with self.assertRaisesRegex(ValueError, "Wait for all interrupted turns"):
                    room.control(action)
        finally:
            cleanup.set()
        await self.until(lambda: len(codex.calls) == 2)
        self.assertIsNone(room.writer)
        self.assertEqual(codex.calls[1]["phase"], "recovery")
        self.assertNotIn("Stale checkpoint", [m["text"] for m in room.messages])
        self.assertEqual(room.workflow.phase, "implementation")
        self.assertTrue(room.status()["paused"])
        room.control("interrupt")

    async def test_unusable_reset_times_stay_unknown_and_json_safe(self):
        room = self.start(Scripted(), Scripted(), start=False)
        for reset in (
            None,
            True,
            False,
            [],
            {},
            "2000000600",
            float("nan"),
            float("inf"),
            -float("inf"),
            0,
            -1,
            self.now,
            self.now - 1,
            self.now * 1000,
            10**400,
        ):
            for name in room.adapters:
                with self.subTest(reset=reset, name=name):
                    room.record_quota(name, QuotaExceeded("Usage exhausted", resets_at=reset), "t")
                    quota = room.quotas[name]
                    self.assertIsNone(quota["retry_at"])
                    self.assertEqual(quota["retry_source"], "unknown")
                    self.assertIsNone(quota["resets_at"])
                    json.dumps(quota, allow_nan=False)

    async def test_repeated_limit_replaces_deadline_and_never_falls_back(self):
        for mode in ("chatroom", "serial"):
            first, second = self.now + 300, self.now + 900
            claude = Scripted(
                QuotaExceeded("First limit", resets_at=first),
                QuotaExceeded("New limit", resets_at=second),
                QuotaExceeded("No reset reported"),
            )
            room = self.start(claude, Scripted(), mode)
            await self.until(lambda room=room: "short" in room.quotas and not room.active)
            self.now = first + 30
            room.wake.set()
            await self.until(
                lambda room=room, claude=claude: len(claude.calls) == 2 and not room.active
            )
            self.assertEqual(room.quotas["short"]["retry_at"], second + 30)
            self.now = second + 30
            room.wake.set()
            await self.until(
                lambda room=room, claude=claude: len(claude.calls) == 3 and not room.active
            )
            self.assertIsNone(room.quotas["short"]["retry_at"])
            self.assertIsNone(room.quota_timer)
            self.assertTrue(room.status()["paused"])
            await room.close()

    async def test_provider_timer_retries_without_messages_or_connected_humans(self):
        for mode in ("chatroom", "serial"):
            with (
                patch("agent_team.engine.time.time", side_effect=REAL_TIME),
                patch("agent_team.engine.QUOTA_RESET_BUFFER_SECONDS", 0.05),
            ):
                claude = Scripted(
                    QuotaExceeded("Usage exhausted", resets_at=REAL_TIME() + 0.1), "[[PASS]]"
                )
                room = self.start(claude, Scripted(), mode)
                await self.until(
                    lambda room=room, claude=claude: (
                        len(claude.calls) >= 2 and not room.quotas and not room.active
                    )
                )
                self.assertEqual(claude.calls[1]["phase"], "recovery")
                await room.close()

    async def test_manual_pause_defers_due_retry_until_resume(self):
        for mode in ("chatroom", "serial"):
            claude = Scripted(QuotaExceeded("Usage exhausted", resets_at=self.now + 300))
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
            await self.until(
                lambda room=room, claude=claude: (
                    len(claude.calls) > 1 and not room.quotas and not room.active
                )
            )
            self.assertEqual(claude.calls[1]["phase"], "recovery")
            await room.close()

    async def test_cancelled_recovery_never_clears_quota_or_resumes_team(self):
        for mode in ("chatroom", "serial"):
            gate = asyncio.Event()
            claude = Scripted(
                QuotaExceeded("Usage exhausted"), (gate, "[[PASS]]"), resist_cancel=True
            )
            room = self.start(claude, Scripted(), mode)
            await self.until(lambda room=room: "short" in room.quotas and not room.active)
            room.control("resume")
            await self.until(lambda claude=claude: len(claude.calls) == 2)
            room.control("interrupt")
            await self.until(lambda room=room: not room.active)
            self.assertIn("short", room.quotas)
            self.assertTrue(room.manual_paused)
            self.assertFalse(room.quota_retries)
            await room.close()

    async def test_nonquota_failure_during_recovery_requires_explicit_resume(self):
        for mode in ("chatroom", "serial"):
            claude = Scripted(
                QuotaExceeded("Usage exhausted", resets_at=self.now + 300),
                AdapterError("Operation not permitted"),
            )
            room = self.start(claude, Scripted(), mode)
            await self.until(lambda room=room: "short" in room.quotas and not room.active)
            self.now += 330
            room.wake.set()
            await self.until(lambda room=room: room.reason == "error" and not room.active)
            self.assertTrue(room.manual_paused)
            self.assertIsNone(room.quota_timer)
            self.assertFalse(room.quotas)
            self.now += 86400
            room.say("human", "Do not resume yet")
            await asyncio.sleep(0.02)
            self.assertEqual(len(claude.calls), 2)
            await room.close()

    async def test_recovering_room_is_distinguishable_from_an_idle_one(self):
        """Issue #1: between clearing a quota and scheduling a turn, a room reported
        itself running, unpaused, with nothing active — exactly like an idle team."""
        for mode in ("chatroom", "serial"):
            room = self.start(
                Scripted(QuotaExceeded("Usage exhausted", resets_at=self.now + 300)),
                Scripted(),
                mode,
            )
            await self.until(lambda room=room: "short" in room.quotas and not room.active)
            self.assertEqual(room.room_state(), "paused_by_quota")
            room.quota_succeeded("short")
            self.assertFalse(room.quotas)
            status = room.status()
            self.assertIsNone(status["active"])
            self.assertEqual(status["room_state"], "running")
            self.assertEqual(status["reason"], "recovering")
            await room.close()

    async def test_restart_preserves_provider_deadline_and_recovers_unattended(self):
        for mode in ("chatroom", "serial"):
            room = self.start(
                Scripted(QuotaExceeded("Usage exhausted", resets_at=self.now + 300)),
                Scripted(),
                mode,
            )
            await self.until(lambda room=room: "short" in room.quotas and not room.active)
            saved = room.quotas["short"].copy()
            await room.close()
            claude = Scripted()
            recovered = self.start(claude, Scripted(), mode, store=room.store)
            self.assertEqual(recovered.quotas["short"], saved)
            self.assertTrue(recovered.manual_paused)
            self.assertEqual(recovered.room_state(), "paused_by_quota")
            # Before the provider's reset the restarted room stays put.
            recovered.wake.set()
            await asyncio.sleep(0.02)
            self.assertFalse(claude.calls)
            # After it, the room recovers with nobody present to resume it.
            self.now = saved["retry_at"] + 1
            recovered.wake.set()
            await self.until(
                lambda recovered=recovered: not recovered.quotas and not recovered.active
            )
            self.assertEqual(claude.calls[0]["phase"], "recovery")
            # The restart pause is gone; any later pause is the room's own doing.
            self.assertFalse(recovered.restart_paused)
            await recovered.close()
            again = self.start(Scripted(), Scripted(), mode, store=room.store)
            self.assertFalse(again.quotas)
            await again.close()

    async def test_old_fixed_delay_records_are_discarded_without_deleting_history(self):
        for mode in ("chatroom", "serial"):
            for source in (None, "fallback", "manual"):
                room = self.start(Scripted(), Scripted(), mode, start=False)
                room.publish_system("Saved idea")
                legacy = {"backend": "claude", "error": "Old limit", "retry_at": self.now + 18000}
                if source:
                    legacy["retry_source"] = source
                room.emit("agent.quota", speaker="short", quota=legacy)
                await room.close()
                recovered = self.start(Scripted(), Scripted(), mode, store=room.store)
                self.assertIsNone(recovered.quotas["short"]["retry_at"])
                self.assertEqual(recovered.quotas["short"]["retry_source"], "unknown")
                self.assertTrue(recovered.manual_paused)
                self.assertEqual(recovered.messages[0]["text"], "Saved idea")
                self.assertIsNone(recovered.quota_timer)
                await recovered.close()

    async def test_quota_never_waives_consensus_or_transfers_the_write_lease(self):
        for mode in ("chatroom", "serial"):
            for phase in ("discussion", "implementation", "judging", "acceptance"):
                room = self.start(Scripted(), Scripted(), mode, build=True, start=False)
                room.workflow.apply("long", shared_plan())
                room.start()
                room.workflow.data["phase"] = phase
                room.record_quota("short", QuotaExceeded("Usage exhausted"), "t")
                room.publish_system("Waiting for all members")
                await asyncio.sleep(0.02)
                self.assertFalse(any(a.calls for a in room.adapters.values()))
                self.assertEqual(room.workflow.phase, phase)
                self.assertFalse(room.workflow.data["consensus_history"])
                self.assertIsNone(room.active)
                await room.close()

    async def test_recovery_response_cannot_apply_a_workflow_action(self):
        for mode in ("chatroom", "serial"):
            room = self.start(
                Scripted(
                    action_reply("Untrusted probe reply", {"action": "approve", "version": 1})
                ),
                Scripted(),
                mode,
                build=True,
                start=False,
            )
            room.workflow.apply("long", shared_plan())
            room.record_quota("short", QuotaExceeded("Usage exhausted"), "t")
            room.record_quota("long", QuotaExceeded("Usage exhausted"), "t")
            room.start()
            room.control("retry", "short")
            await self.until(lambda room=room: "short" not in room.quotas and not room.active)
            # "long" approved by proposing; the probe reply must not have added "short".
            self.assertEqual(room.workflow.data["approvals"], ["long"])
            self.assertNotIn("Untrusted probe reply", [m["text"] for m in room.messages])
            self.assertFalse(room.adapters["long"].calls)
            await room.close()
