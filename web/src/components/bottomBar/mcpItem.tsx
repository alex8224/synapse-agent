/**
 * The strip's MCP entry: the configured servers' live phase, opening the F5
 * panel as a popover.
 *
 * The trigger keeps its own store subscription (a reasoning delta must not
 * re-render the strip), and the popover is a `FloatingPanel`: the panel is
 * portalled and positioned from this trigger's rect instead of being an
 * `absolute` box inside the strip, which is what keeps the strip's own material
 * from becoming the panel's backdrop root.
 */
import { Flash20Regular, ChevronDown16Regular } from '@fluentui/react-icons';
import { useShallow } from 'zustand/react/shallow';
import { useConsoleStore } from '../../stores/useConsoleStore.ts';
import { withChord } from '../consoleShortcuts.ts';
import { McpPanel } from '../McpPanel.tsx';
import type { BottomBarItemDefinition } from './contract.ts';

export const MCP_ITEM_ID = 'mcp';

export const mcpItem: BottomBarItemDefinition = {
  id: MCP_ITEM_ID,
  label: 'MCP 服务器',
  region: 'left',
  order: 20,
  shortcutKey: 'F5',
  overlay: 'popover',
  panelLabel: 'MCP 工具与服务器',
  // `font-numeric` keeps the panel's font exactly as it was when the panel lived
  // inside the strip and inherited it; portalling it to the body would otherwise
  // repaint it in the window's UI font.
  panelClassName:
    'w-96 max-w-[calc(100vw-2rem)] rounded-card border border-line/80 material-flyout flyout-in p-3.5 font-numeric text-left shadow-flyout',
  Trigger: function McpTrigger({ context, open, anchorRef }) {
    const { mcpStatus, anyEnabled } = useConsoleStore(
      useShallow((state) => ({
        mcpStatus: state.mcpStatus,
        anyEnabled: state.mcpServers.some((server) => server.enabled),
      })),
    );
    return (
      <button
        ref={anchorRef}
        data-entry={MCP_ITEM_ID}
        type="button"
        onClick={(event) => context.toggle(MCP_ITEM_ID, event.currentTarget)}
        title={withChord('管理 MCP 服务器', 'F5')}
        aria-haspopup="dialog"
        aria-expanded={open}
        className="flex cursor-pointer items-center gap-1 transition-colors hover:text-gray-900"
      >
        <Flash20Regular
          aria-hidden="true"
          className={`shrink-0 ${anyEnabled ? 'text-green-600' : 'text-gray-400'}`}
          style={{ fontSize: '15px' }}
        />
        <span className="text-gray-700">mcp: {mcpStatus}</span>
        <ChevronDown16Regular aria-hidden="true" className="shrink-0 text-gray-400" />
      </button>
    );
  },
  Content: function McpContent({ context }) {
    return <McpPanel onClose={context.close} />;
  },
};
