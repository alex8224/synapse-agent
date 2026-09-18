/**
 * Readable presentation model for the host's read-only runtime diagnostics
 * (phase-5 C3-1/C3-2/C3-3).
 *
 * Pure module: no React, no socket, no fetch.  It turns the store snapshot of
 * `GET /api/runtime-status` into the exact lines the banner shows, and it is the
 * single place that decides when the console must stay on its previous copy
 * (return `null` = render nothing at all).
 *
 * Wording discipline (C3-3): only diagnostic facts are stated.  Nothing here
 * claims that any check passed, and no field is described as verified.
 */
import type { RuntimeStatusView } from '../client/runtimeStatus.ts';

export type RuntimeDiagnosticsStatus = 'idle' | 'loading' | 'ready' | 'unavailable';

/** Store snapshot of the last diagnostics read. */
export interface RuntimeDiagnosticsSnapshot {
  status: RuntimeDiagnosticsStatus;
  view: RuntimeStatusView | null;
  /** Failure reason token (`unauthorized` / `network` / ...) when unavailable. */
  reason: string | null;
  /** What asked for the read (`relay_unavailable` / `connect_failed` / ...). */
  trigger: string | null;
  /** Bounded connection detail captured when the read was triggered. */
  detail: string | null;
}

export const RUNTIME_DIAGNOSTICS_IDLE: RuntimeDiagnosticsSnapshot = {
  status: 'idle',
  view: null,
  reason: null,
  trigger: null,
  detail: null,
};

export interface RuntimeDiagnosticsBannerModel {
  title: string;
  facts: string[];
  note: string;
}

const TRIGGER_LABELS: Record<string, string> = {
  relay_unavailable: '运行时中继不可用',
  connect_failed: '运行时连接失败',
  manual: '运行时诊断（手动刷新）',
};

function triggerLabel(trigger: string | null): string {
  if (trigger === null) return '运行时诊断';
  return TRIGGER_LABELS[trigger] ?? `运行时诊断（${trigger}）`;
}

/**
 * The banner for one snapshot, or `null` when the console must show nothing new.
 *
 * `null` is returned for every state except a complete read: while the read is
 * in flight, when it was refused (401/403), when the transport failed and when
 * the body was unusable.  In those cases the existing copy stays exactly as it
 * was, which is the C3-2 "silent degradation" requirement.
 */
export function selectRuntimeDiagnosticsBanner(
  state: RuntimeDiagnosticsSnapshot,
  relay: { connected: boolean } = { connected: false },
): RuntimeDiagnosticsBannerModel | null {
  // The relay is back: the diagnosis is stale, so it is withdrawn instead of
  // being left on screen as if it still described the current connection.
  if (relay.connected) return null;
  if (state.status !== 'ready' || state.view === null) return null;
  const view = state.view;
  const facts = [
    `daemon endpoint: ${formatEndpoint(view)}`,
    `state dir: ${view.state_dir}`,
  ];
  if (view.hint !== null) {
    facts.push(`hint: ${view.hint}`);
  } else {
    facts.push('hint: (host reported no start hint)');
  }
  if (state.detail !== null) {
    facts.push(`connection: ${state.detail}`);
  }
  return {
    title: `${triggerLabel(state.trigger)}：宿主只读诊断信息`,
    facts,
    note:
      '以上字段取自宿主只读端点 GET /api/runtime-status（endpoint / state_dir / hint），仅用于本机排障。',
  };
}

function formatEndpoint(view: RuntimeStatusView): string {
  if (view.endpoint === null) return 'unknown (host reported no runtime metadata)';
  return `${view.endpoint.host}:${view.endpoint.port}`;
}