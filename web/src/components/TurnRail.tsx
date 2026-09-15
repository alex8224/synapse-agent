import React, { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowDown16Regular } from '@fluentui/react-icons';
import { useConsoleStore } from '../stores/useConsoleStore';
import {
  TURN_RAIL_ACTIVE_SLACK_PX,
  activeTurnIndex,
  transcriptTurns,
  turnRailHoverText,
  turnRailRowFor,
  turnRailSlotLabel,
  turnRailTickSlots,
} from '../stores/turnRail.ts';
import type { TranscriptViewport } from './transcriptViewport.ts';

/** Rows the rail maps turns onto; a row is 6px of bar plus a 1px gap. */
const MAX_RAIL_ROWS = 24;

/**
 * How long a bar grows while it is pointed at, or while its turn is the one on
 * screen.  One length for both, because they are the same signal: "this is the
 * turn you are looking at".
 */
const BAR_LENGTHENED = 'w-5';

/** The bar's own animation: width and colour, eased, so it reads as a slide. */
const BAR_ANIMATION = 'transition-[width,background-color] duration-200 ease-out';

/**
 * The turn rail: a minimap of the transcript, centred on the left edge.
 *
 * One row per turn while they fit (packed and centred, like the TUI's rail), and
 * one row per *range* once the transcript has more turns than rows.  Clicking a
 * row scrolls to that turn; hovering it shows the turn's user message and the
 * conclusion it reached, so a turn can be recognised without jumping to it.
 *
 * The rail is also a position indicator: the bar for the turn the transcript is
 * currently showing plays the same lengthening animation a hovered bar does, so
 * scrolling the transcript (or the transcript following a running turn) animates
 * the rail along with it.
 *
 * Every offset and every jump goes through the transcript's viewport handle: the
 * transcript mounts only the rows near the viewport, so measuring
 * `[data-turn-id]` in the DOM would report every off-screen turn as past the end.
 */
export const TurnRail: React.FC<{ viewport: TranscriptViewport }> = ({ viewport }) => {
  const messages = useConsoleStore((state) => state.messages);
  const turns = useMemo(() => transcriptTurns(messages), [messages]);
  const railRows = useMemo(() => Math.min(MAX_RAIL_ROWS, Math.max(turns.length, 1)), [turns.length]);
  const slots = useMemo(() => turnRailTickSlots(turns.length, railRows), [turns.length, railRows]);
  const [activeTurn, setActiveTurn] = useState(-1);
  const [isAtBottom, setIsAtBottom] = useState(false);
  /** Anchor offsets inside the scrollable content, measured on the scroll frame. */
  const offsetsRef = useRef<number[]>([]);
  const frameRef = useRef<number | null>(null);

  useEffect(() => {
    // No rail to animate (and nothing rendered): leave the last value alone, the
    // next measurement overwrites it.
    if (turns.length < 2) return;
    const scroller = viewport.scroller();
    if (scroller === null) return;

    // Every read happens in one batch on the scroll frame, before the state
    // update that follows it: measuring per store update would force a layout on
    // every streamed chunk.
    const sync = () => {
      frameRef.current = null;
      offsetsRef.current = viewport.offsetsOf(turns.map((turn) => turn.anchorId));
      const next = activeTurnIndex(
        offsetsRef.current,
        scroller.scrollTop,
        TURN_RAIL_ACTIVE_SLACK_PX,
        scroller.scrollHeight - scroller.clientHeight,
      );
      setActiveTurn((current) => (current === next ? current : next));
      const atBottom =
        scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= TURN_RAIL_ACTIVE_SLACK_PX;
      setIsAtBottom(atBottom);
    };
    const schedule = () => {
      if (frameRef.current !== null) return;
      frameRef.current = requestAnimationFrame(sync);
    };

    // The first measurement is a frame away on purpose: setting state
    // synchronously here would cascade a render on every transcript change.
    schedule();
    scroller.addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('resize', schedule);
    return () => {
      scroller.removeEventListener('scroll', schedule);
      window.removeEventListener('resize', schedule);
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
      frameRef.current = null;
    };
  }, [turns, viewport]);

  // A single-turn transcript has nothing to navigate: no rail at all.
  if (turns.length < 2) return null;

  const activeRow = turnRailRowFor(slots, activeTurn);

  const jumpTo = (index: number) => {
    const turn = turns[index];
    if (turn === undefined) return;
    viewport.scrollToMessage(turn.anchorId);
  };

  const jumpToBottom = () => {
    const scroller = viewport.scroller();
    if (scroller === null) return;
    scroller.scrollTo({ top: scroller.scrollHeight, behavior: 'smooth' });
    window.dispatchEvent(new CustomEvent('transcript:jump-bottom'));
  };

  return (
    <div className="console-turn-rail pointer-events-none absolute left-2.5 top-1/2 z-20 -translate-y-1/2 select-none">
      <div
        title="会话轮次快速导航"
        className="pointer-events-auto flex flex-col items-center gap-1.5 rounded-full border border-line/70 bg-surface/85 px-1.5 py-2.5 shadow-flyout backdrop-blur-md transition-all duration-200 hover:border-line hover:bg-surface/95"
      >
        <div data-turn-rail className="flex flex-col items-center gap-1.5">
          {slots.map((indices, row) => {
            if (indices.length === 0) {
              return <div key={`gap-${row}`} className="h-1 w-2.5" />;
            }
            const previews = indices.map((i) => turns[i].user);
            const label = turnRailSlotLabel(indices, previews);
            // A denser bucket reads as a longer bar, so the shape of the session
            // is visible at a glance.
            const resting = indices.length === 1 ? 'w-2' : indices.length < 4 ? 'w-3' : 'w-4';
            // The row on screen takes the lengthened width outright instead of
            // through the hover variant, so the two can never fight over it.
            const onScreen = row === activeRow;
            return (
              <button
                key={`row-${row}`}
                type="button"
                onClick={() => jumpTo(indices[0])}
                title={indices.length === 1 ? turnRailHoverText(turns[indices[0]]) : label}
                className="group relative flex h-2.5 w-5 cursor-pointer items-center justify-center"
              >
                <span
                  // `shrink-0`: the bar outgrows its 20px hit area when it is
                  // lengthened, and a flex item would otherwise be squeezed back
                  // to the button's width instead of overflowing it.
                  className={`${onScreen ? BAR_LENGTHENED : resting} h-[3px] shrink-0 rounded-full ${
                    onScreen ? 'bg-accent' : 'bg-gray-400/70'
                  } ${BAR_ANIMATION} group-hover:w-5 group-hover:bg-accent`}
                />
              </button>
            );
          })}
        </div>
        <div className="my-0.5 h-px w-2.5 bg-line/50" />
        <button
          type="button"
          data-jump-bottom
          onClick={jumpToBottom}
          title="跳转到会话尾部"
          aria-label="跳转到会话尾部"
          className="group relative flex h-4 w-5 cursor-pointer items-center justify-center rounded-full text-gray-400 transition-colors hover:text-accent"
        >
          <ArrowDown16Regular
            aria-hidden="true"
            className={`shrink-0 transition-colors ${
              isAtBottom ? 'text-accent' : 'text-gray-400/80 group-hover:text-accent'
            }`}
            style={{ fontSize: '12px' }}
          />
        </button>
      </div>
    </div>
  );
};