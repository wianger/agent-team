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

    async def test_member_failure_is_isolated_and_retry_is_explicit(self):
        a, b = Scripted(AdapterError("Quota exhausted"), "Recovered"), Scripted("Still here")
        room = self.start({"a": a, "b": b})
        await self.until(lambda: room.reason == "degraded" and not room.active)
        self.assertFalse(room.manual_paused)
        self.assertIn("Quota exhausted", room.status()["runtimes"]["a"]["error"])
        self.assertTrue(any(m["text"] == "Still here" for m in room.messages))
        self.assertEqual(len(a.calls), 1)
        room.control("retry", "a")
        await self.until(lambda: room.reason == "waiting_messages")
        self.assertTrue(any(m["text"] == "Recovered" for m in room.messages))

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
        self.assertEqual([r["exit_code"] for r in final["checks_result"]], [0])
        for task in final["proposal"]["tasks"]:
            point = task["contributions"][-1]
            self.assertEqual(
                {j["speaker"] for j in point["judgments"]}, set(adapters) - {point["author"]}
            )
        active = {}
        concurrent = False
        for event in self.events:
            if event["type"] == "turn.started":
                active[event["turn_id"]] = event
                work = [e for e in active.values() if e["lane"] == "work"]
                writers = [e for e in work if e["phase"] in {"implementation", "verification"}]
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
