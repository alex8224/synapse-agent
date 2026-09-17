/**
 * Background-alert policy for the installable console (PWA).
 *
 * The console is a long-running local client: a turn can sit on a human
 * approval for minutes while the window is behind the editor, and a long answer
 * finishes while nobody is looking.  Everything in this module is pure, so the
 * question "is this transition worth a system notification?" is decided — and
 * tested — without a browser; the effectful half (Notification API, Badging
 * API) lives in `components/BackgroundAlerts.tsx`.
 *
 * Alerts are derived from *store state transitions* rather than from the raw
 * event stream: `pendingApproval` and `runtimeStatus` already encode the two
 * edges that matter, and deriving them here leaves the reducer untouched.
 */

/** The two things worth interrupting the user for. */
export type AlertKind = 'approval' | 'turn-finished';

/** The slice of console state an alert decision depends on. */
export interface AlertSnapshot {
  /** A human decision is waiting (`pendingApproval !== null`). */
  pendingApproval: boolean;
  /** A turn is in flight (`runtimeStatus === 'running'`). */
  running: boolean;
}

/** Notification permission, with "this browser has no Notification API" folded in. */
export type NotificationPermissionState = NotificationPermission | 'unsupported';

/**
 * The subset of the `Notification` global this module needs.
 *
 * Injected so the policy can be exercised in the Node test runner, which has no
 * `Notification`; production passes the real global.
 */
export interface NotificationScope {
  Notification?: {
    new (title: string, options?: NotificationOptions): Notification;
    readonly permission: NotificationPermission;
    requestPermission?: () => Promise<NotificationPermission>;
  };
}

export interface AlertDecision {
  kind: AlertKind;
  title: string;
  body: string;
  /** Dedupe key: a newer alert of the same kind replaces the older toast. */
  tag: string;
}

export const APPROVAL_ALERT_TITLE = '需要审批';
export const TURN_FINISHED_ALERT_TITLE = '任务已完成';
export const APPROVAL_ALERT_BODY = '有危险操作在等待你的决定，本轮已暂停。';
export const TURN_FINISHED_ALERT_BODY = '本轮已经结束，可以回控制台查看。';

/** Reads the current permission without ever prompting. */
export function notificationPermission(
  scope: NotificationScope = globalThis as NotificationScope,
): NotificationPermissionState {
  const ctor = scope.Notification;
  if (!ctor) return 'unsupported';
  const permission = ctor.permission;
  return permission === 'granted' || permission === 'denied' ? permission : 'default';
}

/** The permission as a settings-row value. */
export function permissionLabel(state: NotificationPermissionState): string {
  switch (state) {
    case 'granted':
      return '已授权';
    case 'denied':
      return '已被拒绝（需在浏览器站点设置里恢复）';
    case 'unsupported':
      return '当前浏览器不支持';
    default:
      return '未请求';
  }
}

export interface AlertInput {
  previous: AlertSnapshot;
  next: AlertSnapshot;
  /** `document.visibilityState`: a window in the foreground already shows it. */
  visibility: 'visible' | 'hidden';
  permission: NotificationPermissionState;
  /** Session title, used to name the finished turn when the console knows it. */
  sessionTitle?: string;
}

/**
 * The alert (if any) that a state transition deserves.
 *
 * Three rules, in order: only a granted permission can notify; only a hidden
 * window should (interrupting a user who is already looking at the console is
 * pure noise); and only an *edge* counts, so a re-render that repeats the same
 * snapshot returns `null`.
 */
export function decideAlert(input: AlertInput): AlertDecision | null {
  const { previous, next, visibility, permission, sessionTitle } = input;
  if (permission !== 'granted') return null;
  if (visibility !== 'hidden') return null;

  if (!previous.pendingApproval && next.pendingApproval) {
    return {
      kind: 'approval',
      title: APPROVAL_ALERT_TITLE,
      body: APPROVAL_ALERT_BODY,
      tag: 'synapse-approval',
    };
  }

  // `runtimeStatus` only drops to idle on a terminal turn event, which also
  // clears `pendingApproval`; a turn that ended *into* an approval must report
  // the approval, never "finished".
  if (previous.running && !next.running && !next.pendingApproval) {
    return {
      kind: 'turn-finished',
      title: TURN_FINISHED_ALERT_TITLE,
      body: sessionTitle ? `${sessionTitle}：${TURN_FINISHED_ALERT_BODY}` : TURN_FINISHED_ALERT_BODY,
      tag: 'synapse-turn-finished',
    };
  }

  return null;
}

/**
 * The number on the installed app's badge.
 *
 * One for the approval blocking a turn, plus every finished-but-unseen turn.
 * Returning to the window is the acknowledgement — see `BackgroundAlerts`.
 */
export function badgeTotal(pendingApproval: boolean, unreadFinished: number): number {
  return (pendingApproval ? 1 : 0) + Math.max(0, unreadFinished);
}

/**
 * One session's status, as the alert surfaces need it.
 *
 * Deliberately just two booleans rather than the console's own view type: the
 * policy stays decoupled from the store and testable without one.
 */
export interface AlertSessionStatus {
  pendingApproval: boolean;
  running: boolean;
}

/**
 * One *background* view's status plus whether its watch is still live.
 *
 * A stale view (`subscriptionId === null`) means "unknown", not "still running":
 * counting it would keep a running indicator or an approval badge alive forever
 * after the watch that produced it stopped.
 */
export interface BackgroundViewStatus extends AlertSessionStatus {
  live: boolean;
}

/** Aggregate status across the active session and every background session. */
export interface BackgroundAlertSummary {
  /** How many sessions are waiting on a human decision. */
  approvals: number;
  /** How many sessions have a turn in flight. */
  running: number;
}

/**
 * Fold the active session and every background view into one summary.
 *
 * The alert surfaces used to look only at the session on screen, so a turn that
 * finished — or an approval that appeared — in a session the reader had switched
 * away from was invisible.  Feeding this aggregate to the unchanged
 * `decideAlert` keeps its edge rules intact: a background approval is still an
 * approval edge, and "running" stays true until *every* session has stopped, so
 * a background turn ending is what raises the "finished" alert.
 *
 * Only *live* background views are counted: a view whose watch ended reports
 * "unknown", and letting it keep the aggregate running would leave the console
 * alerting forever.
 */
export function backgroundAlertSummary(
  views: readonly BackgroundViewStatus[],
  activeView: AlertSessionStatus,
): BackgroundAlertSummary {
  let approvals = activeView.pendingApproval ? 1 : 0;
  let running = activeView.running ? 1 : 0;
  for (const view of views) {
    if (!view.live) continue;
    if (view.pendingApproval) approvals += 1;
    if (view.running) running += 1;
  }
  return { approvals, running };
}

/** Counts a delivered alert as unread when it was a finished turn. */
export function applyAlertToUnread(unreadFinished: number, decision: AlertDecision | null): number {
  const current = Math.max(0, unreadFinished);
  return decision?.kind === 'turn-finished' ? current + 1 : current;
}

/**
 * The `Notification` constructor when this browser has one.
 *
 * Shared so the component reaches the API through the same injectable accessor
 * the policy tests use, instead of re-deriving `globalThis.Notification` at
 * every call site.
 */
export function notificationCtor(
  scope: NotificationScope = globalThis as NotificationScope,
): NotificationScope['Notification'] | undefined {
  return scope.Notification;
}

/**
 * Asks for notification permission.  Must be called from a user gesture: a
 * browser ignores (or auto-denies) a prompt raised without one, which is why the
 * only caller is the settings dialog's button.
 */
export async function requestNotificationPermission(
  scope: NotificationScope = globalThis as NotificationScope,
): Promise<NotificationPermissionState> {
  const ctor = notificationCtor(scope);
  if (!ctor) return 'unsupported';
  if (ctor.permission === 'granted' || ctor.permission === 'denied') return ctor.permission;
  if (typeof ctor.requestPermission !== 'function') return 'default';
  try {
    return await ctor.requestPermission();
  } catch {
    // Degradation boundary: a browser that refuses to prompt (permissions
    // policy, insecure context) leaves the console without notifications.  The
    // in-page UI and the badge keep working, so this reports "default" rather
    // than pretending the user decided something.
    return 'default';
  }
}
