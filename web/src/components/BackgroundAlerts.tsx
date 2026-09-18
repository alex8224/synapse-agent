import { useEffect, useRef, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useConsoleStore } from '../stores/useConsoleStore';
import {
  applyAlertToUnread,
  badgeTotal,
  backgroundAlertSummary,
  decideAlert,
  notificationCtor,
  notificationPermission,
  type AlertDecision,
  type AlertSnapshot,
  type NotificationPermissionState,
} from '../stores/backgroundAlerts';

/**
 * Bridges console state to the browser's background surfaces: system
 * notifications for the two transitions worth interrupting a user for
 * (approval requested, turn finished) and the installed app's badge.  Renders
 * nothing.
 *
 * It is mounted only on the paired console, so the pairing gate never asks for
 * anything, and it never touches the runtime: every input is store state the
 * transcript already consumes.
 */

/** The OS truncates long bodies anyway; cap before the text reaches the API. */
const NOTIFICATION_BODY_LIMIT = 160;

function showAlert(decision: AlertDecision): void {
  const ctor = notificationCtor();
  if (!ctor) return;
  try {
    const notification = new ctor(decision.title, {
      body: decision.body.slice(0, NOTIFICATION_BODY_LIMIT),
      tag: decision.tag,
      icon: '/icon-192.png',
      badge: '/icon-192.png',
    });
    // Clicking the toast lands on the console instead of opening a second copy.
    // The manifest's `launch_handler: focus-existing` covers the cold start;
    // this covers the window that is merely behind another app.
    notification.onclick = () => {
      window.focus();
      notification.close();
    };
  } catch {
    // Degradation boundary: some platforms reject `new Notification` while the
    // page is hidden.  The badge still reports the change, so the toast is
    // dropped rather than escalated into a console error.
  }
}

function syncBadge(pendingApproval: boolean, unreadFinished: number): void {
  const navigatorWithBadge = navigator as Navigator & {
    setAppBadge?: (contents?: number) => Promise<void>;
    clearAppBadge?: () => Promise<void>;
  };
  const total = badgeTotal(pendingApproval, unreadFinished);
  try {
    if (total > 0) {
      void navigatorWithBadge.setAppBadge?.(total)?.catch(() => undefined);
    } else {
      void navigatorWithBadge.clearAppBadge?.()?.catch(() => undefined);
    }
  } catch {
    // Degradation boundary: the Badging API is Chromium-only and can throw on a
    // window that is not installed.  A badge is cosmetic and never blocks the
    // console, so the failure stops here.
  }
}

function currentVisibility(): 'visible' | 'hidden' {
  return document.visibilityState === 'hidden' ? 'hidden' : 'visible';
}

export function BackgroundAlerts() {
  // The active session *and* every background view, folded into counts.  The
  // shallow selector keeps the numbers referentially stable, so a background
  // delta (which rewrites its view on every frame) does not re-render this
  // component while the counts stay the same.
  const summary = useConsoleStore(
    useShallow((s) =>
      backgroundAlertSummary(
        Object.values(s.backgroundViews).map((view) => ({
          pendingApproval: view.pendingApproval !== null,
          running: view.runtimeStatus === 'running',
          // A view whose watch ended is "unknown": counting it would keep the
          // aggregate running (and the badge up) forever.
          live: view.subscriptionId !== null,
        })),
        { pendingApproval: s.pendingApproval !== null, running: s.runtimeStatus === 'running' },
      ),
    ),
  );
  const pendingApproval = summary.approvals > 0;
  const running = summary.running > 0;
  const sessionTitle = useConsoleStore((s) => s.sessionTitle);
  const [permission, setPermission] = useState<NotificationPermissionState>(() =>
    notificationPermission(),
  );

  // The previous snapshot is the whole point: an alert is an edge.  Streaming
  // deltas and renames re-render this component constantly and must not alert.
  const previous = useRef<AlertSnapshot>({ pendingApproval, running });
  const pendingRef = useRef(pendingApproval);
  const unread = useRef(0);

  // Mirrored in an effect rather than during render so the mount-once
  // foreground listener below can read the latest approval without
  // re-subscribing on every turn.
  useEffect(() => {
    pendingRef.current = pendingApproval;
  }, [pendingApproval]);

  useEffect(() => {
    const next: AlertSnapshot = { pendingApproval, running };
    const prev = previous.current;
    previous.current = next;
    if (prev.pendingApproval === next.pendingApproval && prev.running === next.running) return;

    const decision = decideAlert({
      previous: prev,
      next,
      visibility: currentVisibility(),
      permission,
      sessionTitle,
    });
    if (!decision) return;
    unread.current = applyAlertToUnread(unread.current, decision);
    showAlert(decision);
    syncBadge(next.pendingApproval, unread.current);
  }, [pendingApproval, running, permission, sessionTitle]);

  // Returning to the window is the acknowledgement: it clears the badge, and it
  // re-reads the permission because the user may have answered the prompt from
  // the browser's own UI rather than from the settings dialog.
  useEffect(() => {
    const onForeground = () => {
      if (currentVisibility() === 'visible') {
        unread.current = 0;
        syncBadge(pendingRef.current, 0);
      }
      setPermission(notificationPermission());
    };
    document.addEventListener('visibilitychange', onForeground);
    window.addEventListener('focus', onForeground);
    return () => {
      document.removeEventListener('visibilitychange', onForeground);
      window.removeEventListener('focus', onForeground);
    };
  }, []);

  // The approval is *state*, not an edge: the badge has to stay up until the
  // decision is made, even when the alert itself was suppressed because the
  // window was in the foreground.
  useEffect(() => {
    syncBadge(pendingApproval, unread.current);
  }, [pendingApproval, permission]);

  return null;
}
