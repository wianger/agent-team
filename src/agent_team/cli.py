from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import sys
import tempfile
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

from .adapters import command_for, terminate_process
from .client import chat
from .config import DEFAULT_CONFIG, demo_config, load_config
from .server import Server
from .store import read_events


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="A shared team room for humans, Claude Code, and Codex. "
        "Run without a command to initialize and start an interactive team.",
        epilog="Startup options can omit 'start': agent-team --config team.toml "
        "--room .agent-team/default --name user. See 'agent-team start --help' for options.",
    )
    root.add_argument("--version", action="version", version="agent-team 0.2.0")
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create a team.toml configuration")
    init.add_argument("--config", type=Path, default=Path("team.toml"))
    for name, description in (
        ("start", "Start a room server and join the conversation"),
        ("serve", "Run the room server independently of human connections"),
        ("join", "Join a room from another terminal"),
        ("demo", "Run an interactive demo without accounts"),
        ("history", "Read or export the public history offline"),
        ("doctor", "Check configuration and CLI installations"),
    ):
        command = commands.add_parser(name, help=description)
        if name in {"start", "serve", "doctor"}:
            command.add_argument(
                "--config",
                type=Path,
                default=None if name == "start" else Path("team.toml"),
                help="Use an existing configuration; interactive startup otherwise creates "
                "team.toml if missing",
            )
        if name not in {"doctor", "demo"}:
            command.add_argument("--room", type=Path, default=Path(".agent-team/default"))
            # Renamed in 0.2.0; accepted only to fail with the new name rather than
            # argparse's bare "unrecognized arguments".
            command.add_argument("--session", type=Path, help=argparse.SUPPRESS)
        if name in {"start", "join", "demo"}:
            command.add_argument("--name", default="user")
            command.add_argument(
                "--plain", action="store_true", help="Line input and NDJSON output"
            )
        if name == "history":
            command.add_argument(
                "--json", action="store_true", help="Export all durable events as JSONL"
            )
    return root


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or (
        arguments[0].startswith("-") and arguments[0] not in {"-h", "--help", "--version"}
    ):
        arguments.insert(0, "start")
    return parser().parse_args(arguments)


def create_config(path: Path) -> None:
    """Publish a complete default configuration without replacing existing files."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=".agent-team-config-", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(DEFAULT_CONFIG)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path, follow_symlinks=False)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_team_config(path: Path, *, initialize=False):
    if initialize:
        try:
            path.lstat()
        except FileNotFoundError:
            try:
                create_config(path)
            except FileExistsError:
                pass  # Another invocation published its configuration first; read it as-is.
    try:
        return load_config(path)
    except FileNotFoundError as exc:
        raise ValueError(
            f"No team configuration found at {path}. "
            "Run 'agent-team' without --config to initialize the default configuration, "
            "or use --config with an existing configuration."
        ) from exc


async def reported_permission_mode(agent, workspace: Path) -> str | None:
    """The permission mode the CLI actually applies, which need not be the one asked for.

    A CLI can accept --permission-mode auto and still run in `default`, where every
    write is denied. Presence on PATH cannot tell you that, so start it and read the
    mode it reports, then stop it before it answers.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *command_for(agent, "discussion", permission_mode="full_auto"),
            cwd=workspace,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return None
    try:
        process.stdin.write(b"hi\n")
        await process.stdin.drain()
        process.stdin.close()
        async with asyncio.timeout(90):
            while line := await process.stdout.readline():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and event.get("permissionMode"):
                    return event["permissionMode"]
        return None
    except (TimeoutError, OSError):
        return None
    finally:
        await terminate_process(process)


async def run(args: argparse.Namespace) -> None:
    if getattr(args, "session", None) is not None:
        raise ValueError("Renamed in 0.2.0: --session is now --room")
    if args.command == "join":
        await chat(args.room.resolve(), args.name, args.plain)
        return
    if args.command == "doctor":
        config = load_team_config(args.config)
        missing = False
        print(f"Python {sys.version.split()[0]} | workspace: {config.workspace}")
        print(f"Team permission mode: {config.permission_mode}")
        print(f"Interaction mode: {config.interaction_mode}")
        for agent in config.agents:
            executable = agent.command[0] if agent.backend == "command" else agent.backend
            location = "built-in demo" if agent.backend == "mock" else shutil.which(executable)
            print(f"{agent.name} ({agent.backend}): {location or 'not installed / not on PATH'}")
            missing |= location is None
        unusable = False
        if config.permission_mode == "full_auto":
            for agent in config.agents:
                if agent.backend != "claude" or not shutil.which("claude"):
                    continue
                mode = await reported_permission_mode(agent, config.workspace)
                if mode == "auto":
                    print(f"{agent.name}: auto permission mode confirmed")
                    continue
                unusable = True
                print(
                    f"{agent.name}: claude applies {mode or 'no reported'} permission mode, "
                    "not 'auto', so every full_auto turn will fail before it writes. "
                    'Set permission_mode = "phase_scoped".'
                )
        print(
            "Checking permissions starts each CLI briefly; no reply is requested. "
            "Authentication and quota are not verified."
        )
        if missing:
            raise ValueError("Install the missing CLIs and retry")
        if unusable:
            raise ValueError("Resolve the permission modes above and retry")
        return
    config = (
        replace(demo_config(), interaction_mode="chatroom")
        if args.command == "demo"
        else load_team_config(
            args.config or Path("team.toml"),
            initialize=args.command == "start" and args.config is None,
        )
    )
    # A demo gets an isolated disposable room and cannot mix into a real team's history.
    context = (
        tempfile.TemporaryDirectory(prefix="agent-team-demo-")
        if args.command == "demo"
        else nullcontext()
    )
    with context as temp:
        if temp:
            config = replace(config, workspace=Path(temp))
        room_path = Path(temp) if temp else args.room.resolve()
        server = Server(config, room_path)
        await server.start()
        try:
            if args.command == "serve":
                print(
                    f"Room started: {room_path}\n"
                    f"In another terminal, run: agent-team join --room {room_path}\n"
                    "The team keeps running when all humans disconnect. "
                    "Stop this server with Ctrl-C.",
                    flush=True,
                )
                stopped = asyncio.Event()
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(sig, stopped.set)
                try:
                    await stopped.wait()
                finally:
                    for sig in (signal.SIGINT, signal.SIGTERM):
                        loop.remove_signal_handler(sig)
            else:
                stopped = asyncio.Event()
                loop = asyncio.get_running_loop()
                loop.add_signal_handler(signal.SIGTERM, stopped.set)
                client_task = asyncio.create_task(
                    chat(room_path, args.name, args.plain, stop_on_exit=True)
                )
                stop_task = asyncio.create_task(stopped.wait())
                try:
                    done, _ = await asyncio.wait(
                        [client_task, stop_task], return_when=asyncio.FIRST_COMPLETED
                    )
                    if client_task in done:
                        client_task.result()
                finally:
                    client_task.cancel()
                    stop_task.cancel()
                    await asyncio.gather(client_task, stop_task, return_exceptions=True)
                    loop.remove_signal_handler(signal.SIGTERM)
        finally:
            await server.close()


def main() -> None:
    args = parse_args()
    try:
        if args.command == "init":
            create_config(args.config)
            print(f"Created {args.config}. Run agent-team doctor, then agent-team.")
        elif args.command == "history":
            events = read_events(args.room / "events.sqlite3")
            for event in events:
                if args.json:
                    print(json.dumps(event, ensure_ascii=False))
                elif event["type"] == "message":
                    print(f"### {event['speaker']} · #{event['id']}\n\n{event['text']}\n")
        else:
            asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, EOFError) as exc:
        print(f"agent-team: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
