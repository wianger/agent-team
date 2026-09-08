from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path

from agent_team.client import LiveReplies, parse_input
from agent_team.config import DEFAULT_CONFIG, AgentConfig, TeamConfig, demo_config, load_config
from agent_team.store import read_events


class ConfigurationTests(unittest.TestCase):
    def test_default_configs_disable_hard_timeouts_but_enable_idle_notices(self):
        self.assertEqual(Path("team.toml").read_text(), DEFAULT_CONFIG)
        configs = [demo_config(), load_config(Path("team.toml"))]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "team.toml"
            path.write_text('[team]\nworkflow="discussion"\n[[agents]]\nname="a"\nbackend="mock"\n')
            configs.append(load_config(path))
        for config in configs:
            self.assertEqual(
                (config.turn_timeout, config.work_timeout, config.check_timeout), (0, 0, 0)
            )
            self.assertEqual(config.idle_warning_seconds, 120)

    def test_idle_warning_validation_and_explicit_hard_timeout_preserved(self):
        for value in (-1, True, float("nan"), float("inf"), "8"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                TeamConfig(
                    workflow="discussion",
                    agents=(AgentConfig("a", "mock"),),
                    idle_warning_seconds=value,
                )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "team.toml"
            path.write_text(
                '[team]\nworkflow="discussion"\nturn_timeout=7\nwork_timeout=8\n'
                "check_timeout=9\nidle_warning_seconds=0\n"
                '[[agents]]\nname="a"\nbackend="mock"\n'
            )
            config = load_config(path)
        self.assertEqual(
            (config.turn_timeout, config.work_timeout, config.check_timeout), (7, 8, 9)
        )
        self.assertEqual(config.idle_warning_seconds, 0)

    def test_timeouts_and_command_shape(self):
        for value in (-1, True, float("nan"), float("inf"), "8"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                TeamConfig(
                    workflow="discussion",
                    agents=(AgentConfig("a", "mock"),),
                    turn_timeout=value,
                )
        config = TeamConfig(
            workflow="discussion",
            agents=(AgentConfig("a", "mock"),),
            turn_timeout=0,
        )
        self.assertEqual(config.turn_timeout, 0)
        with self.assertRaises(ValueError):
            AgentConfig("a", "command", command="echo unsafe shell")
        with self.assertRaises(ValueError):
            TeamConfig(agents=(AgentConfig("a", "mock"), AgentConfig("a", "codex")))

    def test_bad_tables_and_relative_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "team.toml"
            for text in ("team = 3", '[agents]\nname = "a"', "[team]\nworkspace = 8"):
                config_path.write_text(text)
                with self.subTest(text=text), self.assertRaises(ValueError):
                    load_config(config_path)
            config_path.write_text(
                '[team]\nworkflow = "discussion"\nworkspace = "."\n'
                '[[agents]]\nname="a"\nbackend="mock"'
            )
            self.assertEqual(load_config(config_path).workspace, Path(directory).resolve())

    def test_build_needs_independent_peers_and_old_caps_need_migration(self):
        with self.assertRaisesRegex(ValueError, "at least two"):
            TeamConfig(agents=(AgentConfig("a", "mock"),))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "team.toml"
            for key in ("max_turns", "max_work_turns", "max_context_chars"):
                path.write_text(f"[team]\n{key} = 8\n")
                with self.assertRaisesRegex(ValueError, "obsolete limit"):
                    load_config(path)

    def test_commands(self):
        self.assertEqual(
            parse_input("/next codex"), {"type": "control", "action": "next", "target": "codex"}
        )
        self.assertEqual(parse_input("hello"), {"type": "say", "text": "hello"})
        for command in ("/resume unexpected", "/history -1", "/history bad", "/unknown"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                parse_input(command)

    def test_chatroom_configuration_and_commands(self):
        self.assertEqual(load_config(Path("team.toml")).interaction_mode, "chatroom")
        self.assertEqual(
            parse_input("/redirect Keep the API  but change storage"),
            {"type": "redirect", "text": "Keep the API  but change storage"},
        )
        self.assertEqual(
            parse_input("/retry claude"), {"type": "control", "action": "retry", "target": "claude"}
        )
        with self.assertRaises(ValueError):
            parse_input("/redirect")
        with self.assertRaises(ValueError):
            TeamConfig(
                agents=(AgentConfig("a", "mock"),),
                workflow="discussion",
                interaction_mode="unknown",
            )

    def test_interleaved_live_replies_remain_separate(self):
        replies = LiveReplies()
        for turn, speaker in (("1", "a"), ("2", "b")):
            replies.update({"type": "turn.started", "turn_id": turn, "speaker": speaker})
        for turn, speaker, text in (("1", "a", "A"), ("2", "b", "B"), ("1", "a", " again")):
            replies.update({"type": "delta", "turn_id": turn, "speaker": speaker, "text": text})
        self.assertEqual(replies.render(), "a › A again\n\nb › B")
        replies.update({"type": "message", "turn_id": "2"})
        self.assertEqual(replies.render(), "a › A again")
        replies.update({"type": "turn.finished", "turn_id": "1"})
        self.assertFalse(replies.turns)


class CLISmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_serve_continues_after_plain_join_eof_and_replays_on_reconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config_path, session = path / "team.toml", path / "session"
            config_path.write_text(
                '[team]\nworkflow="discussion"\nturn_delay=0\n'
                '[[agents]]\nname="member"\nbackend="mock"\n'
            )
            server = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "agent_team",
                "serve",
                "--config",
                str(config_path),
                "--session",
                str(session),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            processes = [server]

            async def join():
                client = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "agent_team",
                    "join",
                    "--plain",
                    "--session",
                    str(session),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                processes.append(client)
                return client

            try:
                async with asyncio.timeout(10):
                    self.assertIn(b"Session started:", await server.stdout.readline())
                    client = await join()
                    self.assertEqual(json.loads(await client.stdout.readline())["type"], "welcome")
                    client.stdin.write(b"Continue without observers\n")
                    await client.stdin.drain()
                    while True:
                        line = await client.stdout.readline()
                        self.assertTrue(line, "Client unexpectedly exited")
                        if json.loads(line)["type"] == "floor.granted":
                            break
                    # communicate closes stdin, exercising the same EOF path as Ctrl-D.
                    _, stderr = await client.communicate()
                    self.assertEqual(client.returncode, 0, stderr.decode())

                    while True:
                        events = read_events(session / "events.sqlite3")
                        if any(e["type"] == "agent.passed" for e in events):
                            break
                        await asyncio.sleep(0.01)
                    self.assertIsNone(server.returncode)
                    self.assertFalse(any(e["type"] == "room.control" for e in events))
                    messages = [e for e in events if e["type"] == "message"]
                    self.assertEqual([m["speaker"] for m in messages], ["user", "member"])

                    client = await join()
                    welcome = json.loads(await client.stdout.readline())
                    self.assertEqual(welcome["state"]["reason"], "all_passed")
                    self.assertEqual(welcome["state"]["turns"], 2)
                    replay = [json.loads(await client.stdout.readline()) for _ in messages]
                    self.assertEqual(replay, [{**m, "replay": True} for m in messages])
                    _, stderr = await client.communicate(b"/quit\n")
                    self.assertEqual(client.returncode, 0, stderr.decode())
                    server.send_signal(signal.SIGTERM)
                    _, stderr = await server.communicate()
                    self.assertEqual(server.returncode, 0, stderr.decode())
                    self.assertFalse((session / "connection.json").exists())
            finally:
                for process in processes:
                    if process.returncode is None:
                        process.kill()
                        await process.communicate()

    async def test_plain_demo_runs_complete_discussion_and_exits(self):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "agent_team",
            "demo",
            "--plain",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(10):
                hello = json.loads(await process.stdout.readline())
                self.assertEqual(hello["type"], "welcome")
                self.assertEqual(hello["state"]["interaction_mode"], "chatroom")
                process.stdin.write(b"Design a real-time chat room\n")
                await process.stdin.drain()
                messages = []
                while True:
                    line = await process.stdout.readline()
                    self.assertTrue(line, "CLI unexpectedly exited")
                    event = json.loads(line)
                    if event["type"] == "message":
                        messages.append(event)
                    if event["type"] == "state" and event["reason"] == "completed":
                        break
                self.assertEqual(
                    {m["speaker"] for m in messages}, {"user", "member_a", "member_b", "system"}
                )
                final = messages[-1]["workflow"]
                self.assertEqual(final["phase"], "completed")
                self.assertEqual(final["checks_result"][0]["exit_code"], 0)
                self.assertTrue((Path(final["workspace"]) / "hello.py").is_file())
                process.stdin.write(b"/quit\n")
                await process.stdin.drain()
                await process.communicate()
                self.assertEqual(process.returncode, 0)
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()

    async def test_start_sigterm_cleans_connection_and_active_child(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config_path, child_script = path / "team.toml", path / "child.py"
            pid_path = path / "child.pid"
            child_script.write_text(
                "import os,pathlib,time\n"
                f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()))\n"
                'print(\'{"type":"delta","text":"working"}\', flush=True)\n'
                "time.sleep(60)\n"
            )
            config_path.write_text(
                '[team]\nworkflow="discussion"\nidle_warning_seconds=0.02\n'
                '[[agents]]\nname="custom"\nbackend="command"\n'
                f"command = {json.dumps([sys.executable, str(child_script)])}\n"
            )
            session = path / "session"
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "agent_team",
                "start",
                "--plain",
                "--config",
                str(config_path),
                "--session",
                str(session),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                async with asyncio.timeout(10):
                    self.assertEqual(json.loads(await process.stdout.readline())["type"], "welcome")
                    process.stdin.write(b"topic\n")
                    await process.stdin.drain()
                    while True:
                        line = await process.stdout.readline()
                        self.assertTrue(line)
                        if json.loads(line)["type"] == "turn.idle":
                            break
                    child_pid = int(pid_path.read_text())
                    process.send_signal(signal.SIGTERM)
                    _, stderr = await process.communicate()
                    self.assertEqual(process.returncode, 0, stderr.decode())
                    self.assertFalse((session / "connection.json").exists())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(child_pid, 0)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.communicate()
