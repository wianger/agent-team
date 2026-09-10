from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agent_team.config import AgentConfig, TeamConfig, demo_config
from agent_team.server import Server, connect, encode, receive


class GatedAdapter:
    """Hold the first turn until the test has disconnected every human."""

    def __init__(self, inner):
        self.inner = inner
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def stream(self, prompt, *, phase="discussion"):
        if not self.started.is_set():
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        async for delta in self.inner.stream(prompt, phase=phase):
            yield delta


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.config = TeamConfig(
            workflow="discussion", agents=(AgentConfig("mock", "mock"),), turn_delay=0
        )
        self.server = Server(self.config, self.path)
        self.writers = []
        await self.server.start()

    async def asyncTearDown(self):
        for writer in self.writers:
            writer.close()
            await writer.wait_closed()
        await self.server.close()
        self.temp.cleanup()

    async def join(self, name):
        reader, writer = await connect(self.path, name)
        self.writers.append(writer)
        return reader, writer

    async def until(self, reader, predicate):
        async with asyncio.timeout(5):
            while True:
                event = await receive(reader)
                if predicate(event):
                    return event

    async def wait_until(self, predicate, timeout=5):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.005)

    async def start_gated_turn(self):
        name = self.server.config.agents[0].name
        adapter = GatedAdapter(self.server.room.adapters[name])
        self.server.room.adapters[name] = adapter
        reader, writer = await self.join("alice")
        self.assertEqual((await receive(reader))["type"], "welcome")
        writer.write(encode({"type": "say", "text": "Build a greeting function and guide"}))
        await writer.drain()
        await asyncio.wait_for(adapter.started.wait(), 5)
        return reader, writer, adapter

    async def disconnect_last_client(self, writer):
        writer.close()
        await writer.wait_closed()
        await self.wait_until(lambda: not self.server.clients)

    async def test_two_clients_receive_identical_committed_messages(self):
        ar, aw = await self.join("alice")
        br, _ = await self.join("bob")
        self.assertEqual((await receive(ar))["type"], "welcome")
        self.assertEqual((await receive(br))["type"], "welcome")
        aw.write(encode({"type": "say", "text": "shared topic"}))
        await aw.drain()
        user_a = await self.until(ar, lambda e: e["type"] == "message")
        user_b = await self.until(br, lambda e: e["type"] == "message")
        self.assertEqual(user_a, user_b)
        agent_a = await self.until(ar, lambda e: e["type"] == "message" and e["role"] == "agent")
        agent_b = await self.until(br, lambda e: e["type"] == "message" and e["role"] == "agent")
        self.assertEqual(agent_a, agent_b)

    async def test_simultaneous_user_messages_are_both_committed_and_seen(self):
        ar, aw = await self.join("alice")
        br, bw = await self.join("bob")
        await receive(ar)
        await receive(br)
        aw.write(encode({"type": "say", "text": "first"}))
        bw.write(encode({"type": "say", "text": "second"}))
        await asyncio.gather(aw.drain(), bw.drain())
        await self.until(ar, lambda e: e["type"] == "message" and e["role"] == "agent")
        humans = [m for m in self.server.room.messages if m["role"] == "user"]
        self.assertEqual({m["text"] for m in humans}, {"first", "second"})
        self.assertLess(humans[0]["id"], humans[1]["id"])

    async def test_auth_and_duplicate_names_and_agent_impersonation_rejected(self):
        reader, _ = await self.join("alice")
        await receive(reader)
        for name in ("alice", "mock"):
            rejected, _ = await self.join(name)
            self.assertEqual((await receive(rejected))["type"], "error")
        info = json.loads((self.path / "connection.json").read_text())
        reader, writer = await asyncio.open_connection(info["host"], info["port"])
        self.writers.append(writer)
        writer.write(encode({"type": "join", "name": "eve", "token": "wrong"}))
        await writer.drain()
        self.assertEqual((await receive(reader))["text"], "Authentication failed")

    async def test_single_server_lock_and_clean_restart(self):
        duplicate = Server(self.config, self.path)
        with self.assertRaisesRegex(ValueError, "already running"):
            await duplicate.start()
        self.assertTrue((self.path / "connection.json").exists())
        await self.server.close()
        self.assertFalse((self.path / "connection.json").exists())
        self.server = Server(self.config, self.path)
        await self.server.start()

    async def test_disconnect_last_user_preserves_turn_and_replays_offline_progress(self):
        _, writer, adapter = await self.start_gated_turn()
        room = self.server.room
        active, revision = dict(room.active), room.revision
        await self.disconnect_last_client(writer)
        self.assertFalse(room.manual_paused)
        self.assertEqual(room.active, active)
        self.assertEqual(room.revision, revision)
        self.assertFalse(adapter.cancelled)

        adapter.release.set()
        await self.wait_until(lambda: room.reason == "all_passed" and room.active is None)
        self.assertFalse(self.server.clients)
        self.assertEqual(room.turns, 2)  # A reply, then a subsequent turn yielding the floor.
        self.assertEqual(len(room.messages), 2)
        self.assertEqual(self.server.store.messages(), room.messages)
        self.assertFalse(any(e["type"] == "room.control" for e in self.server.store.events()))

        reader, _ = await self.join("alice")
        welcome = await receive(reader)
        self.assertEqual(welcome["state"], room.status())
        replay = [await receive(reader) for _ in room.messages]
        self.assertEqual(replay, [{**m, "replay": True} for m in room.messages])

    async def test_manual_pause_survives_disconnect_and_reconnect(self):
        reader, writer, adapter = await self.start_gated_turn()
        writer.write(encode({"type": "control", "action": "pause"}))
        await writer.drain()
        await self.until(reader, lambda e: e["type"] == "state" and e["paused"])
        await self.disconnect_last_client(writer)
        self.assertFalse(adapter.cancelled)
        adapter.release.set()
        room = self.server.room
        await self.wait_until(lambda: room.active is None)
        self.assertEqual(room.turns, 1)
        self.assertEqual(len(room.messages), 2)
        reader, _ = await self.join("alice")
        state = (await receive(reader))["state"]
        self.assertTrue(state["paused"])
        self.assertEqual(state["reason"], "user")
        controls = [e["action"] for e in self.server.store.events() if e["type"] == "room.control"]
        self.assertEqual(controls, ["pause"])

    async def test_reconnected_user_can_explicitly_interrupt_offline_turn(self):
        _, writer, adapter = await self.start_gated_turn()
        active = dict(self.server.room.active)
        await self.disconnect_last_client(writer)
        reader, writer = await self.join("alice")
        state = (await receive(reader))["state"]
        self.assertFalse(state["paused"])
        self.assertEqual(state["active"], active)
        writer.write(encode({"type": "control", "action": "interrupt"}))
        await writer.drain()
        await self.wait_until(lambda: self.server.room.active is None)
        self.assertTrue(adapter.cancelled)
        self.assertTrue(self.server.room.manual_paused)
        self.assertEqual(self.server.room.reason, "user")
        self.assertEqual(len(self.server.room.messages), 1)

    async def test_build_reaches_consensus_peer_judgment_and_acceptance_without_humans(self):
        await self.server.close()
        config = replace(demo_config(), workspace=self.path, turn_delay=0)
        self.server = Server(config, self.path)
        await self.server.start()
        _, writer, adapter = await self.start_gated_turn()
        await self.disconnect_last_client(writer)
        self.assertFalse(adapter.cancelled)
        adapter.release.set()
        room = self.server.room
        await self.wait_until(lambda: room.reason == "completed" and room.active is None, 10)
        self.assertFalse(self.server.clients)
        events = self.server.store.events()
        self.assertEqual(
            {e["phase"] for e in events if e["type"] == "floor.granted"},
            {"discussion", "implementation", "judging", "review", "acceptance"},
        )
        self.assertFalse(any(e["type"] == "room.control" for e in events))
        final = self.server.store.messages()[-1]["workflow"]
        self.assertEqual(final["phase"], "completed")
        self.assertEqual([r["exit_code"] for r in final["acceptance_results"]], [0])
        self.assertTrue((self.path / "hello.py").is_file())
        self.assertTrue((self.path / "HOWTO.md").is_file())

        reader, _ = await self.join("alice")
        self.assertEqual((await receive(reader))["state"]["workflow"], final)
        replay = await self.until(
            reader,
            lambda e: e["type"] == "message" and e.get("workflow", {}).get("phase") == "completed",
        )
        self.assertTrue(replay["replay"])
        self.assertEqual(replay["workflow"], final)

    async def test_bad_request_does_not_break_session_and_history_replays(self):
        reader, writer = await self.join("alice")
        await receive(reader)
        writer.write(encode({"type": "say", "text": []}))
        writer.write(encode({"type": "control", "action": "pause"}))
        writer.write(encode({"type": "say", "text": "retained"}))
        await writer.drain()
        await self.until(reader, lambda e: e["type"] == "error")
        message = await self.until(reader, lambda e: e["type"] == "message")
        writer.write(encode({"type": "history"}))
        await writer.drain()
        replay = await self.until(reader, lambda e: e["type"] == "message" and e.get("replay"))
        self.assertEqual(replay["id"], message["id"])

    async def test_chatroom_completes_offline_and_replays_after_reconnect(self):
        await self.server.close()
        config = replace(
            demo_config(), workspace=self.path, interaction_mode="chatroom", turn_delay=0
        )
        self.server = Server(config, self.path)
        await self.server.start()
        reader, writer, adapter = await self.start_gated_turn()
        writer.write(encode({"type": "say", "text": "A noninterrupting opinion"}))
        await writer.drain()
        await self.until(
            reader, lambda e: e["type"] == "message" and e["text"] == "A noninterrupting opinion"
        )
        await self.disconnect_last_client(writer)
        self.assertFalse(adapter.cancelled)
        adapter.release.set()
        await self.wait_until(
            lambda: self.server.room.reason == "completed" and not self.server.room.active, 10
        )
        self.assertFalse(self.server.clients)
        reader, writer = await self.join("alice")
        welcome = await receive(reader)
        self.assertEqual(welcome["state"]["interaction_mode"], "chatroom")
        self.assertEqual(welcome["state"]["workflow"]["acceptance_results"][0]["exit_code"], 0)
        replay = await self.until(
            reader,
            lambda e: e["type"] == "message" and e.get("workflow", {}).get("phase") == "completed",
        )
        self.assertTrue(replay["replay"])
        writer.write(encode({"type": "redirect", "text": "Reconsider the interface"}))
        await writer.drain()
        directed = await self.until(reader, lambda e: e["type"] == "message" and e.get("redirect"))
        self.assertEqual(directed["workflow"]["phase"], "discussion")

    async def test_message_larger_than_old_wire_cap_round_trips(self):
        reader, writer = await self.join("alice")
        await receive(reader)
        self.server.room.control("pause")
        text = "start " + "x" * (9 * 1024 * 1024) + " end"
        writer.write(encode({"type": "say", "text": text}))
        await writer.drain()
        message = await self.until(reader, lambda e: e["type"] == "message")
        self.assertEqual(message["text"], text)
        self.assertEqual(self.server.store.messages()[-1]["text"], text)

    async def test_join_replays_complete_history_beyond_old_window_and_queue_size(self):
        self.server.room.control("pause")
        for i in range(650):
            self.server.room.say("historian", f"history entry {i}")
        reader, _ = await self.join("alice")
        self.assertEqual((await receive(reader))["type"], "welcome")
        messages = []
        async with asyncio.timeout(10):
            while len(messages) < 650:
                event = await receive(reader)
                if event["type"] == "message":
                    messages.append(event)
        self.assertTrue(all(m.get("replay") for m in messages))
        self.assertEqual([m["text"] for m in messages], [f"history entry {i}" for i in range(650)])

    async def test_session_status_and_reset_controls_round_trip(self):
        reader, writer = await self.join("alice")
        await receive(reader)
        writer.write(encode({"type": "sessions"}))
        await writer.drain()
        event = await self.until(reader, lambda e: e["type"] == "session.state")
        self.assertEqual(event["context_mode"], "session")
        self.assertEqual(event["sessions"], {})
        writer.write(encode({"type": "control", "action": "reset-session"}))
        await writer.drain()
        event = await self.until(reader, lambda e: e["type"] == "state")
        self.assertTrue(event["paused"])
