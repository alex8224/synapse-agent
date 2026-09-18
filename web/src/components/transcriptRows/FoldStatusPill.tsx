import {
  BrainCircuit20Regular,
  Checkmark16Regular,
  DismissCircle20Regular,
  Sparkle20Regular,
  SpinnerIos20Regular,
} from '@fluentui/react-icons';
import React from 'react';
import type { GroupIntentStatus } from '../../stores/turnWork.ts';

/**
 * What the fold's own steps are doing, as a pill beside the "已工作" header.
 *
 * It reads the group's rows (`turnWork.ts::getGroupIntentStatus`), so it says what the
 * turn is *doing* -- thinking, running a named tool, or the outcome of the last one --
 * instead of only how long it has been running.  A group with nothing to report paints
 * no pill at all.
 */
function StatusContent({ status }: { status: GroupIntentStatus }) {
  if (status.kind === 'tool' && status.toolName && status.intent) {
    return (
      <span className="inline-flex min-w-0 items-center gap-1.5 truncate">
        <span className="shrink-0 font-mono text-[11.5px] font-medium opacity-80">
          {status.toolName}
        </span>
        <span className="shrink-0 opacity-40">·</span>
        <span className="truncate font-sans text-xs opacity-90" title={status.intent}>
          {status.intent}
        </span>
      </span>
    );
  }
  return (
    <span
      className={`truncate text-xs ${status.kind === 'tool' ? 'font-mono' : 'font-sans'}`}
      title={status.text}
    >
      {status.text}
    </span>
  );
}

export function FoldStatusPill({ status }: { status: GroupIntentStatus }) {
  const basePill =
    'inline-flex shrink-0 items-center gap-1.5 rounded-full border border-line/35 bg-sunken/40 px-2.5 py-0.5 text-xs text-gray-600 transition-colors select-none max-w-[26rem] leading-none';
  if (status.kind === 'thinking') {
    return (
      <span className={basePill}>
        {status.state === 'running' ? (
          <Sparkle20Regular aria-hidden="true" className="shrink-0 animate-pulse text-gray-400" style={{ fontSize: '13px' }} />
        ) : (
          <BrainCircuit20Regular aria-hidden="true" className="shrink-0 text-gray-400" style={{ fontSize: '13px' }} />
        )}
        <StatusContent status={status} />
      </span>
    );
  }
  if (status.state === 'running') {
    return (
      <span className={basePill}>
        <SpinnerIos20Regular aria-hidden="true" className="shrink-0 animate-spin text-gray-400" style={{ fontSize: '12px' }} />
        <StatusContent status={status} />
      </span>
    );
  }
  if (status.state === 'failed') {
    return (
      <span className={basePill}>
        <DismissCircle20Regular aria-hidden="true" className="shrink-0 text-danger" style={{ fontSize: '13px' }} />
        <StatusContent status={status} />
      </span>
    );
  }
  return (
    <span className={basePill}>
      <Checkmark16Regular aria-hidden="true" className="shrink-0 text-green-600" style={{ fontSize: '12px' }} />
      <StatusContent status={status} />
    </span>
  );
}
