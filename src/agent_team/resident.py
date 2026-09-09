"""Resident, bidirectional CLI connections. Public replies exclude private tool events."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Callable
from pathlib import Path

from .adapters import (
    AdapterError,
    EventDecoder,
    QuotaExceeded,
    codex_quota_reset,
    command_for,
    drain_and_terminate,
    make_adapter,
    provider_error,
)
from .config import AgentConfig
from .sessions import session_id as validate_session_id
from .streams import iter_lines


class JsonProcess:
    """One reader routes RPC replies independently of the current model response."""

    supports_activity = True
    supports_sessions = True

    def __init__(self, agent: AgentConfig, workspace: Path, *, permission_mode: str):
        self.agent, self.workspace, self.permission_mode = agent, workspace, permission_mode
        self.process: asyncio.subprocess.Process | None = None
        self.reader_task: asyncio.Task | None = None
        self.stderr_task: asyncio.Task | None = None
        self.pending: dict[str, asyncio.Future] = {}
        self.events: asyncio.Queue | None = None
        self.write_lock = asyncio.Lock()
        self.stderr = bytearray()
        self.failure: Exception | None = None
        self.on_activity: Callable[[], None] | None = None
        self.result_session_id: str | None = None
        self.phase = "discussion"
        self.connection_session: str | None = None

    def activity(self):
        if self.on_activity:
            self.on_activity()

    async def start_process(self, command: list[str]):
        if self.failure:
            raise self.failure
        if self.process:
            return
        self.process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.workspace,
            env=dict(os.environ),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self.reader_task = asyncio.create_task(self.read_output())
        self.stderr_task = asyncio.create_task(self.read_stderr())

    async def read_stderr(self):
        while chunk := await self.process.stderr.read(65_536):
            self.stderr.extend(chunk)
            self.activity()

    async def read_output(self):
        try:
            async for line in iter_lines(self.process.stdout, on_activity=self.activity):
                if not line.strip():
                    continue
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise AdapterError("Resident CLI events must be JSON objects")
                await self.route(event)
            await self.process.wait()
            await self.stderr_task
            raise provider_error(
                self.agent.backend,
                f"Resident {self.agent.backend} connection closed "
                f"(exit {self.process.returncode}): " + self.stderr.decode(errors="replace"),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failure = exc if isinstance(exc, AdapterError) else AdapterError(str(exc))
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(self.failure)
            if self.events is not None:
                self.events.put_nowait(self.failure)

    async def send(self, value: dict):
        async with self.write_lock:
            if self.failure:
                raise self.failure
            if not self.process or self.process.returncode is not None:
                raise AdapterError("Resident CLI is not running")
            self.process.stdin.write((json.dumps(value, ensure_ascii=False) + "\n").encode())
            await self.process.stdin.drain()

    async def request(self, method: str, params: dict):
        identifier = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            if self.agent.backend == "codex":
                packet = {"id": identifier, "method": method, "params": params}
            else:
                packet = {
                    "type": "control_request",
                    "request_id": identifier,
                    "request": {"subtype": method, **params},
                }
            await self.send(packet)
            # Turn timeouts are owned by the room and are disabled by default.
            return await future
        finally:
            self.pending.pop(identifier, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Observe errors even if cancellation raced with the response.

    def resolve(self, identifier, result, error=None):
        future = self.pending.get(identifier)
        if future is not None and not future.done():
            if error is not None:
                future.set_exception(provider_error(self.agent.backend, error))
            else:
                future.set_result(result)

    async def next_event(self):
        event = await self.events.get()
        if isinstance(event, Exception):
            raise event
        return event

    async def route(self, event: dict):
        raise NotImplementedError

    async def close(self):
        process = self.process
        if not process:
            return
        # Cancellation revokes the private turn. Kill its process group, including tools;
        # a later authorized invocation reconstructs uncertain state from the public log.
        tasks = [t for t in (self.reader_task, self.stderr_task) if t]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        try:
            await drain_and_terminate(process)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(AdapterError("Resident connection stopped"))
            self.process = None
            self.connection_session = None
            self.failure = None
            self.stderr.clear()


class CodexResident(JsonProcess):
    async def route(self, event):
        if "id" in event:
            if "method" in event:
                # No host approvals or user input can be silently granted.
                await self.send(
                    {
                        "id": event["id"],
                        "error": {
                            "code": -32601,
                            "message": "Interactive host requests are disabled",
                        },
                    }
                )
            else:
                self.resolve(event["id"], event.get("result"), event.get("error"))
        elif self.events is not None:
            self.events.put_nowait(event)

    async def stream(
        self, prompt, *, phase="discussion", persist_session=True, session_id=None, on_activity=None
    ):
        if self.events is not None:
            raise AdapterError("An agent cannot run two turns in its private session")
        self.events = asyncio.Queue()
        self.on_activity, self.phase = on_activity, phase
        self.result_session_id = None
        try:
            if not self.process:
                await self.start_process(
                    ["codex", "app-server", "--listen", "stdio://", "-c", 'approval_policy="never"']
                )
                await self.request(
                    "initialize",
                    {"clientInfo": {"name": "agent_team", "version": "0.1.0"}},
                )
                await self.send({"method": "initialized", "params": {}})
            writer = phase == "implementation"
            sandbox = (
                "danger-full-access"
                if self.permission_mode == "full_auto"
                else "workspace-write"
                if writer
                else "read-only"
            )
            if not session_id or self.connection_session != session_id:
                params = {
                    "cwd": str(self.workspace),
                    "approvalPolicy": "never",
                    "sandbox": sandbox,
                }
                if self.agent.model:
                    params["model"] = self.agent.model
                if session_id:
                    params["threadId"] = validate_session_id(session_id)
                else:
                    params["ephemeral"] = not persist_session
                result = await self.request(
                    "thread/resume" if session_id else "thread/start", params
                )
                identifier = validate_session_id(result["thread"]["id"])
                if session_id and identifier != session_id:
                    raise AdapterError("Codex resumed a different private thread")
                self.connection_session = identifier
            policy = {
                "type": {
                    "danger-full-access": "dangerFullAccess",
                    "workspace-write": "workspaceWrite",
                    "read-only": "readOnly",
                }[sandbox]
            }
            if sandbox == "workspace-write":
                policy["writableRoots"] = [str(self.workspace)]
            params = {
                "threadId": self.connection_session,
                "input": [{"type": "text", "text": prompt}],
                "cwd": str(self.workspace),
                "approvalPolicy": "never",
                "sandboxPolicy": policy,
            }
            if self.agent.model:
                params["model"] = self.agent.model
            result = await self.request("turn/start", params)
            turn_id = result["turn"]["id"]
            items: dict[str, str] = {}
            last_error = None
            while True:
                event = await self.next_event()
                method, data = event.get("method"), event.get("params", {})
                if data.get("threadId", self.connection_session) != self.connection_session:
                    continue
                if data.get("turnId", turn_id) != turn_id:
                    continue
                if method == "error":
                    last_error = data.get("error")
                elif method == "item/agentMessage/delta":
                    key, delta = data["itemId"], data["delta"]
                    if not isinstance(delta, str):
                        raise AdapterError("Codex text delta must be a string")
                    if key not in items and items:
                        yield "\n"
                    items[key] = items.get(key, "") + delta
                    yield delta
                elif (
                    method == "item/completed"
                    and data.get("item", {}).get("type") == "agentMessage"
                ):
                    item = data["item"]
                    key, full = item["id"], item["text"]
                    previous = items.get(key, "")
                    if not full.startswith(previous):
                        raise AdapterError("Codex rewrote an already streamed message")
                    if key not in items and items:
                        yield "\n"
                    items[key] = full
                    if full[len(previous) :]:
                        yield full[len(previous) :]
                elif method == "turn/completed" and data.get("turn", {}).get("id") == turn_id:
                    turn = data["turn"]
                    if turn["status"] != "completed":
                        raise provider_error(
                            "codex", turn.get("error") or last_error or turn["status"]
                        )
                    if not any(t.strip() for t in items.values()):
                        raise AdapterError("Codex returned an empty reply")
                    self.result_session_id = self.connection_session
                    return
        except QuotaExceeded as exc:
            try:
                # A best-effort metadata read, not a model call or quota polling loop.
                try:
                    async with asyncio.timeout(5):
                        limits = await self.request("account/rateLimits/read", {})
                    exc.resets_at, exc.limit_type = codex_quota_reset(limits)
                except (AdapterError, TimeoutError):
                    pass
                raise
            finally:
                await self.close()
        except BaseException:
            await self.close()
            raise
        finally:
            self.events = None
            self.on_activity = None


class ClaudeResident(JsonProcess):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.observed_permission: str | None = None

    async def route(self, event):
        kind = event.get("type")
        if kind == "control_response":
            response = event["response"]
            self.resolve(
                response["request_id"],
                response.get("response", {}),
                response.get("error") if response.get("subtype") == "error" else None,
            )
        elif kind == "control_request":
            request = event["request"]
            result = {"subtype": "success", "request_id": event["request_id"], "response": {}}
            if (
                request.get("subtype") == "hook_callback"
                and request.get("callback_id") == "phase_guard"
            ):
                tool = request.get("input", {}).get("tool_name")
                active = self.events is not None and self.phase != "idle"
                allowed = active and (
                    self.permission_mode == "full_auto"
                    or self.phase == "implementation"
                    or (
                        self.phase in {"planning", "judging", "review"}
                        and tool in {"Read", "Glob", "Grep"}
                    )
                )
                if not allowed:
                    result["response"] = {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": "A workspace lease is required."
                            if active
                            else "No active agent turn.",
                        }
                    }
            elif request.get("subtype") == "can_use_tool":
                result["response"] = {
                    "behavior": "deny",
                    "message": "Human permission prompts are disabled",
                }
            else:
                result = {
                    "subtype": "error",
                    "request_id": event["request_id"],
                    "error": "Unsupported host request",
                }
            await self.send({"type": "control_response", "response": result})
        else:
            if kind == "result":
                # Revoke tools before the consumer commits and releases its write lease.
                self.phase = "idle"
            if kind == "system" and (event.get("subtype") == "init" or "permissionMode" in event):
                self.observed_permission = event.get("permissionMode")
                if self.permission_mode == "full_auto" and self.observed_permission != "auto":
                    raise AdapterError("Claude did not enter or retain auto permission mode")
            if self.events is not None:
                self.events.put_nowait(event)

    async def stream(
        self, prompt, *, phase="discussion", persist_session=True, session_id=None, on_activity=None
    ):
        if self.events is not None:
            raise AdapterError("An agent cannot run two turns in its private session")
        # Explicit full-context mode intentionally starts a fresh private conversation.
        if self.process and (not persist_session or session_id != self.connection_session):
            await self.close()
        self.events = asyncio.Queue()
        self.on_activity, self.phase = on_activity, phase
        self.result_session_id = None
        try:
            if not self.process:
                self.observed_permission = None
                self.connection_session = validate_session_id(session_id or str(uuid.uuid4()))
                command = command_for(
                    self.agent,
                    "implementation",
                    permission_mode=self.permission_mode,
                    persist_session=persist_session,
                    session_id=session_id,
                    new_session_id=self.connection_session,
                )
                command.extend(["--input-format", "stream-json"])
                await self.start_process(command)
                await self.request(
                    "initialize",
                    {
                        "hooks": {
                            "PreToolUse": [{"matcher": None, "hookCallbackIds": ["phase_guard"]}]
                        }
                    },
                )
            if self.permission_mode == "phase_scoped":
                await self.request(
                    "set_permission_mode",
                    {"mode": "acceptEdits" if phase == "implementation" else "dontAsk"},
                )
            decoder = EventDecoder(
                "claude",
                self.connection_session if persist_session else None,
                "auto" if self.permission_mode == "full_auto" else None,
            )
            decoder.permission_mode = self.observed_permission
            await self.send(
                {
                    "type": "user",
                    "session_id": self.connection_session,
                    "parent_tool_use_id": None,
                    "message": {"role": "user", "content": prompt},
                }
            )
            while True:
                try:
                    event = await self.next_event()
                except QuotaExceeded as exc:
                    # EOF/stderr failures also retain metadata already consumed this turn.
                    raise provider_error(
                        "claude", str(exc), quota_info=decoder.quota_info, rate_limited=True
                    ) from exc
                delta = decoder.feed(event)
                if delta:
                    yield delta
                if event.get("type") == "result":
                    decoder.finish()
                    if persist_session and decoder.session_id != self.connection_session:
                        raise AdapterError("Claude omitted or changed its private session ID")
                    self.result_session_id = decoder.session_id
                    return
        except BaseException:
            await self.close()
            raise
        finally:
            self.events = None
            self.on_activity = None


def make_resident(agent: AgentConfig, workspace: Path, *, permission_mode: str):
    if agent.backend in {"codex", "claude"}:
        cls = CodexResident if agent.backend == "codex" else ClaudeResident
        return cls(agent, workspace, permission_mode=permission_mode)
    return make_adapter(agent, workspace, permission_mode=permission_mode)
