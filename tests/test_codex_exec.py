"""Codex exec calls: start-up-only command records and scripts that outlive their call."""
from __future__ import annotations

import json

from support import IsolatedTest, at, ms, stamp, write_jsonl  # first: sets the import paths

import parser as codex  # noqa: E402

TURN = "turn-1"


def record(seconds: float, kind: str, payload: dict) -> dict:
    return {"timestamp": stamp(at(seconds)), "type": kind, "payload": payload}


def exec_call(call_id: str, seconds: float, code: str) -> dict:
    return record(seconds, "response_item", {"type": "custom_tool_call", "call_id": call_id, "name": "exec", "input": code,
                                             "internal_chat_message_metadata_passthrough": {"turn_id": TURN}})


def wait_call(call_id: str, seconds: float, cell: str) -> dict:
    return record(seconds, "response_item", {"type": "custom_tool_call", "call_id": call_id, "name": "wait",
                                             "input": json.dumps({"cell_id": cell}), "internal_chat_message_metadata_passthrough": {"turn_id": TURN}})


def output(call_id: str, seconds: float, text: str) -> dict:
    return record(seconds, "response_item", {"type": "custom_tool_call_output", "call_id": call_id, "output": text})


def command(item_id: str, seconds: float, cmd: str, nanos: int = 400_000) -> dict:
    """A command item that recorded only its start-up time (under 1 ms)."""

    done = ms(at(seconds))
    return record(seconds, "event_msg", {"type": "item_completed", "turn_id": TURN, "started_at_ms": done, "completed_at_ms": done,
                                         "item": {"type": "CommandExecution", "id": item_id, "command": ["/bin/zsh", "-lc", cmd],
                                                  "status": "completed", "exit_code": 0, "duration": {"secs": 0, "nanos": nanos}}})


def turn(lines: list[dict], end: float) -> list[dict]:
    return [record(0, "event_msg", {"type": "task_started", "turn_id": TURN}), *lines,
            record(end, "event_msg", {"type": "task_complete", "turn_id": TURN})]


class CodexExecTest(IsolatedTest):
    def parse(self, lines: list[dict]) -> dict:
        return codex._parse_rollout(write_jsonl(self.tmp / "rollout.jsonl", lines), {"id": "chat-1"})

    def commands(self, lines: list[dict]) -> dict[str, dict]:
        return {event["id"]: event for event in self.parse(lines)["events"] if event["label"] == "Command"}

    def test_single_exec_command_takes_the_exec_span(self) -> None:
        found = self.commands(turn([exec_call("c1", 1, 'const r = await tools.exec_command({cmd: "npm test"});\ntext(r.output);'),
                                    command("cmd1", 1.002, "npm test"), output("c1", 13.5, "ok")], end=14))
        self.assertEqual(found["cmd1"]["timing"], "exec_span")
        self.assertEqual((found["cmd1"]["startMs"], found["cmd1"]["endMs"]), (ms(at(1)), ms(at(13.5))))
        self.assertEqual(found["cmd1"]["durationMs"], 12_500)

    def test_script_with_other_tool_calls_is_unknown(self) -> None:
        code = 'await tools.exec_command({cmd: "npm run dev"});\nawait tools.write_stdin({session_id: 1, chars: ""});'
        found = self.commands(turn([exec_call("c1", 1, code), command("cmd1", 1.002, "npm run dev"), output("c1", 9, "ok")], end=10))
        self.assertEqual(found["cmd1"]["timing"], "startup_only")
        self.assertIsNone(found["cmd1"]["durationMs"])
        self.assertIsNone(found["cmd1"]["endMs"])

    def test_two_commands_in_one_exec_call_are_unknown(self) -> None:
        code = 'for (const cmd of ["ls", "pwd"]) await tools.exec_command({cmd});'
        found = self.commands(turn([exec_call("c1", 1, code), command("cmd1", 1.002, "ls"), command("cmd2", 1.004, "pwd"),
                                    output("c1", 3, "ok")], end=4))
        for item in ("cmd1", "cmd2"):
            with self.subTest(item=item):
                self.assertEqual(found[item]["timing"], "startup_only")
                self.assertIsNone(found[item]["durationMs"])

    def test_command_with_a_real_duration_keeps_it(self) -> None:
        made = record(6, "event_msg", {"type": "item_completed", "turn_id": TURN, "started_at_ms": ms(at(1.5)), "completed_at_ms": ms(at(6)),
                                       "item": {"type": "CommandExecution", "id": "cmd1", "command": ["make"], "duration": {"secs": 4, "nanos": 500_000_000}}})
        found = self.commands(turn([exec_call("c1", 1, 'await tools.exec_command({cmd: "make"});'), made, output("c1", 7, "ok")], end=8))
        self.assertEqual(found["cmd1"]["durationMs"], 4_500)
        self.assertEqual(found["cmd1"]["timing"], "native_interval")

    def test_cell_finishes_in_a_later_wait(self) -> None:
        lines = turn([exec_call("c1", 1, '// @exec: {"yield_time_ms": 1000}\nawait tools.exec_command({cmd: "npm run build"});'),
                      output("c1", 2, "Script running with cell ID 7"),
                      wait_call("w1", 30, "7"), output("w1", 31, "Script running with cell ID 7"),
                      wait_call("w2", 60, "7"), output("w2", 95, "Script completed\nbuild ok")], end=96)
        [job] = [event for event in self.parse(lines)["events"] if event["kind"] == "job"]
        self.assertEqual(job["timing"], "cell_pair")
        self.assertEqual((job["startMs"], job["endMs"], job["durationMs"]), (ms(at(1)), ms(at(95)), 94_000))
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["detail"]["cellId"], "7")
        self.assertFalse(job["detail"]["command"].startswith("// @exec"))

    def test_cell_no_wait_saw_finish_stays_open(self) -> None:
        lines = turn([exec_call("c1", 1, 'await tools.exec_command({cmd: "npm run dev"});'),
                      output("c1", 2, "Script running with cell ID 3"),
                      wait_call("w1", 30, "3"), output("w1", 31, "Script running with cell ID 3"),
                      wait_call("w2", 40, "9"), output("w2", 41, "Script completed")], end=50)
        [job] = [event for event in self.parse(lines)["events"] if event["kind"] == "job"]
        self.assertEqual(job["timing"], "unknown")
        self.assertIsNone(job["endMs"])
        self.assertEqual(job["status"], "no completion recorded")
