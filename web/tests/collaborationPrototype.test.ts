import assert from 'node:assert/strict';
import { test } from 'node:test';
import { adopt, createItem, initial, pendingForSession, setBusy, targetOccupied, updateItem } from '../src/collaboration-prototype/state.ts';

const request = { target: 's3', title: '研究', expected: '摘要', shared: [] };
const available = () => updateItem(initial(), 'c1', 'deliver', '初始结论');

test('same target serializes through clarification and delivery', () => {
  let model = createItem(available(), request);
  const first = model.active!;
  model = createItem(model, request);
  const second = model.active!;
  assert.equal(model.items.at(-1)?.phase, 'queued');
  assert.equal(targetOccupied(model, 's3', second), true);
  model = updateItem(model, first, 'clarify', '范围？');
  assert.equal(updateItem(model, second, 'start').items.at(-1)?.phase, 'queued');
  model = updateItem(model, first, 'supplement', '全部');
  assert.equal(updateItem(model, second, 'start').items.at(-1)?.phase, 'queued');
  model = updateItem(model, first, 'deliver', '结论');
  assert.equal(pendingForSession(model, 's1'), 2); // both deliveries remain pending adoption
  model = updateItem(model, second, 'start');
  assert.equal(model.items.at(-1)?.phase, 'working');
});

test('revision queues behind another active item and can start once delivered', () => {
  let model = createItem(available(), request);
  const first = model.active!;
  model = updateItem(model, first, 'deliver', '结论');
  model = createItem(model, request);
  const second = model.active!;
  model = updateItem(model, first, 'revise', '再核查');
  assert.equal(model.items[1].phase, 'queued');
  assert.equal(updateItem(model, first, 'start').items[1].phase, 'queued');
  model = updateItem(model, second, 'deliver', '另一结论');
  model = updateItem(model, first, 'start');
  assert.equal(model.items[1].phase, 'working');
});

test('pending is counted by role and includes delivered even when not deferred', () => {
  let model = initial();
  assert.equal(pendingForSession(model, 's1'), 1);
  assert.equal(pendingForSession(model, 's3'), 0);
  model = createItem(model, request); // queued behind clarifying c1
  assert.equal(pendingForSession(model, 's3'), 1);
  model = updateItem(model, 'c1', 'deliver', '结论');
  assert.equal(pendingForSession(model, 's1'), 1);
  model = updateItem(model, 'c1', 'defer');
  assert.equal(pendingForSession(model, 's1'), 1);
  model = adopt(model, 'c1', '');
  assert.equal(pendingForSession(model, 's1'), 0);
});

test('busy existing target queues without preempting; starts after target finishes', () => {
  let model = createItem(initial(), { target: 's2', title: '审查', expected: '风险', shared: [] });
  const id = model.active!;
  assert.equal(model.items.at(-1)?.phase, 'queued');
  assert.equal(model.sessions.find((s) => s.id === 's2')?.busy, true);
  model = updateItem(model, id, 'start');
  assert.equal(model.items.at(-1)?.phase, 'queued');
  model = updateItem(setBusy(model, 's2', false), id, 'start');
  assert.equal(model.items.at(-1)?.phase, 'working');
});

test('clarification, delivery, busy source deferral, adoption and revision', () => {
  let model = createItem(available(), { target: 's3', title: '研究', expected: '摘要', shared: ['一条消息'] });
  const id = model.active!;
  model = updateItem(model, id, 'clarify', '范围？');
  assert.equal(model.items.at(-1)?.phase, 'clarifying');
  model = updateItem(model, id, 'supplement', '全部');
  model = updateItem(model, id, 'deliver', '结论 A');
  assert.equal(model.sessions[0].messages.some((s) => s.includes('结论 A')), false);
  model = setBusy(model, 's1', true);
  assert.equal(adopt(model, id, ''), model);
  model = updateItem(model, id, 'defer');
  assert.equal(model.items.at(-1)?.deferred, true);
  model = adopt(setBusy(model, 's1', false), id, '先试点');
  assert.equal(model.items.at(-1)?.phase, 'adopted');
  assert.match(model.sessions[0].messages.at(-1)!, /结论 A.*先试点/s);
  model = updateItem(model, id, 'revise', '补充数据');
  model = updateItem(model, id, 'deliver', '结论 B');
  assert.equal(model.items.at(-1)?.phase, 'delivered');
  assert.equal(model.items.at(-1)?.entries.at(-1)?.text, '结论 B');
});

test('new target inherits selected project and only selected messages', () => {
  const model = createItem(initial(), { target: 'new', newTitle: 'Northstar::专项', title: '核对', expected: '清单', shared: [] });
  assert.equal(model.sessions.at(-1)?.project, 'Northstar');
  assert.deepEqual(model.items.at(-1)?.shared, []);
  assert.equal(model.items.at(-1)?.phase, 'working');
});
