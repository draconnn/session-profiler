"""The whole pipeline on the made-up demo session: inventory, runtime copy, report build, summary.

The report is built with ``launch.py --refresh-only``, as the helper does before it starts the
server; no server is started.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys

from support import IsolatedTest  # first: sets the import paths

import make_demo  # noqa: E402
import profile_session  # noqa: E402


class DemoSessionTest(IsolatedTest):
    def setUp(self) -> None:
        super().setUp()
        os.environ["SESSION_PROFILER_SOURCE"] = "claude"
        with contextlib.redirect_stdout(io.StringIO()):
            make_demo.main(self.tmp)
        rows = profile_session.read_inventory()
        self.rows = {row["id"]: row for row in rows}
        _selected, root = profile_session.select(rows, make_demo.MAIN, None, os.environ, "claude")
        runtime = profile_session.runtime_root("claude") / root
        self.assertTrue(str(runtime).startswith(str(self.tmp)))
        runtime.mkdir(parents=True)
        profile_session.prepare(runtime)
        subprocess.run([sys.executable, str(runtime / "launch.py"), "--session", root, "--limit", "1", "--refresh-only"],
                       check=True, capture_output=True, text=True, timeout=120)
        self.profile = json.loads((runtime / "data/profile.json").read_text())
        self.detail = self.profile["details"][root]
        self.summary = profile_session.summarize(self.profile, root)
        self.agents = {agent["name"]: agent for agent in self.detail["agents"]}
        self.main = self.detail["agents"][0]

    def events(self, kind: str) -> list[dict]:
        return [event for agent in self.detail["agents"] for event in agent["events"] if event["kind"] == kind]

    def test_subagents_and_side_session(self) -> None:
        self.assertEqual(self.summary["subagentCount"], 3)
        self.assertEqual(self.main["id"], make_demo.MAIN)
        self.assertEqual(self.agents["Write release notes"]["id"], make_demo.SIDE)
        self.assertEqual(self.agents["Write release notes"]["parentId"], make_demo.MAIN)
        self.assertEqual(self.rows[make_demo.SIDE]["parentId"], make_demo.MAIN)
        self.assertEqual(sorted(agent["parentId"] for agent in self.detail["agents"][1:]), [make_demo.MAIN] * 3)

    def test_background_job(self) -> None:
        jobs = self.events("job")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["summary"], "Run the full test suite")
        self.assertEqual(jobs[0]["durationMs"], 4 * 60_000 + 18_000)
        self.assertEqual(jobs[0]["status"], "completed")
        self.assertEqual((self.summary["backgroundJobCount"], self.summary["backgroundJobOpenCount"]), (1, 0))

    def test_question_waits_for_the_user(self) -> None:
        [question] = [event for event in self.events("tool") if event["label"] == "AskUserQuestion"]
        self.assertEqual(question["durationMs"], 3 * 60_000 + 56_000)

    def test_idle_wait_before_wake_up_is_not_active(self) -> None:
        [wait] = self.events("wait")
        self.assertEqual(wait["label"], "Idle until app message")
        self.assertTrue(wait["idle"])
        self.assertGreater(wait["durationMs"], 0)
        turns = self.main["turns"]
        self.assertEqual([turn.get("kind") for turn in turns], [None, "wake", None])
        for turn in turns:
            self.assertFalse(turn["startMs"] < wait["endMs"] and wait["startMs"] < turn["endMs"], "wait overlaps a turn")
        self.assertEqual(self.main["activeMs"], sum(turn["endMs"] - turn["startMs"] for turn in turns))
        self.assertEqual(self.summary["summedActionMs"], sum(e["durationMs"] for e in self.events("tool")))

    def test_prompts(self) -> None:
        prompts = [message for message in self.main["messages"] if message["phase"] == "prompt"]
        self.assertEqual(len(prompts), 2)
        self.assertTrue(prompts[0]["text"].startswith("Add a CSV export button"))

    def test_no_unknown_durations(self) -> None:
        self.assertEqual(self.summary["unknownDurationCount"], 0)
        for agent in self.detail["agents"]:
            for event in agent["events"]:
                self.assertIsNotNone(event["durationMs"], event["id"])

    def test_time_breakdown_adds_up_to_wall_span(self) -> None:
        parts = self.summary["timeBreakdown"]
        wall = self.detail["wallDurationMs"]
        self.assertEqual(wall, 30 * 60_000 + 48_000)
        self.assertEqual(parts["workingMs"] + parts["backgroundOnlyMs"] + parts["nothingRunningMs"], wall)
        self.assertGreater(parts["nothingRunningMs"], 0)
