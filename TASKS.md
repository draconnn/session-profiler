# Standalone dashboard (option A)

## Goal
The profiler's report page runs without any code from the Codex Data Analytics plugin (proprietary), so the whole skill can be open-sourced. Done = `/session-profiler` in Codex or Claude Code works as today and opens the same report page, and everything in the skill folder is the user's own code or openly licensed code.

## Milestones
- [x] Standalone report page in `assets/profiler/web/` (plain HTML, CSS, JavaScript; no build step, no Node.js), with the same sections as today: top bar (session, prompt and agent pickers, Refresh), summary figures and background-jobs line, timeline (agent lanes, zoom 1-16x, legend, click to inspect), Time by action (click filters the explorer), Work detail panel (agent and action views, full command), Action explorer (search, filter, sort, show more, expandable "what it did"), timing definitions. Follows the system light/dark mode.
- [x] `launch.py` and `scripts/profile_session.py` serve the page and `data/profile.json` directly; no plugin build, no Node.js; old runtimes upgrade cleanly; plugin template (`assets/profiler/app/`) deleted.
- [x] Bug: a background job killed or stopped after its session went quiet (e.g. by app cleanup days later) gets an unknown end instead of a duration to the kill notice (was 75 h).
- [x] Bug: Codex commands recorded with under 1 ms (start-up only, ~64% of commands) take the span of the exec call that ran them when that call ran nothing else; otherwise their duration is unknown.
- [x] Numbers match the old dashboard on session 0c06d6c0 (Claude Code) and Codex chat 01a14abf, apart from the two bug fixes; screenshots before/after.

## Assumed
- `timing.js` moves to `web/` unchanged apart from times shown in the viewer's own time zone (was hard-coded Europe/Warsaw).
- Plugin-only extras are dropped: Publish, Ask ChatGPT, edit pencil, card menus (export, links), card dragging.
- Card descriptions show as a line under each card title instead of an info tooltip.
- The server reports itself as `session-profiler` (still accepts the old `codex-session-profiler` name so running servers are replaced cleanly).
- The plugin template is never committed (`.gitignore`), so git history stays publishable.

## Pending decisions
(none)

## Later
- Publishing: one repo with both skill entries and one-command installs, README, MIT licence, synthetic test transcripts, demo session and screenshots, X post.
