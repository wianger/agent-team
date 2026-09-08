"""Event-driven resident members: concurrent conversation, exclusive workspace writes."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from contextlib import aclosing
from dataclasses import dataclass, field

from .activity import observe_activity
from .adapters import AdapterError
from .context import PASS, build_prompt
from .engine import Room
from .resident import make_resident
from .workflow import Workflow, parse_action


@dataclass
class Turn:
    speaker: str
    phase: str
    lane: str
    through: int
    fence: tuple
    workflow: Workflow | None
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def public(self):
        return {
            "speaker": self.speaker,
            "phase": self.phase,
            "lane": self.lane,
            "turn_id": self.turn_id,
            "context_through": self.through,
        }


@dataclass
class Member:
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    runner: asyncio.Task | None = None
    invocation: asyncio.Task | None = None
    active: Turn | None = None
    seen: tuple | None = None
    error: str | None = None


class ChatRoom(Room):
    def __init__(self, config, store, broadcast, adapters=None):
        super().__init__(config, store, broadcast, adapters)
        if adapters is None:
            self.adapters = {
                a.name: make_resident(a, config.workspace, permission_mode=config.permission_mode)
                for a in config.agents
            }
        self.members = {a.name: Member() for a in config.agents}
        self.members["system"] = Member()
        self.writer: str | None = None
        self.shutdown = asyncio.Event()

    def fence(self):
        if not self.workflow:
            return (self.revision,)
        state = self.workflow.data
        point = state["checkpoint"] or {}
        return (
            self.revision,
            state["version"],
            state["phase"],
            state.get("chat_epoch", 0),
            point.get("task_id"),
            point.get("revision"),
        )

    def status(self):
        state = super().status()
        turns = [m.active.public() for m in self.members.values() if m.active]
        state.update(
            interaction_mode="chatroom",
            active=turns[0] if turns else None,
            active_turns=turns,
            writer=self.writer,
            runtimes={
                name: {
                    "state": "failed" if m.error else "thinking" if m.active else "waiting",
                    "error": m.error,
                    "pid": getattr(getattr(self.adapters.get(name), "process", None), "pid", None),
                    "pending_messages": sum(
                        msg["id"] > (m.active.through if m.active else m.seen[0] if m.seen else 0)
                        for msg in self.messages
                    ),
                }
                for name, m in self.members.items()
                if name != "system"
            },
        )
        return state

    def start(self):
        for name, member in self.members.items():
            member.runner = asyncio.create_task(self.member_loop(name), name=f"member-{name}")
        super().start()

    def refresh_active(self):
        self.active = next((m.active.public() for m in self.members.values() if m.active), None)

    def cancel_active(self, *, exclude=None):
        self.revision += 1
        for member in self.members.values():
            if (
                member.invocation
                and member.active.turn_id != exclude
                and not member.invocation.done()
                and not member.invocation.cancelling()
            ):
                member.invocation.cancel()

    def say(self, speaker, text):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Message must be nonempty text")
        if self.reason in {"completed", "blocked"}:
            self.redirect(speaker, text)
            return
        self.messages.append(
            self.emit(
                "message",
                role="user",
                speaker=speaker,
                text=text.strip(),
                **({"workflow": self.workflow.snapshot()} if self.workflow else {}),
            )
        )
        if not self.manual_paused:
            self.reason = "running"
        self.state()
        self.wake.set()

    def redirect(self, speaker, text):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Redirect requires nonempty guidance")
        self.cancel_active()
        if self.workflow:
            candidate = Workflow(self.config, self.workflow.snapshot())
            candidate.reconsider(speaker=speaker, reason=text.strip())
            candidate.data["chat_epoch"] = candidate.data.get("chat_epoch", 0) + 1
            self.workflow = candidate
        if self.reason in {"completed", "blocked"}:
            self.manual_paused = False
        self.messages.append(
            self.emit(
                "message",
                role="user",
                speaker=speaker,
                text=text.strip(),
                redirect=True,
                **({"workflow": self.workflow.snapshot()} if self.workflow else {}),
            )
        )
        self.reason = "user" if self.manual_paused else "running"
        self.state()
        self.wake.set()

    def control(self, action, target=None):
        names = list(self.adapters)
        if target is not None and target not in names:
            raise ValueError(f"Unknown agent: {target}")
        if action in {"pause", "interrupt"}:
            self.manual_paused, self.reason = True, "user"
            if action == "interrupt":
                self.cancel_active()
        elif action == "reset-session":
            if self.active:
                raise ValueError("Use /interrupt and wait for all active turns before resetting")
            for name in [target] if target else names:
                self.sessions.invalidate(name, "Private session reset by the human")
                self.members[name].seen = None
            self.manual_paused, self.reason = True, "user"
        elif action in {"resume", "next", "retry"}:
            if not self.messages:
                raise ValueError("Send an idea first")
            if self.workflow and self.workflow.phase == "completed":
                raise ValueError("Idea completed; send new guidance")
            if action == "next" and self.active:
                raise ValueError("Use /interrupt and wait for all active turns before /next")
            if action == "next" and self.workflow:
                if self.workflow.phase == "verification":
                    raise ValueError("Acceptance checks are pending; use /resume")
                if target:
                    self.workflow.choose(self.cursor, target)
            for name in [target] if target else names + ["system"]:
                self.members[name].error = None
                self.members[name].seen = None
            self.manual_paused, self.reason = False, "running"
            self.single_step = action == "next"
            self.next_target = target if self.single_step else None
        else:
            raise ValueError(f"Unknown control action: {action}")
        self.emit("room.control", action=action, target=target)
        self.state()
        self.wake.set()

    def launch(self, name, phase, lane, *, force=False):
        member = self.members[name]
        head, fence = self.messages[-1]["id"], self.fence()
        if member.active or member.error or (not force and member.seen == (head, fence, lane)):
            return False
        flow = Workflow(self.config, self.workflow.snapshot()) if self.workflow else None
        turn = Turn(name, phase, lane, head, fence, flow)
        member.active = turn
        if phase in {"implementation", "verification"} and lane == "work":
            if self.writer is not None:
                raise RuntimeError("Workspace write lease already held")
            self.writer = name
        member.queue.put_nowait(turn)
        self.refresh_active()
        self.emit("turn.started", **turn.public())
        return True

    def dispatch(self):
        if not self.messages:
            return
        if self.manual_paused and self.document_error:
            return  # A failed document export is retried only after an explicit resume.
        phase = self.workflow.phase if self.workflow else "discussion"
        work = [m.active for m in self.members.values() if m.active and m.active.lane == "work"]
        # A unanimous vote is provisional until already-running discussion work has
        # returned. An in-flight objection must not race with starting a writer.
        agreeing = (
            self.workflow
            and phase == "discussion"
            and (
                set(self.workflow.data["approvals"]) == set(self.adapters)
                and not self.workflow.data["objections"]
            )
        )
        if agreeing and not work:
            candidate = Workflow(self.config, self.workflow.snapshot())
            candidate.data["phase"] = phase = "implementation"
            record = candidate.confirm_consensus()
            self.messages.append(
                self.emit(
                    "message",
                    role="system",
                    speaker="system",
                    text=f"Unanimous agreement confirmed. Consensus v{record['version']}: "
                    f"{record['document']}. Shared implementation follows document publication.",
                    workflow=candidate.snapshot(),
                )
            )
            self.workflow = candidate
        if not work and not self.ensure_consensus_documents():
            return
        if self.manual_paused:
            return
        # A redirected or revised write/check turn retains its lease until cancellation
        # actually finishes. New formal readers must not inspect a changing workspace.
        if self.writer is not None:
            writing = self.members[self.writer].active
            if phase not in {"implementation", "verification"} or writing.fence[0] != self.revision:
                return
        if phase == "completed":
            self.manual_paused, self.reason = True, "completed"
            return
        if self.single_step:
            if work:
                return
            name = self.next_target or (
                self.workflow.choose(self.cursor)
                if self.workflow
                else list(self.adapters)[self.cursor]
            )
            self.launch(name, phase, "work", force=True)
            return
        if phase in {"implementation", "verification"}:
            if not work:
                if phase == "verification":
                    self.launch("system", phase, "work")
                else:
                    preferred = self.workflow.choose(self.cursor)
                    eligible = [preferred] + [n for n in self.adapters if n != preferred]
                    for name in eligible:
                        if not self.members[name].error:
                            self.launch(name, phase, "work")
                            break
            primary = {self.writer} if self.writer else set()
        else:
            # Drain formal readers of the previous checkpoint before starting the next
            # write/review generation. Conversation-only turns never approve anything.
            if phase != "discussion" and any(
                t.phase != phase or t.fence != self.fence() for t in work
            ):
                return
            primary = set(self.workflow.eligible() if self.workflow else self.adapters)
            if agreeing:
                return
        for name in self.adapters:
            if name in primary:
                if phase not in {"implementation", "verification"}:
                    self.launch(name, phase, "work")
            else:
                self.launch(name, phase, "chat")
        active = any(m.active for m in self.members.values())
        self.reason = "running" if active else "waiting_messages"
        if any(m.error for m in self.members.values()):
            self.reason = "degraded"

    async def run(self):
        while not self.closed:
            await self.wake.wait()
            self.wake.clear()
            if self.closed:
                break
            self.dispatch()
            self.state()

    def publish_system(self, text):
        self.messages.append(
            self.emit(
                "message",
                role="system",
                speaker="system",
                text=text,
                **({"workflow": self.workflow.snapshot()} if self.workflow else {}),
            )
        )
        self.wake.set()

    async def member_loop(self, name):
        member = self.members[name]
        while True:
            turn = await member.queue.get()
            if turn is None:
                return
            outcome = "cancelled"
            try:
                if self.closed or turn.fence[0] != self.revision:
                    continue
                member.invocation = asyncio.create_task(self.invoke(turn), name=f"thinking-{name}")
                outcome = await member.invocation
                # Its own publication is already in private memory and must not, by
                # itself, trigger another model call. Never skip intervening peer input.
                unseen = [m for m in self.messages if m["id"] > turn.through]
                seen = (
                    self.messages[-1]["id"]
                    if all(m.get("turn_id") == turn.turn_id for m in unseen)
                    else turn.through
                )
                member.seen = (seen, turn.fence, turn.lane)
                self.turns += 1
                if turn.lane == "work" and name != "system":
                    self.cursor = (list(self.adapters).index(name) + 1) % len(self.adapters)
                if self.single_step:
                    self.single_step = False
                    self.manual_paused, self.reason = True, "step_complete"
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                if turn.fence[0] == self.revision:
                    outcome = "failed"
                    member.error = "Turn timed out" if isinstance(exc, TimeoutError) else str(exc)
                    self.emit("error", speaker=name, turn_id=turn.turn_id, text=member.error)
                    self.publish_system(
                        f"{name} is unavailable: {member.error}. Other members may continue; "
                        "required votes are not waived. Use /retry after resolving the issue."
                    )
            finally:
                if name != "system" and outcome not in {"completed", "passed", "rejected"}:
                    self.sessions.invalidate(name, "Concurrent invocation did not commit")
                if self.writer == name:
                    self.writer = None
                member.active, member.invocation = None, None
                self.refresh_active()
                self.emit("turn.finished", **turn.public(), outcome=outcome)
                self.state()
                self.wake.set()
            # A per-member delay does not hold a global speaking lock or limit rounds.
            if not self.closed and self.config.turn_delay:
                try:
                    await asyncio.wait_for(self.shutdown.wait(), self.config.turn_delay)
                except TimeoutError:
                    pass

    async def invoke(self, turn):
        name = turn.speaker
        phase = (
            "chat"
            if turn.lane == "chat"
            else ("planning" if turn.workflow and turn.phase == "discussion" else turn.phase)
        )
        messages = [m for m in self.messages if m["id"] <= turn.through]
        session_plan = None
        adapter = self.adapters.get(name)
        if (
            adapter
            and self.config.context_mode == "session"
            and getattr(adapter, "supports_sessions", False)
        ):
            agent = next(a for a in self.config.agents if a.name == name)
            session_plan = self.sessions.plan(agent, turn.through, {m["id"] for m in messages})
        elif adapter:
            self.sessions.invalidate(name, "Full context or stateless backend")
        prompt = (
            "Execute agreed acceptance checks"
            if name == "system"
            else build_prompt(
                next(a for a in self.config.agents if a.name == name),
                self.config,
                messages,
                turn.workflow,
                after=session_plan["synced_through"] if session_plan else 0,
                resumed=bool(session_plan and session_plan["session_id"]),
                concurrent=True,
                lane=turn.lane,
                known_own_messages=tuple((session_plan or {}).get("known_own_messages", [])),
            )
        )
        self.emit(
            "turn.context",
            **turn.public(),
            prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
        )
        if session_plan:
            self.sessions.begin(name, session_plan, turn.turn_id, turn.through)

        def warn(idle):
            if turn.fence[0] == self.revision:
                self.emit(
                    "turn.idle",
                    durable=False,
                    **turn.public(),
                    idle_seconds=idle,
                    text="No observable output; still waiting. Use /interrupt to cancel.",
                )

        async with observe_activity(self.config.idle_warning_seconds, warn) as activity:
            if name == "system":
                results = await self.verify(turn.turn_id, activity)
                if turn.fence[0] != self.revision:
                    raise asyncio.CancelledError
                self.accept_verification(results, turn.turn_id)
                return "completed"
            options = {}
            if session_plan:
                options.update(persist_session=True, session_id=session_plan["session_id"])
            elif getattr(adapter, "supports_sessions", False):
                options["persist_session"] = False
            if getattr(adapter, "supports_activity", False):
                options["on_activity"] = activity
            timeout = (
                self.config.work_timeout
                if phase in {"implementation", "judging", "review"}
                else self.config.turn_timeout
            )
            parts = []
            async with asyncio.timeout(timeout or None):
                async with aclosing(adapter.stream(prompt, phase=phase, **options)) as stream:
                    async for delta in stream:
                        if turn.fence[0] != self.revision:
                            raise asyncio.CancelledError
                        if not isinstance(delta, str):
                            raise AdapterError("Adapter deltas must be strings")
                        activity()
                        parts.append(delta)
                        self.emit("delta", durable=False, **turn.public(), text=delta)
            if turn.fence[0] != self.revision:
                raise asyncio.CancelledError
            reply = "".join(parts).strip()
            if not reply:
                raise AdapterError("Agent returned an empty reply")
            update = (
                self.sessions.completed(
                    name, turn.turn_id, adapter.result_session_id, concurrent=True
                )
                if session_plan
                else None
            )
            return self.accept_concurrent(turn, reply, update)

    def accept_concurrent(self, turn, reply, update):
        action, candidate, note, rejected = None, self.workflow, "", None
        if reply != PASS and self.workflow:
            reply, action = parse_action(reply)
            if action:
                if (
                    turn.lane != "work"
                    and action["action"] != "request_revision"
                    or turn.fence != self.fence()
                ):
                    rejected = (
                        "Workflow changed or this turn has no formal work assignment; "
                        "synchronize and reconsider."
                    )
                else:
                    # Duplicate approvals must not manufacture new messages and wake each
                    # other forever. A later objection remains a meaningful action.
                    if (
                        action["action"] == "approve"
                        and turn.speaker in self.workflow.data["approvals"]
                    ):
                        reply, action = PASS, None
                    else:
                        candidate = Workflow(self.config, self.workflow.snapshot())
                        note = candidate.apply(turn.speaker, action)
                        if action["action"] in {
                            "propose",
                            "object",
                            "contribute",
                            "task_done",
                            "judge_fail",
                            "review_fail",
                            "request_revision",
                        }:
                            candidate.data["chat_epoch"] = candidate.data.get("chat_epoch", 0) + 1
                        if (
                            self.workflow.phase == "discussion"
                            and candidate.phase == "implementation"
                        ):
                            candidate.data["phase"] = "discussion"
                            note = "All members approved; awaiting in-flight discussion."
            elif turn.lane == "work" and turn.phase in {"implementation", "judging", "review"}:
                raise ValueError("Formal work requires a valid team-action")
        if reply == PASS:
            if self.workflow and turn.lane == "work" and turn.phase != "discussion":
                raise ValueError("Formal work requires a checkpoint, verdict, or explicit blocker")
            self.emit(
                "agent.passed", speaker=turn.speaker, turn_id=turn.turn_id, session_update=update
            )
            return "passed"
        data = {}
        if self.workflow:
            data = {"workflow": candidate.snapshot(), "action": None if rejected else action}
        if rejected:
            data.update(rejected_action=action, rejection=rejected)
        self.messages.append(
            self.emit(
                "message",
                role="agent",
                speaker=turn.speaker,
                text=reply or note or rejected or "Turn processed.",
                turn_id=turn.turn_id,
                context_through=turn.through,
                lane=turn.lane,
                session_update=update,
                **data,
            )
        )
        if candidate:
            self.workflow = candidate
        if action and action["action"] == "request_revision" and not rejected:
            self.cancel_active(exclude=turn.turn_id)
        if note.startswith("blocked:"):
            self.manual_paused, self.reason = True, "blocked"
        if note or rejected:
            self.emit(
                "workflow.changed",
                durable=False,
                text=rejected or note,
                workflow=self.workflow.snapshot(),
            )
        self.wake.set()
        return "rejected" if rejected else "completed"

    async def close(self):
        self.closed = True
        self.shutdown.set()
        self.cancel_active()
        self.wake.set()
        if self.runner:
            await self.runner
        for member in self.members.values():
            member.queue.put_nowait(None)
        await asyncio.gather(*(m.runner for m in self.members.values() if m.runner))
        await asyncio.gather(*(a.close() for a in self.adapters.values() if hasattr(a, "close")))
        self.emit("room.stopped")
