from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agent_team.adapters import command_for
from agent_team.config import DEFAULT_CONFIG, AgentConfig, TeamConfig, demo_config, load_config
from agent_team.context import SHARED_RESPONSIBILITIES, build_prompt
from agent_team.workflow import Workflow


class SharedResponsibilityTests(unittest.TestCase):
    def setUp(self):
        self.config = TeamConfig(
            agents=(AgentConfig("claude", "claude"), AgentConfig("codex", "codex"))
        )
        self.messages = [{"id": 1, "role": "user", "speaker": "human", "text": "Build together"}]

    def test_generated_checked_in_and_demo_configs_have_no_assigned_focus(self):
        self.assertEqual(Path("team.toml").read_text(), DEFAULT_CONFIG)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "team.toml"
            path.write_text(DEFAULT_CONFIG)
            generated = load_config(path)
        for config in (generated, load_config(Path("team.toml")), demo_config()):
            self.assertTrue(all(agent.role == "" for agent in config.agents))
        self.assertEqual([agent.name for agent in demo_config().agents], ["member_a", "member_b"])

    def test_all_backends_receive_identical_default_responsibilities(self):
        agents = (
            *self.config.agents,
            AgentConfig("mock", "mock"),
            AgentConfig("custom", "command", command=("custom-agent",)),
        )
        config = replace(self.config, agents=agents, workflow="discussion")
        bodies = []
        for agent in agents:
            prompt = build_prompt(agent, config, self.messages)
            self.assertIn(SHARED_RESPONSIBILITIES, prompt)
            self.assertNotIn("Optional additional focus:", prompt)
            self.assertNotIn("Your perspective:", prompt)
            self.assertIn("Discussion only: do not modify files", prompt)
            # Only the first-line identity differs in an otherwise identical turn.
            bodies.append(prompt.split("\n", 1)[1])
        self.assertEqual(len(set(bodies)), 1)

    def test_optional_focus_supplements_shared_duties_without_changing_peer_prompt(self):
        focus = "Pay extra attention to security risks."
        focused = replace(self.config.agents[0], role=focus)
        config = replace(self.config, agents=(focused, self.config.agents[1]))
        prompt = build_prompt(focused, config, self.messages, Workflow(config))
        self.assertIn(SHARED_RESPONSIBILITIES, prompt)
        self.assertIn("Optional additional focus: " + focus, prompt)
        self.assertIn("supplements your shared responsibilities", prompt)
        self.assertIn("no exclusive assignment or permissions override", prompt)
        self.assertIn("Read and discuss only; do not modify files yet", prompt)
        peer = build_prompt(config.agents[1], config, self.messages, Workflow(config))
        self.assertNotIn(focus, peer)
        self.assertIn(SHARED_RESPONSIBILITIES, peer)

    def test_empty_or_whitespace_focus_adds_no_specialization(self):
        default = build_prompt(self.config.agents[0], self.config, self.messages)
        for role in ("", " ", "\n\t"):
            agent = replace(self.config.agents[0], role=role)
            self.assertEqual(build_prompt(agent, self.config, self.messages), default)

    def test_focus_never_changes_backend_permissions_or_commands(self):
        for agent in self.config.agents:
            for phase in ("discussion", "planning", "implementation", "judging", "review"):
                with self.subTest(backend=agent.backend, phase=phase):
                    focused = replace(agent, role="Pay extra attention to security risks.")
                    self.assertEqual(command_for(agent, phase), command_for(focused, phase))

    def test_shared_duties_and_optional_focus_are_refreshed_on_incremental_turns(self):
        agent = replace(self.config.agents[0], role="Check accessibility.")
        prompt = build_prompt(agent, self.config, self.messages, after=1, resumed=True)
        self.assertIn(SHARED_RESPONSIBILITIES, prompt)
        self.assertIn("Optional additional focus: Check accessibility.", prompt)
        self.assertTrue(prompt.endswith("[]"))
