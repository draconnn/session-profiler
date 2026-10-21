export const valid = n => typeof n === 'number' && Number.isFinite(n);

export function mergedIntervals(intervals, bounds = [-Infinity, Infinity]) {
  const ordered = intervals.filter(([a,b]) => valid(a) && valid(b) && b >= a)
    .map(([a,b]) => [Math.max(a,bounds[0]), Math.min(b,bounds[1])])
    .filter(([a,b]) => b > a).sort((a,b) => a[0]-b[0]);
  const out=[];
  for(const [a,b] of ordered) {
    const last=out.at(-1);
    if(last && a<=last[1]) last[1]=Math.max(last[1],b);
    else out.push([a,b]);
  }
  return out;
}
export const unionMs = (intervals,bounds) => mergedIntervals(intervals,bounds).reduce((s,[a,b])=>s+b-a,0);
export const overlaps = (a,b,bounds) => valid(a) && valid(b) && b>=bounds[0] && a<=bounds[1];
export const clipMs = (a,b,bounds) => valid(a)&&valid(b) ? Math.max(0,Math.min(b,bounds[1])-Math.max(a,bounds[0])) : null;
export function turnEnd(agent,turn) {
  if(valid(turn.endMs))return turn.endMs;
  if(valid(turn.observedEndMs))return turn.observedEndMs;
  return Math.max(turn.startMs||0,...(agent.events||[]).filter(e=>e.turnId===turn.id&&valid(e.endMs)).map(e=>e.endMs));
}
export function duration(ms) {
  if(!valid(ms)) return 'Unknown';
  if(ms<1000) return `${Math.round(ms)} ms`;
  const s=ms/1000;
  if(s<10) return `${s.toFixed(1)}s`;
  if(s<60) return `${Math.round(s)}s`;
  if(s<3600) return `${Math.floor(s/60)}m ${Math.floor(s%60)}s`;
  return `${Math.floor(s/3600)}h ${Math.floor(s%3600/60)}m`;
}
// Times are shown in the viewer's own time zone.
export const zoneName = Intl.DateTimeFormat().resolvedOptions().timeZone || 'local time';
export const clock = ms => valid(ms) ? new Date(ms).toLocaleTimeString('en-GB',{hour12:false}) : 'Unknown';
export const dateTime = ms => valid(ms) ? new Date(ms).toLocaleString('en-GB',{dateStyle:'medium',timeStyle:'medium'}) : 'Unknown';
export const excerpt = (v,n=110) => String(v??'').replace(/\s+/g,' ').slice(0,n);
export function actionText(event) {
  const value=event.detail?.fullCommand||event.detail?.command||event.summary||event.label||'';
  try {const command=JSON.parse(value);if(Array.isArray(command)&&command.length>=3&&['-c','-lc'].includes(command[1]))return String(command[2]);}catch{}
  return value;
}
export const isWait = e => e.kind==='wait' || /^(wait|wait_agent|wait_threads|sleep)$/.test(e.label?.split('.').at(-1)||'');
// A background job (a command left running after its call returned, timed to its
// completion notice) is shown with the calls but is neither busy nor summed action time.
export const isJob = e => e.kind==='job';
export const isCall = e => ['tool','wait','compaction','job'].includes(e.kind);
export function clippedEvents(agent,bounds) {
  return (agent.events||[]).filter(e=>isCall(e)&&overlaps(e.startMs,e.endMs??e.startMs,bounds))
    .map(e=>({...e,agentId:agent.id,agentName:agent.name,scopedMs:e.endMs==null?null:clipMs(e.startMs,e.endMs,bounds)}));
}
export function agentStats(agent,bounds) {
  const calls=clippedEvents(agent,bounds);
  const turns=(agent.turns||[]).map(t=>({...t,provisional:!valid(t.endMs),endMs:turnEnd(agent,t)})).filter(t=>overlaps(t.startMs,t.endMs,bounds));
  const intervals=turns.map(t=>[t.startMs,t.endMs??t.startMs]);
  const toolIntervals=calls.filter(e=>!e.idle&&!isJob(e)).map(e=>[e.startMs,e.endMs]);
  const jobs=calls.filter(isJob);
  const activeMs=unionMs(intervals,bounds);
  const busyMs=unionMs(toolIntervals,bounds);
  return {agent,calls,turns,activeMs,busyMs,otherMs:Math.max(0,activeMs-busyMs),
    sumMs:calls.reduce((s,e)=>s+(e.idle||isJob(e)?0:e.scopedMs??0),0), unknown:calls.filter(e=>e.scopedMs==null).length,
    jobs,jobMs:unionMs(jobs.map(e=>[e.startMs,e.endMs]),bounds)};
}
// What an action was for: the one-line purpose its call recorded, if any.
export function purpose(e) {
  const said=e.detail?.input?.description;
  return typeof said==='string'&&said.trim()?said.trim():'';
}
// The row an action counts toward in the time ranking: a background job by its
// purpose (or, without one, the start of its command), every other action by name.
export function groupKey(e) {
  if(!isJob(e)) return e.label||'Unnamed action';
  return `Background job · ${purpose(e)||excerpt(actionText(e),60)||'no purpose recorded'}`;
}
export function toolGroups(calls) {
  const groups=new Map();
  for(const e of calls) {
    const key=groupKey(e);
    if(!groups.has(key)) groups.set(key,{label:key,count:0,sumMs:0,maxMs:null,unknown:0,kind:e.kind});
    const g=groups.get(key);g.count++;
    if(valid(e.scopedMs)) {g.sumMs+=e.scopedMs;g.maxMs=Math.max(g.maxMs??0,e.scopedMs);} else g.unknown++;
  }
  return [...groups.values()].sort((a,b)=>b.sumMs-a.sumMs);
}
export function packEvents(events) {
  const ends=[];
  return [...events].sort((a,b)=>a.startMs-b.startMs).map(e=>{
    let track=ends.findIndex(end=>end<=e.startMs);
    if(track<0)track=ends.length;
    ends[track]=e.endMs??e.startMs;
    return {...e,track};
  });
}
