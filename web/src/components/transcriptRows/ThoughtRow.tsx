import { BrainCircuit20Regular, ChevronDown16Regular, ChevronRight16Regular, Sparkle20Regular } from '@fluentui/react-icons';
import React from 'react';
import { Markdown } from '../Markdown.tsx';
import { expandHint, thoughtLabel } from '../../stores/transcriptLabels.ts';
import { FoldStatusPill } from './FoldStatusPill.tsx';
import type { RowRenderProps } from './context.ts';
import { updateSpotlight } from './spotlight.ts';

/**
 * The reasoning chain, as a compact secondary log line that opens into a panel.
 *
 * The row is the turn's first step as often as not, so it carries the "已工作" header
 * (and its rule) when the fold says it is first; a collapsed turn shows that header
 * alone.  The label is the spec's wording, and the glyph separates a chain that is
 * still streaming from one that finished.
 */
export const ThoughtRow = React.memo(function ThoughtRow({
  message,
  processMeta,
  actions,
}: RowRenderProps) {
  const expanded = message.expanded === true;
  return (
    <div className="max-w-[85%]">
      {processMeta && !processMeta.isExpanded ? (
        <div className="transcript-fold-header">
          <div className="flex items-center gap-2 py-1 min-w-0">
            <button
              type="button"
              onClick={processMeta.onToggleExpand}
              className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-700 py-1 cursor-pointer select-none font-sans transition-colors"
            >
              <span>已工作 {processMeta.totalDurationText}</span>
              <ChevronRight16Regular aria-hidden="true" style={{ fontSize: '13px' }} />
            </button>
            {processMeta.groupStatus && <FoldStatusPill status={processMeta.groupStatus} />}
          </div>
          <div className="border-b border-line/60 my-2.5" />
        </div>
      ) : (
        <>
          {processMeta && processMeta.isFirst && (
            <div className="transcript-fold-header">
              <div className="flex items-center gap-2 py-1 min-w-0">
                <button
                  type="button"
                  onClick={processMeta.onToggleExpand}
                  className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-700 py-1 cursor-pointer select-none font-sans transition-colors"
                >
                  <span>已工作 {processMeta.totalDurationText}</span>
                  <ChevronDown16Regular aria-hidden="true" style={{ fontSize: '13px' }} />
                </button>
                {processMeta.groupStatus && <FoldStatusPill status={processMeta.groupStatus} />}
              </div>
              {/* The rule belongs to the header, in both folds: the steps below it
                  are the fold's content, so the block must not draw a second rule
                  at their end.  Header and rule stay in flow with the steps they
                  name, so nothing of the fold is masked while the column scrolls. */}
              <div className="border-b border-line/60 my-2.5" />
            </div>
          )}
          <div
            onClick={() => actions.onToggleExpand(message.id)}
            onMouseMove={updateSpotlight}
            className="inline-flex cursor-pointer select-none items-center gap-1.5 rounded-control border border-line bg-surface px-2.5 py-1 font-mono text-xs text-gray-600 transition-colors hover:bg-surface-hover hover:text-gray-900 active:bg-surface-pressed fluent-spotlight"
          >
            {message.duration === 'streaming' ? (
              <Sparkle20Regular aria-hidden="true" className="shrink-0 animate-pulse text-accent" style={{ fontSize: '14px' }} />
            ) : (
              <BrainCircuit20Regular aria-hidden="true" className="shrink-0 text-gray-500" style={{ fontSize: '14px' }} />
            )}
            <span>{thoughtLabel(message.duration)}</span>
            <span className="text-gray-400">{expandHint(expanded)}</span>
          </div>
          <div className="fluent-accordion" data-expanded={expanded}>
            <div className="fluent-accordion-content pt-1.5">
              <div className="material-card rounded-card border border-line p-3 text-sm text-gray-700 shadow-card">
                <Markdown text={message.content ?? ''} />
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
});
