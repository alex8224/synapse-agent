import { Info20Regular, Warning20Regular } from '@fluentui/react-icons';
import React from 'react';
import type { RowRenderProps } from './context.ts';

/**
 * A runtime notice (`info` / `warning`), as a compact log line.
 *
 * The level decides the colour and the glyph; the row is not a fold and owns no
 * state, so it renders straight from its message.
 */
export const InfoRow = React.memo(function InfoRow({ message }: RowRenderProps) {
  const warning = message.infoLevel === 'warning';
  return (
    <div
      className={`flex max-w-[85%] items-start gap-1.5 rounded-control border px-2.5 py-1.5 font-mono text-xs leading-relaxed ${
        warning
          ? 'border-amber-200 bg-amber-50 text-amber-800'
          : 'border-line bg-surface text-gray-600'
      }`}
    >
      {warning ? (
        <Warning20Regular aria-hidden="true" className="shrink-0 text-amber-600" style={{ fontSize: '14px' }} />
      ) : (
        <Info20Regular aria-hidden="true" className="shrink-0 text-blue-500" style={{ fontSize: '14px' }} />
      )}
      <span className="whitespace-pre-wrap break-all">{message.content}</span>
    </div>
  );
});
