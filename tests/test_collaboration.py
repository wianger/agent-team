from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agent_team.adapters import command_for
from agent_team.client import describe_workflow
from agent_team.config import AgentConfig, TeamConfig
from agent_team.engine import Room
from agent_team.store import Store
from agent_team.workflow import STATE_MARKER, Workflow, action_reply, validate_plan


def shared_plan():
    return {
        "action": "propose",
        "summary": "Build an addition function together",
        "acceptance_criteria": ["add(2, 3) returns 5"],
        "milestones": [
            {"id": "code", "title": "Addition", "details": "Implement add", "depends_on": []}
        ],
        "acceptance_checks": [
            [sys.executable, "-c", "from calc import add; assert add(2, 3) == 5"]
        ],
    }


class JudgmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = TeamConfig(
            workspace=Path(self.temp.name),
            agents=tuple(AgentConfig(n, "mock") for n in ("a", "b", "c")),
        )
        self.flow = Workflow(self.config)
        self.flow.apply("a", shared_plan())
        for member in self.flow.members:
            self.flow.apply(member, {"action": "approve", "version": 1})
        (self.config.workspace / "calc.py").write_text("def add(a, b): return a + b\n")

    def contribute(self, who="a", ready=True):
        self.flow.apply(
            who,
            {
                "action": "contribute",
                "version": 1,
                "milestone_id": "code",
                "ready": ready,
                "summary": "Implemented addition",
                "files": ["calc.py"],
                "tests": "Not run",
            },
        )

    def judge(self, who, kind="judge_pass", revision=None):
        return self.flow.apply(
            who,
            {
                "action": kind,
                "version": 1,
                "milestone_id": "code",
                "revision": revision
                if revision is not None
                else self.flow.data["checkpoint"]["revision"],
                "evidence": "Read calc.py and compared the zero boundary case",
            },
        )

    def test_all_other_members_must_judge_before_acceptance(self):
        self.contribute()
        self.assertEqual(self.flow.phase, "judging")
        self.assertEqual(self.flow.choose(0), "b")
        for who in ("a",):
            before = self.flow.snapshot()
            with self.assertRaises(ValueError):
                self.judge(who)
            self.assertEqual(self.flow.snapshot(), before)
        self.judge("b")
        self.assertEqual(self.flow.phase, "judging")
        self.assertEqual(self.flow.choose(0), "c")
        with self.assertRaises(ValueError):
            self.judge("b")
        self.judge("c")
        self.assertEqual(self.flow.phase, "review")
        self.assertEqual(self.flow.data["proposal"]["milestones"][0]["status"], "done")

    def test_critic_can_revise_peer_file_and_original_author_judges(self):
        self.contribute()
        self.judge("b", "judge_fail")
        self.assertEqual(self.flow.phase, "implementation")
        self.assertEqual(self.flow.choose(0), "b")
        (self.config.workspace / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        self.contribute("b")
        self.assertEqual(self.flow.data["checkpoint"]["revision"], 2)
        before = self.flow.snapshot()
        with self.assertRaisesRegex(ValueError, "stale"):
            self.judge("a", revision=1)
        self.assertEqual(self.flow.snapshot(), before)
        self.judge("a")
        self.judge("c")
        history = self.flow.data["proposal"]["milestones"][0]["contributions"]
        self.assertEqual([c["author"] for c in history], ["a", "b"])
        self.assertEqual(history[0]["judgments"][0]["action"], "judge_fail")
        self.assertEqual(history[1]["judgments"][0]["speaker"], "a")
        rendered = describe_workflow(self.flow.snapshot())
        self.assertIn("r2 by b", rendered)
        self.assertIn("judge_fail", rendered)

    def test_optional_focus_does_not_assign_permanent_writers_or_judges(self):
        focused = replace(
            self.config,
            agents=tuple(
                replace(agent, role=focus)
                for agent, focus in zip(
                    self.config.agents,
                    ("Security risks", "Performance risks", "Usability concerns"),
                    strict=True,
                )
            ),
        )
        self.flow = Workflow(focused, self.flow.snapshot())
        for member in self.flow.members:
            self.assertEqual(self.flow.choose(0, member), member)
        self.contribute("b")
        self.assertEqual(self.flow.eligible(), ["a", "c"])
        self.judge("a", "judge_fail")
        self.assertEqual(self.flow.choose(0), "a")
        self.contribute("a")
        self.assertEqual(self.flow.eligible(), ["b", "c"])
        self.judge("b")
        self.judge("c")
        self.assertEqual(self.flow.phase, "review")
        self.assertEqual(self.flow.eligible(), ["a", "b", "c"])

    def test_partial_draft_passes_judgment_without_completing_task(self):
        self.contribute(ready=False)
        self.judge("b")
        self.judge("c")
        self.assertEqual(self.flow.phase, "implementation")
        self.assertEqual(self.flow.current_milestone()["status"], "pending")
        self.assertEqual(self.flow.choose(0), "b")
        # Explicit human targeting may choose any writer, not just the preferred one.
        self.assertEqual(self.flow.choose(0, "c"), "c")

    def test_owner_is_optional_and_never_exclusive(self):
        proposal = shared_plan()
        proposal["milestones"][0]["owner"] = "c"
        flow = Workflow(self.config)
        flow.apply("a", proposal)
        for member in flow.members:
            flow.apply(member, {"action": "approve", "version": 1})
        self.flow = flow
        self.contribute("b")
        self.assertEqual(self.flow.data["checkpoint"]["author"], "b")

    def test_proposals_and_evidence_have_no_text_or_count_caps(self):
        proposal = shared_plan()
        proposal["summary"] = "x" * 250_000
        proposal["acceptance_criteria"] *= 40
        proposal["milestones"] = [
            {**proposal["milestones"][0], "id": f"T{i}", "details": "d" * 10_000} for i in range(35)
        ]
        proposal["acceptance_checks"] *= 12
        validated = validate_plan(proposal, self.flow.members)
        self.assertEqual(validated["summary"], proposal["summary"])
        self.assertEqual(len(validated["milestones"]), 35)
        self.contribute()
        evidence = "Evidence " + "e" * 250_000
        self.flow.apply(
            "b",
            {
                "action": "judge_pass",
                "version": 1,
                "milestone_id": "code",
                "revision": 1,
                "evidence": evidence,
            },
        )
        self.assertEqual(
            self.flow.current_milestone()["contributions"][0]["judgments"][0]["evidence"], evidence
        )

    def test_judging_permissions_are_read_only(self):
        codex = command_for(AgentConfig("a", "codex"), "judging")
        self.assertIn("read-only", codex)
        claude = command_for(AgentConfig("b", "claude"), "judging")
        self.assertEqual(claude[claude.index("--tools") + 1], "Read,Glob,Grep")
        self.assertNotIn("acceptEdits", claude)

    def test_restart_clears_pending_judgment_votes(self):
        self.contribute()
        self.judge("b")
        store = Store(self.config.workspace / "events.sqlite3")
        self.addCleanup(store.close)
        store.append(
            "message",
            role="member",
            speaker="b",
            text="Judged",
            workflow=self.flow.snapshot(),
        )
        room = Room(self.config, store, lambda e: None)
        self.assertTrue(room.manual_paused)
        self.assertEqual(room.workflow.phase, "judging")
        self.assertEqual(room.workflow.data["checkpoint"]["approvals"], [])
        self.assertEqual(room.workflow.eligible(), ["b", "c"])


class CollaborativeFixture:
    """Two agents really edit the same file, inspect it, and judge each other."""

    def __init__(self, workspace, *, reject_first=False, drafts=0):
        self.workspace = workspace
        self.reject_first = reject_first
        self.drafts = drafts

    async def stream(self, prompt, *, phase="discussion"):
        state = json.loads(prompt.split(STATE_MARKER, 1)[1].split("\n", 1)[0])
        action = {"version": state["version"]}
        if not state["proposal"]:
            action = shared_plan()
        elif phase == "planning":
            action.update(action="approve")
        elif phase == "implementation":
            revision = state["proposal"]["milestones"][0]["revision"]
            operator = "-" if self.reject_first and revision == 0 else "+"
            (self.workspace / "calc.py").write_text(f"def add(a, b):\n    return a {operator} b\n")
            action.update(
                action="contribute",
                milestone_id="code",
                ready=revision >= self.drafts,
                summary="Responded to the previous contribution",
                files=["calc.py"],
                tests="Awaiting the coordinator's real acceptance check",
            )
        elif phase == "judging":
            source = (self.workspace / "calc.py").read_text()
            action.update(
                action="judge_fail" if "a - b" in source else "judge_pass",
                milestone_id="code",
                revision=state["checkpoint"]["revision"],
                evidence="calc.py:2 subtracts instead of adding"
                if "a - b" in source
                else "Read calc.py:2; addition matches the agreed contract",
            )
        elif phase == "review":
            action.update(action="review_pass", evidence="Read calc.py and acceptance criteria")
        yield action_reply("My contribution and response to the team's latest work.", action)


class CollaborationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def run_fixture(self, **kwargs):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        workspace = Path(temp.name)
        config = TeamConfig(
            workspace=workspace,
            agents=(AgentConfig("a", "mock"), AgentConfig("b", "mock")),
            turn_delay=0,
            turn_timeout=0,
            work_timeout=0,
            acceptance_timeout=0,
        )
        store = Store(workspace / "events.sqlite3")
        self.addCleanup(store.close)
        room = Room(
            config,
            store,
            lambda e: None,
            {name: CollaborativeFixture(workspace, **kwargs) for name in ("a", "b")},
        )
        self.addAsyncCleanup(room.close)
        room.start()
        room.say("human", "Implement addition together")
        async with asyncio.timeout(10):
            while room.reason not in {"completed", "error", "blocked"}:
                await asyncio.sleep(0.005)
        self.assertEqual(room.reason, "completed", room.messages[-1])
        return room

    async def test_rejection_fix_and_reciprocal_judgment_before_completion(self):
        room = await self.run_fixture(reject_first=True)
        history = room.workflow.data["proposal"]["milestones"][0]["contributions"]
        self.assertEqual([c["author"] for c in history], ["b", "a"])
        self.assertEqual(history[0]["judgments"][0]["action"], "judge_fail")
        self.assertEqual(history[1]["judgments"][0]["speaker"], "b")
        self.assertEqual(history[1]["judgments"][0]["action"], "judge_pass")
        self.assertEqual(room.workflow.data["acceptance_results"][0]["exit_code"], 0)
        room.say("human", "Now consider a follow-up improvement")
        room.control("pause")
        self.assertEqual(room.workflow.phase, "discussion")

    async def test_implementation_continues_past_former_work_budget(self):
        room = await self.run_fixture(drafts=24)
        history = room.workflow.data["proposal"]["milestones"][0]["contributions"]
        self.assertEqual(len(history), 25)
        self.assertGreater(room.turns, 40)
        self.assertTrue(all(c["judgments"] for c in history))
        self.assertEqual({c["author"] for c in history}, {"a", "b"})
