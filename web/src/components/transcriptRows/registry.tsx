import type { TranscriptMessage } from '../../stores/historyMapper.ts';
import { AssistantRow } from './AssistantRow.tsx';
import { InfoRow } from './InfoRow.tsx';
import { ThoughtRow } from './ThoughtRow.tsx';
import { ToolGroupRow } from './ToolGroupRow.tsx';
import { UserRow } from './UserRow.tsx';
import type { RowRenderer } from './context.ts';

/**
 * Every row kind the transcript can paint, as a table.
 *
 * One entry per `TranscriptMessage['type']`: a new kind is a new module plus a line
 * here, and because the map is a `Record` over the union, a kind without a renderer is
 * a type error rather than a row that silently paints nothing.  The layout reads this
 * table and nothing else about the kinds, so no row is special-cased by position or
 * by the order the kinds happen to be listed in.
 */
export const ROW_RENDERERS: Record<TranscriptMessage['type'], RowRenderer> = {
  user: UserRow,
  thought: ThoughtRow,
  tool_group: ToolGroupRow,
  assistant: AssistantRow,
  info: InfoRow,
};
