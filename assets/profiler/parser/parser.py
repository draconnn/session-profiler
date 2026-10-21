"""Read-only parser for Codex session JSONL logs (Claude Code: see claude.py).

The module deliberately treats the JSONL files as an observation stream.  It
does not update the Codex database or any session files.  Native
``event_msg/item_completed`` records are preferred for timing and are paired
with response wrapper calls only when no native item exists.

Public entry points are :func:`read_inventory`, :func:`analyze_session`, and
:func:`export_snapshot`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import re
import sqlite3
import sys
from collections import OrderedDict, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Sequence


DEFAULT_LIMIT = 10
MAX_TASK_CHARS = 4000
MAX_MESSAGE_CHARS = 1200
MAX_DETAIL_CHARS = 1200
# The exec/wait output's own opening words, never text the script printed.
_CELL_RUNNING = re.compile(r"\s*Script running with cell ID (\w+)")


def _default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _default_db_path() -> Path:
    return _default_codex_home() / "state_5.sqlite"


def _sources() -> set[str]:
    """History sources to read: SESSION_PROFILER_SOURCE=codex (default), claude or all."""

    value = os.environ.get("SESSION_PROFILER_SOURCE", "codex").strip().lower()
    return {"codex", "claude"} if value == "all" else {value} if value in {"codex", "claude"} else {"codex"}


def _claude():
    if __package__:
        from . import claude
    else:
        import claude
    return claude


def _session_dirs(codex_home: Path | str | None = None) -> list[Path]:
    root = Path(codex_home).expanduser() if codex_home else _default_codex_home()
    return [root / "sessions", root / "archived_sessions"]


def _read_session_titles(codex_home: Path | str | None = None) -> dict[str, str]:
    """Read friendly thread names from the append-only session index, if present."""

    root = Path(codex_home).expanduser() if codex_home else _default_codex_home()
    index_path = root / "session_index.jsonl"
    titles: dict[str, str] = {}
    try:
        with index_path.open("r", encoding="utf-8", errors="replace") as stream:
            for raw_line in stream:
                try:
                    record = json.loads(raw_line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(record, Mapping):
                    continue
                session_id = record.get("id")
                title = record.get("thread_name") or record.get("title")
                if isinstance(session_id, str) and isinstance(title, str) and title.strip():
                    titles[session_id] = redact_text(title, 400)
    except OSError:
        pass
    return titles


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite(value: Any) -> int | float | None:
    if not _is_number(value):
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    return int(value) if value.is_integer() else value


def _valid_ms(value: Any) -> int | None:
    number = _finite(value)
    if number is None or number <= 0:
        return None
    return int(round(number))


def _as_ms(value: Any, *, default_unit: str = "auto") -> int | None:
    """Convert an epoch number into milliseconds.

    Codex uses both epoch seconds (turn records) and epoch milliseconds (item
    records).  Values below 10^11 are interpreted as seconds when ``auto`` is
    requested.
    """

    if isinstance(value, str):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return _parse_iso_ms(value)
    number = _finite(value)
    if number is None:
        return None
    if (default_unit == "seconds" and abs(number) < 100_000_000_000) or (default_unit == "auto" and abs(number) < 100_000_000_000):
        number *= 1000
    if not math.isfinite(number):
        return None
    return int(round(number))


def _parse_iso_ms(value: str | None) -> int | None:
    if not value or not isinstance(value, str):
        return None
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = _dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        return int(round(parsed.timestamp() * 1000))
    except (TypeError, ValueError, OverflowError):
        return None


def _record_timestamp(record: Mapping[str, Any]) -> int | None:
    return _parse_iso_ms(record.get("timestamp")) or _as_ms(record.get("timestamp"))


def _duration_ms(value: Any, *, numeric_unit: str = "milliseconds") -> int | None:
    """Return a duration in milliseconds, preserving unknown as ``None``."""

    if value is None:
        return None
    if isinstance(value, Mapping):
        secs = _finite(value.get("secs"))
        nanos = _finite(value.get("nanos"))
        if secs is None and nanos is None:
            # Some versions use seconds/nanoseconds names.
            secs = _finite(value.get("seconds"))
            nanos = _finite(value.get("nanoseconds"))
        if secs is None and nanos is None:
            return None
        return int(round((secs or 0) * 1000 + (nanos or 0) / 1_000_000))
    number = _finite(value)
    if number is None or number < 0:
        return None
    if numeric_unit == "seconds":
        number *= 1000
    return int(round(number))


_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)([\"']?\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|secret|authorization|cookie)\b[\"']?\s*[:=]\s*[\"']?)([^\s,;}'\"]+)"),
    re.compile(r"\b(?:sk|rk|ghp|github_pat|xox[baprs])-[-A-Za-z0-9_]{8,}\b"),
    re.compile(r"\bgAAAA[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.DOTALL),
)
_PATCH_LINE = re.compile(r"(?m)^diff --git .*(?:\n(?:^[+\-]{3}.*|^[+\-].*)*)?", re.MULTILINE)


def redact_text(value: Any, limit: int | None = None) -> str:
    """Normalize, bound, and redact user-visible log text."""

    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            value = str(value)
    text = value.replace("\x00", "").strip()
    if "diff --git" in text:
        text = _PATCH_LINE.sub("[file patch omitted]", text)
        text = text.replace("diff --git", "[file patch omitted]")
    if "-----BEGIN" in text:
        text = re.sub(r"-----BEGIN [^-]+-----.*?(?:-----END [^-]+-----|$)", "[private key omitted]", text, flags=re.DOTALL)
        text = re.sub(r"-----BEGIN[^\n]*", "[private key omitted]", text)
    # Do not carry image/audio/blob payloads into a timing snapshot.  They are
    # useful to the live tool surface but add no diagnostic timing value.
    # Cover both base64 data URLs and shorter data/blob probes that may not
    # include a media payload marker.  A data URL has no timing value here.
    text = re.sub(r"data:[^\s\"']+", "[binary data omitted]", text)
    text = re.sub(r"gAAAA[A-Za-z0-9_-]*", "[encrypted payload omitted]", text)
    # Tool output sometimes prints a JSON/session record containing fields that
    # are intentionally excluded from the snapshot.  Drop those lines before
    # clipping so a command that inspected a rollout cannot smuggle the raw
    # prompt/reasoning payload into an otherwise bounded result summary.
    sensitive_terms = ("base_instructions", "encrypted_content", "raw_content")
    if any(term in text for term in sensitive_terms):
        text = "\n".join("[sensitive metadata omitted]" if any(term in line for term in sensitive_terms) else line for line in text.splitlines())
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda match: match.group(1) + "[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    # Keep accidental dumps of environment variables out of the snapshot.
    text = re.sub(r"(?im)^\s*(?:export\s+)?(?:[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Z0-9_]*)\s*=.*$", "[environment value omitted]", text)
    if limit is not None and len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text


def _clip(value: Any, limit: int = MAX_DETAIL_CHARS) -> str:
    return redact_text(value, limit)


_OMIT_KEYS = {
    "base_instructions",
    "encrypted_content",
    "raw_content",
    "environment",
    "env",
    "patch",
    "diff",
    "content",  # file-change content is a patch-sized payload
    "data",  # binary/image payloads have no timing value
}


def _safe_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Make a bounded, redacted detail value without copying sensitive blobs."""

    if depth > 3:
        return _clip(value)
    if key.lower() in _OMIT_KEYS:
        return "[omitted]"
    if isinstance(value, str):
        return _clip(value)
    if value is None or isinstance(value, (bool, int, float)):
        return _finite(value) if _is_number(value) else value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:40]:
            name = str(raw_key)
            if name.lower() in _OMIT_KEYS:
                result[name] = "[omitted]"
            else:
                result[name] = _safe_value(raw_value, key=name, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, key=key, depth=depth + 1) for item in list(value)[:40]]
    return _clip(value)


def _text_from_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        # Text blocks appear as {type: Text, text: ...}; avoid metadata fields.
        if isinstance(value.get("text"), str):
            return value["text"]
        if isinstance(value.get("value"), str):
            return value["value"]
        parts = []
        for key in ("content", "output", "message"):
            if key in value:
                text = _text_from_content(value[key])
                if text:
                    parts.append(text)
        return "\n".join(parts)
    if isinstance(value, (list, tuple)):
        return "\n".join(filter(None, (_text_from_content(part) for part in value)))
    return str(value)


def _norm_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def _looks_like_injected_context(text: Any) -> bool:
    """Recognize framework/context messages that are not user prompts.

    Rollouts contain the complete assembled prompt as ``response_item``
    messages. Those records include AGENTS.md, app/environment context and
    skill bodies. Native ``UserMessage`` items are the authoritative prompt
    records; this helper only filters response-message fallbacks.
    """

    if not isinstance(text, str):
        return False
    stripped = text.lstrip()
    prefixes = (
        "# AGENTS.md",
        "<app-context>",
        "<environment_context>",
        "<external_codex_apps_",
        "<codex_apps_",
        "<skill>",
        "<model_switch>",
        "<collaboration_mode>",
        "<permissions instructions>",
        "<plugins_instructions>",
        "<tools>",
    )
    return stripped.startswith(prefixes)


def _is_coordination_message(text: Any) -> bool:
    if not isinstance(text, str):
        return False
    stripped = text.lstrip()
    return stripped.startswith("Message Type: NEW_TASK") or stripped.startswith("Message Type: MESSAGE")


def _wrapper_exec_commands(value: Any) -> list[str]:
    """Extract nested ``exec_command`` command strings from wrapper JavaScript."""

    if not isinstance(value, str):
        return []
    result: list[str] = []
    pattern = re.compile(r"\b(?:cmd|command)\s*:\s*\"")
    for match in pattern.finditer(value):
        start = match.end()
        chars: list[str] = []
        escaped = False
        for char in value[start:]:
            if escaped:
                chars.append("\\" + char)
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == '"':
                break
            chars.append(char)
        raw = "".join(chars)
        try:
            decoded = json.loads('"' + raw.replace('"', '\\"') + '"')
        except (TypeError, ValueError):
            decoded = raw.replace('\\n', '\n').replace('\\"', '"')
        if decoded.strip():
            result.append(decoded)
    return result


def _native_command_text(item: Mapping[str, Any]) -> str:
    command = item.get("command")
    if isinstance(command, (list, tuple)):
        if command and isinstance(command[-1], str):
            return command[-1]
        return " ".join(str(value) for value in command)
    return command if isinstance(command, str) else ""


def _command_key(value: Any) -> str:
    text = value if isinstance(value, str) else str(value or "")
    text = text.replace("\\\"", '"').replace("\\'", "'")
    text = text.replace("\\n", "\n").replace("\\t", "\t")
    return re.sub(r"\s+", " ", text).strip()


def _commands_match(wrapper: str, native: str) -> bool:
    left = _command_key(wrapper)
    right = _command_key(native)
    if not left or not right:
        return False
    if left == right or left in right or right in left:
        return True
    # The native command may contain a shell prefix or a trailing wrapper
    # argument. A substantial common prefix avoids pairing unrelated short
    # commands while handling those prefixes.
    prefix_len = min(180, len(left), len(right))
    return prefix_len >= 32 and left[:prefix_len] == right[:prefix_len]


def _usable_prompt(value: Any) -> str:
    text = redact_text(value, MAX_TASK_CHARS)
    return "" if not text or _looks_like_injected_context(text) else text


def _call_arguments(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _assignment_prompt(arguments: Mapping[str, Any]) -> str:
    for key in ("message", "prompt", "task", "instructions"):
        value = arguments.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        stripped = value.strip()
        # Spawn payloads are encrypted in current Codex logs. Do not expose a
        # redacted token as though it were a readable task prompt.
        if stripped.startswith(("gAAAA", "eyJ")):
            continue
        candidate = _usable_prompt(stripped)
        if candidate:
            return candidate
    return ""


def _union_duration(intervals: Iterable[tuple[int | None, int | None]]) -> int:
    valid = sorted((int(start), int(end)) for start, end in intervals if start is not None and end is not None and end >= start)
    if not valid:
        return 0
    total = 0
    start, end = valid[0]
    for next_start, next_end in valid[1:]:
        if next_start <= end:
            end = max(end, next_end)
        else:
            total += end - start
            start, end = next_start, next_end
    return total + end - start


def _span(values: Iterable[tuple[int | None, int | None]]) -> tuple[int | None, int | None]:
    starts = [start for start, _ in values if start is not None]
    ends = [end for _, end in values if end is not None]
    return (min(starts) if starts else None, max(ends) if ends else None)


def _source(path: Path, line: int, source_type: str) -> dict[str, Any]:
    return {"file": str(path), "line": int(line), "type": source_type}


def _event_times(item: Mapping[str, Any], payload: Mapping[str, Any], record_ms: int | None) -> tuple[int | None, int | None, int | None, str]:
    start = _valid_ms(payload.get("started_at_ms"))
    end = _valid_ms(payload.get("completed_at_ms"))
    duration = _duration_ms(item.get("duration"))
    if duration is None:
        duration = _duration_ms(item.get("duration_ms"))
    if start is not None and end is not None and end < start:
        start, end = end, start
    # A few command records have zeroed item bounds while retaining duration.
    if duration is not None and duration > 0 and start is not None and end is not None and end - start == 0:
        start = end - duration
        return start, end, duration, "derived_from_duration"
    if end is None and start is not None and duration is not None:
        end = start + duration
        return start, end, duration, "derived_from_duration"
    if start is None and end is not None and duration is not None:
        start = end - duration
        return start, end, duration, "derived_from_duration"
    if start is not None and end is not None:
        return start, end, max(0, end - start), "native_interval"
    # Completion records have a reliable line timestamp.  Use it only as an
    # anchor for a duration; a timestamp by itself is an observed instant.
    if record_ms is not None and duration is not None:
        return record_ms - duration, record_ms, duration, "record_timestamp_duration"
    if record_ms is not None:
        return record_ms, record_ms, None, "record_timestamp"
    return start, end, duration, "duration_only" if duration is not None else "unknown"


def _wrapper_times(record_ms: int | None, output_ms: int | None) -> tuple[int | None, int | None, int | None, str]:
    if record_ms is not None and output_ms is not None and output_ms >= record_ms:
        return record_ms, output_ms, output_ms - record_ms, "record_interval"
    if record_ms is not None:
        return record_ms, record_ms, None, "record_timestamp"
    return None, None, None, "unknown"


def _record_payload(record: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = record.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _get_turn_id(payload: Mapping[str, Any]) -> str | None:
    for key in ("turn_id", "root_turn_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    attribution = payload.get("turn_attribution")
    if isinstance(attribution, Mapping):
        value = attribution.get("turn_id") or attribution.get("root_turn_id")
        if isinstance(value, str) and value:
            return value
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    if isinstance(metadata, Mapping):
        for key in ("turn_id", "root_turn_id"):
            value = metadata.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _status(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return redact_text(value, 80)
    return str(value)


def _item_detail(item: Mapping[str, Any]) -> tuple[str, str, Any]:
    item_type = str(item.get("type") or "Item")
    status = _status(item.get("status"))
    if item_type == "CommandExecution":
        command = _clip(item.get("command"), MAX_DETAIL_CHARS)
        output = _clip(item.get("aggregated_output"), MAX_DETAIL_CHARS)
        if re.search(r"(?:AGENTS|CLAUDE)\.md", command, re.IGNORECASE):
            output = "[base instructions omitted]"
        label = "Command"
        summary = command or output or "Command execution"
        detail = {
            "command": command,
            "fullCommand": redact_text(_native_command_text(item)),
            "output": output,
            "exitCode": _finite(item.get("exit_code")),
            "cwd": _clip(item.get("cwd"), 300) if item.get("cwd") else None,
        }
        return label, summary, detail
    if item_type == "McpToolCall":
        server = _clip(item.get("server"), 160)
        tool = _clip(item.get("tool"), 240)
        label = ".".join(part for part in (server, tool) if part) or "MCP tool"
        detail = {
            "server": server,
            "tool": tool,
            "arguments": _safe_value(item.get("arguments"), key="arguments"),
            "result": _safe_value(item.get("result"), key="result"),
        }
        summary = label
        return label, summary, detail
    if item_type == "Extension":
        kind = _clip(item.get("kind"), 180) or "extension"
        label = f"Extension: {kind}"
        detail = {
            "kind": kind,
            "query": _clip(item.get("query"), MAX_DETAIL_CHARS),
            "action": _safe_value(item.get("action"), key="action"),
            "results": _safe_value(item.get("results"), key="results"),
        }
        return label, _clip(item.get("query") or label, MAX_DETAIL_CHARS), detail
    if item_type == "FileChange":
        changes = item.get("changes")
        paths = []
        if isinstance(changes, Mapping):
            for path, change in list(changes.items())[:100]:
                kind = change.get("type") if isinstance(change, Mapping) else None
                paths.append({"path": _clip(path, 400), "type": _clip(kind, 80) if kind else None})
        detail = {
            "changes": paths,
            "status": _status(item.get("status")),
            "stdout": _clip(item.get("stdout"), MAX_DETAIL_CHARS),
            "stderr": _clip(item.get("stderr"), MAX_DETAIL_CHARS),
        }
        return "File change", f"{len(paths)} file change(s)", detail
    if item_type == "ImageView":
        path = _clip(item.get("path"), MAX_DETAIL_CHARS)
        return "Image view", path or "Image view", {"path": path}
    if item_type == "SubAgentActivity":
        kind = _clip(item.get("kind"), 80) or "activity"
        agent = _clip(item.get("agent_path") or item.get("agent_thread_id"), 240)
        return f"Subagent {kind}", agent or f"Subagent {kind}", {
            "activity": kind,
            "agentThreadId": item.get("agent_thread_id"),
            "agentPath": agent,
        }
    if item_type == "CollabAgentToolCall":
        tool = _clip(item.get("tool"), 160) or "collaboration"
        return f"Collaboration: {tool}", tool, _safe_value(item, key="item")
    if item_type == "ContextCompaction":
        return "Context compaction", "Context compaction", {}
    label = item_type
    summary = _clip(item.get("summary") or item.get("name") or label, MAX_DETAIL_CHARS)
    return label, summary, _safe_value(item, key="item")


def _is_native_action(item_type: str) -> bool:
    return item_type not in {"Reasoning", "UserMessage", "AgentMessage", "FunctionCallOutput"}


def _native_event_kind(item_type: str, item: Mapping[str, Any]) -> str:
    if item_type == "ContextCompaction":
        return "compaction"
    if item_type == "SubAgentActivity":
        return "lifecycle"
    if item_type == "CollabAgentToolCall" and str(item.get("tool") or "").lower() in {"wait", "wait_threads", "poll"}:
        return "wait"
    return "tool"


def _fallback_kind(name: str, namespace: str | None, detail: Mapping[str, Any]) -> str:
    lowered = f"{namespace or ''}.{name}".lower()
    if "wait" in lowered or name.lower() in {"wait", "wait_threads"}:
        return "wait"
    return "tool"


def _new_turn(turns: MutableMapping[str, dict[str, Any]], turn_id: str) -> dict[str, Any]:
    existing = turns.get(turn_id)
    if existing is None:
        existing = {
            "id": turn_id,
            "startMs": None,
            "endMs": None,
            "status": "observed",
            "prompt": "",
            "rootTurnId": None,
            "_messages": [],
        }
        turns[turn_id] = existing
    return existing


def _set_if_known(target: MutableMapping[str, Any], key: str, value: int | None) -> None:
    if value is not None:
        target[key] = value


def _parse_rollout(path: Path, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
    warnings: list[str] = []
    records: list[dict[str, Any]] = []
    current_turn: str | None = None
    meta: dict[str, Any] = dict(metadata or {})
    requested_session_id = meta.get("id")
    first_session_meta_id: str | None = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line_number, raw_line in enumerate(stream, 1):
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    warnings.append(f"line {line_number}: malformed JSON ({exc.msg})")
                    continue
                if not isinstance(record, Mapping):
                    warnings.append(f"line {line_number}: JSON value is not an object")
                    continue
                payload = record.get("payload")
                if not isinstance(payload, Mapping):
                    payload = {}
                explicit_turn = _get_turn_id(payload)
                if record.get("type") == "event_msg" and payload.get("type") == "task_started":
                    explicit_turn = str(payload.get("turn_id") or payload.get("root_turn_id") or "") or explicit_turn
                if explicit_turn:
                    current_turn = explicit_turn
                record_copy = {
                    "line": line_number,
                    "record": record,
                    "payload": payload,
                    "timestamp_ms": _record_timestamp(record),
                    "turn_id": explicit_turn or current_turn,
                }
                records.append(record_copy)
                if record.get("type") == "session_meta" and isinstance(payload, Mapping):
                    if first_session_meta_id is None:
                        candidate_id = payload.get("id") or payload.get("session_id")
                        if candidate_id:
                            first_session_meta_id = str(candidate_id)
                    for key in ("id", "session_id", "source", "model_provider", "thread_source", "originator", "timestamp"):
                        if payload.get(key) is not None and not meta.get(key):
                            meta[key] = payload.get(key)
    except OSError as exc:
        warnings.append(f"could not read rollout: {exc}")

    session_id = str(requested_session_id or first_session_meta_id or meta.get("id") or meta.get("session_id") or _session_id_from_path(path))
    # A spawned rollout can begin with a copy of the parent's active turn and
    # its assembled context.  Those records describe the parent, not the
    # child's lifespan. Prefer recorded parent-turn attribution to find the
    # child's first own turn; older records fall back to the creation time.
    # UUID timestamp prefixes cannot distinguish rapidly spawned agents.
    parent_id = meta.get("parentId")
    if not parent_id and isinstance(meta.get("source"), Mapping):
        source_object = meta.get("source")
        subagent = source_object.get("subagent") if isinstance(source_object, Mapping) else None
        spawn = subagent.get("thread_spawn") if isinstance(subagent, Mapping) else None
        if isinstance(spawn, Mapping):
            parent_id = spawn.get("parent_thread_id")
    if parent_id:
        task_starts: list[tuple[int, str, Mapping[str, Any], int | None]] = []
        for index, record in enumerate(records):
            if record["record"].get("type") != "event_msg" or record["payload"].get("type") != "task_started":
                continue
            turn_value = record["payload"].get("turn_id") or record["payload"].get("root_turn_id")
            if isinstance(turn_value, str) and turn_value:
                attribution = record["payload"].get("turn_attribution")
                if not isinstance(attribution, Mapping):
                    attribution = {}
                started_ms = _as_ms(record["payload"].get("started_at"), default_unit="seconds") or record.get("timestamp_ms")
                task_starts.append((index, turn_value, attribution, started_ms))
        # A child fork's inherited parent turn has no attribution. Its first
        # own task_started record carries parent_turn_id/initiating_agent_path.
        owned_starts = [
            index
            for index, _turn_value, attribution, _started_ms in task_starts
            if attribution.get("parent_turn_id") or attribution.get("initiating_agent_path")
        ]
        if not owned_starts:
            created_ms = _as_ms(meta.get("createdAtMs"))
            if created_ms is not None:
                owned_starts = [
                    index
                    for index, _turn_value, _attribution, started_ms in task_starts
                    if started_ms is not None and started_ms >= created_ms - 5_000
                ]
        if owned_starts:
            records = records[min(owned_starts) :]
        else:
            warnings.append("child turn boundary unavailable; inherited records may be incomplete")
    turns: OrderedDict[str, dict[str, Any]] = OrderedDict()
    events: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    seen_native: set[str] = set()
    seen_messages: list[tuple[str, str, int | None]] = []
    sequence = 0

    def append_event(event: dict[str, Any]) -> None:
        nonlocal sequence
        sequence += 1
        event["_sequence"] = sequence
        # Keep all required timing values either finite integers or null.
        for key in ("startMs", "endMs", "durationMs"):
            value = event.get(key)
            event[key] = int(value) if _is_number(value) and math.isfinite(float(value)) else None
        events.append(event)

    def append_message(*, msg_id: str, role: str, at_ms: int | None, text: str, phase: str | None, source: dict[str, Any], turn_id: str | None, native: bool = False) -> None:
        clean = redact_text(text, MAX_MESSAGE_CHARS)
        if not clean:
            return
        if role == "user" and _looks_like_injected_context(clean) and not native:
            return
        if role == "assistant" and _is_coordination_message(clean) and not native:
            return
        norm = _norm_text(clean)
        for old_role, old_norm, old_at in seen_messages:
            if old_role == role and old_norm == norm and (at_ms is None or old_at is None or abs(at_ms - old_at) <= 3000):
                return
        seen_messages.append((role, norm, at_ms))
        message = {
            "id": msg_id,
            "role": role,
            "atMs": at_ms,
            "text": clean,
            "phase": phase,
            "source": source,
        }
        if role == "user":
            message["_promptText"] = redact_text(text, MAX_TASK_CHARS)
            message["_native"] = native
        messages.append(message)
        if turn_id:
            turn = _new_turn(turns, turn_id)
            turn["_messages"].append(message)
        append_event({
            "id": f"{msg_id}:message",
            "kind": "message",
            "label": role,
            "startMs": at_ms,
            "endMs": at_ms,
            "durationMs": 0 if at_ms is not None else None,
            "timing": "observed" if at_ms is not None else "unknown",
            "status": phase,
            "summary": clean,
            "detail": {"role": role, "phase": phase},
            "source": source,
            "turnId": turn_id,
        })

    # Pair response wrapper calls with the response output and locate native
    # items emitted between them.  Native items are the timing authority.
    wrapper_indexes: list[dict[str, Any]] = []
    output_by_call: dict[str, dict[str, Any]] = {}
    # An exec call can log several outputs ("Script running with cell ID N",
    # then later results); the first one says whether the script outlived it.
    first_output_by_call: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(records):
        if record["record"].get("type") != "response_item":
            continue
        payload = record["payload"]
        subtype = payload.get("type")
        if subtype in {"function_call_output", "custom_tool_call_output"}:
            call_id = payload.get("call_id")
            if isinstance(call_id, str):
                output_by_call[call_id] = {"index": index, "record": record}
                first_output_by_call.setdefault(call_id, {"index": index, "record": record})
        elif subtype in {"function_call", "custom_tool_call"}:
            call_id = payload.get("call_id")
            if isinstance(call_id, str):
                wrapper_indexes.append({"index": index, "record": record, "call_id": call_id})

    native_index_records: list[tuple[int, Mapping[str, Any]]] = []
    for index, record in enumerate(records):
        if record["record"].get("type") == "event_msg" and record["payload"].get("type") == "item_completed":
            item = record["payload"].get("item")
            if isinstance(item, Mapping):
                native_index_records.append((index, item))

    native_child_by_call: dict[str, str] = {}
    for _index, item in native_index_records:
        if str(item.get("type") or "") != "SubAgentActivity":
            continue
        item_id = item.get("id")
        child_id = item.get("agent_thread_id")
        if isinstance(item_id, str) and isinstance(child_id, str) and child_id:
            native_child_by_call[item_id] = child_id
    assignments: dict[str, dict[str, Any]] = {}
    for wrapper in wrapper_indexes:
        payload = wrapper["record"]["payload"]
        name = str(payload.get("name") or "")
        if name not in {"spawn_agent", "followup_task"}:
            continue
        child_id = native_child_by_call.get(wrapper["call_id"])
        if not child_id:
            continue
        arguments = _call_arguments(payload.get("arguments") or payload.get("input"))
        task_name = arguments.get("task_name") or arguments.get("name")
        assignment = {
            "taskName": redact_text(task_name, 400) if isinstance(task_name, str) else "",
            "prompt": _assignment_prompt(arguments),
            "source": _source(path, int(wrapper["record"]["line"]), f"response_item:{payload.get('type')}:assignment"),
        }
        previous = assignments.get(child_id)
        if previous and previous.get("prompt") and not assignment.get("prompt"):
            assignment["prompt"] = previous["prompt"]
        if previous and previous.get("taskName") and not assignment.get("taskName"):
            assignment["taskName"] = previous["taskName"]
        # Keep the first readable spawn source when a later follow-up targets
        # the same child.  The initial spawn is the authoritative assignment
        # provenance; follow-ups may only repeat the task name or carry an
        # encrypted body.
        if previous and previous.get("source"):
            assignment["source"] = previous["source"]
        assignments[child_id] = assignment

    wrapper_is_native: set[str] = set()
    native_command_records = [
        (index, item)
        for index, item in native_index_records
        if str(item.get("type") or "") == "CommandExecution"
    ]
    claimed_native_commands: set[str] = set()
    for wrapper in wrapper_indexes:
        output = output_by_call.get(wrapper["call_id"])
        upper = output["index"] if output else len(records)
        call_payload = wrapper["record"]["payload"]
        name = str(call_payload.get("name") or "")
        namespace = call_payload.get("namespace")
        for index, item in native_index_records:
            if index <= wrapper["index"] or index > upper:
                continue
            item_type = str(item.get("type") or "")
            if not _is_native_action(item_type):
                continue
            # Any native action between an exec wrapper and its output is its
            # underlying operation.  Collaboration calls additionally pair by
            # call ID when available.
            if name == "exec" or item.get("id") == wrapper["call_id"] or item_type in {"SubAgentActivity", "CollabAgentToolCall"}:
                wrapper_is_native.add(wrapper["call_id"])
                break

        # Native command items are occasionally flushed after the wrapper's
        # output (especially for long-running processes and write_stdin
        # polling). Pair by the command text as a second pass so the wrapper
        # is not counted alongside the native execution.
        if name == "exec" and wrapper["call_id"] not in wrapper_is_native:
            wrapper_input = call_payload.get("input") or call_payload.get("arguments")
            nested_commands = _wrapper_exec_commands(wrapper_input)
            if isinstance(wrapper_input, str) and "tools.write_stdin" in wrapper_input and native_command_records:
                wrapper_is_native.add(wrapper["call_id"])
                continue
            candidates = sorted(
                native_command_records,
                key=lambda pair: (pair[0] < wrapper["index"], abs(pair[0] - wrapper["index"])),
            )
            for native_index, native_item in candidates:
                native_id = str(native_item.get("id") or "")
                if native_id and native_id in claimed_native_commands:
                    continue
                native_text = _native_command_text(native_item)
                if nested_commands and any(_commands_match(command, native_text) for command in nested_commands):
                    wrapper_is_native.add(wrapper["call_id"])
                    if native_id:
                        claimed_native_commands.add(native_id)
                    break

    # First pass: turn boundaries and native message/tool items.
    for record in records:
        top_type = record["record"].get("type")
        payload = record["payload"]
        turn_id = record.get("turn_id")
        event_type = payload.get("type")
        if top_type == "event_msg" and event_type == "task_started":
            turn_id = str(payload.get("turn_id") or payload.get("root_turn_id") or turn_id or "") or None
            if turn_id:
                turn = _new_turn(turns, turn_id)
                turn["status"] = "active"
                turn["rootTurnId"] = payload.get("root_turn_id")
                started = _as_ms(payload.get("started_at"), default_unit="seconds")
                if started is None:
                    started = record["timestamp_ms"]
                _set_if_known(turn, "startMs", started)
            continue
        if top_type == "event_msg" and event_type == "task_complete":
            turn_id = str(payload.get("turn_id") or payload.get("root_turn_id") or turn_id or "") or None
            if turn_id:
                turn = _new_turn(turns, turn_id)
                turn["status"] = "completed"
                turn["rootTurnId"] = payload.get("root_turn_id") or turn.get("rootTurnId")
                completed = _as_ms(payload.get("completed_at"), default_unit="seconds")
                if completed is None:
                    completed = record["timestamp_ms"]
                _set_if_known(turn, "endMs", completed)
                started = _as_ms(payload.get("started_at"), default_unit="seconds")
                if started is not None and turn.get("startMs") is None:
                    turn["startMs"] = started
                last_message = payload.get("last_agent_message")
                if isinstance(last_message, str) and last_message.strip():
                    append_message(
                        msg_id=f"{turn_id}:complete",
                        role="assistant",
                        at_ms=completed,
                        text=last_message,
                        phase="final",
                        source=_source(path, int(record["line"]), "event_msg:task_complete"),
                        turn_id=turn_id,
                    )
            continue
        if top_type == "event_msg" and event_type == "turn_aborted":
            turn_id = str(payload.get("turn_id") or turn_id or "") or None
            if turn_id:
                turn = _new_turn(turns, turn_id)
                turn["status"] = "aborted"
                turn["endMs"] = record["timestamp_ms"]
            continue
        if top_type != "event_msg" or event_type != "item_completed":
            continue
        item = payload.get("item")
        if not isinstance(item, Mapping):
            continue
        item_type = str(item.get("type") or "")
        item_id = str(item.get("id") or f"line-{record['line']}")
        if item_id in seen_native:
            continue
        seen_native.add(item_id)
        start_ms, end_ms, duration_ms, timing = _event_times(item, payload, record["timestamp_ms"])
        source = _source(path, int(record["line"]), "event_msg:item_completed")
        if item_type in {"UserMessage", "AgentMessage"}:
            role = "user" if item_type == "UserMessage" else "assistant"
            phase = item.get("phase") if item_type == "AgentMessage" else "prompt"
            at_ms = end_ms or start_ms or record["timestamp_ms"]
            append_message(
                msg_id=item_id,
                role=role,
                at_ms=at_ms,
                text=_text_from_content(item.get("content")),
                phase=phase,
                source=source,
                turn_id=turn_id,
                native=True,
            )
            continue
        if item_type == "Reasoning":
            continue
        label, summary, detail = _item_detail(item)
        kind = _native_event_kind(item_type, item)
        append_event({
            "id": item_id,
            "kind": kind,
            "label": label,
            "startMs": start_ms,
            "endMs": end_ms,
            "durationMs": duration_ms,
            "timing": timing,
            "status": _status(item.get("status") or item.get("kind")),
            "summary": _clip(summary, MAX_DETAIL_CHARS),
            "detail": detail,
            "source": source,
            "turnId": turn_id,
        })

    # Second pass: response messages and unpaired fallback calls.
    for wrapper in wrapper_indexes:
        record = wrapper["record"]
        payload = record["payload"]
        subtype = payload.get("type")
        call_id = wrapper["call_id"]
        output = output_by_call.get(call_id)
        output_payload = output["record"]["payload"] if output else {}
        if subtype == "function_call" and call_id in wrapper_is_native:
            continue
        if subtype == "custom_tool_call" and call_id in wrapper_is_native:
            continue
        name = str(payload.get("name") or "tool")
        namespace = payload.get("namespace")
        input_value = payload.get("arguments") if subtype == "function_call" else payload.get("input")
        output_value = output_payload.get("output") if isinstance(output_payload, Mapping) else None
        end_ms = output["record"].get("timestamp_ms") if output else record.get("timestamp_ms")
        start_ms, end_ms, duration_ms, timing = _wrapper_times(record.get("timestamp_ms"), end_ms)
        detail = {
            "namespace": _clip(namespace, 160) if namespace else None,
            "name": _clip(name, 240),
            "input": _safe_value(input_value, key="arguments"),
            "output": _safe_value(output_value, key="result"),
        }
        label = f"{namespace}.{name}" if namespace else name
        append_event({
            "id": call_id,
            "kind": _fallback_kind(name, namespace, detail),
            "label": _clip(label, 300),
            "startMs": start_ms,
            "endMs": end_ms,
            "durationMs": duration_ms,
            "timing": timing,
            "status": _status(payload.get("status") or output_payload.get("status")),
            "summary": _clip(name, MAX_DETAIL_CHARS),
            "detail": detail,
            "source": _source(path, int(record["line"]), f"response_item:{subtype}"),
            "turnId": record.get("turn_id"),
        })

    # Background jobs: an exec script that outlives its call returns "Script
    # running with cell ID N" and keeps running; a later wait on that cell
    # returns its final output. The job runs from the exec call to that output.
    # Its own commands are still timed above; the job is neither busy nor
    # summed action time. A cell no wait saw finish stays open (unknown end).
    for wrapper in wrapper_indexes:
        payload = wrapper["record"]["payload"]
        output = first_output_by_call.get(wrapper["call_id"])
        if payload.get("name") != "exec" or not output:
            continue
        launched = _CELL_RUNNING.match(_text_from_content(output["record"]["payload"].get("output")))
        if not launched:
            continue
        cell_id = launched.group(1)
        code = payload.get("input") or payload.get("arguments")
        if isinstance(code, str):
            # Drop the "// @exec: {yield_time_ms...}" settings line so the job reads as its code.
            code = re.sub(r"\A\s*// @exec:[^\n]*\n", "", code)
        finished = None
        for poll in wrapper_indexes:
            poll_payload = poll["record"]["payload"]
            poll_output = first_output_by_call.get(poll["call_id"])
            if poll["index"] <= wrapper["index"] or poll_payload.get("name") != "wait" or not poll_output:
                continue
            if str(_call_arguments(poll_payload.get("arguments") or poll_payload.get("input")).get("cell_id")) != cell_id:
                continue
            text = _text_from_content(poll_output["record"]["payload"].get("output"))
            if _CELL_RUNNING.match(text):
                continue
            finished = (poll_output, text)
            break
        start_ms = wrapper["record"].get("timestamp_ms")
        job = {
            "id": f"job:{wrapper['call_id']}",
            "kind": "job",
            "label": "exec · background job",
            "startMs": start_ms,
            "endMs": None,
            "durationMs": None,
            "timing": "unknown",
            "status": "no completion recorded",
            "summary": _clip(code, MAX_DETAIL_CHARS),
            "detail": {"command": _clip(code, MAX_DETAIL_CHARS), "cellId": cell_id, "output": None},
            "source": _source(path, int(wrapper["record"]["line"]), "response_item:custom_tool_call:job"),
            "turnId": wrapper["record"].get("turn_id"),
        }
        if finished:
            poll_output, text = finished
            end_ms = poll_output["record"].get("timestamp_ms")
            state = re.match(r"\s*Script (\w+)", text)
            job.update(
                endMs=end_ms,
                durationMs=max(0, end_ms - start_ms) if end_ms is not None and start_ms is not None else None,
                timing="cell_pair" if end_ms is not None and start_ms is not None else "unknown",
                status=state.group(1).lower() if state else "completed",
            )
            job["source"]["endLine"] = int(poll_output["record"]["line"])
            job["detail"]["output"] = _clip(text, MAX_DETAIL_CHARS)
        append_event(job)

    # Response messages are useful for agent-to-agent messages and for older
    # logs that lack native UserMessage/AgentMessage items.
    for record in records:
        if record["record"].get("type") != "response_item":
            continue
        payload = record["payload"]
        subtype = payload.get("type")
        source = _source(path, int(record["line"]), f"response_item:{subtype}")
        role: str | None = None
        phase: str | None = None
        text = ""
        if subtype == "message":
            raw_role = payload.get("role")
            if raw_role in {"user", "assistant"}:
                role = str(raw_role)
                text = _text_from_content(payload.get("content"))
        elif subtype == "agent_message":
            role = "assistant"
            text = _text_from_content(payload.get("content"))
            phase = "agent"
        if role:
            if role == "user" and _looks_like_injected_context(text):
                continue
            # Parent/child coordination records usually carry an encrypted
            # payload and are not the child's task or result. Native
            # AgentMessage items are parsed above and remain authoritative.
            if role == "assistant" and _is_coordination_message(text):
                continue
            append_message(
                msg_id=str(payload.get("id") or f"line-{record['line']}"),
                role=role,
                at_ms=record.get("timestamp_ms"),
                text=text,
                phase=phase,
                source=source,
                turn_id=record.get("turn_id"),
            )

    # Fill turn prompts from the first user message and bound all internal
    # fields before exposing the JSON contract.
    for turn in turns.values():
        prompt = next((message.get("_promptText") or message["text"] for message in turn["_messages"] if message["role"] == "user" and message.get("_native")), "")
        if not prompt:
            prompt = next((message.get("_promptText") or message["text"] for message in turn["_messages"] if message["role"] == "user"), "")
        turn["prompt"] = redact_text(prompt, MAX_TASK_CHARS)
        turn.pop("_messages", None)
        if turn.get("startMs") is None:
            turn["startMs"] = next((message["atMs"] for message in messages if message.get("id") and turn["id"] in str(message["id"])), None)

    events.sort(key=lambda event: (event.get("startMs") is None, event.get("startMs") or 0, event.get("_sequence", 0)))
    for event in events:
        event.pop("_sequence", None)
    for message in messages:
        message.pop("_promptText", None)
        message.pop("_native", None)
    messages.sort(key=lambda message: (message.get("atMs") is None, message.get("atMs") or 0))

    return {
        "id": session_id,
        "path": str(path),
        "meta": meta,
        "turns": list(turns.values()),
        "events": events,
        "messages": messages,
        "assignments": assignments,
        "warnings": warnings,
    }


def _connect_readonly(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve()
    uri = f"file:{path}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _rollout_index(dirs: Sequence[Path]) -> dict[str, Path]:
    index: dict[str, Path] = {}
    for directory in dirs:
        for path in directory.rglob("*.jsonl"):
            index[_session_id_from_path(path)] = path
    return index


def _find_rollout(session_id: str, row_path: str | None, dirs: Sequence[Path], index: Mapping[str, Path] | None = None) -> Path | None:
    candidates: list[Path] = []
    if row_path:
        candidates.append(Path(row_path).expanduser())
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    if index is not None:
        return index.get(session_id)
    for directory in dirs:
        direct = directory / f"{session_id}.jsonl"
        if direct.is_file():
            return direct
        try:
            matches = list(directory.rglob(f"*{session_id}.jsonl"))
        except OSError:
            matches = []
        if matches:
            return sorted(matches)[-1]
    return None


def _session_id_from_path(path: Path) -> str:
    match = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", path.stem, re.IGNORECASE)
    return match.group(1) if match else path.stem.removeprefix("rollout-")


def _db_inventory(db_path: Path | str) -> tuple[list[dict[str, Any]], dict[str, str | None], dict[str, list[str]]]:
    rows: list[dict[str, Any]] = []
    parents: dict[str, str | None] = {}
    children: dict[str, list[str]] = defaultdict(list)
    try:
        connection = _connect_readonly(db_path)
    except (OSError, sqlite3.Error):
        return rows, parents, children
    try:
        cursor = connection.execute("SELECT * FROM threads")
        columns = [column[0] for column in cursor.description or []]
        for raw in cursor.fetchall():
            row = dict(zip(columns, raw))
            session_id = str(row.get("id") or "")
            if not session_id:
                continue
            raw_source = row.get("source") or row.get("thread_source")
            source_parent = None
            if isinstance(raw_source, Mapping):
                source_object = raw_source
            elif isinstance(raw_source, str) and raw_source.lstrip().startswith("{"):
                try:
                    source_object = json.loads(raw_source)
                except (TypeError, ValueError):
                    source_object = {}
            else:
                source_object = {}
            if isinstance(source_object, Mapping):
                subagent = source_object.get("subagent")
                spawn = subagent.get("thread_spawn") if isinstance(subagent, Mapping) else None
                if isinstance(spawn, Mapping) and spawn.get("parent_thread_id"):
                    source_parent = str(spawn["parent_thread_id"])
            rows.append({
                "id": session_id,
                "title": redact_text(row.get("title") or row.get("name") or "", MAX_TASK_CHARS),
                "source": redact_text(row.get("source") or row.get("thread_source") or "", 120),
                "rolloutPath": row.get("rollout_path"),
                "createdAtMs": _as_ms(row.get("created_at_ms") or row.get("created_at")),
                "updatedAtMs": _as_ms(row.get("updated_at_ms") or row.get("recency_at_ms") or row.get("updated_at") or row.get("recency_at")),
                "archived": bool(row.get("archived")),
                "agentPath": redact_text(row.get("agent_path") or "", 300),
                "agentNickname": redact_text(row.get("agent_nickname") or "", 200),
                "agentRole": redact_text(row.get("agent_role") or "", 160),
                "model": redact_text(row.get("model") or "", 160),
                "firstUserMessage": redact_text(row.get("first_user_message") or "", MAX_TASK_CHARS),
                "parentId": source_parent,
            })
            if source_parent:
                parents.setdefault(session_id, source_parent)
                children[source_parent].append(session_id)
        try:
            for parent, child, _status_value in connection.execute("SELECT parent_thread_id, child_thread_id, status FROM thread_spawn_edges"):
                parent_id, child_id = str(parent), str(child)
                parents[child_id] = parent_id
                children[parent_id].append(child_id)
        except sqlite3.Error:
            pass
    except sqlite3.Error:
        pass
    finally:
        connection.close()
    by_id = {row["id"]: row for row in rows}
    for parent_id, child_ids in list(children.items()):
        children[parent_id] = list(dict.fromkeys(child_ids))
    for row in rows:
        row["parentId"] = parents.get(row["id"])
    return rows, parents, children


def read_inventory(
    db_path: Path | str | None = None,
    *,
    codex_home: Path | str | None = None,
    session_dirs: Sequence[Path | str] | None = None,
) -> list[dict[str, Any]]:
    """Return every thread row plus read-only rollout availability metadata."""

    db = Path(db_path).expanduser() if db_path else (Path(codex_home).expanduser() / "state_5.sqlite" if codex_home else _default_db_path())
    if db.is_dir():
        db = db / "state_5.sqlite"
    dirs = [Path(path).expanduser() for path in session_dirs] if session_dirs else _session_dirs(codex_home)
    if "codex" not in _sources():
        rows = _claude().claude_inventory()
        rows.sort(key=lambda row: (row.get("updatedAtMs") is None, -(row.get("updatedAtMs") or 0), row["id"]))
        return rows
    rows, _parents, children = _db_inventory(db)
    file_index = _rollout_index(dirs)
    session_titles = _read_session_titles(codex_home)
    known = {row["id"] for row in rows}
    # If a test or a portable copy has no DB, still expose discovered files.
    if not rows:
        for directory in dirs:
            try:
                files = directory.rglob("*.jsonl")
            except OSError:
                files = []
            for path in files:
                session_id = _session_id_from_path(path)
                if session_id in known:
                    continue
                rows.append({"id": session_id, "title": "", "source": "", "rolloutPath": str(path), "createdAtMs": None, "updatedAtMs": None, "archived": "archived_sessions" in str(directory), "agentPath": "", "agentNickname": "", "agentRole": "", "model": "", "firstUserMessage": "", "parentId": None})
                known.add(session_id)
    for row in rows:
        if row["id"] in session_titles:
            row["displayTitle"] = session_titles[row["id"]]
        path = _find_rollout(row["id"], row.get("rolloutPath"), dirs, file_index)
        row["path"] = str(path) if path else None
        row["available"] = path is not None
        row["childCount"] = len(children.get(row["id"], []))
    if "claude" in _sources():
        rows.extend(_claude().claude_inventory())
    rows.sort(key=lambda row: (row.get("updatedAtMs") is None, -(row.get("updatedAtMs") or 0), row["id"]))
    return rows


def _inventory_context(db_path: Path | str | None, codex_home: Path | str | None, session_dirs: Sequence[Path | str] | None) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, str | None], dict[str, list[str]], list[Path]]:
    db = Path(db_path).expanduser() if db_path else (Path(codex_home).expanduser() / "state_5.sqlite" if codex_home else _default_db_path())
    if db.is_dir():
        db = db / "state_5.sqlite"
    dirs = [Path(path).expanduser() for path in session_dirs] if session_dirs else _session_dirs(codex_home)
    if "codex" in _sources():
        rows, parents, children = _db_inventory(db)
        file_index = _rollout_index(dirs)
    else:
        rows, parents, children, file_index = [], {}, defaultdict(list), {}
    session_titles = _read_session_titles(codex_home)
    by_id = {row["id"]: row for row in rows}
    for row in rows:
        if row["id"] in session_titles:
            row["displayTitle"] = session_titles[row["id"]]
        path = _find_rollout(row["id"], row.get("rolloutPath"), dirs, file_index)
        row["path"] = str(path) if path else None
        row["available"] = path is not None
        row["childCount"] = len(children.get(row["id"], []))
    if "claude" in _sources():
        claude_rows = _claude().claude_inventory()
        rows.extend(claude_rows)
        by_id.update((row["id"], row) for row in claude_rows)
    return rows, by_id, parents, children, dirs


def _graph_ids(root_id: str, by_id: Mapping[str, Mapping[str, Any]], children: Mapping[str, Sequence[str]], parsed_children: Mapping[str, Sequence[str]] | None = None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    queue = [root_id]
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        result.append(current)
        next_ids = list(children.get(current, []))
        if parsed_children:
            next_ids.extend(parsed_children.get(current, []))
        for child in next_ids:
            if child not in seen:
                queue.append(child)
    return result


def _agent_name(row: Mapping[str, Any] | None, session_id: str) -> str:
    if row:
        path = str(row.get("agentPath") or "").strip()
        nickname = str(row.get("agentNickname") or "").strip()
        if path:
            path_name = path.rstrip("/").rsplit("/", 1)[-1]
            return f"{path_name} · {nickname}" if nickname and nickname != path_name else path_name
        if nickname:
            return nickname
        role = str(row.get("agentRole") or "").strip()
        if role:
            return role
        if not row.get("parentId"):
            return "Codex"
    return "Codex" if not row or not row.get("parentId") else session_id


def _detail_from_parsed(session_id: str, parsed: Mapping[str, Any], row: Mapping[str, Any] | None, parent_id: str | None, missing: bool = False, assignment: Mapping[str, Any] | None = None) -> dict[str, Any]:
    turns = []
    for turn in parsed.get("turns", []):
        turn_prompt = _usable_prompt(turn.get("prompt"))
        if not turn_prompt and row:
            turn_prompt = _usable_prompt(row.get("firstUserMessage"))
        turns.append({
            "id": turn.get("id"),
            "startMs": turn.get("startMs"),
            "endMs": turn.get("endMs"),
            "status": turn.get("status"),
            "prompt": turn_prompt,
            "rootTurnId": turn.get("rootTurnId"),
        })
    events = [dict(event) for event in parsed.get("events", [])]
    messages = [dict(message) for message in parsed.get("messages", [])]
    # An active turn may not have emitted task_complete yet.  Keep its public
    # endMs null to show the missing boundary, but use the last observed item
    # as the provisional endpoint for active-time accounting.
    turn_intervals: list[tuple[int | None, int | None]] = []
    for turn in turns:
        start_ms = turn.get("startMs")
        end_ms = turn.get("endMs")
        if end_ms is None and start_ms is not None:
            observed_ends = [
                event.get("endMs") for event in events if event.get("turnId") == turn.get("id") and event.get("endMs") is not None
            ]
            observed_ends.extend(
                message.get("atMs") for message in messages if message.get("atMs") is not None and turn.get("id") in str(message.get("id"))
            )
            if observed_ends:
                end_ms = max(observed_ends)
        turn_intervals.append((start_ms, end_ms))
    tool_intervals = [(event.get("startMs"), event.get("endMs")) for event in events if event.get("kind") == "tool"]
    active_ms = _union_duration(turn_intervals)
    tool_busy_ms = _union_duration(tool_intervals)
    tool_time_ms = sum(event.get("durationMs") or 0 for event in events if event.get("kind") == "tool" and event.get("durationMs") is not None)
    start_ms, end_ms = _span(list(turn_intervals) + [(event.get("startMs"), event.get("endMs")) for event in events])
    if start_ms is None and row:
        start_ms = row.get("createdAtMs")
    if end_ms is None and row:
        end_ms = row.get("updatedAtMs")
    if missing:
        status = "missing"
    elif turns:
        latest_turn = max(turns, key=lambda turn: turn.get("startMs") or 0)
        status = latest_turn.get("status") or "observed"
        if latest_turn.get("endMs") is None and status not in {"aborted", "interrupted"}:
            status = "active"
    else:
        status = "observed"
    wall_duration_ms = end_ms - start_ms if start_ms is not None and end_ms is not None and end_ms >= start_ms else None
    idle_ms = max(0, wall_duration_ms - active_ms) if wall_duration_ms is not None else None
    agent_warnings = list(parsed.get("warnings", []))
    if idle_ms is not None and idle_ms > 0 and len(turns) > 1:
        agent_warnings.append(f"lifespan includes {idle_ms}ms outside active turn spans; activeMs excludes idle waits")
    task = _usable_prompt((row or {}).get("firstUserMessage"))
    if not task:
        task = next((_usable_prompt(turn.get("prompt")) for turn in turns if _usable_prompt(turn.get("prompt"))), "")
    assignment_prompt = _usable_prompt((assignment or {}).get("prompt"))
    assignment_name = _usable_prompt((assignment or {}).get("taskName"))
    if not task:
        task = assignment_prompt or assignment_name
    if not task and parent_id and not missing:
        agent_warnings.append("task prompt unavailable; spawned prompt is not present in readable native records")
    elif parent_id and assignment and not assignment_prompt and assignment_name:
        agent_warnings.append("spawn prompt encrypted; taskName used as task label")
    if parent_id and assignment and not assignment_prompt and assignment_name:
        task_availability = "encrypted"
    elif task:
        task_availability = "available"
    else:
        task_availability = "unavailable"
    return {
        "id": session_id,
        "parentId": parent_id,
        "name": _agent_name(row, session_id),
        "task": task,
        "taskAvailability": task_availability,
        "taskSource": (assignment or {}).get("source") if assignment and task in {assignment_prompt, assignment_name} else None,
        "model": (row or {}).get("model") or (parsed.get("meta") or {}).get("model") or None,
        "status": status,
        "startMs": start_ms,
        "endMs": end_ms,
        "activeMs": active_ms,
        "wallDurationMs": wall_duration_ms,
        "idleMs": idle_ms,
        "toolCount": sum(1 for event in events if event.get("kind") == "tool"),
        "toolTimeMs": tool_time_ms,
        "toolBusyMs": tool_busy_ms,
        "turns": turns,
        "events": events,
        "messages": messages,
        "warnings": agent_warnings,
    }


def analyze_session(
    session_id: str,
    db_path: Path | str | None = None,
    *,
    codex_home: Path | str | None = None,
    session_dirs: Sequence[Path | str] | None = None,
) -> dict[str, Any]:
    """Parse one root session and recursively include its spawn descendants."""

    rows, by_id, parents, children, dirs = _inventory_context(db_path, codex_home, session_dirs)
    parsed_by_id: dict[str, dict[str, Any]] = {}
    parsed_children: dict[str, list[str]] = defaultdict(list)
    root_row = by_id.get(session_id)
    root_path = _find_rollout(session_id, (root_row or {}).get("rolloutPath"), dirs)
    if root_path:
        parsed_by_id[session_id] = _parse_rollout(root_path, root_row)
        source_meta = parsed_by_id[session_id].get("meta", {}).get("source")
        if isinstance(source_meta, Mapping):
            spawn = source_meta.get("subagent", {}).get("thread_spawn") if isinstance(source_meta.get("subagent"), Mapping) else None
            parent_from_source = spawn.get("parent_thread_id") if isinstance(spawn, Mapping) else None
            if parent_from_source:
                parents.setdefault(session_id, str(parent_from_source))
                parsed_children[str(parent_from_source)].append(session_id)
    queue = [session_id]
    while queue:
        current = queue.pop(0)
        candidate_children = list(children.get(current, [])) + list(parsed_children.get(current, []))
        for child_id in candidate_children:
            if child_id in parsed_by_id:
                continue
            child_row = by_id.get(child_id)
            child_path = _find_rollout(child_id, (child_row or {}).get("rolloutPath"), dirs)
            if child_path:
                parsed_by_id[child_id] = _parse_rollout(child_path, child_row)
                source_meta = parsed_by_id[child_id].get("meta", {}).get("source")
                if isinstance(source_meta, Mapping):
                    spawn = source_meta.get("subagent", {}).get("thread_spawn") if isinstance(source_meta.get("subagent"), Mapping) else None
                    source_parent = spawn.get("parent_thread_id") if isinstance(spawn, Mapping) else None
                    if source_parent and str(source_parent) != current:
                        parents[child_id] = str(source_parent)
                        parsed_children[str(source_parent)].append(child_id)
            queue.append(child_id)

    ids = _graph_ids(session_id, by_id, children, parsed_children)
    for child in parsed_by_id:
        if child not in ids and (parents.get(child) == session_id or child == session_id):
            ids.append(child)
    agents: list[dict[str, Any]] = []
    warnings: list[str] = []
    assignments: dict[str, dict[str, Any]] = {}
    for parsed in parsed_by_id.values():
        for child_id, assignment in (parsed.get("assignments") or {}).items():
            if isinstance(child_id, str) and isinstance(assignment, Mapping):
                assignments[child_id] = dict(assignment)
    for agent_id in ids:
        row = by_id.get(agent_id)
        parsed = parsed_by_id.get(agent_id)
        parent_id = parents.get(agent_id)
        if parsed is None:
            path = _find_rollout(agent_id, (row or {}).get("rolloutPath"), dirs)
            parsed = {"id": agent_id, "meta": {}, "turns": [], "events": [], "messages": [], "warnings": ["rollout file unavailable"]}
            if path:
                parsed = _parse_rollout(path, row)
            warnings.append(f"missing descendant rollout: {agent_id}")
        detail = _detail_from_parsed(
            agent_id,
            parsed,
            row,
            parent_id,
            missing=not bool(parsed.get("events") or parsed.get("turns") or parsed.get("messages")),
            assignment=assignments.get(agent_id),
        )
        agents.append(detail)
        warnings.extend(f"{agent_id}: {warning}" for warning in detail.get("warnings", []))
    all_spans = [(agent.get("startMs"), agent.get("endMs")) for agent in agents]
    all_turn_spans: list[tuple[int | None, int | None]] = []
    for agent in agents:
        for turn in agent.get("turns", []):
            start_ms = turn.get("startMs")
            end_ms = turn.get("endMs")
            if end_ms is None and start_ms is not None:
                observed = [
                    event.get("endMs")
                    for event in agent.get("events", [])
                    if event.get("turnId") == turn.get("id") and event.get("endMs") is not None
                ]
                if observed:
                    end_ms = max(observed)
            all_turn_spans.append((start_ms, end_ms))
    root_agent = next((agent for agent in agents if agent["id"] == session_id), None)
    start_ms, end_ms = _span(all_spans)
    title = (root_row or {}).get("displayTitle") or (root_row or {}).get("title") or (parsed_by_id.get(session_id, {}).get("meta") or {}).get("title") or ""
    if not title:
        first_prompt = root_agent.get("task") if root_agent else ""
        title = first_prompt.splitlines()[0][:200] if first_prompt else session_id
    return {
        "id": session_id,
        "title": redact_text(title, MAX_TASK_CHARS),
        "startMs": start_ms,
        "endMs": end_ms,
        "activeMs": _union_duration(all_turn_spans),
        "rootActiveMs": root_agent.get("activeMs", 0) if root_agent else 0,
        "wallDurationMs": end_ms - start_ms if start_ms is not None and end_ms is not None and end_ms >= start_ms else None,
        "turnIntervals": [
            {"agentId": agent["id"], "startMs": turn.get("startMs"), "endMs": turn.get("endMs"), "status": turn.get("status")}
            for agent in agents for turn in agent.get("turns", [])
        ],
        "agents": agents,
        "warnings": warnings,
    }


def _select_root_ids(rows: Sequence[Mapping[str, Any]], limit: int) -> list[str]:
    available = [row for row in rows if row.get("available") and not row.get("parentId")]
    # Put a recent root with several available descendants first so the
    # default export demonstrates the full graph, then fill with recent roots.
    # This stays useful as the local session inventory changes.
    selected: list[str] = []
    substantial = sorted(
        (row for row in available if int(row.get("childCount") or 0) >= 2),
        key=lambda row: (-(row.get("updatedAtMs") or 0), str(row.get("id"))),
    )
    if substantial:
        selected.append(str(substantial[0]["id"]))
    recent = sorted(available, key=lambda row: (-(row.get("updatedAtMs") or 0), str(row.get("id"))))
    for row in recent:
        if len(selected) >= limit:
            break
        if str(row["id"]) not in selected:
            selected.append(str(row["id"]))
    return selected[:limit]


def _summary(detail: Mapping[str, Any], inventory_row: Mapping[str, Any] | None) -> dict[str, Any]:
    available = bool((inventory_row or {}).get("available")) if inventory_row is not None else any(
        agent.get("id") == detail.get("id") and agent.get("status") != "missing" for agent in detail.get("agents", [])
    )
    return {
        "id": detail.get("id"),
        "title": detail.get("title") or (inventory_row or {}).get("title") or detail.get("id"),
        "startMs": detail.get("startMs"),
        "endMs": detail.get("endMs"),
        "agentCount": len(detail.get("agents", [])),
        "available": available,
        "activeMs": detail.get("activeMs"),
        "rootActiveMs": detail.get("rootActiveMs"),
        "archived": bool((inventory_row or {}).get("archived", False)),
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() else value if math.isfinite(value) else None
    return value


def export_snapshot(
    output: Path | str | None = None,
    *,
    session_ids: Sequence[str] | None = None,
    session_id: str | None = None,
    limit: int = DEFAULT_LIMIT,
    db_path: Path | str | None = None,
    codex_home: Path | str | None = None,
    session_dirs: Sequence[Path | str] | None = None,
    split_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Build and optionally write the profiler snapshot contract."""

    rows, by_id, _parents, _children, _dirs = _inventory_context(db_path, codex_home, session_dirs)
    requested = list(session_ids or [])
    if session_id:
        requested.append(session_id)
    root_ids = requested or _select_root_ids(rows, max(1, int(limit)))
    details: dict[str, Any] = {}
    summaries: list[dict[str, Any]] = []
    for root_id in root_ids:
        if root_id in details:
            continue
        row = by_id.get(root_id) or {}
        if row.get("source") == "claude-code":
            detail = _claude().analyze_claude_session(root_id, row["path"])
        else:
            detail = analyze_session(root_id, db_path, codex_home=codex_home, session_dirs=session_dirs)
        details[root_id] = detail
        summaries.append(_summary(detail, by_id.get(root_id)))
    generated_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    snapshot: dict[str, Any] = {
        "generatedAt": generated_at,
        "sessions": summaries,
        "selectedSessionId": root_ids[0] if root_ids else None,
        "details": details,
        "inventory": [
            {
                "id": row.get("id"),
                "title": row.get("displayTitle") or row.get("title"),
                "updatedAtMs": row.get("updatedAtMs"),
                "createdAtMs": row.get("createdAtMs"),
                "available": row.get("available"),
                "archived": row.get("archived"),
                "parentId": row.get("parentId"),
            }
            for row in rows
        ],
    }
    snapshot = _json_safe(snapshot)
    if output:
        output_path = Path(output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    if split_dir:
        split_path = Path(split_dir).expanduser()
        split_path.mkdir(parents=True, exist_ok=True)
        for root_id, detail in details.items():
            per_session = {
                "generatedAt": generated_at,
                "sessions": [_summary(detail, by_id.get(root_id))],
                "selectedSessionId": root_id,
                "details": {root_id: detail},
            }
            (split_path / f"{root_id}.json").write_text(json.dumps(_json_safe(per_session), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return snapshot


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export read-only Codex session timing data")
    parser.add_argument("--output", default="snapshot.json", help="snapshot JSON path")
    parser.add_argument("--session", dest="sessions", action="append", help="root session ID (repeatable)")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="number of recent top-level sessions when --session is omitted")
    parser.add_argument("--db", dest="db_path", help="state_5.sqlite path")
    parser.add_argument("--codex-home", help="Codex home containing state_5.sqlite and session directories")
    parser.add_argument("--sessions-dir", action="append", help="additional session directory (repeatable)")
    parser.add_argument("--split-dir", help="also write one complete snapshot JSON per root session")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    snapshot = export_snapshot(
        args.output,
        session_ids=args.sessions,
        limit=args.limit,
        db_path=args.db_path,
        codex_home=args.codex_home,
        session_dirs=args.sessions_dir,
        split_dir=args.split_dir,
    )
    print(json.dumps({"output": str(Path(args.output).expanduser()), "selectedSessionId": snapshot.get("selectedSessionId"), "sessions": len(snapshot.get("sessions", [])), "inventory": len(snapshot.get("inventory", []))}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
