import React, { useState, useEffect, useRef } from 'react';
import { useConsoleStore } from '../stores/useConsoleStore';
import {
  contextOccupancy,
  sessionUsageSegments,
  turnStatSegments,
} from '../stores/usageView.ts';
import { goalLabel, goalTooltip } from '../stores/goalView.ts';
import { McpPanel } from './McpPanel.tsx';
import { GoalDialog } from './GoalDialog.tsx';

/** Goal status label -> text colour, mirroring the TUI goal indicator styles. */
const GOAL_STATUS_CLASS: Record<string, string> = {
  active: 'font-medium text-gray-900',
  paused: 'text-gray-400',
  stalled: 'text-yellow-600',
  'usage limited': 'text-yellow-600',
  'limited by budget': 'text-yellow-600',
  complete: 'text-green-600',
};

/** Full shortcut list for the F1 dialog. */
const HELP_ROWS: Array<{ keys: string; label: string }> = [
  { keys: 'Enter', label: '发送指令 / 运行态下排队插话' },
  { keys: 'Ctrl + C', label: '中止当前运行中的轮次' },
  { keys: 'Ctrl + B', label: '展开 / 收起侧边栏' },
  { keys: 'Ctrl + N', label: '新建会话' },
  { keys: 'Ctrl + K', label: '搜索会话' },
  { keys: 'F1', label: '打开快捷键帮助' },
  { keys: 'F2', label: '切换大语言模型' },
  { keys: 'F5', label: 'MCP 服务器' },
  { keys: 'F6', label: '目标管理 (Goal)' },
];

export const BottomBar: React.FC = () => {
  const {
    mcpStatus,
    mcpServers,
    runtimeStatus,
    usage,
    sessionUsage,
    contextWindow,
    goal,
  } = useConsoleStore();

  const [showMcpPanel, setShowMcpPanel] = useState(false);
  const [showGoalDialog, setShowGoalDialog] = useState(false);
  const [showHelp, setShowHelp] = useState(false);

  const closeOthers = () => {
    setShowMcpPanel(false);
  };

  // The MCP popover closes as soon as it loses focus: a click anywhere outside
  // its own trigger+panel closes it. The trigger is part of the same wrapper on
  // purpose, so its own click toggles the popover instead of fighting this
  // handler. (The model / reasoning pickers keep the same contract, but they now
  // live in the composer — see `ModelControls`.)
  const mcpRef = useRef<HTMLDivElement | null>(null);
  const popoverOpen = showMcpPanel;

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'F1') {
        e.preventDefault();
        setShowHelp((v) => !v);
      } else if (e.key === 'F5') {
        e.preventDefault();
        closeOthers();
        setShowMcpPanel((v) => !v);
      } else if (e.key === 'F6') {
        e.preventDefault();
        closeOthers();
        setShowGoalDialog((v) => !v);
      } else if (e.key === 'Escape') {
        // The popover titles promise "关闭 (Esc)": honour it here as well as in
        // the top bar, so every advertised dismissal path really works.
        closeOthers();
        setShowHelp(false);
        setShowGoalDialog(false);
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, []);

  useEffect(() => {
    if (!popoverOpen) return;
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as Node | null;
      if (target === null) return;
      const inside = [mcpRef].some((ref) =>
        ref.current?.contains(target),
      );
      if (!inside) closeOthers();
    };
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [popoverOpen]);

  const busy = runtimeStatus === 'running';
  // The bar reports this turn's speed/latency/steps, then the session's usage as
  // two raw groups (totals, then context/hit share).  No label and no tooltip:
  // the numbers are printed as the runtime reported them.
  const telemetry = [
    ...turnStatSegments(usage),
    ...sessionUsageSegments(sessionUsage, contextOccupancy(usage), contextWindow),
  ];
  // An absent goal renders nothing at all (never a placeholder).
  const goalText = goalLabel(goal);
  const goalClass = goal === null ? '' : (GOAL_STATUS_CLASS[goal.label] ?? 'text-gray-600');

  return (
    <>
      {/*
        Layout decision (kept deliberately): three tracks `1fr auto 1fr` keep the
        telemetry block in the exact horizontal centre of the bar, because both
        flexible tracks resolve to the same leftover width. The left column
        carries activity + model / reasoning / MCP / goal, the centre carries the
        current turn's telemetry, and the right track stays an empty, symmetric
        spacer (F1 opens the full shortcut list). Do not switch the centre to a
        right-aligned column: the bar must stay centre-weighted.
      */}
      {/*
        `whitespace-nowrap` + `overflow-hidden` are load-bearing: the bar is a
        fixed 28px strip, and a squeezed label that wraps would double a row's
        line box and push the whole bar out of alignment.  Long labels truncate
        (or clip at the track edge) instead of wrapping.

        It is a real flex child of the app column rather than an overlay: while
        it was `fixed`, the middle row still stretched to the viewport bottom and
        the bar covered the sidebar's own footer (its settings entry), leaving a
        strip of it unreachable.
      */}
      <footer className="grid h-7 w-full grid-cols-[1fr_auto_1fr] items-center gap-4 overflow-hidden whitespace-nowrap border-t border-[#e5e7eb] bg-white px-3 font-mono text-[11px] text-gray-500 shrink-0 select-none">
        {/* Left: activity + configuration */}
        <div className="flex min-w-0 items-center gap-2.5">
          <span
            className={`flex shrink-0 items-center gap-1.5 font-sans text-[11px] font-medium ${
              busy ? 'text-blue-600' : 'text-gray-500'
            }`}
          >
            <span
              className={`h-1.5 w-1.5 rounded-full ${busy ? 'animate-pulse bg-blue-600' : 'bg-gray-400'}`}
            />
            {busy ? '运行中' : '空闲'}
          </span>

          <span className="text-gray-200">|</span>

          {/* MCP (the model and reasoning pickers moved into the composer) */}
          <div className="relative" ref={mcpRef}>
            <button
              type="button"
              onClick={() => {
                const next = !showMcpPanel;
                closeOthers();
                setShowMcpPanel(next);
              }}
              title="管理 MCP 服务器 (F5)"
              className="flex items-center gap-1 cursor-pointer transition-colors hover:text-gray-900"
            >
              <span
                className={`material-symbols-outlined text-[14px] ${
                  mcpServers.some((s) => s.enabled) ? 'text-green-600' : 'text-gray-400'
                }`}
              >
                bolt
              </span>
              <span className="text-gray-700">mcp: {mcpStatus}</span>
              <span className="material-symbols-outlined text-[14px] text-gray-400">expand_more</span>
            </button>
            {showMcpPanel && (
              <McpPanel onClose={() => setShowMcpPanel(false)} />
            )}
          </div>

          {/* Goal: the trigger is always visible so a goal can be set; the label
              shows the live goal when there is one and "未设置" otherwise. */}
          <span className="text-gray-200">|</span>
          <button
            type="button"
            onClick={() => {
              closeOthers();
              setShowGoalDialog(true);
            }}
            title={goal === null ? '设置目标 (F6)' : goalTooltip(goal)}
            className={`flex min-w-0 items-center gap-1 cursor-pointer transition-colors hover:text-gray-900 ${goalClass}`}
          >
            <span className="material-symbols-outlined text-[14px] text-gray-500">
              flag
            </span>
            <span className="max-w-[18rem] truncate">
              {goalText === '' ? 'goal: 未设置' : goalText}
            </span>
          </button>
        </div>

        {/* Centre: all turn telemetry (tokens + speed / latency / steps) */}
        <div
          className="flex shrink-0 items-center justify-self-center gap-2 tabular-nums"
        >
          {telemetry.length === 0 ? (
            <span className="font-sans text-[11px] text-gray-300">尚无本轮指标</span>
          ) : (
            telemetry.map((segment, index) => (
              <span key={segment.key} className="flex items-center gap-2">
                {index > 0 && <span className="text-gray-200">|</span>}
                <span className="flex items-baseline gap-1">
                  {segment.label !== '' && (
                    <span className="font-sans text-[10px] text-gray-400">{segment.label}</span>
                  )}
                  <span
                    className={segment.emphasis ? 'font-medium text-gray-800' : 'text-gray-600'}
                  >
                    {segment.value}
                  </span>
                </span>
              </span>
            ))
          )}
        </div>

        {/* Right slot intentionally empty (symmetric spacer): F1 opens the list. */}
        <div className="min-w-0" />
      </footer>

      {showHelp && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/20 p-4"
          onClick={() => setShowHelp(false)}
        >
          <div
            className="w-full max-w-md rounded-lg border border-gray-200 bg-white p-5 font-sans shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-3 flex items-center justify-between border-b pb-2">
              <span className="text-sm font-bold text-gray-900">快捷键与使用帮助</span>
              <button
                onClick={() => setShowHelp(false)}
                title="关闭 (Esc)"
                className="material-symbols-outlined cursor-pointer text-[18px] text-gray-400 hover:text-gray-600"
              >
                close
              </button>
            </div>
            <div className="space-y-1.5 font-mono text-xs text-gray-600">
              {HELP_ROWS.map((row) => (
                <div
                  key={row.keys}
                  className="flex items-center justify-between border-b border-gray-50 py-1 last:border-b-0"
                >
                  <kbd className="rounded border border-gray-200 bg-gray-50 px-1.5 py-0.5 text-[10px] text-gray-500">
                    {row.keys}
                  </kbd>
                  <span className="font-sans">{row.label}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {showGoalDialog && <GoalDialog onClose={() => setShowGoalDialog(false)} />}
    </>
  );
};
