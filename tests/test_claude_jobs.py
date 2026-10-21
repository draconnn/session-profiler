"""When a killed or stopped Claude Code background job's notice is its real end."""
from __future__ import annotations

from support import IsolatedTest, at, ms, stamp, write_jsonl  # first: sets the import paths

import claude  # noqa: E402

SESSION = "11111111-2222-4333-8444-555555555555"


def entry(kind: str, seconds: float, content, sidechain: bool = False) -> dict:
    return {"type": kind, "timestamp": stamp(at(seconds)), "sessionId": SESSION, "isSidechain": sidechain,
            "uuid": f"{kind}-{seconds}", "message": {"role": kind, "content": content}}


def launch(call_id: str, seconds: float, sidechain: bool = False) -> list[dict]:
    """A Bash call run in the background: the call, then its immediate result."""

    use = {"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": "sleep 999", "run_in_background": True}}
    result = {"type": "tool_result", "tool_use_id": call_id, "content": f"Command running in background with ID: {call_id[-4:]}"}
    return [entry("assistant", seconds, [use], sidechain), entry("user", seconds + 1, [result], sidechain)]


def notice_text(call_id: str, status: str, summary: str) -> str:
    return (f"<task-notification><task-id>{call_id[-4:]}</task-id><tool-use-id>{call_id}</tool-use-id>"
            f"<status>{status}</status><summary>{summary}</summary></task-notification>")


def queued(call_id: str, seconds: float, status: str, summary: str) -> dict:
    return {"type": "queue-operation", "operation": "enqueue", "timestamp": stamp(at(seconds)), "sessionId": SESSION,
            "content": notice_text(call_id, status, summary)}


class KilledJobTest(IsolatedTest):
    def job(self, status: str, summary: str, notice_after: float) -> dict:
        """One background job launched at 1 s; the last other entry is at 5 s; the notice comes ``notice_after`` s later."""

        lines = [entry("user", 0, "Run the slow thing"), *launch("toolu_job1", 1), entry("assistant", 5, [{"type": "text", "text": "Started."}]),
                 queued("toolu_job1", 5 + notice_after, status, summary)]
        parsed = claude._parse_transcript(write_jsonl(self.tmp / "main.jsonl", lines), sidechain=False)
        [job] = [event for event in parsed["events"] if event["kind"] == "job"]
        return job

    def assertRealEnd(self, job: dict, notice_after: float) -> None:
        self.assertEqual(job["timing"], "notification_pair")
        self.assertEqual(job["endMs"], ms(at(5 + notice_after)))
        self.assertEqual(job["durationMs"], ms(at(5 + notice_after)) - ms(at(1)))

    def assertUnknownEnd(self, job: dict) -> None:
        self.assertEqual(job["timing"], "unknown")
        self.assertIsNone(job["endMs"])
        self.assertIsNone(job["durationMs"])
        self.assertIn("real end unknown", job["status"])

    def test_named_stops_are_real_ends_even_when_quiet(self) -> None:
        for summary in ('Background command "sleep" was stopped by the user',
                        "Task was stopped by the main session",
                        "Stopped by Claude",
                        "Killed after exceeding the background time limit"):
            for status in ("killed", "stopped"):
                with self.subTest(summary=summary, status=status):
                    self.assertRealEnd(self.job(status, summary, notice_after=3600), 3600)

    def test_outlived_its_session_is_unknown_even_when_prompt(self) -> None:
        for summary in ("The task didn't finish before the previous session ended",
                        "No completion record was found for this task"):
            with self.subTest(summary=summary):
                self.assertUnknownEnd(self.job("killed", summary, notice_after=2))

    def test_no_reason_while_quiet_is_unknown(self) -> None:
        self.assertUnknownEnd(self.job("stopped", 'Background command "sleep" was stopped', notice_after=61))

    def test_no_reason_within_a_minute_is_a_real_end(self) -> None:
        self.assertRealEnd(self.job("stopped", 'Background command "sleep" was stopped', notice_after=59), 59)

    def test_completed_is_always_a_real_end(self) -> None:
        self.assertRealEnd(self.job("completed", "Background command completed (exit code 0)", notice_after=7200), 7200)

    def test_two_cleanup_notices_in_a_subagent_are_both_quiet(self) -> None:
        # In a subagent transcript the notices arrive as user entries. The second, 5 ms after the
        # first, must not count the first as recent activity.
        lines = [entry("user", 0, "Subagent task", sidechain=True), *launch("toolu_jobA", 1, True), *launch("toolu_jobB", 2, True),
                 entry("assistant", 5, [{"type": "text", "text": "Both started."}], sidechain=True),
                 entry("user", 900, notice_text("toolu_jobA", "killed", "Background command was killed"), sidechain=True),
                 entry("user", 900.005, notice_text("toolu_jobB", "killed", "Background command was killed"), sidechain=True)]
        parsed = claude._parse_transcript(write_jsonl(self.tmp / "agent-x.jsonl", lines), sidechain=True)
        jobs = [event for event in parsed["events"] if event["kind"] == "job"]
        self.assertEqual(len(jobs), 2)
        for job in jobs:
            with self.subTest(job=job["id"]):
                self.assertUnknownEnd(job)
