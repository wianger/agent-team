from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from agent_team.adapters import AdapterError, QuotaExceeded, SessionUnavailable
from agent_team.config import AgentConfig
from agent_team.resident import ClaudeResident, CodexResident

FIXTURE = Path(__file__).parent / "fixtures" / "resident_cli.py"
PHASES = ("discussion", "planning", "implementation", "judging", "review", "chat")


class FakeProcess:
    async def start_process(self, command):
        self.commands.append(command)
        await super().start_process([sys.executable, str(FIXTURE), self.agent.backend, *command])

    async def request(self, method, params):
        self.requests.append((method, params))
        return await super().request(method, params)


class FakeCodex(FakeProcess, CodexResident):
    pass


class FakeClaude(FakeProcess, ClaudeResident):
    pass


class ResidentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.adapters = []

    async def asyncTearDown(self):
        await asyncio.gather(*(a.close() for a in self.adapters))
        self.temp.cleanup()

    def make(self, backend, permission_mode="full_auto"):
        cls = FakeCodex if backend == "codex" else FakeClaude
        adapter = cls(
            AgentConfig(backend, backend, model="configured-model"),
            Path(self.temp.name),
            permission_mode=permission_mode,
        )
        adapter.commands, adapter.requests = [], []
        self.adapters.append(adapter)
        return adapter

    async def ask(self, adapter, prompt="hello", phase="planning", **options):
        async with asyncio.timeout(5):
            return "".join([t async for t in adapter.stream(prompt, phase=phase, **options)])

    async def test_both_backends_reuse_the_same_live_process_and_private_session(self):
        for backend in ("codex", "claude"):
            with self.subTest(backend=backend):
                adapter = self.make(backend)
                self.assertEqual(await self.ask(adapter), "public 1")
                pid, identifier = adapter.process.pid, adapter.result_session_id
                self.assertEqual(
                    await self.ask(adapter, session_id=identifier, phase="implementation"),
                    "public 2",
                )
                self.assertEqual(adapter.process.pid, pid)
                self.assertEqual(adapter.result_session_id, identifier)
                self.assertIsNone(adapter.process.returncode)
                self.assertEqual(len(adapter.commands), 1)
                if backend == "codex":
                    calls = [p for m, p in adapter.requests if m == "turn/start"]
                    self.assertEqual(
                        [p["sandboxPolicy"]["type"] for p in calls],
                        ["dangerFullAccess", "dangerFullAccess"],
                    )
                    self.assertTrue(all(p["model"] == "configured-model" for p in calls))
                else:
                    self.assertIn("--input-format", adapter.commands[0])
                    self.assertIn("auto", adapter.commands[0])
                    self.assertIn("configured-model", adapter.commands[0])

    async def test_codex_full_access_on_every_phase_including_reconnect_and_fresh_context(self):
        for persist in (True, False):
            adapter = self.make("codex")
            for phase in PHASES:
                with self.subTest(phase=phase, persist=persist):
                    # Reconnects must override saved native permissions, too.
                    identifier = adapter.result_session_id if persist else None
                    await adapter.close()
                    await self.ask(
                        adapter, phase=phase, persist_session=persist, session_id=identifier
                    )
                    method, params = adapter.requests[-2]
                    self.assertEqual(method, "thread/resume" if identifier else "thread/start")
                    self.assertEqual(params["sandbox"], "danger-full-access")
                    self.assertEqual(params["approvalPolicy"], "never")
                    turn = adapter.requests[-1][1]
                    self.assertEqual(turn["sandboxPolicy"], {"type": "dangerFullAccess"})
                    self.assertEqual(turn["approvalPolicy"], "never")

    async def test_codex_full_auto_never_downgrades_a_live_session(self):
        adapter = self.make("codex")
        for phase in PHASES:
            await self.ask(adapter, phase=phase, session_id=adapter.result_session_id)
        calls = [p for m, p in adapter.requests if m == "turn/start"]
        self.assertEqual(len(adapter.commands), 1)
        self.assertEqual(
            [p["sandboxPolicy"] for p in calls], [{"type": "dangerFullAccess"}] * len(PHASES)
        )

    async def test_codex_phase_scoped_still_restricts_nonwriters_on_each_turn(self):
        adapter = self.make("codex", "phase_scoped")
        for phase in PHASES:
            await self.ask(adapter, phase=phase, session_id=adapter.result_session_id)
            policy = adapter.requests[-1][1]["sandboxPolicy"]
            self.assertEqual(
                policy,
                {"type": "workspaceWrite", "writableRoots": [self.temp.name]}
                if phase == "implementation"
                else {"type": "readOnly"},
            )
        self.assertEqual(len(adapter.commands), 1)

    async def test_claude_full_auto_keeps_all_tools_available_on_every_live_turn(self):
        adapter = self.make("claude")
        for phase in PHASES:
            for tool in ("Read", "Write", "Bash", "WebFetch", "WebSearch"):
                with self.subTest(phase=phase, tool=tool):
                    self.assertEqual(
                        await self.ask(
                            adapter,
                            "try-tool:" + tool,
                            phase=phase,
                            session_id=adapter.result_session_id,
                        ),
                        "allowed",
                    )
        self.assertEqual(len(adapter.commands), 1)
        command = adapter.commands[0]
        self.assertEqual(command[command.index("--tools") + 1], "default")
        self.assertEqual(command[command.index("--permission-mode") + 1], "auto")
        self.assertFalse(any(m == "set_permission_mode" for m, _ in adapter.requests))

    async def test_claude_full_auto_hook_preserves_native_checks_and_revokes_idle_tools(self):
        adapter = self.make("claude")
        adapter.send = AsyncMock()
        event = {
            "type": "control_request",
            "request_id": "tool-request",
            "request": {
                "subtype": "hook_callback",
                "callback_id": "phase_guard",
                "input": {"tool_name": "Bash"},
            },
        }
        adapter.events = asyncio.Queue()
        adapter.phase = "planning"
        await adapter.route(event)
        # Empty output continues native auto checks; an explicit allow would bypass them.
        self.assertEqual(adapter.send.call_args.args[0]["response"]["response"], {})
        await adapter.route({"type": "result"})
        await adapter.route(event)
        output = adapter.send.call_args.args[0]["response"]["response"]
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        adapter.events = None
        adapter.phase = "implementation"
        await adapter.route(event)
        output = adapter.send.call_args.args[0]["response"]["response"]
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")

    async def test_claude_phase_scoped_guard_blocks_nonwriter_tools_without_restarting(self):
        adapter = self.make("claude", "phase_scoped")
        self.assertEqual(await self.ask(adapter, "try-write"), "deny")
        pid, identifier = adapter.process.pid, adapter.result_session_id
        self.assertEqual(
            await self.ask(adapter, "try-write", phase="implementation", session_id=identifier),
            "allowed",
        )
        self.assertEqual(
            await self.ask(adapter, "try-write", phase="judging", session_id=identifier), "deny"
        )
        self.assertEqual(adapter.process.pid, pid)
        for phase in PHASES:
            for tool in ("Read", "Write", "Bash", "WebFetch", "WebSearch"):
                with self.subTest(phase=phase, tool=tool):
                    allowed = phase == "implementation" or (
                        phase in {"planning", "judging", "review"} and tool == "Read"
                    )
                    self.assertEqual(
                        await self.ask(
                            adapter, "try-tool:" + tool, phase=phase, session_id=identifier
                        ),
                        "allowed" if allowed else "deny",
                    )

    async def test_claude_rejects_permission_downgrade_on_a_later_turn(self):
        adapter = self.make("claude")
        await self.ask(adapter)
        with self.assertRaisesRegex(AdapterError, "auto permission mode"):
            await self.ask(adapter, "bad-permission", session_id=adapter.result_session_id)
        self.assertIsNone(adapter.process)

    async def test_native_quota_errors_keep_their_type_and_invalidate_the_invocation(self):
        for backend, prompt in (("claude", "quota"), ("claude", "quota-exit"), ("codex", "quota")):
            adapter = self.make(backend)
            with (
                self.subTest(backend=backend, prompt=prompt),
                self.assertRaises(QuotaExceeded) as raised,
            ):
                await self.ask(adapter, prompt)
            self.assertEqual(raised.exception.resets_at, 2_000_000_600)
            self.assertEqual(
                raised.exception.limit_type, "five_hour" if backend == "claude" else "primary"
            )
            self.assertIsNone(adapter.result_session_id)
            self.assertIsNone(adapter.process)

    async def test_codex_reset_read_is_best_effort_and_only_runs_after_a_quota_error(self):
        for failure in (AdapterError("Unsupported method"), TimeoutError()):
            adapter = self.make("codex")
            request = adapter.request

            async def unavailable(method, params, error=failure, call=request):
                if method == "account/rateLimits/read":
                    raise error
                return await call(method, params)

            adapter.request = unavailable
            with self.assertRaises(QuotaExceeded) as raised:
                await self.ask(adapter, "quota")
            self.assertIsNone(raised.exception.resets_at)
            self.assertIsNone(adapter.process)
        adapter = self.make("codex")
        await self.ask(adapter)
        self.assertNotIn("account/rateLimits/read", [method for method, _ in adapter.requests])

    async def test_early_native_identity_survives_first_turn_quota_and_resumes_after_process_exit(
        self,
    ):
        for backend in ("claude", "codex"):
            with self.subTest(backend=backend):
                adapter = self.make(backend)
                identities = []
                with self.assertRaises(QuotaExceeded):
                    await self.ask(adapter, "quota", on_session=identities.append)
                self.assertEqual(len(identities), 1)
                identifier = identities[0]
                self.assertIsNone(adapter.result_session_id)
                self.assertIsNone(adapter.process)
                await self.ask(adapter, session_id=identifier, on_session=identities.append)
                self.assertEqual(identities, [identifier, identifier])
                self.assertEqual(adapter.result_session_id, identifier)
                if backend == "claude":
                    command = adapter.commands[-1]
                    self.assertEqual(command[command.index("--resume") + 1], identifier)
                    self.assertIn("auto", command)
                else:
                    self.assertTrue(
                        any(
                            method == "thread/resume" and params["threadId"] == identifier
                            for method, params in adapter.requests
                        )
                    )
                await self.ask(adapter, persist_session=False, on_session=identities.append)
                self.assertEqual(identities, [identifier, identifier])

    async def test_native_missing_session_is_reported_before_starting_a_turn(self):
        for backend in ("claude", "codex"):
            with self.subTest(backend=backend):
                adapter = self.make(backend)
                identities = []
                with self.assertRaises(SessionUnavailable):
                    await self.ask(
                        adapter,
                        session_id="00000000-0000-0000-0000-000000000404",
                        on_session=identities.append,
                    )
                self.assertFalse(identities)
                self.assertIsNone(adapter.process)
                self.assertNotIn("turn/start", [method for method, _ in adapter.requests])

    async def test_cancellation_during_codex_reset_read_closes_the_process(self):
        adapter = self.make("codex")
        request = adapter.request
        reading = asyncio.Event()

        async def pending(method, params):
            if method == "account/rateLimits/read":
                reading.set()
                await asyncio.Event().wait()
            return await request(method, params)

        adapter.request = pending
        task = asyncio.create_task(self.ask(adapter, "quota"))
        try:
            async with asyncio.timeout(2):
                await reading.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertIsNone(adapter.process)
            self.assertIsNone(adapter.result_session_id)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_transport_failures_unblock_pending_rpc_and_never_commit_partial_output(self):
        for backend in ("codex", "claude"):
            with self.subTest(backend=backend):
                adapter = self.make(backend)
                with self.assertRaisesRegex(AdapterError, "connection closed"):
                    await self.ask(adapter, "exit")
                self.assertIsNone(adapter.result_session_id)
                self.assertIsNone(adapter.process)
        adapter = self.make("codex")
        with self.assertRaises(AdapterError):
            await self.ask(adapter, "malformed")

    async def test_cancellation_terminates_resident_process_and_rejects_overlapping_turns(self):
        for backend in ("codex", "claude"):
            with self.subTest(backend=backend):
                adapter = self.make(backend)
                started = asyncio.Event()

                async def run(connection=adapter, ready=started):
                    async for _ in connection.stream("hang"):
                        ready.set()

                task = asyncio.create_task(run())
                try:
                    await asyncio.wait_for(started.wait(), 5)
                    pid = adapter.process.pid
                    with self.assertRaisesRegex(AdapterError, "two turns"):
                        await self.ask(adapter)
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 5)
                self.assertIsNone(adapter.process)
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)

    async def test_full_context_requests_use_fresh_private_conversations(self):
        for backend in ("codex", "claude"):
            with self.subTest(backend=backend):
                adapter = self.make(backend)
                await self.ask(adapter, persist_session=False)
                first = adapter.connection_session
                await self.ask(adapter, persist_session=False)
                self.assertNotEqual(first, adapter.connection_session)
