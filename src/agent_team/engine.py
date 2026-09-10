from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections.abc import Callable
from contextlib import aclosing
from datetime import UTC, datetime

from .acceptance import run_acceptance_checks
from .activity import observe_activity
from .adapters import (
    Adapter,
    AdapterError,
    QuotaExceeded,
    SessionUnavailable,
    make_adapter,
    reset_timestamp,
)
from .config import TeamConfig
from .consensus import write_consensus
from .context import PASS, build_prompt
from .sessions import Sessions
from .store import Store
from .workflow import Workflow, parse_action

QUOTA_RESET_BUFFER_SECONDS = 30
QUOTA_PROBE_PROMPT = (
    "Availability check only. Reply with [[PASS]] to confirm you can respond. "
    "Do not use tools, modify files, discuss the project, or issue workflow actions."
)


class Room:
    """Single-event-loop coordinator. Only this owner can commit an agent turn."""

    def __init__(
        self,
        config: TeamConfig,
        store: Store,
        broadcast: Callable[[dict], None],
        adapters: dict[str, Adapter] | None = None,
    ) -> None:
        self.config, self.store, self.broadcast = config, store, broadcast
        self.messages = store.messages()
        saved = next((m["workflow"] for m in reversed(self.messages) if "workflow" in m), None)
        self.workflow = Workflow(config, saved) if config.workflow == "build" else None
        if self.workflow and saved and self.workflow.phase in {"review", "acceptance"}:
            self.workflow.data.update(phase="review", review_approvals=[])
        if self.workflow and saved and self.workflow.phase == "judging":
            self.workflow.data["checkpoint"]["approvals"] = []
        self.adapters = adapters or {
            agent.name: make_adapter(
                agent, config.workspace, permission_mode=config.permission_mode
            )
            for agent in config.agents
        }
        self.sessions = Sessions(config, store)
        self.cursor = 0
        self.turns = 0
        self.next_target: str | None = None
        self.passes: set[str] = set()
        self.manual_paused = bool(self.messages)
        self.reason = "restart" if self.messages else "waiting"
        self.revision = 0
        self.active: dict | None = None
        self.active_task: asyncio.Task | None = None
        self.wake = asyncio.Event()
        self.runner: asyncio.Task | None = None
        self.closed = False
        self.single_step = False
        self.document_versions: set[int] = set()
        self.document_error: str | None = None
        self.quotas: dict[str, dict] = {}
        self.quota_retries: set[str] = set()
        self.quota_timer: asyncio.TimerHandle | None = None
        backends = {a.name: a.backend for a in config.agents}
        for event in store.events():
            if event["type"] == "agent.quota":
                name, quota = event["speaker"], event["quota"]
                if quota and quota["backend"] == backends.get(name):
                    # Old fixed-delay deadlines are not provider reset information.
                    if quota.get("retry_source") != "provider":
                        quota = {
                            **quota,
                            "retry_at": None,
                            "resets_at": None,
                            "retry_source": "unknown",
                        }
                    self.quotas[name] = quota
                else:
                    self.quotas.pop(name, None)

    def emit(self, kind: str, *, durable: bool = True, **data) -> dict:
        event = self.store.append(kind, **data) if durable else {"type": kind, **data}
        self.broadcast(event)
        return event

    def status(self) -> dict:
        return {
            "agents": [{"name": a.name, "backend": a.backend} for a in self.config.agents],
            "paused": self.manual_paused
            or bool(self.quotas)
            or not self.messages
            or self.reason == "completed",
            "reason": self.reason,
            "turns": self.turns,
            "active": self.active,
            "messages": len(self.messages),
            "workflow": self.workflow.snapshot() if self.workflow else None,
            "context_mode": self.config.context_mode,
            "permission_mode": self.config.permission_mode,
            "interaction_mode": "serial",
            "writer": self.active["speaker"]
            if self.active and self.active["phase"] in {"implementation", "acceptance"}
            else None,
            "sessions": self.store.sessions(),
            "quotas": {
                name: {**quota, "retrying": name in self.quota_retries}
                for name, quota in self.quotas.items()
            },
            "consensus_documents": {
                "ready_versions": sorted(self.document_versions),
                "error": self.document_error,
            },
        }

    def state(self) -> None:
        self.schedule_quota_retry()
        self.emit("state", durable=False, **self.status())

    def publish_system(self, text: str) -> None:
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

    def quota_blocked(self, name: str) -> bool:
        return name in self.quotas and name not in self.quota_retries

    def clear_quota(self, name: str) -> None:
        self.quota_retries.discard(name)
        if self.quotas.pop(name, None) is not None:
            self.emit("agent.quota", speaker=name, quota=None)

    def quota_succeeded(self, name: str) -> None:
        if name in self.quotas:
            self.clear_quota(name)
            self.publish_system(f"{name} recovered from its usage limit and rejoined the team.")
            if not self.quotas and not self.manual_paused:
                self.reason = "running"

    def record_quota(self, name: str, error: Exception, turn_id: str) -> bool:
        backend = next((a.backend for a in self.config.agents if a.name == name), None)
        if not isinstance(error, QuotaExceeded) or backend not in {"claude", "codex"}:
            return False
        now = time.time()
        resets_at = reset_timestamp(error.resets_at)
        if resets_at is not None and resets_at <= now:
            resets_at = None
        retry_at = resets_at + QUOTA_RESET_BUFFER_SECONDS if resets_at is not None else None
        quota = {
            "backend": backend,
            "error": str(error),
            "retry_at": retry_at,
            "retry_source": "provider" if resets_at is not None else "unknown",
            "resets_at": resets_at,
            "limit_type": error.limit_type,
        }
        self.quotas[name] = quota
        self.quota_retries.discard(name)
        self.passes.clear()
        self.emit("agent.quota", speaker=name, quota=quota)
        self.emit("error", speaker=name, turn_id=turn_id, text=str(error), quota=True)
        if self.single_step:
            self.manual_paused, self.reason = True, "step_complete"
        elif not self.manual_paused:
            self.reason = "quota"
        timing = (
            "No usable reset time was received; use /retry or /resume after resolving the limit."
            if retry_at is None
            else "A recovery check is scheduled for "
            f"{datetime.fromtimestamp(retry_at, UTC).isoformat()} "
            f"(provider reset plus {QUOTA_RESET_BUFFER_SECONDS} seconds), unless manually paused."
        )
        self.single_step, self.next_target = False, None
        self.publish_system(
            f"{name} reached its {backend} usage limit. The entire team is paused; active turns "
            f"are being interrupted. {timing} Discussion and work resume only after all limited "
            "members pass recovery checks. Partial file changes remain; "
            "required votes are not waived."
        )
        return True

    def retry_due_quotas(self) -> None:
        if self.closed or self.manual_paused:
            return
        for name, quota in self.quotas.items():
            if (
                quota["retry_at"] is not None
                and quota["retry_at"] <= time.time()
                and name not in self.quota_retries
            ):
                self.quota_retries.add(name)
                self.on_quota_retry(name)
                self.publish_system(f"Checking {name} after the provider's quota reset.")

    def on_quota_retry(self, name: str) -> None:
        self.passes.clear()

    def schedule_quota_retry(self) -> None:
        if self.quota_timer:
            self.quota_timer.cancel()
            self.quota_timer = None
        if self.closed or self.manual_paused or not self.messages:
            return
        due = [
            quota["retry_at"]
            for name, quota in self.quotas.items()
            if quota["retry_at"] is not None and name not in self.quota_retries
        ]
        if due:
            self.quota_timer = asyncio.get_running_loop().call_later(
                max(0, min(due) - time.time()), self.wake.set
            )

    def retry_quotas(self, target: str | None) -> None:
        for name in list(self.quotas):
            if target is None or target == name:
                self.quota_retries.add(name)
                self.on_quota_retry(name)

    def choose(self, target: str | None = None) -> str:
        return (
            self.workflow.choose(self.cursor, target)
            if self.workflow
            else target or list(self.adapters)[self.cursor]
        )

    def start(self) -> None:
        self.recover_consensus_records()
        self.ensure_consensus_documents()
        self.runner = asyncio.create_task(self.run(), name="room-coordinator")
        self.emit("room.started", recovered=bool(self.messages))

    def recover_consensus_records(self) -> None:
        if not self.workflow or self.workflow.data["consensus_history"]:
            return
        history = self.workflow.data["consensus_history"]
        versions = set()
        latest_state = None
        for message in self.messages:
            saved = message.get("workflow")
            if (
                not saved
                or saved["phase"] == "discussion"
                or not saved["proposal"]
                or set(saved["approvals"]) != set(self.workflow.members)
                or saved["objections"]
            ):
                continue
            latest_state = saved
            if saved["version"] in versions:
                continue
            previous = Workflow(self.config, saved)
            previous.data["document_namespace"] = self.workflow.data["document_namespace"]
            previous.data["consensus_history"] = history.copy()
            history.append(previous.confirm_consensus(recovered=True))
            versions.add(saved["version"])
        if self.workflow.phase != "discussion" and self.workflow.data["version"] not in versions:
            self.workflow.confirm_consensus(recovered=True)
        if history:
            if (
                latest_state
                and self.workflow.phase == "discussion"
                and not self.workflow.data["revision_base"]
            ):
                previous = Workflow(self.config, latest_state)
                previous.reconsider()
                self.workflow.data["revision_base"] = previous.data["revision_base"]
            latest = history[-1]
            self.messages.append(
                self.emit(
                    "message",
                    role="system",
                    speaker="system",
                    text=f"Recovered {len(history)} existing consensus records. "
                    f"Latest: v{latest['version']} at {latest['document']}",
                    workflow=self.workflow.snapshot(),
                )
            )

    def ensure_consensus_documents(self) -> bool:
        """Replay durable document intents before any further model or check work."""
        if not self.workflow:
            return True
        for record in self.workflow.data["consensus_history"]:
            if record["version"] in self.document_versions:
                continue
            try:
                write_consensus(
                    self.config.workspace, self.workflow.data["document_namespace"], record
                )
            except (OSError, ValueError) as exc:
                self.document_error = str(exc)
                self.manual_paused, self.reason = True, "document_error"
                self.emit(
                    "error",
                    speaker="system",
                    text=f"Consensus document could not be saved: {exc}. "
                    "Resolve the document path, then /resume. "
                    "The approved record remains in public history.",
                )
                return False
            self.document_versions.add(record["version"])
            self.emit("consensus.saved", version=record["version"], document=record["document"])
        self.document_error = None
        return True

    def cancel_active(self) -> None:
        self.revision += 1  # Revoke authority before asking the process to stop.
        if self.active_task and not self.active_task.done() and not self.active_task.cancelling():
            self.active_task.cancel()

    def say(self, speaker: str, text: str) -> None:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Message must be nonempty text")
        self.cancel_active()
        data = {}
        if self.workflow:
            candidate = self.workflow.clone()
            candidate.reconsider(speaker=speaker, reason=text.strip())
            data["workflow"] = candidate.snapshot()
        self.messages.append(
            self.emit("message", role="user", speaker=speaker, text=text.strip(), **data)
        )
        if self.workflow:
            self.workflow = candidate
            self.emit(
                "workflow.changed",
                durable=False,
                text="New human guidance: discuss and confirm the plan again.",
                workflow=self.workflow.snapshot(),
            )
        self.next_target = None
        self.single_step = False
        self.passes.clear()
        if self.reason in {"blocked", "completed", "all_passed", "no_consensus"}:
            self.manual_paused = False
        if not self.manual_paused:
            self.reason = "quota" if self.quotas else "running"
        self.state()
        self.wake.set()

    def control(self, action: str, target: str | None = None) -> None:
        if action == "reset-session":
            names = [a.name for a in self.config.agents]
            if target is not None and target not in names:
                raise ValueError(f"Unknown agent: {target}")
            if self.active is not None:
                raise ValueError("Use /interrupt and wait for the floor before resetting sessions")
            for name in [target] if target else names:
                self.sessions.invalidate(name, "Private session reset by the human")
            self.manual_paused, self.reason = True, "user"
            self.next_target = None
        elif action in {"pause", "interrupt"}:
            self.manual_paused = True
            self.reason = "user"
            if action == "interrupt":
                self.cancel_active()
        elif action in {"resume", "next", "retry"}:
            if action == "resume" and not self.status()["paused"]:
                return
            if not self.messages:
                raise ValueError("Send an idea first")
            if action == "next" and self.active is not None:
                raise ValueError(
                    "An agent has the floor; /interrupt and wait for release before /next"
                )
            if self.workflow and self.workflow.phase == "completed":
                raise ValueError("Idea completed; send a new idea or revision to collaborate again")
            if self.workflow and self.workflow.phase == "acceptance" and action == "next":
                raise ValueError("Acceptance checks are pending; use /resume")
            if target is not None and target not in self.adapters:
                raise ValueError(f"Unknown agent: {target}")
            if self.quotas and self.active:
                raise ValueError("Wait for interrupted turns to stop before retrying the team")
            if self.quotas and action == "next":
                raise ValueError("Usage limits pause the entire team; use /retry or /resume first")
            if action == "next" and target:
                names = [a.name for a in self.config.agents]
                if self.workflow:
                    self.workflow.choose(self.cursor, target)
                self.cursor = names.index(target)
            self.retry_quotas(target)
            self.manual_paused = False
            self.single_step = action == "next"
            self.next_target = target if self.single_step else None
            self.reason = "running"
            self.passes.clear()
        else:
            raise ValueError(f"Unknown control action: {action}")
        if self.quotas and not self.manual_paused:
            self.reason = "quota"
        self.emit("room.control", action=action, target=target)
        self.state()
        self.wake.set()

    async def execute_turn(
        self,
        name: str,
        prompt: str,
        turn_id: str,
        revision: int,
        phase: str,
        session_plan: dict | None,
    ):
        def warn(idle_seconds: float) -> None:
            if revision == self.revision and self.active and self.active["turn_id"] == turn_id:
                self.emit(
                    "turn.idle",
                    durable=False,
                    speaker=name,
                    turn_id=turn_id,
                    phase=self.active["phase"],
                    idle_seconds=idle_seconds,
                    text=f"No observable output for {idle_seconds:.0f} seconds. "
                    "Still waiting; silence does not prove the process is stuck. "
                    "Use /interrupt to cancel.",
                )

        async with observe_activity(
            self.config.idle_warning_seconds,
            warn,
            on_activity=lambda: self.report_activity(name, turn_id, revision),
        ) as activity:
            if phase == "acceptance":
                return await self.run_acceptance(turn_id, activity)
            return await self.collect(
                name, prompt, turn_id, revision, phase, session_plan, activity
            )

    def report_activity(self, name: str, turn_id: str, revision: int) -> None:
        if not self.closed and revision == self.revision:
            self.emit("turn.activity", durable=False, speaker=name, turn_id=turn_id)

    async def collect(
        self,
        name: str,
        prompt: str,
        turn_id: str,
        revision: int,
        phase: str,
        session_plan: dict | None = None,
        on_activity: Callable[[], None] | None = None,
    ) -> tuple[str, tuple[str, dict] | None]:
        parts: list[str] = []
        timeout = (
            self.config.work_timeout
            if phase in {"implementation", "judging", "review"}
            else self.config.turn_timeout
        )
        options = {}
        if session_plan is not None:
            options = {"persist_session": True, "session_id": session_plan["session_id"]}
            if getattr(self.adapters[name], "supports_session_notifications", False):
                options["on_session"] = self.session_observer(name, turn_id, revision)
        elif phase == "recovery" and getattr(self.adapters[name], "supports_sessions", False):
            options = {"persist_session": False}
        if getattr(self.adapters[name], "supports_activity", False):
            options["on_activity"] = on_activity
        try:
            async with asyncio.timeout(timeout or None):
                async with aclosing(
                    self.adapters[name].stream(prompt, phase=phase, **options)
                ) as stream:
                    async for delta in stream:
                        if revision != self.revision:
                            raise asyncio.CancelledError
                        if not isinstance(delta, str):
                            raise AdapterError("adapter must yield strings")
                        if on_activity:
                            on_activity()
                        parts.append(delta)
                        if phase != "recovery":
                            self.emit(
                                "delta", durable=False, speaker=name, turn_id=turn_id, text=delta
                            )
        except SessionUnavailable:
            if revision != self.revision:
                raise asyncio.CancelledError from None
            if parts or session_plan is None or not session_plan["session_id"]:
                raise
            # Only a definite rejection BEFORE any backend activity is safe to retry.
            self.sessions.invalidate(name, "Saved backend session is unavailable")
            agent = next(a for a in self.config.agents if a.name == name)
            through = self.active["context_through"]
            fresh = self.sessions.plan(agent, through, {m["id"] for m in self.messages})
            prompt = build_prompt(agent, self.config, self.messages, self.workflow)
            self.active.update(context_mode="full", session_generation=fresh["generation"])
            self.sessions.begin(name, fresh, turn_id, through)
            self.emit(
                "session.rebuilt",
                speaker=name,
                turn_id=turn_id,
                text="Saved session unavailable; rebuilding from the full public conversation.",
                prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
            )
            self.state()
            return await self.collect(name, prompt, turn_id, revision, phase, fresh, on_activity)
        if revision != self.revision:
            raise asyncio.CancelledError
        reply = "".join(parts).strip()
        if not reply:
            raise AdapterError("Agent returned an empty reply")
        update = (
            self.sessions.completed(name, turn_id, self.adapters[name].result_session_id)
            if session_plan is not None
            else None
        )
        return reply, update

    def session_observer(self, name: str, turn_id: str, revision: int):
        def remember(identifier: str) -> None:
            if self.closed or revision != self.revision:
                raise asyncio.CancelledError
            self.sessions.bind(name, turn_id, identifier)

        return remember

    def accept_reply(
        self,
        speaker: str,
        reply: str,
        turn_id: str,
        session_update: tuple[str, dict] | None = None,
    ) -> None:
        data = {}
        note = ""
        if self.workflow:
            display, action = parse_action(reply)
            candidate = self.workflow.clone()
            note = candidate.apply(speaker, action)
            if self.workflow.phase == "discussion" and candidate.phase == "implementation":
                record = candidate.confirm_consensus()
                note += f" Consensus document: {record['document']}"
            data = {"workflow": candidate.snapshot(), "action": action}
            reply = display or note or "Turn processed."
        self.messages.append(
            self.emit(
                "message",
                role="agent",
                speaker=speaker,
                text=reply,
                turn_id=turn_id,
                context_through=self.active["context_through"],
                session_update=session_update,
                **data,
            )
        )
        if self.workflow:
            self.workflow = candidate
            if note.startswith("blocked:"):
                self.manual_paused, self.reason = True, "blocked"
            if note:
                self.emit(
                    "workflow.changed", durable=False, text=note, workflow=candidate.snapshot()
                )
            self.ensure_consensus_documents()

    async def run_acceptance(
        self, turn_id: str, on_activity: Callable[[], None] | None = None
    ) -> list[dict]:
        return await run_acceptance_checks(
            self.workflow.data["proposal"]["acceptance_checks"],
            self.config.workspace,
            self.config.acceptance_timeout,
            lambda text: self.emit(
                "delta", durable=False, speaker="system", turn_id=turn_id, text=text
            ),
            on_activity=on_activity,
        )

    def record_acceptance(self, results: list[dict], turn_id: str) -> None:
        candidate = self.workflow.clone()
        note = candidate.accepted(results)
        details = "\n".join(
            f"{r['command']!r} → exit {r['exit_code']}\n{r['output']}" for r in results
        )
        self.messages.append(
            self.emit(
                "message",
                role="system",
                speaker="system",
                text=note + "\n" + details,
                turn_id=turn_id,
                workflow=candidate.snapshot(),
            )
        )
        self.workflow = candidate
        self.emit("workflow.changed", durable=False, text=note, workflow=candidate.snapshot())
        if candidate.phase == "completed":
            self.manual_paused, self.reason = True, "completed"

    async def run(self) -> None:
        while not self.closed:
            await self.wake.wait()
            self.wake.clear()
            if self.closed:
                break
            if self.manual_paused or not self.messages:
                continue
            self.retry_due_quotas()
            recovering = bool(self.quotas)
            if not recovering and not self.ensure_consensus_documents():
                self.state()
                continue
            phase = (
                "recovery" if recovering else self.workflow.phase if self.workflow else "discussion"
            )
            accepting = phase == "acceptance"
            if phase == "completed":
                self.manual_paused, self.reason = True, "completed"
                self.state()
                continue
            name = (
                next((n for n in self.adapters if n in self.quota_retries), None)
                if recovering
                else "system"
                if accepting
                else self.choose(self.next_target)
            )
            self.next_target = None
            if name is None:
                self.reason = "quota"
                self.state()
                continue
            agent = next((a for a in self.config.agents if a.name == name), None)
            turn_id = uuid.uuid4().hex
            through = self.messages[-1]["id"]
            session_plan = None
            if not accepting and not recovering:
                if self.config.context_mode == "session" and getattr(
                    self.adapters[name], "supports_sessions", False
                ):
                    session_plan = self.sessions.plan(
                        agent, through, {m["id"] for m in self.messages}
                    )
                else:
                    # Full-context work can supersede dormant private history.
                    self.sessions.invalidate(name, "Full-context mode or stateless backend")
            try:
                prompt = (
                    QUOTA_PROBE_PROMPT
                    if recovering
                    else "Execute unanimously accepted checks"
                    if accepting
                    else build_prompt(
                        agent,
                        self.config,
                        self.messages,
                        self.workflow,
                        after=session_plan["synced_through"] if session_plan else 0,
                        resumed=bool(session_plan and session_plan["session_id"]),
                        recovering=bool(session_plan and session_plan.get("dirty")),
                    )
                )
            except ValueError as exc:
                self.manual_paused, self.reason = True, "error"
                self.emit("error", speaker="system", text=str(exc))
                self.state()
                continue
            revision = self.revision
            self.active = {
                "speaker": name,
                "turn_id": turn_id,
                "context_through": through,
                "phase": phase,
                "context_mode": "incremental"
                if session_plan and session_plan["session_id"]
                else "full",
                "session_generation": session_plan["generation"] if session_plan else None,
            }
            self.emit(
                "floor.granted",
                **self.active,
                prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
            )
            if session_plan is not None:
                self.sessions.begin(name, session_plan, turn_id, through)
            self.state()
            adapter_phase = "planning" if self.workflow and phase == "discussion" else phase
            self.active_task = asyncio.create_task(
                self.execute_turn(name, prompt, turn_id, revision, adapter_phase, session_plan),
                name=f"turn-{name}",
            )
            outcome = "cancelled"
            try:
                result = await self.active_task
                reply, session_update = (result, None) if accepting else result
                if revision == self.revision and not self.closed:
                    self.turns += 1
                    if not accepting:
                        self.cursor = ([a.name for a in self.config.agents].index(name) + 1) % len(
                            self.config.agents
                        )
                    if recovering:
                        outcome = "recovered"
                    elif accepting:
                        self.record_acceptance(reply, turn_id)
                        outcome = "completed"
                    elif reply == PASS and (not self.workflow or phase == "discussion"):
                        outcome = "passed"
                        self.passes.add(name)
                        self.emit(
                            "agent.passed",
                            speaker=name,
                            turn_id=turn_id,
                            session_update=session_update,
                        )
                    else:
                        outcome = "completed"
                        self.passes.clear()
                        self.accept_reply(name, reply, turn_id, session_update)
                    if not accepting:
                        self.quota_succeeded(name)
                    if not self.manual_paused:
                        if len(self.passes) == len(self.config.agents):
                            self.manual_paused = True
                            self.reason = "no_consensus" if self.workflow else "all_passed"
                        elif self.single_step:
                            self.manual_paused, self.reason = True, "step_complete"
            except asyncio.CancelledError:
                # User steering invalidates this turn, not the coordinator itself.
                pass
            except Exception as exc:
                if not self.closed and (
                    revision == self.revision or isinstance(exc, QuotaExceeded)
                ):
                    outcome = "failed"
                    if not self.record_quota(name, exc, turn_id):
                        self.clear_quota(name)
                        self.manual_paused, self.reason = True, "error"
                        detail = "Turn timed out" if isinstance(exc, TimeoutError) else str(exc)
                        self.emit("error", speaker=name, turn_id=turn_id, text=detail)
                    if self.workflow and not isinstance(exc, QuotaExceeded):
                        self.messages.append(
                            self.emit(
                                "message",
                                role="system",
                                speaker="system",
                                text=f"{name} failed: {detail}. "
                                "Partial changes may remain; inspect them before resuming.",
                                workflow=self.workflow.snapshot(),
                            )
                        )
            finally:
                if not accepting and outcome not in {"completed", "passed", "recovered"}:
                    self.quota_retries.discard(name)
                    if not recovering:
                        self.sessions.suspend(
                            name, "Invocation cancelled, failed, or not committed"
                        )
                self.emit("floor.released", speaker=name, turn_id=turn_id, outcome=outcome)
                self.active, self.active_task = None, None
                self.state()
            if not self.closed and not self.manual_paused:
                # A command wakes this delay immediately. There is still only one floor owner.
                try:
                    await asyncio.wait_for(self.wake.wait(), self.config.turn_delay)
                except TimeoutError:
                    pass
                self.wake.set()

    async def close(self) -> None:
        self.closed = True
        self.schedule_quota_retry()
        self.cancel_active()
        self.wake.set()
        if self.runner:
            await self.runner
        self.emit("room.stopped")
