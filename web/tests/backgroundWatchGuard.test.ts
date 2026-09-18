/**
 * Source guards for the multi-session console (stage 3b).
 *
 * Two invariants make concurrent sessions work, and both are easy to undo by
 * accident, so they are pinned statically:
 *
 * 1. Leaving a session must not detach its watch.  The single-session console
 *    called `client.unwatchEvents()` (no argument) on every switch, which drops
 *    *every* subscription -- including the one the session being left is still
 *    streaming on.  Only a named subscription may be detached, and only when
 *    re-attaching that same session.
 * 2. Events and subscription notices must be routed by `subscription_id`.  With
 *    several watches live, the id is the only thing that says which session a
 *    frame belongs to; the session on screen must never receive another one's
 *    events, and a background `complete` must never trigger the active resync.
 *
 * The background fold must also be the *same* fold the active session uses --
 * `foldLiveEvents` plus the terminal-turn usage bookkeeping -- never a second
 * reducer that could drift from it.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const webRoot = join(here, '..');
const store = readFileSync(join(webRoot, 'src', 'stores', 'useConsoleStore.ts'), 'utf8');
const sessionViews = readFileSync(join(webRoot, 'src', 'stores', 'sessionViews.ts'), 'utf8');

test('leaving a session keeps its watch live instead of detaching every watch', () => {
  // The no-argument detach is exactly the call that used to drop the leaving
  // session's subscription; no path may issue it any more.
  assert.equal(
    /client\.unwatchEvents\(\s*\)/.test(store),
    false,
    'a switch must not drop every watch -- the leaving session must keep streaming',
  );
  // The leaving session is snapshotted into a background view before the active
  // fields are cleared, so its transcript is moved out of view, never lost.
  assert.ok(store.includes('snapshotLiveView('), 'the leaving view must be snapshotted');
  assert.ok(store.includes('backgroundActiveSession('), 'the snapshot must land in backgroundViews');
  // Re-attaching the *same* session may detach only that one lease.
  assert.ok(
    store.includes('client.unwatchEvents(leaving.activeSubscriptionId)'),
    'a same-session reload may detach only its own subscription',
  );
});

test('live events are routed to a background view by subscription id', () => {
  assert.ok(store.includes('meta?.subscription_id'), 'the event route must read the subscription id');
  assert.ok(
    store.includes('backgroundViewKeyForSubscription('),
    'an event for a background watch must resolve to its view',
  );
  assert.ok(store.includes('foldBackgroundEvents('), 'a background batch folds into its own view');
});

test('subscription notices are routed by subscription id', () => {
  assert.ok(store.includes('notice.subscription_id'), 'the notice route must read the subscription id');
  // A background watch that ended keeps its transcript but is marked stale, so
  // returning to it re-attaches from history instead of trusting it.
  assert.ok(
    store.includes('subscriptionId: null'),
    'an ended background watch must be marked stale (subscriptionId === null)',
  );
});

test('the background fold reuses the shared reducer, not a second one', () => {
  assert.ok(sessionViews.includes('foldLiveEvents('), 'the background fold must call the shared fold');
  assert.equal(
    sessionViews.includes('reduceRuntimeEvent'),
    false,
    'a second reducer would be a second source of truth',
  );
});
