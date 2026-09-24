/**
 * Status strip entry for the integrated terminal.
 *
 * Implements:
 * - Direct click to toggle central bottom terminal dock
 * - Active state highlight when terminal panel is open
 * - Displays active session count
 */
import { WindowConsole20Regular } from '@fluentui/react-icons';
import { useShallow } from 'zustand/react/shallow';
import { useTerminalStore } from '../../stores/useTerminalStore.ts';
import type { BottomBarItemDefinition } from './contract.ts';

export const TERMINAL_ITEM_ID = 'terminal';

export const terminalItem: BottomBarItemDefinition = {
  id: TERMINAL_ITEM_ID,
  label: '终端',
  region: 'left',
  order: 25,
  Trigger: function TerminalTrigger({ anchorRef }) {
    const { open, sessions, toggleOpen } = useTerminalStore(
      useShallow((s) => ({
        open: s.open,
        sessions: s.sessions,
        toggleOpen: s.toggleOpen,
      })),
    );

    const activeCount = sessions.length;
    const label = activeCount > 0 ? `终端: ${activeCount}` : '终端';

    return (
      <button
        ref={anchorRef}
        data-entry={TERMINAL_ITEM_ID}
        type="button"
        onClick={() => toggleOpen()}
        title="打开/收起底部集成终端 (Ctrl+`)"
        aria-expanded={open}
        className={`flex cursor-pointer items-center gap-1.5 rounded-control px-1.5 py-0.5 transition-colors ${
          open
            ? 'bg-accent/15 text-accent font-semibold'
            : 'text-gray-700 hover:text-gray-900 hover:bg-surface-hover'
        }`}
      >
        <WindowConsole20Regular
          aria-hidden="true"
          className={`shrink-0 ${open ? 'text-accent' : 'text-gray-500'}`}
          style={{ fontSize: '15px' }}
        />
        <span>{label}</span>
      </button>
    );
  },
};
