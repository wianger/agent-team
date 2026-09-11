"""Versioned consensus and reciprocal implementation judgment."""

from __future__ import annotations

import copy
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import PurePosixPath

from .config import TeamConfig, valid_name
from .consensus import document_path

ACTION_START = "<team-action>"
ACTION_END = "</team-action>"
STATE_MARKER = "Current workflow state JSON:\n"
PHASES = {
    "discussion": "Discussion",
    "implementation": "Shared implementation",
    "judging": "Peer judgment",
    "review": "Integration review",
    "acceptance_criteria": "Acceptance checks",
    "completed": "Completed",
}


def action_reply(text: str, action: dict) -> str:
    return text + "\n" + ACTION_START + json.dumps(action, ensure_ascii=False) + ACTION_END


def visible_text(text: str) -> str:
    """Keep protocol metadata out of both committed text and the live display."""
    text = text.split(ACTION_START, 1)[0]
    for length in range(1, len(ACTION_START)):
        if text.endswith(ACTION_START[:length]):
            return text[:-length].rstrip()
    return text.rstrip()


UNWRAPPED_ACTION = re.compile(r'\{\s*"action"\s*:\s*"')


def unwrapped_action(text: str) -> bool:
    """True when a reply carries a bare action object outside a <team-action> block.

    Such a reply reads as an ordinary message, so the vote or checkpoint it intends
    never reaches the coordinator, and its author has no way to notice.
    """
    return bool(UNWRAPPED_ACTION.search(text))


def parse_action(reply: str) -> tuple[str, dict | None]:
    blocks = list(re.finditer(r"<team-action>(.*?)</team-action>", reply, re.DOTALL))
    if not blocks:
        if ACTION_START in reply:
            raise ValueError("Incomplete team-action block")
        return reply, None
    # The last block is the action, so anything after it cannot change what was meant.
    # Members routinely sign off with a closing sentence, and refusing the turn for that
    # pauses the whole room over output whose intent is unambiguous.
    block = blocks[-1]
    action = json.loads(block.group(1))
    if not isinstance(action, dict) or not isinstance(action.get("action"), str):
        raise ValueError("team-action must be a JSON object with an action string")
    return visible_text(reply), action


def nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonempty text")
    return value.strip()


def texts(value: object, label: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ValueError(f"{label} must be {'an' if allow_empty else 'a nonempty'} array")
    return [nonempty(item, label) for item in value]


def validate_plan(action: dict, members: list[str]) -> dict:
    summary = nonempty(action.get("summary"), "summary")
    acceptance = texts(action.get("acceptance_criteria"), "acceptance")
    raw_milestones = action.get("milestones")
    if not isinstance(raw_milestones, list) or not raw_milestones:
        raise ValueError("A proposal needs at least one shared milestone")
    milestones, ids = [], set()
    for raw in raw_milestones:
        if not isinstance(raw, dict) or not valid_name(raw.get("id")):
            raise ValueError("Each milestone needs a valid id")
        milestone_id = raw["id"]
        if milestone_id in ids:
            raise ValueError(f"Duplicate milestone id: {milestone_id}")
        ids.add(milestone_id)
        # Accepted for old proposals, but never grants exclusive ownership.
        owner = raw.get("owner")
        if owner is not None and owner not in members:
            raise ValueError(f"Suggested owner of {milestone_id} must be a configured member")
        milestones.append(
            {
                "id": milestone_id,
                "title": nonempty(raw.get("title"), "title"),
                "details": nonempty(raw.get("details"), "details"),
                "owner": owner,
                "depends_on": texts(raw.get("depends_on", []), "depends_on", allow_empty=True),
                "status": "pending",
                "report": None,
                "revision": 0,
                "contributions": [],
            }
        )
    visited = set()
    while len(visited) < len(milestones):
        ready = [
            t for t in milestones if t["id"] not in visited and set(t["depends_on"]) <= visited
        ]
        if not ready:
            raise ValueError(
                "Milestone dependencies contain a cycle, self-reference, or unknown id"
            )
        visited.update(t["id"] for t in ready)
    checks = action.get("acceptance_checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError("A proposal needs at least one executable acceptance command")
    for check in checks:
        if (
            not isinstance(check, list)
            or not check
            or any(not isinstance(arg, str) or "\0" in arg for arg in check)
            or not check[0]
        ):
            raise ValueError("Each check must be a nonempty command argument array")
    return {
        "summary": summary,
        "acceptance_criteria": acceptance,
        "milestones": milestones,
        "acceptance_checks": checks,
    }


class Workflow:
    def __init__(self, config: TeamConfig, saved: dict | None = None) -> None:
        self.config = config
        self.members = [a.name for a in config.agents]
        self.data = (
            copy.deepcopy(saved)
            if saved
            else {
                "phase": "discussion",
                "version": 0,
                "proposal": None,
                "proposer": None,
                "approvals": [],
                "objections": {},
                "review_approvals": [],
                "acceptance_results": [],
                "members": self.members,
                "workspace": str(config.workspace.resolve()),
            }
        )
        if self.data["members"] != self.members or self.data["workspace"] != str(
            config.workspace.resolve()
        ):
            raise ValueError("Room members or workspace changed; use a new --room")
        if saved and ("checks_result" in self.data or "tasks" in (self.data["proposal"] or {})):
            raise ValueError(
                "This room was recorded before 0.2.0 renamed the workflow schema "
                "(tasks are now milestones, checks are now acceptance_checks). "
                "Read it with agent-team history, and start new work with a new --room."
            )
        # Additive migration keeps existing transcripts, plans, and artifacts intact.
        self.data.setdefault("checkpoint", None)
        self.data.setdefault("next_writer", None)
        self.data.setdefault("feedback", [])
        self.data.setdefault("document_namespace", uuid.uuid4().hex)
        self.data.setdefault("consensus_history", [])
        self.data.setdefault("revision_base", None)
        self.data.setdefault("revision_request", None)
        if self.data["proposal"]:
            for milestone in self.data["proposal"]["milestones"]:
                milestone.setdefault("revision", 0)
                milestone.setdefault("contributions", [])

    @property
    def phase(self) -> str:
        return self.data["phase"]

    def snapshot(self) -> dict:
        return copy.deepcopy(self.data)

    def clone(self) -> Workflow:
        return Workflow(self.config, self.data)

    def reconsider(self, *, speaker: str = "user", reason: str = "Reconsider the plan") -> None:
        if self.data["proposal"]:
            self.data["revision_base"] = {
                "version": self.data["version"],
                "phase": self.phase,
                "proposal": copy.deepcopy(self.data["proposal"]),
                "checkpoint": copy.deepcopy(self.data["checkpoint"]),
                "feedback": copy.deepcopy(self.data["feedback"]),
                "acceptance_results": copy.deepcopy(self.data["acceptance_results"]),
            }
        self.data["revision_request"] = (
            {"speaker": speaker, "reason": reason} if self.data["version"] else None
        )
        self.data.update(
            phase="discussion",
            proposal=None,
            proposer=None,
            approvals=[],
            objections={},
            review_approvals=[],
            acceptance_results=[],
            checkpoint=None,
            next_writer=None,
            feedback=[],
        )

    def consensus_reached(self) -> bool:
        return set(self.data["approvals"]) == set(self.members) and not self.data["objections"]

    def confirm_consensus(self, *, recovered: bool = False) -> dict:
        """Called by the coordinator after all relevant discussion has finished."""
        if (
            not self.data["proposal"]
            or set(self.data["approvals"]) != set(self.members)
            or self.data["objections"]
        ):
            raise ValueError("A consensus record requires unanimous approval without objections")
        history = self.data["consensus_history"]
        if history and history[-1]["version"] == self.data["version"]:
            return history[-1]
        record = {
            "version": self.data["version"],
            "recorded_at": datetime.now(UTC).isoformat(),
            "proposal": copy.deepcopy(self.data["proposal"]),
            "approvals": list(self.members),
            "document": document_path(self.data["document_namespace"], self.data["version"]),
            "supersedes": history[-1]["version"] if history else None,
            "revision_request": copy.deepcopy(self.data["revision_request"]),
            "recovered": recovered,
        }
        history.append(record)
        return record

    def current_milestone(self) -> dict | None:
        proposal = self.data["proposal"]
        if not proposal:
            return None
        if self.phase == "judging":
            return next(
                t
                for t in proposal["milestones"]
                if t["id"] == self.data["checkpoint"]["milestone_id"]
            )
        if self.phase != "implementation":
            return None
        done = {t["id"] for t in proposal["milestones"] if t["status"] == "done"}
        return next(
            (
                t
                for t in proposal["milestones"]
                if t["status"] == "pending" and set(t["depends_on"]) <= done
            ),
            None,
        )

    def eligible(self) -> list[str]:
        if self.phase == "judging":
            checkpoint = self.data["checkpoint"]
            return [
                m
                for m in self.members
                if m != checkpoint["author"] and m not in checkpoint["approvals"]
            ]
        if self.phase == "review":
            return [m for m in self.members if m not in self.data["review_approvals"]]
        return list(self.members)

    def choose(self, cursor: int, target: str | None = None) -> str:
        eligible = self.eligible()
        if target is not None:
            if target not in eligible:
                raise ValueError("That member cannot judge this revision or has already approved")
            return target
        if self.phase == "implementation":
            if self.current_milestone() is None:
                raise ValueError("No executable shared milestone")
            if self.data["next_writer"] in eligible:
                return self.data["next_writer"]
        for offset in range(len(self.members)):
            name = self.members[(cursor + offset) % len(self.members)]
            if name in eligible:
                return name
        raise ValueError("No eligible member")

    def apply(self, speaker: str, action: dict | None) -> str:
        """Malformed or stale actions never partly change state."""
        candidate = self.clone()
        note = candidate._apply(speaker, action)
        self.data = candidate.data
        return note

    def _apply(self, speaker: str, action: dict | None) -> str:
        if speaker not in self.members:
            raise ValueError("Workflow actions must come from configured agents")
        if not action:
            if self.phase != "discussion":
                raise ValueError("Implementation and review turns require a valid team-action")
            return ""
        kind = action["action"]
        if kind == "blocked":
            return "blocked: " + nonempty(action.get("reason"), "reason")
        if self.phase == "discussion" and kind == "propose":
            # Replacing someone else's standing proposal wipes their approvals, so it must
            # cost the replacer a recorded reason. Refining your own draft stays free.
            if (
                self.data["proposal"]
                and not self.data["objections"]
                and self.data.get("proposer") != speaker
            ):
                raise ValueError(
                    f"Proposal v{self.data['version']} stands unopposed; object with a "
                    "reason before proposing a replacement"
                )
            proposal = validate_plan(action, self.members)
            self.data.update(
                proposal=proposal,
                proposer=speaker,
                version=self.data["version"] + 1,
                # Authoring a proposal is approving it; a separate vote only costs a turn.
                approvals=[speaker],
                objections={},
                review_approvals=[],
                acceptance_results=[],
                checkpoint=None,
                next_writer=None,
                feedback=[],
            )
            version = self.data["version"]
            limit = self.config.proposal_version_limit
            if limit and version > limit:
                return (
                    f"stalled: proposal v{version} passed proposal_version_limit={limit} "
                    "without consensus. Members are replacing each other's proposals "
                    "instead of converging; a human must steer."
                )
            if self.consensus_reached():
                self.data["phase"] = "implementation"
                return "Consensus reached. Begin shared implementation with peer judgment."
            return f"Proposal v{version} awaits explicit approval from every other member."
        if type(action.get("version")) is not int or action["version"] != self.data["version"]:
            raise ValueError("Action references a stale or missing proposal version")
        if kind == "request_revision":
            if self.phase == "discussion" or not self.data["proposal"]:
                raise ValueError(
                    "Already discussing; propose changes or object to the current draft"
                )
            reason = nonempty(action.get("reason"), "reason")
            self.reconsider(speaker=speaker, reason=reason)
            return (
                f"{speaker} requests a revised proposal: {reason}. "
                "Discuss and approve a new version."
            )
        if self.phase == "discussion" and kind in {"approve", "object"}:
            if not self.data["proposal"]:
                raise ValueError("There is no proposal to vote on")
            if kind == "object":
                self.data["approvals"] = []
                self.data["objections"][speaker] = nonempty(action.get("reason"), "reason")
                return f"{speaker} objects; consensus must be confirmed again."
            self.data["objections"].pop(speaker, None)
            if speaker not in self.data["approvals"]:
                self.data["approvals"].append(speaker)
            if self.consensus_reached():
                self.data["phase"] = "implementation"
                return "Consensus reached. Begin shared implementation with peer judgment."
            return f"{speaker} approves proposal v{self.data['version']}."
        if self.phase == "implementation" and kind in {"contribute", "task_done"}:
            return self.contribute(speaker, action)
        if self.phase == "judging" and kind in {"judge_pass", "judge_fail"}:
            return self.judge(speaker, action)
        if self.phase == "review" and kind in {"review_pass", "review_fail"}:
            evidence = nonempty(action.get("evidence"), "evidence")
            if kind == "review_fail":
                ids = texts(action.get("milestone_ids"), "milestone_ids")
                if not set(ids) <= {t["id"] for t in self.data["proposal"]["milestones"]}:
                    raise ValueError("Review references an unknown milestone")
                self.data["feedback"].append(
                    {
                        "speaker": speaker,
                        "action": kind,
                        "milestone_ids": ids,
                        "evidence": evidence,
                    }
                )
                self.reopen(set(ids))
                self.data["next_writer"] = speaker
                return f"{speaker} requests revisions: {evidence}"
            if speaker not in self.data["review_approvals"]:
                self.data["review_approvals"].append(speaker)
            if set(self.data["review_approvals"]) == set(self.members):
                self.data["phase"] = "acceptance"
            return f"{speaker} approves the integrated result: {evidence}"
        raise ValueError(f"Phase {self.phase} does not accept action {kind}")

    def contribute(self, speaker: str, action: dict) -> str:
        milestone = self.current_milestone()
        if not milestone or action.get("milestone_id") != milestone["id"]:
            raise ValueError(
                "Contribute to the current shared milestone; dependencies must be accepted"
            )
        ready = action.get("ready", action["action"] == "task_done")
        if type(ready) is not bool:
            raise ValueError("ready must be a boolean")
        report = {
            "summary": nonempty(action.get("summary"), "summary"),
            "files": texts(action.get("files"), "files", allow_empty=True),
            "tests": nonempty(action.get("tests"), "tests"),
        }
        for filename in report["files"]:
            path = PurePosixPath(filename)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise ValueError("Reported files must be workspace-relative paths")
            full = (self.config.workspace / filename).resolve()
            if not full.is_relative_to(self.config.workspace.resolve()) or not full.is_file():
                raise ValueError(f"Reported file is missing or outside the workspace: {filename}")
        milestone["revision"] += 1
        milestone.update(status="judging", report=report)
        milestone["contributions"].append(
            {
                **report,
                "author": speaker,
                "revision": milestone["revision"],
                "ready": ready,
                "judgments": [],
            }
        )
        self.data.update(
            phase="judging",
            review_approvals=[],
            checkpoint={
                "milestone_id": milestone["id"],
                "revision": milestone["revision"],
                "author": speaker,
                "ready": ready,
                "approvals": [],
            },
            next_writer=self.members[(self.members.index(speaker) + 1) % len(self.members)],
        )
        return f"{speaker} submits {milestone['id']} r{milestone['revision']} for peer judgment."

    def judge(self, speaker: str, action: dict) -> str:
        checkpoint = self.data["checkpoint"]
        if (
            action.get("milestone_id") != checkpoint["milestone_id"]
            or type(action.get("revision")) is not int
            or action["revision"] != checkpoint["revision"]
        ):
            raise ValueError("Judgment references a stale or missing checkpoint revision")
        if speaker not in self.eligible():
            raise ValueError("Authors cannot judge their own checkpoint or vote twice")
        evidence = nonempty(action.get("evidence"), "evidence")
        milestone = self.current_milestone()
        verdict = {"speaker": speaker, "action": action["action"], "evidence": evidence}
        milestone["contributions"][-1]["judgments"].append(verdict)
        if action["action"] == "judge_fail":
            self.data["feedback"].append(
                {
                    **verdict,
                    "milestone_ids": [milestone["id"]],
                    "revision": milestone["revision"],
                }
            )
            self.reopen({milestone["id"]})
            # The critic can demonstrate a fix; the previous author then judges it.
            self.data["next_writer"] = speaker
            return f"{speaker} challenges {milestone['id']} r{milestone['revision']}: {evidence}"
        checkpoint["approvals"].append(speaker)
        if set(checkpoint["approvals"]) == set(self.members) - {checkpoint["author"]}:
            milestone["status"] = "done" if checkpoint["ready"] else "pending"
            self.data.update(phase="implementation", checkpoint=None)
            if all(t["status"] == "done" for t in self.data["proposal"]["milestones"]):
                self.data.update(phase="review", next_writer=None, review_approvals=[])
            return (
                f"Peers accept {milestone['id']} r{milestone['revision']}; "
                f"status: {milestone['status']}."
            )
        return (
            f"{speaker} accepts {milestone['id']} r{milestone['revision']}; awaiting other peers."
        )

    def reopen(self, ids: set[str]) -> None:
        milestones = self.data["proposal"]["milestones"]
        while True:
            expanded = ids | {t["id"] for t in milestones if set(t["depends_on"]) & ids}
            if expanded == ids:
                break
            ids = expanded
        for milestone in milestones:
            if milestone["id"] in ids:
                milestone.update(status="pending", report=None)
        self.data.update(
            phase="implementation", review_approvals=[], acceptance_results=[], checkpoint=None
        )

    def accepted(self, results: list[dict]) -> str:
        if self.phase != "acceptance":
            raise ValueError("Not in the acceptance phase")
        if (
            results
            and len(results) == len(self.data["proposal"]["acceptance_checks"])
            and all(r["exit_code"] == 0 for r in results)
        ):
            self.data.update(phase="completed", acceptance_results=results)
            return "Shared work, peer judgments, integration reviews, and acceptance checks passed."
        self.reopen({t["id"] for t in self.data["proposal"]["milestones"]})
        self.data["acceptance_results"] = results
        return "Acceptance checks failed. Continue shared repairs, peer judgment, and acceptance."


def workflow_instructions(workflow: Workflow, speaker: str) -> str:
    state = workflow.snapshot()
    version = state["version"]
    common = (
        "Turn the user's idea into an agreed plan and a verified implementation together.\n"
        "Current coordinator state takes precedence over superseded plans in the transcript.\n"
        "Milestones and files are shared, not private assignments. You may improve another "
        "member's "
        "implementation, and they must judge yours. Respond to critiques with evidence.\n"
        "Preserve existing edits. No unrelated deletions, git resets, commits, pushes, or deploys. "
        "Do not modify .agent-team data. Inspect interrupted work before continuing.\n"
        "Every consensus is saved by the coordinator as a versioned Markdown document "
        "under docs/agent-team. Do not edit generated consensus records or ask a peer to write "
        "them. The current workflow and consensus_history identify approved versions.\n"
        "Consensus is revisable, not permanent. If new evidence requires changing the agreed "
        "scope or approach, request a new discussion within the user's authorized scope: "
        f'{{"action":"request_revision","version":{version},"reason":"specific new evidence"}}. '
        "This stops current work, preserves the previous consensus and files, and requires "
        "fresh unanimous approval of a new proposal. "
        "During discussion, propose or object instead.\n"
        "Explain your reasoning to the team. End your FINAL reply with one "
        "<team-action>JSON</team-action> block; do not put action blocks in interim commentary.\n"
        'If blocked, return {"action":"blocked","reason":"missing user input or authority"}.\n'
    )
    if workflow.phase == "discussion":
        instructions = (
            "Read and discuss only; do not modify files yet. Discuss goals, tradeoffs, "
            "assumptions and acceptance criteria, and critically respond to peer proposals.\n"
            "Propose shared milestones, not isolated assignments. An optional owner is only a "
            "suggestion, never exclusive ownership. Each member must explicitly approve the same "
            "version, including the proposer in a later turn. Reproposing clears every vote.\n"
            "When revising, read revision_base and consensus_history. Explain what changes and "
            "what remains valid; inspect and reuse existing artifacts where appropriate. "
            "Previous contributions are context, not automatic acceptance of revised milestones.\n"
            'Proposal: {"action":"propose","summary":"goal, approach, assumptions and tradeoffs",'
            '"acceptance_criteria":["checkable requirement"],'
            '"milestones":[{"id":"T1","title":"Shared milestone",'
            '"details":"what to implement and judge","depends_on":[]}],'
            '"acceptance_checks":[["python3","-m","unittest","discover","-s","tests"]]}\n'
            "Choose meaningful, noninteractive acceptance commands. The coordinator "
            "actually executes these argv arrays after reviews; an echo is not a check.\n"
            f'Approve: {{"action":"approve","version":{version}}}; '
            f'object: {{"action":"object","version":{version},"reason":"specific issue"}}.\n'
            "Ordinary discussion can omit an action. Agreeing in prose or [[PASS]] is not a vote.\n"
        )
    elif workflow.phase == "implementation":
        milestone = workflow.current_milestone()
        instructions = (
            "You hold the write turn for shared milestone: "
            f"{json.dumps(milestone, ensure_ascii=False)}\n"
            "Read previous contributions and judgments. Actually inspect and change files and run "
            "relevant checks. You may revise anyone's code within the agreed scope.\n"
            "Submit a checkpoint whenever useful, including partial work: peers inspect it before "
            "you continue. ready=false means a draft needing more work; ready=true requests "
            "milestone "
            "acceptance. Neither self-report completes a milestone without independent peer "
            "approval.\n"
            f'Checkpoint: {{"action":"contribute","version":{version},'
            f'"milestone_id":"{milestone["id"]}",'
            '"ready":true,"summary":"actual changes and response to feedback",'
            '"files":["existing/relative/path"],"tests":"commands and results, or not run"}.\n'
        )
    elif workflow.phase == "judging":
        point = state["checkpoint"]
        instructions = (
            f"Judge {point['author']}'s {point['milestone_id']} r{point['revision']} now. "
            "Do not wait until all milestones finish. Read the actual files, compare the "
            "requirements, "
            "and challenge correctness, design, tests, and prior feedback. Do not rubber-stamp.\n"
            "This turn is read-only. Give precise evidence, file locations, failure cases and a "
            "concrete improvement. A rejection returns to implementation; you may then fix the "
            "other agent's work, and that agent will judge your revision.\n"
            f'Verdict: {{"action":"judge_pass","version":{version},'
            f'"milestone_id":"{point["milestone_id"]}",'
            f'"revision":{point["revision"]},"evidence":"specific inspection and reasoning"}}.\n'
            "Use judge_fail with the same fields to request changes. Judge the actual readiness "
            "claim: a good partial draft may pass without completing the milestone.\n"
            "Tests are part of the work, not a courtesy. Name which of the proposal's acceptance "
            "criteria this checkpoint's tests actually exercise, and which they leave uncovered. "
            "Code you believe correct, whose tests would not catch a regression in an agreed "
            "criterion, is a judge_fail with the missing cases named.\n"
        )
    else:
        instructions = (
            "Read the ENTIRE integrated result, especially peers' code and interactions between "
            "milestones. Individual judgments do not replace this final integration review.\n"
            "Read-only turn: report issues rather than modifying files. All members review before "
            "the coordinator runs the agreed checks.\n"
            "Check coverage across the whole proposal: name any acceptance criterion that no test "
            "exercises. The agreed checks passing is not evidence of coverage, because a thin "
            "suite passes them exactly as readily as a thorough one.\n"
            f'Pass: {{"action":"review_pass","version":{version},"evidence":"files inspected"}}.\n'
            f'Request repairs: {{"action":"review_fail","version":{version},"milestone_ids":["T1"],'
            '"evidence":"specific defect, location, and requested improvement"}.\n'
        )
    return common + instructions + "\n" + STATE_MARKER + json.dumps(state, ensure_ascii=False)
