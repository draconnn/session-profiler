"""Shared setup for the tests: import paths, an isolated environment and tiny JSONL writers.

Every input here is made up. Run the tests with ``python3 -m unittest discover -s tests``.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
PARSER = REPO / "assets/profiler/parser"
for folder in (PARSER, REPO / "scripts", REPO / "examples"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

T0 = datetime(2026, 1, 5, 12, 0, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def write_jsonl(path: Path, lines: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


class IsolatedTest(unittest.TestCase):
    """Runs each test in a temp dir, with every log and report location pointing into it."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="session-profiler-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = {key: value for key, value in os.environ.items()
               if key != "CLAUDE_CODE_SESSION_ID" and not key.startswith("CODEX_") and key != "SESSION_PROFILER_SOURCE"}
        env.update(CLAUDE_CONFIG_DIR=str(self.tmp / "claude"), CLAUDE_DESKTOP_SESSIONS_DIR=str(self.tmp / "desktop"),
                   SESSION_PROFILER_HOME=str(self.tmp / "reports"), CODEX_HOME=str(self.tmp / "codex"),
                   XDG_CACHE_HOME=str(self.tmp / "cache"))
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
