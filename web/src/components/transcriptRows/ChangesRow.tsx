import { ChevronRight16Regular, DocumentAdd20Regular, DocumentEdit20Regular, DocumentError20Regular } from '@fluentui/react-icons';
import React from 'react';
import type { TurnChangeView } from '../../stores/historyMapper.ts';
import type { RowRenderProps } from './context.ts';

/**
 * What the turn did to the workspace, as one card per file.
 *
 * A turn's outcome rather than its process: the row is not a step of the fold, so it
 * stays on screen while the turn's thoughts and calls are folded away, and it reads
 * from the turn's own list (each count is *that* turn's contribution, so the same file
 * may appear in several turns with different numbers).
 *
 * Clicking a card opens the read-only git explorer on that file.  Each card also offers
 * to undo that one file -- the runtime owns that write, refuses when the file has moved
 * on since the turn, and reports the refusal back here rather than swallowing it.
 */
export const ChangesRow = React.memo(function ChangesRow({ message, actions }: RowRenderProps) {
  const changes = message.changes ?? [];
  if (changes.length === 0) return null;
  const total = message.changesTotal ?? changes.length;
  return (
    <div className="max-w-[85%] py-1">
      <div className="rounded-card border border-line bg-surface p-2.5">
        <div className="mb-2 flex items-center gap-2 text-xs text-gray-500">
          <DocumentEdit20Regular aria-hidden="true" className="shrink-0" style={{ fontSize: '14px' }} />
          <span className="font-medium text-gray-700">本轮工作区改动</span>
          <span className="font-mono text-[10px] text-gray-400">
            {total === changes.length ? `${total} 个文件` : `${total} 个文件 · 显示前 ${changes.length} 个`}
          </span>
        </div>
        <div className="space-y-1">
          {changes.map((change) => (
            <ChangeCard
              key={change.path}
              change={change}
              onReview={() => actions.onReviewFile(change.path)}
              onRevert={() => actions.onRevertFile(message.turnId ?? '', change.path)}
            />
          ))}
        </div>
      </div>
    </div>
  );
});

/**
 * One file: its state, its path, its own line counts, and what can be done with it.
 *
 * Undo is armed before it fires, in place: this is the one control in the transcript that
 * changes the reader's files, and a single stray click must not be able to do it.  A file
 * whose change was not counted (binary, or too large) is never offered -- the runtime kept
 * no copy of it to restore from.
 */
function ChangeCard({
  change,
  onReview,
  onRevert,
}: {
  change: TurnChangeView;
  onReview: () => void;
  onRevert: () => void;
}) {
  const [armed, setArmed] = React.useState(false);
  const failed = change.status === 'deleted';
  return (
    <div className="flex w-full min-w-0 items-center gap-2 rounded-control px-2 py-1 font-mono text-xs">
      <button
        type="button"
        onClick={onReview}
        title={`在 Git Explorer 中审查 ${change.path}`}
        aria-label={`审查 ${change.path}`}
        className="flex min-w-0 flex-1 cursor-pointer select-none items-center gap-2 rounded-control text-left transition-colors hover:bg-surface-hover active:bg-surface-pressed"
      >
        <span
          className={"flex h-4 w-4 shrink-0 items-center justify-center rounded-control text-[10px] font-medium " + (failed
            ? 'bg-red-50 text-red-600'
            : change.status === 'added'
              ? 'bg-green-50 text-green-700'
              : 'bg-sunken text-gray-600')}
        >
          {change.status === 'added' ? (
            <DocumentAdd20Regular aria-hidden="true" style={{ fontSize: '11px' }} />
          ) : failed ? (
            <DocumentError20Regular aria-hidden="true" style={{ fontSize: '11px' }} />
          ) : (
            <DocumentEdit20Regular aria-hidden="true" style={{ fontSize: '11px' }} />
          )}
        </span>
        <span className="min-w-0 flex-1 truncate text-gray-900" title={change.path}>
          {change.path}
        </span>
        {/* A file whose change could not be counted says nothing rather than "±0".  Once
            the change is undone the counts describe nothing at all, so they go too. */}
        {!change.binary && !change.reverted && (
          <span className="shrink-0 font-mono text-[10px]">
            {change.insertions > 0 && <span className="text-green-700">+{change.insertions}</span>}
            {change.insertions > 0 && change.deletions > 0 && <span className="text-gray-400"> </span>}
            {change.deletions > 0 && <span className="text-red-600">-{change.deletions}</span>}
          </span>
        )}
        <ChevronRight16Regular aria-hidden="true" className="shrink-0 text-gray-400" style={{ fontSize: '13px' }} />
      </button>
      {change.reverted ? (
        <span className="shrink-0 rounded-control bg-sunken px-1.5 py-0.5 text-[10px] text-gray-600">
          已撤销
        </span>
      ) : (
        !change.binary && (
          <span className="flex shrink-0 items-center gap-1">
            {armed && (
              <>
                <span className="text-[10px] text-gray-500">恢复到本轮开始前？</span>
                <button
                  type="button"
                  onClick={() => {
                    setArmed(false);
                    onRevert();
                  }}
                  aria-label={`确认撤销 ${change.path} 的本轮改动`}
                  className="cursor-pointer select-none rounded-control bg-red-600 px-1.5 py-0.5 text-[10px] text-on-accent transition-colors hover:bg-red-700"
                >
                  确认撤销
                </button>
              </>
            )}
            <button
              type="button"
              onClick={() => setArmed((was) => !was)}
              title="撤销本轮对该文件的改动（恢复到本轮开始前）"
              aria-label={`撤销 ${change.path} 的本轮改动`}
              aria-expanded={armed}
              className="cursor-pointer select-none rounded-control border border-line px-1.5 py-0.5 text-[10px] text-gray-600 transition-colors hover:bg-surface-hover active:bg-surface-pressed"
            >
              {armed ? '取消' : '撤销'}
            </button>
          </span>
        )
      )}
    </div>
  );
}
