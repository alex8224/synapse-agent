import { BranchFork16Regular, Checkmark16Regular, Copy16Regular } from '@fluentui/react-icons';
import React from 'react';
import { Markdown } from '../Markdown.tsx';
import type { RowRenderProps } from './context.ts';
import { useCopyFlag } from './useCopyFlag.ts';

/**
 * The assistant's answer: body typography on the left, with copy and fork actions.
 *
 * It hugs the left edge and is capped at 80% of the reading column -- the pair the
 * user row's 20% inset completes (`transcriptLayoutGuard.test.ts`).
 *
 * The fork action starts a new session *before this turn*: the child inherits the
 * parent's projected text history (tool outputs omitted) and the console attaches
 * to it.  It sits next to copy, on every assistant answer.
 */
export const AssistantRow = React.memo(function AssistantRow({ message, actions }: RowRenderProps) {
  const [copied, copy] = useCopyFlag();
  return (
    <div className="flex max-w-[80%] flex-col items-start gap-1.5">
      <div className="text-base leading-relaxed font-sans text-gray-900">
        <Markdown text={message.content ?? ''} />
      </div>
      <div className="flex items-center gap-1.5 pt-0.5">
        <button
          type="button"
          onClick={() => copy(message.content ?? '')}
          title={copied ? '已复制' : '复制回答'}
          aria-label="复制回答"
          className="p-1 rounded text-gray-400 hover:text-gray-700 hover:bg-surface-hover cursor-pointer transition-colors"
        >
          {copied ? <Checkmark16Regular aria-hidden="true" className="text-accent" /> : <Copy16Regular aria-hidden="true" />}
        </button>
        <button
          type="button"
          onClick={() => actions.onFork(message.turnId ?? '')}
          title="从此处分叉新会话"
          aria-label="从此处分叉新会话"
          className="p-1 rounded text-gray-400 hover:text-gray-700 hover:bg-surface-hover cursor-pointer transition-colors"
        >
          <BranchFork16Regular aria-hidden="true" />
        </button>
        <span className="font-mono text-[10px] text-gray-400">{message.timestamp}</span>
      </div>
    </div>
  );
});
