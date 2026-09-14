import React, { useMemo } from 'react';
import { useConsoleStore } from '../stores/useConsoleStore';
import { parseMarkdown, type Block, type Span } from '../markdown/parse.ts';
import { looksLikeFileRef, splitFileRefs } from '../markdown/filePaths.ts';
import { CodeBlock } from './CodeBlock.tsx';
import { DisplayMath, InlineMath } from './MathTex.tsx';
import { MermaidBlock } from './MermaidBlock.tsx';

/** Above this size the document is rendered as plain text instead of parsed. */
const PARSE_MAX_CHARS = 200_000;

const HEADING_CLASS = ['text-lg', 'text-base', 'text-sm', 'text-sm', 'text-xs', 'text-xs'];

/**
 * A file path the model wrote, rendered as a link-styled button.  Clicking it
 * opens the workspace file manager centered on that file (`FileViewerHost`).
 */
const FileRefButton: React.FC<{ text: string; code?: boolean }> = ({ text, code = false }) => {
  const openFileViewer = useConsoleStore((state) => state.openFileViewer);
  return (
    <button
      type="button"
      onClick={() => openFileViewer(text)}
      title={`打开文件：${text}`}
      className={
        code
          ? 'inline break-all rounded-control bg-sunken px-1 py-0.5 font-mono text-[0.85em] text-blue-500 underline decoration-dotted underline-offset-2 hover:text-blue-600'
          : 'inline break-all font-mono text-[0.92em] text-blue-500 underline decoration-dotted underline-offset-2 hover:text-blue-600'
      }
    >
      {text}
    </button>
  );
};

function renderSpans(spans: Span[], keyPrefix: string, linkify = true): React.ReactNode[] {
  return spans.map((span, index) => {
    const key = `${keyPrefix}.${index}`;
    if (span.type === 'text') {
      if (!linkify) return <React.Fragment key={key}>{span.text}</React.Fragment>;
      const parts = splitFileRefs(span.text);
      const only = parts[0];
      if (parts.length === 1 && only !== undefined && only.type === 'text') {
        return <React.Fragment key={key}>{span.text}</React.Fragment>;
      }
      return (
        <React.Fragment key={key}>
          {parts.map((part, partIndex) =>
            part.type === 'file' ? (
              <FileRefButton key={`${key}.f${partIndex}`} text={part.text} />
            ) : (
              <React.Fragment key={`${key}.t${partIndex}`}>{part.text}</React.Fragment>
            ),
          )}
        </React.Fragment>
      );
    }
    if (span.type === 'math') {
      return <InlineMath key={key} tex={span.tex} />;
    }
    if (span.type === 'code') {
      // Models usually wrap a path in backticks; a code span that is exactly a
      // path is a file reference too, so it stays clickable.
      const trimmed = span.text.trim();
      if (linkify && !/\s/.test(trimmed) && looksLikeFileRef(trimmed)) {
        return <FileRefButton key={key} text={trimmed} code />;
      }
      return (
        <code
          key={key}
          className="rounded-control bg-sunken px-1 py-0.5 font-mono text-[0.85em] text-gray-800"
        >
          {span.text}
        </code>
      );
    }
    if (span.type === 'strong') {
      return (
        <strong key={key} className="font-semibold text-gray-900">
          {renderSpans(span.spans, key, linkify)}
        </strong>
      );
    }
    if (span.type === 'em') {
      return <em key={key}>{renderSpans(span.spans, key, linkify)}</em>;
    }
    if (span.type === 'del') {
      return (
        <del key={key} className="text-gray-400">
          {renderSpans(span.spans, key, linkify)}
        </del>
      );
    }
    // `<br>` in the source: a real element, so a cell can stack several values
    // without the tag itself ever reaching the DOM as text or as markup.
    if (span.type === 'break') {
      return <br key={key} />;
    }
    return (
      <a
        key={key}
        href={span.href}
        target="_blank"
        rel="noreferrer noopener"
        className="break-all text-blue-500 underline"
      >
        {renderSpans(span.spans, key, false)}
      </a>
    );
  });
}

function renderBlocks(blocks: Block[], keyPrefix: string): React.ReactNode[] {
  return blocks.map((block, index) => {
    const key = `${keyPrefix}.${index}`;

    if (block.type === 'code') {
      // A `mermaid` fence is a diagram, not code: it renders as an SVG, and
      // falls back to this same code block (with a visible reason) while it
      // streams or when it cannot be rendered.
      if (block.lang === 'mermaid') {
        return <MermaidBlock key={key} code={block.code} streaming={!block.closed} />;
      }
      return (
        <CodeBlock
          key={key}
          lang={block.lang}
          code={block.code}
          streaming={!block.closed}
        />
      );
    }

    if (block.type === 'math') {
      return <DisplayMath key={key} tex={block.tex} streaming={!block.closed} />;
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
      return <hr key={key} className="my-3 border-line" />;
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
                    className="border border-line bg-sunken px-3 py-1.5 text-left font-semibold text-gray-800"
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
                      className="border border-line px-3 py-1.5 align-top text-gray-700"
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
 * answers stream in token by token.  The two exceptions whose output is markup
 * by nature -- KaTeX formulas and mermaid diagrams -- are rendered by their own
 * components, which own the sanitization (see `GeneratedHtml.tsx`).
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
