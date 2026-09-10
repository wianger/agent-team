"""A presentation-only terminal client; no scheduling or protocol decisions live here."""

from __future__ import annotations

import asyncio
import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime
from math import ceil
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import (
    ConditionalContainer,
    Float,
    FloatContainer,
    HSplit,
    Layout,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import AfterInput, ConditionalProcessor
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import TextArea

from .client import (
    HELP,
    LiveReplies,
    clean,
    describe_sessions,
    describe_workflow,
    parse_input,
)
from .consensus import render_consensus
from .server import encode, receive
from .workflow import PHASES, visible_text

ACTIONS = {
    "/chat": "Return to the conversation",
    "/plan": "Inspect the agreed plan and votes",
    "/consensus": "Read the latest consensus document and document paths",
    "/revise": "Reopen discussion of the consensus; add your requested changes",
    "/milestones": "Inspect shared milestones and peer judgments",
    "/pause": "Let active turns finish, then pause",
    "/resume": "Resume a paused team",
    "/redirect": "Interrupt work and discuss a new direction; add your guidance",
    "/interrupt": "Cancel all active turns and pause",
    "/retry": "Retry unavailable members; optionally add an agent name",
    "/status": "Inspect team state and each member",
    "/activity": "Read operational events and complete error details",
    "/sessions": "Inspect private-session synchronization",
    "/next": "Run one eligible turn; optionally add an agent name",
    "/reset-session": "Forget private context while idle; preserve public history",
    "/history": "Request historical messages after an optional event ID",
    "/help": "Read keyboard shortcuts and commands",
    "/quit": "Leave this terminal",
}


class ActionCompleter(Completer):
    def __init__(self, names):
        self.names = names

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if "\n" in text or not text.startswith("/"):
            return
        if " " not in text:
            for command, description in ACTIONS.items():
                if command.startswith(text):
                    yield Completion(command, -len(text), display_meta=description)
        else:
            command, partial = text.split(" ", 1)
            if command in {"/retry", "/next", "/reset-session"}:
                for name in self.names():
                    if name.startswith(partial):
                        yield Completion(name, -len(partial), display_meta="Team member")


@dataclass
class Page:
    text: str
    styles: list[str]


class PageBuilder:
    def __init__(self):
        self.lines: list[str] = []
        self.styles: list[str] = []

    def add(self, text="", style=""):
        for line in clean(text).split("\n"):
            self.lines.append("  " + line if line else "")
            self.styles.append(style)

    def block(self, title, text, style="class:heading", *, compact=False):
        self.add(title, style)
        if not compact:
            self.add()
        code = False
        for line in clean(text).split("\n"):
            prefix = "  " if compact and line else ""
            if line.lstrip().startswith("```"):
                self.add(prefix + line, "class:muted")
                code = not code
            else:
                self.add(
                    prefix + line,
                    "class:code" if code else "class:heading" if line.startswith("#") else "",
                )
        self.add()
        if not compact:
            self.add()

    def finish(self):
        return Page("\n".join(self.lines), self.styles)


class RoomView:
    """Complete public messages and separate ephemeral drafts, with quiet event routing."""

    def __init__(self, name):
        self.name = name
        self.state: dict = {}
        self.messages: dict[int, dict] = {}
        self.replies = LiveReplies()
        self.turns: dict[str, dict] = {}
        self.started: dict[str, float] = {}
        self.last_activity: dict[str, float] = {}
        self.activity: list[tuple[str, str]] = []
        self.idle: set[str] = set()
        self.online: list[str] = []
        self.revision = 0
        self.new_messages = 0
        self.notice = ""
        self.quota_notice: tuple[str, str] | None = None

    def record(self, title, text):
        self.activity.append((clean(title), clean(text)))
        self.revision += 1

    def handle(self, event):
        kind = event.get("type")
        identifier = event.get("turn_id")
        self.replies.update(event)
        if kind == "welcome":
            self.state = event["state"]
            active = self.state.get("active_turns")
            if active is None:
                active = [self.state["active"]] if self.state.get("active") else []
            for turn in active:
                self.handle({"type": "turn.started", **turn, "joined_mid_turn": True})
            self.record("Connected", "Public history is loading. " + self.permissions())
        elif kind == "state":
            self.state = event
            if self.quota_notice and "quotas" in event:
                name, _ = self.quota_notice
                if name not in event["quotas"] and not event.get("runtimes", {}).get(name, {}).get(
                    "error"
                ):
                    self.clear_quota_notice(name)
        elif kind == "message":
            if event["id"] not in self.messages:
                self.messages[event["id"]] = event
                if not event.get("replay"):
                    self.new_messages += 1
            if event.get("rejection"):
                self.record("Decision not applied", event["rejection"])
        elif kind in {"turn.started", "floor.granted"}:
            self.turns[identifier] = event
            self.started[identifier] = time.monotonic()
        elif kind in {"delta", "turn.activity"}:
            if identifier in self.turns:
                self.last_activity[identifier] = time.monotonic()
                self.idle.discard(identifier)
        elif kind in {"turn.finished", "floor.released"}:
            self.turns.pop(identifier, None)
            self.started.pop(identifier, None)
            self.last_activity.pop(identifier, None)
            self.idle.discard(identifier)
            outcome = event.get("outcome", "finished")
            self.record(
                event["speaker"],
                f"Turn {outcome}."
                + (
                    " Partial output was not published; file changes may remain."
                    if outcome in {"cancelled", "failed"}
                    else ""
                ),
            )
        elif kind == "presence":
            self.online = event["names"]
            self.record("Presence", "Online: " + (", ".join(self.online) or "no humans"))
        elif kind == "workflow.changed":
            self.state = {**self.state, "workflow": event["workflow"]}
            self.record("Workflow", event["text"])
        elif kind == "workflow":
            self.state = {**self.state, "workflow": event["workflow"]}
        elif kind == "session.state":
            self.state = {**self.state, **event}
        elif kind == "error":
            self.notice = f"{event.get('speaker', 'Team')}: {event['text']}"
            self.quota_notice = (event["speaker"], self.notice) if event.get("quota") else None
            self.record("Error", self.notice)
        elif kind == "agent.quota":
            name = event["speaker"]
            quotas = dict(self.state.get("quotas", {}))
            if event["quota"] is None:
                quotas.pop(name, None)
                self.clear_quota_notice(name)
            else:
                quotas[name] = event["quota"]
            self.state = {**self.state, "quotas": quotas}
        elif kind == "turn.idle":
            self.idle.add(identifier)
            self.record(event["speaker"], event["text"])
        elif kind == "session.rebuilt":
            self.record(event["speaker"], event["text"])
        elif kind == "agent.passed":
            self.record(event["speaker"], "Nothing to add; listening for new messages.")
        elif kind == "history.end":
            self.record(
                "History",
                f"Received {event['count']} messages. Next: /history {event['next_after']}",
            )
        elif kind == "consensus.saved":
            self.record("Consensus document", f"v{event['version']}: {event['document']}")
        self.revision += 1

    def clear_quota_notice(self, name):
        if self.quota_notice and self.quota_notice[0] == name:
            if self.notice == self.quota_notice[1]:
                self.notice = ""
            self.quota_notice = None

    def turn_progress(self, identifier):
        now = time.monotonic()
        seconds = max(0, int(now - self.started[identifier]))
        minutes, seconds = divmod(seconds, 60)
        label = "Observed for" if self.turns[identifier].get("joined_mid_turn") else "Running for"
        progress = f"{label} {minutes}m {seconds:02}s"
        if identifier in self.last_activity:
            age = max(0, int(now - self.last_activity[identifier]))
            progress += f" · Backend activity {age}s ago"
        else:
            progress += " · No backend activity observed yet"
        if identifier in self.idle:
            progress += " · Still waiting; silence does not prove a stall"
        return progress

    def permissions(self):
        if self.state.get("permission_mode") == "full_auto":
            return (
                "Full auto in every phase: Codex full access; Claude auto with all built-in tools."
            )
        return "Phase-scoped permissions. Agreed acceptance commands run in the workspace."

    def phase(self):
        workflow = self.state.get("workflow") or {}
        reason = self.state.get("reason", "waiting")
        label = PHASES.get(workflow.get("phase"), "Discussion")
        if reason == "completed":
            return "Completed · agreed acceptance checks passed"
        if reason == "document_error":
            return "Paused · consensus document needs attention"
        if reason == "error":
            return (
                "Pausing · stopping active turns after an error"
                if self.turns
                else "Paused · a team call failed"
            )
        if reason == "quota":
            if any(t.get("phase") == "recovery" for t in self.turns.values()):
                return "Paused · checking quota recovery"
            return "Pausing · stopping active turns" if self.turns else "Paused · usage limit"
        if self.state.get("paused") and reason != "waiting":
            return "Pausing · active turns are finishing" if self.turns else "Paused · " + label
        if not self.messages and not self.state.get("messages"):
            return "Ready for your idea"
        if workflow.get("proposal"):
            total = len(self.state.get("agents", []))
            if workflow["phase"] == "discussion":
                return (
                    f"{label} · proposal v{workflow['version']} · "
                    f"{len(workflow.get('approvals', []))}/{total} approved"
                )
            milestones = workflow["proposal"]["milestones"]
            done = sum(t["status"] == "done" for t in milestones)
            return f"{label} · {done}/{len(milestones)} milestones accepted"
        return label

    def guidance(self):
        reason = self.state.get("reason", "waiting")
        if reason == "completed":
            return "Review the result in Plan. Send a new idea to start another discussion."
        if reason == "blocked":
            return "The team needs your input. Read its latest message and provide guidance."
        if reason == "document_error":
            return "Resolve the consensus document path, then /resume. See Activity for details."
        if reason == "error":
            if self.turns:
                return "Stopping active turns after an error. Wait for cleanup before retrying."
            retry = (
                "/retry [agent] or /resume"
                if self.state.get("interaction_mode") == "chatroom"
                else "/resume"
            )
            return f"Team paused. Check Activity, resolve the error, then {retry}."
        if reason == "quota":
            return (
                "Team paused by a usage limit. Known reset times trigger recovery checks; "
                "unknown times require /retry or /resume. /status shows timing. "
                "Discussion resumes only after all limited members recover."
            )
        if self.state.get("paused") and reason != "waiting":
            if self.turns:
                return "Waiting for active turns to finish. /interrupt cancels them immediately."
            return "Messages do not resume a paused team. Use /resume when you are ready."
        if not self.messages and not self.state.get("messages"):
            return "Send your idea to start discussion automatically. Work follows consensus."
        if self.state.get("interaction_mode") == "serial":
            return "Serial mode: a message interrupts work. /pause lets the current turn finish."
        return "Messages add context. /redirect <guidance> stops work to change direction."

    def member_statuses(self):
        statuses = []
        for agent in self.state.get("agents", []):
            name = agent["name"]
            runtime = self.state.get("runtimes", {}).get(name, {})
            quota = self.state.get("quotas", {}).get(name)
            turn = next((t for t in self.turns.values() if t["speaker"] == name), None)
            if quota:
                status = (
                    "Checking quota"
                    if quota.get("retrying")
                    else "Quota · reset unknown"
                    if quota["retry_at"] is None
                    else "Quota · waiting for reset"
                )
            elif runtime.get("error"):
                status = "Unavailable"
            elif turn:
                if turn.get("turn_id") in self.idle:
                    status = "Waiting for output"
                elif turn.get("lane") == "chat":
                    status = "Replying in chat"
                elif turn.get("phase") == "implementation":
                    status = "Writing"
                elif turn.get("phase") in {"judging", "review"}:
                    status = "Reviewing"
                else:
                    status = "Working"
            else:
                status = (
                    "Ready"
                    if self.state.get("reason") == "waiting"
                    else "Paused"
                    if self.state.get("paused")
                    else "Listening"
                )
            statuses.append((name, status))
        if self.state.get("writer") == "system":
            statuses.append(("Checks", "Running"))
        return statuses

    def quota_details(self, quota):
        lines = [quota["error"]]
        if quota.get("limit_type"):
            lines.append("Limit window: " + quota["limit_type"])
        when = quota["retry_at"]
        if when is None:
            lines.append("Timing source: unknown; no reset time is assumed.")
            lines.append("No automatic retry. Use /resume or /retry after resolving the limit.")
        else:
            reset = quota.get("resets_at")
            if quota.get("retry_source") == "provider" and reset is not None:
                lines.append("Timing source: provider reset time + 30s safety buffer.")
                lines.append(
                    "Provider reset: " + datetime.fromtimestamp(reset).astimezone().isoformat()
                )
            lines.append(
                "Retry due: "
                + datetime.fromtimestamp(when).astimezone().isoformat()
                + " (deferred while manually paused)."
            )
            remaining = max(0, ceil(when - time.time()))
            hours, seconds = divmod(remaining, 3600)
            minutes, seconds = divmod(seconds, 60)
            lines.append(
                f"Remaining wait: {hours}h {minutes:02}m {seconds:02}s"
                if remaining
                else "Retry is due; waiting for cleanup or explicit resume if manually paused."
            )
        if quota.get("retrying"):
            lines.append("Recovery check pending or in progress; discussion remains paused.")
        return "\n".join(lines)

    def page(self, view, *, room_label="Shared workspace"):
        page = PageBuilder()
        page.add()
        if view == "conversation":
            page.add("✳ agent-team", "class:accent")
            page.add(room_label, "class:muted")
            members = " · ".join(a["name"] for a in self.state.get("agents", []))
            if members:
                page.add(members, "class:muted")
            page.add()
            if not self.messages and not self.replies.turns:
                if self.state.get("messages"):
                    page.block("Loading your conversation", "Replaying all committed messages.")
                else:
                    page.add("What would you like to build?", "class:heading")
                    page.add("Share an idea. Discuss, agree, build, and review together.")
                    page.add()
                    page.add(
                        "Enter to send · Alt+Enter for a new line · ? for shortcuts",
                        "class:muted",
                    )
            for identifier in sorted(self.messages):
                message = self.messages[identifier]
                name = message["speaker"]
                style = self.speaker_style(name)
                label = f"❯ You · {name}" if name == self.name else f"● {name}"
                if message.get("role") == "system":
                    style = "class:muted"
                    label = "· Team"
                page.block(label, message["text"], style, compact=True)
                if message.get("rejection"):
                    page.add("Decision not applied: " + message["rejection"], "class:warning")
                    page.add()
            for identifier, (name, text) in self.replies.turns.items():
                turn = self.turns.get(identifier, {})
                phase = turn.get("phase", "discussion")
                if phase == "recovery":
                    continue  # Availability probes do not publish conversation drafts.
                label = (
                    "chatting"
                    if turn.get("lane") == "chat"
                    else "writing"
                    if phase == "implementation"
                    else "reviewing"
                    if phase in {"judging", "review"}
                    else "thinking"
                )
                body = visible_text(text)
                if turn.get("joined_mid_turn"):
                    body = "Joined mid-turn; earlier draft is not replayed.\n\n" + (
                        body or "Waiting for new public output…"
                    )
                page.block(
                    f"✻ {name} · {label} · live · not published",
                    body or "Waiting for public output…",
                    self.speaker_style(name),
                    compact=True,
                )
            # Keep ticking counters after all drafts so long, unchanged replies
            # retain their cached display rows instead of rewrapping every second.
            for identifier, turn in self.turns.items():
                if turn.get("phase") != "recovery":
                    page.add(f"{turn['speaker']} · {self.turn_progress(identifier)}", "class:muted")
        elif view == "plan":
            page.block("Plan & shared work", describe_workflow(self.state.get("workflow")))
        elif view == "consensus":
            workflow = self.state.get("workflow") or {}
            history = workflow.get("consensus_history", [])
            if not history:
                page.block(
                    "Consensus",
                    "No approved document yet. A document is generated once consensus is reached. "
                    "Older servers need an upgrade to export existing consensus documents.",
                )
            else:
                latest = history[-1]
                status = "Under revision" if workflow["phase"] == "discussion" else "Approved"
                page.block(f"Consensus · {status}", "Document: " + latest["document"])
                if latest["version"] not in self.state.get("consensus_documents", {}).get(
                    "ready_versions", []
                ):
                    page.add(
                        "Document publication is pending or failed; "
                        "the approved record is shown below.",
                        "class:warning",
                    )
                    page.add()
                page.block("Approved record", render_consensus(latest))
                page.block(
                    "Version history",
                    "\n".join(f"v{r['version']} · {r['document']}" for r in history),
                )
        elif view == "activity":
            page.block(
                "Activity",
                "Operational events stay here, separate from the conversation.\n"
                "This view records events received by this terminal; public history is durable.",
            )
            for title, text in self.activity:
                page.block(title, text, "class:warning" if title == "Error" else "class:muted")
        elif view == "sessions":
            page.block("Private context", describe_sessions(self.state))
        elif view == "status":
            page.block("Team status", self.phase() + "\n\n" + self.guidance())
            for name, status in self.member_statuses():
                page.block(name, status, self.speaker_style(name))
                for identifier, turn in self.turns.items():
                    if turn["speaker"] == name or name == "Checks" and turn["speaker"] == "system":
                        page.add(self.turn_progress(identifier), "class:muted")
                error = self.state.get("runtimes", {}).get(name, {}).get("error")
                if error:
                    page.block("Needs attention", error, "class:warning")
                quota = self.state.get("quotas", {}).get(name)
                if quota:
                    page.block("Usage limit", self.quota_details(quota), "class:warning")
            page.block("Execution permissions", self.permissions())
            page.add(f"Completed turns: {self.state.get('turns', 0)} · no round limit")
        elif view == "help":
            page.block(
                "Make yourself at home",
                "Enter                 Send your complete message\n"
                "Alt+Enter / Ctrl-J     Insert a new line\n"
                "Tab / Shift-Tab       Browse action suggestions\n"
                "? on empty input      Open keyboard shortcuts\n"
                "Ctrl-O                Toggle Activity\n"
                "Ctrl-T                Toggle Plan\n"
                "Mouse wheel           Scroll three display rows; bottom resumes live output\n"
                "PgUp / PgDn           Scroll one screen at a time\n"
                "Ctrl-End              Return to the latest conversation\n"
                "F2 / F3 / F4 / F1     Conversation / Plan / Activity / Help\n"
                "Escape                Dismiss suggestions or return to the conversation\n"
                "Ctrl-C                Exit when idle; interrupt active work, "
                "then press again to exit\n"
                "Ctrl-D                Leave this terminal\n\n"
                "An unsent draft needs a second Ctrl-C to confirm leaving.\n"
                "Escape or editing cancels confirmation without discarding the draft.\n"
                "Use /interrupt to pause without leaving. Use Ctrl-D or /quit to leave a join "
                "client without interrupting the team.\n\n"
                "Multiline bracketed paste stays in the editor until you press Enter.\n"
                "Scrolling keeps your input focus and draft. While reading history, new output "
                "is retained without moving your view. Scroll to the bottom to follow again.",
            )
            page.block(
                "Actions", HELP + "\n/chat          Conversation\n/activity      Event details"
            )
            page.block("Execution permissions", self.permissions())
        return page.finish()

    def speaker_style(self, name):
        if name == self.name:
            return "class:you"
        names = [a["name"] for a in self.state.get("agents", [])]
        return f"class:member{names.index(name) % 4}" if name in names else "class:muted"


class PageLexer(Lexer):
    def __init__(self):
        self.styles: list[str] = []

    def lex_document(self, document):
        styles = self.styles[:]

        def line(index):
            style = styles[index] if index < len(styles) else ""
            return [(style, document.lines[index])]

        return line


class TranscriptWindow(Window):
    """Scroll display rows independently of the read-only buffer's cursor."""

    def __init__(self, content, following):
        self.following = following
        self.top = 0
        self.pending_scroll = 0
        self.resume_at_bottom = False
        self.at_bottom = True
        self.rows = [(0, 0)]
        self.lines: list[str] = []
        self.wrap_width = 80
        self.cache_key = None
        super().__init__(
            content,
            height=Dimension(min=1),
            wrap_lines=True,
            style="class:text-area",
            get_line_prefix=self.line_prefix,
        )

    def line_prefix(self, line, wrap):
        return [("", " " * min(4, max(0, self.wrap_width - 1)))] if wrap else []

    def _scroll_when_linewrapping(self, ui_content, width, height):
        if width <= 0 or height <= 0:
            return
        key = (self.content.buffer.text, width)
        if key != self.cache_key:
            anchor = self.rows[min(self.top, len(self.rows) - 1)]
            lines = self.content.buffer.document.lines
            unchanged = 0
            if self.cache_key is not None and width == self.wrap_width:
                for old, new in zip(self.lines, lines, strict=False):
                    if old != new:
                        break
                    unchanged += 1
            # Streaming typically changes only the tail. Reuse the preceding display
            # rows so each token does not reprocess the entire conversation.
            del self.rows[bisect_left(self.rows, (unchanged, 0)) :]
            self.wrap_width = width
            # Match the renderer's character widths, including wide characters,
            # combining marks, expanded tabs, and continuation indentation.
            for row in range(unchanged, ui_content.line_count):
                self.rows.append((row, 0))
                used = 0
                for col, char in enumerate(fragment_list_to_text(ui_content.get_line(row))):
                    size = get_cwidth(char)
                    if used + size > width:
                        self.rows.append((row, col))
                        used = min(4, width - 1)
                    used += size
            self.top = max(0, bisect_right(self.rows, anchor) - 1)
            self.lines = lines
            self.cache_key = key
        maximum = max(0, len(self.rows) - height)
        if self.following():
            cursor = ui_content.cursor_position
            position = bisect_right(self.rows, (cursor.y, cursor.x)) - 1
            self.top = max(position - height + 1, min(self.top, position))
        self.top = max(0, min(maximum, self.top + self.pending_scroll))
        if self.resume_at_bottom:
            self.top = maximum  # A reading/status row may have changed the viewport height.
        self.pending_scroll = 0
        self.at_bottom = self.top == maximum
        self.vertical_scroll = self.rows[self.top][0]
        self.vertical_scroll_2 = self.top - bisect_left(self.rows, (self.vertical_scroll, 0))
        self.horizontal_scroll = 0


class TeamUI:
    """Testable terminal layout, input routing, and stable scrollback snapshots."""

    def __init__(self, name, *, room_path: Path | None = None, stop_on_exit=False, **app_options):
        self.model = RoomView(name)
        self.room_path = room_path
        self.stop_on_exit = stop_on_exit
        self.connected = True
        self.confirm_leave: str | None = None
        self.exiting = False
        self.view = "conversation"
        self.follow = True
        self.read_at = 0
        self.painted = None
        self.pending: asyncio.Queue[str | None] = asyncio.Queue()
        self.lexer = PageLexer()
        self.body = TextArea(
            read_only=True,
            scrollbar=False,
            wrap_lines=True,
            lexer=self.lexer,
        )
        self.body.window = TranscriptWindow(self.body.control, lambda: self.follow)
        self.input = TextArea(
            multiline=True,
            read_only=Condition(lambda: self.exiting),
            height=lambda: Dimension(
                min=1, max=min(6, max(1, self.application.output.get_size().rows // 4))
            ),
            dont_extend_height=True,
            prompt=[("class:accent", "  ❯ ")],
            wrap_lines=True,
            get_line_prefix=lambda line, wrap: [("", "    ")] if line or wrap else [],
            history=InMemoryHistory(),
            completer=ActionCompleter(
                lambda: [a["name"] for a in self.model.state.get("agents", [])]
            ),
            complete_while_typing=Condition(lambda: self.input.text.startswith("/")),
            input_processors=[
                ConditionalProcessor(
                    AfterInput(lambda: [("class:muted", self.placeholder())]),
                    filter=Condition(lambda: not self.input.text),
                )
            ],
        )
        self.input.buffer.on_text_changed += lambda _: setattr(self, "confirm_leave", None)
        bindings = KeyBindings()

        @bindings.add("enter", eager=True)
        def send(event):
            if event.app.layout.has_focus(self.input):
                self.submit()

        @bindings.add("escape", "enter")
        @bindings.add("c-j")
        def newline(event):
            if not self.exiting:
                event.app.layout.focus(self.input)
                self.input.buffer.insert_text("\n")

        @bindings.add("c-c")
        def interrupt(event):
            self.leave("Ctrl-C")

        @bindings.add("c-d")
        def leave(event):
            self.leave()

        @bindings.add("escape")
        def escape(event):
            self.confirm_leave = None
            self.model.notice = ""
            if self.input.buffer.complete_state:
                self.input.buffer.cancel_completion()
            elif self.view != "conversation":
                self.show("conversation")
            event.app.layout.focus(self.input)

        for key, view in (("?", "help"), ("c-o", "activity"), ("c-t", "plan")):
            bindings.add(
                key,
                filter=Condition(
                    lambda: not self.input.text and self.application.layout.has_focus(self.input)
                )
                if key == "?"
                else True,
            )(lambda event, view=view: self.show("conversation" if self.view == view else view))

        @bindings.add("pageup")
        @bindings.add("pagedown")
        def scroll(event):
            self.scroll(event.key_sequence[-1].key == "pageup")

        @bindings.add(Keys.ScrollUp)
        @bindings.add(Keys.ScrollDown)
        def wheel(event):
            self.scroll(event.key_sequence[-1].key == Keys.ScrollUp, lines=3)

        @bindings.add("c-end")
        def latest(event):
            self.show("conversation")

        for key, view in (
            ("f1", "help"),
            ("f2", "conversation"),
            ("f3", "plan"),
            ("f4", "activity"),
        ):
            bindings.add(key)(lambda event, view=view: self.show(view))

        container = HSplit(
            [
                ConditionalContainer(
                    Window(FormattedTextControl(self.title), height=1),
                    filter=Condition(lambda: self.view != "conversation"),
                ),
                self.body,
                ConditionalContainer(
                    Window(FormattedTextControl(self.reading_hint), height=1, style="class:muted"),
                    filter=Condition(lambda: not self.follow),
                ),
                Window(
                    FormattedTextControl(self.roster),
                    height=lambda: Dimension(
                        min=1, max=2 if self.application.output.get_size().rows >= 18 else 1
                    ),
                    wrap_lines=True,
                    dont_extend_height=True,
                ),
                ConditionalContainer(
                    Window(FormattedTextControl(self.guidance), height=1, style="class:muted"),
                    filter=Condition(self.guidance_visible),
                ),
                Window(height=1, char="─", style="class:border"),
                self.input,
                Window(height=1, char="─", style="class:border"),
                Window(FormattedTextControl(self.footer), height=1, style="class:footer"),
            ]
        )
        self.application = Application(
            layout=Layout(
                FloatContainer(
                    container,
                    floats=[
                        Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=8))
                    ],
                ),
                focused_element=self.input,
            ),
            key_bindings=bindings,
            full_screen=True,
            mouse_support=True,
            min_redraw_interval=0.05,
            refresh_interval=1.0,
            before_render=lambda app: self.paint(),
            after_render=self.after_render,
            style=Style.from_dict(
                {
                    "": "",
                    "accent": "#d08770 bold",
                    "heading": "bold",
                    "muted": "#888888",
                    "border": "#666666",
                    "warning": "#d7a35e",
                    "you": "bold",
                    "member0": "#d08770 bold",
                    "member1": "#74a4bf bold",
                    "member2": "#ab92bf bold",
                    "member3": "#8baa7f bold",
                    "code": "#8baa7f",
                    "footer": "#888888",
                    "completion-menu.completion": "bg:#262626 #d7d7d7",
                    "completion-menu.completion.current": "bg:#45403d #ffffff bold",
                    "completion-menu.meta.completion": "bg:#262626 #aaaaaa",
                    "completion-menu.meta.completion.current": "bg:#45403d #e0b49f",
                }
            ),
            **app_options,
        )
        for window in self.application.layout.find_all_windows():
            control = window.content
            if control in (self.body.control, self.input.control) or isinstance(
                control, FormattedTextControl
            ):
                original_mouse = control.mouse_handler

                def mouse(event, original=original_mouse, control=control):
                    if event.event_type in {MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN}:
                        self.scroll(event.event_type == MouseEventType.SCROLL_UP, lines=3)
                        return None
                    if control is self.body.control:
                        if event.event_type == MouseEventType.MOUSE_DOWN:
                            self.freeze()
                        # Native selection needs focus while handling the mouse, but
                        # must not leave typing/Enter trapped in the read-only buffer.
                        layout = self.application.layout
                        layout.focus(self.body.window)
                        try:
                            return original(event)
                        finally:
                            layout.focus_last()
                    return original(event)

                control.mouse_handler = mouse

    def room_label(self):
        label = "Shared conversation"
        if self.room_path:
            label = self.room_path.name
            if self.room_path.parent.name == ".agent-team":
                label = f"{self.room_path.parent.parent.name} / {self.room_path.name}"
        return clean(label)

    def title(self):
        return [
            ("class:accent", f"  /{self.view}"),
            ("class:muted", "  ·  Esc back to chat"),
        ]

    def roster(self):
        state = self.model.state
        reason = state.get("reason", "waiting")
        phase = PHASES.get((state.get("workflow") or {}).get("phase"), "Discussion")
        if not self.connected:
            return [("class:warning", "  ○ Disconnected · last known state in /status")]
        if reason == "waiting" and not self.model.messages:
            phase = "Ready"
        elif reason == "completed":
            phase = "Completed"
        elif state.get("paused"):
            phase = "Pausing" if self.model.turns else "Paused"
        marker = "✻" if self.model.turns else "○"
        fragments = [("class:accent", f"  {marker} {phase}")]
        for name, status in self.model.member_statuses():
            fragments.extend(
                [
                    ("class:muted", "  ·  "),
                    (self.model.speaker_style(name), clean(name)),
                    (
                        "class:warning"
                        if status == "Unavailable" or "quota" in status.lower()
                        else "class:muted",
                        " " + status.lower(),
                    ),
                ]
            )
        return fragments

    def placeholder(self):
        if not self.model.messages and not self.model.state.get("messages"):
            return "Describe your idea…"
        return "Add a message, or type / for actions…"

    def guidance_visible(self):
        state = self.model.state
        reason = state.get("reason", "waiting")
        return bool(
            self.exiting
            or self.confirm_leave
            or not self.connected
            or self.model.notice
            or state.get("paused")
            and reason != "waiting"
            or reason in {"completed", "blocked", "error", "document_error"}
            or state.get("interaction_mode") == "serial"
            and state.get("messages")
        )

    def guidance(self):
        compact = self.application.output.get_size().columns < 70
        if self.exiting:
            return [("class:warning", "  Leaving… Ctrl-C again to close immediately.")]
        if self.confirm_leave:
            key = self.confirm_leave
            if compact:
                question = (
                    "Stop this team?"
                    if self.stop_on_exit
                    else "Discard draft?"
                    if self.input.text.strip()
                    else "Leave chat?"
                )
                return [("class:warning", f"  {question} {key} confirms; Esc cancels.")]
            return [
                (
                    "class:warning",
                    "  "
                    + (
                        f"Leaving stops this team. {key} again to confirm; Esc to stay."
                        if self.stop_on_exit
                        else f"Unsent draft. {key} again to discard and leave; Esc to keep editing."
                        if self.input.text.strip()
                        else f"Interrupt requested. {key} again to leave; Esc to stay."
                    ),
                )
            ]
        if not self.connected:
            if compact:
                return [("class:warning", "  Disconnected · leave and rejoin to reconnect.")]
            return [
                (
                    "class:warning",
                    "  Disconnected. Drafts stay here; leave and rejoin to reconnect.",
                )
            ]
        if self.model.notice:
            return [
                (
                    "class:warning",
                    "  /activity for details · " + clean(self.model.notice).replace("\n", " "),
                )
            ]
        if compact:
            reason = self.model.state.get("reason", "waiting")
            if reason == "waiting":
                return "  Enter sends your idea and starts discussion."
            if reason == "completed":
                return "  Completed · F3 Plan for results."
            if reason in {"blocked", "error"}:
                return "  Needs attention · read messages and /activity."
            if self.model.state.get("paused"):
                return "  Paused · /resume to continue."
            return "  Add context, or /redirect <guidance>."
        return "  " + clean(self.model.guidance())

    def reading_hint(self):
        if not self.follow:
            new = self.model.new_messages - self.read_at
            if self.application.output.get_size().columns < 70:
                return f"  Reading · {new} new · Ctrl-End latest"
            return f"  Reading {self.view} · {new} new messages · Ctrl-End for latest conversation"
        return ""

    def footer(self):
        confirm = self.active_work() or self.input.text.strip()
        leave = "Ctrl-C ×2 Exit" if confirm and self.confirm_leave != "Ctrl-C" else "Ctrl-C Exit"
        auto = self.model.state.get("permission_mode") == "full_auto"
        mode = "full auto" if auto else "phase-scoped"
        if self.application.output.get_size().columns < 70:
            mode = "auto" if auto else "scoped"
            return f"  {mode} · / commands · ? help · {leave}"
        return f"  {mode} · / commands · ? shortcuts · {leave}"

    def scroll(self, up, *, lines=None):
        window = self.body.window
        info = window.render_info
        if not info:
            return
        maximum = max(0, len(window.rows) - info.window_height)
        current = window.top + window.pending_scroll
        if up and current == 0 or not up and current == maximum and self.follow:
            return
        self.freeze()
        distance = lines if lines is not None else max(1, info.window_height - 1)
        target = max(0, min(maximum, current + (-distance if up else distance)))
        window.pending_scroll = target - window.top
        window.resume_at_bottom = not up and target == maximum
        self.application.invalidate()

    def after_render(self, app):
        window = self.body.window
        if (
            self.view == "conversation"
            and not self.follow
            and window.resume_at_bottom
            and window.at_bottom
        ):
            self.follow = True
            window.resume_at_bottom = False
            self.painted = None
            self.paint()
            app.invalidate()

    def freeze(self):
        if self.follow:
            self.follow = False
            self.read_at = self.model.new_messages

    def show(self, view):
        self.confirm_leave = None
        self.view = view
        self.follow = True
        self.body.window.pending_scroll = 0
        self.body.window.resume_at_bottom = False
        self.painted = None
        self.model.notice = ""
        self.application.layout.focus(self.input)
        self.paint()
        self.application.invalidate()

    def paint(self):
        key = (self.view, self.model.revision)
        if (
            self.view == "status"
            and self.model.state.get("quotas")
            or self.view in {"conversation", "status"}
            and self.model.turns
        ):
            key += (int(time.time()),)
        if key == self.painted or (not self.follow and self.painted is not None):
            return
        page = self.model.page(self.view, room_label=self.room_label())
        if self.room_path and self.view in {"help", "status"}:
            page.text += "\n  Room: " + clean(str(self.room_path)) + "\n"
        position = (
            len(page.text)
            if self.view == "conversation" and (self.model.messages or self.model.replies.turns)
            else 0
        )
        if self.painted and self.view != "conversation":
            position = min(self.body.buffer.cursor_position, len(page.text))
        self.lexer.styles = page.styles
        self.body.buffer.set_document(Document(page.text, position), bypass_readonly=True)
        self.painted = key

    def submit(self):
        if self.exiting:
            return
        buffer = self.input.buffer
        if buffer.complete_state and buffer.complete_state.current_completion:
            # Accept the suggestion, not the action. A second Enter explicitly sends it.
            buffer.complete_state = None
            return
        text = buffer.text.strip()
        if not text:
            return
        try:
            if text not in {"/chat", "/activity"}:
                parse_input(text)
        except ValueError as exc:
            self.model.notice = str(exc)
            self.model.record("Input", str(exc))
            return  # Preserve the draft so it can be corrected.
        local_views = {
            "/chat": "conversation",
            "/activity": "activity",
            "/help": "help",
            "/plan": "plan",
            "/consensus": "consensus",
            "/milestones": "plan",
            "/status": "status",
            "/sessions": "sessions",
        }
        if not self.connected and text not in {*local_views, "/quit"}:
            return  # Never clear a draft when there is no transport to send it.
        buffer.append_to_history()
        buffer.reset()
        self.model.notice = ""
        if text in local_views:
            self.show(local_views[text])
            if self.connected and text in {
                "/plan",
                "/milestones",
                "/consensus",
                "/status",
                "/sessions",
            }:
                self.pending.put_nowait(text)
        elif text == "/quit":
            self.leave()
        else:
            if not text.startswith("/") or text.startswith(("/redirect ", "/revise ")):
                self.show("conversation")
            self.pending.put_nowait(text)

    def active_work(self):
        state = self.model.state
        return self.connected and bool(
            self.model.turns or state.get("active_turns") or state.get("active")
        )

    def leave(self, key="Ctrl-D"):
        active = key == "Ctrl-C" and self.active_work()
        if self.exiting or self.confirm_leave == key:
            self.request_exit()
        elif active or self.input.text.strip() or (key == "Ctrl-D" and self.stop_on_exit):
            if active:
                self.pending.put_nowait("/interrupt")
            self.confirm_leave = key
            self.application.invalidate()
        else:
            self.request_exit()

    def exit_now(self):
        if not self.application.is_done:
            self.application.exit()

    def request_exit(self):
        if self.exiting or not self.connected:
            self.exit_now()
        else:
            self.exiting = True
            # Keep a fast double Ctrl-C from dropping its queued interrupt. Exit is
            # ordered after preceding requests have drained to the transport, not
            # after a server acknowledgement. Another Ctrl-C can force local exit.
            self.pending.put_nowait(None)
            self.application.invalidate()

    async def run(self, reader, writer, welcome):
        self.model.handle(welcome)

        async def receive_events():
            while True:
                self.model.handle(await receive(reader))
                self.application.invalidate()

        async def send_inputs():
            while True:
                text = await self.pending.get()
                if text is None:
                    self.exit_now()
                    return
                try:
                    writer.write(encode(parse_input(text)))
                    await writer.drain()
                except (OSError, EOFError, asyncio.CancelledError):
                    self.model.record("Delivery not confirmed", text)
                    if not self.input.text:
                        self.input.text = text
                    raise

        async def supervise():
            tasks = [asyncio.create_task(receive_events()), asyncio.create_task(send_inputs())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            except (EOFError, OSError) as exc:
                self.connected = False
                self.model.record("Disconnected", str(exc) or "The server closed the connection.")
                if self.exiting:
                    self.exit_now()
                self.application.invalidate()
            except Exception as exc:
                if not self.application.is_done:
                    self.application.exit(exception=exc)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                while not self.pending.empty():
                    text = self.pending.get_nowait()
                    if text is None:
                        continue
                    self.model.record("Not sent", text)
                    if not self.input.text:
                        self.input.text = text

        supervisor = None

        def started():
            nonlocal supervisor
            supervisor = asyncio.create_task(supervise())

        try:
            await self.application.run_async(pre_run=started)
        finally:
            if supervisor:
                supervisor.cancel()
                await asyncio.gather(supervisor, return_exceptions=True)
