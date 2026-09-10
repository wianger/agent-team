from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

from agent_team.adapters import AdapterError, CLIAdapter, EventDecoder, command_for
from agent_team.config import AgentConfig


class DecoderTests(unittest.TestCase):
    def test_codex_messages_are_deduplicated_and_reasoning_omitted(self):
        decoder = EventDecoder("codex")
        self.assertEqual(
            decoder.feed(
                {
                    "type": "item.completed",
                    "item": {"type": "reasoning", "text": "private", "id": "r"},
                }
            ),
            "",
        )
        self.assertEqual(
            decoder.feed(
                {"type": "item.updated", "item": {"type": "agent_message", "text": "Hi", "id": "a"}}
            ),
            "Hi",
        )
        self.assertEqual(
            decoder.feed(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Hi there", "id": "a"},
                }
            ),
            " there",
        )
        decoder.feed({"type": "turn.completed"})
        decoder.finish()
        self.assertEqual(decoder.text, "Hi there")

    def test_claude_partial_assistant_and_result_do_not_duplicate(self):
        decoder = EventDecoder("claude")
        decoder.feed(
            {"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "café"}}}
        )
        decoder.feed(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "café"}]}}
        )
        self.assertEqual(
            decoder.feed({"type": "result", "subtype": "success", "result": "café"}), ""
        )
        decoder.finish()
        self.assertEqual(decoder.text, "café")

    def test_claude_result_only_and_failure(self):
        decoder = EventDecoder("claude")
        self.assertEqual(decoder.feed({"type": "result", "result": "final"}), "final")
        decoder.finish()
        with self.assertRaises(AdapterError):
            EventDecoder("claude").feed({"type": "result", "is_error": True, "result": "no auth"})

    def test_missing_completion_and_text_after_done_rejected(self):
        decoder = EventDecoder("command")
        decoder.feed({"type": "delta", "text": "partial"})
        with self.assertRaises(AdapterError):
            decoder.finish()
        decoder.feed({"type": "done"})
        with self.assertRaises(AdapterError):
            decoder.feed({"type": "delta", "text": "late"})

    def test_cli_flags_and_prompt_transport(self):
        codex = command_for(AgentConfig("a", "codex", model="chosen-model"))
        self.assertEqual(codex[-1], "-")
        self.assertIn("read-only", codex)
        self.assertIn("chosen-model", codex)
        claude = command_for(AgentConfig("b", "claude"))
        self.assertEqual(claude[claude.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", claude)


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, script):
        return CLIAdapter(
            AgentConfig("custom", "command", command=(sys.executable, "-c", script)), Path.cwd()
        )

    async def test_stdin_unicode_and_jsonl_output(self):
        adapter = self.adapter(
            "import json,sys; p=sys.stdin.read(); "
            "print(json.dumps({'type':'delta','text':p})); print('{\"type\":\"done\"}')"
        )
        self.assertEqual(
            "".join([text async for text in adapter.stream("café\ncontext")]), "café\ncontext"
        )

    async def test_large_stderr_does_not_deadlock(self):
        adapter = self.adapter(
            "import sys; sys.stderr.write('x'*200000); sys.stderr.flush(); sys.stdin.read(); "
            'print(\'{"type":"delta","text":"ok"}\'); print(\'{"type":"done"}\')'
        )
        async with asyncio.timeout(5):
            self.assertEqual([text async for text in adapter.stream("x" * 200000)], ["ok"])

    async def test_failed_process_never_counts_as_success(self):
        adapter = self.adapter('import sys; print(\'{"type":"delta","text":"half"}\'); sys.exit(3)')
        with self.assertRaisesRegex(AdapterError, "exit code 3"):
            _ = [text async for text in adapter.stream("topic")]

    async def test_cancellation_terminates_process(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / "pid"
            adapter = self.adapter(
                "import os,pathlib,time; "
                f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid())); "
                'print(\'{"type":"delta","text":"partial"}\', flush=True); time.sleep(60)'
            )
            started = asyncio.Event()

            async def consume():
                async for _ in adapter.stream("topic"):
                    started.set()

            task = asyncio.create_task(consume())
            await asyncio.wait_for(started.wait(), 5)
            pid = int(pid_path.read_text())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    async def test_invalid_json_and_missing_completion(self):
        for script in ("print('not json')", 'print(\'{"type":"delta","text":"partial"}\')'):
            with self.subTest(script=script), self.assertRaises(AdapterError):
                _ = [text async for text in self.adapter(script).stream("topic")]

    async def test_large_single_frame_and_complete_stderr_are_not_truncated(self):
        size = 5 * 1024 * 1024
        adapter = self.adapter(
            "import json,sys; sys.stdin.read(); "
            f"print(json.dumps({'{'}'type':'delta','text':'start'+'x'*{size}+'end'{'}'})); "
            "print(json.dumps({'type':'done'}))"
        )
        async with asyncio.timeout(10):
            reply = "".join([text async for text in adapter.stream("prompt")])
        self.assertEqual(reply, "start" + "x" * size + "end")
        adapter = self.adapter(
            "import sys; sys.stdin.read(); sys.stderr.write('first'+'e'*300000+'last'); sys.exit(7)"
        )
        with self.assertRaises(AdapterError) as raised:
            _ = [text async for text in adapter.stream("prompt")]
        self.assertIn("first" + "e" * 300000 + "last", str(raised.exception))

    def test_codex_reasoning_effort_is_passed_through_and_scoped_to_codex(self):
        agent = AgentConfig("codex", "codex", reasoning_effort="low")
        command = command_for(agent)
        self.assertIn('model_reasoning_effort="low"', command)
        # An override is a -c pair, and must not disturb the approval policy.
        self.assertEqual(command[command.index('model_reasoning_effort="low"') - 1], "-c")
        self.assertIn('approval_policy="never"', command)
        # Omitted, nothing is sent and the user's own codex config decides.
        self.assertFalse(
            any("model_reasoning_effort" in arg for arg in command_for(AgentConfig("c", "codex")))
        )
        for backend in ("claude", "mock"):
            with self.subTest(backend=backend), self.assertRaises(ValueError):
                AgentConfig("x", backend, reasoning_effort="low")
        for value in ("", 3, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                AgentConfig("codex", "codex", reasoning_effort=value)
