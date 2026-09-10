from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_team.acceptance import run_acceptance_checks
from agent_team.activity import observe_activity
from agent_team.adapters import AdapterError, CLIAdapter
from agent_team.config import AgentConfig, TeamConfig
from agent_team.context import PASS
from agent_team.engine import Room
from agent_team.store import Store
from agent_team.streams import iter_lines


async def eventually(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


class ObserverTests(unittest.IsolatedAsyncioTestCase):
    async def test_activity_reports_are_throttled_without_a_timer_or_warning(self):
        reports = []
        loop = asyncio.get_running_loop()
        async with observe_activity(
            0, self.fail, on_activity=lambda: reports.append(True)
        ) as touch:
            self.assertEqual(reports, [])
            with patch.object(loop, "time", return_value=10) as clock:
                for _ in range(100):
                    touch()
                self.assertEqual(len(reports), 1)
                clock.return_value = 10.9
                touch()
                self.assertEqual(len(reports), 1)
                clock.return_value = 11
                touch()
                self.assertEqual(len(reports), 2)
        touch()  # A late callback cannot revive a completed turn.
        self.assertEqual(len(reports), 2)

    async def test_activity_after_idle_reports_immediately_even_inside_throttle_window(self):
        notices, reports = [], []
        async with observe_activity(
            0.03, notices.append, on_activity=lambda: reports.append(True)
        ) as touch:
            touch()
            await eventually(lambda: len(notices) == 1)
            self.assertEqual(len(reports), 1)
            touch()
            self.assertEqual(len(reports), 2)

    async def test_one_notice_per_silence_activity_rearms_and_exit_cleans_up(self):
        notices = []
        async with observe_activity(0.03, notices.append) as touch:
            await eventually(lambda: len(notices) == 1)
            await asyncio.sleep(0.08)
            self.assertEqual(len(notices), 1)
            touch()
            await eventually(lambda: len(notices) == 2)
        await asyncio.sleep(0.05)
        self.assertEqual(len(notices), 2)
        self.assertTrue(all(seconds >= 0.03 for seconds in notices))
        self.assertFalse(any(t.get_name() == "idle-observer" for t in asyncio.all_tasks()))

    async def test_activity_postpones_warning_until_the_next_silent_period(self):
        notices = []
        async with observe_activity(0.1, notices.append) as touch:
            for _ in range(20):
                touch()
                await asyncio.sleep(0.01)
            self.assertEqual(notices, [])
            await eventually(lambda: len(notices) == 1)

    async def test_zero_does_not_start_observer(self):
        notices = []
        with patch("agent_team.activity.asyncio.create_task") as spawn:
            async with observe_activity(0, notices.append) as touch:
                touch()
                await asyncio.sleep(0)
            spawn.assert_not_called()
        self.assertEqual(notices, [])

    async def test_exception_cleans_up_observer(self):
        with self.assertRaisesRegex(RuntimeError, "worker failed"):
            async with observe_activity(1, lambda seconds: None):
                await asyncio.sleep(0)
                raise RuntimeError("worker failed")
        self.assertFalse(any(t.get_name() == "idle-observer" for t in asyncio.all_tasks()))


class StreamActivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_frames_are_activity_before_a_line_can_be_decoded(self):
        reader = asyncio.StreamReader()
        activity, lines = [], []

        async def consume():
            async for line in iter_lines(reader, on_activity=lambda: activity.append(True)):
                lines.append(line)

        task = asyncio.create_task(consume())
        try:
            reader.feed_data(b'{"type":')
            await eventually(lambda: len(activity) == 1)
            self.assertEqual(lines, [])
            reader.feed_data(b'"done"}\nfirst\nlast')
            await eventually(lambda: len(activity) == 2)
            reader.feed_eof()
            await asyncio.wait_for(task, 3)
            self.assertEqual(lines, [b'{"type":"done"}\n', b"first\n", b"last"])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_non_public_cli_frames_and_stderr_signal_activity_without_leaking(self):
        for channel, script in (
            ("stdout", 'print(\'{"type":"tool","private":"secret"}\', flush=True)'),
            ("stderr", 'sys.stderr.write("secret"); sys.stderr.flush()'),
        ):
            with self.subTest(channel=channel):
                notices = []
                adapter = CLIAdapter(
                    AgentConfig(
                        "a",
                        "command",
                        command=(
                            sys.executable,
                            "-c",
                            "import sys; sys.stdin.read(); " + script,
                        ),
                    ),
                    Path.cwd(),
                )
                # EOF without a successful reply still fails, even after observable activity.
                replies = []
                with self.assertRaises(AdapterError):
                    async for delta in adapter.stream(
                        "topic", on_activity=lambda notices=notices: notices.append(True)
                    ):
                        replies.append(delta)
                self.assertTrue(notices)
                self.assertEqual(replies, [])


class ControlledAdapter:
    supports_activity = True

    def __init__(self):
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.cancelled = False
        self.activity = None

    async def stream(self, prompt, *, phase="discussion", on_activity=None):
        self.activity = on_activity
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        yield "finished"


class RoomActivityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "events.sqlite3")
        self.events = []
        self.adapter = ControlledAdapter()
        config = TeamConfig(
            workflow="discussion",
            workspace=Path(self.temp.name),
            agents=(AgentConfig("a", "mock"),),
            idle_warning_seconds=0.04,
            turn_delay=0,
        )
        self.room = Room(config, self.store, self.events.append, {"a": self.adapter})
        self.room.start()

    async def asyncTearDown(self):
        await self.room.close()
        self.store.close()
        self.temp.cleanup()

    def warnings(self):
        return [e for e in self.events if e["type"] == "turn.idle"]

    async def start_turn(self):
        self.room.say("human", "topic")
        self.room.single_step = True
        await asyncio.wait_for(self.adapter.started.wait(), 3)

    async def test_notice_preserves_floor_and_successful_reply_can_still_commit(self):
        await self.start_turn()
        turn_id = self.room.active["turn_id"]
        await eventually(lambda: len(self.warnings()) == 1)
        warning = self.warnings()[0]
        self.assertEqual((warning["speaker"], warning["turn_id"]), ("a", turn_id))
        self.assertEqual(warning["phase"], "discussion")
        self.assertFalse(self.adapter.cancelled)
        self.assertFalse(self.room.manual_paused)
        self.assertEqual(self.room.active["turn_id"], turn_id)
        self.assertEqual(len(self.room.messages), 1)
        self.assertNotIn("turn.idle", [e["type"] for e in self.store.events()])
        self.adapter.release.set()
        await eventually(lambda: self.room.active is None)
        self.assertEqual(self.room.messages[-1]["text"], "finished")
        self.assertEqual(self.room.reason, "step_complete")
        await asyncio.sleep(0.06)
        self.assertEqual(len(self.warnings()), 1)

    async def test_interrupt_after_notice_still_cancels_without_committing(self):
        await self.start_turn()
        await eventually(lambda: len(self.warnings()) == 1)
        self.room.control("interrupt")
        await eventually(lambda: self.room.active is None)
        self.assertTrue(self.adapter.cancelled)
        self.assertEqual(len(self.room.messages), 1)
        self.assertEqual(self.room.reason, "user")
        await asyncio.sleep(0.06)
        self.assertEqual(len(self.warnings()), 1)

    async def test_non_reply_activity_rearms_room_observer(self):
        await self.start_turn()
        await eventually(lambda: len(self.warnings()) == 1)
        self.assertIsNotNone(self.adapter.activity)
        self.adapter.activity()
        await eventually(lambda: len(self.warnings()) == 2)
        self.assertEqual(len(self.room.messages), 1)
        self.assertFalse(self.adapter.cancelled)

    async def test_non_reply_activity_is_ephemeral_and_does_not_advance_the_room(self):
        await self.start_turn()
        head = self.room.messages[-1]["id"]
        self.adapter.activity()
        reports = [e for e in self.events if e["type"] == "turn.activity"]
        self.assertEqual(
            reports,
            [{"type": "turn.activity", "speaker": "a", "turn_id": self.room.active["turn_id"]}],
        )
        self.assertEqual(self.room.messages[-1]["id"], head)
        self.assertNotIn("turn.activity", [e["type"] for e in self.store.events()])
        self.assertFalse(self.room.manual_paused)
        await eventually(lambda: bool(self.warnings()))
        self.room.control("interrupt")
        self.adapter.activity()  # Revoked turns must not appear active during cleanup.
        self.assertEqual(sum(e["type"] == "turn.activity" for e in self.events), 1)

    async def test_every_agent_phase_uses_no_default_hard_deadline(self):
        class FastAdapter:
            async def stream(self, prompt, *, phase="discussion"):
                yield PASS

        self.room.adapters["a"] = FastAdapter()
        with patch("agent_team.engine.asyncio.timeout", wraps=asyncio.timeout) as deadline:
            for phase in ("discussion", "planning", "implementation", "judging", "review"):
                reply, _ = await self.room.collect("a", "topic", "turn", self.room.revision, phase)
                self.assertEqual(reply, PASS)
            self.assertEqual([c.args for c in deadline.call_args_list], [(None,)] * 5)


class CheckActivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_silent_check_warns_but_finishes_successfully_with_no_deadline(self):
        notices = []
        with tempfile.TemporaryDirectory() as directory:
            async with observe_activity(0.03, notices.append) as touch:
                results = await run_acceptance_checks(
                    [[sys.executable, "-c", "import time; time.sleep(0.15); print('passed')"]],
                    Path(directory),
                    0,
                    lambda text: None,
                    on_activity=touch,
                )
        self.assertTrue(notices)
        self.assertEqual(results[0]["exit_code"], 0)
        self.assertEqual(results[0]["output"], "passed\n")

    async def test_acceptance_command_chunks_are_activity(self):
        activity = []
        with tempfile.TemporaryDirectory() as directory:
            results = await run_acceptance_checks(
                [[sys.executable, "-c", "print('test output')"]],
                Path(directory),
                0,
                lambda text: None,
                on_activity=lambda: activity.append(True),
            )
        # Starting the command, its output, and its exit are all observable.
        self.assertGreaterEqual(len(activity), 3)
        self.assertEqual(results[0]["exit_code"], 0)
