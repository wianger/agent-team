from __future__ import annotations

import asyncio
import copy
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agent_team.adapters import EventDecoder, MockAdapter, command_for
from agent_team.config import AgentConfig, TeamConfig, demo_config
from agent_team.engine import Room
from agent_team.server import Server
from agent_team.store import Store
from agent_team.verification import run_checks
from agent_team.workflow import Workflow, action_reply, parse_action, validate_plan, visible_text


def plan():
    return {
        "action": "propose",
        "summary": "Implement and document a function",
        "acceptance": ["function returns the expected value"],
        "tasks": [
            {
                "id": "code",
                "title": "Implementation",
                "details": "Write code",
                "owner": "a",
                "depends_on": [],
            },
            {
                "id": "docs",
                "title": "Documentation",
                "details": "Document code",
                "owner": "b",
                "depends_on": ["code"],
            },
        ],
        "checks": [[sys.executable, "-c", "assert 1 + 1 == 2"]],
    }


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = TeamConfig(
            agents=(AgentConfig("a", "mock"), AgentConfig("b", "mock")),
            workspace=Path(self.temp.name),
        )
        self.flow = Workflow(self.config)

    def tearDown(self):
        self.temp.cleanup()

    def agree(self):
        self.flow.apply("a", plan())
        self.flow.apply("a", {"action": "approve", "version": 1})
        self.flow.apply("b", {"action": "approve", "version": 1})

    def test_clone_preserves_state_without_sharing_mutable_data(self):
        self.agree()
        cloned = self.flow.clone()
        self.assertEqual(cloned.snapshot(), self.flow.snapshot())
        self.assertIs(cloned.config, self.config)
        cloned.data["proposal"]["tasks"][0]["depends_on"].append("new-dependency")
        cloned.data["approvals"].clear()
        cloned.members.append("new-member")
        self.assertEqual(self.flow.data["proposal"]["tasks"][0]["depends_on"], [])
        self.assertEqual(set(self.flow.data["approvals"]), {"a", "b"})
        self.assertEqual(self.flow.members, ["a", "b"])

    def complete_tasks(self):
        for owner, task_id in (("a", "code"), ("b", "docs")):
            filename = task_id + ".txt"
            (self.config.workspace / filename).write_text("artifact")
            self.flow.apply(
                owner,
                {
                    "action": "task_done",
                    "version": 1,
                    "task_id": task_id,
                    "summary": "written",
                    "files": [filename],
                    "tests": "waiting for verification",
                },
            )

            self.flow.apply(
                "b" if owner == "a" else "a",
                {
                    "action": "judge_pass",
                    "version": 1,
                    "task_id": task_id,
                    "revision": self.flow.data["checkpoint"]["revision"],
                    "evidence": "Read the submitted artifact",
                },
            )

    def test_requires_explicit_unanimous_approval_of_same_version(self):
        self.flow.apply("a", plan())
        self.assertEqual(self.flow.data["approvals"], [])
        self.flow.apply("a", None)
        self.flow.apply("b", {"action": "approve", "version": 1})
        self.assertEqual(self.flow.phase, "discussion")
        self.flow.apply("a", {"action": "approve", "version": 1})
        self.assertEqual(self.flow.phase, "implementation")

    def test_new_proposal_invalidates_votes_and_rejects_stale_vote(self):
        self.flow.apply("a", plan())
        self.flow.apply("a", {"action": "approve", "version": 1})
        self.flow.apply("b", plan())
        self.assertEqual(self.flow.data["version"], 2)
        self.assertEqual(self.flow.data["approvals"], [])
        before = self.flow.snapshot()
        with self.assertRaises(ValueError):
            self.flow.apply("b", {"action": "approve", "version": 1})
        self.assertEqual(self.flow.snapshot(), before)

    def test_objection_revokes_all_votes_and_must_be_resolved(self):
        self.flow.apply("a", plan())
        self.flow.apply("a", {"action": "approve", "version": 1})
        self.flow.apply("b", {"action": "object", "version": 1, "reason": "Missing detail"})
        self.assertEqual(self.flow.data["approvals"], [])
        self.flow.apply("a", {"action": "approve", "version": 1})
        self.assertEqual(self.flow.phase, "discussion")
        self.flow.apply("b", {"action": "approve", "version": 1})
        self.assertEqual(self.flow.phase, "implementation")

    def test_invalid_dependencies_and_verification_commands_rejected(self):
        for mutate in (
            lambda p: p["tasks"][0].update(depends_on=["docs"]),
            lambda p: p["tasks"][0].update(depends_on=["missing"]),
            lambda p: p["tasks"][0].update(owner="outsider"),
            lambda p: p.update(checks=[]),
            lambda p: p.update(checks=["echo success"]),
        ):
            value = plan()
            mutate(value)
            with self.assertRaises(ValueError):
                validate_plan(value, ["a", "b"])

    def test_tasks_need_dependency_order_and_real_files_but_are_not_owned(self):
        self.agree()
        self.assertEqual(self.flow.choose(1), "b")
        action = {
            "action": "task_done",
            "version": 1,
            "task_id": "code",
            "summary": "done",
            "files": ["not-there.py"],
            "tests": "not tested",
        }
        for who in ("b", "a"):
            with self.assertRaises(ValueError):
                self.flow.apply(who, action)
        self.complete_tasks()
        self.assertEqual(self.flow.phase, "review")

    def test_everyone_reviews_and_only_actual_checks_can_complete(self):
        self.agree()
        self.complete_tasks()
        with self.assertRaises(ValueError):
            self.flow.apply("a", {"action": "completed", "version": 1})
        self.flow.apply("a", {"action": "review_pass", "version": 1, "evidence": "checked files"})
        self.assertEqual(self.flow.phase, "review")
        self.flow.apply("b", {"action": "review_pass", "version": 1, "evidence": "checked files"})
        self.assertEqual(self.flow.phase, "verification")
        self.flow.verified([{"command": plan()["checks"][0], "exit_code": 0, "output": ""}])
        self.assertEqual(self.flow.phase, "completed")

    def test_review_failure_reopens_upstream_and_dependents(self):
        self.agree()
        self.complete_tasks()
        self.flow.apply("a", {"action": "review_pass", "version": 1, "evidence": "checked files"})
        self.flow.apply(
            "b",
            {
                "action": "review_fail",
                "version": 1,
                "task_ids": ["code"],
                "evidence": "function broken",
            },
        )
        self.assertEqual(self.flow.phase, "implementation")
        self.assertEqual(self.flow.data["review_approvals"], [])
        self.assertTrue(all(t["status"] == "pending" for t in self.flow.data["proposal"]["tasks"]))

    def test_user_steering_invalidates_plan_and_workspace_change_rejected(self):
        self.agree()
        self.flow.reconsider()
        self.assertEqual(self.flow.phase, "discussion")
        self.assertIsNone(self.flow.data["proposal"])
        saved = self.flow.snapshot()
        saved["workspace"] += "/other"
        with self.assertRaises(ValueError):
            Workflow(self.config, saved)

    def test_protocol_is_hidden_and_incomplete_action_is_not_accepted(self):
        text = action_reply("I agree", {"action": "approve", "version": 2})
        self.assertEqual(parse_action(text), ("I agree", {"action": "approve", "version": 2}))
        self.assertEqual(visible_text("I agree\n<team-ac"), "I agree")
        with self.assertRaises(ValueError):
            parse_action("hello <team-action>{}")

    def test_phase_specific_cli_permissions_and_multistep_claude_output(self):
        self.assertIn("workspace-write", command_for(AgentConfig("a", "codex"), "implementation"))
        claude = command_for(AgentConfig("b", "claude"), "implementation")
        self.assertIn("acceptEdits", claude)
        self.assertNotIn("Bash", command_for(AgentConfig("b", "claude"), "review")[-1])
        decoder = EventDecoder("claude")
        for text in ("I will inspect files. ", "Implementation complete."):
            decoder.feed(
                {
                    "type": "stream_event",
                    "event": {
                        "delta": {"type": "text_delta", "text": text},
                    },
                }
            )
        decoder.feed({"type": "result", "result": "Implementation complete."})
        decoder.finish()
        self.assertEqual(decoder.text, "I will inspect files. Implementation complete.")


class WorkflowIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.config = replace(demo_config(), workspace=self.workspace, turn_delay=0)
        self.store = Store(self.workspace / "events.sqlite3")
        self.room = None

    async def asyncTearDown(self):
        if self.room and not self.room.closed:
            await self.room.close()
        self.store.close()
        self.temp.cleanup()

    async def wait_until(self, predicate):
        async with asyncio.timeout(8):
            while not predicate():
                await asyncio.sleep(0.005)

    async def test_complete_pipeline_has_unanimity_before_writes_and_verified_artifacts(self):
        events = []
        self.room = Room(self.config, self.store, lambda e: events.append(copy.deepcopy(e)))
        self.room.start()
        self.room.say("human", "Build the example")
        await self.wait_until(lambda: self.room.reason == "completed")
        states = [m["workflow"] for m in self.room.messages]
        implementing = next(s for s in states if s["phase"] == "implementation")
        self.assertEqual(set(implementing["approvals"]), {"member_a", "member_b"})
        self.assertTrue((self.workspace / "hello.py").is_file())
        self.assertTrue((self.workspace / "HOWTO.md").is_file())
        self.assertEqual(self.room.workflow.data["checks_result"][0]["exit_code"], 0)
        stages = [e["phase"] for e in events if e["type"] == "floor.granted"]
        self.assertEqual(
            stages,
            ["discussion"] * 3
            + ["implementation", "judging"] * 2
            + ["review"] * 2
            + ["verification"],
        )
        with self.assertRaises(ValueError):
            self.room.control("resume")

    async def test_recovery_keeps_tasks_and_votes_but_requires_resume(self):
        self.room = Room(self.config, self.store, lambda e: None)
        self.room.start()
        self.room.say("human", "Build")
        await self.wait_until(lambda: self.room.reason == "completed")
        await self.room.close()
        self.room = Room(self.config, self.store, lambda e: None)
        self.assertEqual(self.room.workflow.phase, "completed")
        self.assertTrue(self.room.manual_paused)
        self.assertEqual(len(self.room.workflow.data["proposal"]["tasks"]), 2)

    async def test_failed_and_timed_out_checks_do_not_count_as_success(self):
        results = await run_checks(
            [[sys.executable, "-c", "raise SystemExit(7)"]],
            self.workspace,
            2,
            lambda text: None,
        )
        self.assertEqual(results[0]["exit_code"], 7)
        timed = await run_checks(
            [[sys.executable, "-c", "import time; time.sleep(60)"]],
            self.workspace,
            0.1,
            lambda text: None,
        )
        self.assertEqual(timed[0]["exit_code"], -1)
        self.assertIn("timed out", timed[0]["output"])

    async def test_same_workspace_cannot_have_two_writing_rooms(self):
        first = Server(self.config, self.workspace / "one")
        second = Server(self.config, self.workspace / "two")
        await first.start()
        try:
            with self.assertRaisesRegex(ValueError, "workspace already"):
                await second.start()
            self.assertTrue((self.workspace / "one" / "connection.json").is_file())
        finally:
            await first.close()
            await second.close()

    async def test_single_step_at_consensus_does_not_start_implementation(self):
        self.room = Room(self.config, self.store, lambda e: None)
        self.room.start()
        self.room.control("pause")
        self.room.say("human", "Build")
        for _ in range(3):
            self.room.control("next")
            await self.wait_until(lambda: self.room.manual_paused and self.room.active is None)
        self.assertEqual(self.room.workflow.phase, "implementation")
        self.assertFalse((self.workspace / "hello.py").exists())
        self.room.control("resume")
        await self.wait_until(lambda: self.room.reason == "completed")

    async def test_user_steering_during_write_preserves_files_and_invalidates_delivery(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()
        workspace = self.workspace

        class InterruptedAdapter(MockAdapter):
            async def stream(self, prompt, *, phase="discussion"):
                if phase == "implementation":
                    (workspace / "partial.txt").write_text("in progress")
                    started.set()
                    yield "working"
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()
                else:
                    async for text in super().stream(prompt, phase=phase):
                        yield text

        adapters = {a.name: InterruptedAdapter(a, self.workspace) for a in self.config.agents}
        self.room = Room(self.config, self.store, lambda e: None, adapters)
        self.room.start()
        self.room.say("human", "Build")
        await asyncio.wait_for(started.wait(), 5)
        self.room.say("human", "Change direction")
        self.room.control("pause")
        await self.wait_until(lambda: self.room.active is None)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.room.workflow.phase, "discussion")
        self.assertIsNone(self.room.workflow.data["proposal"])
        self.assertEqual((self.workspace / "partial.txt").read_text(), "in progress")
        self.assertFalse(
            any((m.get("action") or {}).get("action") == "task_done" for m in self.room.messages)
        )

    async def test_failed_verification_returns_to_shared_repairs_until_success(self):
        class RepairingCheckAdapter(MockAdapter):
            def workflow_reply(self, state):
                reply = super().workflow_reply(state)
                if not state["proposal"]:
                    display, action = parse_action(reply)
                    action["checks"] = [
                        [
                            sys.executable,
                            "-c",
                            "from pathlib import Path; p=Path('attempts'); "
                            "n=int(p.read_text())+1 if p.exists() else 1; "
                            "p.write_text(str(n)); raise SystemExit(3 if n < 3 else 0)",
                        ]
                    ]
                    return action_reply(display, action)
                return reply

        adapters = {a.name: RepairingCheckAdapter(a, self.workspace) for a in self.config.agents}
        self.room = Room(self.config, self.store, lambda e: None, adapters)
        self.room.start()
        self.room.say("human", "Build")
        await self.wait_until(lambda: self.room.reason == "completed")
        self.assertEqual((self.workspace / "attempts").read_text(), "3")
        results = [
            m["workflow"]["checks_result"] for m in self.room.messages if m["role"] == "system"
        ]
        self.assertEqual([r[0]["exit_code"] for r in results], [3, 3, 0])
        self.assertGreater(self.room.turns, 8)
        self.assertEqual(self.room.workflow.phase, "completed")

    async def test_demo_preserves_existing_files_and_answer_unblocks_discussion(self):
        (self.workspace / "hello.py").write_text("user content")
        self.room = Room(self.config, self.store, lambda e: None)
        self.room.start()
        self.room.say("human", "Build")
        await self.wait_until(lambda: self.room.reason == "blocked")
        self.assertEqual((self.workspace / "hello.py").read_text(), "user content")
        self.room.say("human", "Here is an updated constraint")
        self.assertFalse(self.room.manual_paused)
        self.assertEqual(self.room.workflow.phase, "discussion")
        self.room.control("pause")

    async def test_restart_before_verification_requires_fresh_reviews(self):
        flow = Workflow(self.config)
        flow.apply(
            "member_a",
            {
                **plan(),
                "tasks": [
                    {
                        "id": "T1",
                        "title": "Example",
                        "details": "Example",
                        "owner": "member_a",
                        "depends_on": [],
                    }
                ],
            },
        )
        flow.data.update(phase="verification", review_approvals=["member_a", "member_b"])
        self.store.append(
            "message", role="agent", speaker="member_b", text="Reviewed", workflow=flow.snapshot()
        )
        self.room = Room(self.config, self.store, lambda e: None)
        self.assertEqual(self.room.workflow.phase, "review")
        self.assertEqual(self.room.workflow.data["review_approvals"], [])
        self.assertTrue(self.room.manual_paused)

    async def test_acceptance_output_is_preserved_in_full(self):
        results = await run_checks(
            [[sys.executable, "-c", "print('first'+'x'*300000+'last')"]],
            self.workspace,
            0,
            lambda text: None,
        )
        self.assertEqual(results[0]["exit_code"], 0)
        self.assertEqual(results[0]["output"], "first" + "x" * 300000 + "last\n")
