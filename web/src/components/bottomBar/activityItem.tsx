/**
 * The strip's activity entry: `● 运行中` / `○ 空闲`.
 *
 * It subscribes to the one store field it paints, so a usage update or a
 * reasoning delta never re-renders it (and never re-renders the strip).
 */
import { useConsoleStore } from '../../stores/useConsoleStore.ts';
import type { BottomBarItemDefinition } from './contract.ts';

export const ACTIVITY_ITEM_ID = 'activity';

export const activityItem: BottomBarItemDefinition = {
  id: ACTIVITY_ITEM_ID,
  label: '运行态',
  region: 'left',
  order: 10,
  Trigger: function ActivityTrigger() {
    const busy = useConsoleStore((state) => state.runtimeStatus === 'running');
    return (
      <span
        data-entry={ACTIVITY_ITEM_ID}
        className={`flex shrink-0 items-center gap-1.5 font-sans text-[11px] font-medium ${
          busy ? 'text-blue-600' : 'text-gray-500'
        }`}
      >
        <span
          className={`h-1.5 w-1.5 rounded-full ${busy ? 'animate-pulse bg-blue-600' : 'bg-gray-400'}`}
        />
        {busy ? '运行中' : '空闲'}
      </span>
    );
  },
};
