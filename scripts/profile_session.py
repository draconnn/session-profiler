#!/usr/bin/env python3
"""Find, refresh and open a local Codex or Claude Code session profiler without a manual launch."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen
import uuid

SKILL = Path(__file__).resolve().parents[1]
ASSETS = SKILL / 'assets/profiler'
sys.path.insert(0, str(ASSETS / 'parser'))
from parser import read_inventory  # noqa: E402

UUID = re.compile(r'(?<![a-f0-9])[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}(?![a-f0-9])', re.I)


class SelectionError(ValueError):
    def __init__(self, message, candidates=None):
        super().__init__(message)
        self.candidates = candidates or []


CURRENT_ENV = {'codex': ('CODEX_THREAD_ID', 'CODEX_SESSION_ID'), 'claude': ('CLAUDE_CODE_SESSION_ID',),
               'all': ('CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'CLAUDE_CODE_SESSION_ID')}


def brief(row):
    return {key: row.get(key) for key in ('id', 'displayTitle', 'title', 'available', 'parentId', 'updatedAtMs')} | {key: row[key] for key in ('source', 'project') if row.get('source') == 'claude-code'}


def matches(rows, query):
    candidates = [r for r in rows if not r.get('parentId')]
    query = query.casefold().strip()
    if query:
        exact = [r for r in candidates if query in {str(r.get('displayTitle') or '').casefold(), str(r.get('title') or '').casefold()}]
        candidates = exact or [r for r in candidates if query in str(r.get('displayTitle') or r.get('title') or '').casefold()]
    return sorted(candidates, key=lambda row: row.get('updatedAtMs') or 0, reverse=True)


def select(rows, session, query, env, source='codex'):
    by_id = {row['id']: row for row in rows}
    if query:
        candidates = matches(rows, query)
        if len(candidates) != 1:
            raise SelectionError('Session title is ambiguous' if candidates else 'No session title matched', [brief(r) for r in candidates[:25]])
        selected = candidates[0]['id']
    else:
        if session == 'current':
            session = next((env[key] for key in CURRENT_ENV[source] if env.get(key)), '')
        found = UUID.search(session or '')
        if not found:
            raise SelectionError('No session ID found. Pass an exact UUID/chat link, or use --list --query to find the chat.')
        selected = found.group().lower()
    if selected not in by_id:
        raise SelectionError(f'Session {selected} is not in the local history inventory')
    root = selected
    seen = set()
    while by_id.get(root, {}).get('parentId'):
        if root in seen:
            raise SelectionError('Cyclic parent relationships in session metadata')
        seen.add(root)
        root = by_id[root]['parentId']
    if root not in by_id or not by_id[root].get('available'):
        raise SelectionError(f'Local rollout history is unavailable for root session {root}')
    return selected, root


def request_json(url, *, data=None, timeout=4):
    headers = {}
    if data is not None:
        headers = {'Origin': url.rsplit('/api/', 1)[0], 'Content-Type': 'application/json'}
        data = json.dumps(data).encode()
    with urlopen(Request(url, data=data, headers=headers), timeout=timeout) as response:
        return json.load(response)


def health(base, runtime):
    # Never send refresh requests to an unrelated listener or trust a stale PID.
    if not re.fullmatch(r'http://127\.0\.0\.1:\d+', base or ''):
        return False
    try:
        status = request_json(base + '/api/status', timeout=1)
        return status.get('tool') == 'codex-session-profiler' and status.get('runtimePath') == str(runtime)
    except (URLError, OSError, ValueError):
        return False


def code_files(root):
    # Code is everything the skill ships; the report snapshot, build output, data and server records stay with the runtime.
    files = {}
    for folder, dirs, names in os.walk(root):
        base = Path(folder)
        rel_dir = base.relative_to(root).as_posix()
        dirs[:] = [d for d in dirs if d not in {'__pycache__', 'node_modules'} and (base / d).relative_to(root).as_posix() not in {'data', 'app/dist'}]
        for name in names:
            rel = (base / name).relative_to(root).as_posix()
            runtime_record = rel_dir == '.' and (name.startswith('server.') or name in {'.launch.lock', '.code-stamp'})
            if not runtime_record and rel != 'app/src/data.json' and not name.endswith('.pyc'):
                files[rel] = base / name
    return files


def stop(runtime):
    # Only stop a server that answers as this runtime's profiler and whose recorded PID runs this runtime's launcher.
    record = runtime / 'server.json'
    try:
        info = json.loads(record.read_text())
    except (OSError, ValueError):
        return
    base, pid = info.get('baseUrl'), info.get('pid')
    if not health(base, runtime):
        return
    command = subprocess.run(['ps', '-o', 'command=', '-p', str(pid)], capture_output=True, text=True).stdout if isinstance(pid, int) else ''
    if str(runtime / 'launch.py') not in command:
        raise RuntimeError(f'The profiler at {base} runs outdated code, but its process is not PID {pid}; stop it and rerun')
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while health(base, runtime):
        if time.monotonic() > deadline:
            raise RuntimeError(f'The outdated profiler at {base} (PID {pid}) did not stop')
        time.sleep(0.2)
    record.unlink(missing_ok=True)


def prepare(runtime):
    # The runtime follows the skill: when the shipped code changes, stop the old server and copy the code again, keeping the data.
    sources = code_files(ASSETS)
    digest = hashlib.sha256()
    for rel, path in sorted(sources.items()):
        content = path.read_bytes()
        digest.update(f'{rel}\0{len(content)}\0'.encode() + content)
    stamp = digest.hexdigest()
    stamp_path = runtime / '.code-stamp'
    if (runtime / 'launch.py').exists():
        try:
            if stamp_path.read_text().strip() == stamp:
                return
        except OSError:
            pass
        stop(runtime)
    for rel, path in code_files(runtime).items():
        if rel not in sources:
            path.unlink()
    for rel, path in sources.items():
        (runtime / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, runtime / rel)
    target = runtime / 'app/src/data.json'
    if not target.exists():
        target.write_text(json.dumps({'id': 'dashboard:' + str(uuid.uuid4()), 'surface': 'dashboard', 'queries': {}}) + '\n')
    stamp_path.write_text(stamp + '\n')


def launch(runtime, root):
    record = runtime / 'server.json'
    if record.exists():
        try:
            base = json.loads(record.read_text()).get('baseUrl')
        except (OSError, ValueError):
            base = None
        if health(base, runtime):
            request_json(base + '/api/open', data={'sessionId': root}, timeout=320)
            return base, True
    # Build before detaching so build failures are reported to the invoking agent.
    command = [sys.executable, str(runtime / 'launch.py'), '--session', root, '--limit', '1']
    built = subprocess.run(command + ['--refresh-only'], capture_output=True, text=True, timeout=330)
    if built.returncode:
        raise RuntimeError('Report build failed: ' + (built.stderr or built.stdout)[-5000:])
    log_path = runtime / 'server.log'
    with log_path.open('w') as log:
        process = subprocess.Popen(command + ['--no-build'], cwd=runtime, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        output = log_path.read_text(errors='replace')
        found = re.search(r'Session profiler: (http://127\.0\.0\.1:\d+)/', output)
        if found and health(found.group(1), runtime):
            base = found.group(1)
            record.write_text(json.dumps({'baseUrl': base, 'pid': process.pid}) + '\n')
            return base, False
        if process.poll() is not None:
            raise RuntimeError('Profiler server exited: ' + output[-3000:])
        time.sleep(0.2)
    process.terminate()
    raise RuntimeError(f'Profiler server did not become ready; see {log_path}')


def summarize(profile, root):
    detail = profile['details'][root]
    agents = detail['agents']
    actions = []
    for agent in agents:
        for event in agent.get('events', []):
            if event.get('kind') in {'tool', 'wait', 'compaction'}:
                actions.append({'agentId': agent['id'], 'agentName': agent['name'], **event})
    known = [e for e in actions if isinstance(e.get('durationMs'), (int, float))]
    # Background jobs (a Bash command run in the background, from launch to its
    # completion notice) run beside the turns, so they are reported apart from
    # actions and their overlapping time is counted once.
    jobs = [{'agentId': agent['id'], 'agentName': agent['name'], **event} for agent in agents for event in agent.get('events', []) if event.get('kind') == 'job']
    timed_jobs = [e for e in jobs if isinstance(e.get('durationMs'), (int, float))]
    def union(spans):
        total, last_end = 0, None
        for start, end in sorted((a, b) for a, b in spans if isinstance(a, (int, float)) and isinstance(b, (int, float)) and b > a):
            if last_end is not None and start <= last_end:
                total += max(0, end - last_end); last_end = max(last_end, end)
            else:
                total += end - start; last_end = end
        return total
    job_spans = [(e['startMs'], e['endMs']) for e in timed_jobs]
    turn_spans = [(t.get('startMs'), t.get('endMs')) for agent in agents for t in agent.get('turns', [])]
    job_ms, working_ms, either_ms = union(job_spans), union(turn_spans), union(turn_spans + job_spans)
    # The wall span in three parts that add up to it.
    wall_ms = detail.get('wallDurationMs') or 0
    breakdown = {'workingMs': working_ms, 'backgroundOnlyMs': either_ms - working_ms, 'nothingRunningMs': max(0, wall_ms - either_ms),
                 'toolCallMs': union((e['startMs'], e['endMs']) for e in known if e.get('kind') == 'tool')}
    def activity(agent):
        messages = [m for m in agent.get('messages', []) if m.get('role') == 'assistant' and m.get('text')]
        return {**{k: agent.get(k) for k in ('id', 'name', 'task', 'taskAvailability', 'activeMs', 'wallDurationMs', 'toolCount', 'status')},
                'firstUpdate': messages[0]['text'][:900] if messages else None,
                'lastUpdate': messages[-1]['text'][:1200] if messages else None}
    return {
        'title': detail['title'], 'generatedAt': profile['generatedAt'],
        'wallDurationMs': detail.get('wallDurationMs'), 'activeMs': detail.get('activeMs'),
        'rootStatus': next((a.get('status') for a in agents if a['id'] == root), None),
        'subagentCount': len(agents) - 1, 'missingAgentCount': sum(a.get('status') == 'missing' for a in agents),
        'toolCount': sum(e.get('kind') == 'tool' for e in actions), 'actionCount': len(actions),
        'unknownDurationCount': len(actions) - len(known), 'summedActionMs': sum(e['durationMs'] for e in known if not e.get('idle')),
        'longestSubagents': [activity(a) for a in sorted((a for a in agents if a['id'] != root), key=lambda a: a.get('activeMs') or 0, reverse=True)[:8]],
        'longestActions': [{k: e.get(k) for k in ('agentId', 'agentName', 'kind', 'label', 'durationMs', 'startMs', 'endMs', 'timing', 'status', 'source')} | {'summary': str(e.get('summary') or '')[:1200]} for e in sorted(known, key=lambda e: e['durationMs'], reverse=True)[:10]],
        'timeBreakdown': breakdown,
        'backgroundJobCount': len(jobs), 'backgroundJobOpenCount': len(jobs) - len(timed_jobs), 'backgroundJobMs': job_ms,
        'longestBackgroundJobs': [{k: e.get(k) for k in ('agentId', 'agentName', 'label', 'durationMs', 'startMs', 'endMs', 'timing', 'status', 'source')} | {'summary': str(e.get('summary') or '')[:600]} for e in sorted(timed_jobs, key=lambda e: e['durationMs'], reverse=True)[:10]],
        'warningCount': len(detail.get('warnings', [])), 'warnings': detail.get('warnings', [])[:10],
    }


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    select_group = cli.add_mutually_exclusive_group()
    select_group.add_argument('--session', default='current', help='current, a UUID, or a Codex chat link')
    select_group.add_argument('--query', help='exact or partial chat title; ambiguous matches return candidates')
    cli.add_argument('--list', action='store_true', help='list matching top-level chats without launching')
    cli.add_argument('--source', choices=('codex', 'claude', 'all'), default=os.environ.get('SESSION_PROFILER_SOURCE', 'codex'),
                     help='history to read: Codex (default), Claude Code, or both')
    args = cli.parse_args()
    # The parser, launcher and server all read this, so the choice follows them.
    os.environ['SESSION_PROFILER_SOURCE'] = args.source
    rows = read_inventory()
    if args.list:
        found = matches(rows, args.query or '')
        print(json.dumps({'matchCount': len(found), 'sessions': [brief(r) for r in found[:25]]}, ensure_ascii=False))
        return 0
    selected, root = select(rows, args.session, args.query, os.environ, args.source)
    codex_home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser().resolve()
    runtime = codex_home / '.tmp/session-profiler' / ('v1' if args.source == 'codex' else f'v1-{args.source}') / root
    runtime.mkdir(parents=True, exist_ok=True)
    # Serialize repeated requests for this chat, while other chats remain independent.
    with (runtime / '.launch.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        prepare(runtime)
        base, reused = launch(runtime, root)
        profile_path = runtime / 'data/profile.json'
        profile = json.loads(profile_path.read_text())
    print(json.dumps({'ok': True, 'selectedId': selected, 'rootId': root, 'url': base + '/?view=1&tab=dashboard',
                      'reusedServer': reused, 'profilePath': str(profile_path), 'summary': summarize(profile, root)}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SelectionError as exc:
        print(json.dumps({'ok': False, 'error': str(exc), 'candidates': exc.candidates}, ensure_ascii=False))
        raise SystemExit(2)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
