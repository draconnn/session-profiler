// Session profiler report page: plain JavaScript, no build step.
// It reads data/profile.json (written by launch.py) and renders the timing views.
import {actionText,agentStats,clock,dateTime,duration,excerpt,groupKey,isJob,isWait,overlaps,packEvents,purpose,toolGroups,unionMs,valid,zoneName} from './timing.js';

const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const hue = e => e.kind==='compaction'?'compact':isJob(e)?'job':isWait(e)?'wait':'tool';
const timingLabel = t => ({native_duration:'Recorded execution duration',native_interval:'Recorded start and end',native_timestamps:'Recorded start and end',recorded_duration:'Recorded execution duration',paired_timestamps:'Paired call and result timestamps',transcript_pair:'Paired call and result timestamps',notification_pair:'From launch to its completion notice',cell_pair:'From launch to the wait that saw it finish',exec_span:'From the exec call to its output (Codex recorded only the start-up time)',startup_only:'Unknown: Codex recorded only the start-up time, and the command shared its exec call with other actions',inferred:'Estimated from transcript timestamps'}[t]||t||'Timing provenance unavailable');
const keyOf = e => `${e.agentId}\u001f${e.id}`;
const PAGE = 40;

const state = {profile:null, sessionId:'', turnId:'all', agentScope:'all', selectedAgent:null, selectedEvent:null,
  zoom:1, search:'', toolFilter:'all', sort:'duration', limit:PAGE, openRows:new Set(), busy:false};
let view = {};

// ---- data -----------------------------------------------------------------

function derive() {
  const p = state.profile, loaded = p.sessions || [];
  const session = p.details?.[state.sessionId];
  const sessions = [...loaded, ...(p.inventory || []).filter(r => r.available && !r.parentId && !loaded.some(s => s.id === r.id))
    .sort((a,b) => (b.updatedAtMs||b.createdAtMs||0) - (a.updatedAtMs||a.createdAtMs||0)).map(r => ({...r, startMs:r.createdAtMs}))];
  const agents = session?.agents || [], root = agents.find(a => a.id === state.sessionId) || agents[0];
  const rootTurns = root?.turns || [], turn = rootTurns.find(t => t.id === state.turnId);
  const bounds = [turn?.startMs ?? session?.startMs ?? 0, turn?.endMs ?? session?.endMs ?? 1];
  const scoped = agents.filter(a => (state.agentScope==='all' || a.id===state.agentScope) && overlaps(a.startMs, a.endMs, bounds));
  const stats = scoped.map(a => agentStats(a, bounds));
  const calls = stats.flatMap(s => s.calls), groups = toolGroups(calls.filter(e => !e.idle));
  view = {session, sessions, agents, root, rootTurns, turn, bounds, scoped, stats, calls, groups,
    app: session?.source === 'claude-code' ? 'Claude' : 'Codex',
    byKey: new Map(calls.map(e => [keyOf(e), e])), marks: [], turnMarks: []};
}

function selectedAgent() { return view.agents.find(a => a.id === state.selectedAgent) || view.root; }
function selectedEvent() { return state.selectedEvent ? view.byKey.get(state.selectedEvent) : null; }

// ---- rendering --------------------------------------------------------------

function renderAll() {
  derive();
  $('generated').textContent = state.profile.generatedAt ? `Read from local logs ${dateTime(Date.parse(state.profile.generatedAt))} · runs on your machine, read only` : '';
  renderControls();
  if (!view.session) {
    $('heading').innerHTML = '<p class="sp-empty">No session snapshot is loaded yet.</p>';
    for (const id of ['summary','timeline','ranking','inspector','explorer']) $(id).innerHTML = '';
    return;
  }
  renderHeading(); renderSummary(); renderTimeline(); renderRanking(); renderInspector(); renderFilterOptions(); renderExplorer(); renderMethods();
}

function options(choices, value) {
  return choices.map(([v,l]) => `<option value="${esc(v)}"${v===value?' selected':''}>${esc(l)}</option>`).join('');
}

function renderControls() {
  const {sessions, agents, rootTurns} = view, prompts = rootTurns.filter(t => t.kind !== 'wake');
  $('session').innerHTML = options(sessions.map(s => [s.id, `${dateTime(s.startMs)} · ${excerpt(s.title,85)}${s.agentCount?` · ${s.agentCount} agents`:''}`]), state.sessionId);
  $('turn').innerHTML = options([['all','All prompts'], ...rootTurns.map(t => [t.id, t.kind==='wake'
    ? `Wake-up · ${clock(t.startMs)} · ${excerpt(String(t.prompt||'').replace(/^Wake-up: /,''),90)}`
    : `Prompt ${prompts.indexOf(t)+1} · ${clock(t.startMs)} · ${excerpt(t.prompt,90)}`])], state.turnId);
  $('agent').innerHTML = options([['all','All agents'], ...agents.map(a => [a.id, a.name])], state.agentScope);
  $('refresh').disabled = state.busy;
  $('refresh').textContent = state.busy ? 'Reading logs…' : 'Refresh logs';
}

function renderHeading() {
  const {session, bounds, turn} = view;
  $('heading').innerHTML = `<div class="sp-session-heading"><div><h1>${esc(excerpt(session.title,160))}</h1><span>${esc(dateTime(bounds[0]))} — ${esc(dateTime(bounds[1]))} · ${esc(zoneName)}</span></div><span class="sp-local-badge">Local session history</span></div>`
    + (turn ? `<p class="sp-prompt" title="${esc(turn.prompt)}">${esc(excerpt(turn.prompt,420))}</p>` : '');
}

function stat(label, value, note) {
  return `<div class="sp-stat"><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(note)}</small></div>`;
}

function renderSummary() {
  const {stats, calls, bounds, scoped, root, rootTurns, app} = view;
  const turnSpans = stats.flatMap(s => s.turns.map(t => [t.startMs, t.endMs]));
  const activeMs = unionMs(turnSpans, bounds), unknown = calls.filter(e => !valid(e.scopedMs)).length;
  const jobs = calls.filter(isJob), jobSpans = jobs.map(e => [e.startMs, e.endMs]), openJobs = jobs.filter(e => !valid(e.scopedMs)).length;
  // The time range in three parts that add up to it: the agent working (active turns),
  // only background jobs running, and nothing running (waiting for the user, idle).
  const rangeMs = Math.max(0, bounds[1]-bounds[0]), jobMs = unionMs(jobSpans, bounds), eitherMs = unionMs([...turnSpans, ...jobSpans], bounds);
  const jobsOnlyMs = eitherMs - activeMs, quietMs = Math.max(0, rangeMs - eitherMs);
  const callMs = unionMs(calls.filter(e => !e.idle && !isJob(e)).map(e => [e.startMs, e.endMs]), bounds);
  const prompts = rootTurns.filter(t => t.kind !== 'wake').length;
  $('summary').innerHTML = `<div class="sp-stats">${[
    stat('Time range', duration(rangeMs), 'First to last recorded activity; the next three add up to it'),
    stat(`${app} working`, duration(activeMs), `Active turns, overlaps once; tool calls ${duration(callMs)} of it`),
    stat('Only background jobs running', duration(jobsOnlyMs), `${app} idle while builds, tests or reels ran`),
    stat('Nothing running', duration(quietMs), 'Waiting for you, or idle'),
    stat('Subagents', String(scoped.filter(a => a.id !== root?.id).length), `${prompts} recorded prompts in session`),
    stat('Recorded actions', String(calls.length), unknown ? `${unknown} have unknown duration` : 'Tools, waits and compactions'),
  ].join('')}</div><p class="sp-jobs-note">Background jobs: ${jobs.length} ran for ${esc(duration(jobMs))} in all (overlaps once), ${esc(duration(Math.max(0, jobMs-jobsOnlyMs)))} of it while ${app} was also working${openJobs ? `; ${openJobs} have no recorded end` : ''}.</p>`;
}

function renderTimeline() {
  const {stats, bounds} = view, span = Math.max(1, bounds[1]-bounds[0]);
  const pos = t => Math.max(0, Math.min(100, 100*(t-bounds[0])/span));
  const byId = new Map(view.agents.map(a => [a.id, a]));
  const depth = id => { let d = 0, a = byId.get(id); const seen = new Set();
    while (a?.parentId && byId.has(a.parentId) && !seen.has(a.id)) { seen.add(a.id); d++; a = byId.get(a.parentId); } return d; };
  const selAgent = selectedAgent()?.id, selEvent = state.selectedEvent;
  view.marks = []; view.turnMarks = [];
  const ticks = [0,1,2,3,4,5,6];
  const lanes = stats.filter(s => overlaps(s.agent.startMs, s.agent.endMs, bounds)).map(s => {
    const {agent, turns, calls, activeMs} = s, packed = packEvents(calls);
    const tracks = Math.max(1, ...packed.map(e => e.track+1)), height = 48 + Math.min(tracks, 8)*10;
    const lifespan = valid(agent.startMs) && valid(agent.endMs)
      ? `<div class="sp-lifespan" style="left:${pos(agent.startMs)}%;width:${Math.max(.03, pos(agent.endMs)-pos(agent.startMs))}%"></div>` : '';
    const turnHtml = turns.map((t,i) => {
      const n = view.turnMarks.push({agent, turn:t}) - 1;
      return `<button type="button" class="sp-active${agent.parentId?'':' root'}" data-turn="${n}" style="left:${pos(t.startMs)}%;width:${Math.max(.04, pos(t.endMs??t.startMs)-pos(t.startMs))}%" aria-label="${esc(`${agent.name} active turn ${i+1}`)}"></button>`;
    }).join('');
    const eventHtml = packed.map(e => {
      const n = view.marks.push(e) - 1;
      return `<button type="button" class="sp-event ${hue(e)}${selEvent===keyOf(e)?' selected':''}${e.endMs==null?' unknown':''}" data-ev="${n}" style="left:${pos(e.startMs)}%;width:max(2px, ${Math.max(0, pos(e.endMs??e.startMs)-pos(e.startMs))}%);top:${34+Math.min(e.track,7)*10}px" aria-label="${esc(`${e.label}, ${duration(e.scopedMs)}, ${agent.name}`)}"></button>`;
    }).join('');
    return `<div class="sp-lane${selAgent===agent.id?' selected':''}" style="min-height:${height}px">
      <button type="button" class="sp-lane-name" data-agent="${esc(agent.id)}" style="padding-left:${Math.min(depth(agent.id),3)*10}px" title="${esc(`${agent.name}\n${agent.task||''}`)}"><i class="${agent.parentId?'child':'root'}"></i><span><b>${esc(agent.name)}</b><small>${esc(duration(activeMs))} active · ${calls.length} actions</small></span></button>
      <div class="sp-lane-track" style="height:${height}px"><div class="sp-grid-lines">${ticks.map(i => `<i style="left:${i*100/6}%"></i>`).join('')}</div>${lifespan}${turnHtml}${eventHtml}${tracks>8?`<span class="sp-dense">${tracks} overlapping tracks; inspect actions below</span>`:''}</div></div>`;
  }).join('');
  const legend = [['active','Active turn'],['tool','Tool'],['job','Background job'],['wait','Wait'],['compact','Compaction'],['lifespan','Agent lifespan']]
    .map(([c,l]) => `<span><i class="${c}"></i>${l}</span>`).join('');
  const zoom = [1,2,4,8,16].map(n => `<button type="button" data-zoom="${n}" class="${state.zoom===n?'on':''}" aria-pressed="${state.zoom===n}">${n}×</button>`).join('');
  $('timeline').innerHTML = `<div class="sp-timeline-toolbar"><div class="sp-legend">${legend}</div><div class="sp-zoom"><span>Zoom</span>${zoom}</div></div>
    <div class="sp-timeline-scroll"><div class="sp-timeline" style="min-width:${state.zoom*100}%"><div class="sp-axis"><span>${esc(zoneName)}</span><div>${ticks.map(i => `<span style="left:${i*100/6}%">${esc(clock(bounds[0]+span*i/6))}</span>`).join('')}</div></div>${lanes}</div></div>
    <div class="sp-chart-footer"><span>Click an agent or tool to inspect it. Short marks have a minimum width of 2 pixels.</span><span>${esc(duration(span))} shown</span></div>`;
}

function renderRanking() {
  const {groups} = view, top = Math.max(1, groups[0]?.sumMs || 0);
  $('ranking').innerHTML = groups.length ? `<div class="sp-tools">${groups.slice(0,12).map((g,i) =>
    `<button type="button" data-rank="${i}" class="${state.toolFilter===g.label?'selected':''}"><span class="sp-tool-label${g.kind==='job'?' job':''}" title="${esc(g.label)}">${esc(g.kind==='job'?g.label.replace(/^Background job · /,''):g.label)}</span><div class="sp-tool-meter"><i class="${g.kind==='wait'?'wait':g.kind==='job'?'job':''}" style="width:${g.sumMs/top*100}%"></i></div><strong>${esc(duration(g.sumMs))}</strong><span>${g.count} calls${g.unknown?` · ${g.unknown} unknown`:''}</span></button>`).join('')}</div>`
    : '<p class="sp-empty">No actions in this scope.</p>';
}

function renderInspector() {
  const agent = selectedAgent(), event = selectedEvent(), bounds = view.bounds;
  if (!agent) { $('inspector').innerHTML = '<p class="sp-empty">Select an agent or action.</p>'; return; }
  const stats = agentStats(agent, bounds);
  const facts = event
    ? [['Duration in scope', duration(event.scopedMs)], ['Started', clock(event.startMs)], ['Ended', clock(event.endMs)], ['Agent', agent.name]]
    : [['Active time', duration(stats.activeMs)], ['Actions', stats.calls.length], ['Action busy time', duration(stats.busyMs)], ['Other active time', duration(stats.otherMs)]];
  let body = `<div class="sp-inspector-top"><span>${event?'Selected action':'Selected agent'}</span>${event?'<button type="button" data-back>Back to agent</button>':''}</div>
    <h3>${esc(event?.label || agent.name)}</h3><div class="sp-badges"><span>${esc(event?.status || agent.status || 'Recorded')}</span>${agent.model?`<span>${esc(agent.model)}</span>`:''}</div>
    <dl class="sp-facts">${facts.map(([l,v]) => `<div><dt>${esc(l)}</dt><dd>${esc(v)}</dd></div>`).join('')}</dl>`;
  if (event) {
    const detail = event.detail && event.detail !== event.summary ? (typeof event.detail === 'string' ? event.detail : JSON.stringify(event.detail, null, 2)) : '';
    body += `<p class="sp-muted">${esc(timingLabel(event.timing))}${event.durationMs!==event.scopedMs && valid(event.durationMs) ? ` · full duration ${esc(duration(event.durationMs))}` : ''}</p>
      <div class="sp-command-heading"><h4>Action</h4><button type="button" class="sp-button sp-small" data-full>View full command</button></div><pre class="sp-code">${esc(actionText(event))}</pre>
      ${detail ? `<h4>Recorded detail</h4><pre class="sp-code">${esc(detail)}</pre>` : ''}
      ${event.source ? `<details class="sp-source"><summary>Source record</summary><code>${esc(event.source.file)}<br>Line ${esc(event.source.line)} · ${esc(event.source.type)}</code></details>` : ''}`;
  } else {
    const messages = (agent.messages || []).filter(m => m.atMs >= bounds[0] && m.atMs <= bounds[1]);
    const last = messages.filter(m => m.role === 'assistant').at(-1), first = messages.find(m => m.role === 'assistant');
    const encrypted = agent.taskAvailability === 'encrypted' || (agent.warnings || []).some(w => String(w).includes('spawn prompt encrypted'));
    const slow = [...stats.calls].sort((a,b) => (b.scopedMs??-1) - (a.scopedMs??-1)).slice(0,5);
    body += '<p class="sp-muted">Other active time includes model work, transport, orchestration and anything the log does not attribute to a tool.</p>'
      + (!encrypted ? `<h4>Assigned task</h4><p class="sp-task">${esc(agent.task || 'Task text unavailable in local history.')}</p>` : '')
      + (encrypted && first && first !== last ? `<h4>First recorded update</h4><p class="sp-task">${esc(first.text)}</p>` : '')
      + (last ? `<h4>Last recorded response</h4><p class="sp-task">${esc(last.text)}</p>` : '')
      + (slow.length ? `<h4>Slowest actions</h4><div class="sp-inspector-actions">${slow.map(e => `<button type="button" data-key="${esc(keyOf(e))}"><span>${esc(e.label)}</span><b>${esc(duration(e.scopedMs))}</b></button>`).join('')}</div>` : '')
      + (messages.length ? `<details class="sp-messages"><summary>${messages.length} recorded messages in scope</summary>${messages.map(m => `<div><span>${esc(m.role)} · ${esc(clock(m.atMs))}</span><p>${esc(m.text)}</p></div>`).join('')}</details>` : '');
  }
  $('inspector').innerHTML = `<div class="sp-inspector-body">${body}</div>`;
}

function renderFilterOptions() {
  $('filter').innerHTML = options([['all','All actions'], ...view.groups.map(g => [g.label, g.label])], state.toolFilter);
  $('search').value = state.search;
  $('sort').value = state.sort;
}

function whatItDid(e, open) {
  const said = purpose(e), command = actionText(e) || (typeof e.detail === 'string' ? e.detail : '');
  const long = command.length > 240 || command.split('\n').length > 3;
  return `<div class="sp-what${open?' open':''}">${said?`<b>${esc(said)}</b>`:''}${command && command !== said ? `<code>${esc(command)}</code>` : ''}${!said && !command ? '<span>No description recorded</span>' : ''}${long ? `<button type="button" class="sp-what-toggle" data-toggle="${esc(keyOf(e))}">${open?'Show less':'Show the whole command'}</button>` : ''}</div>`;
}

function filteredCalls() {
  const q = state.search.toLowerCase();
  return view.calls.filter(e => (state.toolFilter === 'all' || groupKey(e) === state.toolFilter)
      && (!q || `${e.label} ${e.summary} ${purpose(e)} ${actionText(e)} ${e.agentName}`.toLowerCase().includes(q)))
    .sort((a,b) => state.sort === 'time' ? a.startMs - b.startMs : (b.scopedMs ?? -1) - (a.scopedMs ?? -1));
}

function renderExplorer() {
  const rows = filteredCalls(), shown = rows.slice(0, state.limit);
  const body = shown.map(e => { const k = keyOf(e);
    return `<tr class="${state.selectedEvent===k?'selected':''}"><td>${esc(clock(e.startMs))}</td><td><button type="button" class="sp-duration-link" data-key="${esc(k)}">${esc(duration(e.scopedMs))}</button></td><td title="${esc(e.agentName)}">${esc(excerpt(e.agentName,35))}</td><td><button type="button" class="sp-action-link" data-key="${esc(k)}">${esc(e.label)}</button>${e.status?`<small>${esc(e.status)}</small>`:''}</td><td>${whatItDid(e, state.openRows.has(k))}</td></tr>`;
  }).join('');
  $('explorer').innerHTML = `<div class="sp-table-scroll"><table class="sp-action-table"><thead><tr>${['Started','Duration','Agent','Action','What it did'].map(t => `<th>${t}</th>`).join('')}</tr></thead><tbody>${body}</tbody></table></div>
    ${rows.length ? '' : '<p class="sp-empty">No actions match these filters.</p>'}
    <div class="sp-table-footer"><span>${Math.min(state.limit, rows.length)} of ${rows.length} actions</span>${state.limit < rows.length ? '<button type="button" data-more>Show 100 more</button>' : ''}${state.search || state.toolFilter !== 'all' ? '<button type="button" data-clear>Clear action filters</button>' : ''}</div>`;
}

function renderMethods() {
  const {session, app} = view;
  $('methods').innerHTML = `<summary>Timing definitions and coverage</summary>
    <p>The time range splits into three parts that add up to it: ${app} working (recorded turn intervals across selected agents, overlaps once), only background jobs running (a command left running after its call returned, from launch to its completion notice, while no turn was active), and nothing running. Agent lifespans can include idle time. Summed action durations in Time by action can exceed elapsed time when work runs in parallel. Prompt and Agent filters apply to the summary, timeline and rankings; timeline zoom only changes the viewport.</p>
    <p>Observed durations do not establish why work was slow. Transcript-paired timings can include transport and scheduling overhead. Action busy time is the union of action intervals. Other active time is not a measurement of model inference. Missing ends have unknown duration. Open turns run only through the last recorded event.</p>
    <p>Snapshot: ${esc(state.profile.generatedAt)}. Task text and responses are bounded excerpts. No raw reasoning is included. Some historical sessions or child logs may be unavailable locally.</p>
    ${session.warnings?.length ? `<ul>${session.warnings.map(w => `<li>${esc(typeof w === 'string' ? w : JSON.stringify(w))}</li>`).join('')}</ul>` : ''}`;
}

// ---- selection --------------------------------------------------------------

function revealInspector() {
  requestAnimationFrame(() => $('inspector-card').scrollIntoView({block:'nearest', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'}));
}

function markSelection() {
  for (const el of document.querySelectorAll('.sp-event.selected')) el.classList.remove('selected');
  if (state.selectedEvent) {
    const n = view.marks.findIndex(e => keyOf(e) === state.selectedEvent);
    if (n >= 0) document.querySelector(`.sp-event[data-ev="${n}"]`)?.classList.add('selected');
  }
  const agentId = selectedAgent()?.id;
  for (const lane of document.querySelectorAll('.sp-lane')) lane.classList.toggle('selected', lane.querySelector('.sp-lane-name')?.dataset.agent === agentId);
}

function selectAgent(id) { state.selectedAgent = id; state.selectedEvent = null; renderInspector(); markSelection(); renderExplorer(); revealInspector(); }
function selectEvent(key) {
  const e = view.byKey.get(key); if (!e) return;
  state.selectedAgent = e.agentId; state.selectedEvent = key; renderInspector(); markSelection(); renderExplorer(); revealInspector();
}

// ---- server actions ---------------------------------------------------------

function notice(text) { const n = $('notice'); n.textContent = text || ''; n.hidden = !text; }

function reloadAt(sessionId) { location.hash = `session=${encodeURIComponent(sessionId)}`; location.reload(); }

async function post(route, sessionId, failure) {
  state.busy = true; notice(''); renderControls();
  try {
    const r = await fetch(route, {method:'POST', cache:'no-store', headers:{'Content-Type':'application/json'}, body:JSON.stringify({sessionId})});
    if (!r.ok) throw Error(failure);
    reloadAt(sessionId);
  } catch (e) {
    notice(e.message); state.busy = false; renderControls();
  }
}

function switchSession(id) {
  if (!state.profile.details?.[id]) { post('/api/open', id, 'Could not open this session. Run the profiler again for it.'); return; }
  Object.assign(state, {sessionId:id, turnId:'all', agentScope:'all', selectedAgent:null, selectedEvent:null, zoom:1, search:'', toolFilter:'all', limit:PAGE});
  state.openRows.clear(); renderAll();
}

// ---- tooltip ----------------------------------------------------------------

function showTip(el) {
  let label, description;
  if (el.dataset.ev) { const e = view.marks[+el.dataset.ev]; label = `${e.label} · ${duration(e.scopedMs)}`; description = actionText(e); }
  else { const {agent, turn} = view.turnMarks[+el.dataset.turn]; label = `${agent.name} · ${duration((turn.endMs??turn.startMs)-turn.startMs)}`; description = turn.prompt || agent.task; }
  const tip = $('tip'), r = el.getBoundingClientRect(), width = Math.min(520, innerWidth-32);
  tip.innerHTML = `<strong>${esc(label)}</strong><span class="sp-hover-command">${esc(description || 'No description recorded')}</span><small>Click to inspect this action and read the full command.</small>`;
  Object.assign(tip.style, {left:`${Math.max(16, Math.min(r.left, innerWidth-width-16))}px`, top:`${Math.max(16, Math.min(r.bottom+10, innerHeight-230))}px`, width:`${width}px`});
  tip.hidden = false; el.setAttribute('aria-describedby', 'tip');
}
function hideTip(el) { $('tip').hidden = true; el?.removeAttribute('aria-describedby'); }

// ---- events -----------------------------------------------------------------

function wire() {
  $('session').addEventListener('change', e => switchSession(e.target.value));
  $('turn').addEventListener('change', e => { Object.assign(state, {turnId:e.target.value, selectedEvent:null, zoom:1, limit:PAGE}); renderAll(); });
  $('agent').addEventListener('change', e => { const id = e.target.value; Object.assign(state, {agentScope:id, selectedEvent:null}); if (id !== 'all') state.selectedAgent = id; renderAll(); });
  $('refresh').addEventListener('click', () => post('/api/refresh', state.sessionId, 'Live refresh is unavailable. Run the profiler again to read the logs.'));
  $('search').addEventListener('input', e => { state.search = e.target.value; state.limit = PAGE; renderExplorer(); });
  $('filter').addEventListener('change', e => { state.toolFilter = e.target.value; state.limit = PAGE; renderRanking(); renderExplorer(); });
  $('sort').addEventListener('change', e => { state.sort = e.target.value; renderExplorer(); });
  $('command-close').addEventListener('click', () => $('command-dialog').close());

  document.addEventListener('click', e => {
    const t = e.target.closest('button'); if (!t) return;
    const d = t.dataset;
    if (d.agent) selectAgent(d.agent);
    else if (d.turn) { hideTip(t); selectAgent(view.turnMarks[+d.turn].agent.id); }
    else if (d.ev) { hideTip(t); selectEvent(keyOf(view.marks[+d.ev])); }
    else if (d.key) selectEvent(d.key);
    else if (d.zoom) { state.zoom = +d.zoom; renderTimeline(); }
    else if (d.rank) { const g = view.groups[+d.rank]; state.toolFilter = state.toolFilter === g.label ? 'all' : g.label; state.limit = PAGE; renderRanking(); $('filter').value = state.toolFilter; renderExplorer(); }
    else if (d.toggle) { state.openRows.has(d.toggle) ? state.openRows.delete(d.toggle) : state.openRows.add(d.toggle); renderExplorer(); }
    else if ('more' in d) { state.limit += 100; renderExplorer(); }
    else if ('clear' in d) { state.search = ''; state.toolFilter = 'all'; renderFilterOptions(); renderRanking(); renderExplorer(); }
    else if ('back' in d) { state.selectedEvent = null; renderInspector(); markSelection(); renderExplorer(); }
    else if ('full' in d) { $('command-text').textContent = actionText(selectedEvent()); $('command-dialog').showModal(); }
  });
  const timeline = $('timeline'), mark = e => e.target.closest?.('.sp-event, .sp-active');
  timeline.addEventListener('pointerover', e => { const m = mark(e); if (m) showTip(m); });
  timeline.addEventListener('pointerout', e => { const m = mark(e); if (m && !m.contains(e.relatedTarget)) hideTip(m); });
  timeline.addEventListener('focusin', e => { const m = mark(e); if (m) showTip(m); });
  timeline.addEventListener('focusout', e => { const m = mark(e); if (m) hideTip(m); });
  timeline.addEventListener('keydown', e => { if (e.key === 'Escape') { hideTip(mark(e)); e.stopPropagation(); } });
  timeline.addEventListener('scroll', () => hideTip(), true);
}

async function main() {
  wire();
  try {
    const r = await fetch('data/profile.json', {cache:'no-store'});
    if (!r.ok) throw Error('The report data is missing. Run the profiler again to read the logs.');
    state.profile = await r.json();
  } catch (e) {
    notice(e.message); return;
  }
  const fromHash = decodeURIComponent((location.hash.match(/session=([^&]+)/) || [])[1] || '');
  const p = state.profile;
  state.sessionId = (fromHash && p.details?.[fromHash] ? fromHash : '') || p.selectedSessionId || p.sessions?.[0]?.id || '';
  renderAll();
}

main();
