"""Read-only parser for Claude Code session transcripts.

Claude Code stores each session at
``~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`` and its subagents at
``<session-id>/subagents/agent-<id>.jsonl`` with an ``agent-<id>.meta.json``
beside each.  This module maps them into the same detail shape that
``parser.analyze_session`` returns for Codex threads.

Timing rules follow cc_timing.py: only ``user``/``assistant`` entries with a
timestamp count (main transcripts skip ``isSidechain`` entries), sorted by
timestamp.  A prompt is a user entry without ``tool_result`` blocks whose text
does not start with ``<``; its turn runs to the last entry before the next
prompt.  A message the app injects (text starting with ``<system-reminder>`` or
containing ``<side-sessions-event>`` or ``<cross-session-message``, e.g. a side
session reporting back or another session's message) also
ends the turn: the idle gap before it becomes a ``wait`` event and the message
starts a wake-up turn (``kind: "wake"``), so idle waits are not active time.
A tool call runs from its ``tool_use`` entry to the first entry
carrying its ``tool_result``, so it includes permission-prompt waits and hooks.
A Bash command run in the background (``run_in_background``, or moved there
after its timeout) returns its ``tool_result`` at once; the work itself is a
``job`` event from the ``tool_use`` to the ``<task-notification>`` that names
its ``tool-use-id`` (completed, failed, killed or stopped). A job with no
notification stays open (unknown end), and so does a killed or stopped one whose notice
is not when the work ended: it says the job outlived its session, or it gives no reason
and came more than a minute after the transcript's previous entry (the app closing it
while the session was quiet, possibly days later). Background subagents are not jobs: they are timed
from their own transcripts.

Side sessions the desktop app started from a session (start_session) are
separate transcripts with no link inside them; ``side_session_links`` reads the
link from the app's own session records, and they are included as descendant
agents with their own turns, tools, waits and subagents.
"""

from __future__ import annotations

import bisect
import json
import os
import re
from pathlib import Path
from typing import Any, Iterator, Mapping

if __package__:
    from .parser import MAX_DETAIL_CHARS, MAX_MESSAGE_CHARS, MAX_TASK_CHARS, _clip, _parse_iso_ms, _safe_value, _source, _span, _text_from_content, _union_duration, redact_text
else:
    from parser import MAX_DETAIL_CHARS, MAX_MESSAGE_CHARS, MAX_TASK_CHARS, _clip, _parse_iso_ms, _safe_value, _source, _span, _text_from_content, _union_duration, redact_text

SOURCE = "claude-code"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
# The tool_result's own opening words, never text a command printed.
_BACKGROUND = re.compile(r"\s*(?:Command running in background with ID: (\w+)|Command did not complete within its \d+\w* timeout and was moved to the background \(ID: (\w+)\))")
_NOTICE = re.compile(r"<task-notification>(.*?)</task-notification>", re.S)
# When a killed or stopped job's notice is not when its work ended:
# - the notice says the job outlived its session (sent when a later session starts);
# - or it gives no reason and came more than a minute after the transcript's previous
#   entry (the app closing it while the session was quiet, possibly days later).
# A notice that names who or what stopped it (the user, the main session, Claude, the
# background time limit) is a real end.
_CLEANUP_STATES = {"killed", "stopped"}
_OUTLIVED_SESSION = re.compile(r"didn't finish before the previous session ended|No completion record was found", re.I)
_NAMED_STOP = re.compile(r"stopped by (?:the )?(?:user|main session|Claude)|background time limit", re.I)
_CLEANUP_GRACE_MS = 60_000
_SUMMARY_KEYS = ("description", "command", "file_path", "pattern", "url", "query", "prompt", "action")


def claude_home() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


def _transcripts(home: Path | None = None) -> Iterator[Path]:
    root = (home or claude_home()) / "projects"
    try:
        projects = list(root.iterdir())
    except OSError:
        return
    for project in projects:
        try:
            for path in project.glob("*.jsonl"):
                if _UUID.match(path.stem):
                    yield path
        except OSError:
            continue


def _prompt_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "text" and not str(block.get("text", "")).lstrip().startswith("<system-reminder>"):
                return block.get("text")
    return None


def _real_prompt(entry: Mapping[str, Any]) -> str | None:
    """Return the prompt text when this user entry is a typed prompt."""

    if entry.get("type") != "user" or entry.get("isMeta"):
        return None
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list) and any(isinstance(c, Mapping) and c.get("type") == "tool_result" for c in content):
        return None
    text = _prompt_text(content)
    return text if text and not text.startswith("<") else None


def _app_message(entry: Mapping[str, Any]) -> str | None:
    """Readable text when this user entry is a message the app injected (a wake-up).

    Messages from another session (SendMessage) carry ``<cross-session-message``;
    the app may record them as ``isMeta`` entries, so that marker is accepted there too.
    """

    if entry.get("type") != "user":
        return None
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list):
        if any(isinstance(c, Mapping) and c.get("type") == "tool_result" for c in content):
            return None
        content = "\n".join(str(c.get("text") or "") for c in content if isinstance(c, Mapping) and c.get("type") == "text")
    if not isinstance(content, str):
        return None
    text = content.lstrip()
    if "<cross-session-message" not in text and (entry.get("isMeta") or not (text.startswith("<system-reminder>") or "<side-sessions-event>" in text)):
        return None
    # Prefer what follows the reminders (a side session's brief, a message from another session).
    rest = re.sub(r"<system-reminder>.*?</system-reminder>", " ", text, flags=re.S)
    readable = rest if re.sub(r"<[^>]*>", "", rest).strip() else text
    return " ".join(re.sub(r"</?[\w-]+[^>]*>", " ", readable).split()) or "app message"


def _scan_head(path: Path, max_lines: int = 400) -> tuple[int | None, str]:
    """First timestamp and first typed prompt, reading only the file's start."""

    first_ms, prompt = None, ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for number, line in enumerate(stream):
                if number >= max_lines or (first_ms and prompt):
                    break
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, Mapping) or entry.get("isSidechain"):
                    continue
                if first_ms is None and entry.get("type") in ("user", "assistant"):
                    first_ms = _parse_iso_ms(entry.get("timestamp"))
                prompt = prompt or (_real_prompt(entry) or "")
    except OSError:
        pass
    return first_ms, prompt


def _scan_tail(path: Path, size: int = 262_144) -> dict[str, str]:
    """Latest custom-title / last-prompt values from the end of the file."""

    found: dict[str, str] = {}
    try:
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - size))
            lines = stream.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return found
    for line in lines:
        if '"custom-title"' not in line and '"last-prompt"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") == "custom-title" and entry.get("customTitle"):
            found["customTitle"] = entry["customTitle"]
        elif entry.get("type") == "last-prompt" and entry.get("lastPrompt"):
            found["lastPrompt"] = entry["lastPrompt"]
    return found


def _title(custom: str, first_prompt: str, last_prompt: str, fallback: str) -> str:
    text = custom or first_prompt or last_prompt
    return redact_text(" ".join(text.split())[:200], 400) if text else fallback


def desktop_sessions_dir() -> Path:
    configured = os.environ.get("CLAUDE_DESKTOP_SESSIONS_DIR")
    return Path(configured).expanduser() if configured else Path.home() / "Library/Application Support/Claude/claude-code-sessions"


def side_session_links(directory: Path | None = None) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Side sessions started from another session, from the desktop app's own records.

    Each ``<dir>/*/*/local_<id>.json`` names its transcript (``cliSessionId``) and,
    for a started session, its parent's ``local_`` id (``spawnedFrom.sessionId``).
    Returns ``{child transcript id: {"parentId", "title", "file"}}`` and warnings.
    The files are undocumented and version-dependent, so anything missing or
    unexpected is skipped and reported instead of failing.
    """

    directory = directory or desktop_sessions_dir()
    try:
        files = sorted(directory.glob("*/*/local_*.json")) if directory.is_dir() else []
    except OSError as exc:
        return {}, [f"could not read desktop app session records ({exc}); side sessions are not linked"]
    if not files:
        return {}, [f"no desktop app session records in {directory}; side sessions are not linked"]
    transcript_of: dict[str, str] = {}
    spawned: list[tuple[str, str, str, str]] = []
    unreadable = 0
    for file in files:
        try:
            record = json.loads(file.read_text(encoding="utf-8"))
            local, cli = record.get("sessionId"), record.get("cliSessionId")
        except (OSError, ValueError, AttributeError):
            unreadable += 1
            continue
        if not isinstance(local, str) or not isinstance(cli, str) or not _UUID.match(cli):
            continue  # not started yet, or a shape this reader does not know
        transcript_of[local] = cli.lower()
        parent = record.get("spawnedFrom")
        if isinstance(parent, Mapping) and isinstance(parent.get("sessionId"), str):
            spawned.append((cli.lower(), parent["sessionId"], str(record.get("title") or "").strip(), str(file)))
    warnings = [f"{unreadable} desktop app session record(s) unreadable; skipped"] if unreadable else []
    if not transcript_of:
        return {}, warnings + ["desktop app session records have an unknown shape; side sessions are not linked"]
    links = {child: {"parentId": transcript_of[parent], "title": title, "file": file}
             for child, parent, title, file in spawned if transcript_of.get(parent) not in (None, child)}
    for child in list(links):  # a cycle would leave the tree without a root
        seen, current = {child}, links[child]["parentId"]
        while current in links and current not in seen:
            seen.add(current)
            current = links[current]["parentId"]
        if current in seen:
            del links[child]
            warnings.append(f"{child}: cyclic side-session parent records; shown as a top-level session")
    return links, warnings


def claude_inventory(home: Path | None = None) -> list[dict[str, Any]]:
    """One inventory row per Claude Code session transcript; side sessions carry their parent's id."""

    links, _warnings = side_session_links()
    rows = []
    for path in _transcripts(home):
        try:
            stat = path.stat()
        except OSError:
            continue
        created_ms, first_prompt = _scan_head(path)
        tail = _scan_tail(path)
        subagents = path.with_suffix("") / "subagents"
        child_count = len(list(subagents.glob("agent-*.jsonl"))) if subagents.is_dir() else 0
        title = _title(tail.get("customTitle", ""), first_prompt, tail.get("lastPrompt", ""), path.stem)
        rows.append({
            "id": path.stem.lower(),
            "title": title,
            "displayTitle": title,
            "source": SOURCE,
            "rolloutPath": str(path),
            "path": str(path),
            "available": True,
            "createdAtMs": created_ms,
            "updatedAtMs": int(stat.st_mtime * 1000),
            "archived": False,
            "agentPath": "",
            "agentNickname": "",
            "agentRole": "",
            "model": "",
            "firstUserMessage": redact_text(first_prompt, MAX_TASK_CHARS),
            "parentId": None,
            "childCount": child_count,
            "project": path.parent.name,
        })
    by_id = {row["id"]: row for row in rows}
    for row in rows:
        parent = by_id.get(links.get(row["id"], {}).get("parentId"))
        if parent:
            row["parentId"] = parent["id"]
            parent["childCount"] += 1
    return rows


def _entries(path: Path, sidechain: bool) -> tuple[list[tuple[int, dict[str, Any]]], list[str], dict[str, str], dict[str, tuple[int, int, str]], set[int]]:
    """Timed user/assistant entries (with line numbers), warnings, title records,
    background-task notifications by tool-use id: (ms, line, notice text), and the
    lines that carry a notification.
    A notification is timed when the app queued it (``queue-operation``
    ``enqueue``), which is when the task ended; it may reach the transcript as a
    message much later, or only inside another entry."""

    entries, warnings, titles, notices, notice_lines = [], [], {}, {}, set()
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for number, line in enumerate(stream, 1):
                try:
                    entry = json.loads(line)
                except ValueError:
                    warnings.append(f"line {number}: malformed JSON")
                    continue
                if not isinstance(entry, dict):
                    continue
                kind = entry.get("type")
                if kind == "custom-title" and entry.get("customTitle"):
                    titles["customTitle"] = entry["customTitle"]
                elif kind == "last-prompt" and entry.get("lastPrompt"):
                    titles["lastPrompt"] = entry["lastPrompt"]
                if kind in ("user", "assistant") and entry.get("timestamp") and (sidechain or not entry.get("isSidechain")):
                    entries.append((number, entry))
                if kind in ("queue-operation", "user", "attachment") and entry.get("timestamp") and "<task-notification>" in line:
                    # Queued for the main session, delivered as a message, or
                    # (in a subagent's transcript) attached as a queued command.
                    queued = entry.get("operation") == "enqueue"
                    if kind == "queue-operation" and not queued:
                        continue
                    attached = entry.get("attachment") if isinstance(entry.get("attachment"), Mapping) else {}
                    text = (entry.get("content") if queued else attached.get("prompt") if kind == "attachment"
                            else _text_from_content((entry.get("message") or {}).get("content")))
                    found = _NOTICE.findall(text if isinstance(text, str) else "")
                    content = (entry.get("message") or {}).get("content")
                    if found and not (isinstance(content, list) and any(isinstance(c, Mapping) and c.get("type") == "tool_result" for c in content)):
                        notice_lines.add(number)  # a notice entry, not a tool result that printed one
                    for notice in found:
                        use = re.search(r"<tool-use-id>([^<]+)</tool-use-id>", notice)
                        at = _parse_iso_ms(entry["timestamp"])
                        if use and at is not None:
                            known = notices.get(use.group(1).strip())
                            if known is None or at < known[0]:
                                notices[use.group(1).strip()] = (at, number, notice)
    except OSError as exc:
        warnings.append(f"could not read transcript: {exc}")
    entries.sort(key=lambda pair: pair[1]["timestamp"])
    return entries, warnings, titles, notices, notice_lines


def _input_summary(tool_input: Any) -> str:
    if isinstance(tool_input, Mapping):
        for key in _SUMMARY_KEYS:
            if isinstance(tool_input.get(key), str):
                return " ".join(tool_input[key].split())
    return ""


def _parse_transcript(path: Path, sidechain: bool) -> dict[str, Any]:
    entries, warnings, titles, notices, notice_lines = _entries(path, sidechain)
    turns: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    pending: dict[str, dict[str, Any]] = {}
    tool_inputs: dict[str, Any] = {}
    jobs: dict[str, dict[str, Any]] = {}
    for line, entry in entries:
        at_ms = _parse_iso_ms(entry["timestamp"])
        content = (entry.get("message") or {}).get("content")
        blocks = content if isinstance(content, list) else []
        turn_id = turns[-1]["id"] if turns else None
        if entry["type"] == "assistant":
            for block in blocks:
                if not isinstance(block, Mapping):
                    continue
                if block.get("type") == "tool_use" and block.get("id"):
                    name = str(block.get("name") or "tool")
                    tool_input = block.get("input")
                    tool_inputs[block["id"]] = tool_input
                    command = tool_input.get("command") if isinstance(tool_input, Mapping) and isinstance(tool_input.get("command"), str) else None
                    pending[block["id"]] = {
                        "id": block["id"],
                        "kind": "tool",
                        "label": _clip(name, 300),
                        "startMs": at_ms,
                        "endMs": None,
                        "durationMs": None,
                        "timing": "unknown",
                        "status": "no result recorded",
                        "summary": _clip(_input_summary(tool_input) or name, MAX_DETAIL_CHARS),
                        "detail": {"name": _clip(name, 240), "command": redact_text(command) if command else None, "input": _safe_value(tool_input, key="input")},
                        "source": _source(path, line, "claude:assistant:tool_use"),
                        "turnId": turn_id,
                    }
                elif block.get("type") == "text" and str(block.get("text") or "").strip():
                    messages.append({"id": f"{entry.get('uuid')}:{len(messages)}", "role": "assistant", "atMs": at_ms,
                                     "text": redact_text(block["text"], MAX_MESSAGE_CHARS), "phase": None,
                                     "source": _source(path, line, "claude:assistant:text")})
        else:
            results = [b for b in blocks if isinstance(b, Mapping) and b.get("type") == "tool_result"]
            for result in results:
                event = pending.pop(result.get("tool_use_id"), None)
                if event is None:
                    continue
                event.update(endMs=at_ms, durationMs=at_ms - event["startMs"], timing="transcript_pair",
                             status="error" if result.get("is_error") else "completed")
                tool_input = tool_inputs.get(event["id"])
                file_path = tool_input.get("file_path") if isinstance(tool_input, Mapping) else None
                if isinstance(file_path, str) and re.search(r"(?:AGENTS|CLAUDE)\.md$", file_path, re.I):
                    event["detail"]["output"] = "[base instructions omitted]"
                else:
                    event["detail"]["output"] = _clip(_text_from_content(result.get("content")), MAX_DETAIL_CHARS)
                events.append(event)
                started = _BACKGROUND.match(event["detail"]["output"] or "")
                if started and event["label"] == "Bash":
                    # The call returned; the command keeps running until its notification.
                    jobs[event["id"]] = {
                        **event, "id": f"job:{event['id']}", "kind": "job", "label": "Bash · background job",
                        "endMs": None, "durationMs": None, "timing": "unknown", "status": "no completion recorded",
                        "detail": {**event["detail"], "taskId": started.group(1) or started.group(2), "output": None},
                    }
            prompt = None if results else _real_prompt(entry)
            wake = None if results or prompt else _app_message(entry)
            if wake:
                # The app woke the session (or, for a side session, started it): the gap since
                # the previous turn is an idle wait, not active time.
                turn_id = str(entry.get("uuid") or f"line-{line}")
                if turns:
                    idle_from = turns[-1]["endMs"]
                    events.append({
                        "id": f"wake-wait:{turn_id}",
                        "kind": "wait",
                        "label": "Idle until app message",
                        "startMs": idle_from,
                        "endMs": at_ms,
                        "durationMs": at_ms - idle_from,
                        "timing": "transcript_gap",
                        "idle": True,
                        "status": "completed",
                        "summary": _clip(wake, MAX_DETAIL_CHARS),
                        "detail": {"name": "app message", "command": None, "input": None, "output": redact_text(wake, MAX_DETAIL_CHARS)},
                        "source": _source(path, line, "claude:user:app_message"),
                        "turnId": turn_id,
                    })
                turns.append({"id": turn_id, "startMs": at_ms, "endMs": at_ms, "status": "observed", "kind": "wake",
                              "prompt": redact_text(f"Wake-up: {wake}", MAX_TASK_CHARS), "rootTurnId": None})
            if prompt:
                turn_id = str(entry.get("uuid") or f"line-{line}")
                turns.append({"id": turn_id, "startMs": at_ms, "endMs": at_ms, "status": "observed",
                              "prompt": redact_text(prompt, MAX_TASK_CHARS), "rootTurnId": None})
                messages.append({"id": turn_id, "role": "user", "atMs": at_ms, "text": redact_text(prompt, MAX_MESSAGE_CHARS),
                                 "phase": "prompt", "source": _source(path, line, "claude:user:prompt")})
        if turns:
            turns[-1]["endMs"] = at_ms
    events.extend(pending.values())
    # Notice entries (in a subagent's transcript) are not activity: another notice right
    # after one must still count as coming while the session was quiet.
    entry_times = sorted(at for at in (_parse_iso_ms(entry["timestamp"]) for line, entry in entries if line not in notice_lines) if at is not None)
    for tool_use_id, job in jobs.items():
        if tool_use_id in notices:
            at_ms, notice_line, notice = notices[tool_use_id]
            state = re.search(r"<status>([^<]+)</status>", notice)
            said = re.search(r"<summary>(.*?)</summary>", notice, re.S)
            status = state.group(1).strip() if state else "completed"
            job["source"] = {**job["source"], "endLine": notice_line}
            job["detail"]["output"] = _clip(said.group(1).strip() if said else notice, MAX_DETAIL_CHARS)
            reason = said.group(1) if said else notice
            before = bisect.bisect_left(entry_times, at_ms)
            quiet = before > 0 and at_ms - entry_times[before - 1] > _CLEANUP_GRACE_MS
            if status in _CLEANUP_STATES and _OUTLIVED_SESSION.search(reason):
                job["status"] = f"{status}: did not finish before its session ended; real end unknown"
            elif status in _CLEANUP_STATES and quiet and not _NAMED_STOP.search(reason):
                job["status"] = f"{status} while the session was quiet; real end unknown"
            else:
                job.update(endMs=at_ms, durationMs=max(0, at_ms - job["startMs"]), timing="notification_pair", status=status)
        events.append(job)
    events.sort(key=lambda event: (event["startMs"] is None, event["startMs"] or 0))
    return {"turns": turns, "events": events, "messages": messages, "warnings": warnings, "titles": titles, "toolInputs": tool_inputs}


def _agent(agent_id: str, parent_id: str | None, name: str, task: str, model: str | None, parsed: Mapping[str, Any]) -> dict[str, Any]:
    turns, events = parsed["turns"], parsed["events"]
    intervals = [(turn["startMs"], turn["endMs"]) for turn in turns]
    start_ms, end_ms = _span(intervals + [(event["startMs"], event["endMs"]) for event in events])
    active_ms = _union_duration(intervals)
    wall_ms = end_ms - start_ms if start_ms is not None and end_ms is not None else None
    idle_ms = max(0, wall_ms - active_ms) if wall_ms is not None else None
    warnings = list(parsed["warnings"])
    if idle_ms and len(turns) > 1:
        warnings.append(f"lifespan includes {idle_ms}ms outside active turn spans; activeMs excludes idle waits")
    unpaired = sum(event["durationMs"] is None for event in events)
    if unpaired:
        warnings.append(f"{unpaired} tool call(s) without a recorded result; duration unknown")
    tools = [event for event in events if event["kind"] == "tool"]
    return {
        "id": agent_id,
        "parentId": parent_id,
        "name": name,
        "task": task,
        "taskAvailability": "available" if task else "unavailable",
        "taskSource": None,
        "model": model,
        "status": "missing" if not turns else "observed",
        "startMs": start_ms,
        "endMs": end_ms,
        "activeMs": active_ms,
        "wallDurationMs": wall_ms,
        "idleMs": idle_ms,
        "toolCount": len(tools),
        "toolTimeMs": sum(event["durationMs"] or 0 for event in tools),
        "toolBusyMs": _union_duration((event["startMs"], event["endMs"]) for event in tools),
        "turns": turns,
        "events": events,
        "messages": sorted(parsed["messages"], key=lambda message: message["atMs"] or 0),
        "warnings": warnings,
    }


def _transcript_model(path: Path) -> str | None:
    """Most recent assistant model id in a transcript, read from its tail."""

    try:
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 262_144))
            tail = stream.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    models = re.findall(r'"model":"(claude-[^"]+)"', tail)
    return models[-1] if models else None


def _session_agents(session_id: str, path: Path, parent_id: str | None, name: str | None) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    """The session's own agent first, then its Agent-tool subagents; plus skip warnings and the parsed root."""

    root = _parse_transcript(path, sidechain=False)
    first_prompt = next((turn["prompt"].removeprefix("Wake-up: ") for turn in root["turns"]), "")
    if name is None:
        name = "Claude Code"
    elif not name:
        name = _title(root["titles"].get("customTitle", ""), first_prompt, root["titles"].get("lastPrompt", ""), session_id)
    agents = [_agent(session_id, parent_id, name, first_prompt, _transcript_model(path), root)]
    owner_of_call = {call_id: session_id for call_id in root["toolInputs"]}
    inputs = dict(root["toolInputs"])
    children = []
    for child_path in sorted((path.with_suffix("") / "subagents").glob("agent-*.jsonl")):
        try:
            meta = json.loads(child_path.with_suffix(".meta.json").read_text())
        except (OSError, ValueError):
            meta = {}
        parsed = _parse_transcript(child_path, sidechain=True)
        agent_id = child_path.stem.removeprefix("agent-")
        for call_id in parsed["toolInputs"]:
            owner_of_call[call_id] = agent_id
        inputs.update(parsed["toolInputs"])
        children.append((agent_id, meta if isinstance(meta, Mapping) else {}, parsed))
    warnings: list[str] = []
    for agent_id, meta, parsed in children:
        if not parsed["turns"]:
            warnings.append(f"{agent_id}: subagent transcript has no prompt; skipped")
            continue
        call_id = meta.get("toolUseId")
        owner = owner_of_call.get(call_id, session_id)
        spawn_input = inputs.get(call_id) if call_id else None
        task = redact_text(spawn_input.get("prompt"), MAX_TASK_CHARS) if isinstance(spawn_input, Mapping) and spawn_input.get("prompt") else parsed["turns"][0]["prompt"]
        description = str(meta.get("description") or "").strip()
        agent_type = str(meta.get("agentType") or "").strip()
        label = redact_text(" · ".join(part for part in (agent_type, description) if part) or agent_id, 300)
        agent = _agent(agent_id, owner, label, task, meta.get("model") or None, parsed)
        if call_id:
            agent["taskSource"] = {"file": str(path), "toolUseId": call_id, "type": "claude:assistant:tool_use:Agent"}
        agents.append(agent)
    return agents, warnings, root


def analyze_claude_session(session_id: str, path: Path | str) -> dict[str, Any]:
    """Parse one Claude Code session, its subagents, and every side session started from it, recursively."""

    path = Path(path)
    agents, warnings, root = _session_agents(session_id, path, None, None)
    first_prompt = agents[0]["task"]
    links, link_warnings = side_session_links()
    children_of: dict[str, list[str]] = {}
    for child, link in sorted(links.items()):
        children_of.setdefault(link["parentId"], []).append(child)
    transcripts = {p.stem.lower(): p for p in _transcripts()} if session_id in children_of else {}
    queue, seen = [session_id], {session_id}
    while queue:
        current = queue.pop(0)
        for child in children_of.get(current, []):
            if child in seen:
                continue
            seen.add(child)
            child_path = transcripts.get(child)
            if child_path is None:
                warnings.append(f"{child}: side session transcript not found locally; skipped with its descendants")
                continue
            child_agents, child_warnings, _parsed = _session_agents(child, child_path, current, redact_text(links[child]["title"], 300))
            child_agents[0]["taskSource"] = {"file": links[child]["file"], "type": "claude-desktop:spawnedFrom"}
            agents.extend(child_agents)
            warnings.extend(child_warnings)
            queue.append(child)
    warnings.extend(link_warnings)
    agents[1:] = sorted(agents[1:], key=lambda agent: agent["startMs"] or 0)
    for agent in agents:
        warnings.extend(f"{agent['id']}: {warning}" for warning in agent["warnings"])
    titles = root["titles"]
    all_turns = [(turn["startMs"], turn["endMs"]) for agent in agents for turn in agent["turns"]]
    start_ms, end_ms = _span([(agent["startMs"], agent["endMs"]) for agent in agents])
    return {
        "id": session_id,
        "source": SOURCE,
        "title": _title(titles.get("customTitle", ""), first_prompt, titles.get("lastPrompt", ""), session_id),
        "startMs": start_ms,
        "endMs": end_ms,
        "activeMs": _union_duration(all_turns),
        "rootActiveMs": agents[0]["activeMs"],
        "wallDurationMs": end_ms - start_ms if start_ms is not None and end_ms is not None else None,
        "turnIntervals": [
            {"agentId": agent["id"], "startMs": turn["startMs"], "endMs": turn["endMs"], "status": turn["status"]}
            for agent in agents for turn in agent["turns"]
        ],
        "agents": agents,
        "warnings": warnings,
    }
