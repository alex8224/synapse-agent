import React, { useCallback, useEffect, useRef } from 'react';
import {
  BrainCircuit20Regular,
  Sparkle20Regular,
  Wrench20Regular,
  SpinnerIos20Regular,
  Warning20Regular,
  Info20Regular,
  ArrowSort20Regular,
  DismissCircle20Regular,
  Shield20Regular,
} from '@fluentui/react-icons';
import { useShallow } from 'zustand/react/shallow';
import { useConsoleStore } from '../stores/useConsoleStore';
import {
  expandHint,
  thoughtLabel,
  toolGroupLabel,
  toolStatusLabel,
} from '../stores/transcriptLabels.ts';
import { Markdown } from './Markdown.tsx';
import { AttachmentThumb } from './AttachmentThumb.tsx';
import { TurnRail } from './TurnRail.tsx';
import { TodoPanel } from './TodoPanel.tsx';
import type { TranscriptMessage } from '../stores/historyMapper.ts';

/**
 * How close to the bottom the view still counts as "following the stream", in
 * pixels: a little slack keeps a sub-pixel scroll position (or a wrapped line
 * landing mid-frame) from silently stopping the follow.
 */
const PINNED_TO_BOTTOM_PX = 32;

/**
 * How long after the last wheel / touch event a scroll gesture counts as over.
 *
 * Long enough that a continuous gesture keeps the follow latch live, short enough
 * that the next streamed update is not yanked back while the reader is still
 * looking elsewhere.
 */
const USER_SCROLL_SETTLE_MS = 150;

/**
 * How close to the top of the transcript counts as "reached the top", in pixels.
 *
 * Reaching it loads the next page of earlier history, so the button above the
 * oldest loaded turn is a fallback rather than the only way in.
 */
const EARLIER_HISTORY_TRIGGER_PX = 48;

/**
 * One transcript row.
 *
 * Memoized on the message object: folding streamed text rebuilds the transcript
 * array but leaves every row that did not grow identical, so a long conversation
 * does not re-render -- and re-parse the Markdown of -- every row on every chunk.
 * `handleToggleExpand` is the transcript's stable fold handler, so it does not
 * invalidate the memo.
 */
const TranscriptRow = React.memo(function TranscriptRow({
  message: m,
  handleToggleExpand,
}: {
  message: TranscriptMessage;
  handleToggleExpand: (id: string) => void;
}) {
  if (m.type === 'user') {
    return (
      // Chat layout: the user's turn sits on the right, the assistant's on
      // the left, and the side it is on is the role — so no "User" /
      // "Assistant" heading is needed.
      // The 20% right inset shares the assistant body's right edge: that block is
      // capped at 80% of the reading column, so the user's turn is held back by
      // whatever is left. The two numbers must keep summing to 100% (pinned by
      // `transcriptLayoutGuard.test.ts`), which is what keeps the bubble from
      // hanging past the answer it belongs to.
      // `data-turn-id` is the anchor the turn rail scrolls to.
      <div key={m.id} data-turn-id={m.id} className="flex justify-end">
        <div className="mr-[20%] flex max-w-[80%] flex-col items-end gap-1.5">
          {m.content !== '' && (
            <div className="ui-user-bubble whitespace-pre-wrap break-words text-base leading-relaxed text-gray-900">
              {m.content}
            </div>
          )}
          {m.attachments !== undefined && m.attachments.length > 0 && (
            <div className="flex flex-wrap justify-end gap-2">
              {m.attachments.map((attachment) => (
                <AttachmentThumb key={attachment.attachmentId} attachment={attachment} />
              ))}
            </div>
          )}
          <span className="font-mono text-[10px] text-gray-400">{m.timestamp}</span>
        </div>
      </div>
    );
  }
  if (m.type === 'thought') {
    return (
      <div key={m.id} className="max-w-[85%]">
        <div
          onClick={() => handleToggleExpand(m.id)}
          className="inline-flex cursor-pointer select-none items-center gap-1.5 rounded-control border border-line bg-surface px-2.5 py-1 font-mono text-xs text-gray-600 transition-colors hover:bg-surface-hover hover:text-gray-900 active:bg-surface-pressed"
        >
          {m.duration === 'streaming' ? (
            <Sparkle20Regular aria-hidden="true" className="shrink-0 animate-pulse text-accent" style={{ fontSize: '14px' }} />
          ) : (
            <BrainCircuit20Regular aria-hidden="true" className="shrink-0 text-gray-500" style={{ fontSize: '14px' }} />
          )}
          <span>{thoughtLabel(m.duration)}</span>
          <span className="text-gray-400">{expandHint(m.expanded === true)}</span>
        </div>
        {m.expanded && (
          <div className="material-card mt-1.5 rounded-card border border-line p-3 text-sm text-gray-700 shadow-card">
            <Markdown text={m.content ?? ''} />
          </div>
        )}
      </div>
    );
  }
  if (m.type === 'tool_group') {
    const toolList = m.tools || [];
    // A batch opens its group before the first item lands (and a batch can
    // end up carrying none), so an empty placeholder is not a row yet.
    if (toolList.length === 0) {
      return null;
    }
    const failed = toolList.filter((t) => t.error || t.status === 'failed').length;
    const running = toolList.filter(
      (t) => t.status === 'running' || t.status === 'pending',
    ).length;
    const expanded = m.expanded === true;
    return (
      <div key={m.id} className="max-w-[85%] py-1">
        <div
          onClick={() => handleToggleExpand(m.id)}
          title={expanded ? '收起工具详情' : '展开工具详情'}
          className="inline-flex cursor-pointer select-none items-center gap-1.5 rounded-control border border-line bg-surface px-2.5 py-1 font-mono text-xs text-gray-600 transition-colors hover:bg-surface-hover hover:text-gray-900 active:bg-surface-pressed"
        >
          {failed > 0 ? (
            <DismissCircle20Regular aria-hidden="true" className="shrink-0 text-red-500" style={{ fontSize: '14px' }} />
          ) : running > 0 ? (
            <SpinnerIos20Regular aria-hidden="true" className="shrink-0 animate-spin text-blue-500" style={{ fontSize: '14px' }} />
          ) : (
            <Wrench20Regular aria-hidden="true" className="shrink-0 text-gray-500" style={{ fontSize: '14px' }} />
          )}
          <span>{toolGroupLabel(toolList.length, m.parallel === true)}</span>
          {running > 0 && (
            <span className="font-medium text-blue-500">{running} running</span>
          )}
          {failed > 0 && <span className="font-medium text-red-600">{failed} failed</span>}
          {!expanded && toolList.length > 0 && (
            <span className="truncate text-gray-400">
              {toolList.slice(0, 4).map((t) => t.name).join(' · ')}
              {toolList.length > 4 ? ` +${toolList.length - 4}` : ''}
            </span>
          )}
          <span className="text-gray-400">{expandHint(expanded)}</span>
        </div>
        {expanded && (
          <div className="mt-1.5 space-y-1.5">
            {toolList.map((t) => (
              <div
                key={t.id}
                className={`rounded-control border px-2.5 py-1.5 font-mono text-xs ${
                  t.error ? 'border-red-200 bg-red-50' : 'border-line bg-surface'
                }`}
              >
                <div className="flex items-center space-x-2">
                  <Wrench20Regular aria-hidden="true" className="shrink-0 text-gray-500" style={{ fontSize: '13px' }} />
                  <span className="font-medium text-gray-900">{t.label || t.name}</span>
                  {t.sub && (
                    <span className="rounded-control bg-sunken px-1 text-[10px] text-gray-500">
                      sub
                    </span>
                  )}
                  {t.subagentName && (
                    <span className="text-[10px] text-gray-400">@{t.subagentName}</span>
                  )}
                  {t.path && <span className="truncate text-gray-500">{t.path}</span>}
                  <span
                    className={`ml-auto shrink-0 rounded-control px-1 text-[10px] ${
                      t.error
                        ? 'bg-red-100 text-red-700'
                        : t.status === 'completed'
                          ? 'bg-green-100 text-green-700'
                          : 'bg-blue-50 text-blue-500'
                    }`}
                  >
                    {t.subagentStatus
                      ? `${toolStatusLabel(t.status)} · ${t.subagentStatus}`
                      : toolStatusLabel(t.status)}
                  </span>
                </div>
                {t.preview && (
                  <div className="mt-1 whitespace-pre-wrap break-all text-gray-600">
                    {t.preview}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }
  if (m.type === 'assistant') {
    return (
      <div key={m.id} className="flex max-w-[80%] flex-col items-start gap-1.5">
        <div className="text-base leading-relaxed font-sans text-gray-900">
          <Markdown text={m.content ?? ''} />
        </div>
        <span className="font-mono text-[10px] text-gray-400">{m.timestamp}</span>
      </div>
    );
  }
  if (m.type === 'info') {
    const warning = m.infoLevel === 'warning';
    return (
      <div
        key={m.id}
        className={`flex max-w-[85%] items-start gap-1.5 rounded-control border px-2.5 py-1.5 font-mono text-xs leading-relaxed ${
          warning
            ? 'border-amber-200 bg-amber-50 text-amber-800'
            : 'border-line bg-surface text-gray-600'
        }`}
      >
        {warning ? (
          <Warning20Regular aria-hidden="true" className="shrink-0 text-amber-600" style={{ fontSize: '14px' }} />
        ) : (
          <Info20Regular aria-hidden="true" className="shrink-0 text-blue-500" style={{ fontSize: '14px' }} />
        )}
        <span className="whitespace-pre-wrap break-all">{m.content}</span>
      </div>
    );
  }
  return null;
});

export const Transcript: React.FC = () => {
  const {
    messages,
    activity,
    toggleMessageExpand,
    pendingApproval,
    resolveApproval,
    historyLoading,
    historyHasMore,
    historyAvailable,
    historyError,
    loadEarlierHistory,
  } = useConsoleStore(
    // Only the fields this column paints: an activity tick or a usage update must
    // not re-render the transcript.
    useShallow((state) => ({
      messages: state.messages,
      activity: state.activity,
      toggleMessageExpand: state.toggleMessageExpand,
      pendingApproval: state.pendingApproval,
      resolveApproval: state.resolveApproval,
      historyLoading: state.historyLoading,
      historyHasMore: state.historyHasMore,
      historyAvailable: state.historyAvailable,
      historyError: state.historyError,
      loadEarlierHistory: state.loadEarlierHistory,
    })),
  );
  const scrollerRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const skipAutoScroll = useRef(false);
  /**
   * Whether the view is following the newest content.
   *
   * Following is the reader's choice: a stream that scrolls on every update makes
   * reading back through a running turn impossible, so only a view that is
   * already at the bottom follows.  Submitting a prompt is an explicit intent, so
   * that always follows.
   */
  const pinnedToBottom = useRef(true);
  /**
   * True while a wheel / touch gesture is driving the scroller.
   *
   * Only the reader may end the follow.  Our own `scrollIntoView` and the
   * browser's scroll anchoring also fire `scroll`, and a layout change *above* the
   * viewport (a streamed thought settling to its final height, a tool row
   * appearing) moves the scroll position on its own: reading the latch from every
   * scroll event ended the follow for the rest of the turn, so the reasoning
   * streamed into view but the tool call after it did not.
   */
  const userScrolling = useRef(false);

  // Stable identity: the scroll listener below triggers the same guarded path the
  // button uses, and a changing callback would re-attach that listener.
  const handleLoadEarlier = useCallback(() => {
    skipAutoScroll.current = true;
    loadEarlierHistory();
  }, [loadEarlierHistory]);

  useEffect(() => {
    const scroller = scrollerRef.current;
    if (scroller === null) return;
    const distance = (): number =>
      scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
    const track = () => {
      if (!userScrolling.current) return;
      pinnedToBottom.current = distance() <= PINNED_TO_BOTTOM_PX;
    };
    let settle: ReturnType<typeof setTimeout> | null = null;
    const beginUserScroll = () => {
      userScrolling.current = true;
      if (settle !== null) clearTimeout(settle);
      // A gesture is over shortly after its last event; the latch is then final
      // until the next one.  A gesture that ended at the bottom re-arms the
      // follow, which is how scrolling back down resumes it.
      settle = setTimeout(() => {
        settle = null;
        pinnedToBottom.current = distance() <= PINNED_TO_BOTTOM_PX;
        userScrolling.current = false;
      }, USER_SCROLL_SETTLE_MS);
    };
    /**
     * Load the next page of earlier history once the reader reaches the top.
     *
     * The gates are read from the store rather than closed over, so the listener
     * never acts on a stale `historyHasMore` / `historyLoading`; the store refuses
     * a load that is already running or exhausted anyway.
     */
    const loadEarlierAtTop = () => {
      if (scroller.scrollTop > EARLIER_HISTORY_TRIGGER_PX) return;
      const { historyHasMore, historyLoading } = useConsoleStore.getState();
      if (!historyHasMore || historyLoading) return;
      handleLoadEarlier();
    };
    track();
    scroller.addEventListener('wheel', beginUserScroll, { passive: true });
    scroller.addEventListener('touchstart', beginUserScroll, { passive: true });
    scroller.addEventListener('touchmove', beginUserScroll, { passive: true });
    scroller.addEventListener('scroll', track, { passive: true });
    scroller.addEventListener('scroll', loadEarlierAtTop, { passive: true });
    return () => {
      if (settle !== null) clearTimeout(settle);
      scroller.removeEventListener('wheel', beginUserScroll);
      scroller.removeEventListener('touchstart', beginUserScroll);
      scroller.removeEventListener('touchmove', beginUserScroll);
      scroller.removeEventListener('scroll', track);
      scroller.removeEventListener('scroll', loadEarlierAtTop);
    };
  }, [handleLoadEarlier]);

  useEffect(() => {
    if (skipAutoScroll.current) {
      // Prepending an earlier history page must not yank the view back to bottom.
      skipAutoScroll.current = false;
      return;
    }
    const newest = messages[messages.length - 1];
    if (!pinnedToBottom.current && newest?.type !== 'user') return;
    // Instant rather than smooth: while a turn streams, the target moves every
    // few milliseconds, so a smooth animation is restarted (and never finishes)
    // hundreds of times over a single thought.
    bottomRef.current?.scrollIntoView({ block: 'end' });
    // The view is at the bottom now, whatever moved it there in between.
    pinnedToBottom.current = true;
  }, [messages]);

  /**
   * Expand/collapse one fold.
   *
   * This is a *view* change, not new content, but the store replaces the
   * `messages` array to flip the flag — and the auto-scroll effect keys off that
   * array identity.  Without the same guard the "load earlier" path uses, opening
   * a fold yanked the transcript to the bottom, so it never appeared to open in
   * place.
   */
  // Stable identity: a row that re-renders because this callback changed would
  // defeat the row-level memo below.
  const handleToggleExpand = useCallback(
    (id: string) => {
      skipAutoScroll.current = true;
      toggleMessageExpand(id);
    },
    [toggleMessageExpand],
  );

  return (
    <>
      {/* Minimap of the transcript, centred on the left edge (see TurnRail). */}
      <TurnRail />
      {/* Floating progress panel for the session's todo list (hidden until one
          exists). */}
      <TodoPanel />
    {/* `.no-scrollbar` keeps the reading column centred in the *pane* rather than
        in the pane minus a one-sided scrollbar, which is what makes it line up
        with the composer's `console-column` below. */}
    <div
      ref={scrollerRef}
      // No bottom padding for a floating composer: the composer is a sibling row,
      // so the scrollport's bottom edge is the last visible line.
      // `.no-scrollbar`: the wheel, touch and the keyboard still scroll it, but no
      // scrollbar takes a bite out of the reading column, so its edges line up
      // with the composer card's (the sidebar tree works the same way).
      className="console-gutter no-scrollbar console-pane-inset flex-1 overflow-y-auto font-sans"
    >
      <div className="console-column space-y-5">
        {historyAvailable === false && (
          <div className="rounded-card border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800 font-mono leading-relaxed">
            此会话在 transcript 投影中不可用（history.available=false）。以下只显示建立连接后的实时内容；
            不按“空历史”显示，也不会回退到 checkpoint。
          </div>
        )}

        {historyError !== null && (
          <div
            role="alert"
            className="rounded-card border border-red-200 bg-red-50 p-3 text-xs text-red-800 font-mono leading-relaxed"
          >
            {historyError}
            以下只显示建立连接后的实时内容，不回退到 checkpoint。
          </div>
        )}

        {historyHasMore && messages.length > 0 && (
          <div className="flex justify-center pt-1">
            {/* Reaching the top loads this page on its own (see the scroll
                listener); the button stays as the manual path and as the "there is
                more" hint. */}
            <button
              onClick={handleLoadEarlier}
              disabled={historyLoading}
              className="inline-flex h-8 cursor-pointer select-none items-center space-x-1.5 rounded-control border border-line bg-surface px-3 text-xs font-mono text-gray-600 transition-colors hover:bg-surface-hover active:bg-surface-pressed disabled:cursor-not-allowed disabled:text-fg-disabled"
            >
              <ArrowSort20Regular aria-hidden="true" className="shrink-0" style={{ fontSize: '14px' }} />
              <span>{historyLoading ? '加载更早历史中…' : '加载更早历史'}</span>
            </button>
          </div>
        )}

        {historyLoading && messages.length === 0 && (
          <div className="py-6 text-center text-gray-400 text-xs font-mono select-none">正在加载历史…</div>
        )}

        {messages.length === 0 &&
          !historyLoading &&
          historyAvailable !== false &&
          historyError === null && (
          <div className="py-16 text-center text-gray-400 text-xs font-mono select-none">
            当前会话已建立长连接，在下方输入指令即可开始与 Synapse Agent 对话
          </div>
        )}
        {messages.map((m) => (
          <TranscriptRow key={m.id} message={m} handleToggleExpand={handleToggleExpand} />
        ))}

        {/* HITL Pending Approval Dialog */}
        {pendingApproval && (
          <div className="space-y-3 rounded-card border border-amber-200 bg-amber-50 p-4 shadow-card">
            <div className="flex items-center space-x-2 text-amber-800 font-medium text-xs">
              <Shield20Regular aria-hidden="true" className="shrink-0" style={{ fontSize: '18px' }} />
              <span>需要审批危险操作 (Turn: {pendingApproval.turn_id})</span>
            </div>
            <div className="space-y-1.5 font-mono text-xs text-gray-700">
              {pendingApproval.actions.map((act, idx) => (
                <div key={idx} className="rounded-control border border-amber-200 bg-surface p-2">
                  <div className="font-bold text-gray-900">{act.name}</div>
                  <div className="truncate text-xs text-gray-600">{JSON.stringify(act.args)}</div>
                </div>
              ))}
            </div>
            <div className="flex space-x-2 pt-1">
              <button
                onClick={() => resolveApproval('allow_once')}
                className="h-8 cursor-pointer rounded-control bg-accent px-3 text-xs font-medium text-on-accent transition-colors hover:bg-blue-700"
              >
                批准本次
              </button>
              <button
                onClick={() => resolveApproval('reject_once')}
                className="h-8 cursor-pointer rounded-control bg-red-500 px-3 text-xs font-medium text-on-accent transition-colors hover:bg-red-700"
              >
                拒绝
              </button>
            </div>
          </div>
        )}
        {activity && activity.active && (
          <div className="flex select-none items-center gap-1.5 font-mono text-xs text-gray-500">
            <span className="h-1.5 w-1.5 rounded-full bg-blue-600 animate-pulse" />
            <span>{activity.phase}</span>
            {activity.detail && <span className="text-gray-400">{activity.detail}</span>}
          </div>
        )}
        <div ref={bottomRef} />
      </div>
    </div>
    </>
  );
};
