import type React from 'react';
import type { TranscriptMessage } from '../../stores/historyMapper.ts';
import type { GroupIntentStatus } from '../../stores/turnWork.ts';

/**
 * The fold a row's own visibility hangs off.
 *
 * A turn's steps share one "已工作 N 秒" header; the row that owns it knows it is
 * first, and every step needs the same toggle so any of them can open the fold.
 */
export interface RowProcessMeta {
  isFirst: boolean;
  isExpanded: boolean;
  totalDurationText: string;
  groupStatus: GroupIntentStatus | null;
  onToggleExpand: () => void;
}

/**
 * Everything a row may raise, as one stable object.
 *
 * A bag rather than a prop per callback: a new action is added here once and any row
 * can take it up, and a row that ignores it is not re-rendered by it (the object's
 * identity only changes when a handler does).
 */
export interface RowActions {
  /** Open or close a row's own detail fold. */
  onToggleExpand: (messageId: string) => void;
  /** Open or close one call's detail inside a tool batch. */
  onToggleTool: (messageId: string, toolKey: string, hasDetail: boolean) => void;
  /** Open or close one subagent card's steps. */
  onToggleSubagent: (messageId: string, subagentKey: string) => void;
  /** Open the read-only git explorer on one file, for a change card. */
  onReviewFile: (path: string) => void;
  /**
   * Put one file of one turn back the way that turn found it.
   *
   * The only action here that writes to the reader's own files: the runtime refuses
   * while a turn is running or when the file has moved on since, and the refusal is
   * reported back in the transcript rather than swallowed.
   */
  onRevertFile: (turnId: string, path: string) => void;
}

/** What every row renderer is handed. */
export interface RowRenderProps {
  message: TranscriptMessage;
  /** Call-detail folds, already narrowed to this message. */
  toolExpansions: Readonly<Record<string, boolean>>;
  /** Subagent-card folds, already narrowed to this message. */
  subagentExpansions: Readonly<Record<string, boolean>>;
  processMeta?: RowProcessMeta;
  actions: RowActions;
}

/**
 * One row kind's renderer.
 *
 * `ROW_RENDERERS` maps every `TranscriptMessage['type']` to one of these, so a kind
 * cannot exist without a renderer and a renderer cannot exist without a kind.
 */
export type RowRenderer = React.FC<RowRenderProps>;
