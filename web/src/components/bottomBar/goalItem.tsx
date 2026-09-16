/**
 * The strip's goal entry: the live goal (or `goal: 未设置`), opening the F6
 * dialog.
 *
 * The dialog is a `modal`: it portals its own scrim and owns its own Escape and
 * focus, so the strip keeps no second listener for it.
 */
import { Flag20Regular } from '@fluentui/react-icons';
import { useConsoleStore } from '../../stores/useConsoleStore.ts';
import { goalLabel, goalTooltip } from '../../stores/goalView.ts';
import { withChord } from '../consoleShortcuts.ts';
import { GoalDialog } from '../GoalDialog.tsx';
import type { BottomBarItemDefinition } from './contract.ts';

export const GOAL_ITEM_ID = 'goal';

/** Goal status label -> text colour, mirroring the TUI goal indicator styles. */
const GOAL_STATUS_CLASS: Record<string, string> = {
  active: 'font-medium text-gray-900',
  paused: 'text-gray-400',
  stalled: 'text-yellow-600',
  'usage limited': 'text-yellow-600',
  'limited by budget': 'text-yellow-600',
  complete: 'text-green-600',
};

export const goalItem: BottomBarItemDefinition = {
  id: GOAL_ITEM_ID,
  label: '目标 (Goal)',
  region: 'left',
  order: 30,
  shortcutKey: 'F6',
  overlay: 'modal',
  Trigger: function GoalTrigger({ context, open }) {
    const goal = useConsoleStore((state) => state.goal);
    // An absent goal renders nothing at all (never a placeholder).
    const goalText = goalLabel(goal);
    const goalClass = goal === null ? '' : (GOAL_STATUS_CLASS[goal.label] ?? 'text-gray-600');
    return (
      <button
        data-entry={GOAL_ITEM_ID}
        type="button"
        onClick={() => context.toggle(GOAL_ITEM_ID)}
        title={goal === null ? withChord('设置目标', 'F6') : goalTooltip(goal)}
        aria-haspopup="dialog"
        aria-expanded={open}
        className={`flex min-w-0 cursor-pointer items-center gap-1 transition-colors hover:text-gray-900 ${goalClass}`}
      >
        <Flag20Regular
          aria-hidden="true"
          className="shrink-0 text-gray-500"
          style={{ fontSize: '15px' }}
        />
        <span className="max-w-[18rem] truncate">
          {goalText === '' ? 'goal: 未设置' : goalText}
        </span>
      </button>
    );
  },
  Content: function GoalContent({ context }) {
    return <GoalDialog onClose={context.close} />;
  },
};
