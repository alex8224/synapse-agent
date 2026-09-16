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
    'inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs transition-colors select-none max-w-[26rem] leading-none';
  if (status.kind === 'thinking') {
    return status.state === 'running' ? (
      <span className={`${basePill} border-blue-500/25 bg-blue-500/10 text-blue-500`}>
        <Sparkle20Regular aria-hidden="true" className="shrink-0 animate-pulse" style={{ fontSize: '13px' }} />
        <StatusContent status={status} />
      </span>
    ) : (
      <span className={`${basePill} border-line/40 bg-sunken/40 text-gray-500`}>
        <BrainCircuit20Regular aria-hidden="true" className="shrink-0" style={{ fontSize: '13px' }} />
        <StatusContent status={status} />
      </span>
    );
  }
  if (status.state === 'running') {
    return (
      <span className={`${basePill} border-blue-500/25 bg-blue-500/10 text-blue-500`}>
        <SpinnerIos20Regular aria-hidden="true" className="shrink-0 animate-spin text-blue-500" style={{ fontSize: '12px' }} />
        <StatusContent status={status} />
      </span>
    );
  }
  if (status.state === 'failed') {
    return (
      <span className={`${basePill} border-danger/25 bg-danger/10 text-danger`}>
        <DismissCircle20Regular aria-hidden="true" className="shrink-0 text-danger" style={{ fontSize: '13px' }} />
        <StatusContent status={status} />
      </span>
    );
  }
  return (
    <span className={`${basePill} border-line/35 bg-sunken/40 text-gray-600`}>
      <Checkmark16Regular aria-hidden="true" className="shrink-0 text-green-600" style={{ fontSize: '12px' }} />
      <StatusContent status={status} />
    </span>
  );
}
