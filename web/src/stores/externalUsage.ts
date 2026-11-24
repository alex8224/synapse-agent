/**
 * The external ACP (e.g. `agy`) usage entry's own store and availability source.
 *
 * Like `codexUsage.ts`, this state deliberately stays out of `useConsoleStore`
 * so streaming deltas never re-render a quota indicator that only changes every
 * minute.  `useConsoleStore` is read-only from here.
 */
import { create } from 'zustand';
import { useConsoleStore } from './useConsoleStore.ts';
import {
  ExternalUsageController,
  type ExternalUsageContext,
  type ExternalUsageState,
} from './externalUsageController.ts';

export type ExternalUsageStoreState = ExternalUsageState;

export interface ExternalUsageAvailability {
  getSnapshot: () => boolean;
  subscribe: (listener: () => void) => () => void;
}

export const useExternalUsageStore = create<ExternalUsageStoreState>(() => ({
  available: false,
  agentId: '',
  usage: null,
  loading: false,
  error: null,
}));

function readContext(): ExternalUsageContext {
  const state = useConsoleStore.getState();
  return {
    client: state.client,
    session: state.currentSession,
    agentId: state.externalSession?.agentId ?? null,
    connected: state.connectionState === 'connected' && state.activeSubscriptionId !== null,
  };
}

function contextChanged(
  state: ReturnType<typeof useConsoleStore.getState>,
  previous: ReturnType<typeof useConsoleStore.getState>,
): boolean {
  return (
    state.client !== previous.client ||
    state.currentSession.project_id !== previous.currentSession.project_id ||
    state.currentSession.thread_id !== previous.currentSession.thread_id ||
    (state.externalSession?.agentId ?? null) !== (previous.externalSession?.agentId ?? null) ||
    state.activeSubscriptionId !== previous.activeSubscriptionId ||
    (state.connectionState === 'connected') !== (previous.connectionState === 'connected')
  );
}

const controller = new ExternalUsageController({
  read: readContext,
  subscribe: (listener) =>
    useConsoleStore.subscribe((state, previous) => {
      if (contextChanged(state, previous)) listener();
    }),
  onState: (state) => useExternalUsageStore.setState(state),
  isVisible: () => document.visibilityState === 'visible',
  schedule: (run, delayMs) => window.setTimeout(run, delayMs),
  cancel: (handle) => window.clearTimeout(handle),
});

export const externalUsageAvailability: ExternalUsageAvailability = {
  getSnapshot: () => controller.isAvailable(),
  subscribe: (listener) => {
    let last = controller.isAvailable();
    const unsubscribe = useExternalUsageStore.subscribe((state) => {
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

/** Force-refresh the external agent's quota snapshot (bypasses 300s cache). */
export function refreshExternalUsage(): void {
  controller.refresh();
}
