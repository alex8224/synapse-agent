/**
 * The strip's workflow entry: how many runs the project has, opening a compact panel.
 *
 * The panel is a `popover`, so the host owns the portal, the outside-click rule and the
 * Escape handling — this module only paints the run list, the selected run's calls and the
 * two actions the run's own records allow.
 *
 * Two deliberate omissions:
 *
 * - No progress bar. A dynamic workflow expands as it runs, so the only honest progress is
 *   the count of calls it has dispatched.
 * - No "continue" button for a run whose outcome is unknown. The run reports why it cannot
 *   continue, and the console shows that reason instead of offering a guess.
 */
import { Flowchart20Regular } from '@fluentui/react-icons';
import { useEffect } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useWorkflowStore } from '../../stores/workflowView.ts';
import { useConsoleStore } from '../../stores/useConsoleStore.ts';
import {
  workflowProgress,
  workflowResumeHint,
  workflowStatusLabel,
  workflowSummary,
  type WorkflowRunView,
} from '../../runtime-client/workflows.ts';
import type { BottomBarItemDefinition } from './contract.ts';

export const WORKFLOW_ITEM_ID = 'workflow';

function RunDetail({ run }: { run: WorkflowRunView }): React.ReactElement {
  const cancel = useWorkflowStore((state) => state.cancel);
  const progress = workflowProgress(run);
  const hint = workflowResumeHint(run);
  return (
    <div className="space-y-2 text-xs">
      <div className="flex items-center justify-between gap-2">
        <span className="font-medium text-gray-900">{workflowStatusLabel(run.status)}</span>
        <span className="text-gray-600">{workflowSummary(run)}</span>
      </div>
      <div className="font-mono text-[11px] text-gray-500">
        {run.run_id} · {progress.total} 次调用 · 用量 {run.input_tokens + run.output_tokens}
      </div>
      {run.error !== null && <div className="text-red-600">{run.error}</div>}
      {hint !== null && <div className="text-amber-700">{hint}</div>}
      <ul className="max-h-40 space-y-1 overflow-y-auto">
        {run.calls.map((call) => (
          <li key={call.call_key} className="flex items-center gap-2">
            <span className="w-16 shrink-0 text-gray-500">{workflowStatusLabel(call.status)}</span>
            <span className="min-w-0 flex-1 truncate font-mono">{call.call_key}</span>
            <span className="shrink-0 text-gray-500">{call.role}</span>
          </li>
        ))}
      </ul>
      {run.active && (
        <button
          type="button"
          onClick={() => void cancel(run.run_id)}
          className="h-7 cursor-pointer rounded-control bg-red-500 px-2 text-xs font-medium text-on-accent hover:bg-red-700"
        >
          取消工作流
        </button>
      )}
    </div>
  );
}

export const workflowItem: BottomBarItemDefinition = {
  id: WORKFLOW_ITEM_ID,
  label: '工作流 (Workflow)',
  region: 'left',
  order: 35,
  overlay: 'popover',
  panelLabel: '工作流运行',
  panelClassName:
    'w-[26rem] max-w-[calc(100vw-2rem)] max-h-[calc(100dvh-5rem)] overflow-y-auto no-scrollbar rounded-card border border-line/80 material-flyout flyout-in p-3 font-sans text-left shadow-flyout',
  Trigger: function WorkflowTrigger({ context, open, anchorRef }) {
    const runCount = useWorkflowStore((state) => state.runs.length);
    const active = useWorkflowStore(
      useShallow((state) => state.runs.find((run) => run.active) ?? null),
    );
    const loadRuns = useWorkflowStore((state) => state.loadRuns);
    const threadId = useConsoleStore((state) => state.currentSession.thread_id);
    // Refresh when the session changes, so the panel never shows another project's runs.
    useEffect(() => {
      if (threadId !== '') void loadRuns();
    }, [threadId, loadRuns]);
    return (
      <button
        // The strip anchors the popover to this element: without it the panel has no
        // position to render from, which is how a popover entry silently paints nothing.
        ref={anchorRef}
        data-entry={WORKFLOW_ITEM_ID}
        type="button"
        onClick={(event) => context.toggle(WORKFLOW_ITEM_ID, event.currentTarget)}
        aria-haspopup="dialog"
        aria-expanded={open}
        title={active === null ? '工作流' : workflowSummary(active)}
        className="flex min-w-0 cursor-pointer items-center gap-1 transition-colors hover:text-gray-900"
      >
        <Flowchart20Regular
          aria-hidden="true"
          className="shrink-0 text-gray-500"
          style={{ fontSize: '15px' }}
        />
        <span className="max-w-[14rem] truncate">
          {active === null ? `workflow: ${runCount}` : workflowStatusLabel(active.status)}
        </span>
      </button>
    );
  },
  Content: function WorkflowContent() {
    const runs = useWorkflowStore(useShallow((state) => state.runs));
    const selected = useWorkflowStore((state) => state.selected);
    const error = useWorkflowStore((state) => state.error);
    const select = useWorkflowStore((state) => state.select);
    const loadRuns = useWorkflowStore((state) => state.loadRuns);
    const loading = useWorkflowStore((state) => state.loading);
    return (
      <div className="space-y-3">
        <div className="flex items-center justify-between gap-2">
          <span className="text-xs font-medium text-gray-900">工作流</span>
          {/*
            An explicit refresh, not polling: a run's status changes on the daemon's own
            schedule, and a panel that silently showed a stale list would invite a reader
            to act on a run that has already moved on.
          */}
          <button
            type="button"
            onClick={() => void loadRuns()}
            disabled={loading}
            className="cursor-pointer rounded-control px-2 py-0.5 text-[11px] text-gray-600 transition-colors hover:bg-surface-hover disabled:cursor-default disabled:text-gray-400"
          >
            {loading ? '刷新中' : '刷新'}
          </button>
        </div>
        {error !== null && <div className="text-xs text-red-600">{error}</div>}
        {runs.length === 0 && <div className="text-xs text-gray-500">本项目暂无工作流运行</div>}
        <ul className="space-y-1">
          {runs.map((run) => (
            <li key={run.run_id}>
              <button
                type="button"
                onClick={() => void select(run.run_id)}
                className={`w-full cursor-pointer rounded-control px-2 py-1 text-left text-xs transition-colors hover:bg-surface-hover ${
                  selected?.run_id === run.run_id ? 'bg-surface-hover' : ''
                }`}
              >
                <span className="mr-2 text-gray-500">{workflowStatusLabel(run.status)}</span>
                <span className="font-mono">{run.run_id}</span>
                <span className="ml-2 text-gray-500">{workflowSummary(run)}</span>
              </button>
            </li>
          ))}
        </ul>
        {selected !== null && (
          <div className="border-t border-line pt-2">
            <RunDetail run={selected} />
          </div>
        )}
      </div>
    );
  },
};
