import test from 'node:test';
import assert from 'node:assert/strict';
import {unionMs,agentStats,toolGroups,packEvents,isJob,groupKey} from './timing.js';
test('elapsed union does not add parallel work or idle gaps',()=>{
  assert.equal(unionMs([[0,60000],[30000,120000],[180000,190000]]),130000);
  assert.equal(unionMs([[0,60000],[30000,120000]],[45000,90000]),45000);
});
test('turn scope clips tool durations and preserves unknown ends',()=>{
  const s=agentStats({id:'a',turns:[{startMs:0,endMs:10000},{startMs:20000,endMs:30000}],events:[
    {kind:'tool',label:'run',startMs:1000,endMs:8000},
    {kind:'tool',label:'run',startMs:2000,endMs:9000},
    {kind:'tool',label:'run',startMs:6000,endMs:null},
  ]},[5000,10000]);
  assert.equal(s.activeMs,5000);assert.equal(s.busyMs,4000);assert.equal(s.sumMs,7000);assert.equal(s.unknown,1);
  const g=toolGroups(s.calls)[0];assert.equal(g.count,3);assert.equal(g.unknown,1);assert.equal(g.maxMs,4000);
});
test('timeline gives overlapping calls different vertical tracks',()=>{
  const p=packEvents([{id:'a',startMs:0,endMs:10},{id:'b',startMs:2,endMs:5},{id:'c',startMs:5,endMs:9}]);
  assert.deepEqual(p.map(x=>x.track),[0,1,1]);
});
test('an open turn ends at its last own recorded event, not at the current clock',()=>{
  const s=agentStats({id:'a',turns:[{id:'open',startMs:1000,endMs:null}],events:[{kind:'tool',turnId:'open',startMs:3000,endMs:5000},{kind:'tool',turnId:'other',startMs:10000,endMs:20000}]},[0,30000]);
  assert.equal(s.activeMs,4000);assert.equal(s.turns[0].provisional,true);
});
test('an idle wait before a wake-up is shown but is not active, busy or summed action time',()=>{
  const s=agentStats({id:'a',turns:[{startMs:0,endMs:10000},{startMs:70000,endMs:75000,kind:'wake'}],events:[
    {kind:'tool',label:'run',startMs:2000,endMs:6000},
    {kind:'wait',label:'Idle until app message',idle:true,startMs:10000,endMs:70000},
  ]},[0,75000]);
  assert.equal(s.activeMs,15000);assert.equal(s.busyMs,4000);assert.equal(s.otherMs,11000);assert.equal(s.sumMs,4000);assert.equal(s.calls.length,2);
});

test('a background job is shown with the calls but is not busy or summed action time, and its overlaps count once',()=>{
  const s=agentStats({id:'a',turns:[{startMs:0,endMs:10000},{startMs:60000,endMs:70000,kind:'wake'}],events:[
    {kind:'tool',label:'Bash',startMs:1000,endMs:2000},
    {kind:'job',label:'Bash · background job',startMs:1000,endMs:60000},
    {kind:'job',label:'Bash · background job',startMs:30000,endMs:65000},
  ]},[0,70000]);
  assert.equal(s.calls.length,3);assert.equal(s.busyMs,1000);assert.equal(s.sumMs,1000);
  assert.equal(s.jobs.length,2);assert.equal(s.jobMs,64000);assert.equal(isJob(s.calls[1]),true);
});

test('background jobs rank by purpose, or by the start of their command without one',()=>{
  const g=toolGroups([
    {kind:'job',label:'Bash · background job',startMs:0,endMs:600000,scopedMs:600000,detail:{input:{description:'Build the player'},command:'bash tools/unity/build-player.sh'}},
    {kind:'job',label:'Bash · background job',startMs:0,endMs:300000,scopedMs:300000,detail:{input:{description:'Build the player'},command:'bash tools/unity/build-player.sh'}},
    {kind:'job',label:'Bash · background job',startMs:0,endMs:1000,scopedMs:1000,detail:{command:'blender --background --python x.py'}},
    {kind:'tool',label:'Bash',startMs:0,endMs:2000,scopedMs:2000},
  ]);
  assert.deepEqual(g.map(x=>[x.label,x.sumMs,x.kind]),[['Background job · Build the player',900000,'job'],['Bash',2000,'tool'],['Background job · blender --background --python x.py',1000,'job']]);
  assert.equal(groupKey({kind:'tool',label:'Read'}),'Read');
});
