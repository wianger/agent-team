from __future__ import annotations

import asyncio
import tempfile
import unittest
import uuid
from pathlib import Path

from test_chatroom import transcript

from agent_team.adapters import QuotaExceeded, SessionUnavailable
from agent_team.chatroom import ChatRoom
from agent_team.config import AgentConfig, TeamConfig
from agent_team.context import DELTA_MARKER, PASS, TRANSCRIPT_MARKER
from agent_team.engine import Room
from agent_team.store import Store


class Recoverable:
    supports_sessions = True
    supports_session_notifications = True

    def __init__(self):
        self.calls, self.replies, self.history = [], [], {}
        self.result_session_id = None
        self.partial_failure = False

    async def stream(
        self, prompt, *, phase, persist_session=True, session_id=None, on_session=None
    ):
        self.result_session_id = None
        self.calls.append(
            {
                "prompt": prompt,
                "phase": phase,
                "session_id": session_id,
                "persistent": persist_session,
                "observed": on_session is not None,
            }
        )
        if session_id and session_id not in self.history:
            raise SessionUnavailable("No session found")
        identifier = session_id or str(uuid.uuid4())
        self.history.setdefault(identifier, []).append(prompt)
        if on_session:
            on_session(identifier)
        reply = self.replies.pop(0) if self.replies else PASS
        if isinstance(reply, tuple):
            gate, reply = reply
            try:
                await gate.wait()
            except asyncio.CancelledError:
                # A late response must remain fenced even when a backend ignores cancellation.
                pass
        if self.partial_failure:
            yield "partial output"
            raise SessionUnavailable("No session found")
        if isinstance(reply, Exception):
            raise reply
        yield reply
        self.result_session_id = identifier


class SessionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.rooms, self.stores, self.events = [], [], []

    async def asyncTearDown(self):
        for room in self.rooms:
            if not room.closed:
                await room.close()
        for store in self.stores:
            store.close()
        self.temp.cleanup()

    def make(self, mode, *, store=None, adapters=None):
        if store is None:
            store = Store(Path(self.temp.name) / f"events-{len(self.stores)}.sqlite3")
            self.stores.append(store)
        config = TeamConfig(
            workspace=Path(self.temp.name),
            workflow="discussion",
            interaction_mode=mode,
            context_mode="session",
            permission_mode="full_auto",
            turn_delay=0,
            agents=(AgentConfig("a", "claude"), AgentConfig("b", "codex")),
        )
        cls = ChatRoom if mode == "chatroom" else Room
        room = cls(config, store, self.events.append, adapters or {n: Recoverable() for n in "ab"})
        self.rooms.append(room)
        room.start()
        if not room.messages:
            room.control("pause")
            room.say("human", "Original idea")
        return room

    async def until(self, predicate):
        async with asyncio.timeout(3):
            while not predicate():
                for room in self.rooms:
                    if room.runner.done():
                        room.runner.result()
                await asyncio.sleep(0.002)

    async def step(self, room, name):
        room.control("next", name)
        await self.until(lambda: room.manual_paused and not room.active)

    async def seed(self, room):
        for name in "ab":
            room.adapters[name].replies = ["Published by " + name]
            await self.step(room, name)
        return room.store.sessions()

    async def test_quota_probe_preserves_working_session_and_resume_sends_only_missing_messages(
        self,
    ):
        for mode in ("chatroom", "serial"):
            for restart in (False, True):
                with self.subTest(mode=mode, restart=restart):
                    room = self.make(mode)
                    saved = await self.seed(room)
                    gate = asyncio.Event()
                    a = room.adapters["a"]
                    a.replies = [QuotaExceeded("Usage exhausted"), (gate, PASS)]
                    await self.step(room, "a")
                    suspended = room.store.sessions()["a"].copy()
                    self.assertEqual(suspended["session_id"], saved["a"]["session_id"])
                    self.assertEqual(suspended["synced_through"], saved["a"]["synced_through"])
                    self.assertTrue(suspended["dirty"])
                    if restart:
                        await room.close()
                        room = self.make(mode, store=room.store, adapters=room.adapters)
                    room.say("human", "Guidance received while unavailable")
                    room.control("resume")
                    await self.until(lambda a=a: len(a.calls) == 3)
                    self.assertEqual(a.calls[-1]["phase"], "recovery")
                    self.assertFalse(a.calls[-1]["persistent"])
                    self.assertFalse(a.calls[-1]["observed"])
                    self.assertEqual(room.store.sessions()["a"], suspended)
                    gate.set()
                    await self.until(
                        lambda room=room, a=a: (
                            len(a.calls) > 3 and not room.quotas and not room.active
                        )
                    )
                    call = a.calls[3]
                    self.assertEqual(call["session_id"], saved["a"]["session_id"])
                    self.assertIn(DELTA_MARKER, call["prompt"])
                    self.assertIn("Session recovery:", call["prompt"])
                    texts = [m["text"] for m in transcript(call["prompt"])]
                    self.assertNotIn("Original idea", texts)
                    self.assertNotIn("Published by a", texts)
                    self.assertIn("Published by b", texts)
                    self.assertIn("Guidance received while unavailable", texts)
                    self.assertEqual(
                        room.store.sessions()["a"]["generation"], saved["a"]["generation"]
                    )
                    self.assertFalse(room.store.sessions()["a"]["dirty"])
                    await room.close()

    async def test_first_turn_quota_keeps_the_early_native_session_id(self):
        for mode in ("chatroom", "serial"):
            room = self.make(mode)
            a = room.adapters["a"]
            a.replies = [QuotaExceeded("Usage exhausted"), PASS]
            await self.step(room, "a")
            saved = room.store.sessions()["a"]
            self.assertIsNotNone(saved["session_id"])
            self.assertEqual(saved["synced_through"], 0)
            room.control("resume")
            await self.until(
                lambda room=room, a=a: len(a.calls) > 2 and not room.quotas and not room.active
            )
            self.assertEqual(a.calls[2]["session_id"], saved["session_id"])
            self.assertEqual(room.store.sessions()["a"]["generation"], saved["generation"])
            await room.close()

    async def test_failed_quota_probe_never_overwrites_the_suspended_session(self):
        for mode in ("chatroom", "serial"):
            room = self.make(mode)
            saved = await self.seed(room)
            a = room.adapters["a"]
            a.replies = [QuotaExceeded("Usage exhausted"), QuotaExceeded("Still unavailable")]
            await self.step(room, "a")
            suspended = room.store.sessions()["a"].copy()
            room.control("resume")
            await self.until(lambda room=room, a=a: len(a.calls) == 3 and not room.active)
            self.assertTrue(room.quotas)
            self.assertEqual(room.store.sessions()["a"], suspended)
            room.control("resume")
            await self.until(
                lambda room=room, a=a: len(a.calls) > 4 and not room.quotas and not room.active
            )
            self.assertEqual(a.calls[4]["session_id"], saved["a"]["session_id"])
            await room.close()

    async def test_cancelled_peer_resumes_without_losing_or_acknowledging_uncommitted_input(self):
        room = self.make("chatroom")
        saved = await self.seed(room)
        fail, peer = asyncio.Event(), asyncio.Event()
        a, b = room.adapters.values()
        a.replies = [(fail, QuotaExceeded("Usage exhausted")), PASS]
        b.replies = [(peer, "Stale peer response")]
        room.say("human", "Input before interruption")
        room.control("resume")
        await self.until(lambda: len(a.calls) == len(b.calls) == 2)
        fail.set()
        await self.until(lambda: bool(room.quotas) and not room.active)
        for name in "ab":
            current = room.store.sessions()[name]
            self.assertEqual(current["session_id"], saved[name]["session_id"])
            self.assertEqual(current["synced_through"], saved[name]["synced_through"])
        room.say("human", "Input after interruption")
        room.control("resume")
        await self.until(lambda: len(b.calls) > 2 and not room.quotas and not room.active)
        call = b.calls[2]
        self.assertEqual(call["session_id"], saved["b"]["session_id"])
        self.assertIn("Session recovery:", call["prompt"])
        texts = [m["text"] for m in transcript(call["prompt"])]
        self.assertIn("Input before interruption", texts)
        self.assertIn("Input after interruption", texts)
        self.assertNotIn("Original idea", texts)
        self.assertNotIn("Stale peer response", [m["text"] for m in room.messages])

    async def test_running_resume_is_idempotent_and_does_not_wake_a_passed_peer(self):
        for mode in ("chatroom", "serial"):
            room = self.make(mode)
            await self.seed(room)
            a, b = room.adapters.values()
            gate = asyncio.Event()
            a.replies = [(gate, PASS)]
            room.control("resume")
            await self.until(lambda a=a: len(a.calls) == 2)
            await asyncio.sleep(0.02)
            before = room.store.sessions()
            events = len(self.events)
            counts = len(a.calls), len(b.calls)
            for _ in range(3):
                room.control("resume")
            await asyncio.sleep(0.02)
            self.assertEqual(len(self.events), events)
            self.assertEqual((len(a.calls), len(b.calls)), counts)
            self.assertEqual(room.store.sessions(), before)
            gate.set()
            await self.until(lambda room=room: not room.active)
            if mode == "chatroom":
                counts = len(a.calls), len(b.calls)
                room.control("resume")
                await asyncio.sleep(0.02)
                self.assertEqual((len(a.calls), len(b.calls)), counts)
            await room.close()

    async def test_chatroom_missing_session_rebuilds_once_but_partial_failures_do_not(self):
        room = self.make("chatroom")
        saved = await self.seed(room)
        a = room.adapters["a"]
        a.history.clear()
        await self.step(room, "a")
        self.assertEqual(len(a.calls), 3)
        self.assertEqual(a.calls[1]["session_id"], saved["a"]["session_id"])
        self.assertIsNone(a.calls[2]["session_id"])
        self.assertIn(TRANSCRIPT_MARKER, a.calls[2]["prompt"])
        self.assertEqual(sum(e["type"] == "session.rebuilt" for e in self.events), 1)
        self.assertNotEqual(room.store.sessions()["a"]["session_id"], saved["a"]["session_id"])
        a.partial_failure = True
        await self.step(room, "a")
        self.assertEqual(len(a.calls), 4)
        self.assertEqual(room.reason, "error")
        self.assertEqual(sum(e["type"] == "session.rebuilt" for e in self.events), 1)
        self.assertNotIn("partial output", [m["text"] for m in room.messages])

    async def test_suspended_turn_cannot_acknowledge_its_cursor_later(self):
        room = self.make("serial")
        saved = await self.seed(room)
        sessions = room.sessions
        sessions.begin("a", saved["a"], "old-turn", room.messages[-1]["id"])
        sessions.suspend("a", "Interrupted")
        with self.assertRaisesRegex(ValueError, "revoked invocation"):
            sessions.completed("a", "old-turn", saved["a"]["session_id"])
        self.assertEqual(room.store.sessions()["a"]["synced_through"], saved["a"]["synced_through"])
