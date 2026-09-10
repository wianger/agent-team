from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agent_team.adapters import AdapterError, CLIAdapter, EventDecoder, command_for
from agent_team.config import DEFAULT_CONFIG, AgentConfig, TeamConfig, load_config
from agent_team.context import build_prompt
from agent_team.engine import Room
from agent_team.store import Store
from agent_team.workflow import Workflow

PHASES = ("planning", "implementation", "judging", "review", "discussion", "chat")


class PermissionTests(unittest.TestCase):
    def test_shipped_and_generated_configs_enable_full_auto_explicitly(self):
        self.assertEqual(Path("team.toml").read_text(), DEFAULT_CONFIG)
        self.assertEqual(load_config(Path("team.toml")).permission_mode, "full_auto")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "team.toml"
            path.write_text(DEFAULT_CONFIG)
            self.assertEqual(load_config(path).permission_mode, "full_auto")
            path.write_text('[team]\nworkflow="discussion"\n[[agents]]\nname="a"\nbackend="mock"\n')
            self.assertEqual(load_config(path).permission_mode, "phase_scoped")

    def test_permission_mode_validation(self):
        config = TeamConfig(workflow="discussion", agents=(AgentConfig("a", "mock"),))
        for value in ("auto", "full-access", "", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(config, permission_mode=value)
            with self.assertRaises(ValueError):
                command_for(AgentConfig("a", "codex"), permission_mode=value)

    def test_codex_full_access_on_every_phase_and_resume(self):
        identifier = str(uuid.uuid4())
        for phase in PHASES:
            for resume in (False, True):
                with self.subTest(phase=phase, resume=resume):
                    command = command_for(
                        AgentConfig("a", "codex", model="chosen-model"),
                        phase,
                        permission_mode="full_auto",
                        persist_session=True,
                        session_id=identifier if resume else None,
                    )
                    self.assertEqual(command[command.index("--sandbox") + 1], "danger-full-access")
                    self.assertIn('approval_policy="never"', command)
                    self.assertEqual(command[command.index("--model") + 1], "chosen-model")
                    self.assertNotIn("--last", command)
                    if resume:
                        self.assertEqual(command[-3:], ["resume", identifier, "-"])
                        self.assertLess(command.index("--sandbox"), command.index("resume"))

    def test_claude_auto_does_not_preapprove_tools_or_bypass_checks(self):
        identifier = str(uuid.uuid4())
        for phase in PHASES:
            for resume in (False, True):
                with self.subTest(phase=phase, resume=resume):
                    command = command_for(
                        AgentConfig("a", "claude"),
                        phase,
                        permission_mode="full_auto",
                        persist_session=True,
                        session_id=identifier if resume else None,
                    )
                    self.assertEqual(command[command.index("--permission-mode") + 1], "auto")
                    self.assertEqual(command[command.index("--permission-prompts") + 1], "none")
                    for flag in (
                        "--allowedTools",
                        "bypassPermissions",
                        "--dangerously-skip-permissions",
                    ):
                        self.assertNotIn(flag, command)
                    self.assertIn("--strict-mcp-config", command)
                    self.assertEqual(command[command.index("--tools") + 1], "default")
                    if resume:
                        self.assertEqual(command[command.index("--resume") + 1], identifier)

    def test_room_passes_configured_policy_to_both_adapters_and_reports_it(self):
        config = TeamConfig(
            agents=(AgentConfig("a", "codex"), AgentConfig("b", "claude")),
            permission_mode="full_auto",
        )
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "events.sqlite3")
            try:
                room = Room(config, store, lambda event: None)
                self.assertEqual(room.status()["permission_mode"], "full_auto")
                self.assertTrue(
                    all(a.permission_mode == "full_auto" for a in room.adapters.values())
                )
            finally:
                store.close()

    def test_full_access_does_not_expand_workflow_scope(self):
        config = replace(load_config(Path("team.toml")), permission_mode="full_auto")
        for agent in config.agents:
            for concurrent in (False, True):
                with self.subTest(backend=agent.backend, concurrent=concurrent):
                    prompt = build_prompt(
                        agent, config, [], Workflow(config), concurrent=concurrent
                    )
                    self.assertIn("full_auto throughout every phase", prompt)
                    self.assertIn("must not modify project files", prompt)
                    self.assertIn("Read and discuss only; do not modify files yet", prompt)
                    self.assertIn("not a tool or network restriction", prompt)
                    self.assertIn("Do not bypass a denied action", prompt)

    def test_full_auto_allows_research_in_chat_and_discussion_only_workflows(self):
        config = load_config(Path("team.toml"))
        for mode in ("full_auto", "phase_scoped"):
            for lane in ("work", "chat"):
                for concurrent in (False, True):
                    with self.subTest(mode=mode, lane=lane, concurrent=concurrent):
                        prompt = build_prompt(
                            config.agents[0],
                            replace(config, workflow="discussion", permission_mode=mode),
                            [],
                            concurrent=concurrent,
                            lane=lane,
                        )
                        self.assertIn("do not modify files", prompt.lower())
                        if mode == "full_auto":
                            self.assertIn("You may use tools for research", prompt)
                            self.assertNotIn("Do not run tools", prompt)
                            self.assertNotIn("or use external tools", prompt)
                        elif concurrent and lane == "chat":
                            self.assertIn("Do not run tools", prompt)
                        else:
                            self.assertIn("or use external tools", prompt)

    def test_full_auto_chat_research_does_not_grant_formal_work_authority(self):
        config = replace(load_config(Path("team.toml")), permission_mode="full_auto")
        workflow = Workflow(config)
        workflow.data["phase"] = "implementation"
        prompt = build_prompt(config.agents[0], config, [], workflow, concurrent=True, lane="chat")
        self.assertIn("You may use tools for research", prompt)
        self.assertIn("Do not modify files", prompt)
        self.assertIn("Do not issue votes, checkpoints, or verdicts", prompt)
        self.assertIn("request_revision", prompt)

    def test_command_backends_are_not_given_native_permission_flags(self):
        agent = AgentConfig("custom", "command", command=("custom-agent", "--flag"))
        self.assertEqual(command_for(agent, permission_mode="full_auto"), list(agent.command))


class AutoModeDecoderTests(unittest.TestCase):
    def test_confirmed_auto_can_finish_and_nested_modes_do_not_override_it(self):
        decoder = EventDecoder("claude", expected_permission_mode="auto")
        decoder.feed({"type": "system", "subtype": "init", "permissionMode": "auto"})
        decoder.feed(
            {
                "type": "system",
                "subtype": "init",
                "parent_tool_use_id": "nested",
                "permissionMode": "plan",
            }
        )
        decoder.feed({"type": "result", "subtype": "success", "result": "done"})
        decoder.finish()
        self.assertEqual(decoder.permission_mode, "auto")

    def test_auto_unavailable_or_missing_confirmation_is_rejected(self):
        for mode in ("default", "manual", "dontAsk", "bypassPermissions", None):
            decoder = EventDecoder("claude", expected_permission_mode="auto")
            with self.subTest(mode=mode), self.assertRaisesRegex(AdapterError, "requested auto"):
                decoder.feed({"type": "system", "subtype": "init", "permissionMode": mode})
        decoder = EventDecoder("claude", expected_permission_mode="auto")
        decoder.feed({"type": "result", "result": "unverified reply"})
        with self.assertRaisesRegex(AdapterError, "omitted confirmation"):
            decoder.finish()

    def test_reported_mid_turn_mode_change_is_rejected(self):
        decoder = EventDecoder("claude", expected_permission_mode="auto")
        decoder.feed({"type": "system", "subtype": "init", "permissionMode": "auto"})
        with self.assertRaisesRegex(AdapterError, "retain"):
            decoder.feed({"type": "system", "subtype": "status", "permissionMode": "default"})


class AutoModeProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_enforces_auto_confirmation_on_real_subprocess_output(self):
        for mode in ("auto", "default"):
            frames = [
                {"type": "system", "subtype": "init", "permissionMode": mode},
                {"type": "result", "result": "done"},
            ]
            script = (
                "import sys; sys.stdin.read(); print("
                + repr("\n".join(map(json.dumps, frames)))
                + ")"
            )
            adapter = CLIAdapter(
                AgentConfig("a", "claude"), Path.cwd(), permission_mode="full_auto"
            )
            with patch(
                "agent_team.adapters.command_for", return_value=[sys.executable, "-c", script]
            ) as command:
                async with asyncio.timeout(3):
                    if mode == "auto":
                        self.assertEqual([text async for text in adapter.stream("topic")], ["done"])
                    else:
                        with self.assertRaisesRegex(AdapterError, "requested auto"):
                            _ = [text async for text in adapter.stream("topic")]
                self.assertEqual(command.call_args.kwargs["permission_mode"], "full_auto")
