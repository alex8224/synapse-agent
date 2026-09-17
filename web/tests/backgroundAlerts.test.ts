/**
 * Policy tests for the console's background alerts.
 *
 * The interesting decisions are all "when *not* to interrupt": a visible window
 * already shows the change, a permission that was never granted cannot notify,
 * and a re-render that repeats the same state is not an edge.  Each of those is
 * pinned here, because the failure mode in production is a toast storm that
 * nobody can reproduce on demand.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  APPROVAL_ALERT_TITLE,
  TURN_FINISHED_ALERT_TITLE,
  applyAlertToUnread,
  badgeTotal,
  backgroundAlertSummary,
  decideAlert,
  notificationPermission,
  permissionLabel,
  requestNotificationPermission,
} from '../src/stores/backgroundAlerts.ts';
import type {
  AlertInput,
  AlertSessionStatus,
  AlertSnapshot,
  BackgroundViewStatus,
} from '../src/stores/backgroundAlerts.ts';

const IDLE: AlertSnapshot = { pendingApproval: false, running: false };
const RUNNING: AlertSnapshot = { pendingApproval: false, running: true };
const WAITING_APPROVAL: AlertSnapshot = { pendingApproval: true, running: true };

const ACTIVE_IDLE: AlertSessionStatus = { pendingApproval: false, running: false };
const ACTIVE_RUNNING: AlertSessionStatus = { pendingApproval: false, running: true };
// A background view also carries liveness: `live: false` means its watch ended
// and its last status is unknown, not a fact.
const BG_IDLE: BackgroundViewStatus = { pendingApproval: false, running: false, live: true };
const BG_RUNNING: BackgroundViewStatus = { pendingApproval: false, running: true, live: true };
const BG_APPROVAL: BackgroundViewStatus = { pendingApproval: true, running: true, live: true };
const BG_STALE_APPROVAL: BackgroundViewStatus = { pendingApproval: true, running: true, live: false };
const BG_STALE_RUNNING: BackgroundViewStatus = { pendingApproval: false, running: true, live: false };

function input(previous: AlertSnapshot, next: AlertSnapshot, overrides: Partial<AlertInput> = {}): AlertInput {
  return {
    previous,
    next,
    visibility: 'hidden',
    permission: 'granted',
    ...overrides,
  };
}

function fakeScope(
  permission: NotificationPermission | undefined,
  requestPermission?: () => Promise<NotificationPermission>,
) {
  return permission === undefined ? {} : { Notification: { permission, requestPermission } };
}

test('notificationPermission reports "unsupported" without the API', () => {
  assert.equal(notificationPermission(fakeScope(undefined)), 'unsupported');
});

test('notificationPermission never prompts and normalises unknown values', () => {
  assert.equal(notificationPermission(fakeScope('granted')), 'granted');
  assert.equal(notificationPermission(fakeScope('denied')), 'denied');
  assert.equal(notificationPermission(fakeScope('default')), 'default');
});

test('permissionLabel names every state for the settings row', () => {
  assert.equal(permissionLabel('granted'), '已授权');
  assert.equal(permissionLabel('unsupported'), '当前浏览器不支持');
  assert.ok(permissionLabel('denied').startsWith('已被拒绝'));
  assert.equal(permissionLabel('default'), '未请求');
});

test('requestNotificationPermission never prompts for a decided permission', async () => {
  let prompted = 0;
  const prompt = async () => {
    prompted += 1;
    return 'granted' as NotificationPermission;
  };
  assert.equal(
    await requestNotificationPermission(fakeScope('granted', prompt)),
    'granted',
  );
  assert.equal(await requestNotificationPermission(fakeScope('denied', prompt)), 'denied');
  assert.equal(prompted, 0, 'a decided permission must not raise another prompt');
});

test('requestNotificationPermission reports the browser answer', async () => {
  assert.equal(
    await requestNotificationPermission(fakeScope('default', async () => 'granted')),
    'granted',
  );
});

test('requestNotificationPermission degrades instead of throwing', async () => {
  assert.equal(await requestNotificationPermission(fakeScope(undefined)), 'unsupported');
  assert.equal(await requestNotificationPermission(fakeScope('default')), 'default');
  assert.equal(
    await requestNotificationPermission(
      fakeScope('default', async () => {
        throw new Error('permissions policy');
      }),
    ),
    'default',
  );
});

test('an approval edge in a hidden window raises the approval alert', () => {
  const decision = decideAlert(input(RUNNING, WAITING_APPROVAL));
  assert.equal(decision?.kind, 'approval');
  assert.equal(decision?.title, APPROVAL_ALERT_TITLE);
  assert.equal(decision?.tag, 'synapse-approval');
});

test('a turn that ends without an approval raises the finished alert', () => {
  const decision = decideAlert(input(RUNNING, IDLE, { sessionTitle: '修复登录' }));
  assert.equal(decision?.kind, 'turn-finished');
  assert.equal(decision?.title, TURN_FINISHED_ALERT_TITLE);
  assert.ok(decision?.body.startsWith('修复登录：'));
});

test('a turn that ended into an approval reports the approval, not "finished"', () => {
  const decision = decideAlert(input(RUNNING, WAITING_APPROVAL));
  assert.notEqual(decision?.kind, 'turn-finished');
});

test('a visible window is never notified — the console already shows it', () => {
  assert.equal(decideAlert(input(RUNNING, WAITING_APPROVAL, { visibility: 'visible' })), null);
  assert.equal(decideAlert(input(RUNNING, IDLE, { visibility: 'visible' })), null);
});

test('a permission that was not granted suppresses every alert', () => {
  for (const permission of ['default', 'denied', 'unsupported'] as const) {
    assert.equal(decideAlert(input(RUNNING, WAITING_APPROVAL, { permission })), null);
    assert.equal(decideAlert(input(RUNNING, IDLE, { permission })), null);
  }
});

test('repeated snapshots are not edges', () => {
  assert.equal(decideAlert(input(IDLE, IDLE)), null);
  assert.equal(decideAlert(input(RUNNING, RUNNING)), null);
  assert.equal(decideAlert(input(WAITING_APPROVAL, WAITING_APPROVAL)), null);
});

test('badgeTotal counts the blocking approval plus unseen finished turns', () => {
  assert.equal(badgeTotal(false, 0), 0);
  assert.equal(badgeTotal(true, 0), 1);
  assert.equal(badgeTotal(true, 2), 3);
  assert.equal(badgeTotal(false, -3), 0);
});

test('only a finished turn accumulates as unread', () => {
  const finished = decideAlert(input(RUNNING, IDLE));
  const approval = decideAlert(input(RUNNING, WAITING_APPROVAL));
  assert.equal(applyAlertToUnread(0, finished), 1);
  assert.equal(applyAlertToUnread(1, finished), 2);
  assert.equal(applyAlertToUnread(1, approval), 1);
  assert.equal(applyAlertToUnread(1, null), 1);
});

test('the summary counts the active session even with no background views', () => {
  assert.deepEqual(backgroundAlertSummary([], ACTIVE_IDLE), { approvals: 0, running: 0 });
  assert.deepEqual(backgroundAlertSummary([], ACTIVE_RUNNING), { approvals: 0, running: 1 });
});

test('a stale background view is unknown and never keeps the console alerting', () => {
  // Its watch ended, so the last status it reported is not a fact any more:
  // counting it would keep a running indicator (and an approval badge) alive
  // forever, which is exactly the "stale session pollutes alerts" defect.
  assert.deepEqual(
    backgroundAlertSummary([BG_STALE_APPROVAL, BG_STALE_RUNNING], ACTIVE_IDLE),
    { approvals: 0, running: 0 },
  );
  // A live view next to a stale one is still counted.
  assert.deepEqual(
    backgroundAlertSummary([BG_STALE_APPROVAL, BG_RUNNING], ACTIVE_IDLE),
    { approvals: 0, running: 1 },
  );
});

test('a background approval keeps the console alerting', () => {
  const summary = backgroundAlertSummary([BG_APPROVAL, BG_RUNNING], ACTIVE_IDLE);
  assert.deepEqual(summary, { approvals: 1, running: 2 });
  // The aggregate is what `decideAlert` sees, so a background approval is an
  // approval edge even though the session on screen is idle.
  const decision = decideAlert(
    input(
      { pendingApproval: false, running: summary.running > 0 },
      { pendingApproval: summary.approvals > 0, running: summary.running > 0 },
    ),
  );
  assert.equal(decision?.kind, 'approval');
});

test('"running" stays true until every session has stopped', () => {
  // The active session finishing while a background one still runs is not a
  // "finished" edge: the aggregate is still running.
  const summary = backgroundAlertSummary([BG_RUNNING], ACTIVE_IDLE);
  assert.equal(summary.running, 1);
  assert.equal(
    decideAlert(input(
      { pendingApproval: false, running: true },
      { pendingApproval: false, running: summary.running > 0 },
    )),
    null,
  );
});

test('the last session stopping raises the finished alert', () => {
  const summary = backgroundAlertSummary([BG_IDLE], ACTIVE_IDLE);
  assert.deepEqual(summary, { approvals: 0, running: 0 });
  const decision = decideAlert(
    input({ pendingApproval: false, running: true }, { pendingApproval: false, running: false }),
  );
  assert.equal(decision?.kind, 'turn-finished');
});
