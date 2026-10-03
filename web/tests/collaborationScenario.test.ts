import assert from 'node:assert/strict';
import { test } from 'node:test';
import { addSession, advance, initial, needsTick, send, setAuto, stop, type Scenario } from '../src/collaboration-prototype/scenario.ts';
function run(s: Scenario, ticks: number): Scenario {
  for (let i = 0; i < ticks; i++) s = advance(s);
  return s;
}
test('contact, parallel work, receipt and automatic continuation', () => {
  let s = send(initial(), '请让对方确认接口', 'b');
  s = run(s, 4);
  assert.equal(s.phase, 'waiting');
  assert.equal(s.contacted, true);
  assert.ok(s.sessions[0].rows.some((r) => r.text.startsWith('页面完成')));
  s = run(s, 7);
  assert.equal(s.phase, 'done');
  assert.equal(s.delivered, true);
  assert.equal(needsTick(s), false);
  assert.equal(s.sessions[0].rows.filter((r) => r.type === 'receipt').length, 1);
});
test('reading history never starts the other agent', () => {
  const original = initial();
  const s = send(original, '参考这个会话的结论', 'b');
  assert.equal(s.phase, 'idle');
  assert.deepEqual(s.sessions[1], original.sessions[1]);
});
test('busy target finishes original work before processing request', () => {
  let s = run(send(initial(), '请让对方确认', 'busy'), 2);
  assert.equal(s.queued, true);
  assert.equal(s.sessions[2].status, '原任务运行中');
  s = run(s, 3);
  assert.equal(s.queued, false);
  assert.equal(s.sessions[2].status, '处理中');
});
test('stop before contact revokes request and stops timer', () => {
  const s = stop(send(initial(), '请让对方确认', 'b'));
  assert.equal(needsTick(s), false);
  assert.equal(run(s, 20).contacted, false);
});
test('late receipt does not resume stopped source', () => {
  let s = stop(run(send(initial(), '请让对方确认', 'b'), 3));
  s = run(s, 20);
  assert.equal(s.phase, 'stopped');
  assert.equal(s.delivered, true);
  assert.equal(needsTick(s), false);
  assert.ok(!s.sessions[0].rows.some((r) => r.text.startsWith('继续接入')));
  s = send(s, '继续', null);
  assert.equal(s.phase, 'done');
});
test('receive only then explicitly continue via normal composer', () => {
  let s = setAuto(run(send(initial(), '请让对方确认', 'b'), 2), false);
  s = run(s, 20);
  assert.equal(s.phase, 'received');
  assert.equal(needsTick(s), false);
  s = send(s, '继续接入', null);
  assert.equal(s.phase, 'done');
});
test('navigation does not change routing and fresh state has no stale run', () => {
  let s = send(initial(), '请让对方确认', 'b');
  s = run({ ...s, selected: 'b', preview: 'a' }, 11);
  assert.equal(s.phase, 'done');
  assert.equal(s.sessions[0].rows.filter((r) => r.type === 'receipt').length, 1);
  assert.equal(initial().sessions[0].rows.length, 2);
  assert.equal(needsTick(initial()), false);
  const added = addSession(initial(), '后端 · auth-service');
  assert.equal(added.sessions.at(-1)?.project, '后端 · auth-service');
});
