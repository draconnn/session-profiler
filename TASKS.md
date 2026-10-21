# Standalone dashboard (option A)

## Goal
The profiler's report page runs without any code from the Codex Data Analytics plugin (proprietary), so the whole skill can be open-sourced. Done = `/session-profiler` in Codex or Claude Code works as today and opens the same report page, and everything in the skill folder is the user's own code or openly licensed code.

## Milestones
- [x] Standalone report page in `assets/profiler/web/` (plain HTML, CSS, JavaScript; no build step, no Node.js), with the same sections as today: top bar (session, prompt and agent pickers, Refresh), summary figures and background-jobs line, timeline (agent lanes, zoom 1-16x, legend, click to inspect), Time by action (click filters the explorer), Work detail panel (agent and action views, full command), Action explorer (search, filter, sort, show more, expandable "what it did"), timing definitions. Follows the system light/dark mode.
- [x] `launch.py` and `scripts/profile_session.py` serve the page and `data/profile.json` directly; no plugin build, no Node.js; old runtimes upgrade cleanly; plugin template (`assets/profiler/app/`) deleted.
- [x] Bug: a killed or stopped background job whose notice is not when its work ended (the notice says it outlived its session, or gives no reason and came while the session was quiet, e.g. app cleanup days later) gets an unknown end instead of a duration to the notice (was 75 h). Notices naming the user, the main session, Claude or the time limit keep their real end.
- [x] Bug: Codex commands recorded with under 1 ms (start-up only, ~64% of commands) take the span of the exec call that ran them when that call ran nothing else; otherwise their duration is unknown.
- [x] Numbers match the old dashboard on session 0c06d6c0 (Claude Code) and Codex chat 01a14abf, apart from the two bug fixes; screenshots before/after.

- [x] Publishable repo: Claude Code plugin (`.claude-plugin/`, `skills/session-profiler/`) next to the Codex skill, README, MIT licence; Claude Code reports kept out of `~/.codex` (`~/.cache/session-profiler/`).
- [ ] Published as a public GitHub repo `draconnn/session-profiler`; plugin installs from it in a clean Claude Code config.

## Assumed
- `timing.js` moves to `web/` unchanged apart from times shown in the viewer's own time zone (was hard-coded Europe/Warsaw).
- Plugin-only extras are dropped: Publish, Ask ChatGPT, edit pencil, card menus (export, links), card dragging.
- Card descriptions show as a line under each card title instead of an info tooltip.
- The server reports itself as `session-profiler` (still accepts the old `codex-session-profiler` name so running servers are replaced cleanly).
- The plugin template is never committed (`.gitignore`), so git history stays publishable.

## Pending decisions
(none)

## Later
- A malformed `#session=` address (bad %-escape) leaves the page blank; catch the decode error in `web/app.js`.
- A start-up-only Codex command whose record lands after its exec call's first output still shows 0 ms instead of unknown (1 case seen).
- `launch.py` does not check the Host header on HEAD requests (only static page headers exposed).
- A plain "was stopped" notice for a subagent's job that a timer killed (e.g. exactly 60 min after launch) is treated as cleanup, so two real 60-min jobs show an unknown end; consider a later notice for the same id, or round-hour caps.
- In subagent transcripts the notice is itself an entry, so two cleanup notices a few ms apart could make the second look not-quiet and get a days-long end (no live case yet); leave notice entries out of `entry_times`.
- The `duration_only` timing code has no label on the page.
- A Codex command record written late can land inside the next exec call's window and take that call's span (seen once).
- Synthetic test transcripts for the parsers; demo session and screenshots for the README and the X post.
