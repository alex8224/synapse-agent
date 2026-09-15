/**
 * Deep links the installed console can be launched with.
 *
 * The console is a single-page app with no router: the only URL the runtime
 * depends on is the origin itself (`/runtime-ws` is derived from it, never from
 * a query string).  These parameters are therefore read once, at boot, and
 * stripped from the address bar so that a reload cannot replay the action.
 */

/** Manifest shortcut action: open a fresh session (`/?action=new-session`). */
export const NEW_SESSION_ACTION = 'new-session';

export interface ShortcutAction {
  /** The recognised action, or `null` when the URL carries none this build knows. */
  action: typeof NEW_SESSION_ACTION | null;
  /** The remaining query string (no leading `?`), with the action removed. */
  remainingSearch: string;
}

export function readShortcutAction(search: string): ShortcutAction {
  const params = new URLSearchParams(search);
  const raw = params.get('action');
  if (raw !== null) params.delete('action');
  return {
    action: raw === NEW_SESSION_ACTION ? NEW_SESSION_ACTION : null,
    remainingSearch: params.toString(),
  };
}
