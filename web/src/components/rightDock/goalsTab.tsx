/**
 * Goals & Tasks Tab for the Right Auxiliary Dock (inspired by ZCode Goal Mode).
 *
 * Implements:
 * - Persistent inspection of the active session's long-running goal (RPC-backed)
 * - Budget tracking with token consumption progress bar
 * - Task controls: pause, resume, clear, and edit goal objective
 */
import {
  Target16Regular,
  Play16Regular,
  Pause16Regular,
  Dismiss16Regular,
  Edit16Regular,
  Checkmark16Regular,
} from '@fluentui/react-icons';
import React, { useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useConsoleStore } from '../../stores/useConsoleStore.ts';
import type { RightDockContext, RightDockTabDefinition } from './contract.ts';

export const GoalsContent: React.FC<{ context: RightDockContext }> = () => {
  const {
    goal,
    goalBusy,
    goalActionError,
    setGoal,
    clearGoal,
    pauseGoal,
    resumeGoal,
  } = useConsoleStore(
    useShallow((state) => ({
      goal: state.goal,
      goalBusy: state.goalBusy,
      goalActionError: state.goalActionError,
      setGoal: state.setGoal,
      clearGoal: state.clearGoal,
      pauseGoal: state.pauseGoal,
      resumeGoal: state.resumeGoal,
    })),
  );

  const [isEditing, setIsEditing] = useState(false);
  const [objectiveInput, setObjectiveInput] = useState('');
  const [budgetInput, setBudgetInput] = useState('');

  const handleStartCreate = () => {
    setObjectiveInput('');
    setBudgetInput('');
    setIsEditing(true);
  };

  const handleSaveGoal = async () => {
    if (!objectiveInput.trim()) return;
    const numBudget = budgetInput.trim() ? Number(budgetInput.trim()) : undefined;
    try {
      await setGoal(objectiveInput.trim(), numBudget);
      setIsEditing(false);
    } catch {
      // Handled in store
    }
  };

  const hasBudget = goal?.token_budget !== null && goal?.token_budget !== undefined;
  const tokenUsed = goal?.tokens_used ?? 0;
  const tokenBudget = goal?.token_budget ?? 1;
  const progressPct = hasBudget
    ? Math.min(100, Math.round((tokenUsed / tokenBudget) * 100))
    : null;

  return (
    <div className="flex h-full flex-col overflow-y-auto p-4 text-xs font-sans">
      {goalActionError && (
        <div className="mb-3 rounded bg-red-50 dark:bg-red-900/30 p-2 text-red-600 dark:text-red-300">
          {goalActionError}
        </div>
      )}

      {/* Goal Hero Card */}
      {goal && goal.status !== 'cleared' ? (
        <div className="rounded-card border border-line bg-surface p-4 shadow-card space-y-3">
          <div className="flex items-start justify-between gap-2">
            <div>
              <span className="text-[10px] font-mono uppercase tracking-wider text-gray-400">
                当前长程目标 (Goal)
              </span>
              <h4 className="mt-0.5 text-sm font-bold text-gray-900 leading-snug">
                {goal.objective}
              </h4>
            </div>
            <span
              className={`rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase ${
                goal.status === 'active'
                  ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-400'
                  : goal.status === 'paused'
                  ? 'bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-400'
                  : 'bg-gray-100 text-gray-600 dark:bg-gray-800'
              }`}
            >
              {goal.status}
            </span>
          </div>

          {/* Token Budget Progress */}
          {hasBudget && (
            <div>
              <div className="flex justify-between text-[11px] text-gray-500 font-mono">
                <span>Token 用量预算</span>
                <span>
                  {tokenUsed.toLocaleString()} / {goal.token_budget?.toLocaleString()} ({progressPct}%)
                </span>
              </div>
              <div className="mt-1 h-2 w-full rounded-full bg-sunken overflow-hidden">
                <div
                  className="h-full rounded-full bg-emerald-500 transition-all duration-300"
                  style={{ width: `${progressPct}%` }}
                />
              </div>
            </div>
          )}

          {/* Quick Action Buttons */}
          <div className="flex items-center gap-1.5 pt-2 border-t border-line/60">
            {goal.status === 'active' ? (
              <button
                type="button"
                disabled={goalBusy}
                onClick={() => void pauseGoal()}
                className="ui-button ui-compact text-xs"
              >
                <Pause16Regular /> 暂停
              </button>
            ) : (
              <button
                type="button"
                disabled={goalBusy}
                onClick={() => void resumeGoal()}
                className="ui-button ui-compact ui-primary text-xs"
              >
                <Play16Regular /> 继续执行
              </button>
            )}
            <button
              type="button"
              disabled={goalBusy}
              onClick={() => void clearGoal()}
              className="ui-button ui-compact text-red-600 hover:bg-red-50 dark:hover:bg-red-950 text-xs ml-auto"
            >
              <Dismiss16Regular /> 清除目标
            </button>
          </div>
        </div>
      ) : isEditing ? (
        <div className="rounded-card border border-line bg-surface p-4 shadow-card space-y-3">
          <h4 className="text-sm font-semibold text-gray-900">设定新的长程任务目标</h4>
          <div>
            <label className="text-[11px] text-gray-500">具体目标描述 (Objective)</label>
            <textarea
              value={objectiveInput}
              onChange={(e) => setObjectiveInput(e.target.value)}
              placeholder="例如：重构右侧栏为可扩展架构并接入所有 Tab..."
              className="mt-1 w-full rounded-control border border-line bg-sunken/40 p-2 text-xs outline-none focus:border-accent"
              rows={3}
            />
          </div>
          <div>
            <label className="text-[11px] text-gray-500">Token 消耗预算上限 (可选)</label>
            <input
              type="number"
              value={budgetInput}
              onChange={(e) => setBudgetInput(e.target.value)}
              placeholder="例如：50000"
              className="mt-1 w-full rounded-control border border-line bg-sunken/40 p-1.5 text-xs outline-none focus:border-accent"
            />
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={() => setIsEditing(false)}
              className="ui-button ui-compact text-xs"
            >
              取消
            </button>
            <button
              type="button"
              onClick={() => void handleSaveGoal()}
              disabled={!objectiveInput.trim() || goalBusy}
              className="ui-button ui-compact ui-primary text-xs"
            >
              <Checkmark16Regular /> 确认开始
            </button>
          </div>
        </div>
      ) : (
        <div className="flex flex-1 flex-col items-center justify-center rounded-card border border-dashed border-line p-8 text-center text-gray-400">
          <Target16Regular className="text-3xl text-gray-300 dark:text-gray-600 mb-2" />
          <p className="text-xs font-medium text-gray-700 dark:text-gray-300">
            当前会话暂未设定长程目标
          </p>
          <p className="mt-1 text-[11px] text-gray-500 max-w-[220px]">
            Goal 模式允许 Agent 在自主循环中跟踪 Token 预算与目标完成度。
          </p>
          <button
            type="button"
            onClick={handleStartCreate}
            className="mt-4 ui-button ui-primary text-xs"
          >
            + 设定会话目标
          </button>
        </div>
      )}
    </div>
  );
};

export const goalsTab: RightDockTabDefinition = {
  id: 'goals',
  label: '目标',
  order: 30,
  Icon: Target16Regular,
  badge: () => {
    const goal = useConsoleStore.getState().goal;
    if (!goal || goal.status === 'cleared') return null;
    return goal.status === 'active' ? '●' : null;
  },
  badgeVariant: 'goal',
  Content: GoalsContent,
};
