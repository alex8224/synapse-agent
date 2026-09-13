import React from 'react';
import { useConsoleStore } from '../stores/useConsoleStore';
import {
  transcriptTurns,
  turnRailHoverText,
  turnRailSlotLabel,
  turnRailTickSlots,
} from '../stores/turnRail.ts';

/** Rows the rail maps turns onto; a row is 6px of bar plus a 1px gap. */
const RAIL_ROWS = 30;

/**
 * The turn rail: a minimap of the transcript, centred on the left edge.
 *
 * One row per turn while they fit (packed and centred, like the TUI's rail), and
 * one row per *range* once the transcript has more turns than rows.  Clicking a
 * row scrolls to that turn; hovering it shows the turn's user message and the
 * conclusion it reached, so a turn can be recognised without jumping to it.
 */
export const TurnRail: React.FC = () => {
  const messages = useConsoleStore((state) => state.messages);
  const turns = transcriptTurns(messages);
  const slots = turnRailTickSlots(turns.length, RAIL_ROWS);

  // A single-turn transcript has nothing to navigate: no rail at all.
  if (turns.length < 2) return null;

  const jumpTo = (index: number) => {
    const turn = turns[index];
    if (turn === undefined) return;
    const anchor = document.querySelector(`[data-turn-id="${turn.anchorId}"]`);
    anchor?.scrollIntoView({ block: 'start', behavior: 'smooth' });
  };

  return (
    <div className="pointer-events-none absolute left-1 top-1/2 z-20 -translate-y-1/2 select-none">
      <div className="pointer-events-auto flex flex-col gap-px">
        {slots.map((indices, row) => {
          if (indices.length === 0) {
            return <div key={`gap-${row}`} className="h-1.5 w-2" />;
          }
          const previews = indices.map((i) => turns[i].user);
          const label = turnRailSlotLabel(indices, previews);
          // A denser bucket reads as a longer bar, so the shape of the session
          // is visible at a glance.
          const width = indices.length === 1 ? 'w-3' : indices.length < 4 ? 'w-4' : 'w-5';
          return (
            <button
              key={`row-${row}`}
              type="button"
              onClick={() => jumpTo(indices[0])}
              title={indices.length === 1 ? turnRailHoverText(turns[indices[0]]) : label}
              className="group relative flex h-1.5 w-5 cursor-pointer items-center"
            >
              <span
                className={`${width} h-[3px] rounded-full bg-gray-300 transition-colors group-hover:bg-blue-500`}
              />
            </button>
          );
        })}
      </div>
    </div>
  );
};
