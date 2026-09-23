/**
 * The workflow panel: runs list, execution step pipeline, metrics overview,
 * execution outcome preview, and real-time polling.
 *
 * Sits as the content of the bottom bar's workflow popover. The host owns the
 * portal, floating anchor, outside-click dismissal, and keyboard escape.
 */
import React, { useEffect, useState } from 'react';
import {
  Flowchart20Regular,
  CheckmarkCircle16Filled,
  Circle16Regular,
  DismissCircle16Filled,
  Warning16Filled,
  ArrowSync16Regular,
  Dismiss16Regular,
  ArrowLeft16Regular,
  ChevronRight16Regular,
  ChevronDown16Regular,
  Copy16Regular,
  Checkmark16Regular,
} from '@fluentui/react-icons';
import { useShallow } from 'zustand/react/shallow';
import { useWorkflowStore } from '../stores/workflowView.ts';
import {
  formatWorkflowDuration,
  formatWorkflowTime,
  formatWorkflowTokens,
  workflowProgress,
  workflowResumeHint,
  workflowRoleLabel,
  workflowStatusLabel,
  workflowSummary,
  type WorkflowCallView,
  type WorkflowRunView,
} from '../runtime-client/workflows.ts';

export function WorkflowStatusBadge({ status }: { status: string }): React.ReactElement {
  const label = workflowStatusLabel(status);
  switch (status) {
    case 'completed':
    case 'approved':
      return (
        <span className="inline-flex items-center gap-1 rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-700 dark:border-emerald-800/60 dark:bg-emerald-950/40 dark:text-emerald-300">
          <CheckmarkCircle16Filled aria-hidden="true" className="h-3 w-3 text-emerald-600 dark:text-emerald-400" />
          {label}
        </span>
      );
    case 'running':
      return (
        <span className="inline-flex items-center gap-1 rounded-full border border-blue-200 bg-blue-50 px-2 py-0.5 text-[11px] font-medium text-blue-700 dark:border-blue-800/60 dark:bg-blue-950/40 dark:text-blue-300">
          <span className="relative flex h-2 w-2">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-blue-500 opacity-75"></span>
            <span className="relative inline-flex h-2 w-2 rounded-full bg-blue-600"></span>
          </span>
          {label}
        </span>
      );
    case 'failed':
      return (
        <span className="inline-flex items-center gap-1 rounded-full border border-red-200 bg-red-50 px-2 py-0.5 text-[11px] font-medium text-red-700 dark:border-red-800/60 dark:bg-red-950/40 dark:text-red-300">
          <DismissCircle16Filled aria-hidden="true" className="h-3 w-3 text-red-600 dark:text-red-400" />
          {label}
        </span>
      );
    case 'waiting_approval':
      return (
        <span className="inline-flex items-center gap-1 rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[11px] font-medium text-amber-700 dark:border-amber-800/60 dark:bg-amber-950/40 dark:text-amber-300">
          <Warning16Filled aria-hidden="true" className="h-3 w-3 text-amber-600 dark:text-amber-400" />
          {label}
        </span>
      );
    case 'uncertain':
      return (
        <span className="inline-flex items-center gap-1 rounded-full border border-purple-200 bg-purple-50 px-2 py-0.5 text-[11px] font-medium text-purple-700 dark:border-purple-800/60 dark:bg-purple-950/40 dark:text-purple-300">
          <Warning16Filled aria-hidden="true" className="h-3 w-3 text-purple-600 dark:text-purple-400" />
          {label}
        </span>
      );
    case 'cancelled':
    case 'cancelling':
    default:
      return (
        <span className="inline-flex items-center gap-1 rounded-full border border-gray-200 bg-gray-100 px-2 py-0.5 text-[11px] font-medium text-gray-600 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300">
          {label}
        </span>
      );
  }
}

function CallStepIcon({ status }: { status: string }): React.ReactElement {
  switch (status) {
    case 'completed':
      return <CheckmarkCircle16Filled aria-hidden="true" className="h-3.5 w-3.5 text-emerald-600 dark:text-emerald-400" />;
    case 'running':
      return (
        <span className="relative flex h-3 w-3 items-center justify-center">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-blue-500 opacity-75"></span>
          <span className="relative inline-flex h-2 w-2 rounded-full bg-blue-600"></span>
        </span>
      );
    case 'failed':
      return <DismissCircle16Filled aria-hidden="true" className="h-3.5 w-3.5 text-red-600 dark:text-red-400" />;
    case 'uncertain':
      return <Warning16Filled aria-hidden="true" className="h-3.5 w-3.5 text-purple-600 dark:text-purple-400" />;
    default:
      return <Circle16Regular aria-hidden="true" className="h-3.5 w-3.5 text-gray-400" />;
  }
}

function CallStepItem({ call, index, isLast }: { call: WorkflowCallView; index: number; isLast: boolean }): React.ReactElement {
  const [expanded, setExpanded] = useState(false);
  const totalTokens = call.input_tokens + call.output_tokens;
  const hasError = Boolean(call.error);

  return (
    <div className="relative flex items-start gap-2.5">
      {/* Timeline track line */}
      {!isLast && (
        <div className="absolute left-[7px] top-4 h-[calc(100%+4px)] w-0.5 bg-line/60 dark:bg-gray-700/60" />
      )}
      {/* Node status icon */}
      <div className="relative z-10 mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center bg-surface">
        <CallStepIcon status={call.status} />
      </div>

      {/* Card body */}
      <div className="min-w-0 flex-1 rounded-control border border-line/60 bg-surface/50 p-2 text-xs transition-colors hover:border-line">
        <div className="flex flex-wrap items-center justify-between gap-1.5">
          <div className="flex min-w-0 items-center gap-1.5">
            <span className="font-mono text-[10px] text-gray-400">#{index + 1}</span>
            <span className="truncate font-mono font-medium text-gray-800 dark:text-gray-200">
              {call.call_key}
            </span>
            <span className="rounded-control bg-surface-hover px-1.5 py-0.5 text-[10px] text-gray-600 dark:text-gray-300">
              {workflowRoleLabel(call.role)}
            </span>
          </div>

          <div className="flex shrink-0 items-center gap-1.5 font-numeric text-[11px] text-gray-500">
            {call.attempts > 1 && (
              <span className="rounded-control bg-amber-50 px-1 text-[10px] text-amber-700 dark:bg-amber-950/40 dark:text-amber-300">
                重试 {call.attempts} 次
              </span>
            )}
            {totalTokens > 0 && <span>{formatWorkflowTokens(totalTokens)} tok</span>}
            {hasError && (
              <button
                type="button"
                onClick={() => setExpanded((v) => !v)}
                className="cursor-pointer text-red-600 hover:text-red-700"
              >
                {expanded ? '隐藏错误' : '查看错误'}
              </button>
            )}
          </div>
        </div>

        {hasError && (expanded || call.status === 'failed') && (
          <div className="mt-1.5 rounded-control border border-red-200/80 bg-red-50/70 p-1.5 font-mono text-[11px] text-red-700 dark:border-red-900/60 dark:bg-red-950/30 dark:text-red-300">
            {call.error}
          </div>
        )}
      </div>
    </div>
  );
}

function RunDetailView({ run }: { run: WorkflowRunView }): React.ReactElement {
  const cancel = useWorkflowStore((state) => state.cancel);
  const unselect = useWorkflowStore((state) => state.unselect);
  const progress = workflowProgress(run);
  const hint = workflowResumeHint(run);
  const totalTokens = run.input_tokens + run.output_tokens;
  const duration = formatWorkflowDuration(run.created_at, run.finished_at);
  const [showResult, setShowResult] = useState(true);
  const [copied, setCopied] = useState(false);

  const handleCopyResult = () => {
    if (run.result === null) return;
    const text = typeof run.result === 'string' ? run.result : JSON.stringify(run.result, null, 2);
    void navigator.clipboard.writeText(text);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="space-y-3">
      {/* Navigation bar */}
      <div className="flex items-center justify-between gap-2 border-b border-line/60 pb-2">
        <button
          type="button"
          onClick={unselect}
          className="flex cursor-pointer items-center gap-1 text-xs text-accent transition-colors hover:underline"
        >
          <ArrowLeft16Regular aria-hidden="true" />
          <span>返回运行列表</span>
        </button>
        {run.active && (
          <button
            type="button"
            onClick={() => void cancel(run.run_id)}
            className="cursor-pointer rounded-control border border-red-200 bg-red-50 px-2 py-0.5 text-xs font-medium text-red-700 transition-colors hover:bg-red-100 active:bg-red-200 dark:border-red-800 dark:bg-red-950/40 dark:text-red-300"
          >
            取消工作流
          </button>
        )}
      </div>

      {/* Run header */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-mono text-xs font-semibold text-gray-900 dark:text-gray-100">
              {run.run_id}
            </span>
            <WorkflowStatusBadge status={run.status} />
          </div>
          <div className="mt-0.5 font-mono text-[11px] text-gray-500">
            {run.workflow_id} · 创建于 {formatWorkflowTime(run.created_at)}
            {duration ? ` · 耗时 ${duration}` : ''}
          </div>
        </div>
      </div>

      {/* Metrics Micro-Cards */}
      <div className="grid grid-cols-3 gap-2">
        <div className="rounded-control border border-line/60 bg-surface-hover/30 p-2 text-center">
          <div className="text-[10px] text-gray-500">调用步骤</div>
          <div className="font-numeric text-xs font-semibold text-gray-800 dark:text-gray-200">
            {progress.completed}/{progress.total}
          </div>
        </div>
        <div className="rounded-control border border-line/60 bg-surface-hover/30 p-2 text-center">
          <div className="text-[10px] text-gray-500">总用量</div>
          <div className="font-numeric text-xs font-semibold text-gray-800 dark:text-gray-200">
            {formatWorkflowTokens(totalTokens)}
          </div>
        </div>
        <div className="rounded-control border border-line/60 bg-surface-hover/30 p-2 text-center">
          <div className="text-[10px] text-gray-500">输入 / 输出</div>
          <div className="font-numeric text-[11px] text-gray-600 dark:text-gray-300">
            {formatWorkflowTokens(run.input_tokens)} / {formatWorkflowTokens(run.output_tokens)}
          </div>
        </div>
      </div>

      {/* Run error alert */}
      {run.error !== null && (
        <div className="rounded-control border border-red-200 bg-red-50 p-2 text-xs text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
          <div className="flex items-center gap-1.5 font-medium">
            <DismissCircle16Filled aria-hidden="true" className="h-3.5 w-3.5" />
            运行失败
          </div>
          <div className="mt-1 font-mono text-[11px]">{run.error}</div>
        </div>
      )}

      {/* Resume hint / blockers alert */}
      {hint !== null && run.error === null && (
        <div className="rounded-control border border-amber-200 bg-amber-50 p-2 text-xs text-amber-800 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300">
          <div className="flex items-center gap-1.5 font-medium">
            <Warning16Filled aria-hidden="true" className="h-3.5 w-3.5 text-amber-600" />
            状态提示
          </div>
          <div className="mt-0.5 text-[11px]">{hint}</div>
        </div>
      )}

      {/* Execution Call Pipeline */}
      <div className="space-y-1.5">
        <div className="flex items-center justify-between text-xs font-medium text-gray-700 dark:text-gray-300">
          <span>调用时序与执行角色</span>
          <span className="font-mono text-[10px] text-gray-400">{run.calls.length} 步</span>
        </div>
        {run.calls.length === 0 ? (
          <div className="rounded-control border border-dashed border-line p-3 text-center text-xs text-gray-400">
            暂未派发任何子调用
          </div>
        ) : (
          <div className="fluent-scrollbar max-h-56 space-y-2 overflow-y-auto pr-1">
            {run.calls.map((call, idx) => (
              <CallStepItem
                key={call.call_key}
                call={call}
                index={idx}
                isLast={idx === run.calls.length - 1}
              />
            ))}
          </div>
        )}
      </div>

      {/* Outcome / Result preview */}
      {run.result !== null && (
        <div className="space-y-1 rounded-control border border-line/60 bg-surface/40 p-2">
          <div className="flex items-center justify-between">
            <button
              type="button"
              onClick={() => setShowResult((v) => !v)}
              className="flex cursor-pointer items-center gap-1 text-xs font-medium text-gray-700 transition-colors hover:text-gray-900 dark:text-gray-300"
            >
              {showResult ? (
                <ChevronDown16Regular aria-hidden="true" />
              ) : (
                <ChevronRight16Regular aria-hidden="true" />
              )}
              <span>执行产物 / 结果 (Output)</span>
            </button>
            <button
              type="button"
              onClick={handleCopyResult}
              className="flex cursor-pointer items-center gap-1 rounded-control px-1.5 py-0.5 text-[11px] text-gray-500 transition-colors hover:bg-surface-hover hover:text-gray-800 active:bg-surface-pressed"
            >
              {copied ? (
                <>
                  <Checkmark16Regular aria-hidden="true" className="text-emerald-600" />
                  <span className="text-emerald-600">已复制</span>
                </>
              ) : (
                <>
                  <Copy16Regular aria-hidden="true" />
                  <span>复制</span>
                </>
              )}
            </button>
          </div>
          {showResult && (
            <pre className="fluent-scrollbar max-h-36 overflow-y-auto whitespace-pre-wrap rounded-control bg-surface-hover/60 p-2 font-mono text-[11px] text-gray-800 dark:text-gray-200">
              {typeof run.result === 'string'
                ? run.result
                : JSON.stringify(run.result, null, 2)}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

export const WorkflowPanel: React.FC<{ onClose?: () => void }> = ({ onClose }) => {
  const runs = useWorkflowStore(useShallow((state) => state.runs));
  const selected = useWorkflowStore((state) => state.selected);
  const error = useWorkflowStore((state) => state.error);
  const select = useWorkflowStore((state) => state.select);
  const loadRuns = useWorkflowStore((state) => state.loadRuns);
  const loading = useWorkflowStore((state) => state.loading);

  // Active polling: poll every 2.5s if any run is currently active
  const hasActiveRun = runs.some((r) => r.active) || selected?.active === true;
  useEffect(() => {
    if (!hasActiveRun) return;
    const interval = window.setInterval(() => {
      if (selected !== null) {
        void select(selected.run_id);
      } else {
        void loadRuns();
      }
    }, 2500);
    return () => window.clearInterval(interval);
  }, [hasActiveRun, selected, select, loadRuns]);

  return (
    <div className="select-none space-y-3 font-sans">
      {/* Global Top Bar */}
      <div className="flex items-center justify-between border-b border-line/60 pb-2">
        <div className="flex min-w-0 items-center gap-2">
          <Flowchart20Regular
            aria-hidden="true"
            className="shrink-0 text-accent"
            style={{ fontSize: '16px' }}
          />
          <span className="truncate font-semibold text-gray-800 dark:text-gray-200 text-xs">
            工作流运行 (Workflow)
          </span>
          {runs.length > 0 && (
            <span className="rounded-full bg-surface-hover px-1.5 py-0.2 text-[10px] text-gray-500">
              共 {runs.length} 次
            </span>
          )}
        </div>

        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => {
              if (selected !== null) {
                void select(selected.run_id);
              } else {
                void loadRuns();
              }
            }}
            disabled={loading}
            title="刷新"
            className="flex cursor-pointer items-center gap-1 rounded-control px-1.5 py-0.5 text-[11px] text-gray-600 transition-colors hover:bg-surface-hover hover:text-gray-900 active:bg-surface-pressed disabled:cursor-default disabled:text-gray-400"
          >
            <ArrowSync16Regular
              aria-hidden="true"
              className={`h-3 w-3 ${loading ? 'animate-spin' : ''}`}
            />
            <span>{loading ? '刷新中' : '刷新'}</span>
          </button>
          {onClose && (
            <button
              type="button"
              onClick={onClose}
              title="关闭 (Esc)"
              className="ui-icon-button ui-compact shrink-0 text-gray-400 hover:text-gray-700"
            >
              <Dismiss16Regular aria-hidden="true" />
            </button>
          )}
        </div>
      </div>

      {error !== null && (
        <div className="rounded-control border border-red-200 bg-red-50 p-2 text-xs text-red-600 dark:border-red-900/60 dark:bg-red-950/40 dark:text-red-300">
          {error}
        </div>
      )}

      {selected !== null ? (
        <RunDetailView run={selected} />
      ) : (
        /* Runs List */
        <div className="space-y-2">
          {runs.length === 0 ? (
            <div className="py-6 text-center">
              <div className="mx-auto flex h-10 w-10 items-center justify-center rounded-full bg-surface-hover text-gray-400">
                <Flowchart20Regular style={{ fontSize: '22px' }} />
              </div>
              <div className="mt-2 text-xs font-medium text-gray-700 dark:text-gray-300">
                本项目暂无工作流运行
              </div>
              <div className="mx-auto mt-1 max-w-xs text-[11px] text-gray-500 leading-relaxed">
                工作流是由模型编排的多角色协作引擎，支持状态回放、步骤隔离与断点恢复。
              </div>
            </div>
          ) : (
            <ul className="fluent-scrollbar max-h-72 space-y-1.5 overflow-y-auto pr-1">
              {runs.map((run) => {
                const totalTokens = run.input_tokens + run.output_tokens;
                return (
                  <li key={run.run_id}>
                    <button
                      type="button"
                      onClick={() => void select(run.run_id)}
                      className="group flex w-full cursor-pointer items-center justify-between rounded-card border border-line/60 bg-surface/50 p-2 text-left transition-colors hover:border-accent/40 hover:bg-surface-hover active:bg-surface-pressed"
                    >
                      <div className="min-w-0 flex-1 space-y-1">
                        <div className="flex items-center gap-1.5">
                          <WorkflowStatusBadge status={run.status} />
                          <span className="truncate font-mono text-xs font-medium text-gray-800 dark:text-gray-200">
                            {run.run_id}
                          </span>
                        </div>
                        <div className="flex items-center gap-2 text-[11px] text-gray-500">
                          <span>{workflowSummary(run)}</span>
                          {totalTokens > 0 && (
                            <span className="font-numeric">
                              · {formatWorkflowTokens(totalTokens)} tok
                            </span>
                          )}
                          <span>· {formatWorkflowTime(run.created_at)}</span>
                        </div>
                      </div>
                      <ChevronRight16Regular
                        aria-hidden="true"
                        className="h-4 w-4 shrink-0 text-gray-400 transition-transform group-hover:translate-x-0.5 group-hover:text-accent"
                      />
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  );
};
