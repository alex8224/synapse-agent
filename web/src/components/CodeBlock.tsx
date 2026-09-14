import React, { useMemo, useState } from 'react';
import { highlight, type HighlightKind } from '../markdown/highlight.ts';

/** Above this size highlighting is skipped so a huge paste cannot stall a frame. */
const HIGHLIGHT_MAX_CHARS = 40_000;

const TOKEN_CLASS: Record<HighlightKind, string> = {
  plain: 'text-gray-800',
  comment: 'text-gray-400',
  string: 'text-emerald-700',
  number: 'text-amber-700',
  keyword: 'text-violet-700',
  added: 'text-emerald-700',
  removed: 'text-red-700',
  meta: 'text-blue-600',
};

export interface CodeBlockProps {
  lang: string;
  code: string;
  /** True while a streamed fence has not been closed yet. */
  streaming?: boolean;
  /**
   * Optional short caveat rendered in the header (e.g. a language the console
   * has no renderer for).  Kept as plain text so it can never inject markup.
   */
  note?: string;
}

/**
 * One fenced code block: language label, copy button, and a monospace body that
 * scrolls instead of wrapping (wrapped code is unreadable).
 */
export const CodeBlock: React.FC<CodeBlockProps> = ({
  lang,
  code,
  streaming = false,
  note,
}) => {
  const [copied, setCopied] = useState(false);
  const tokens = useMemo(
    () =>
      code.length > HIGHLIGHT_MAX_CHARS
        ? [{ text: code, kind: 'plain' as HighlightKind }]
        : highlight(code, lang),
    [code, lang],
  );

  const copy = (): void => {
    const clipboard = typeof navigator === 'undefined' ? undefined : navigator.clipboard;
    if (!clipboard) return;
    void clipboard
      .writeText(code)
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1200);
      })
      .catch(() => undefined);
  };

  return (
    <div className="my-2 overflow-hidden rounded-control border border-gray-200 bg-canvas">
      <div className="flex items-center justify-between border-b border-gray-200 bg-sunken px-2.5 py-1">
        <span className="font-mono text-[10px] uppercase tracking-wide text-gray-500">
          {lang || 'text'}
          {streaming ? ' · streaming' : ''}
        </span>
        <div className="flex items-center gap-2">
          {note !== undefined && (
            // A renderer's failure reason can be long; it truncates instead of
            // squeezing the language label or wrapping the header.
            <span
              title={note}
              className="max-w-[24rem] truncate font-sans text-[10px] text-amber-600"
            >
              {note}
            </span>
          )}
          <button
            type="button"
            onClick={copy}
            title="复制代码"
            className="font-mono text-[10px] text-gray-500 hover:text-gray-900 transition-colors cursor-pointer"
          >
            {copied ? '已复制' : '复制'}
          </button>
        </div>
      </div>
      <pre className="max-h-[28rem] overflow-auto px-3 py-2 font-mono text-[12px] leading-5">
        <code className="whitespace-pre">
          {tokens.map((token, index) => (
            <span key={index} className={TOKEN_CLASS[token.kind]}>
              {token.text}
            </span>
          ))}
        </code>
      </pre>
    </div>
  );
};
