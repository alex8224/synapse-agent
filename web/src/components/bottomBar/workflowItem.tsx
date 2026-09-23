/**
 * The strip's workflow entry: shows project workflow status, opening the WorkflowPanel popover.
 *
 * The panel is a `popover`, so the host owns the portal, the outside-click rule and the
 * Escape handling.
 */
import { Flowchart20Regular } from '@fluentui/react-icons';
import { useEffect } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useWorkflowStore } from '../../stores/workflowView.ts';
import { useConsoleStore } from '../../stores/useConsoleStore.ts';
import { WorkflowPanel } from '../WorkflowPanel.tsx';
import {
  formatWorkflowTokens,
  workflowStatusLabel,
  workflowSummary,
} from '../../runtime-client/workflows.ts';
import type { BottomBarItemDefinition } from './contract.ts';

export const WORKFLOW_ITEM_ID = 'workflow';

export const workflowItem: BottomBarItemDefinition = {
  id: WORKFLOW_ITEM_ID,
  label: '工作流 (Workflow)',
  region: 'left',
  order: 35,
  overlay: 'popover',
  panelLabel: '工作流运行',
  panelClassName:
    'w-[32rem] max-w-[calc(100vw-2rem)] max-h-[calc(100dvh-5rem)] overflow-y-auto fluent-scrollbar rounded-card border border-line/80 material-flyout flyout-in p-3 font-sans text-left shadow-flyout',
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

    const title = active === null
      ? (runCount > 0 ? `工作流：共 ${runCount} 次运行` : '工作流 (Workflow)')
      : `工作流运行中：${workflowSummary(active)} (${formatWorkflowTokens(active.input_tokens + active.output_tokens)} tok)`;

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
        title={title}
        className={`flex min-w-0 cursor-pointer items-center gap-1.5 transition-colors hover:text-gray-900 ${
          active !== null ? 'text-accent font-medium' : 'text-gray-700'
        }`}
      >
        {active !== null ? (
          <span className="relative flex h-2 w-2 mr-0.5">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-75"></span>
            <span className="relative inline-flex h-2 w-2 rounded-full bg-accent"></span>
          </span>
        ) : (
          <Flowchart20Regular
            aria-hidden="true"
            className="shrink-0 text-gray-500"
            style={{ fontSize: '15px' }}
          />
        )}
        <span className="max-w-[14rem] truncate">
          {active !== null
            ? `工作流: ${workflowStatusLabel(active.status)}`
            : (runCount > 0 ? `工作流: ${runCount}` : '工作流')}
        </span>
      </button>
    );
  },
  Content: function WorkflowContent({ context }) {
    return <WorkflowPanel onClose={context.close} />;
  },
};
