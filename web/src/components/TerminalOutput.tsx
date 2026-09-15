import React, { useMemo } from 'react';
import { parseAnsi } from '../markdown/ansi.ts';

/** Above this size the escapes are left as text so a huge dump cannot stall a frame. */
const PARSE_MAX_CHARS = 40_000;

/**
 * A command's own output, painted the way the command painted it.
 *
 * A tool that runs a program returns what that program wrote to a terminal, colour
 * sequences included.  Rendering it as plain text printed the escapes themselves
 * (`[32;1mName[0m`), so the runs are read back through `parseAnsi` and the frame
 * matches the code block a file body gets.  Output is monospaced and never
 * re-wrapped: a terminal's columns carry meaning, and a wrapped table is unreadable.
 */
export const TerminalOutput: React.FC<{ text: string }> = ({ text }) => {
  const spans = useMemo(
    () => (text.length > PARSE_MAX_CHARS ? [{ text, className: '' }] : parseAnsi(text)),
    [text],
  );
  return (
    <div className="mt-1 overflow-hidden rounded-control border border-line bg-canvas">
      <pre className="fluent-scrollbar max-h-[28rem] overflow-auto whitespace-pre px-2.5 py-1.5 font-mono text-[12px] leading-5">
        <code>
          {spans.map((span, index) => (
            <span
              key={index}
              className={span.className}
              style={
                span.color !== undefined || span.backgroundColor !== undefined
                  ? { color: span.color, backgroundColor: span.backgroundColor }
                  : undefined
              }
            >
              {span.text}
            </span>
          ))}
        </code>
      </pre>
    </div>
  );
};