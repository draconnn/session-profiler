#!/usr/bin/env python3
"""Refresh and serve the local session profiler. Codex source files are read only."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"
DIST = APP / "dist"
LOCK = Lock()


def runtime():
    candidate = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
    node = str(candidate) if candidate.exists() else shutil.which("node")
    plugins = sorted((Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "plugins/cache/openai-curated-remote/data-analytics").glob("*/scripts/data-app.mjs"), key=lambda p: p.stat().st_mtime)
    if not node or not plugins:
        raise RuntimeError("Building requires Node.js and the installed Data Analytics plugin. The existing report can still be served with --no-build.")
    return node, str(plugins[-1])


def make_snapshot(profile):
    previous = json.loads((APP / "src/data.json").read_text())
    events, agents, turns = [], [], []
    files = set()
    for sid, detail in profile["details"].items():
        for a in detail["agents"]:
            agents.append({"sessionId": sid, **{k:v for k,v in a.items() if k not in {"events","messages","turns"}}})
            for e in a.get("events", []):
                if e.get("kind") not in {"tool", "wait", "compaction"}:
                    continue
                events.append({"sessionId": sid, "agentId": a["id"], "agentName": a.get("name"), **e})
                if e.get("source", {}).get("file"):
                    files.add(e["source"]["file"])
            for t in a.get("turns", []):
                turns.append({"sessionId": sid, "agentId": a["id"], **t})
    claude = any(detail.get("source") == "claude-code" for detail in profile["details"].values())
    source = {
        "label": "Local Claude Code session history" if claude else "Local Codex session history",
        "executedAt": profile["generatedAt"],
        "files": sorted(files),
        "evidenceFlow": ([
            {"title": "Read local thread metadata", "detail": "Read-only SQLite connection to the local Codex state database; thread metadata and parent-child spawn relationships."},
            {"title": "Read available session records", "detail": "Local rollout JSONL, including archived files when present. Native item timing takes precedence. Call/result pairing is a fallback where native tool items are not available. Raw reasoning and binary contents are omitted."},
            {"title": "Calculate durations", "detail": "Durations use explicit native tool duration or native start/end timestamps, with paired transcript timestamps as fallback. Active elapsed is the union of active turn intervals. Summed tool durations retain parallel overlap. Unknown durations remain null."},
        ] if not claude else [
            {"title": "Read session transcripts", "detail": "Local Claude Code transcripts under ~/.claude/projects (read only): the session JSONL plus subagents/agent-*.jsonl and their meta.json. Subagents link to the parent's Agent call through meta.toolUseId. Side sessions the desktop app started from a session link through the app's own session records (claude-code-sessions/*/*/local_*.json: cliSessionId and spawnedFrom), read only; when those records are missing or unrecognised, side sessions stay separate and a warning says so. Raw thinking and binary contents are omitted."},
            {"title": "Calculate durations", "detail": "A prompt's turn runs from the prompt to the last entry before the next prompt. A message the app injects (for example a side session reporting back) starts a wake-up turn; the idle gap before it is shown as a wait and is not active time. A tool call runs from its tool_use entry to the entry carrying its tool_result, so it includes permission-prompt waits and hooks. Subagent lifespans come from their own transcripts. Calls without a result have unknown duration."},
        ]) + [
            {"title": "Reproduce", "detail": "Run python3 launch.py --refresh-only from this report's folder, or run parser/export.py for a JSON snapshot. --session SESSION_ID selects an explicit root and all discoverable descendants."},
        ],
        "metricDefinitions": [
            {"label": "Time range", "definition": "Last recorded session or selected-turn end minus its first start. This range can include idle gaps."},
            {"label": "Active elapsed", "definition": "Union of recorded active turn intervals across selected agents. Overlaps count once; gaps between turns are excluded."},
            {"label": "Action time, summed", "definition": "Sum of known action durations in scope. Parallel calls count separately, so this is not elapsed session time. Idle waits before an app message are shown but not summed."},
            {"label": "Subagents", "definition": "Descendant threads found through recorded spawn relationships; the root is excluded. Missing local child history is disclosed in coverage."},
            {"label": "Other active time", "definition": "Agent active time less the union of its recorded action intervals. This includes inference, transport, scheduling, overhead and unrecorded activity; it is not a measurement of model inference."},
        ],
    }
    def flat(rows):
        result = []
        for row in rows:
            record = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for k, v in row.items()}
            for key, label in [("startMs", "startedAt"), ("endMs", "endedAt")]:
                if isinstance(row.get(key), (int, float)):
                    record[label] = datetime.fromtimestamp(row[key]/1000, timezone.utc).isoformat()
            result.append(record)
        return result
    return {"id": previous["id"], "surface": "dashboard", "title": "Session profiler",
            "generatedAt": profile["generatedAt"], "status": "partial", "buildStatus": "complete", "filters": [],
            "queries": {"profiler_events": {"rows": flat(events), "payloadColumns": ["detail", "source"], "source": source},
                        "profiler_agents": {"rows": flat(agents), "source": {**source, "label": "Agent tasks, relationships and active intervals"}},
                        "profiler_turns": {"rows": flat(turns), "source": {**source, "label": "Recorded prompt and turn intervals"}}},
            "profiler": profile}


def refresh(args):
    node, builder = runtime()
    data = ROOT / "data"
    data.mkdir(exist_ok=True)
    output = data / "profile.json"
    command = [sys.executable, str(ROOT / "parser/export.py"), "--output", str(output), "--limit", str(args.limit)]
    for sid in args.session or []:
        command.extend(["--session", sid])
    subprocess.run(command, check=True, timeout=180, capture_output=True, text=True)
    profile = json.loads(output.read_text())
    snapshot = make_snapshot(profile)
    target = APP / "src/data.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(target)
    result = subprocess.run([node, builder, "build", "--project-dir", str(APP), "--separate-data"], check=True, timeout=120, capture_output=True, text=True)
    metadata = json.loads(result.stdout.splitlines()[-1])
    current_snapshot = Path(metadata["snapshotPath"]).name
    for old in DIST.glob("snapshot.*.json"):
        if old.name != current_snapshot:
            old.unlink()
    print(f"Loaded {len(profile['details'])} sessions; report refreshed.", flush=True)
    return profile


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DIST), **kwargs)

    def allowed(self):
        host = self.headers.get("Host", "").split(":")[0]
        return host in {"127.0.0.1", "localhost"}

    def do_GET(self):
        if not self.allowed():
            self.send_error(403)
            return
        if urlsplit(self.path).path == "/api/status":
            payload = json.dumps({"tool": "codex-session-profiler", "version": 1, "runtimePath": str(ROOT)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if urlsplit(self.path).path.startswith("/api/"):
            self.send_error(404)
            return
        super().do_GET()

    def do_POST(self):
        origin = self.headers.get("Origin")
        expected = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
        if not self.allowed() or origin not in expected:
            self.send_error(403)
            return
        route = urlsplit(self.path).path
        if route not in {"/api/refresh", "/api/open"}:
            self.send_error(404)
            return
        if not LOCK.acquire(blocking=False):
            self.send_error(409, "A refresh is already running")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if route == "/api/open" or length:
                if not 0 < length <= 1024:
                    self.send_error(400)
                    return
                requested = json.loads(self.rfile.read(length)).get("sessionId")
                current = json.loads((APP / "src/data.json").read_text())["profiler"]
                allowed = {r["id"] for r in current.get("inventory", []) if r.get("available") and not r.get("parentId")}
                if requested not in allowed:
                    self.send_error(400, "Session is not in the local inventory")
                    return
                existing = list(current["details"])
                self.server.profiler_args.session = [requested] + [sid for sid in existing if sid != requested][:self.server.profiler_args.limit - 1]
            refresh(self.server.profiler_args)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        except Exception as exc:
            print(f"Refresh failed: {exc}", file=sys.stderr, flush=True)
            self.send_error(500, "Could not refresh the local logs; see the launcher terminal")
        finally:
            LOCK.release()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def log_message(self, fmt, *args):
        if args and str(args[0]).startswith("POST"):
            super().log_message(fmt, *args)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--session", action="append", help="root thread ID; repeat to include several roots")
    cli.add_argument("--limit", type=int, default=8, help="recent root sessions included by default")
    cli.add_argument("--port", type=int, default=8768)
    cli.add_argument("--refresh-only", action="store_true")
    cli.add_argument("--no-build", action="store_true", help="serve the existing snapshot")
    args = cli.parse_args()
    if args.no_build and not args.session and (APP / "src/data.json").exists():
        existing = json.loads((APP / "src/data.json").read_text()).get("profiler", {})
        args.session = list(existing.get("details", {})) or None
    if not args.no_build:
        refresh(args)
    if args.refresh_only:
        return
    if not (DIST / "index.html").exists():
        cli.error("The report is not built. Run without --no-build first.")
    server = None
    for port in range(args.port, args.port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if server is None:
        cli.error("No unused local port found")
    server.profiler_args = args
    print(f"Session profiler: http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
