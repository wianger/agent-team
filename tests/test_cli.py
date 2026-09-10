from __future__ import annotations

import asyncio
import io
import json
import os
import signal
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import chdir, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agent_team.cli import create_config, load_team_config, main, parse_args
from agent_team.client import LiveReplies, parse_input
from agent_team.config import DEFAULT_CONFIG, AgentConfig, TeamConfig, demo_config, load_config
from agent_team.store import read_events


class ArgumentTests(unittest.TestCase):
    def test_no_arguments_match_explicit_start_defaults(self):
        self.assertEqual(parse_args([]), parse_args(["start"]))
        self.assertEqual(parse_args([]).command, "start")

    def test_implicit_start_accepts_options_without_changing_the_argument_list(self):
        arguments = [
            "--config",
            "project settings.toml",
            "--room",
            "join",
            "--name",
            "observer",
            "--plain",
        ]
        original = arguments.copy()
        self.assertEqual(parse_args(arguments), parse_args(["start", *arguments]))
        self.assertEqual(arguments, original)
        self.assertEqual(parse_args(arguments).room, Path("join"))

    def test_existing_subcommands_keep_their_meaning(self):
        for command in ("init", "start", "serve", "join", "demo", "doctor", "history"):
            with self.subTest(command=command):
                self.assertEqual(parse_args([command]).command, command)

    def test_help_and_version_remain_top_level_actions(self):
        for option, expected in (("--help", "Run without a command"), ("--version", "agent-team")):
            with self.subTest(option=option), redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as caught:
                    parse_args([option])
                self.assertEqual(caught.exception.code, 0)
                self.assertIn(expected, output.getvalue())

    def test_unknown_commands_and_options_are_not_treated_as_ideas(self):
        for arguments in (["strat"], ["--unknown"], ["--name", "user", "unexpected"]):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    parse_args(arguments)
                self.assertEqual(caught.exception.code, 2)

    def test_entrypoint_with_no_arguments_runs_interactive_start(self):
        with (
            patch.object(sys, "argv", ["agent-team"]),
            patch("agent_team.cli.run", new_callable=AsyncMock) as run,
        ):
            main()
        run.assert_awaited_once_with(parse_args(["start"]))


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
                (config.turn_timeout, config.work_timeout, config.acceptance_timeout), (0, 0, 0)
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
                "acceptance_timeout=9\nidle_warning_seconds=0\n"
                '[[agents]]\nname="a"\nbackend="mock"\n'
            )
            config = load_config(path)
        self.assertEqual(
            (config.turn_timeout, config.work_timeout, config.acceptance_timeout), (7, 8, 9)
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
        self.assertEqual(replies.turns, {"1": ("a", "A again"), "2": ("b", "B")})
        replies.update({"type": "message", "turn_id": "2"})
        self.assertEqual(replies.turns, {"1": ("a", "A again")})
        replies.update({"type": "turn.finished", "turn_id": "1"})
        self.assertFalse(replies.turns)


class BootstrapTests(unittest.TestCase):
    def test_bare_entrypoint_initializes_and_enters_the_chat_without_model_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with (
                chdir(path),
                patch.object(sys, "argv", ["agent-team"]),
                patch("agent_team.cli.chat", new_callable=AsyncMock) as chat,
                patch(
                    "agent_team.resident.JsonProcess.start_process", new_callable=AsyncMock
                ) as native,
            ):
                main()
            chat.assert_awaited_once_with(
                path / ".agent-team/default", "user", False, stop_on_exit=True
            )
            native.assert_not_awaited()
            self.assertEqual((path / "team.toml").read_text(), DEFAULT_CONFIG)
            self.assertEqual(load_config(path / "team.toml").workspace, path)
            self.assertFalse((path / ".agent-team/default/connection.json").exists())

    def test_existing_configuration_is_never_replaced_or_repaired_automatically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "team.toml"
            original = '[team]\nworkflow="discussion"\n[[agents]]\nname="solo"\nbackend="mock"\n'
            path.write_text(original)
            modified = path.stat().st_mtime_ns
            config = load_team_config(path, initialize=True)
            self.assertEqual(config.agents[0].name, "solo")
            self.assertEqual(path.read_text(), original)
            self.assertEqual(path.stat().st_mtime_ns, modified)
            with self.assertRaises(FileExistsError):
                create_config(path)
            self.assertEqual(path.read_text(), original)
            path.write_text("[broken")
            with self.assertRaises(ValueError):
                load_team_config(path, initialize=True)
            self.assertEqual(path.read_text(), "[broken")

    def test_dangling_config_symlinks_are_preserved_instead_of_creating_their_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            path, target = Path(directory) / "team.toml", Path(directory) / "missing.toml"
            path.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "No team configuration"):
                load_team_config(path, initialize=True)
            self.assertTrue(path.is_symlink())
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_failed_atomic_creation_leaves_no_partial_config_or_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "team.toml"
            with patch("agent_team.cli.os.link", side_effect=OSError("Publication failed")):
                with self.assertRaises(OSError):
                    load_team_config(path, initialize=True)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_concurrent_initialization_reads_one_complete_config_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "team.toml"
            with ThreadPoolExecutor(max_workers=4) as workers:
                configs = list(
                    workers.map(lambda _: load_team_config(path, initialize=True), range(8))
                )
            self.assertTrue(all(c.workspace == Path(directory) for c in configs))
            self.assertEqual(path.read_text(), DEFAULT_CONFIG)
            self.assertEqual(list(Path(directory).iterdir()), [path])


class CLISmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_project_starts_directly_with_default_agents_and_clean_jsonl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "agent_team",
                "--plain",
                cwd=path,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                async with asyncio.timeout(5):
                    welcome = json.loads(await process.stdout.readline())
                    self.assertEqual(welcome["type"], "welcome")
                    self.assertEqual(welcome["state"]["reason"], "waiting")
                    self.assertEqual(
                        [a["backend"] for a in welcome["state"]["agents"]], ["claude", "codex"]
                    )
                    self.assertEqual(welcome["state"]["interaction_mode"], "chatroom")
                    self.assertEqual(welcome["state"]["permission_mode"], "full_auto")
                    output, errors = await process.communicate(b"/quit\n")
                    self.assertEqual(process.returncode, 0, errors.decode())
                    for line in output.splitlines():
                        self.assertIsInstance(json.loads(line), dict)
            finally:
                if process.returncode is None:
                    process.terminate()
                    await process.wait()
            self.assertEqual((path / "team.toml").read_text(), DEFAULT_CONFIG)
            events = read_events(path / ".agent-team/default/events.sqlite3")
            self.assertFalse(any(e["type"] in {"turn.started", "floor.granted"} for e in events))

    async def test_implicit_start_creates_and_recovers_a_mock_room_without_rewriting_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config_path, session = path / "team.toml", path / "session"
            config_text = (
                '[team]\nworkflow="discussion"\ninteraction_mode="chatroom"\nturn_delay=0\n'
                '[[agents]]\nname="member"\nbackend="mock"\n'
            )
            config_path.write_text(config_text)
            for recovered in (False, True):
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "agent_team",
                    "--plain",
                    "--config",
                    str(config_path),
                    "--room",
                    str(session),
                    "--name",
                    "observer",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    async with asyncio.timeout(10):
                        greeting = json.loads(await process.stdout.readline())
                        self.assertEqual(greeting["type"], "welcome")
                        self.assertEqual(
                            greeting["state"]["reason"], "restart" if recovered else "waiting"
                        )
                        if recovered:
                            self.assertTrue(greeting["state"]["paused"])
                            self.assertGreater(greeting["state"]["messages"], 0)
                        else:
                            process.stdin.write(b"Discuss a local notes tool.\n")
                            await process.stdin.drain()
                            while True:
                                raw = await process.stdout.readline()
                                self.assertTrue(raw, "Implicit start unexpectedly exited")
                                event = json.loads(raw)
                                if event["type"] == "message" and event["role"] == "agent":
                                    break
                        _, errors = await process.communicate(b"/quit\n")
                        self.assertEqual(process.returncode, 0, errors.decode())
                finally:
                    if process.returncode is None:
                        process.terminate()
                        await process.wait()
                self.assertEqual(config_path.read_text(), config_text)
                self.assertFalse((session / "connection.json").exists())
            self.assertTrue(
                any(e.get("speaker") == "observer" for e in read_events(session / "events.sqlite3"))
            )

    async def test_explicit_missing_config_and_read_only_commands_never_initialize(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for arguments in (
                ["--config", "team.toml"],
                ["--config", "missing.toml"],
                ["doctor"],
                ["serve"],
                ["--help"],
                ["--version"],
            ):
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "agent_team",
                    *arguments,
                    cwd=path,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    async with asyncio.timeout(5):
                        output, errors = await process.communicate()
                finally:
                    if process.returncode is None:
                        process.terminate()
                        await process.wait()
                if arguments in (["--help"], ["--version"]):
                    self.assertEqual(process.returncode, 0, errors.decode())
                    self.assertIn(b"agent-team", output)
                else:
                    self.assertEqual(process.returncode, 1)
                    self.assertIn(b"without --config", errors)
                    self.assertNotIn(b"Traceback", errors)
                self.assertEqual(await asyncio.to_thread(lambda: list(path.iterdir())), [])

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
                "--room",
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
                    "--room",
                    str(session),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                processes.append(client)
                return client

            try:
                async with asyncio.timeout(10):
                    self.assertIn(b"Room started:", await server.stdout.readline())
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
                self.assertEqual(final["acceptance_results"][0]["exit_code"], 0)
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
                "--room",
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
                    # A terminating child stays visible to kill(pid, 0) as a zombie until
                    # it is reparented and reaped, so wait for the exit rather than racing it.
                    while True:
                        try:
                            os.kill(child_pid, 0)
                        except ProcessLookupError:
                            break
                        await asyncio.sleep(0.01)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.communicate()
