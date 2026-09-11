import React from 'react';
import { useConsoleStore } from '../stores/useConsoleStore';
import { selectRuntimeDiagnosticsBanner } from '../stores/runtimeDiagnosticsView';

/**
 * Read-only runtime diagnostics banner (phase-5 C3-1/C3-2/C3-3).
 *
 * Shown only while the relay is not connected and only when the host's
 * `GET /api/runtime-status` read succeeded: it then states the daemon endpoint,
 * the state dir and the `start synapse-runtime ...` hint as plain facts.  A
 * refused (401/403), failed or malformed read renders nothing at all, so the
 * console keeps its previous copy instead of showing a broken panel.
 */
export const RuntimeDiagnosticsBanner: React.FC = () => {
  const runtimeDiagnostics = useConsoleStore((s) => s.runtimeDiagnostics);
  const connectionState = useConsoleStore((s) => s.connectionState);
  const loadRuntimeDiagnostics = useConsoleStore((s) => s.loadRuntimeDiagnostics);
  const model = selectRuntimeDiagnosticsBanner(runtimeDiagnostics, {
    connected: connectionState === 'connected',
  });
  if (model === null) return null;

  return (
    <div
      role="status"
      className="border-b border-amber-200 bg-amber-50/80 px-8 py-2 font-mono text-[11px] text-amber-900 leading-relaxed"
    >
      <div className="font-semibold">{model.title}</div>
      <ul className="mt-1 space-y-0.5">
        {model.facts.map((fact) => (
          <li key={fact} className="break-all">
            {fact}
          </li>
        ))}
      </ul>
      <div className="mt-1 flex items-center space-x-2 text-amber-800/80">
        <span>{model.note}</span>
        <button
          type="button"
          onClick={() => {
            void loadRuntimeDiagnostics({ trigger: 'manual', force: true });
          }}
          className="shrink-0 rounded border border-amber-300 px-1.5 py-0.5 hover:bg-amber-100"
        >
          重新读取
        </button>
      </div>
    </div>
  );
};