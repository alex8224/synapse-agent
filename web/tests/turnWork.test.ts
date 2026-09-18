import assert from 'node:assert/strict';
import { beforeEach, test } from 'node:test';
import { mapHistoryEvents, historyToolItem, type TranscriptMessage } from '../src/stores/historyMapper.ts';
import { workGroups, workSeconds, formatWorkDuration, bindWorkTurn, getGroupIntentStatus, rowPaints } from '../src/stores/turnWork.ts';
import { reduceRuntimeEvent, type LiveReducibleState } from '../src/stores/liveEventReducer.ts';
import { readTranscriptViews, restoreTranscriptViews, saveTranscriptViews, clearTranscriptViews } from '../src/stores/transcriptCache.ts';
import type { HistoryEvent, RuntimeEvent } from '../src/client/types.ts';

const session = { project_id: 'p', thread_id: 's' };
const data = new Map<string, string>();
const storage = {
  get length() { return data.size; }, key: (i: number) => [...data.keys()][i] ?? null,
  getItem: (k: string) => data.get(k) ?? null, setItem: (k: string, v: string) => { data.set(k, v); },
  removeItem: (k: string) => { data.delete(k); }, clear: () => data.clear(),
};
beforeEach(() => { Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: storage }); data.clear(); });
const row = (id: string, type: TranscriptMessage['type'], extra: Partial<TranscriptMessage> = {}): TranscriptMessage => ({ id, type, timestamp: '', ...extra });
const user = (id = 'u', turnId = 'A') => row(id, 'user', { turnId, work: { startedAt: 1000, ended: false } });
const history = (extra: Partial<HistoryEvent> = {}) => mapHistoryEvents([
  { kind: 'user', text: 'prompt', tool_calls: [], tool_results: [], turn_id: 'A', elapsed_s: 19.8, attachments: [], ...extra },
  { kind: 'tools', text: '', tool_calls: [{ id: 'c1', name: 'execute', args: { intent: 'run checks' } }], tool_results: [{ id: 'c1', name: 'execute', status: 'error', content: 'failed output' }], attachments: [] },
] as HistoryEvent[], { startTurn: 1, pageTag: 'latest' });
function base(messages = [user()]): LiveReducibleState {
  return { messages, activeTurnId: 'A', runtimeStatus: 'running', steerQueueCount: 0, pendingApproval: null, activity: null, usage: null, metricsLabel: '' };
}
function apply(s: LiveReducibleState, kind: string, turn = 'A', payload: unknown = {}, at = 21000) {
  return { ...s, ...reduceRuntimeEvent(s, { turn_id: turn, kind, sequence: 1, turn_sequence: 1, payload } as RuntimeEvent, () => new Date(at)) };
}

test('one clock/header across reasoning, narration, tools, warning and final answer', () => {
  const messages = [user(), row('t','thought'), row('a','assistant'), row('x','tool_group', { tools: [historyToolItem('i','execute')] }), row('w','info'), row('t2','thought'), row('a2','assistant')];
  const groups = workGroups(messages, 'A', true);
  assert.equal(groups.length, 1);
  assert.deepEqual(groups[0].rows.map((m) => m.id), ['t','x','t2']);
  assert.equal(workSeconds(groups[0], 20000), 19);
  assert.equal(groups[0].anchor.id, 'u');
});
test('live narration between tool batches never splits or resets the turn clock', () => {
  let state = base([row('pending', 'user', { work: { startedAt: 1000, ended: false } })]);
  state.activeTurnId = null;
  const steps: Array<[string, Record<string, unknown>, number]> = [
    ['activity_started', {}, 1000],
    ['reasoning_delta', { text: 'inspect changes' }, 2000],
    ['reasoning_completed', { text: 'inspect changes' }, 3000],
    ['tool_batch_started', {}, 4000],
    ['tool_started', { item_id: 'i1', call_id: 'c1', name: 'execute' }, 5000],
    ['tool_batch_finished', {}, 160000],
    ['answer_delta', { text: 'Checks reviewed; running tests next.' }, 167000],
    ['answer_completed', { text: 'Checks reviewed; running tests next.' }, 168000],
    ['activity_updated', { reset_timer: true, phase: 'model' }, 169000],
    ['tool_batch_started', {}, 170000],
    ['tool_started', { item_id: 'i2', call_id: 'c2', name: 'execute' }, 171000],
    ['tool_batch_finished', {}, 184000],
    ['answer_completed', { text: 'Changes committed.' }, 185000],
  ];
  for (const [kind, payload, at] of steps) {
    state = apply(state, kind, 'A', payload, at);
    const groups = workGroups(state.messages, state.activeTurnId, state.runtimeStatus === 'running');
    assert.equal(groups.length, 1, kind);
    assert.equal(groups[0].anchor.id, 'pending', kind);
    assert.equal(groups[0].running, true, kind);
    assert.equal(workSeconds(groups[0], at), (at - 1000) / 1000, kind);
  }
  state = apply(state, 'turn_completed', 'A', { elapsed_s: 184.5 }, 185500);
  const groups = workGroups(state.messages, state.activeTurnId, false);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].running, false);
  assert.equal(workSeconds(groups[0], 999999), 184);
  assert.deepEqual(groups[0].rows.map((m) => m.type), ['thought', 'tool_group', 'tool_group']);
  assert.deepEqual(state.messages.filter((m) => m.type === 'assistant').map((m) => m.content), [
    'Checks reviewed; running tests next.', 'Changes committed.',
  ]);
  assert.ok(state.messages.every((m) => m.turnId === 'A'));
});
test('steer stays in same work group and an empty batch cannot hide the pending header', () => {
  const groups = workGroups([user(), row('x','tool_group', { turnId:'A', tools:[] }), row('steer','user', { turnId:'A', steer:true })], 'A', true);
  assert.equal(groups.length, 1); assert.equal(groups[0].rows.length, 0);
  assert.equal(workSeconds(groups[0], 21000), 20);
});
test('refresh keeps authoritative completed duration, tool result, error and intent', () => {
  const messages = history();
  assert.equal(workSeconds(workGroups(messages, null, false)[0], 999999), 19);
  assert.equal(messages[1].tools?.[0].preview, 'failed output');
  assert.equal(messages[1].tools?.[0].error, true);
  assert.equal(messages[1].tools?.[0].label, 'run checks');
});
test('history keys survive page changes and old history does not invent seconds', () => {
  const events = [{ kind:'user',text:'hi',tool_calls:[],tool_results:[] }] as HistoryEvent[];
  const a = mapHistoryEvents(events, { startTurn:21,pageTag:'latest' });
  const b = mapHistoryEvents(events, { startTurn:21,pageTag:'earlier-21' });
  assert.equal(a[0].id,b[0].id);
  assert.equal(workSeconds(workGroups(a,null,false)[0],999999),undefined);
  assert.equal(formatWorkDuration(undefined),'（耗时未知）');
  assert.equal(formatWorkDuration(0),'0 秒');
  assert.equal(formatWorkDuration(61.9),'1 分 1 秒');
});
for (const kind of ['turn_completed','turn_failed','turn_cancelled']) {
  test(`${kind} freezes exact elapsed time and closes unfinished tool/reasoning rows`, () => {
    let s = apply(base(), 'reasoning_delta', 'A', {text:'thinking'});
    s = apply(s, 'tool_started', 'A', {item_id:'i1',call_id:'c1',name:'execute',status:'running'});
    s = apply(s, kind, 'A', { elapsed_s:19.8 });
    assert.equal(s.runtimeStatus,'idle'); assert.equal(s.activeTurnId,null);
    assert.equal(s.messages[0].work?.elapsed,19.8);
    assert.equal(workSeconds(workGroups(s.messages,null,false)[0],999999),19);
    assert.equal(s.messages.find((m)=>m.type==='thought')?.duration,'done');
    assert.notEqual(s.messages.find((m)=>m.type==='tool_group')?.tools?.[0].status,'running');
    const stale = apply(s,'activity_updated');
    assert.equal(stale.runtimeStatus,'idle');
    assert.equal(stale.messages,s.messages);
  });
}
test('a late old-turn event cannot steal the new turn or stop its timer', () => {
  let s = apply(base(),'turn_completed','A',{elapsed_s:20});
  s = { ...s, messages:[...s.messages,user('u2','B')],runtimeStatus:'running',activeTurnId:'B' };
  for (const kind of ['activity_started','activity_updated','turn_completed','turn_failed']) {
    s = apply(s,kind,'A'); assert.equal(s.activeTurnId,'B'); assert.equal(s.runtimeStatus,'running');
  }
  const groups = workGroups(s.messages,'B',true);
  assert.equal(groups[0].running,false); assert.equal(groups[1].running,true);
});
test('terminal tombstones work even when the console never saw a user row', () => {
  let s = apply(base([]),'activity_started');
  s = apply(s,'turn_completed'); s = apply(s,'activity_started');
  assert.equal(s.runtimeStatus,'idle'); assert.equal(s.activeTurnId,null);
});
test('parallel tools with same name are correlated by call id', () => {
  let s = base();
  s = apply(s,'tool_started','A',{item_id:'i1',call_id:'c1',name:'execute'});
  s = apply(s,'tool_started','A',{item_id:'i2',call_id:'c2',name:'execute'});
  s = apply(s,'tool_result','A',{call_id:'c2',name:'execute',status:'failed'});
  const tools = s.messages.find((m)=>m.type==='tool_group')!.tools!;
  assert.equal(tools[0].error,false); assert.equal(tools[1].error,true);
});
test('refresh restores start/folds without caching text or overwriting terminal time', () => {
  const messages = [ { ...user(),workExpanded:true, content:'PRIVATE PROMPT' }, row('x','tool_group',{turnId:'A', expanded:true, tools:[{...historyToolItem('i','execute'),callId:'c1',preview:'PRIVATE OUTPUT'}]}) ];
  saveTranscriptViews(session,messages);
  const raw = [...data.values()][0];
  assert.ok(!raw.includes('PRIVATE')); assert.ok(!raw.includes('execute'));
  const views = readTranscriptViews(session);
  const completed = restoreTranscriptViews(history(),views);
  assert.equal(completed[0].workExpanded,true); assert.equal(completed[1].expanded,true);
  assert.equal(completed[0].work?.ended,true); assert.equal(completed[0].work?.elapsed,19.8);
  const active = restoreTranscriptViews([row('work-A','info',{turnId:'A',work:{ended:false}})],views);
  assert.equal(workSeconds(workGroups(active,'A',true)[0],21000),20);
  assert.deepEqual(readTranscriptViews({...session,project_id:'other'}),{});
  assert.deepEqual(readTranscriptViews({...session,thread_id:'other'}),{});
});
test('merged historical tool batches retain any opened call detail', () => {
  saveTranscriptViews(session,[user(),row('x','tool_group',{turnId:'A',expanded:false,tools:[{...historyToolItem('i','execute'),callId:'c1'}]}),row('y','tool_group',{turnId:'A',expanded:true,tools:[{...historyToolItem('j','execute'),callId:'c2'}]})]);
  const messages=history(); messages[1].tools!.push({...historyToolItem('j','execute'),callId:'c2'});
  assert.equal(restoreTranscriptViews(messages,readTranscriptViews(session))[1].expanded,true);
});
test('storage failures are safe, bounded and cleared on logout', () => {
  saveTranscriptViews(session,Array.from({length:150},(_,i)=>user(`u${i}`,`T${i}`)));
  assert.equal(Object.keys(readTranscriptViews(session)).length,100);
  clearTranscriptViews(); assert.equal(data.size,0);
  data.set('synapse:transcript-view:v1:'+JSON.stringify(['p','s']),'{broken');
  assert.deepEqual(readTranscriptViews(session),{});
  Object.defineProperty(globalThis,'sessionStorage',{configurable:true,get(){throw new Error('disabled');}});
  assert.doesNotThrow(()=>saveTranscriptViews(session,[user()]));
  assert.deepEqual(readTranscriptViews(session),{});
});
test('a submit receipt binds only the pending user, never previous completed history', () => {
  const completed = history();
  assert.equal(bindWorkTurn(completed,'B',123),completed);
  const messages=[...completed,row('pending','user',{work:{startedAt:10,ended:false}})];
  const bound=bindWorkTurn(messages,'B',123);
  assert.equal(bound.at(-1)!.turnId,'B'); assert.equal(bound.at(-1)!.work?.startedAt,10);
  assert.equal(bindWorkTurn(bound,'B',999),bound);
});
const tool = (id: string, name: string, extra: Partial<ReturnType<typeof historyToolItem>> = {}) =>
  ({ ...historyToolItem(id, name), ...extra });
const activity = (phase: string, detail = '', active = true) =>
  ({ phase, detail, startedAt: 1000, active });

test('fold status reports streaming reasoning while the group runs', () => {
  const live = workGroups([user(), row('t','thought',{turnId:'A',duration:'streaming'})],'A',true)[0];
  assert.deepEqual(getGroupIntentStatus(live), { kind:'thinking', state:'running', text:'正在思考...' });
  // The `streaming` flag on the row says the same thing as the `duration` marker.
  const flagged = workGroups([user(), row('t','thought',{turnId:'A',streaming:true})],'A',true)[0];
  assert.equal(getGroupIntentStatus(flagged)?.state,'running');
  // A settled group whose last row is reasoning reports a finished thought.
  const done = workGroups([user(), row('t','thought',{turnId:'A',duration:'done'})],'A',false)[0];
  assert.deepEqual(getGroupIntentStatus(done), { kind:'thinking', state:'completed', text:'思考完成' });
  // With rows present the rows are the authority, so a live activity cannot override them.
  assert.deepEqual(getGroupIntentStatus(done, activity('thinking')), getGroupIntentStatus(done));
});
test('fold status reports a running tool with its name and intent', () => {
  const messages = [user(), row('x','tool_group',{turnId:'A',tools:[tool('i','execute',{status:'running',label:'run checks'})]})];
  const status = getGroupIntentStatus(workGroups(messages,'A',true)[0]);
  assert.equal(status?.kind,'tool'); assert.equal(status?.state,'running');
  assert.equal(status?.toolName,'execute'); assert.equal(status?.intent,'run checks');
  assert.equal(status?.text,'execute · run checks');
  // A running tool in an earlier group row wins over a later settled thought.
  const trailing = [user(),
    row('x','tool_group',{turnId:'A',tools:[tool('i','execute',{status:'pending',label:'run checks'})]}),
    row('t','thought',{turnId:'A',duration:'done'})];
  assert.equal(getGroupIntentStatus(workGroups(trailing,'A',true)[0])?.state,'running');
});
test('fold status reports the last tool of a settled group and drops an empty intent', () => {
  const messages = [user(), row('x','tool_group',{turnId:'A',tools:[
    tool('i','execute',{status:'completed',label:'run checks'}),
    tool('j','read_file',{status:'completed'}),
  ]})];
  const status = getGroupIntentStatus(workGroups(messages,null,false)[0]);
  assert.equal(status?.kind,'tool'); assert.equal(status?.state,'completed');
  assert.equal(status?.toolName,'read_file'); assert.equal(status?.intent,undefined);
  assert.equal(status?.text,'read_file');
  const failed = [user(), row('x','tool_group',{turnId:'A',tools:[tool('i','execute',{status:'failed',error:true,label:'run checks'})]})];
  assert.equal(getGroupIntentStatus(workGroups(failed,null,false)[0])?.state,'failed');
});
test('a turn with no process row reports the runtime activity, and nothing once it is stale', () => {
  const group = workGroups([row('pending','user',{work:{startedAt:1000,ended:false}})],null,true)[0];
  assert.deepEqual(group.rows,[]);
  assert.deepEqual(getGroupIntentStatus(group, activity('thinking')), { kind:'thinking', state:'running', text:'正在思考...' });
  assert.deepEqual(getGroupIntentStatus(group, activity('model')), { kind:'thinking', state:'running', text:'正在思考...' });
  assert.deepEqual(getGroupIntentStatus(group, activity('tool','execute')),
    { kind:'tool', state:'running', text:'执行工具 · execute' });
  assert.deepEqual(getGroupIntentStatus(group, activity('tool')), { kind:'tool', state:'running', text:'执行工具中...' });
  // A stopped activity keeps its phase, so only an active one may be painted.
  assert.equal(getGroupIntentStatus(group, activity('thinking','',false)), null);
  assert.equal(getGroupIntentStatus(group, activity('idle')), null);
  assert.equal(getGroupIntentStatus(group), null);
});

test('a collapsed fold paints its first step only, and the open fold paints them all', () => {
  const steps = [
    row('t','thought',{turnId:'A'}),
    row('x','tool_group',{turnId:'A',tools:[tool('i','execute')]}),
    row('t2','thought',{turnId:'A'}),
  ];
  const group = workGroups([user(), ...steps],'A',true)[0];
  const collapsed = steps.map((m, i) => rowPaints(m, { isFirst: i === 0, isExpanded: false }));
  assert.deepEqual(collapsed, [true, false, false], 'only the header row of a folded turn is on screen');
  const open = steps.map((m, i) => rowPaints(m, { isFirst: i === 0, isExpanded: true }));
  assert.deepEqual(open, [true, true, true]);
  assert.equal(group.rows.length, steps.length);
});
test('rows that own no fold step never paint, and narration always does', () => {
  // A group with no items is not a fold step (`workGroups` skips it), so it paints
  // nothing even while the turn is open.
  assert.equal(rowPaints(row('x','tool_group',{turnId:'A',tools:[]}), { isFirst: false, isExpanded: true }), false);
  assert.equal(rowPaints(row('x','tool_group',{turnId:'A'}), { isFirst: true, isExpanded: false }), false);
  // A fold-less row (no process meta) is never hidden by the fold.
  assert.equal(rowPaints(row('t','thought')), true);
  for (const type of ['user','assistant','info'] as const) {
    assert.equal(rowPaints(row('r', type), { isFirst: false, isExpanded: false }), true);
  }
});
