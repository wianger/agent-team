from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sys
from pathlib import Path

from .server import connect, encode, receive
from .streams import readline
from .workflow import PHASES

HELP = """Type a message to participate.
/pause          Pause after all active replies finish
/interrupt      Cancel all active turns and pause without leaving
/resume         Continue automatically, without a round limit
/retry [agent]  Retry one unavailable member, or all unavailable members
/redirect text  Interrupt active work and reopen discussion with new guidance
/revise text    Revisit the consensus, preserving documents and existing work
/next [agent]   Advance exactly one eligible agent turn
/status         Show active thinkers, the writer, and completed turn count
/sessions       Show private session IDs and public-message synchronization
/reset-session [agent]  Forget private session(s), preserving public history; pause
/plan           Show the current proposal, votes, and acceptance criteria
/consensus      Inspect the latest approved document and prior versions
/tasks          Show shared work, contributions, judgments, and checks
/history [id]   Read a page of 100 messages after this id
/help           Show help
/quit           Leave this terminal (Ctrl-D)
Chatroom messages join the conversation without interruption; /redirect changes direction.
Any native usage limit pauses the whole team. /status shows reset and recovery-check timing.
Known provider reset times trigger a check after 30s; unknown resets require manual recovery.
Discussion resumes only after all limited members pass checks; required votes are never waived.
Automatic checks never override manual pauses, other errors, or a server restart.
Resolve the issue and wait for active turns to stop, then /retry [agent] or /resume.
Legacy serial mode still interrupts on ordinary human messages. Use /resume after a manual pause.
"""


def clean(text: str) -> str:
    # Agent output is plain text, never interpreted as terminal control sequences.
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", text)


def parse_input(line: str) -> dict | str | None:
    line = line.strip()
    if not line:
        return None
    if not line.startswith("/"):
        return {"type": "say", "text": line}
    command, *args = line.split()
    if command in {"/redirect", "/revise"} and args:
        return {"type": "redirect", "text": line.split(maxsplit=1)[1]}
    if command in {"/quit", "/help"} and not args:
        return command[1:]
    if command in {"/pause", "/interrupt", "/resume"} and not args:
        return {"type": "control", "action": command[1:]}
    if command in {"/next", "/reset-session", "/retry"} and len(args) <= 1:
        return {"type": "control", "action": command[1:], "target": args[0] if args else None}
    if command == "/status" and not args:
        return {"type": "status"}
    if command == "/sessions" and not args:
        return {"type": "sessions"}
    if command in {"/plan", "/tasks", "/consensus"} and not args:
        return {"type": "workflow"}
    if command == "/history" and len(args) <= 1:
        after = int(args[0]) if args else 0
        if after < 0:
            raise ValueError("history id must be a nonnegative integer")
        return {"type": "history", "after": after}
    raise ValueError("Unknown command or invalid arguments; use /help")


class LiveReplies:
    """Independent draft buffers: interleaved deltas never become another agent's text."""

    def __init__(self):
        self.turns: dict[str, tuple[str, str]] = {}

    def update(self, event):
        kind, identifier = event.get("type"), event.get("turn_id")
        if kind in {"turn.started", "floor.granted"}:
            self.turns[identifier] = (event["speaker"], "")
        elif kind == "delta":
            _, text = self.turns.get(identifier, (event["speaker"], "(joined mid-turn)\n"))
            self.turns[identifier] = (event["speaker"], text + event["text"])
        elif kind in {"message", "turn.finished", "floor.released"}:
            self.turns.pop(identifier, None)


def describe_sessions(state: dict) -> str:
    lines = ["Context mode: " + state.get("context_mode", "full")]
    for agent in state.get("agents", []):
        saved = state.get("sessions", {}).get(agent["name"], {})
        if state.get("context_mode") == "full" or agent["backend"] not in {"codex", "claude"}:
            lines.append(f"{agent['name']}: full context on every turn")
            continue
        status = "uncertain / in flight" if saved.get("dirty") else "acknowledged"
        lines.append(
            f"{agent['name']}: {saved.get('session_id') or 'new session pending'} · {status} "
            f"· through #{saved.get('synced_through', 0)}"
        )
        if saved.get("reason"):
            lines.append("  " + saved["reason"])
    return "\n".join(lines) + "\n"


def describe_workflow(state: dict | None) -> str:
    if not state:
        return "Discussion-only mode.\n"
    lines = [f"Phase: {PHASES[state['phase']]} · proposal v{state['version']}"]
    if history := state.get("consensus_history"):
        latest = history[-1]
        lines.extend(
            [
                f"Latest approved consensus: v{latest['version']}",
                "Document: " + latest["document"],
                "Use /consensus to read the approved snapshot; "
                "/revise <guidance> to discuss changes.",
            ]
        )
        if state["phase"] == "discussion":
            lines.append("Under discussion: earlier approval does not authorize revised work.")
    proposal = state["proposal"]
    if not proposal:
        if base := state.get("revision_base"):
            lines.extend(
                [
                    "Previous scope (retained for revision):",
                    base["proposal"]["summary"],
                    "Existing artifacts and prior contributions remain; propose a revised plan.",
                ]
            )
        return "\n".join(lines) + "\nNo proposal yet.\n"
    lines.extend(
        [
            proposal["summary"],
            "Approvals: " + (", ".join(state["approvals"]) or "none"),
            "Acceptance: " + "; ".join(proposal["acceptance_criteria"]),
        ]
    )
    for speaker, reason in state["objections"].items():
        lines.append(f"Objection · {speaker}: {reason}")
    for task in proposal["tasks"]:
        lines.append(
            f"[{task['status']}] {task['id']} · shared · {task['title']} "
            f"· revision {task.get('revision', 0)}"
        )
        lines.append("  " + task["details"])
        if task["depends_on"]:
            lines.append("  Dependencies: " + ", ".join(task["depends_on"]))
        for contribution in task.get("contributions", []):
            lines.append(
                f"  r{contribution['revision']} by {contribution['author']}: "
                + contribution["summary"]
            )
            for judgment in contribution["judgments"]:
                lines.append(
                    f"    {judgment['speaker']} · {judgment['action']}: {judgment['evidence']}"
                )
        if task["report"]:
            lines.append("  Files: " + ", ".join(task["report"]["files"]))
            lines.append("  Checks: " + task["report"]["tests"])
    for item in state.get("feedback", []):
        lines.append(f"Feedback · {item['speaker']}: {item['evidence']}")
    lines.append("Integration approvals: " + (", ".join(state["review_approvals"]) or "none"))
    lines.extend("Acceptance command: " + repr(argv) for argv in proposal["acceptance_checks"])
    lines.extend(f"Acceptance exit code: {r['exit_code']}" for r in state["acceptance_results"])
    return "\n".join(lines) + "\n"


async def plain_chat(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, welcome: dict
) -> None:
    """NDJSON output and line input, useful for pipes and scripted clients."""
    pipe_reader = asyncio.StreamReader()
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(pipe_reader), sys.stdin
    )

    async def output() -> None:
        event = welcome
        while True:
            print(json.dumps(event, ensure_ascii=False), flush=True)
            event = await receive(reader)

    async def inputs() -> None:
        while raw := await readline(pipe_reader):
            try:
                request = parse_input(raw.decode())
                if request == "quit":
                    return
                if request == "help":
                    print(HELP, file=sys.stderr)
                elif isinstance(request, dict):
                    writer.write(encode(request))
                    await writer.drain()
            except ValueError as exc:
                print(str(exc), file=sys.stderr)

    tasks = [asyncio.create_task(output()), asyncio.create_task(inputs())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            with contextlib.suppress(EOFError):
                task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        transport.close()


async def chat(
    session: Path, name: str, plain: bool = False, *, stop_on_exit: bool = False
) -> None:
    reader, writer = await connect(session, name)
    try:
        hello = await receive(reader)
        if hello.get("type") != "welcome":
            raise ValueError(hello.get("text", "Failed to join the session"))
        if plain or not sys.stdin.isatty() or not sys.stdout.isatty():
            await plain_chat(reader, writer, hello)
        else:
            from .tui import TeamUI

            await TeamUI(name, session=session, stop_on_exit=stop_on_exit).run(
                reader, writer, hello
            )
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()
