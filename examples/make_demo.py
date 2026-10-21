#!/usr/bin/env python3
"""Write a made-up Claude Code session to profile, for the README screenshot and for trying the tool.

The session ("Add CSV export to the orders page") has two subagents, a background test run,
a question that waits for the user, a side session started from the desktop app and a
follow-up prompt. Nothing in it comes from a real session.

    python3 examples/make_demo.py /tmp/sp-demo
    CLAUDE_CONFIG_DIR=/tmp/sp-demo/claude CLAUDE_DESKTOP_SESSIONS_DIR=/tmp/sp-demo/desktop \\
      SESSION_PROFILER_HOME=/tmp/sp-demo/reports \\
      python3 scripts/profile_session.py --source claude --session 3f2c9a10-5b7e-4c1d-9a2e-6d8b1f0c4e21
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAIN = "3f2c9a10-5b7e-4c1d-9a2e-6d8b1f0c4e21"
SIDE = "8a71d2e4-0c39-4f5b-b6a8-2e9d47c1f053"
PROJECT = "-Users-demo-acme-shop"
MODEL = "claude-opus-5-5"
START = datetime(2026, 10, 20, 9, 0, 0, tzinfo=timezone.utc)


def stamp(at: datetime) -> str:
    # Millisecond precision, as in real Claude Code transcripts.
    return at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"


class Transcript:
    """Builds one transcript: prompts, assistant tool calls with their results, notices."""

    def __init__(self, session: str, sidechain: bool = False, agent: str | None = None):
        self.session, self.sidechain, self.agent = session, sidechain, agent
        self.lines: list[dict] = []
        self.calls = 0

    def _entry(self, kind: str, at: datetime, **fields) -> None:
        entry = {"type": kind, "timestamp": stamp(at), "sessionId": self.session,
                 "isSidechain": self.sidechain, **fields}
        if self.agent:
            entry["agentId"] = self.agent
        self.lines.append(entry)

    def prompt(self, at: datetime, text: str) -> None:
        self._entry("user", at, message={"role": "user", "content": text})

    def app_message(self, at: datetime, text: str) -> None:
        self._entry("user", at, message={"role": "user", "content": [{"type": "text", "text": text}]})

    def say(self, at: datetime, text: str) -> None:
        self._entry("assistant", at, message={"role": "assistant", "model": MODEL, "content": [{"type": "text", "text": text}]})

    def call(self, at: datetime, seconds: float, name: str, tool_input: dict, result: str = "ok", call_id: str | None = None) -> str:
        self.calls += 1
        call_id = call_id or f"toolu_{self.session[:4]}{self.agent or ''}{self.calls:03d}"
        self._entry("assistant", at, message={"role": "assistant", "model": MODEL,
                                              "content": [{"type": "tool_use", "id": call_id, "name": name, "input": tool_input}]})
        self._entry("user", at + timedelta(seconds=seconds),
                    message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": result}]})
        return call_id

    def notice(self, at: datetime, call_id: str, task: str, status: str, summary: str) -> None:
        text = (f"<task-notification><task-id>{task}</task-id><tool-use-id>{call_id}</tool-use-id>"
                f"<status>{status}</status><summary>{summary}</summary></task-notification>")
        self.lines.append({"type": "queue-operation", "operation": "enqueue", "timestamp": stamp(at),
                           "sessionId": self.session, "content": text})

    def title(self, text: str) -> None:
        self.lines.append({"type": "custom-title", "customTitle": text, "sessionId": self.session})

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(line, separators=(",", ":")) + "\n" for line in self.lines), encoding="utf-8")


def t(minutes: float) -> datetime:
    return START + timedelta(minutes=minutes)


def main(out: Path) -> None:
    project = out / "claude" / "projects" / PROJECT
    main = Transcript(MAIN)
    main.title("Add CSV export to the orders page")

    # Prompt 1: explore, ask, implement, test in the background, review, build.
    main.prompt(t(0), "Add a CSV export button to the orders page. It should use the current table filters, "
                      "and big exports must not time out.")
    main.say(t(0.1), "I'll look at how the orders table loads its data first.")
    explore = main.call(t(0.15), 128, "Agent", {"description": "Find how the orders table loads data", "subagent_type": "Explore",
                                                 "prompt": "Find where the orders page fetches and filters its rows, and any existing export helpers."},
                        "The orders page fetches through src/api/orders.ts (listOrders with filter params). No export helper exists yet.")
    main.call(t(2.4), 0.6, "Read", {"file_path": "src/pages/orders/OrdersPage.tsx"})
    main.call(t(2.6), 0.4, "Read", {"file_path": "src/api/orders.ts"})
    main.call(t(2.9), 236, "AskUserQuestion", {"questions": [{"question": "Which columns should the export include?"}]},
              "User answered: all visible columns plus the customer email.")
    main.call(t(7.0), 0.5, "Edit", {"file_path": "src/api/orders.ts", "description": "Stream orders page by page"})
    main.call(t(7.4), 0.4, "Write", {"file_path": "src/api/exportCsv.ts"})
    main.call(t(7.9), 0.5, "Edit", {"file_path": "src/pages/orders/OrdersPage.tsx", "description": "Add the Export CSV button"})
    tests = main.call(t(8.3), 0.8, "Bash", {"command": "npm test -- --runInBand", "description": "Run the full test suite", "run_in_background": True},
                      "Command running in background with ID: b7k2")
    main.call(t(8.6), 35, "Bash", {"command": "npx tsc --noEmit", "description": "Type-check the project"})
    review = main.call(t(9.4), 152, "Agent", {"description": "Review the CSV export change", "subagent_type": "code-reviewer",
                                               "prompt": "Review the CSV export change for correctness and large-export behaviour."},
                       "One issue: the export buffers every row in memory. Stream it instead.")
    main.call(t(12.0), 0.6, "Edit", {"file_path": "src/api/exportCsv.ts", "description": "Stream rows instead of buffering"})
    main.notice(t(12.6), tests, "b7k2", "completed", 'Background command "Run the full test suite" completed (exit code 0)')
    main.call(t(12.8), 92, "Bash", {"command": "npm run build", "description": "Build the app"})
    main.say(t(14.4), "The orders page now has an Export CSV button that streams filtered rows. Tests and build pass. "
                      "I started a side session to write the release notes.")

    # Side session (desktop app): release notes; the main session waits for its report.
    side = Transcript(SIDE)
    side.prompt(t(14.8), "Write release notes for the new CSV export on the orders page.")
    side.call(t(15.0), 1.2, "Bash", {"command": "git log --oneline -5", "description": "See the latest commits"})
    side.call(t(15.4), 0.5, "Read", {"file_path": "CHANGELOG.md"})
    side.call(t(16.6), 0.4, "Edit", {"file_path": "CHANGELOG.md", "description": "Add the CSV export entry"})
    side.say(t(17.3), "Release notes added to CHANGELOG.md.")
    main.app_message(t(17.6), '<system-reminder>The session "Write release notes" you started finished a turn.</system-reminder>'
                              "<side-sessions-event>None of the sessions you started is working now.</side-sessions-event>")
    main.call(t(17.8), 0.4, "Read", {"file_path": "CHANGELOG.md"})
    main.say(t(18.2), "The release notes are in CHANGELOG.md.")

    # Prompt 2 after a break.
    main.prompt(t(26.0), "Looks good. Add the same export to the invoices page.")
    main.call(t(26.2), 0.5, "Read", {"file_path": "src/pages/invoices/InvoicesPage.tsx"})
    main.call(t(26.6), 0.5, "Edit", {"file_path": "src/pages/invoices/InvoicesPage.tsx", "description": "Reuse exportCsv for invoices"})
    main.call(t(27.0), 118, "Bash", {"command": "npm test -- invoices", "description": "Run the invoice tests"})
    main.call(t(29.2), 0.4, "Edit", {"file_path": "src/pages/invoices/InvoicesPage.tsx", "description": "Fix the date column format"})
    main.call(t(29.5), 64, "Bash", {"command": "npm test -- invoices", "description": "Rerun the invoice tests"})
    main.say(t(30.8), "The invoices page has the same export; its tests pass.")
    main.write(project / f"{MAIN}.jsonl")
    side.write(project / f"{SIDE}.jsonl")

    # Subagents of the main session.
    subagents = project / MAIN / "subagents"
    explorer = Transcript(MAIN, sidechain=True, agent="a1e5")
    explorer.prompt(t(0.17), "Find where the orders page fetches and filters its rows, and any existing export helpers.")
    explorer.call(t(0.3), 2.1, "Grep", {"pattern": "listOrders", "path": "src"})
    explorer.call(t(0.6), 0.5, "Read", {"file_path": "src/api/orders.ts"})
    explorer.call(t(0.9), 1.8, "Grep", {"pattern": "export|csv", "path": "src"})
    explorer.call(t(1.3), 0.6, "Read", {"file_path": "src/pages/orders/OrdersPage.tsx"})
    explorer.say(t(2.2), "Orders load through listOrders with filter params; there is no export helper yet.")
    explorer.write(subagents / "agent-a1e5.jsonl")
    (subagents / "agent-a1e5.meta.json").write_text(json.dumps({"agentType": "Explore", "description": "Find how the orders table loads data",
                                                                "toolUseId": explore, "model": "claude-haiku-5-5"}))
    reviewer = Transcript(MAIN, sidechain=True, agent="b9c3")
    reviewer.prompt(t(9.45), "Review the CSV export change for correctness and large-export behaviour.")
    reviewer.call(t(9.6), 0.6, "Read", {"file_path": "src/api/exportCsv.ts"})
    reviewer.call(t(9.9), 0.5, "Read", {"file_path": "src/api/orders.ts"})
    reviewer.call(t(10.3), 41, "Bash", {"command": "npm test -- export", "description": "Run the export tests"})
    reviewer.say(t(11.8), "One issue: the export buffers every row in memory. Stream it instead.")
    reviewer.write(subagents / "agent-b9c3.jsonl")
    (subagents / "agent-b9c3.meta.json").write_text(json.dumps({"agentType": "code-reviewer", "description": "Review the CSV export change",
                                                                "toolUseId": review, "model": MODEL}))

    # The desktop app's records that link the side session to the main one.
    records = out / "desktop" / "demo-account" / "demo-workspace"
    records.mkdir(parents=True, exist_ok=True)
    (records / "local_main.json").write_text(json.dumps({"sessionId": "local_main", "cliSessionId": MAIN,
                                                         "title": "Add CSV export to the orders page"}))
    (records / "local_side.json").write_text(json.dumps({"sessionId": "local_side", "cliSessionId": SIDE,
                                                         "title": "Write release notes", "spawnedFrom": {"sessionId": "local_main"}}))
    print(f"Demo session {MAIN} written to {out}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: make_demo.py OUTPUT_DIR")
    main(Path(sys.argv[1]).expanduser().resolve())
