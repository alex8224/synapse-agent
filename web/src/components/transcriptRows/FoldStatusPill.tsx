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
export function FoldStatusPill({ status }: { status: GroupIntentStatus }) {
  const badge = 'inline-flex shrink-0 items-center gap-1 rounded-control border px-1.5 py-0.5 font-mono text-xs';
  if (status.kind === 'thinking') {
    return status.state === 'running' ? (
      <span className={`${badge} border-blue-200/50 bg-blue-50/80 text-blue-600`}>
        <Sparkle20Regular aria-hidden="true" className="shrink-0 animate-pulse" style={{ fontSize: '12px' }} />
        <span>{status.text}</span>
      </span>
    ) : (
      <span className={`${badge} border-line bg-sunken/60 text-gray-500`}>
        <BrainCircuit20Regular aria-hidden="true" className="shrink-0" style={{ fontSize: '12px' }} />
        <span>{status.text}</span>
      </span>
    );
  }
  if (status.state === 'running') {
    return (
      <span className={`${badge} max-w-[24rem] border-blue-200 bg-blue-50/90 text-blue-700`}>
        <SpinnerIos20Regular aria-hidden="true" className="shrink-0 animate-spin text-blue-600" style={{ fontSize: '12px' }} />
        <span className="truncate" title={status.text}>{status.text}</span>
      </span>
    );
  }
  if (status.state === 'failed') {
    return (
      <span className={`${badge} max-w-[24rem] border-red-200/60 bg-red-50/80 text-red-600`}>
        <DismissCircle20Regular aria-hidden="true" className="shrink-0 text-red-500" style={{ fontSize: '12px' }} />
        <span className="truncate" title={status.text}>{status.text}</span>
      </span>
    );
  }
  return (
    <span className={`${badge} max-w-[24rem] border-line bg-sunken/60 text-gray-600`}>
      <Checkmark16Regular aria-hidden="true" className="shrink-0 text-green-600" style={{ fontSize: '12px' }} />
      <span className="truncate" title={status.text}>{status.text}</span>
    </span>
  );
}
