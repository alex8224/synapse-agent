import { Checkmark16Regular, Copy16Regular, Edit16Regular } from '@fluentui/react-icons';
import React, { useState } from 'react';
import { useConsoleStore } from '../../stores/useConsoleStore';
import { AttachmentThumb } from '../AttachmentThumb.tsx';
import type { RowRenderProps } from './context.ts';
import { updateSpotlight } from './spotlight.ts';
import { useCopyFlag } from './useCopyFlag.ts';

/**
 * The user's own turn: on the right, with its copy / edit-and-resend actions.
 *
 * The side a turn is on *is* the role, so no heading is printed.  The 20% right inset
 * shares the assistant body's right edge: that block is capped at 80% of the reading
 * column, so the user's turn is held back by whatever is left.  The two numbers must
 * keep summing to 100% (`transcriptLayoutGuard.test.ts`), which is what keeps the
 * bubble from hanging past the answer it belongs to.
 *
 * `data-turn-id` is the anchor the turn rail scrolls to.
 */
export const UserRow = React.memo(function UserRow({ message }: RowRenderProps) {
  const submitPrompt = useConsoleStore((state) => state.submitPrompt);
  const [isEditing, setIsEditing] = useState(false);
  const [editText, setEditText] = useState(message.content ?? '');
  const [copied, copy] = useCopyFlag();

  const sendEdit = () => {
    const trimmed = editText.trim();
    if (!trimmed) return;
    void submitPrompt(trimmed);
    setIsEditing(false);
  };

  return (
    <div data-turn-id={message.id} className="flex justify-end">
      <div className="mr-[20%] flex max-w-[80%] flex-col items-end gap-1.5 group">
        {isEditing ? (
          <div className="w-full flex flex-col gap-2 rounded-card border border-accent bg-surface p-2.5 shadow-card">
            <textarea
              value={editText}
              onChange={(e) => setEditText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  sendEdit();
                } else if (e.key === 'Escape') {
                  setIsEditing(false);
                }
              }}
              className="w-full resize-none bg-transparent text-sm text-gray-900 focus:outline-none font-sans"
              rows={Math.min(8, Math.max(2, editText.split('\n').length))}
              autoFocus
            />
            <div className="flex items-center justify-end gap-2 text-xs">
              <button
                type="button"
                onClick={() => setIsEditing(false)}
                className="rounded px-2.5 py-1 text-gray-500 hover:bg-surface-hover cursor-pointer"
              >
                取消
              </button>
              <button
                type="button"
                onClick={sendEdit}
                disabled={!editText.trim()}
                className="rounded bg-accent px-3 py-1 text-on-accent font-medium hover:bg-blue-700 cursor-pointer disabled:opacity-50"
              >
                发送
              </button>
            </div>
          </div>
        ) : (
          <>
            {message.content !== '' && (
              <div
                onMouseMove={updateSpotlight}
                className="ui-user-bubble fluent-spotlight whitespace-pre-wrap break-words text-base leading-relaxed text-gray-900"
              >
                {message.content}
              </div>
            )}
            {message.attachments !== undefined && message.attachments.length > 0 && (
              <div className="flex flex-wrap justify-end gap-2">
                {message.attachments.map((attachment) => (
                  <AttachmentThumb key={attachment.attachmentId} attachment={attachment} />
                ))}
              </div>
            )}
            <div className="flex items-center gap-1">
              <button
                type="button"
                onClick={() => copy(message.content ?? '')}
                title={copied ? '已复制' : '复制消息'}
                aria-label="复制消息"
                className="p-1 rounded text-gray-400 hover:text-gray-700 hover:bg-surface-hover cursor-pointer transition-colors"
              >
                {copied ? <Checkmark16Regular aria-hidden="true" className="text-accent" /> : <Copy16Regular aria-hidden="true" />}
              </button>
              <button
                type="button"
                onClick={() => {
                  setIsEditing(true);
                  setEditText(message.content ?? '');
                }}
                title="编辑并重新发送"
                aria-label="编辑并重新发送"
                className="p-1 rounded text-gray-400 hover:text-gray-700 hover:bg-surface-hover cursor-pointer transition-colors"
              >
                <Edit16Regular aria-hidden="true" />
              </button>
              <span className="font-mono text-[10px] text-gray-400 ml-1">{message.timestamp}</span>
            </div>
          </>
        )}
      </div>
    </div>
  );
});
