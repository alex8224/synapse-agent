/**
 * Policy tests for the console's recovery / degradation notice.
 *
 * The store computes `recoveryState` / `recoveryDetail` /
 * `liveBufferDroppedCount`, but the decision of *whether* the console says
 * anything is the pure module's, and the interesting cases are the quiet ones:
 * a healthy console must render nothing, a successful resume must not leave a
 * permanent strip behind, and a value a newer daemon added must not make an
 * older console paint a notice it cannot explain.  The failure mode in
 * production is the opposite one -- a truncated live replay that stays
 * completely silent -- so every state value is pinned here.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { recoveryNotice } from '../src/stores/recoveryNoticeView.ts';

const here = dirname(fileURLToPath(import.meta.url));

/** The store's whole `recoveryState` union, so no value can drift uncovered. */
const STATES = [
  'idle',
  'reconnecting',
  'resuming',
  'resumed',
  'resync',
  'incomplete',
  'unknown',
  'failed',
] as const;

test('every recovery state maps to its kind, or to silence', () => {
  const expected: Record<string, string | null> = {
    idle: null,
    resumed: null,
    unknown: null,
    failed: 'blocked',
    incomplete: 'degraded',
    resync: 'degraded',
    reconnecting: 'transient',
    resuming: 'transient',
  };
  for (const state of STATES) {
    assert.equal(
      recoveryNotice(state, null, 0)?.kind ?? null,
      expected[state],
      `state ${state} must map to ${expected[state]}`,
    );
  }
});

test('internal diagnostic technical details are humanized for the reader', () => {
  const rawTurn = 'active turn b763e724caca49ca82b0705d8399c6cc live prefix was evicted; replay incomplete until settlement';
  const noticeTurn = recoveryNotice('incomplete', rawTurn, 0);
  assert.equal(noticeTurn?.detail, '重连后较早的流式输出已折叠，生成完成后将自动同步完整记录。');

  const rawGap = 'watch cursor 42 stale (replay_gap); resynced from history snapshot';
  const noticeGap = recoveryNotice('incomplete', rawGap, 0);
  assert.equal(noticeGap?.detail, '会话已重新与最新进度同步，未完成轮次在生成结束后将自动补全完整记录。');
});

test('every rendered notice carries a non-empty Chinese headline', () => {
  for (const state of STATES) {
    const notice = recoveryNotice(state, null, 0);
    if (notice === null) continue;
    assert.ok(notice.title.length > 0, `${state} needs a headline`);
    assert.match(notice.title, /[\u4e00-\u9fff]/, `${state} must be user-facing Chinese`);
  }
});

test('a resume that succeeded is not reported', () => {
  // `resumed` means the resume worked; keeping a strip up afterwards would make
  // a healthy console look degraded forever.
  assert.equal(recoveryNotice('resumed', null, 0), null);
  assert.equal(
    recoveryNotice('resumed', 'recovery snapshot unavailable; resumed from last cursor', 0),
    null,
  );
});

test('a healthy state with nothing dropped renders nothing', () => {
  assert.equal(recoveryNotice('idle', null, 0), null);
  assert.equal(recoveryNotice('idle', 'anything', 0), null);
});

test('an unknown state is silent, so a newer daemon cannot break an older console', () => {
  assert.equal(
    recoveryNotice('unknown', 'steer submitted during a connection drop; outcome unknown', 0),
    null,
  );
  assert.equal(recoveryNotice('a_state_from_the_future', null, 0), null);
  assert.equal(recoveryNotice('', null, 0), null);
  // Inherited object keys must not answer for a state either.
  assert.equal(recoveryNotice('constructor', null, 0), null);
  assert.equal(recoveryNotice('toString', null, 0), null);
});

test('dropped live events alone are a degradation', () => {
  // The turn's earlier events were evicted before the transcript could buffer
  // them: the replay is truncated even while the connection itself is healthy.
  for (const state of ['idle', 'resumed', 'unknown', ''] as const) {
    const notice = recoveryNotice(state, null, 4);
    assert.equal(notice?.kind, 'degraded', `state ${state} + drops must report`);
    assert.equal(notice?.droppedEvents, 4);
    assert.equal(notice?.detail, null, 'there is no recovery detail to repeat');
  }
});

test('a dropped count merges into the state notice instead of adding a second one', () => {
  // One incident, one strip: the count rides along on the notice the state
  // already asked for, and the state's kind stays in charge.
  const blocked = recoveryNotice('failed', 'reconnect budget exhausted', 7);
  assert.equal(blocked?.kind, 'blocked');
  assert.equal(blocked?.droppedEvents, 7);
  assert.equal(blocked?.detail, 'reconnect budget exhausted');

  const degraded = recoveryNotice('incomplete', 'resynced after replay_gap', 3);
  assert.equal(degraded?.kind, 'degraded');
  assert.equal(degraded?.droppedEvents, 3);

  const transient = recoveryNotice('reconnecting', 'connection lost (attempt 2/5)', 1);
  assert.equal(transient?.kind, 'transient');
  assert.equal(transient?.droppedEvents, 1);
});

test('the dropped count is a count, never a guess', () => {
  assert.equal(recoveryNotice('idle', null, -3), null, 'a negative count is not a loss');
  assert.equal(recoveryNotice('idle', null, Number.NaN), null, 'NaN is not a loss');
  assert.equal(recoveryNotice('idle', null, Number.POSITIVE_INFINITY), null);
  assert.equal(recoveryNotice('idle', null, 0), null);
  assert.equal(recoveryNotice('idle', null, 2.7)?.droppedEvents, 2, 'a count is whole');
});

test('the headline never restates the detail verbatim', () => {
  const detail = '运行轮次的早期步骤不可完整恢复；已保存历史不受影响。';
  const notice = recoveryNotice('incomplete', detail, 0);
  assert.equal(notice?.detail, detail, 'the server sentence is echoed as-is');
  assert.notEqual(notice?.title, notice?.detail);
  assert.equal(notice?.title.startsWith(detail), false);

  // A detail that *is* the headline adds nothing, so it is dropped rather than
  // printed twice.
  const title = recoveryNotice('incomplete', null, 0)?.title ?? '';
  assert.ok(title.length > 0);
  assert.equal(recoveryNotice('incomplete', title, 0)?.detail, null);
  assert.equal(recoveryNotice('incomplete', `  ${title}  `, 0)?.detail, null);
  assert.equal(recoveryNotice('incomplete', '   ', 0)?.detail, null);
  assert.equal(recoveryNotice('incomplete', '', 0)?.detail, null);
});

test('the three kinds carry three distinct headlines', () => {
  const titles = [
    recoveryNotice('failed', null, 0)?.title,
    recoveryNotice('incomplete', null, 0)?.title,
    recoveryNotice('reconnecting', null, 0)?.title,
  ];
  assert.equal(new Set(titles).size, 3, 'one headline per kind, never shared');
});

test('the painter subscribes per field, announces per kind and cannot be dismissed', () => {
  // The strip's contract is thin enough to pin statically: three separate
  // selectors (a streaming turn must not re-render it), `alert` for a blocked
  // recovery and `status` for the rest, and no dismiss control -- the state is
  // not the reader's to acknowledge while it holds.
  const source = readFileSync(
    join(here, '..', 'src', 'components', 'RecoveryNotice.tsx'),
    'utf8',
  );
  assert.ok(source.includes('useConsoleStore((s) => s.recoveryState)'));
  assert.ok(source.includes('useConsoleStore((s) => s.recoveryDetail)'));
  assert.ok(source.includes('useConsoleStore((s) => s.liveBufferDroppedCount)'));
  assert.ok(source.includes('recoveryNotice(recoveryState, recoveryDetail, liveBufferDroppedCount)'));
  assert.ok(source.includes("notice.kind === 'blocked' ? 'alert' : 'status'"));
  assert.equal(/\bonClick\b/.test(source), false, 'the notice must not be dismissible');
  assert.equal(source.includes('console-gutter'), true, 'it must share the reading gutters');
  assert.equal(source.includes('console-column'), true, 'and start on the chat edge');
});
