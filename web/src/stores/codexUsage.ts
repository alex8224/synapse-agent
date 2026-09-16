/**
 * The Codex usage entry's own store, and the availability source the strip reads.
 *
 * Codex state deliberately does **not** live in `useConsoleStore` (or in
 * `BottomBar`): it is a second, independent view with its own lifecycle, and
 * putting it in the main store would make every reasoning delta a candidate
 * re-render of a surface that only changes once a minute.  The main store is read
 * *from* here (client / session / model / connection flag) and never written.
 *
 * The availability source is what makes the entry discoverable at all:
 * `subscribe` starts the controller and `unsubscribe` stops it, so the OAuth
 * verdict is looked up while the entry is still hidden.  A source that started
 * the controller only when its Trigger mounted could never flip from hidden to
 * painted — the strip would be waiting for a control it does not paint.
 *
 * The subscription is also *narrow*: the host is only notified when `available`
 * actually changes, so a usage refresh does not re-render the strip's layout.
 */
import { create } from 'zustand';
import { useConsoleStore } from './useConsoleStore.ts';
import { CodexUsageController, type CodexUsageState } from './codexUsageController.ts';
import type { CodexUsageContext } from './codexUsageController.ts';

/** The store's shape: the controller's state, mirrored for React. */
export type CodexUsageStoreState = CodexUsageState;

/** A dynamic visibility source, structurally the strip's own contract. */
export interface CodexUsageAvailability {
  getSnapshot: () => boolean;
  subscribe: (listener: () => void) => () => void;
}

export const useCodexUsageStore = create<CodexUsageStoreState>(() => ({
  available: false,
  model: '',
  usage: null,
  credits: null,
  usageLoading: false,
  creditsLoading: false,
  error: null,
  creditsError: null,
  notice: null,
  pending: null,
  consuming: false,
  lastOutcome: null,
  unresolved: null,
}));

/** The observed context, straight from the console store (read-only). */
function readContext(): CodexUsageContext {
  const state = useConsoleStore.getState();
  return {
    client: state.client,
    session: state.currentSession,
    model: state.modelName,
    // The confirmed-binding revision: a model switch publishes the target
    // optimistically, so `model` alone would keep a verdict that describes the
    // previous profile (see `modelRevision` in the console store).
    revision: state.modelRevision,
    // `currentSession` changes before openSession completes. A gate read then
    // would permanently cache a false verdict for a not-yet-open session.
    connected: state.connectionState === 'connected' && state.activeSubscriptionId !== null,
  };
}

/**
 * Whether anything the controller keys on changed.
 *
 * The client *object identity* is compared: a logout/login builds a new client,
 * while a reconnect reuses the same one (and is caught by the connection flag).
 */
function contextChanged(
  state: ReturnType<typeof useConsoleStore.getState>,
  previous: ReturnType<typeof useConsoleStore.getState>,
): boolean {
  return (
    state.client !== previous.client ||
    state.currentSession.project_id !== previous.currentSession.project_id ||
    state.currentSession.thread_id !== previous.currentSession.thread_id ||
    state.modelName !== previous.modelName ||
    state.modelRevision !== previous.modelRevision ||
    state.activeSubscriptionId !== previous.activeSubscriptionId ||
    (state.connectionState === 'connected') !== (previous.connectionState === 'connected')
  );
}

/** A UUID-shaped idempotency key; minted once per raised confirmation. */
function newCommandId(): string {
  const cryptoScope = globalThis.crypto as { randomUUID?: () => string } | undefined;
  if (typeof cryptoScope?.randomUUID === 'function') return cryptoScope.randomUUID();
  // Fallback for a host without `randomUUID` (older insecure-context browsers):
  // still unique per call, still never reused for a replay.
  return `codex-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

const controller = new CodexUsageController({
  read: readContext,
  subscribe: (listener) =>
    useConsoleStore.subscribe((state, previous) => {
      if (contextChanged(state, previous)) listener();
    }),
  onState: (state) => useCodexUsageStore.setState(state),
  newCommandId,
  isVisible: () => document.visibilityState === 'visible',
  schedule: (run, delayMs) => window.setTimeout(run, delayMs),
  cancel: (handle) => window.clearTimeout(handle),
});

/**
 * The entry's availability source.
 *
 * `subscribe` is where the controller's lifetime is decided, and the listener is
 * only called on the `available` edge — the strip's layout does not depend on
 * anything else this store holds.
 */
export const codexUsageAvailability: CodexUsageAvailability = {
  getSnapshot: () => controller.isAvailable(),
  subscribe: (listener) => {
    let last = controller.isAvailable();
    const unsubscribe = useCodexUsageStore.subscribe((state) => {
      if (state.available === last) return;
      last = state.available;
      listener();
    });
    controller.start();
    return () => {
      unsubscribe();
      controller.stop();
    };
  },
};

/** The panel opened: load the credit rows (300s-cached). */
export function openCodexUsagePanel(): void {
  controller.openPanel();
}

/** The panel closed: a half-raised confirmation never survives it. */
export function closeCodexUsagePanel(): void {
  controller.closePanel();
}

/** The panel's refresh control: both views, cache bypassed. */
export function refreshCodexUsage(): void {
  controller.refresh();
}

/** Raise the confirmation for one credit.  Sends nothing. */
export function requestResetCredit(creditId: string): void {
  controller.requestReset(creditId);
}

/** Drop the confirmation.  Sends nothing. */
export function cancelResetCredit(): void {
  controller.cancelReset();
}

/** The one write: consumes one account-level reset credit. */
export function confirmResetCredit(): void {
  controller.confirmReset();
}
