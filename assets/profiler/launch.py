#!/usr/bin/env python3
"""Refresh and serve the local session profiler. Session logs are only read, never changed."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DATA = ROOT / "data"
PROFILE = DATA / "profile.json"
LOCK = Lock()


def refresh(args):
    # Read the logs with the bundled parser; the page loads the result as data/profile.json.
    DATA.mkdir(exist_ok=True)
    temporary = PROFILE.with_suffix(".json.tmp")
    command = [sys.executable, str(ROOT / "parser/export.py"), "--output", str(temporary), "--limit", str(args.limit)]
    for sid in args.session or []:
        command.extend(["--session", sid])
    subprocess.run(command, check=True, timeout=180, capture_output=True, text=True)
    temporary.replace(PROFILE)
    profile = json.loads(PROFILE.read_text())
    print(f"Loaded {len(profile['details'])} sessions; report refreshed.", flush=True)
    return profile


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB), **kwargs)

    def allowed(self):
        host = self.headers.get("Host", "").split(":")[0]
        return host in {"127.0.0.1", "localhost"}

    def do_GET(self):
        if not self.allowed():
            self.send_error(403)
            return
        if urlsplit(self.path).path == "/api/status":
            payload = json.dumps({"tool": "session-profiler", "version": 2, "runtimePath": str(ROOT)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if urlsplit(self.path).path == "/data/profile.json":
            try:
                payload = PROFILE.read_bytes()
            except OSError:
                self.send_error(404, "The report data is missing")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if urlsplit(self.path).path.startswith(("/api/", "/data/")):
            self.send_error(404)
            return
        super().do_GET()

    def do_HEAD(self):
        if not self.allowed():
            self.send_error(403)
            return
        super().do_HEAD()

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
                current = json.loads(PROFILE.read_text())
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
    cli.add_argument("--no-refresh", "--no-build", dest="no_refresh", action="store_true", help="serve the existing report data without reading the logs again")
    args = cli.parse_args()
    if args.no_refresh and not args.session and PROFILE.exists():
        args.session = list(json.loads(PROFILE.read_text()).get("details", {})) or None
    if not args.no_refresh:
        refresh(args)
    if args.refresh_only:
        return
    if not PROFILE.exists():
        cli.error("No report data yet. Run without --no-refresh first.")
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
