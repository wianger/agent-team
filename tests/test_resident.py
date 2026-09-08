from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

from agent_team.adapters import AdapterError
from agent_team.config import AgentConfig
from agent_team.resident import ClaudeResident, CodexResident

FIXTURE = Path(__file__).parent / "fixtures" / "resident_cli.py"


class FakeProcess:
    async def start_process(self, command):
        self.commands.append(command)
        await super().start_process([sys.executable, str(FIXTURE), self.agent.backend])

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

    def make(self, backend):
        cls = FakeCodex if backend == "codex" else FakeClaude
        adapter = cls(
            AgentConfig(backend, backend, model="configured-model"),
            Path(self.temp.name),
            permission_mode="full_auto",
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
                        ["readOnly", "dangerFullAccess"],
                    )
                    self.assertTrue(all(p["model"] == "configured-model" for p in calls))
                else:
                    self.assertIn("--input-format", adapter.commands[0])
                    self.assertIn("auto", adapter.commands[0])
                    self.assertIn("configured-model", adapter.commands[0])

    async def test_claude_guard_blocks_nonwriter_tools_without_restarting(self):
        adapter = self.make("claude")
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

    async def test_claude_rejects_permission_downgrade_on_a_later_turn(self):
        adapter = self.make("claude")
        await self.ask(adapter)
        with self.assertRaisesRegex(AdapterError, "auto permission mode"):
            await self.ask(adapter, "bad-permission", session_id=adapter.result_session_id)
        self.assertIsNone(adapter.process)

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
