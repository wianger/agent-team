from __future__ import annotations

import asyncio
import copy
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agent_team.chatroom import ChatRoom, Turn
from agent_team.client import describe_workflow, parse_input
from agent_team.config import AgentConfig, TeamConfig, demo_config
from agent_team.consensus import document_path, render_consensus, write_consensus
from agent_team.context import PASS, chat_instructions
from agent_team.engine import Room
from agent_team.store import Store
from agent_team.workflow import Workflow, action_reply, workflow_instructions


def proposal(summary="Build a small shared module"):
    return {
        "action": "propose",
        "summary": summary,
        "acceptance": ["The agreed behavior is covered by tests"],
        "tasks": [
            {
                "id": "code",
                "title": "Shared code",
                "details": "Build and judge the module",
                "depends_on": [],
            }
        ],
        "checks": [["python3", "-m", "unittest", "discover", "-s", "tests"]],
    }


def agree(flow, summary="Build a small shared module"):
    flow.apply(flow.members[0], proposal(summary))
    for name in flow.members:
        flow.apply(name, {"action": "approve", "version": flow.data["version"]})


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.config = TeamConfig(
            agents=(AgentConfig("a", "mock"), AgentConfig("b", "mock")), workspace=self.workspace
        )
        self.flow = Workflow(self.config)

    def tearDown(self):
        self.temp.cleanup()

    def record(self):
        agree(self.flow)
        return self.flow.confirm_consensus()

    def write(self, record):
        return write_consensus(self.workspace, self.flow.data["document_namespace"], record)

    def test_only_explicit_unanimity_can_create_an_approved_record(self):
        self.flow.apply("a", proposal())
        self.flow.apply("a", {"action": "approve", "version": 1})
        with self.assertRaisesRegex(ValueError, "unanimous"):
            self.flow.confirm_consensus()
        self.flow.apply("b", {"action": "approve", "version": 1})
        record = self.flow.confirm_consensus()
        self.assertEqual(record["approvals"], ["a", "b"])
        self.assertEqual(self.flow.confirm_consensus(), record)
        self.assertEqual(len(self.flow.data["consensus_history"]), 1)
        self.assertEqual(record["proposal"]["summary"], proposal()["summary"])

    def test_revision_retains_documents_progress_and_requires_fresh_votes(self):
        first = self.record()
        file = self.write(first)
        original = file.read_bytes()
        artifact = self.workspace / "code.py"
        artifact.write_text("# A partial implementation\n")
        self.flow.apply(
            "a",
            {
                "action": "contribute",
                "version": 1,
                "task_id": "code",
                "ready": False,
                "summary": "Partial work",
                "files": ["code.py"],
                "tests": "Not run",
            },
        )
        self.flow.apply(
            "b", {"action": "request_revision", "version": 1, "reason": "Account for offline usage"}
        )
        self.assertEqual(self.flow.phase, "discussion")
        self.assertIsNone(self.flow.data["proposal"])
        self.assertEqual(self.flow.data["approvals"], [])
        base = self.flow.data["revision_base"]
        self.assertEqual(base["checkpoint"]["author"], "a")
        self.assertEqual(
            base["proposal"]["tasks"][0]["contributions"][0]["summary"], "Partial work"
        )
        with self.assertRaises(ValueError):
            self.flow.apply("a", {"action": "approve", "version": 1})
        agree(self.flow, "Build an offline-capable shared module")
        second = self.flow.confirm_consensus()
        revised = self.write(second)
        self.assertNotEqual(file, revised)
        self.assertEqual(file.read_bytes(), original)
        self.assertTrue(artifact.exists())
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["supersedes"], 1)
        self.assertIn("offline usage", revised.read_text())
        self.assertIn("consensus-v0001.md", revised.read_text())
        self.assertEqual(self.flow.data["proposal"]["tasks"][0]["status"], "pending")

    def test_stale_revision_request_does_not_partly_change_the_plan(self):
        self.record()
        before = self.flow.snapshot()
        with self.assertRaises(ValueError):
            self.flow.apply(
                "a", {"action": "request_revision", "version": 9, "reason": "Late request"}
            )
        self.assertEqual(self.flow.snapshot(), before)

    def test_generated_record_is_complete_and_idempotent(self):
        self.flow.reconsider(speaker="user", reason="Initial idea")
        record = self.record()
        self.assertIsNone(record["revision_request"])
        file = self.write(record)
        stamp = file.stat().st_mtime_ns
        self.assertEqual(file.read_text(), render_consensus(record))
        self.write(record)
        self.assertEqual(file.stat().st_mtime_ns, stamp)
        for text in (
            "Scope and approach",
            "Acceptance criteria",
            "Shared milestones",
            "Approved by: a, b",
            "unittest",
            "/revise",
        ):
            self.assertIn(text, file.read_text())

    def test_conflicting_document_and_special_files_are_never_overwritten(self):
        record = self.record()
        file = self.workspace / record["document"]
        file.parent.mkdir(parents=True)
        file.write_text("User-authored content")
        with self.assertRaisesRegex(ValueError, "will not be overwritten"):
            self.write(record)
        self.assertEqual(file.read_text(), "User-authored content")
        file.unlink()
        os.mkfifo(file)
        with self.assertRaises(ValueError):
            self.write(record)
        self.assertFalse(file.is_file())

    def test_symlinked_directory_or_file_cannot_redirect_writes(self):
        record = self.record()
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside)
            (self.workspace / "docs").symlink_to(external, target_is_directory=True)
            with self.assertRaises(OSError):
                self.write(record)
            self.assertFalse(list(external.iterdir()))
            (self.workspace / "docs").unlink()
            file = self.workspace / record["document"]
            file.parent.mkdir(parents=True)
            target = external / "keep.md"
            target.write_text("Keep this")
            file.symlink_to(target)
            with self.assertRaises(OSError):
                self.write(record)
            self.assertEqual(target.read_text(), "Keep this")

    def test_failed_atomic_publication_cleans_only_its_temporary_file(self):
        record = self.record()
        file = self.workspace / record["document"]
        with patch("agent_team.consensus.os.link", side_effect=OSError("Disk unavailable")):
            with self.assertRaises(OSError):
                self.write(record)
        self.assertFalse(file.exists())
        self.assertEqual(list(file.parent.iterdir()), [])
        self.assertEqual(self.flow.data["consensus_history"], [record])
        self.write(record)
        self.assertTrue(file.exists())

    def test_document_paths_cannot_escape_the_generated_namespace(self):
        for namespace, version in (("../outside", 1), ("a" * 32, -1), ("a" * 32, True)):
            with self.assertRaises(ValueError):
                document_path(namespace, version)
        record = self.record()
        record["document"] = "../outside.md"
        with self.assertRaises(ValueError):
            self.write(record)

    def test_commands_and_prompts_expose_revision_without_extra_write_authority(self):
        self.record()
        self.assertEqual(
            parse_input("/revise Keep the API\nbut change storage"),
            {"type": "redirect", "text": "Keep the API\nbut change storage"},
        )
        self.assertEqual(parse_input("/consensus"), {"type": "workflow"})
        for bad in ("/revise", "/consensus unexpected"):
            with self.assertRaises(ValueError):
                parse_input(bad)
        self.assertIn("request_revision", chat_instructions(self.flow))
        self.assertIn("Do not issue votes", chat_instructions(self.flow))
        self.assertIn("Do not edit generated consensus", workflow_instructions(self.flow, "a"))
        self.flow.reconsider(speaker="user", reason="Revise storage")
        self.assertIn("revision_base", workflow_instructions(self.flow, "a"))
        self.assertIn("Under discussion", describe_workflow(self.flow.snapshot()))


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = Store(self.path / "events.sqlite3")
        self.events = []
        self.room = None
        self.config = replace(
            demo_config(), workspace=self.path, turn_delay=0, interaction_mode="chatroom"
        )

    async def asyncTearDown(self):
        if self.room and not self.room.closed:
            await self.room.close()
        self.store.close()
        self.temp.cleanup()

    async def until(self, predicate):
        async with asyncio.timeout(6):
            while not predicate():
                if self.room.runner and self.room.runner.done():
                    self.room.runner.result()
                await asyncio.sleep(0.005)

    def start(self, room_type=ChatRoom):
        def observe(event):
            self.events.append(event)
            if (
                event["type"] in {"turn.started", "floor.granted"}
                and event["phase"] == "implementation"
                and event.get("lane", "work") == "work"
            ):
                record = self.room.workflow.data["consensus_history"][-1]
                self.assertTrue((self.path / record["document"]).is_file())
                committed = [
                    m
                    for m in self.store.messages()
                    if m.get("workflow", {}).get("consensus_history")
                ]
                self.assertTrue(committed)

        self.room = room_type(self.config, self.store, observe)
        self.room.start()
        self.room.say("human", "Build the greeting example together")
        return self.room

    async def test_chatroom_exports_before_writing_and_can_revise_after_completion(self):
        room = self.start()
        await self.until(lambda: room.reason == "completed" and not room.active)
        first = copy.deepcopy(room.workflow.data["consensus_history"][-1])
        file = self.path / first["document"]
        original = file.read_bytes()
        room.redirect("human", "Revisit the same example and recheck existing artifacts")
        self.assertEqual(room.workflow.data["consensus_history"], [first])
        self.assertEqual(room.workflow.data["revision_base"]["phase"], "completed")
        self.assertTrue((self.path / "hello.py").exists())
        await self.until(lambda: room.reason == "completed" and not room.active)
        history = room.workflow.data["consensus_history"]
        self.assertEqual([r["version"] for r in history], [1, 2])
        self.assertEqual(file.read_bytes(), original)
        self.assertTrue((self.path / history[1]["document"]).is_file())

    async def test_serial_scheduler_also_publishes_before_implementation(self):
        self.config = replace(self.config, interaction_mode="serial")
        room = self.start(Room)
        await self.until(lambda: room.reason == "completed" and not room.active)
        self.assertEqual(room.document_versions, {1})

    async def test_document_failure_pauses_without_replaying_the_approved_turn(self):
        with patch("agent_team.engine.write_consensus", side_effect=OSError("Read-only docs")):
            room = self.start()
            await self.until(lambda: room.reason == "document_error")
        self.assertEqual(len(room.workflow.data["consensus_history"]), 1)
        self.assertTrue(room.manual_paused)
        self.assertFalse((self.path / "hello.py").exists())
        record = copy.deepcopy(room.workflow.data["consensus_history"][-1])
        room.control("resume")
        await self.until(lambda: room.reason == "completed" and not room.active)
        self.assertEqual(room.workflow.data["consensus_history"], [record])
        self.assertEqual(room.document_versions, {1})
        self.assertIsNone(room.document_error)

    async def test_recovery_materializes_a_committed_but_missing_document_without_model_calls(self):
        with patch("agent_team.engine.write_consensus", side_effect=OSError("Unavailable")):
            room = self.start()
            await self.until(lambda: room.reason == "document_error")
        record = copy.deepcopy(room.workflow.data["consensus_history"][-1])
        await room.close()
        self.room = ChatRoom(self.config, self.store, self.events.append)
        self.room.start()
        self.assertTrue(self.room.manual_paused)
        self.assertEqual(self.room.turns, 0)
        self.assertEqual(self.room.workflow.data["consensus_history"], [record])
        self.assertEqual((self.path / record["document"]).read_text(), render_consensus(record))

    async def test_pause_allows_pending_consensus_to_be_documented_but_not_implemented(self):
        gate = asyncio.Event()
        entered = []

        class Approver:
            async def stream(inner, prompt, *, phase):
                entered.append(phase)
                await gate.wait()
                yield action_reply("Approved", {"action": "approve", "version": 1})

        flow = Workflow(self.config)
        flow.apply(flow.members[0], proposal())
        self.store.append(
            "message", role="system", speaker="system", text="Proposed", workflow=flow.snapshot()
        )
        self.room = ChatRoom(
            self.config, self.store, self.events.append, {name: Approver() for name in flow.members}
        )
        self.room.start()
        self.room.control("resume")
        await self.until(lambda: len(entered) == 2)
        self.room.control("pause")
        gate.set()
        await self.until(lambda: self.room.document_versions == {1})
        self.assertTrue(self.room.manual_paused)
        self.assertEqual(entered, ["planning", "planning"])
        self.assertEqual(self.room.workflow.phase, "implementation")
        self.assertIsNone(self.room.writer)
        self.assertTrue(
            (self.path / self.room.workflow.data["consensus_history"][0]["document"]).exists()
        )

    async def test_migrates_legacy_consensus_even_when_the_latest_state_is_discussion(self):
        flow = Workflow(self.config)

        def save_legacy(text):
            saved = flow.snapshot()
            for key in (
                "consensus_history",
                "document_namespace",
                "revision_base",
                "revision_request",
            ):
                saved.pop(key)
            self.store.append("message", role="system", speaker="system", text=text, workflow=saved)

        agree(flow, "First proposal")
        save_legacy("First proposal reached")
        flow.reconsider()
        agree(flow, "Second proposal")
        save_legacy("Second proposal reached")
        flow.reconsider()
        save_legacy("Reopen discussion")
        self.room = ChatRoom(self.config, self.store, self.events.append)
        self.room.start()
        history = copy.deepcopy(self.room.workflow.data["consensus_history"])
        self.assertEqual([r["version"] for r in history], [1, 2])
        self.assertTrue(all(r["recovered"] for r in history))
        self.assertEqual(
            self.room.workflow.data["revision_base"]["proposal"]["summary"], "Second proposal"
        )
        self.assertEqual(self.room.workflow.phase, "discussion")
        self.assertTrue(self.room.manual_paused)
        for record in history:
            self.assertTrue((self.path / record["document"]).exists())
        await self.room.close()
        count = len(self.store.messages())
        self.room = ChatRoom(self.config, self.store, self.events.append)
        self.room.start()
        self.assertEqual(self.room.workflow.data["consensus_history"], history)
        self.assertEqual(len(self.store.messages()), count)

    async def test_agent_revision_cancels_writer_and_waits_for_its_actual_cleanup(self):
        entered, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []

        class Writer:
            async def stream(inner, prompt, *, phase):
                calls.append(("a", phase))
                if phase == "implementation":
                    (self.path / "partial.py").write_text("# Preserve partial work\n")
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        cancelling.set()
                        await release.wait()
                    yield "Stale writer output"
                else:
                    yield PASS

        class Peer:
            async def stream(inner, prompt, *, phase):
                calls.append(("b", phase))
                if phase == "chat":
                    await entered.wait()
                    yield action_reply(
                        "We need to revisit storage",
                        {
                            "action": "request_revision",
                            "version": 1,
                            "reason": "The current approach loses offline updates",
                        },
                    )
                else:
                    yield PASS

        self.config = replace(
            self.config, agents=(AgentConfig("a", "mock"), AgentConfig("b", "mock"))
        )
        flow = Workflow(self.config)
        agree(flow)
        flow.confirm_consensus()
        self.store.append(
            "message", role="system", speaker="system", text="Approved", workflow=flow.snapshot()
        )
        self.room = ChatRoom(
            self.config, self.store, self.events.append, {"a": Writer(), "b": Peer()}
        )
        self.room.start()
        self.room.control("resume")
        try:
            await self.until(lambda: cancelling.is_set())
            self.assertEqual(self.room.workflow.phase, "discussion")
            self.assertEqual(self.room.writer, "a")
            await asyncio.sleep(0.04)
            self.assertFalse(any(phase == "planning" for _, phase in calls))
        finally:
            release.set()
        await self.until(lambda: not self.room.active)
        self.assertTrue(any(phase == "planning" for _, phase in calls))
        self.assertTrue((self.path / "partial.py").exists())
        self.assertFalse(any(m["text"] == "Stale writer output" for m in self.room.messages))
        self.assertEqual(self.room.workflow.data["revision_request"]["speaker"], "b")
        self.assertEqual(len(self.room.workflow.data["consensus_history"]), 1)

    async def test_stale_chat_revision_is_rejected_and_plain_chat_keeps_the_consensus(self):
        self.room = ChatRoom(self.config, self.store, self.events.append)
        agree(self.room.workflow)
        record = self.room.workflow.confirm_consensus()
        self.room.say("human", "An ordinary comment, not a revision")
        self.assertEqual(self.room.workflow.phase, "implementation")
        fence = self.room.fence()
        turn = Turn(
            self.config.agents[1].name,
            "implementation",
            "chat",
            self.room.messages[-1]["id"],
            fence,
            Workflow(self.config, self.room.workflow.snapshot()),
        )
        self.room.revision += 1
        reply = action_reply(
            "A stale request",
            {"action": "request_revision", "version": 1, "reason": "Old evidence"},
        )
        self.assertEqual(self.room.accept_concurrent(turn, reply, None), "rejected")
        self.assertEqual(self.room.workflow.phase, "implementation")
        self.assertEqual(self.room.workflow.data["consensus_history"], [record])


if __name__ == "__main__":
    unittest.main()
