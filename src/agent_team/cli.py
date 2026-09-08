from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import signal
import sys
import tempfile
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

from .client import chat
from .config import DEFAULT_CONFIG, demo_config, load_config
from .server import Server
from .store import read_events


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="A shared team room for humans, Claude Code, and Codex"
    )
    root.add_argument("--version", action="version", version="agent-team 0.1.0")
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create a team.toml configuration")
    init.add_argument("--config", type=Path, default=Path("team.toml"))
    for name, description in (
        ("start", "Start a session server and join the conversation"),
        ("serve", "Run the session server independently of human connections"),
        ("join", "Join a session from another terminal"),
        ("demo", "Run an interactive demo without accounts"),
        ("history", "Read or export the public history offline"),
        ("doctor", "Check configuration and CLI installations"),
    ):
        command = commands.add_parser(name, help=description)
        if name in {"start", "serve", "doctor"}:
            command.add_argument("--config", type=Path, default=Path("team.toml"))
        if name not in {"doctor", "demo"}:
            command.add_argument("--session", type=Path, default=Path(".agent-team/default"))
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


async def run(args: argparse.Namespace) -> None:
    if args.command == "join":
        await chat(args.session.resolve(), args.name, args.plain)
        return
    if args.command == "doctor":
        config = load_config(args.config)
        missing = False
        print(f"Python {sys.version.split()[0]} | workspace: {config.workspace}")
        print(f"Team permission mode: {config.permission_mode}")
        print(f"Interaction mode: {config.interaction_mode}")
        for agent in config.agents:
            executable = agent.command[0] if agent.backend == "command" else agent.backend
            location = "built-in demo" if agent.backend == "mock" else shutil.which(executable)
            print(f"{agent.name} ({agent.backend}): {location or 'not installed / not on PATH'}")
            missing |= location is None
        print("No models called. Live calls require authenticated CLIs and available quota.")
        if missing:
            raise ValueError("Install the missing CLIs and retry")
        return
    config = (
        replace(demo_config(), interaction_mode="chatroom")
        if args.command == "demo"
        else load_config(args.config)
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
        session = Path(temp) if temp else args.session.resolve()
        server = Server(config, session)
        await server.start()
        try:
            if args.command == "serve":
                print(
                    f"Session started: {session}\n"
                    f"In another terminal, run: agent-team join --session {session}\n"
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
                client_task = asyncio.create_task(chat(session, args.name, args.plain))
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
    args = parser().parse_args()
    try:
        if args.command == "init":
            with args.config.open("x") as handle:
                handle.write(DEFAULT_CONFIG)
            print(f"Created {args.config}. Run agent-team doctor, then agent-team start.")
        elif args.command == "history":
            events = read_events(args.session / "events.sqlite3")
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
