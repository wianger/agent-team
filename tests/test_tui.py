from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.data_structures import Size
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent_team.config import demo_config
from agent_team.server import Server, connect, encode, receive
from agent_team.tui import ActionCompleter, RoomView, TeamUI
from agent_team.workflow import Workflow


def welcome(**updates):
    config = demo_config()
    return {
        "type": "welcome",
        "state": {
            "agents": [{"name": a.name, "backend": a.backend} for a in config.agents],
            "reason": "waiting",
            "paused": True,
            "active": None,
            "active_turns": [],
            "writer": None,
            "messages": 0,
            "turns": 0,
            "workflow": Workflow(config).snapshot(),
            "interaction_mode": "chatroom",
            "permission_mode": "full_auto",
            **updates,
        },
    }


def message(identifier, text, **updates):
    return {
        "type": "message",
        "id": identifier,
        "speaker": "member_a",
        "role": "agent",
        "text": text,
        **updates,
    }


def turn(identifier="a", speaker="member_a", **updates):
    return {
        "type": "turn.started",
        "turn_id": identifier,
        "speaker": speaker,
        "phase": "discussion",
        "lane": "work",
        **updates,
    }


class PresentationTests(unittest.TestCase):
    def setUp(self):
        self.view = RoomView("user")
        self.view.handle(welcome())

    def test_empty_room_guides_the_first_idea_without_a_command_dump(self):
        text = self.view.page("conversation").text
        self.assertIn("What would you like to build?", text)
        self.assertIn("Alt+Enter", text)
        self.assertNotIn("/reset-session", text)
        self.assertIn("automatically", self.view.guidance())
        self.assertIn("Codex writers", self.view.permissions())
        self.assertIn("native nonwriters are guarded", self.view.page("status").text)

    def test_recovered_manual_pause_blocker_and_completed_guidance(self):
        for reason in ("restart", "user", "step_complete"):
            self.view.handle(welcome(reason=reason, messages=2))
            self.assertIn("/resume", self.view.guidance())
            self.assertIn("Paused", self.view.phase())
        self.view.handle(welcome(reason="blocked", messages=2))
        self.assertIn("needs your input", self.view.guidance())
        self.view.handle(welcome(reason="completed", messages=2))
        self.assertIn("Completed", self.view.phase())
        self.assertNotIn("/resume", self.view.guidance())

    def test_quiet_events_do_not_pollute_the_conversation(self):
        for event in (
            {"type": "presence", "names": ["user"]},
            {"type": "agent.passed", "speaker": "member_a"},
            {
                "type": "workflow.changed",
                "text": "Proposal changed",
                "workflow": self.view.state["workflow"],
            },
            {"type": "turn.idle", "speaker": "member_a", "turn_id": "a", "text": "Still waiting"},
        ):
            self.view.handle(event)
        self.assertNotIn("Proposal changed", self.view.page("conversation").text)
        details = self.view.page("activity").text
        for text in ("Online: user", "Nothing to add", "Proposal changed", "Still waiting"):
            self.assertIn(text, details)

    def test_team_failure_shows_pause_cleanup_and_explicit_recovery(self):
        self.view.handle(
            welcome(
                reason="error",
                messages=2,
                active_turns=[turn("b", "member_b")],
                runtimes={"member_a": {"state": "failed", "error": "Quota exhausted"}},
            )
        )
        self.assertIn("stopping active turns", self.view.phase())
        self.assertIn("Wait for cleanup", self.view.guidance())
        self.view.handle(
            {"type": "turn.finished", "turn_id": "b", "speaker": "member_b", "outcome": "cancelled"}
        )
        self.view.handle(welcome(reason="error", messages=2))
        self.assertEqual(self.view.phase(), "Paused · a team call failed")
        self.assertIn("/retry [agent] or /resume", self.view.guidance())
        self.assertIn("resolve the error", self.view.guidance())
        self.view.handle(welcome(reason="error", messages=2, interaction_mode="serial"))
        self.assertIn("/resume", self.view.guidance())
        self.assertNotIn("/retry", self.view.guidance())

    def test_concurrent_drafts_are_distinct_and_replaced_by_committed_messages(self):
        for event in (
            turn(),
            turn("b", "member_b"),
            {"type": "delta", "turn_id": "a", "speaker": "member_a", "text": "First draft"},
            {"type": "delta", "turn_id": "b", "speaker": "member_b", "text": "Second draft"},
            {
                "type": "delta",
                "turn_id": "a",
                "speaker": "member_a",
                "text": '<team-action>{"action":"approve"}',
            },
        ):
            self.view.handle(event)
        text = self.view.page("conversation").text
        self.assertEqual(text.count("live · not published"), 2)
        self.assertIn("First draft", text)
        self.assertIn("Second draft", text)
        self.assertNotIn("team-action", text)
        self.view.handle(message(4, "Second final", speaker="member_b", turn_id="b"))
        self.view.handle(
            {"type": "turn.finished", "turn_id": "b", "speaker": "member_b", "outcome": "completed"}
        )
        text = self.view.page("conversation").text
        self.assertEqual(text.count("Second final"), 1)
        self.assertNotIn("Second draft", text)
        self.assertEqual(text.count("live · not published"), 1)

    def test_complete_history_is_deduplicated_and_ordered_without_truncation(self):
        large = "x" * 150_000
        self.view.handle(message(20, large))
        self.view.handle(message(10, "Earlier", replay=True))
        self.view.handle(message(20, large, replay=True))
        text = self.view.page("conversation").text
        self.assertEqual(text.count(large), 1)
        self.assertLess(text.index("Earlier"), text.index(large))
        self.assertEqual(self.view.new_messages, 1)

    def test_legacy_welcome_and_phase_specific_permissions(self):
        event = welcome(interaction_mode="serial", active=turn())
        del event["state"]["active_turns"]
        self.view.handle(event)
        self.assertIn("a", self.view.replies.turns)
        self.assertIn("including non-writing", self.view.permissions())
        self.assertIn("live · not published", self.view.page("conversation").text)
        self.assertIn("earlier draft is not replayed", self.view.page("conversation").text)

    def test_member_states_and_complete_errors_remain_available(self):
        self.view.handle(welcome(paused=False, messages=1, reason="running"))
        self.view.handle(turn(phase="implementation"))
        self.view.handle(turn("b", "member_b", phase="judging"))
        self.assertEqual(
            self.view.member_statuses(), [("member_a", "Writing"), ("member_b", "Reviewing")]
        )
        self.view.handle(
            {"type": "turn.idle", "turn_id": "b", "speaker": "member_b", "text": "No output"}
        )
        self.assertEqual(self.view.member_statuses()[1][1], "Waiting for output")
        detail = "Denied\n" + "detail " * 10_000
        self.view.handle({"type": "error", "speaker": "member_b", "text": detail})
        self.assertIn("detail " * 10_000, self.view.page("activity").text)
        self.assertIn("member_b: Denied", self.view.notice)

    def test_terminal_control_sequences_are_not_executed_in_any_view(self):
        self.view.handle(message(1, "safe\x1b[2J\x07text"))
        self.view.record("Error\x1b", "bad\x00text")
        for name in ("conversation", "activity"):
            text = self.view.page(name).text
            self.assertNotIn("\x1b", text)
            self.assertNotIn("\x00", text)
            self.assertNotIn("\x07", text)

    def test_compact_transcript_distinguishes_humans_agents_and_system_messages(self):
        self.view.handle(message(1, "Keep it local.", speaker="user", role="user"))
        self.view.handle(message(2, "Use a local database.\n\nPreserve the input text."))
        self.view.handle(message(3, "Agreement recorded.", speaker="system", role="system"))
        text = self.view.page("conversation").text
        self.assertIn("❯ You · user\n    Keep it local.", text)
        self.assertIn("● member_a\n    Use a local database.\n\n    Preserve", text)
        self.assertIn("· Team\n    Agreement recorded.", text)
        self.assertNotIn("\n\n\n", text)

    def test_live_labels_describe_chat_and_review_without_implying_write_authority(self):
        self.view.handle(turn(phase="implementation", lane="chat"))
        self.view.handle(turn("b", "member_b", phase="judging"))
        text = self.view.page("conversation").text
        self.assertIn("member_a · chatting · live", text)
        self.assertIn("member_b · reviewing · live", text)
        self.assertNotIn("· writing ·", text)

    def test_action_suggestions_describe_effects_and_complete_member_names(self):
        completer = ActionCompleter(lambda: ["claude", "codex"])
        options = list(completer.get_completions(Document("/pa"), CompleteEvent()))
        self.assertEqual([c.text for c in options], ["/pause"])
        self.assertIn("active turns finish", options[0].display_meta_text)
        options = list(completer.get_completions(Document("/retry co"), CompleteEvent()))
        self.assertEqual([c.text for c in options], ["codex"])
        for text in ("ordinary", "explain /pause", "/redirect discuss this", "/retry\nco"):
            self.assertEqual(list(completer.get_completions(Document(text), CompleteEvent())), [])

    def test_consensus_view_distinguishes_saved_documents_and_revised_drafts(self):
        from agent_team.adapters import MockAdapter
        from agent_team.workflow import parse_action

        config = demo_config()
        flow = Workflow(config)
        plan = MockAdapter(config.agents[0], Path.cwd()).workflow_reply(flow.snapshot())
        flow.apply(flow.members[0], parse_action(plan)[1])
        for name in flow.members:
            flow.apply(name, {"action": "approve", "version": 1})
        record = flow.confirm_consensus()
        self.view.state["workflow"] = flow.snapshot()
        self.assertIn("pending or failed", self.view.page("consensus").text)
        self.view.state["consensus_documents"] = {"ready_versions": [1]}
        self.assertIn(record["document"], self.view.page("consensus").text)
        self.assertNotIn("pending or failed", self.view.page("consensus").text)
        flow.reconsider(speaker="user", reason="Change the approach")
        self.view.state["workflow"] = flow.snapshot()
        self.assertIn("Under revision", self.view.page("consensus").text)
        self.assertIn("Previous scope", self.view.page("plan").text)


class ScreenOutput(DummyOutput):
    def __init__(self, columns=80, rows=24):
        self.columns, self.rows = columns, rows

    def get_size(self):
        return Size(rows=self.rows, columns=self.columns)


class RecordingWriter:
    def __init__(self):
        self.requests = []

    def write(self, data):
        self.requests.append(data)

    async def drain(self):
        await asyncio.sleep(0)


async def eventually(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


def screen_text(ui):
    screen = ui.application.renderer._last_screen
    size = ui.application.output.get_size()
    return "\n".join(
        "".join(screen.data_buffer[y][x].char for x in range(size.columns)).rstrip()
        for y in range(size.rows)
    )


class TerminalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pipe_context = create_pipe_input()
        self.pipe = self.pipe_context.__enter__()
        self.output = ScreenOutput()
        self.ui = TeamUI(
            "user",
            session=Path("/example/.agent-team/default"),
            input=self.pipe,
            output=self.output,
        )
        self.reader = asyncio.StreamReader()
        self.writer = RecordingWriter()
        self.task = asyncio.create_task(self.ui.run(self.reader, self.writer, welcome()))
        await eventually(
            lambda: (
                self.ui.painted == (self.ui.view, self.ui.model.revision)
                and self.ui.application.renderer._last_screen
            )
        )

    async def asyncTearDown(self):
        if not self.task.done():
            self.ui.application.exit()
        try:
            await asyncio.wait_for(self.task, 3)
        finally:
            self.pipe_context.__exit__(None, None, None)

    async def test_empty_layout_and_narrow_terminal_keep_the_composer_visible(self):
        frame = screen_text(self.ui)
        for text in ("✳ agent-team", "What would you like", "Describe your idea", "Ctrl-C Exit"):
            self.assertIn(text, frame)
        self.assertNotIn("/reset-session", frame)
        self.assertNotIn("F2 Conversation", frame)
        self.assertEqual(self.ui.input.window.render_info.window_height, 1)
        self.assertGreaterEqual(self.ui.body.window.render_info.window_height, 18)
        self.assertEqual(frame.count("─" * 80), 2)
        self.output.columns, self.output.rows = 50, 14
        self.ui.application.invalidate()
        await eventually(lambda: "─" * 50 in screen_text(self.ui))
        frame = screen_text(self.ui)
        self.assertIn("Describe your idea", frame)
        self.assertIn("Ctrl-C Exit", frame)
        self.assertNotIn("Window too small", frame)

    async def test_question_mark_opens_help_only_when_the_composer_is_empty(self):
        self.pipe.send_text("?")
        await eventually(lambda: self.ui.view == "help")
        self.assertEqual(self.ui.input.text, "")
        self.assertFalse(self.writer.requests)
        self.pipe.send_text("\x1b")
        await eventually(lambda: self.ui.view == "conversation")
        self.pipe.send_text("Keep storage local?")
        await eventually(lambda: self.ui.input.text.endswith("?"))
        self.assertEqual(self.ui.view, "conversation")
        self.assertFalse(self.writer.requests)

    async def test_detail_shortcuts_toggle_without_sending_or_losing_a_draft(self):
        self.pipe.send_text("Keep this draft")
        await eventually(lambda: bool(self.ui.input.text))
        for key, view in (("\x0f", "activity"), ("\x14", "plan")):
            self.pipe.send_text(key)
            await eventually(lambda view=view: self.ui.view == view)
            self.assertEqual(self.ui.input.text, "Keep this draft")
            self.pipe.send_text(key)
            await eventually(lambda: self.ui.view == "conversation")
        self.assertFalse(self.writer.requests)

    async def test_composer_grows_for_wrapped_input_and_shrinks_without_blank_rows(self):
        text = "word " * 40
        self.ui.input.text = text
        self.ui.application.invalidate()
        await eventually(lambda: self.ui.input.window.render_info.window_height > 1)
        self.assertLessEqual(self.ui.input.window.render_info.window_height, 6)
        self.assertEqual(self.ui.input.text, text)
        self.output.columns, self.output.rows = 50, 14
        self.ui.application.invalidate()
        await eventually(
            lambda: (
                self.ui.input.window.render_info.window_height <= 3
                and "─" * 50 in screen_text(self.ui)
            )
        )
        self.assertNotIn("Window too small", screen_text(self.ui))
        self.ui.input.text = ""
        self.ui.application.invalidate()
        await eventually(lambda: self.ui.input.window.render_info.window_height == 1)
        self.assertFalse(self.writer.requests)

    async def test_theme_uses_the_terminal_background_and_guidance_is_contextual(self):
        self.assertEqual(self.ui.application.style.get_attrs_for_style_str("").bgcolor, "")
        self.assertFalse(self.ui.guidance_visible())
        self.ui.model.handle(welcome(reason="user", messages=1))
        self.assertTrue(self.ui.guidance_visible())
        self.ui.application.invalidate()
        await eventually(lambda: "/resume" in screen_text(self.ui))
        self.ui.model.handle(welcome(paused=False, reason="running", messages=1))
        self.assertFalse(self.ui.guidance_visible())
        self.ui.model.handle({"type": "error", "speaker": "member_a", "text": "Check details"})
        self.assertTrue(self.ui.guidance_visible())

    async def test_escape_dismisses_completion_before_leaving_a_detail_view(self):
        self.ui.show("plan")
        self.pipe.send_text("/pa")
        await eventually(lambda: self.ui.input.buffer.complete_state is not None)
        self.pipe.send_text("\x1b")
        await eventually(lambda: self.ui.input.buffer.complete_state is None)
        self.assertEqual(self.ui.view, "plan")
        self.assertEqual(self.ui.input.text, "/pa")
        self.pipe.send_text("\x1b")
        await eventually(lambda: self.ui.view == "conversation")
        self.assertEqual(self.ui.input.text, "/pa")
        self.assertFalse(self.writer.requests)

    async def test_bracketed_multiline_paste_requires_explicit_enter(self):
        self.pipe.send_text("\x1b[200~Build a tool\nKeep storage local\nAdd tests\x1b[201~")
        await eventually(lambda: "Add tests" in self.ui.input.text)
        self.assertFalse(self.writer.requests)
        self.pipe.send_text("\r")
        await eventually(lambda: bool(self.writer.requests))
        self.assertEqual(
            self.writer.requests,
            [encode({"type": "say", "text": "Build a tool\nKeep storage local\nAdd tests"})],
        )
        self.assertEqual(self.ui.input.text, "")

    async def test_alt_enter_and_ctrl_j_insert_lines_without_sending(self):
        self.pipe.send_text("First\x1b\rSecond\nThird")
        await eventually(lambda: "Third" in self.ui.input.text)
        self.assertEqual(self.ui.input.text, "First\nSecond\nThird")
        self.assertFalse(self.writer.requests)

    async def test_invalid_command_preserves_the_editable_draft(self):
        self.pipe.send_text("/unknown\r")
        await eventually(lambda: bool(self.ui.model.notice))
        self.assertEqual(self.ui.input.text, "/unknown")
        self.assertFalse(self.writer.requests)

    async def test_completion_accepts_the_suggestion_before_executing_the_action(self):
        self.pipe.send_text("/pa")
        await eventually(lambda: self.ui.input.buffer.complete_state is not None)
        self.pipe.send_text("\t")
        await eventually(lambda: self.ui.input.text == "/pause")
        self.pipe.send_text("\r")
        await eventually(lambda: self.ui.input.buffer.complete_state is None)
        self.assertFalse(self.writer.requests)
        self.pipe.send_text("\r")
        await eventually(lambda: bool(self.writer.requests))
        self.assertEqual(self.writer.requests, [encode({"type": "control", "action": "pause"})])

    async def test_reading_snapshot_survives_new_messages_and_draft_reflow(self):
        self.ui.model.handle(message(1, "\n".join(f"Line {i}" for i in range(60))))
        self.ui.model.handle(turn())
        self.ui.application.invalidate()
        await eventually(lambda: "Line 59" in self.ui.body.text)
        self.pipe.send_text("\x1b[5~")
        await eventually(lambda: not self.ui.follow)
        snapshot = self.ui.body.document
        self.ui.model.handle(message(2, "A new public message"))
        self.ui.model.handle(
            {"type": "delta", "turn_id": "a", "speaker": "member_a", "text": "Long draft " * 1_000}
        )
        self.ui.paint()
        self.assertEqual(self.ui.body.document, snapshot)
        self.assertIn("1 new messages", self.ui.reading_hint())
        self.pipe.send_text("\x1b[1;5F")
        await eventually(lambda: self.ui.follow)
        self.assertIn("A new public message", self.ui.body.text)
        self.assertIn(("Long draft " * 1_000).rstrip(), self.ui.body.text)

    async def test_long_wrapped_paragraph_can_be_read_by_screen_position(self):
        self.ui.model.handle(message(1, "word " * 2_000))
        self.ui.application.invalidate()
        await eventually(lambda: "word " in self.ui.body.text)
        before = self.ui.body.buffer.cursor_position
        self.pipe.send_text("\x1b[5~")
        await eventually(lambda: not self.ui.follow)
        position = self.ui.body.buffer.cursor_position
        self.assertLess(position, before)
        self.assertGreater(position, before - 2_000)

    async def test_local_navigation_does_not_send_unknown_wire_commands(self):
        self.pipe.send_text("/activity\r")
        await eventually(lambda: self.ui.view == "activity")
        self.assertFalse(self.writer.requests)
        self.pipe.send_text("/chat\r")
        await eventually(lambda: self.ui.view == "conversation")
        self.pipe.send_text("/status\r")
        await eventually(lambda: bool(self.writer.requests))
        self.assertEqual(self.ui.view, "status")
        self.assertEqual(self.writer.requests[-1], encode({"type": "status"}))

    async def test_disconnect_preserves_drafts_and_disables_network_actions(self):
        self.reader.feed_eof()
        await eventually(lambda: not self.ui.connected)
        self.pipe.send_text("Keep this draft\r")
        await eventually(lambda: "Keep this draft" in self.ui.input.text)
        self.assertFalse(self.writer.requests)
        self.assertFalse(self.task.done())
        self.assertIn("Disconnected", self.ui.guidance()[0][1])

    async def test_failed_send_preserves_the_request_without_automatic_replay(self):
        async def fail():
            raise ConnectionError("Write failed after delivery became uncertain")

        self.writer.drain = fail
        self.pipe.send_text("Preserve this request\r")
        await eventually(lambda: not self.ui.connected)
        self.assertEqual(self.ui.input.text, "Preserve this request")
        self.assertEqual(len(self.writer.requests), 1)
        self.assertIn("Delivery not confirmed", self.ui.model.page("activity").text)
        self.pipe.send_text("\r")
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.writer.requests), 1)

    async def test_interrupt_keeps_the_draft_and_leave_does_not_send_control(self):
        self.ui.model.handle(turn())
        self.pipe.send_text("Unfinished idea\x03")
        await eventually(lambda: bool(self.writer.requests))
        self.assertEqual(self.writer.requests, [encode({"type": "control", "action": "interrupt"})])
        self.assertEqual(self.ui.input.text, "Unfinished idea")
        self.pipe.send_text("\x04")
        await eventually(lambda: self.ui.confirm_leave)
        self.assertFalse(self.task.done())
        self.pipe.send_text("\x04")
        await eventually(lambda: self.task.done())
        self.assertEqual(len(self.writer.requests), 1)

    async def test_owner_exit_requires_confirmation(self):
        self.ui.stop_on_exit = True
        self.pipe.send_text("\x04")
        await eventually(lambda: self.ui.confirm_leave)
        self.assertFalse(self.task.done())
        self.pipe.send_text("\x04")
        await eventually(lambda: self.task.done())
        self.assertFalse(self.writer.requests)

    async def test_ctrl_c_exits_an_idle_owner_immediately_without_sending_interrupt(self):
        self.ui.stop_on_exit = True
        self.pipe.send_text("\x03")
        await eventually(lambda: self.task.done())
        self.assertFalse(self.writer.requests)

    async def test_fast_double_ctrl_c_flushes_one_interrupt_before_exiting(self):
        self.ui.model.handle(turn())
        self.pipe.send_text("\x03\x03")
        await eventually(lambda: self.task.done())
        self.assertEqual(self.writer.requests, [encode({"type": "control", "action": "interrupt"})])

    async def test_idle_draft_requires_confirmation_and_escape_preserves_it(self):
        self.pipe.send_text("Unsent draft\x03")
        await eventually(lambda: self.ui.confirm_leave == "Ctrl-C")
        self.assertFalse(self.task.done())
        self.assertFalse(self.writer.requests)
        self.assertIn("Ctrl-C again", self.ui.guidance()[0][1])
        self.assertEqual(self.ui.input.text, "Unsent draft")
        self.pipe.send_text("\x1b")
        await eventually(lambda: self.ui.confirm_leave is None)
        self.assertEqual(self.ui.input.text, "Unsent draft")
        self.pipe.send_text("\x03\x03")
        await eventually(lambda: self.task.done())
        self.assertFalse(self.writer.requests)

    async def test_editing_after_ctrl_c_cancels_the_old_exit_confirmation(self):
        self.ui.model.handle(turn())
        self.pipe.send_text("\x03")
        await eventually(lambda: self.ui.confirm_leave == "Ctrl-C")
        self.pipe.send_text("Keep this")
        await eventually(
            lambda: self.ui.confirm_leave is None and self.ui.input.text == "Keep this"
        )
        self.pipe.send_text("\x03")
        await eventually(lambda: self.ui.confirm_leave == "Ctrl-C")
        self.assertFalse(self.task.done())
        self.assertEqual(self.ui.input.text, "Keep this")

    async def test_disconnected_ctrl_c_ignores_stale_active_turns_and_exits(self):
        self.ui.model.handle(turn())
        self.reader.feed_eof()
        await eventually(lambda: not self.ui.connected)
        self.pipe.send_text("\x03")
        await eventually(lambda: self.task.done())
        self.assertFalse(self.writer.requests)

    async def test_confirmed_exit_finishes_even_if_interrupt_delivery_fails(self):
        async def fail():
            raise ConnectionError("Connection dropped")

        self.writer.drain = fail
        self.ui.model.handle(turn())
        self.pipe.send_text("\x03\x03")
        await eventually(lambda: self.task.done())
        self.assertEqual(len(self.writer.requests), 1)
        self.assertIn("Delivery not confirmed", self.ui.model.page("activity").text)

    async def test_another_ctrl_c_can_exit_while_the_transport_is_stalled(self):
        async def stall():
            await asyncio.Event().wait()

        self.writer.drain = stall
        self.ui.model.handle(turn())
        self.pipe.send_text("\x03\x03")
        await eventually(lambda: self.ui.exiting and bool(self.writer.requests))
        self.assertFalse(self.task.done())
        self.assertIn("close immediately", self.ui.guidance()[0][1])
        self.pipe.send_text("\x03")
        await eventually(lambda: self.task.done())
        self.assertEqual(len(self.writer.requests), 1)
        self.assertIn("Delivery not confirmed", self.ui.model.page("activity").text)

    async def test_error_and_status_events_render_without_exposing_event_json(self):
        self.reader.feed_data(
            encode(
                {
                    "type": "error",
                    "speaker": "member_a",
                    "text": "Account unavailable\nResolve account access before retrying.",
                }
            )
        )
        await eventually(lambda: "Account unavailable" in self.ui.model.notice)
        self.pipe.send_text("/activity\r")
        await eventually(lambda: self.ui.view == "activity")
        self.assertIn("Resolve account access before retrying.", self.ui.body.text)
        self.assertNotIn('"type": "error"', self.ui.body.text)

    async def test_reading_with_mouse_freezes_the_snapshot(self):
        from prompt_toolkit.data_structures import Point
        from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

        self.ui.model.handle(message(1, "Initial public message"))
        self.ui.paint()
        self.ui.body.control.mouse_handler(
            MouseEvent(Point(0, 0), MouseEventType.SCROLL_UP, MouseButton.NONE, frozenset())
        )
        self.assertFalse(self.ui.follow)
        text = self.ui.body.text
        self.ui.model.handle(message(2, "New public message"))
        self.ui.paint()
        self.assertEqual(self.ui.body.text, text)


class LiveRoomTests(unittest.IsolatedAsyncioTestCase):
    async def test_ctrl_c_from_an_idle_join_client_does_not_stop_or_control_the_server(self):
        with tempfile.TemporaryDirectory(prefix="agent-team-ui-exit-") as directory:
            path = Path(directory)
            server = Server(replace(demo_config(), workspace=path), path / "session")
            await server.start()
            writer, task = None, None
            try:
                reader, writer = await connect(path / "session", "user")
                greeting = await receive(reader)
                with create_pipe_input() as pipe:
                    ui = TeamUI("user", input=pipe, output=ScreenOutput())
                    task = asyncio.create_task(ui.run(reader, writer, greeting))
                    await eventually(lambda: ui.application.is_running)
                    pipe.send_text("\x03")
                    await asyncio.wait_for(task, 3)
                writer.close()
                await writer.wait_closed()
                await eventually(lambda: not server.clients)
                self.assertFalse(server.room.closed)
                self.assertFalse(any(e["type"] == "room.control" for e in server.store.events()))
            finally:
                if task and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if writer:
                    writer.close()
                    await writer.wait_closed()
                await server.close()

    async def test_tui_works_with_the_existing_server_protocol_and_mock_workflow(self):
        with tempfile.TemporaryDirectory(prefix="agent-team-ui-test-") as directory:
            path = Path(directory)
            config = replace(
                demo_config(), workspace=path, interaction_mode="chatroom", turn_delay=0
            )
            server = Server(config, path / "session")
            await server.start()
            writer = None
            task = None
            try:
                reader, writer = await connect(path / "session", "user")
                greeting = await receive(reader)
                with create_pipe_input() as pipe:
                    ui = TeamUI("user", input=pipe, output=ScreenOutput())
                    task = asyncio.create_task(ui.run(reader, writer, greeting))
                    await eventually(lambda: ui.application.is_running)
                    pipe.send_text("Build and review a greeting function together.\r")
                    await eventually(lambda: ui.model.state.get("reason") == "completed")
                    self.assertTrue(ui.model.messages)
                    self.assertIn("Completed", ui.model.phase())
                    pipe.send_text("/plan\r")
                    await eventually(lambda: ui.view == "plan")
                    self.assertIn("Acceptance exit code: 0", ui.body.text)
                    pipe.send_text("\x04")
                    await asyncio.wait_for(task, 3)
                writer.close()
                await writer.wait_closed()
                await eventually(lambda: not server.clients)
                self.assertFalse(server.room.closed)
                self.assertEqual(server.room.reason, "completed")
            finally:
                if task and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if writer:
                    writer.close()
                    await writer.wait_closed()
                await server.close()


if __name__ == "__main__":
    unittest.main()
