import React, { useMemo } from 'react';
import { parseMarkdown, type Block, type Span } from '../markdown/parse.ts';
import { CodeBlock } from './CodeBlock.tsx';

/** Above this size the document is rendered as plain text instead of parsed. */
const PARSE_MAX_CHARS = 200_000;

const HEADING_CLASS = ['text-lg', 'text-base', 'text-sm', 'text-sm', 'text-xs', 'text-xs'];

function renderSpans(spans: Span[], keyPrefix: string): React.ReactNode[] {
  return spans.map((span, index) => {
    const key = `${keyPrefix}.${index}`;
    if (span.type === 'text') {
      return <React.Fragment key={key}>{span.text}</React.Fragment>;
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
      return <CodeBlock key={key} lang={block.lang} code={block.code} streaming={!block.closed} />;
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
        <div key={key} className="my-2 overflow-x-auto">
          <table className="border-collapse text-xs">
            <thead>
              <tr>
                {block.header.map((cell, cellIndex) => (
                  <th
                    key={`${key}.h${cellIndex}`}
                    className="border border-gray-200 bg-[#f8f9fa] px-2 py-1 text-left font-semibold text-gray-800"
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
                      className="border border-gray-200 px-2 py-1 align-top text-gray-700"
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
export const Markdown: React.FC<MarkdownProps> = ({ text }) => {
  const blocks = useMemo(
    () => (text.length > PARSE_MAX_CHARS ? null : parseMarkdown(text)),
    [text],
  );
  if (blocks === null) {
    return <div className="whitespace-pre-wrap break-words">{text}</div>;
  }
  return <div className="markdown-body">{renderBlocks(blocks, 'md')}</div>;
};
