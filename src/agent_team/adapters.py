from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .config import AgentConfig
from .context import PASS, TRANSCRIPT_MARKER
from .sessions import session_id as validate_session_id
from .streams import iter_lines
from .workflow import STATE_MARKER, action_reply


class AdapterError(RuntimeError):
    pass


class QuotaExceeded(AdapterError):
    """A native provider explicitly rejected work because usage was exhausted."""

    def __init__(self, message: str, *, resets_at=None, limit_type=None):
        super().__init__(message)
        self.resets_at = resets_at
        self.limit_type = limit_type if isinstance(limit_type, str) else None


def reset_timestamp(value):
    """Validate provider Unix seconds without interpreting strings or milliseconds."""
    if type(value) in (int, float) and value > 0:
        try:
            datetime.fromtimestamp(value + 86400, UTC)  # Allow local-time formatting.
        except (ValueError, OverflowError, OSError):
            pass
        else:
            return value
    return None


def codex_quota_reset(response):
    """Use an unambiguous bucket, waiting for every explicitly exhausted window."""
    if not isinstance(response, dict):
        return None, None
    buckets = response.get("rateLimitsByLimitId")
    if isinstance(buckets, dict) and buckets:
        if len(buckets) != 1:
            return None, None  # No reliable bucket selection; do not guess from the model name.
        snapshot = next(iter(buckets.values()))
    else:
        snapshot = response.get("rateLimits")
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("spendControlReached")
        or snapshot.get("rateLimitReachedType") not in (None, "rate_limit_reached")
    ):
        return None, None
    exhausted = {}
    for name in ("primary", "secondary"):
        window = snapshot.get(name)
        if window is None:
            continue
        if not isinstance(window, dict) or type(window.get("usedPercent")) not in (int, float):
            return None, None
        used = window["usedPercent"]
        if not 0 <= used < float("inf"):
            return None, None
        if used >= 100:
            reset = reset_timestamp(window.get("resetsAt"))
            if reset is None:
                return None, None
            exhausted[name] = reset
    return (max(exhausted.values()), "+".join(exhausted)) if exhausted else (None, None)


def provider_error(
    backend: str, detail: object, *, quota_info: dict | None = None, rate_limited: bool = False
) -> AdapterError:
    """Classify provider errors, never ordinary assistant text or tool output."""
    text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
    quota = backend in {"claude", "codex"} and re.search(
        r"usage[_ ]?limit[_ ]?(?:exceeded|reached)|insufficient_quota|"
        r"quota[_ ](?:exceeded|exhausted)|"
        r"(?:usage|subscription|weekly|session) (?:limit|quota).{0,30}"
        r"(?:reached|exceeded|exhausted)|"
        r"you(?:'|\u2019)ve (?:hit|reached) your (?:[\w -]+ )?limit",
        text,
        re.IGNORECASE,
    )
    # Only a rejected native window can supply the retry deadline. Advisory
    # utilization warnings must not turn unrelated failures into quota errors.
    info = quota_info or {}
    if backend != "claude" or info.get("status") != "rejected":
        info = {}
    if quota or (info and rate_limited):
        return QuotaExceeded(
            text, resets_at=info.get("resetsAt"), limit_type=info.get("rateLimitType")
        )
    return AdapterError(text)


class SessionUnavailable(AdapterError):
    """A resume was explicitly rejected before any session/model activity."""


def unavailable_session(error: str) -> bool:
    return bool(
        re.search(
            r"(?:no (?:conversation|session|rollout) found|(?:conversation|session|thread) "
            r"(?:with (?:id )?[^\n]+ )?(?:not found|does not exist|could not be found))",
            error,
            re.IGNORECASE,
        )
    )


class Adapter(Protocol):
    def stream(self, prompt: str, *, phase: str = "discussion") -> AsyncGenerator[str, None]: ...


def command_for(
    agent: AgentConfig,
    phase: str = "discussion",
    *,
    permission_mode: str = "phase_scoped",
    persist_session: bool = False,
    session_id: str | None = None,
    new_session_id: str | None = None,
) -> list[str]:
    if permission_mode not in {"phase_scoped", "full_auto"}:
        raise ValueError("permission_mode must be phase_scoped or full_auto")
    if session_id:
        session_id = validate_session_id(session_id)
        if not persist_session:
            raise ValueError("Resuming requires persistent-session mode")
    if agent.backend == "codex":
        command = [
            "codex",
            "exec",
            "--json",
            "--sandbox",
            "danger-full-access"
            if permission_mode == "full_auto"
            else "workspace-write"
            if phase == "implementation"
            else "read-only",
            "--skip-git-repo-check",
            "--color",
            "never",
            "-c",
            'approval_policy="never"',
        ]
    elif agent.backend == "claude":
        command = [
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--tools",
            "default"
            if permission_mode == "full_auto"
            else "Read,Glob,Grep,Edit,Write,Bash"
            if phase == "implementation"
            else ("Read,Glob,Grep" if phase in {"planning", "judging", "review"} else ""),
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--disable-slash-commands",
        ]
        if permission_mode == "full_auto":
            # Let auto evaluate actions; do not pre-approve entire tools with allowedTools.
            # This headless host cannot answer native permission prompts.
            command.extend(["--permission-mode", "auto", "--permission-prompts", "none"])
        elif phase == "implementation":
            command.extend(
                [
                    "--permission-mode",
                    "acceptEdits",
                    "--allowedTools",
                    "Read,Glob,Grep,Edit,Write,Bash",
                ]
            )
        else:
            # Override permissions on every resume; never inherit a writer's mode.
            command.extend(["--permission-mode", "dontAsk"])
            if phase in {"planning", "judging", "review"}:
                command.extend(["--allowedTools", "Read,Glob,Grep"])
        if not persist_session:
            command.append("--no-session-persistence")
        elif session_id:
            command.extend(["--resume", session_id])
        else:
            command.extend(
                ["--session-id", validate_session_id(new_session_id or str(uuid.uuid4()))]
            )
    else:
        return list(agent.command)
    if agent.model:
        command.extend(["--model", agent.model])
    if agent.backend == "codex":
        if not persist_session:
            command.append("--ephemeral")
        if session_id:
            # --sandbox and --color are exec options, not resume options.
            command.extend(["resume", session_id])
        command.append("-")
    return command


class EventDecoder:
    """Normalize public text only. Never expose reasoning or tool output as a reply."""

    def __init__(
        self,
        backend: str,
        expected_session_id: str | None = None,
        expected_permission_mode: str | None = None,
    ) -> None:
        self.backend = backend
        self.text = ""
        self.complete = False
        self.items: dict[str, str] = {}
        self.claude_blocks = ""
        self.session_id: str | None = None
        self.expected_session_id = expected_session_id
        self.activity_started = False
        self.expected_permission_mode = expected_permission_mode
        self.permission_mode: str | None = None
        self.quota_info: dict = {}

    def feed(self, event: dict) -> str:
        if not isinstance(event, dict):
            raise AdapterError("CLI events must be JSON objects")
        kind = event.get("type")
        delta = ""
        if self.backend == "claude" and event.get("parent_tool_use_id"):
            return ""
        if (
            self.backend == "claude"
            and kind == "system"
            and (event.get("subtype") == "init" or "permissionMode" in event)
        ):
            self.permission_mode = event.get("permissionMode")
            if (
                self.expected_permission_mode
                and self.permission_mode != self.expected_permission_mode
            ):
                raise AdapterError(
                    "Claude did not enter or retain the requested auto permission mode. "
                    "Check CLI support, model availability, and permission policies; "
                    "no permissions bypass will be attempted."
                )
        identifier = (
            event.get("thread_id")
            if self.backend == "codex" and kind == "thread.started"
            else event.get("session_id")
            if self.backend == "claude"
            else None
        )
        if identifier is not None:
            try:
                identifier = validate_session_id(identifier)
            except ValueError as exc:
                raise AdapterError(str(exc)) from exc
            if (
                self.expected_session_id
                and identifier != self.expected_session_id
                or self.session_id
                and identifier != self.session_id
            ):
                raise AdapterError("Backend reported a different session ID; refusing the reply")
            self.session_id = identifier
        if (
            kind in {"thread.started", "turn.started", "stream_event", "assistant"}
            or str(kind).startswith("item.")
            or self.backend == "claude"
            and kind == "system"
            and event.get("subtype") == "init"
        ):
            self.activity_started = True
        if self.backend == "codex":
            if kind == "error":
                error = provider_error("codex", event.get("error") or event.get("message", ""))
                if isinstance(error, QuotaExceeded):
                    raise error
            if kind == "error" and unavailable_session(str(event.get("message", ""))):
                raise AdapterError(str(event["message"]))
            if kind == "turn.failed":
                raise provider_error("codex", event.get("error", "Codex turn failed"))
            if kind in {"item.completed", "item.updated"}:
                item = event.get("item", {})
                if item.get("type") == "agent_message":
                    key = item.get("id", "message")
                    current = item.get("text", "")
                    previous = self.items.get(key, "")
                    if not isinstance(current, str) or not current.startswith(previous):
                        raise AdapterError("Codex returned an incompatible message update")
                    prefix = "\n\n" if key not in self.items and self.items else ""
                    delta = prefix + current[len(previous) :]
                    self.items[key] = current
            if kind == "turn.completed":
                self.complete = True
        elif self.backend == "claude":
            if kind == "rate_limit_event":
                # Utilization warnings are advisory, not failed turns. Use this metadata
                # only if a later native error actually rejects the current invocation.
                info = event.get("rate_limit_info")
                self.quota_info = info if isinstance(info, dict) else {}
            if kind == "assistant" and event.get("error"):
                detail = " ".join(
                    block.get("text", "")
                    for block in event.get("message", {}).get("content", [])
                    if block.get("type") == "text"
                )
                raise provider_error(
                    "claude",
                    detail or event["error"],
                    quota_info=self.quota_info,
                    rate_limited=event["error"] == "rate_limit",
                )
            if kind == "stream_event":
                part = event.get("event", {}).get("delta", {})
                if part.get("type") == "text_delta":
                    delta = part.get("text", "")
            elif kind == "assistant":
                self.claude_blocks += "".join(
                    block.get("text", "")
                    for block in event.get("message", {}).get("content", [])
                    if block.get("type") == "text"
                )
            elif kind == "result":
                if event.get("is_error") or event.get("subtype", "success") != "success":
                    raise provider_error(
                        "claude",
                        event.get("errors") or event.get("result") or event,
                        quota_info=self.quota_info,
                    )
                final = event.get("result") or self.claude_blocks
                if not self.text:
                    delta = final
                elif final and not self.text.endswith(final):
                    if final.startswith(self.text):
                        delta = final[len(self.text) :]
                    else:
                        # A tool-using turn can contain commentary before the final message.
                        # Keep the canonical result last so protocol parsing uses that result.
                        delta = "\n\n" + final
                self.complete = True
        else:
            if kind == "delta":
                delta = event.get("text", "")
            elif kind == "done":
                self.complete = True
            elif kind == "error":
                raise AdapterError(str(event.get("message", "command failed")))
        if not isinstance(delta, str):
            raise AdapterError("CLI text fields must be strings")
        if self.complete and delta and kind not in {"done", "result", "turn.completed"}:
            raise AdapterError("CLI emitted text after its completion event")
        self.text += delta
        return delta

    def finish(self) -> None:
        if self.expected_permission_mode and self.permission_mode != self.expected_permission_mode:
            raise AdapterError("Claude omitted confirmation of the requested auto permission mode")
        if not self.complete:
            raise AdapterError(
                "CLI did not report successful completion; partial reply was not committed"
            )
        if not self.text.strip():
            raise AdapterError("CLI returned an empty reply")


async def terminate_process(process: asyncio.subprocess.Process) -> None:
    # Each turn owns a process group, so cancellation also terminates descendants.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), 1.0)
    except TimeoutError:
        pass
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


async def drain_and_terminate(process: asyncio.subprocess.Process) -> None:
    """Stop an owned process group after its original output readers have been cancelled."""

    async def discard(reader):
        while await reader.read(65_536):
            pass

    drains = [asyncio.create_task(discard(r)) for r in (process.stdout, process.stderr)]
    try:
        await terminate_process(process)
    finally:
        for task in drains:
            task.cancel()
        await asyncio.gather(*drains, return_exceptions=True)


class CLIAdapter:
    supports_activity = True
    supports_session_notifications = True

    def __init__(
        self, agent: AgentConfig, workspace: Path, *, permission_mode: str = "phase_scoped"
    ) -> None:
        self.agent = agent
        self.workspace = workspace
        self.permission_mode = permission_mode
        self.supports_sessions = agent.backend in {"codex", "claude"}
        self.result_session_id: str | None = None

    async def stream(
        self,
        prompt: str,
        *,
        phase: str = "discussion",
        persist_session: bool = False,
        session_id: str | None = None,
        on_activity: Callable[[], None] | None = None,
        on_session: Callable[[str], None] | None = None,
    ) -> AsyncIterator[str]:
        self.result_session_id = None
        new_id = str(uuid.uuid4()) if persist_session and self.agent.backend == "claude" else None
        expected_id = validate_session_id(session_id) if session_id else new_id
        command = command_for(
            self.agent,
            phase,
            permission_mode=self.permission_mode,
            persist_session=persist_session,
            session_id=session_id,
            new_session_id=new_id,
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self.workspace,
                env={**os.environ, "AGENT_TEAM_PHASE": phase},
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise AdapterError(f"Cannot find {command[0]}; install it and sign in first") from exc
        assert process.stdin and process.stdout and process.stderr
        stderr_output = bytearray()

        async def drain_stderr() -> None:
            while chunk := await process.stderr.read(4096):
                stderr_output.extend(chunk)
                if on_activity:
                    on_activity()

        async def write_prompt() -> None:
            try:
                process.stdin.write(prompt.encode())
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

        stderr_task = asyncio.create_task(drain_stderr())
        stdin_task = asyncio.create_task(write_prompt())
        decoder = EventDecoder(
            self.agent.backend,
            expected_id,
            "auto"
            if self.agent.backend == "claude" and self.permission_mode == "full_auto"
            else None,
        )
        notified = False
        try:
            async for line in iter_lines(process.stdout, on_activity=on_activity):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise AdapterError(
                        "Invalid CLI stdout JSON; check version and command configuration"
                    ) from exc
                delta = decoder.feed(event)
                if persist_session and on_session and decoder.session_id and not notified:
                    on_session(decoder.session_id)
                    notified = True
                if delta:
                    yield delta
            code = await process.wait()
            await stderr_task
            await stdin_task
            if code:
                detail = stderr_output.decode(errors="replace").strip()
                raise provider_error(
                    self.agent.backend,
                    f"CLI exit code {code}: {detail or 'no stderr'}",
                    quota_info=decoder.quota_info,
                )
            decoder.finish()
            if persist_session and not decoder.session_id:
                raise AdapterError(
                    "Backend omitted its session ID; refusing incremental synchronization"
                )
            self.result_session_id = decoder.session_id
        except AdapterError as exc:
            if session_id and not decoder.activity_started and unavailable_session(str(exc)):
                raise SessionUnavailable(str(exc)) from exc
            raise
        finally:
            for task in (stdin_task, stderr_task):
                task.cancel()
            await asyncio.gather(stdin_task, stderr_task, return_exceptions=True)

            await drain_and_terminate(process)


class MockAdapter:
    def __init__(self, agent: AgentConfig, workspace: Path) -> None:
        self.agent = agent
        self.workspace = workspace

    async def stream(self, prompt: str, *, phase: str = "discussion") -> AsyncIterator[str]:
        if phase == "chat":
            yield PASS
            return
        if STATE_MARKER in prompt:
            state_json = prompt.split(STATE_MARKER, 1)[1].split("\n", 1)[0]
            state = json.loads(state_json)
            reply = self.workflow_reply(state)
            for offset in range(0, len(reply), 40):
                await asyncio.sleep(0.01)
                yield reply[offset : offset + 40]
            return
        messages = json.loads(prompt.split(TRANSCRIPT_MARKER, 1)[1])
        latest_user = max(i for i, message in enumerate(messages) if message["role"] == "user")
        recent = messages[latest_user + 1 :]
        if any(message["speaker"] == self.agent.name for message in recent):
            reply = PASS
        elif not recent:
            topic = messages[latest_user]["text"]
            reply = f"[demo] For {topic}, clarify the goal, acceptance criteria, and tradeoffs."
        else:
            reply = (
                f"[demo] I read {recent[-1]['speaker']}'s suggestion. "
                "What are the acceptance criteria, and how should disagreements be resolved?"
            )
        for offset in range(0, len(reply), 5):
            await asyncio.sleep(0.03)
            yield reply[offset : offset + 5]

    def workflow_reply(self, state: dict) -> str:
        import sys

        version = state["version"]
        if state["phase"] == "discussion":
            if not state["proposal"]:
                members = state["members"]
                return action_reply(
                    "[demo] Let us build and judge a Python greeting function and guide.",
                    {
                        "action": "propose",
                        "summary": "Demo: build a shared Python greeting function and guide",
                        "acceptance_criteria": [
                            "hello('team') returns Hello, team!",
                            "Provide usage instructions",
                        ],
                        "tasks": [
                            {
                                "id": "code",
                                "title": "Implement the function",
                                "details": "Create hello.py together",
                                "owner": members[0],
                                "depends_on": [],
                            },
                            {
                                "id": "docs",
                                "title": "Document the function",
                                "details": "Create HOWTO.md together",
                                "owner": members[-1],
                                "depends_on": ["code"],
                            },
                        ],
                        "acceptance_checks": [
                            [
                                sys.executable,
                                "-c",
                                "from hello import hello; from pathlib import Path; "
                                "assert hello('team') == 'Hello, team!'; "
                                "assert Path('HOWTO.md').is_file()",
                            ]
                        ],
                    },
                )
            return action_reply(
                "[demo] I agree with the goals, shared milestones, and acceptance criteria.",
                {
                    "action": "approve",
                    "version": version,
                },
            )
        if state["phase"] == "implementation":
            if (
                state["proposal"]["summary"]
                != "Demo: build a shared Python greeting function and guide"
            ):
                return action_reply(
                    "[demo] The mock backend only implements the built-in fixture.",
                    {
                        "action": "blocked",
                        "reason": "This task requires a real CLI backend",
                    },
                )
            tasks = state["proposal"]["tasks"]
            done = {t["id"] for t in tasks if t["status"] == "done"}
            task = next(
                t for t in tasks if t["status"] == "pending" and set(t["depends_on"]) <= done
            )
            if task["id"] == "code":
                filename, content = "hello.py", 'def hello(name):\n    return f"Hello, {name}!"\n'
            else:
                filename, content = "HOWTO.md", "# Hello\n\nCall hello(name) from hello.py.\n"
            output_path = self.workspace / filename
            if output_path.exists() and output_path.read_text() != content:
                return action_reply(
                    "[demo] A conflicting file already exists; pausing.",
                    {
                        "action": "blocked",
                        "reason": f"Use an empty workspace; preserve the existing {filename}",
                    },
                )
            output_path.write_text(content)
            return action_reply(
                f"[demo] Wrote {filename}; please judge the actual implementation.",
                {
                    "action": "task_done",
                    "version": version,
                    "task_id": task["id"],
                    "summary": f"Created {filename}",
                    "files": [filename],
                    "tests": "Not run; awaiting coordinator acceptance checks",
                },
            )
        if state["phase"] == "judging":
            point = state["checkpoint"]
            task = next(t for t in state["proposal"]["tasks"] if t["id"] == point["task_id"])
            contents = {f: (self.workspace / f).read_text() for f in task["report"]["files"]}
            return action_reply(
                f"[demo] I inspected {point['author']}'s files: {', '.join(contents)}.",
                {
                    "action": "judge_pass",
                    "version": version,
                    "task_id": point["task_id"],
                    "revision": point["revision"],
                    "evidence": f"Read the submitted artifacts: {contents!r}",
                },
            )
        return action_reply(
            "[demo] Inspected the integrated code and guide; ready for acceptance checks.",
            {
                "action": "review_pass",
                "version": version,
                "evidence": "Code and guide meet the demo acceptance requirements",
            },
        )


def make_adapter(
    agent: AgentConfig, workspace: Path, *, permission_mode: str = "phase_scoped"
) -> Adapter:
    return (
        MockAdapter(agent, workspace)
        if agent.backend == "mock"
        else CLIAdapter(agent, workspace, permission_mode=permission_mode)
    )
