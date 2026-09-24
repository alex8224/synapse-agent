import React, { useEffect, useMemo, useRef, useState } from 'react';
import type { Block, Span } from '../markdown/parse.ts';
import { MarkdownParseQueue, PARSE_MAX_CHARS } from '../markdown/parseQueue.ts';
import type { MarkdownDocument, MarkdownSnapshot } from '../markdown/parseQueue.ts';
import { looksLikeFileRef, splitFileRefs } from '../markdown/filePaths.ts';
import { CodeBlock } from './CodeBlock.tsx';
import { FileRefButton } from './FileRefButton.tsx';
import { MarkdownImage } from './MarkdownImage.tsx';
import { DisplayMath, InlineMath } from './MathTex.tsx';
import { MermaidBlock } from './MermaidBlock.tsx';

const parser = new MarkdownParseQueue(() => new Worker(
  new URL('../markdown/parse.worker.ts', import.meta.url), { type: 'module' },
));

const HEADING_CLASS = ['text-lg', 'text-base', 'text-sm', 'text-sm', 'text-xs', 'text-xs'];

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
    if (span.type === 'image') {
      // The reference decides for itself whether it may be read (a local
      // workspace path) or only linked (a remote URL): see `imageRefs.ts`.
      return <MarkdownImage key={key} alt={span.alt} src={span.src} />;
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
  return blocks.map((block, index) => (
    <MarkdownBlock key={`${keyPrefix}.${index}`} block={block} nodeKey={`${keyPrefix}.${index}`} />
  ));
}

// Worker results preserve the identity of unchanged blocks, so streamed tails
// do not rebuild prior tables, highlighted code, math or diagrams on every tick.
const MarkdownBlock = React.memo(function MarkdownBlock({ block, nodeKey: key }: {
  block: Block;
  nodeKey: string;
}) {
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
      // The wrapper carries the frame and the opaque surface: the cells stay on
      // a solid `--surface` instead of the semi-transparent mica behind the
      // chat, and `index.css` hides the table's own outer border so the frame
      // is drawn once.
      <div
        key={key}
        className="my-2 max-w-full overflow-x-auto rounded-control border border-line bg-surface"
      >
        <table className="w-full border-collapse text-sm">
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
  const channelRef = useRef<MarkdownDocument | null>(null);
  const [snapshot, setSnapshot] = useState<MarkdownSnapshot | null>(null);
  useEffect(() => {
    const channel = parser.open((next) => {
      // Parsing is off-thread and unchanged blocks are memoized. Do not demote
      // this commit to a transition: urgent streaming store notifications can
      // continually preempt it, leaving the answer blank until the turn ends.
      setSnapshot(next);
    });
    channelRef.current = channel;
    return () => { channel.dispose(); channelRef.current = null; };
  }, []);
  useEffect(() => { channelRef.current?.update(text); }, [text]);

  // Keep a completed prefix while its next version is being parsed, but never
  // flash another document's contents after a replacement or a session switch.
  const blocks = text.length <= PARSE_MAX_CHARS && snapshot !== null &&
    snapshot.source.length > 0 &&
    text.startsWith(snapshot.source) ? snapshot.blocks : null;
  const content = useMemo(
    () => blocks === null ? null : renderBlocks(blocks, 'md'),
    [blocks],
  );
  if (blocks === null) {
    return <div className="whitespace-pre-wrap break-words">{text}</div>;
  }
  return <div className="markdown-body">{content}</div>;
};

/**
 * Memoized on `text`: a row that re-renders for an unrelated reason (an activity
 * tick, a fold in another row) must not re-render a document that did not change.
 * The parse cache alone would not be enough -- `renderBlocks` would still rebuild
 * every element of the document.
 */
export const Markdown = React.memo(MarkdownBody);
