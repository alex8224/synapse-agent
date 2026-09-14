import React, { useMemo } from 'react';
import { parseMarkdown, type Block, type Span } from '../markdown/parse.ts';
import { CodeBlock } from './CodeBlock.tsx';

/** Above this size the document is rendered as plain text instead of parsed. */
const PARSE_MAX_CHARS = 200_000;

const HEADING_CLASS = ['text-lg', 'text-base', 'text-sm', 'text-sm', 'text-xs', 'text-xs'];

/**
 * Display math (`$$...$$`).
 *
 * Deliberately dependency-free: the console ships no TeX engine, so the formula
 * is shown as a centered monospace source block that is explicitly labelled as
 * math (never as code), instead of silently pretending to be a rendered formula.
 */
const MathBlock: React.FC<{ tex: string; streaming: boolean }> = ({ tex, streaming }) => (
  <div className="my-2 overflow-hidden rounded-md border border-purple-100 bg-[#faf8ff]">
    <div className="flex items-center justify-between border-b border-purple-100 bg-[#f4f0fd] px-2.5 py-1">
      <span className="font-mono text-[10px] uppercase tracking-wide text-purple-500">
        公式{streaming ? ' · streaming' : ''}
      </span>
      <span className="font-mono text-[10px] text-purple-400">LaTeX 源码（未排版）</span>
    </div>
    <pre className="overflow-auto whitespace-pre-wrap px-3 py-3 text-center font-mono text-[13px] leading-6 text-gray-800">
      {tex}
    </pre>
  </div>
);

function renderSpans(spans: Span[], keyPrefix: string): React.ReactNode[] {
  return spans.map((span, index) => {
    const key = `${keyPrefix}.${index}`;
    if (span.type === 'text') {
      return <React.Fragment key={key}>{span.text}</React.Fragment>;
    }
    if (span.type === 'math') {
      return (
        <span
          key={key}
          title={`公式：${span.tex}`}
          className="rounded bg-[#f6f4fb] px-1 font-mono text-[0.9em] text-purple-700"
        >
          {span.tex}
        </span>
      );
    }
    if (span.type === 'code') {
      return (
        <code
          key={key}
          className="rounded bg-[#f3f4f5] px-1 py-0.5 font-mono text-[0.85em] text-gray-800"
        >
          {span.text}
        </code>
      );
    }
    if (span.type === 'strong') {
      return (
        <strong key={key} className="font-semibold text-gray-900">
          {renderSpans(span.spans, key)}
        </strong>
      );
    }
    if (span.type === 'em') {
      return <em key={key}>{renderSpans(span.spans, key)}</em>;
    }
    if (span.type === 'del') {
      return (
        <del key={key} className="text-gray-400">
          {renderSpans(span.spans, key)}
        </del>
      );
    }
    return (
      <a
        key={key}
        href={span.href}
        target="_blank"
        rel="noreferrer noopener"
        className="break-all text-blue-600 underline"
      >
        {renderSpans(span.spans, key)}
      </a>
    );
  });
}

function renderBlocks(blocks: Block[], keyPrefix: string): React.ReactNode[] {
  return blocks.map((block, index) => {
    const key = `${keyPrefix}.${index}`;

    if (block.type === 'code') {
      return (
        <CodeBlock
          key={key}
          lang={block.lang}
          code={block.code}
          streaming={!block.closed}
          // No mermaid renderer ships with the console (and adding one would
          // pull in a runtime dependency): say so instead of showing a diagram
          // that is silently missing.
          note={block.lang === 'mermaid' ? '终端图形渲染未实现' : undefined}
        />
      );
    }

    if (block.type === 'math') {
      return <MathBlock key={key} tex={block.tex} streaming={!block.closed} />;
    }

    if (block.type === 'heading') {
      const size = HEADING_CLASS[block.level - 1] ?? 'text-sm';
      return (
        <div key={key} className={`mb-1.5 mt-3 font-semibold text-gray-900 ${size}`}>
          {renderSpans(block.spans, key)}
        </div>
      );
    }

    if (block.type === 'rule') {
      return <hr key={key} className="my-3 border-gray-200" />;
    }

    if (block.type === 'quote') {
      return (
        <blockquote key={key} className="my-2 border-l-2 border-gray-300 pl-3 text-gray-600">
          {renderBlocks(block.blocks, key)}
        </blockquote>
      );
    }

    if (block.type === 'list') {
      const rows = block.items.map((item, itemIndex) => {
        const itemKey = `${key}.${itemIndex}`;
        const first = item[0];
        // A single-paragraph item renders inline so list rows stay tight.
        const inline = item.length === 1 && first !== undefined && first.type === 'paragraph';
        return (
          <li key={itemKey} className="leading-relaxed">
            {inline && first !== undefined && first.type === 'paragraph'
              ? renderSpans(first.spans, itemKey)
              : renderBlocks(item, itemKey)}
          </li>
        );
      });
      return block.ordered ? (
        <ol key={key} className="my-1.5 list-decimal space-y-1 pl-5" start={block.start}>
          {rows}
        </ol>
      ) : (
        <ul key={key} className="my-1.5 list-disc space-y-1 pl-5">
          {rows}
        </ul>
      );
    }

    if (block.type === 'table') {
      return (
        // The table body reads at `text-sm` (14px) so it is legible next to the
        // 16px chat prose instead of looking like a footnote; the header keeps the
        // same size and only differs by weight.  `overflow-x-auto` keeps a wide
        // table scrolling inside its own box rather than stretching the column.
        <div key={key} className="my-2 max-w-full overflow-x-auto">
          <table className="border-collapse text-sm">
            <thead>
              <tr>
                {block.header.map((cell, cellIndex) => (
                  <th
                    key={`${key}.h${cellIndex}`}
                    className="border border-gray-200 bg-[#f8f9fa] px-3 py-1.5 text-left font-semibold text-gray-800"
                  >
                    {renderSpans(cell, `${key}.h${cellIndex}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, rowIndex) => (
                <tr key={`${key}.r${rowIndex}`}>
                  {row.map((cell, cellIndex) => (
                    <td
                      key={`${key}.r${rowIndex}c${cellIndex}`}
                      className="border border-gray-200 px-3 py-1.5 align-top text-gray-700"
                    >
                      {renderSpans(cell, `${key}.r${rowIndex}c${cellIndex}`)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    }

    return (
      <p key={key} className="my-1.5 leading-relaxed">
        {renderSpans(block.spans, key)}
      </p>
    );
  });
}

export interface MarkdownProps {
  text: string;
}

/**
 * Render Markdown produced by the model.
 *
 * Typed nodes are rendered through React (never raw HTML).  An oversized
 * document falls back to plain text so a pathological answer cannot stall the
 * transcript, and an unterminated fence still renders as a code block because
 * answers stream in token by token.
 */
const MarkdownBody: React.FC<MarkdownProps> = ({ text }) => {
  const blocks = useMemo(
    () => (text.length > PARSE_MAX_CHARS ? null : parseMarkdown(text)),
    [text],
  );
  if (blocks === null) {
    return <div className="whitespace-pre-wrap break-words">{text}</div>;
  }
  return <div className="markdown-body">{renderBlocks(blocks, 'md')}</div>;
};

/**
 * Memoized on `text`: a row that re-renders for an unrelated reason (an activity
 * tick, a fold in another row) must not re-render a document that did not change.
 * The parse cache alone would not be enough -- `renderBlocks` would still rebuild
 * every element of the document.
 */
export const Markdown = React.memo(MarkdownBody);
