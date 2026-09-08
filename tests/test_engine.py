from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from agent_team.config import AgentConfig, TeamConfig
from agent_team.context import PASS
from agent_team.engine import Room
from agent_team.store import Store


async def eventually(predicate, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


class ScriptedAdapter:
    def __init__(self, replies=None, block=False, resist_cancel=False):
        self.replies = list(replies or ["reply"])
        self.prompts = []
        self.block = block
        self.resist_cancel = resist_cancel
        self.started = asyncio.Event()
        self.cancelled = False

    async def stream(self, prompt, *, phase="discussion"):
        self.prompts.append(prompt)
        self.started.set()
        if self.block:
            yield "incomplete"
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                if not self.resist_cancel:
                    raise
        yield self.replies.pop(0) if self.replies else PASS


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "events.sqlite3")
        self.events = []
        self.room = None

    def make_room(self, adapters, **kwargs):
        config = TeamConfig(
            workflow="discussion",
            agents=tuple(AgentConfig(name, "mock") for name in adapters),
            turn_delay=0,
            **kwargs,
        )
        self.room = Room(config, self.store, self.events.append, adapters)
        self.room.start()
        return self.room

    async def asyncTearDown(self):
        if self.room and not self.room.closed:
            await self.room.close()
        self.store.close()
        self.temp.cleanup()

    async def test_round_robin_shares_every_committed_message(self):
        a, b = ScriptedAdapter(["A contributes"]), ScriptedAdapter(["B builds on A"])
        room = self.make_room({"a": a, "b": b})
        room.say("human", "shared topic")
        await eventually(lambda: room.reason == "all_passed")
        self.assertEqual([m["speaker"] for m in room.messages], ["human", "a", "b"])
        self.assertIn("shared topic", a.prompts[0])
        self.assertIn("A contributes", b.prompts[0])
        self.assertIn("shared topic", b.prompts[0])
        active = None
        for event in self.events:
            if event["type"] == "floor.granted":
                self.assertIsNone(active)
                active = event["turn_id"]
            elif event["type"] == "floor.released":
                self.assertEqual(active, event["turn_id"])
                active = None
        self.assertIsNone(active)

    async def test_user_steering_revokes_stale_reply_even_if_adapter_ignores_cancel(self):
        a = ScriptedAdapter(["stale"], block=True, resist_cancel=True)
        room = self.make_room({"a": a})
        room.say("human", "old topic")
        await a.started.wait()
        room.say("human", "new direction")
        a.block = False
        a.replies = ["stale", "fresh"]
        await eventually(lambda: room.reason == "all_passed")
        self.assertEqual(
            [m["text"] for m in room.messages], ["old topic", "new direction", "fresh"]
        )
        self.assertIn("new direction", a.prompts[-1])
        self.assertNotIn("incomplete", a.prompts[-1])
        self.assertTrue(a.cancelled)

    async def test_pause_allows_current_turn_to_complete_but_no_next_turn(self):
        release = asyncio.Event()

        class Controlled:
            async def stream(self, prompt, *, phase="discussion"):
                await release.wait()
                yield "complete"

        room = self.make_room({"a": Controlled(), "b": ScriptedAdapter()})
        room.say("human", "topic")
        await eventually(lambda: room.active is not None)
        room.control("pause")
        release.set()
        await eventually(lambda: room.active is None)
        self.assertTrue(room.manual_paused)
        self.assertEqual([m["speaker"] for m in room.messages], ["human", "a"])

    async def test_interrupt_discards_partial_and_new_message_does_not_unpause(self):
        a = ScriptedAdapter(block=True)
        room = self.make_room({"a": a})
        room.say("human", "topic")
        await a.started.wait()
        room.control("interrupt")
        room.say("human", "another note")
        await eventually(lambda: room.active is None)
        self.assertEqual(len(room.messages), 2)
        self.assertTrue(room.manual_paused)
        self.assertTrue(a.cancelled)

    async def test_all_passed_stops_without_adding_pass_to_context(self):
        room = self.make_room({"a": ScriptedAdapter([PASS]), "b": ScriptedAdapter([PASS])})
        room.say("human", "topic")
        await eventually(lambda: room.reason == "all_passed")
        self.assertEqual(len(room.messages), 1)
        self.assertTrue(room.manual_paused)

    async def test_timeout_releases_floor_and_pauses(self):
        a = ScriptedAdapter(block=True)
        room = self.make_room({"a": a}, turn_timeout=0.1)
        room.say("human", "topic")
        await eventually(lambda: room.reason == "error" and room.active is None)
        self.assertTrue(a.cancelled)
        self.assertTrue(room.manual_paused)
        self.assertEqual(len(room.messages), 1)

    async def test_next_targets_only_one_agent_and_invalid_target_does_not_mutate(self):
        a, b = ScriptedAdapter(), ScriptedAdapter()
        room = self.make_room({"a": a, "b": b})
        room.control("pause")
        room.say("human", "topic")
        before = room.status()
        with self.assertRaises(ValueError):
            room.control("next", "missing")
        self.assertEqual(room.status(), before)
        room.control("next", "b")
        await eventually(lambda: room.reason == "step_complete")
        self.assertEqual(len(a.prompts), 0)
        self.assertEqual(len(b.prompts), 1)

    async def test_long_context_and_output_are_preserved_without_truncation(self):
        reply = "reply-start " + "y" * 250_000 + " reply-end"
        topic = "topic-start " + "x" * 250_000 + " topic-end"
        a = ScriptedAdapter([reply])
        room = self.make_room({"a": a})
        room.say("human", topic)
        await eventually(lambda: room.reason == "all_passed")
        self.assertIn(topic, a.prompts[0])
        self.assertIn(reply, a.prompts[1])
        self.assertEqual(room.messages[0]["text"], topic)
        self.assertEqual(room.messages[1]["text"], reply)

    async def test_discussion_continues_past_former_round_budgets(self):
        a = ScriptedAdapter([f"contribution {i}" for i in range(65)])
        room = self.make_room({"a": a})
        room.say("human", "Keep discussing")
        await eventually(lambda: room.reason == "all_passed")
        self.assertEqual(len(room.messages), 66)
        self.assertEqual(room.turns, 66)
        self.assertNotIn("remaining", room.status())

    async def test_restart_recovers_history_but_waits_for_resume(self):
        room = self.make_room({"a": ScriptedAdapter()})
        room.say("human", "remember this")
        await eventually(lambda: room.reason == "all_passed")
        await room.close()
        recovered = ScriptedAdapter()
        room = self.make_room({"a": recovered})
        self.assertTrue(room.manual_paused)
        self.assertEqual(room.reason, "restart")
        room.control("resume")
        await eventually(lambda: room.reason == "all_passed")
        self.assertIn("remember this", recovered.prompts[0])
        self.assertEqual(len(room.messages), 3)
