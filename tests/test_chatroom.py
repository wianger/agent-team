from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path

from agent_team.adapters import AdapterError, MockAdapter
from agent_team.chatroom import ChatRoom
from agent_team.config import AgentConfig, TeamConfig, demo_config
from agent_team.context import DELTA_MARKER, PASS, TRANSCRIPT_MARKER
from agent_team.engine import PROTOCOL_LAPSE_LIMIT
from agent_team.store import Store
from agent_team.workflow import action_reply, parse_action


def transcript(prompt):
    marker = DELTA_MARKER if DELTA_MARKER in prompt else TRANSCRIPT_MARKER
    return json.loads(prompt.split(marker, 1)[1])


class Scripted:
    supports_sessions = True

    def __init__(self, *replies, resist_cancel=False):
        self.replies = list(replies)
        self.calls = []
        self.cancelled = 0
        self.resist_cancel = resist_cancel
        self.result_session_id = None

    async def stream(self, prompt, *, phase="discussion", persist_session=True, session_id=None):
        self.calls.append({"prompt": prompt, "phase": phase, "session_id": session_id})
        reply = self.replies.pop(0) if self.replies else PASS
        if isinstance(reply, tuple):
            gate, reply = reply
            try:
                await gate.wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                if not self.resist_cancel:
                    raise
        if isinstance(reply, Exception):
            raise reply
        yield reply
        self.result_session_id = session_id or str(uuid.uuid4())


class ChatRoomTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = Store(self.path / "events.sqlite3")
        self.events = []
        self.room = None

    async def asyncTearDown(self):
        if self.room and not self.room.closed:
            await self.room.close()
        self.store.close()
        self.temp.cleanup()

    def start(self, adapters, config=None):
        config = config or TeamConfig(
            agents=tuple(AgentConfig(n, "mock") for n in adapters),
            workflow="discussion",
            workspace=self.path,
            interaction_mode="chatroom",
            turn_delay=0,
        )
        self.room = ChatRoom(config, self.store, self.events.append, adapters)
        self.room.start()
        self.room.say("human", "Initial idea")
        return self.room

    async def until(self, predicate, timeout=5):
        async with asyncio.timeout(timeout):
            while not predicate():
                if self.room.runner.done():
                    self.room.runner.result()
                await asyncio.sleep(0.005)

    async def test_private_activity_reports_are_isolated_from_public_context_and_peer_wakeups(self):
        class ActiveAdapter:
            supports_activity = True
            activity = None

            async def stream(self, prompt, *, phase="discussion", on_activity=None):
                self.activity = on_activity
                await asyncio.Event().wait()
                yield PASS

        a, b = ActiveAdapter(), ActiveAdapter()
        room = self.start({"a": a, "b": b})
        await self.until(lambda: a.activity and b.activity)
        a.activity()
        b.activity()
        reports = [e for e in self.events if e["type"] == "turn.activity"]
        self.assertEqual(
            {(e["speaker"], e["turn_id"]) for e in reports},
            {(name, room.members[name].active.turn_id) for name in ("a", "b")},
        )
        self.assertEqual(len(room.messages), 1)
        self.assertEqual(sum(e["type"] == "turn.started" for e in self.events), 2)
        self.assertNotIn("turn.activity", [e["type"] for e in self.store.events()])
        room.control("interrupt")
        await self.until(lambda: room.active is None)
        a.activity()
        b.activity()
        self.assertEqual(sum(e["type"] == "turn.activity" for e in self.events), 2)

    async def test_concurrent_thought_and_nonblocking_human_chat_preserve_all_context(self):
        a_gate, b_gate = asyncio.Event(), asyncio.Event()
        a, b = Scripted((a_gate, "A contribution")), Scripted((b_gate, "B contribution"))
        room = self.start({"a": a, "b": b})
        await self.until(lambda: a.calls and b.calls)
        self.assertEqual(len(room.status()["active_turns"]), 2)
        revision = room.revision
        room.say("human", "An additional opinion, not a redirect")
        self.assertEqual(room.revision, revision)
        b_gate.set()
        await self.until(lambda: any(m["text"] == "B contribution" for m in room.messages))
        self.assertEqual(a.cancelled, 0)
        self.assertTrue(room.members["a"].active)
        a_gate.set()
        await self.until(lambda: room.reason == "waiting_messages")
        self.assertFalse(room.manual_paused)
        a_input = transcript(a.calls[1]["prompt"])
        self.assertIn("B contribution", [m["text"] for m in a_input])
        self.assertIn("An additional opinion, not a redirect", [m["text"] for m in a_input])
        self.assertNotIn("A contribution", [m["text"] for m in a_input])
        self.assertTrue(
            any("A contribution" in [m["text"] for m in transcript(c["prompt"])] for c in b.calls)
        )
        self.assertEqual(len({c["session_id"] for c in a.calls[1:]}), 1)
        before = room.turns
        await asyncio.sleep(0.05)
        self.assertEqual(room.turns, before)  # No idle model polling.

    async def test_redirect_revokes_all_stale_replies_even_if_cancellation_is_ignored(self):
        gate = asyncio.Event()
        a = Scripted((gate, "stale A"), "fresh A", resist_cancel=True)
        b = Scripted((gate, "stale B"), "fresh B", resist_cancel=True)
        room = self.start({"a": a, "b": b})
        await self.until(lambda: a.calls and b.calls)
        room.redirect("human", "New direction")
        await self.until(lambda: room.reason == "waiting_messages")
        texts = [m["text"] for m in room.messages]
        self.assertNotIn("stale A", texts)
        self.assertNotIn("stale B", texts)
        self.assertIn("fresh A", texts)
        self.assertIn("fresh B", texts)
        self.assertEqual(a.cancelled, 1)
        self.assertEqual(b.cancelled, 1)
        self.assertIsNone(a.calls[1]["session_id"])

    async def test_pause_finishes_every_active_reply_without_starting_more(self):
        gate = asyncio.Event()
        a, b = Scripted((gate, "A")), Scripted((gate, "B"))
        room = self.start({"a": a, "b": b})
        await self.until(lambda: a.calls and b.calls)
        room.control("pause")
        gate.set()
        await self.until(lambda: room.active is None)
        self.assertEqual(room.turns, 2)
        self.assertTrue(room.manual_paused)
        self.assertEqual(a.cancelled + b.cancelled, 0)
        self.assertEqual(len(a.calls), 1)
        self.assertEqual(len(b.calls), 1)

    async def test_interrupt_cancels_every_member_and_next_runs_exactly_one(self):
        gate = asyncio.Event()
        a, b = Scripted((gate, "A"), "fresh"), Scripted((gate, "B"))
        room = self.start({"a": a, "b": b})
        await self.until(lambda: a.calls and b.calls)
        room.control("interrupt")
        await self.until(lambda: room.active is None)
        self.assertEqual(a.cancelled + b.cancelled, 2)
        room.control("next", "a")
        await self.until(lambda: room.reason == "step_complete")
        self.assertEqual(len(a.calls), 2)
        self.assertEqual(len(b.calls), 1)
        self.assertEqual(room.messages[-1]["text"], "fresh")

    async def test_member_failure_pauses_everyone_and_retry_is_explicit(self):
        failure_gate, peer_gate = asyncio.Event(), asyncio.Event()
        a = Scripted((failure_gate, AdapterError("Quota exhausted")), "Recovered")
        b = Scripted((peer_gate, "Stale reply"), "Ready too", resist_cancel=True)
        room = self.start({"a": a, "b": b})
        await self.until(lambda: a.calls and b.calls)
        failure_gate.set()
        await self.until(lambda: room.reason == "error" and not room.active)
        self.assertTrue(room.status()["paused"])
        self.assertIn("Quota exhausted", room.status()["runtimes"]["a"]["error"])
        self.assertNotIn("Stale reply", [m["text"] for m in room.messages])
        self.assertEqual(b.cancelled, 1)
        self.assertIn("entire team is paused", room.messages[-1]["text"])
        room.say("human", "Additional context while paused")
        room.redirect("human", "Reconsider after recovery")
        await asyncio.sleep(0.03)
        self.assertTrue(room.manual_paused)
        self.assertEqual(room.reason, "error")
        self.assertEqual(len(a.calls), 1)
        self.assertEqual(len(b.calls), 1)
        room.control("retry", "a")
        await self.until(lambda: room.reason == "waiting_messages")
        self.assertFalse(room.manual_paused)
        self.assertEqual(room.failed_members(), [])
        self.assertTrue(any(m["text"] == "Recovered" for m in room.messages))
        self.assertTrue(any(m["text"] == "Ready too" for m in room.messages))
        self.assertIsNone(a.calls[1]["session_id"])
        self.assertIsNone(b.calls[1]["session_id"])
        for adapter in (a, b):
            texts = [m["text"] for m in transcript(adapter.calls[1]["prompt"])]
            self.assertIn("Initial idea", texts)
            self.assertIn("Additional context while paused", texts)
            self.assertIn("Reconsider after recovery", texts)

    async def test_resume_retries_failures_and_a_repeated_failure_pauses_again(self):
        a, b = Scripted(RuntimeError(), AdapterError("Still unavailable"), "Recovered"), Scripted()
        room = self.start({"a": a, "b": b})
        await self.until(lambda: room.reason == "error" and not room.active)
        self.assertEqual(room.members["a"].error, "RuntimeError")
        room.control("resume")
        await self.until(lambda: len(a.calls) == 2 and not room.active)
        self.assertEqual(room.reason, "error")
        self.assertTrue(room.manual_paused)
        self.assertEqual(room.members["a"].error, "Still unavailable")
        room.control("retry")
        await self.until(lambda: room.reason == "waiting_messages")
        self.assertIn("Recovered", [m["text"] for m in room.messages])

    async def test_targeted_retry_cannot_resume_with_another_failure_outstanding(self):
        room = self.start({"a": Scripted(AdapterError("Quota exhausted")), "b": Scripted()})
        await self.until(lambda: room.reason == "error" and not room.active)
        calls = {name: len(adapter.calls) for name, adapter in room.adapters.items()}
        room.control("retry", "b")
        with self.assertRaisesRegex(ValueError, "Other members are unavailable"):
            room.control("next", "b")
        room.control("reset-session", "a")
        room.control("pause")
        room.control("interrupt")
        await asyncio.sleep(0.03)
        self.assertTrue(room.manual_paused)
        self.assertEqual(room.reason, "error")
        self.assertEqual(room.failed_members(), ["a"])
        self.assertEqual(
            calls, {name: len(adapter.calls) for name, adapter in room.adapters.items()}
        )

    async def test_peer_failure_cancels_writer_and_holds_lease_through_cleanup(self):
        started, stopping, cleanup, failure_gate = (asyncio.Event() for _ in range(4))

        class Writer:
            async def stream(inner, prompt, *, phase):
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    stopping.set()
                    await cleanup.wait()
                yield "Stale checkpoint"

        b = Scripted((failure_gate, AdapterError("Connection lost")))
        config = replace(
            demo_config(),
            workspace=self.path,
            interaction_mode="chatroom",
            turn_delay=0,
            agents=(AgentConfig("a", "mock"), AgentConfig("b", "mock")),
        )
        room = self.start({"a": Writer(), "b": b}, config)
        _, proposal = parse_action(
            MockAdapter(config.agents[0], self.path).workflow_reply(room.workflow.snapshot())
        )
        room.workflow.apply("a", proposal)
        for name in room.adapters:
            room.workflow.apply(name, {"action": "approve", "version": 1})
        room.workflow.data["phase"] = "discussion"
        room.publish_system("The fixture plan is ready for implementation")
        try:
            await self.until(lambda: started.is_set() and b.calls)
            self.assertEqual(room.writer, "a")
            failure_gate.set()
            await self.until(stopping.is_set)
            self.assertTrue(room.manual_paused)
            self.assertEqual(room.reason, "error")
            self.assertEqual(room.writer, "a")
            self.assertEqual(b.calls[0]["phase"], "chat")
            for action in ("retry", "resume", "next"):
                with self.assertRaisesRegex(ValueError, "Wait for all interrupted turns"):
                    room.control(action)
            started_turns = sum(e["type"] == "turn.started" for e in self.events)
            room.dispatch()
            self.assertEqual(started_turns, sum(e["type"] == "turn.started" for e in self.events))
        finally:
            cleanup.set()
        await self.until(lambda: not room.active)
        self.assertIsNone(room.writer)
        self.assertNotIn("Stale checkpoint", [m["text"] for m in room.messages])
        self.assertEqual(room.workflow.phase, "implementation")
        self.assertEqual(len(room.workflow.data["consensus_history"]), 1)

    async def test_parallel_build_preserves_consensus_judgments_and_exclusive_writes(self):
        config = replace(
            demo_config(),
            workspace=self.path,
            interaction_mode="chatroom",
            turn_delay=0,
            agents=tuple(AgentConfig(n, "mock") for n in ("a", "b", "c")),
        )
        adapters = {a.name: MockAdapter(a, self.path) for a in config.agents}
        room = self.start(adapters, config)
        await self.until(lambda: room.reason == "completed" and not room.active, 10)
        final = room.workflow.snapshot()
        self.assertEqual([r["exit_code"] for r in final["acceptance_results"]], [0])
        for milestone in final["proposal"]["milestones"]:
            point = milestone["contributions"][-1]
            self.assertEqual(
                {j["speaker"] for j in point["judgments"]}, set(adapters) - {point["author"]}
            )
        active = {}
        concurrent = False
        for event in self.events:
            if event["type"] == "turn.started":
                active[event["turn_id"]] = event
                work = [e for e in active.values() if e["lane"] == "work"]
                writers = [e for e in work if e["phase"] in {"implementation", "acceptance"}]
                self.assertLessEqual(len(writers), 1)
                if writers:
                    self.assertEqual(len(work), 1)
                if len([e for e in work if e["phase"] == "judging"]) >= 2:
                    concurrent = True
            elif event["type"] == "turn.finished":
                active.pop(event["turn_id"])
        self.assertTrue(concurrent)
        self.assertTrue(any("rejected_action" in m for m in room.messages))
        self.assertEqual(self.store.messages(), room.messages)

    async def test_peer_failure_interrupts_checks_and_rejects_late_success(self):
        checking, cancelled, failure_gate = (asyncio.Event() for _ in range(3))

        async def run_acceptance(turn_id, activity):
            checking.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
            return [{"command": ["fixture-check"], "exit_code": 0, "output": "Stale success"}]

        a = Scripted((failure_gate, AdapterError("Authentication failed")))
        b = Scripted((asyncio.Event(), "Stale chat"))
        config = replace(
            demo_config(),
            workspace=self.path,
            interaction_mode="chatroom",
            turn_delay=0,
            agents=(AgentConfig("a", "mock"), AgentConfig("b", "mock")),
        )
        room = self.start({"a": a, "b": b}, config)
        room.workflow.data["phase"] = "acceptance"
        room.run_acceptance = run_acceptance
        await self.until(lambda: checking.is_set() and a.calls and b.calls)
        self.assertEqual(room.writer, "system")
        failure_gate.set()
        await self.until(lambda: room.reason == "error" and not room.active)
        self.assertTrue(cancelled.is_set())
        self.assertTrue(room.manual_paused)
        self.assertIsNone(room.writer)
        self.assertEqual(room.workflow.phase, "acceptance")
        self.assertFalse(room.workflow.data["acceptance_results"])
        self.assertNotIn("Stale chat", [m["text"] for m in room.messages])
        self.assertNotIn("Stale success", "\n".join(m["text"] for m in room.messages))

    async def test_invalid_action_and_explicit_timeout_pause_the_team(self):
        a = Scripted('<team-action>{"action":}</team-action>', TimeoutError(), PASS)
        config = replace(
            demo_config(),
            workspace=self.path,
            interaction_mode="chatroom",
            turn_delay=0,
            agents=(AgentConfig("a", "mock"), AgentConfig("b", "mock")),
        )
        room = self.start({"a": a, "b": Scripted()}, config)
        await self.until(lambda: room.reason == "error" and not room.active)
        self.assertTrue(room.manual_paused)
        self.assertIsNotNone(room.members["a"].error)
        self.assertIsNone(room.workflow.data["proposal"])
        room.control("retry", "a")
        await self.until(lambda: len(a.calls) == 2 and not room.active)
        self.assertTrue(room.manual_paused)
        self.assertEqual(room.reason, "error")
        self.assertEqual(room.members["a"].error, "Turn timed out")
        room.control("resume")
        await self.until(lambda: room.reason == "waiting_messages")
        self.assertFalse(room.manual_paused)

    async def test_restart_retains_history_but_does_not_silently_resume(self):
        room = self.start({"a": Scripted("A"), "b": Scripted("B")})
        await self.until(lambda: room.reason == "waiting_messages")
        await room.close()
        self.room = ChatRoom(
            room.config, self.store, self.events.append, {"a": Scripted(), "b": Scripted()}
        )
        self.room.start()
        self.assertEqual(self.room.reason, "restart")
        self.assertTrue(self.room.manual_paused)
        self.assertEqual(self.room.messages, room.messages)

    async def test_consensus_waits_for_inflight_objection_before_starting_a_writer(self):
        object_gate, approve_gate = asyncio.Event(), asyncio.Event()
        approval = action_reply("Agreed", {"action": "approve", "version": 1})
        objection = action_reply(
            "Missing requirement", {"action": "object", "version": 1, "reason": "Need evidence"}
        )
        a, b, c = (
            Scripted(approval, (object_gate, objection)),
            Scripted(approval),
            Scripted((approve_gate, approval)),
        )
        config = replace(
            demo_config(),
            workspace=self.path,
            interaction_mode="chatroom",
            turn_delay=0,
            agents=tuple(AgentConfig(n, "mock") for n in ("a", "b", "c")),
        )
        room = self.start({"a": a, "b": b, "c": c}, config)
        _, proposal = parse_action(
            MockAdapter(config.agents[0], self.path).workflow_reply(room.workflow.snapshot())
        )
        room.workflow.apply("a", proposal)
        room.publish_system("A proposed the fixture plan")
        await self.until(lambda: len(a.calls) >= 2 and c.calls)
        approve_gate.set()
        await self.until(lambda: set(room.workflow.data["approvals"]) == {"a", "b", "c"})
        self.assertEqual(room.workflow.phase, "discussion")
        self.assertIsNone(room.writer)
        self.assertEqual(room.workflow.data["consensus_history"], [])
        self.assertFalse(list(self.path.glob("docs/agent-team/*/consensus-*.md")))
        object_gate.set()
        await self.until(lambda: room.reason == "waiting_messages")
        self.assertEqual(room.workflow.data["objections"], {"a": "Need evidence"})
        self.assertFalse(list(self.path.glob("docs/agent-team/*/consensus-*.md")))
        self.assertFalse(
            any(e["type"] == "turn.started" and e["phase"] == "implementation" for e in self.events)
        )

    async def test_failure_does_not_finalize_a_pending_consensus(self):
        failure_gate, approval_gate = asyncio.Event(), asyncio.Event()
        approval = action_reply("Agreed", {"action": "approve", "version": 1})
        a = Scripted(approval, (failure_gate, AdapterError("Malformed transport")))
        b = Scripted((approval_gate, approval))
        config = replace(
            demo_config(),
            workspace=self.path,
            interaction_mode="chatroom",
            turn_delay=0,
            agents=(AgentConfig("a", "mock"), AgentConfig("b", "mock")),
        )
        room = self.start({"a": a, "b": b}, config)
        _, proposal = parse_action(
            MockAdapter(config.agents[0], self.path).workflow_reply(room.workflow.snapshot())
        )
        room.workflow.apply("a", proposal)
        room.publish_system("A proposed the fixture plan")
        # Proposing already approves, so wait on a's scripted turn rather than its vote.
        await self.until(lambda: a.calls and not room.members["a"].active)
        room.say("human", "Check another edge case before confirming consensus")
        await self.until(lambda: len(a.calls) == 2 and b.calls)
        approval_gate.set()
        await self.until(lambda: set(room.workflow.data["approvals"]) == {"a", "b"})
        failure_gate.set()
        await self.until(lambda: room.reason == "error" and not room.active)
        self.assertTrue(room.manual_paused)
        self.assertEqual(room.workflow.phase, "discussion")
        self.assertEqual(set(room.workflow.data["approvals"]), {"a", "b"})
        self.assertEqual(room.workflow.data["consensus_history"], [])
        self.assertFalse(list(self.path.glob("docs/agent-team/*/consensus-*.md")))
        self.assertFalse(
            any(e["type"] == "turn.started" and e["phase"] == "implementation" for e in self.events)
        )

    async def test_conversation_continues_while_a_writer_is_working(self):
        gate = asyncio.Event()
        a, b = Scripted((gate, "unfinished checkpoint")), Scripted("Consider an edge case")
        config = replace(
            demo_config(),
            workspace=self.path,
            interaction_mode="chatroom",
            turn_delay=0,
            agents=(AgentConfig("a", "mock"), AgentConfig("b", "mock")),
        )
        room = self.start({"a": a, "b": b}, config)
        _, proposal = parse_action(
            MockAdapter(config.agents[0], self.path).workflow_reply(room.workflow.snapshot())
        )
        room.workflow.apply("a", proposal)
        room.workflow.apply("a", {"action": "approve", "version": 1})
        room.workflow.apply("b", {"action": "approve", "version": 1})
        room.publish_system("The fixture plan is ready for implementation")
        await self.until(lambda: any(m["text"] == "Consider an edge case" for m in room.messages))
        self.assertEqual(room.writer, "a")
        self.assertEqual(a.calls[0]["phase"], "implementation")
        self.assertEqual(b.calls[0]["phase"], "chat")
        self.assertTrue(room.members["a"].active)
        self.assertEqual(a.cancelled, 0)
        room.control("interrupt")
        await self.until(lambda: not room.active)

    async def test_long_member_delay_does_not_delay_server_shutdown(self):
        config = TeamConfig(
            agents=(AgentConfig("a", "mock"),),
            workspace=self.path,
            workflow="discussion",
            interaction_mode="chatroom",
            turn_delay=3600,
        )
        room = self.start({"a": Scripted("A contribution")}, config)
        await self.until(lambda: room.turns == 1)
        await asyncio.wait_for(room.close(), 1)
        self.assertTrue(room.closed)

    async def test_actions_written_as_prose_are_corrected_then_escalated(self):
        """Reproduces the live deadlock: a member votes in plain text, so nothing counts,
        while its peer reads the vote in that text and believes consensus was reached."""
        prose_vote = 'I agree. {"action":"approve","version":1}'
        a = Scripted(*([prose_vote] * PROTOCOL_LAPSE_LIMIT), "[[PASS]]")
        b = Scripted("[[PASS]]", "[[PASS]]", "[[PASS]]")
        config = replace(
            demo_config(),
            workspace=self.path,
            interaction_mode="chatroom",
            turn_delay=0,
            agents=(AgentConfig("a", "mock"), AgentConfig("b", "mock")),
        )
        room = self.start({"a": a, "b": b}, config)
        _, proposal = parse_action(
            MockAdapter(config.agents[0], self.path).workflow_reply(room.workflow.snapshot())
        )
        room.workflow.apply("b", proposal)
        room.publish_system("b proposed the fixture plan")

        await self.until(lambda: room.protocol_lapses.get("a", 0) >= 1)
        # The vote never counted, so consensus cannot have been recorded.
        self.assertNotIn("a", room.workflow.data["approvals"])
        self.assertTrue(
            any("not counted" in m["text"] for m in room.messages),
            "the room must say the action did not count",
        )

        # Repeated lapses are not mistakes to correct but a member that cannot vote.
        await self.until(lambda: room.reason == "protocol")
        self.assertTrue(room.manual_paused)
        self.assertEqual(room.room_state(), "paused_for_input")
        await room.close()

    async def test_a_valid_action_clears_an_earlier_lapse(self):
        room = self.start({"a": Scripted("[[PASS]]"), "b": Scripted("[[PASS]]")})
        room.protocol_lapses["a"] = 1
        room.note_protocol_lapse("a", "fine by me", {"action": "approve", "version": 1})
        self.assertNotIn("a", room.protocol_lapses)
        await room.close()

    async def test_a_malformed_action_is_corrected_once_then_escalates(self):
        """Three live runs died on one bad field — a trailing remark, an empty evidence
        string, an unterminated block — each the author's to fix, each ending the run.
        The end-to-end path is covered by test_sessions; this pins the policy."""
        room = self.start({"a": Scripted("[[PASS]]"), "b": Scripted("[[PASS]]")})

        for _ in range(PROTOCOL_LAPSE_LIMIT - 1):
            self.assertFalse(room.note_invalid_action("a", "evidence must be nonempty text"))
        self.assertEqual(room.protocol_lapses["a"], PROTOCOL_LAPSE_LIMIT - 1)
        self.assertTrue(
            any("was not accepted" in m["text"] for m in room.messages),
            "the author must be told what to send instead",
        )
        self.assertNotEqual(room.reason, "error")

        # A member that cannot produce a valid action is not a slip to forgive forever.
        self.assertTrue(room.note_invalid_action("a", "evidence must be nonempty text"))

        # A valid action clears the count, so occasional slips never accumulate.
        room.note_protocol_lapse("a", "fine", {"action": "approve", "version": 1})
        self.assertNotIn("a", room.protocol_lapses)
        await room.close()
