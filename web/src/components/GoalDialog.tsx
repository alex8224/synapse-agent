import React, { useState } from 'react';
import { useConsoleStore } from '../stores/useConsoleStore';
import {
  GOAL_OBJECTIVE_MAX_CHARS,
  goalActions,
  normalizeGoalBudget,
  normalizeGoalObjective,
} from '../stores/goalView.ts';
import type { GoalAction } from '../stores/goalView.ts';

/**
 * Goal management dialog: the small RPC surface for a session's long-running
 * goal, mirroring the TUI `/goal` commands.
 *
 * Every button drives the store's `runtime.session.goal.*` actions.  The
 * objective and the optional budget are typed into the dialog and validated
 * locally, so there is no `window.prompt` and no unvalidated value ever reaches
 * the wire: an empty / oversized objective or a non-positive budget is refused
 * here, exactly as the goal domain would refuse it.  `pause` only asks *this*
 * session's own live turn to stop; `resume` is status-only and never starts a
 * follow-up turn.
 *
 * `clear` is a two-step action: the first click only arms an explicit in-modal
 * confirmation (never `window.prompt`), and the confirmation is bound to the
 * goal currently on display, so a goal replaced while the prompt is open is
 * refused by the server instead of being cleared.  Clearing a goal cancels no
 * turn, in this session or any other.
 */

/** Action button label, mirroring the `/goal` sub-commands. */
const ACTION_LABEL: Record<GoalAction, string> = {
  edit: '编辑',
  pause: '暂停',
  resume: '恢复',
  clear: '清除',
};

export interface GoalDialogProps {
  onClose: () => void;
}

export const GoalDialog: React.FC<GoalDialogProps> = ({ onClose }) => {
  const {
    currentSession,
    goal,
    goalBusy,
    goalActionError,
    goalNotice,
    setGoal,
    editGoal,
    clearGoal,
    pauseGoal,
    resumeGoal,
    dismissGoalAlert,
  } = useConsoleStore();

  const [objective, setObjective] = useState('');
  const [budget, setBudget] = useState('');
  const [editing, setEditing] = useState(false);
  // The goal id awaiting the explicit second confirmation of a `clear`.  It is
  // bound to the goal on display: if the live goal changes, the armed
  // confirmation no longer matches and is dropped (see `clearPending`).
  const [confirmingClear, setConfirmingClear] = useState<string | null>(null);

  // A goal is bound to a live session; before one is attached the store refuses
  // the write, so the dialog disables its controls instead of pretending.
  const sessionOpen = currentSession.thread_id !== '';
  const actions = goal === null ? [] : goalActions(goal.status);
  const showForm = goal === null || editing;
  const objectiveValid = normalizeGoalObjective(objective) !== null;
  const budgetValid = normalizeGoalBudget(budget) !== 'invalid';
  const disabled = !sessionOpen || goalBusy;
  // A clear stays armed only while the confirmation still refers to the goal on
  // display: a replaced or already-cleared goal drops the pending confirmation.
  const clearPending = confirmingClear !== null && goal !== null && goal.goal_id === confirmingClear;

  const submit = async () => {
    const text = normalizeGoalObjective(objective);
    if (text === null || disabled) return;
    if (editing) {
      if (await editGoal(text)) {
        setEditing(false);
        setObjective('');
      }
      return;
    }
    const parsed = normalizeGoalBudget(budget);
    if (parsed === 'invalid') return;
    if (await setGoal(text, parsed)) {
      setObjective('');
      setBudget('');
    }
  };

  const mutate = async (action: GoalAction) => {
    if (goalBusy) return;
    if (action === 'edit') {
      setConfirmingClear(null);
      setEditing(true);
      setObjective(goal?.objective ?? '');
      return;
    }
    if (action === 'clear') {
      // First step: arm an explicit in-modal confirmation bound to the goal on
      // display.  The clear is never issued straight from this click and no
      // `window.prompt` is involved.
      setConfirmingClear(goal?.goal_id ?? null);
      return;
    }
    setConfirmingClear(null);
    if (action === 'pause') await pauseGoal();
    else await resumeGoal();
  };

  const confirmClear = async () => {
    if (goalBusy || !clearPending) return;
    // Second step: clear exactly the goal the confirmation displayed.  The store
    // forwards this id as `expected_goal_id`, so a goal replaced in the meantime
    // is refused (`conflict`) rather than silently cleared.
    await clearGoal(confirmingClear);
    setConfirmingClear(null);
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/20 p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md space-y-2 rounded-lg border border-gray-200 bg-white p-5 font-sans shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-gray-100 pb-2">
          <span className="text-sm font-bold text-gray-900">目标 (Goal)</span>
          <button
            onClick={onClose}
            title="关闭 (Esc)"
            className="material-symbols-outlined cursor-pointer text-[18px] text-gray-400 hover:text-gray-600"
          >
            close
          </button>
        </div>

        {!sessionOpen && (
          <div className="rounded bg-gray-50 px-2 py-1.5 text-[11px] text-gray-500">
            请先打开一个会话，再管理目标。
          </div>
        )}

        {goal !== null && (
          <div className="rounded bg-gray-50 px-2 py-1.5 text-[11px] text-gray-600">
            <div className="font-medium text-gray-800">
              {goal.label} · {goal.objective}
            </div>
            <div className="mt-0.5 text-gray-500">
              {goal.token_budget === null
                ? `${goal.tokens_used} tokens · ${goal.time_used_seconds}s`
                : `${goal.tokens_used} / ${goal.token_budget} tokens · ${goal.time_used_seconds}s`}
            </div>
          </div>
        )}

        {goal !== null && !editing && (
          <div className="space-y-1.5">
            <div className="flex flex-wrap gap-1.5">
              {actions.map((action) => (
                <button
                  key={action}
                  type="button"
                  disabled={disabled}
                  onClick={() => void mutate(action)}
                  className={`rounded border px-2 py-1 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                    action === 'clear'
                      ? 'border-red-200 text-red-600 hover:bg-red-50'
                      : 'border-gray-200 text-gray-700 hover:bg-gray-100'
                  }`}
                >
                  {ACTION_LABEL[action]}
                </button>
              ))}
            </div>
            {clearPending && (
              <div className="rounded border border-red-200 bg-red-50 px-2 py-1.5 text-[11px] text-red-700">
                <div>确定要清除当前目标吗？清除不会取消任何会话回合。</div>
                <div className="mt-1 flex items-center justify-end gap-2">
                  <button
                    type="button"
                    onClick={() => setConfirmingClear(null)}
                    className="rounded px-2 py-0.5 text-xs text-gray-500 hover:bg-white"
                  >
                    取消
                  </button>
                  <button
                    type="button"
                    disabled={goalBusy}
                    onClick={() => void confirmClear()}
                    className="rounded bg-red-600 px-2 py-0.5 text-xs text-white disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    确认清除
                  </button>
                </div>
              </div>
            )}
          </div>
        )}

        {showForm && (
          <div className="space-y-1.5">
            <textarea
              value={objective}
              onChange={(e) => setObjective(e.target.value)}
              placeholder={`目标描述（1-${GOAL_OBJECTIVE_MAX_CHARS} 字）`}
              rows={3}
              disabled={disabled}
              className="w-full resize-none rounded border border-gray-200 px-2 py-1 text-xs text-gray-800 focus:outline-none disabled:bg-gray-50"
            />
            {!editing && (
              <input
                value={budget}
                onChange={(e) => setBudget(e.target.value)}
                placeholder="token 预算（可选，正整数）"
                inputMode="numeric"
                disabled={disabled}
                className="w-full rounded border border-gray-200 px-2 py-1 text-xs text-gray-800 focus:outline-none disabled:bg-gray-50"
              />
            )}
            {!budgetValid && (
              <div className="text-[10px] text-red-600">token 预算需为正整数。</div>
            )}
            <div className="flex items-center justify-end gap-2">
              {editing && (
                <button
                  type="button"
                  onClick={() => {
                    setEditing(false);
                    setObjective('');
                  }}
                  className="rounded px-2 py-1 text-xs text-gray-500 hover:bg-gray-100"
                >
                  取消
                </button>
              )}
              <button
                type="button"
                disabled={disabled || !objectiveValid || (!editing && !budgetValid)}
                onClick={() => void submit()}
                className="rounded bg-gray-900 px-2 py-1 text-xs text-white disabled:cursor-not-allowed disabled:opacity-50"
              >
                {editing ? '保存' : '设置'}
              </button>
            </div>
          </div>
        )}

        {(goalActionError !== null || goalNotice !== null) && (
          <div
            className={`flex items-start justify-between gap-2 rounded px-2 py-1.5 text-[11px] ${
              goalActionError !== null ? 'bg-red-50 text-red-600' : 'bg-blue-50 text-blue-600'
            }`}
          >
            <span>{goalActionError ?? goalNotice}</span>
            <button
              onClick={dismissGoalAlert}
              className="material-symbols-outlined cursor-pointer text-[14px] opacity-70 hover:opacity-100"
            >
              close
            </button>
          </div>
        )}
      </div>
    </div>
  );
};
