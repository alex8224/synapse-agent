import React, { useMemo } from 'react';
import { renderTex } from '../markdown/tex.ts';
import { GeneratedHtml } from './GeneratedHtml.tsx';

/**
 * Labelled source rendering for a formula the console will not typeset: still
 * streaming (KaTeX would render a half-arrived formula in red), or unparsable.
 * The label says which, so the fallback is never mistaken for the formula.
 */
const MathSource: React.FC<{ tex: string; streaming: boolean; note?: string }> = ({
  tex,
  streaming,
  note,
}) => (
  <div className="my-2 overflow-hidden rounded-control border border-purple-100 bg-math-surface">
    <div className="flex items-center justify-between border-b border-purple-100 bg-math-header px-2.5 py-1">
      <span className="font-mono text-[10px] uppercase tracking-wide text-purple-500">
        公式{streaming ? ' · streaming' : ''}
      </span>
      <span className="font-mono text-[10px] text-amber-600">{note ?? 'LaTeX 源码（未排版）'}</span>
    </div>
    <pre className="fluent-scrollbar overflow-auto whitespace-pre-wrap px-3 py-3 text-center font-mono text-[13px] leading-6 text-gray-800">
      {tex}
    </pre>
  </div>
);

/**
 * Inline math (`$...$`): typeset by KaTeX next to the prose it belongs to.
 *
 * An unparsable fragment degrades to the labelled monospace source instead of
 * KaTeX's red error node, which reads as prose damage at inline size.
 */
export const InlineMath: React.FC<{ tex: string }> = ({ tex }) => {
  const result = useMemo(() => renderTex(tex, false), [tex]);
  if (result.failed) {
    return (
      <span
        title={`公式（未能解析）：${tex}`}
        className="rounded bg-math-inline px-1 font-mono text-[0.9em] text-purple-700"
      >
        {tex}
      </span>
    );
  }
  return (
    <GeneratedHtml tag="span" html={result.html} className="math-inline" title={tex} />
  );
};

/**
 * Display math (`$$...$$`): typeset on its own centred line inside a labelled
 * box, so a formula is visually distinct from prose the way a code block is.
 *
 * While the fence is still open the source is shown instead: KaTeX has no
 * partial parse, so typesetting a half-streamed formula would flash a red error
 * for every token.
 */
export const DisplayMath: React.FC<{ tex: string; streaming: boolean }> = ({ tex, streaming }) => {
  const result = useMemo(() => (streaming ? null : renderTex(tex, true)), [tex, streaming]);
  if (result === null || result.failed) {
    return (
      <MathSource
        tex={tex}
        streaming={streaming}
        note={result === null ? undefined : '公式解析失败，显示源码'}
      />
    );
  }
  return (
    <div className="math-block my-2 overflow-x-auto rounded-control border border-purple-100 bg-math-surface px-3 py-2 text-center">
      <GeneratedHtml html={result.html} className="math-display" />
    </div>
  );
};
