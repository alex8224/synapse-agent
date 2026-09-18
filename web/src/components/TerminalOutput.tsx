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
 *
 * `command` is the invocation, painted as the session's first line behind a `$`
 * prompt.  It belongs to the same scroll region as the output rather than sitting
 * above it as a header: prompt plus output is what a terminal shows, and the pair
 * scrolls and selects as one.
 */
export const TerminalOutput: React.FC<{ text: string; command?: string }> = ({ text, command }) => {
  const spans = useMemo(
    () => (text.length > PARSE_MAX_CHARS ? [{ text, className: '' }] : parseAnsi(text)),
    [text],
  );
  const prompt = (command ?? '').trim();
  return (
    <div className="mt-1 overflow-hidden rounded-control border border-line bg-canvas">
      <pre className="fluent-scrollbar max-h-[28rem] overflow-auto whitespace-pre px-2.5 py-1.5 font-mono text-[12px] leading-5">
        {prompt !== '' && (
          <span className="block text-gray-500">
            <span className="text-gray-400">$ </span>
            {prompt}
          </span>
        )}
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