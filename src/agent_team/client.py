from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sys
from pathlib import Path

from .server import connect, encode, receive
from .streams import readline
from .workflow import PHASES, visible_text

HELP = """Type a message to participate.
/pause          Pause after all active replies finish
/interrupt      Cancel all active turns and pause (Ctrl-C)
/resume         Continue automatically, without a round limit
/retry [agent]  Retry one unavailable member, or all unavailable members
/redirect text  Interrupt active work and reopen discussion with new guidance
/revise text    Revisit the agreement, preserving documents and existing work
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
Legacy serial mode still interrupts on ordinary human messages. Use /resume after a manual pause.
"""
REASONS = {
    "waiting": "Waiting for an idea",
    "running": "Running",
    "restart": "History recovered; /resume to continue",
    "user": "Paused",
    "step_complete": "Single turn complete",
    "error": "Call failed; resolve and /resume",
    "all_passed": "All agents yielded; waiting for human input",
    "no_consensus": "All agents yielded without consensus; add guidance or /resume",
    "blocked": "Waiting for human input",
    "completed": "Work and acceptance checks completed",
    "waiting_messages": "Members are listening for new messages",
    "degraded": "A member is unavailable; others can continue. Use /retry",
    "document_error": "Consensus document needs attention; resolve the path and /resume",
}


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

    def render(self):
        return (
            "\n\n".join(
                f"{name} › {clean(visible_text(text))}" for name, text in self.turns.values()
            )
            or "Listening for the next contribution"
        )


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
            "Acceptance: " + "; ".join(proposal["acceptance"]),
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
    lines.extend("Acceptance command: " + repr(argv) for argv in proposal["checks"])
    lines.extend(f"Acceptance exit code: {r['exit_code']}" for r in state["checks_result"])
    return "\n".join(lines) + "\n"


async def event_stream(reader: asyncio.StreamReader, welcome: dict):
    yield welcome
    while True:
        yield await receive(reader)


async def plain_chat(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, welcome: dict
) -> None:
    """NDJSON output and line input, useful for pipes and scripted clients."""
    pipe_reader = asyncio.StreamReader()
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(pipe_reader), sys.stdin
    )

    async def output() -> None:
        async for event in event_stream(reader, welcome):
            print(json.dumps(event, ensure_ascii=False), flush=True)

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


async def terminal_chat(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, name: str, welcome: dict
) -> None:
    from prompt_toolkit.application import Application
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.document import Document
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import HSplit, Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.styles import Style
    from prompt_toolkit.widgets import Frame, TextArea

    state = {"reason": "waiting", "turns": 0, "active": None}
    output = TextArea(text=HELP + "\n", read_only=True, scrollbar=True, wrap_lines=True)
    live = TextArea(
        text="Waiting for a speaker",
        read_only=True,
        wrap_lines=True,
        height=Dimension(min=2, max=8),
    )
    pending: asyncio.Queue = asyncio.Queue()
    replies = LiveReplies()

    def append(text: str) -> None:
        body = output.text + clean(text)
        output.buffer.set_document(Document(body, len(body)), bypass_readonly=True)

    def accept(buffer) -> bool:
        pending.put_nowait(buffer.text)
        return False

    input_box = TextArea(
        height=3,
        prompt=f"{name} › ",
        multiline=False,
        accept_handler=accept,
        completer=WordCompleter(
            [
                "/pause",
                "/interrupt",
                "/resume",
                "/retry",
                "/redirect",
                "/next",
                "/status",
                "/sessions",
                "/reset-session",
                "/history",
                "/plan",
                "/tasks",
                "/help",
                "/quit",
            ]
        ),
        complete_while_typing=False,
    )
    bindings = KeyBindings()

    @bindings.add("c-c")
    def interrupt(event) -> None:
        pending.put_nowait("/interrupt")

    @bindings.add("c-d")
    def quit_chat(event) -> None:
        event.app.exit()

    @bindings.add("pageup")
    def page_up(event) -> None:
        output.buffer.cursor_up(count=10)

    @bindings.add("pagedown")
    def page_down(event) -> None:
        output.buffer.cursor_down(count=10)

    def status_line():
        active = state.get("active")
        turns = state.get("active_turns", [active] if active else [])
        speaker = ", ".join(t["speaker"] for t in turns) or "—"
        reason = REASONS.get(state.get("reason"), state.get("reason", ""))
        workflow = state.get("workflow")
        if workflow:
            reason = PHASES[workflow["phase"]] + " · " + reason
        return [
            (
                "class:status",
                f" {reason}  ·  Thinking: {speaker}  ·  Writer: {state.get('writer') or '—'}"
                f"  ·  Turns: {state.get('turns', 0)} (uncapped)  ",
            )
        ]

    application = Application(
        layout=Layout(
            HSplit(
                [
                    Window(
                        FormattedTextControl(" AGENT TEAM  /  Shared team room"),
                        height=1,
                        style="class:title",
                    ),
                    output,
                    Frame(
                        live, title="Independent live drafts · public after successful completion"
                    ),
                    Window(FormattedTextControl(status_line), height=1),
                    input_box,
                    Window(
                        FormattedTextControl(
                            " Ctrl-C Interrupt  Ctrl-D Leave  PgUp/PgDn History  /help Help"
                        ),
                        height=1,
                    ),
                ]
            ),
            focused_element=input_box,
        ),
        key_bindings=bindings,
        full_screen=True,
        mouse_support=True,
        style=Style.from_dict({"title": "bg:#164e63 #ffffff bold", "status": "#67e8f9"}),
    )

    async def render_events() -> None:
        nonlocal state
        try:
            async for event in event_stream(reader, welcome):
                kind = event.get("type")
                replies.update(event)
                if kind == "welcome":
                    state = event["state"]
                    roster = ", ".join(a["name"] for a in state["agents"])
                    append(f"Joined. Agents: {roster}. Loading complete message history.\n\n")
                    for turn in state.get("active_turns", []):
                        replies.update({"type": "turn.started", **turn})
                    if state.get("permission_mode") == "full_auto":
                        append(
                            "[Permissions: Codex full access / Claude auto. "
                            "Codex has no local sandbox; task scope and phase rules still apply.]\n"
                        )
                elif kind == "message":
                    append(f"#{event['id']} {event['speaker']}\n{event['text']}\n\n")
                    if event.get("rejection"):
                        append(f"[Action not applied: {event['rejection']}]\n")
                elif kind in {"turn.finished", "floor.released"}:
                    if event["outcome"] in {"cancelled", "failed"}:
                        append(
                            f"[{event['speaker']} cancelled/failed; partial reply not committed]\n"
                        )
                elif kind == "agent.passed":
                    append(f"[{event['speaker']} yielded the floor]\n")
                elif kind == "session.state":
                    append(describe_sessions(event))
                elif kind == "session.rebuilt":
                    append(f"[{event['speaker']}] {event['text']}\n")
                elif kind == "turn.idle":
                    append(f"[Waiting · {event['speaker']}] {event['text']}\n")
                elif kind == "state":
                    state = event
                elif kind == "presence":
                    append(f"[Online humans: {', '.join(event['names'])}]\n")
                elif kind == "workflow.changed":
                    append(f"[workflow] {event['text']}\n")
                    updated = event["workflow"]
                    previous = state.get("workflow") or {}
                    if updated["proposal"] and (
                        updated["version"] != previous.get("version")
                        or updated["phase"] != previous.get("phase")
                    ):
                        append(describe_workflow(updated))
                elif kind == "workflow":
                    append(describe_workflow(event["workflow"]))
                elif kind == "error":
                    append(f"Error · {event.get('speaker', 'system')}: {event['text']}\n")
                elif kind == "history.end":
                    append(
                        f"[History: {event['count']} messages; "
                        f"next page /history {event['next_after']}]\n"
                    )
                live.text = replies.render()
                live.buffer.cursor_position = len(live.text)
                application.invalidate()
        except (EOFError, ConnectionError):
            if application.is_running:
                application.exit(exception=ValueError("Disconnected from the session server"))

    async def send_inputs() -> None:
        while True:
            try:
                request = parse_input(await pending.get())
                if request == "quit":
                    application.exit()
                    return
                if request == "help":
                    append(HELP)
                elif isinstance(request, dict):
                    writer.write(encode(request))
                    await writer.drain()
            except ValueError as exc:
                append(str(exc) + "\n")
            except ConnectionError:
                if application.is_running:
                    application.exit(exception=ValueError("Disconnected from the session server"))
                return

    tasks: list[asyncio.Task] = []

    def started() -> None:
        tasks.extend([asyncio.create_task(render_events()), asyncio.create_task(send_inputs())])

    try:
        await application.run_async(pre_run=started)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def chat(session: Path, name: str, plain: bool = False) -> None:
    reader, writer = await connect(session, name)
    try:
        hello = await receive(reader)
        if hello.get("type") != "welcome":
            raise ValueError(hello.get("text", "Failed to join the session"))
        if plain or not sys.stdin.isatty() or not sys.stdout.isatty():
            await plain_chat(reader, writer, hello)
        else:
            await terminal_chat(reader, writer, name, hello)
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()
