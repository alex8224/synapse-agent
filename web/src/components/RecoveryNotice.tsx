import React from 'react';
import { useConsoleStore } from '../stores/useConsoleStore.ts';
import { recoveryNotice, type RecoveryNoticeKind } from '../stores/recoveryNoticeView.ts';

/**
 * Non-blocking strip for the console's recovery / degradation state.
 *
 * The store already knows when a live replay was truncated or when the relay is
 * recovering, but nothing painted it: the reader saw a transcript with missing
 * steps and no reason why.  This is the thin painter over that state -- all of
 * the policy (which state says what, and when to stay silent) lives in the pure
 * `recoveryNoticeView` module.
 *
 * Deliberately *not* a modal and deliberately without a dismiss button: the
 * condition is not the reader's to acknowledge, it holds until the underlying
 * state changes, and a modal over a live transcript would block the turn the
 * notice is describing.
 */

/**
 * One token pair per kind, in the same style as `RuntimeDiagnosticsBanner`:
 * a soft themed fill with a matching border and foreground, so a theme swaps
 * the values without the component naming a colour.
 */
const KIND_CLASSES: Record<RecoveryNoticeKind, string> = {
  blocked: 'border-red-200 bg-red-50/80 text-red-900',
  degraded: 'border-amber-200 bg-amber-50/80 text-amber-900',
  transient: 'border-blue-100 bg-blue-50/80 text-blue-700',
};

export const RecoveryNotice: React.FC = () => {
  // Three separate selectors, so a streaming turn (which rewrites plenty of
  // other store fields) cannot re-render this strip: it repaints only when one
  // of its own three inputs changes.
  const recoveryState = useConsoleStore((s) => s.recoveryState);
  const recoveryDetail = useConsoleStore((s) => s.recoveryDetail);
  const liveBufferDroppedCount = useConsoleStore((s) => s.liveBufferDroppedCount);
  const notice = recoveryNotice(recoveryState, recoveryDetail, liveBufferDroppedCount);
  // The healthy states render nothing at all -- no wrapper, no reserved height.
  if (notice === null) return null;

  return (
    <div
      // A blocked recovery is the reader's problem to act on, so it is announced
      // assertively; a degraded or self-healing one is announced politely and
      // never steals focus from the composer.
      role={notice.kind === 'blocked' ? 'alert' : 'status'}
      className={`console-gutter border-b py-2 text-[12px] leading-relaxed ${KIND_CLASSES[notice.kind]}`}
    >
      {/* Same reading column as the transcript and the composer, so the strip
          starts on the chat's left edge instead of the pane's. */}
      <div className="console-column">
        <div className="font-semibold">
          {notice.title}
          {notice.droppedEvents > 0 && (
            // The count is part of the headline: it is the one number the reader
            // can act on ("how much did I lose"), and it is what the store
            // counted, never an estimate.
            <span className="ml-2 font-normal opacity-80">
              已丢弃 {notice.droppedEvents} 条实时事件
            </span>
          )}
        </div>
        {notice.detail !== null && (
          <div className="mt-0.5 break-all opacity-90">{notice.detail}</div>
        )}
      </div>
    </div>
  );
};
