import { Dismiss20Regular } from '@fluentui/react-icons';
import React from 'react';
import { FloatingPanel } from './FloatingPanel.tsx';

/**
 * The shell the console's context panels share.
 *
 * Each one is a `FloatingPanel` -- portalled to the body and positioned from the
 * control that opened it, because nested in a rail it could not blur the transcript
 * (the rail's own `backdrop-filter` is a backdrop root) and its height stretched the
 * rail's footer.  Inside it every panel paints the same title bar with its close
 * button, and the same label/value rows.
 *
 * The shell lives apart from the actions that open it because the panels no longer
 * share a trigger: the session info opens from the header's session title, while the
 * file browser, runtime diagnostics and logout stay at the sidebar's settings row.
 *
 * `side`/`align` follow the trigger: a panel opens away from the edge its trigger
 * sits on (up from the rail's footer, down from the header's title) and lines up
 * with it -- `center` for a trigger that is itself centred, so the panel reads as
 * belonging to the title rather than to one half of it.
 */
export const ConsolePanel: React.FC<{
  title: string;
  anchor: HTMLElement | null;
  onClose: () => void;
  side?: 'top' | 'bottom';
  align?: 'start' | 'end' | 'center';
  children: React.ReactNode;
}> = ({ title, anchor, onClose, side = 'top', align = 'start', children }) => (
  <FloatingPanel
    anchor={anchor}
    label={title}
    side={side}
    align={align}
    className="w-96 max-w-[calc(100vw-2rem)] rounded-card border border-line/80 material-flyout flyout-in p-3.5 text-left shadow-flyout"
  >
    <div className="mb-2.5 flex items-center justify-between border-b border-line/60 pb-2">
      <span className="text-xs font-bold text-gray-900">{title}</span>
      <button
        onClick={onClose}
        title="关闭 (Esc)"
        aria-label="关闭"
        className="ui-icon-button ui-compact text-gray-400 hover:text-gray-700"
      >
        <Dismiss20Regular aria-hidden="true" />
      </button>
    </div>
    <div className="space-y-1">{children}</div>
  </FloatingPanel>
);

/** One label/value line inside a `ConsolePanel`. */
export const ConsolePanelRow: React.FC<{ label: string; value: React.ReactNode }> = ({
  label,
  value,
}) => (
  <div className="flex items-start justify-between gap-3 rounded-control border-b border-line/40 px-1 py-1.5 last:border-b-0">
    <span className="shrink-0 font-mono text-[10px] font-medium text-gray-500">{label}</span>
    <span className="break-all text-right text-[11px] font-sans text-gray-900">{value}</span>
  </div>
);
