/**
 * The status strip's Todo entry: shows live checklist progress from write_todos,
 * opening the TodoPanel as a popover.
 *
 * Like the other strip entries, the trigger keeps its own narrow subscription
 * and the popover hangs from FloatingPanel anchored to this trigger.
 */
import { TasksApp20Regular } from '@fluentui/react-icons';
import { useConsoleStore } from '../../stores/useConsoleStore.ts';
import { latestTodos, todoPanelLabel } from '../../stores/todoView.ts';
import { TodoPanel } from '../TodoPanel.tsx';
import type { BottomBarAvailability, BottomBarItemDefinition } from './contract.ts';

export const TODO_ITEM_ID = 'todo';

export const todoAvailability: BottomBarAvailability = {
  getSnapshot(): boolean {
    return latestTodos(useConsoleStore.getState().messages) !== null;
  },
  subscribe(listener: () => void): () => void {
    let prev = latestTodos(useConsoleStore.getState().messages) !== null;
    return useConsoleStore.subscribe((state) => {
      const next = latestTodos(state.messages) !== null;
      if (next !== prev) {
        prev = next;
        listener();
      }
    });
  },
};

export const todoItem: BottomBarItemDefinition = {
  id: TODO_ITEM_ID,
  label: '待办任务',
  region: 'left',
  order: 25,
  overlay: 'popover',
  panelLabel: '任务列表',
  panelClassName:
    'w-88 max-w-[calc(100vw-2rem)] rounded-card border border-line/80 material-flyout flyout-in p-3 font-sans text-left shadow-flyout',
  availability: todoAvailability,
  Trigger: function TodoTrigger({ context, open, anchorRef }) {
    const messages = useConsoleStore((state) => state.messages);
    const view = latestTodos(messages);
    if (!view) return null;

    const total = view.items.length + view.omitted;
    const allDone = view.done === total && total > 0;
    const active = view.active > 0;

    return (
      <button
        ref={anchorRef}
        data-entry={TODO_ITEM_ID}
        type="button"
        onClick={(event) => context.toggle(TODO_ITEM_ID, event.currentTarget)}
        title={todoPanelLabel(view)}
        aria-haspopup="dialog"
        aria-expanded={open}
        className="flex cursor-pointer items-center gap-1.5 transition-colors hover:text-gray-900"
      >
        <TasksApp20Regular
          aria-hidden="true"
          className={`shrink-0 ${allDone ? 'text-emerald-600' : active ? 'text-accent animate-pulse' : 'text-gray-500'}`}
          style={{ fontSize: '15px' }}
        />
        <span className="text-gray-700">
          todos: {view.done}/{total}
        </span>
      </button>
    );
  },
  Content: function TodoContent({ context }) {
    return <TodoPanel onClose={context.close} />;
  },
};
