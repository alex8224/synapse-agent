import React, { useRef, useState } from 'react';
import { useConsoleStore } from '../stores/useConsoleStore';
import { formatBytes } from '../runtime-client/artifacts.ts';
import { ATTACHMENT_MAX_COUNT } from '../runtime-client/attachments.ts';

/**
 * Floating command card.
 *
 * The single round button is the primary action for the current state: `↑`
 * sends when idle, and becomes an enabled `■` stop button while a turn is
 * running (the previous behaviour left it looking disabled because the input
 * was empty, with no way to interrupt from the UI).  Typing while busy still
 * queues a steer — that path is Enter, not the button.
 *
 * Images are added with the paperclip (file picker) or by dropping them onto
 * the card.  Only the image types the runtime accepts are taken, at most eight
 * per submit and 4 MB each; every refusal is shown next to the composer instead
 * of being silently dropped.  A chunk still uploading disables sending, and an
 * attachment-only turn may be submitted with empty text.
 */
export const CommandInput: React.FC = () => {
  const [text, setText] = useState('');
  const [dragging, setDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const {
    steerQueueCount,
    runtimeStatus,
    submitPrompt,
    cancelActiveTurn,
    attachments,
    attachmentError,
    addAttachments,
    removeAttachment,
  } = useConsoleStore();

  const busy = runtimeStatus === 'running';
  const uploading = attachments.some((entry) => entry.status === 'uploading');
  const readyAttachment = attachments.some((entry) => entry.status === 'ready');
  const ready = text.trim() !== '' || readyAttachment;
  const canSend = ready && !uploading;

  const handleSubmit = (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    if (uploading) {
      // The store refuses too (and publishes the reason); this only keeps the
      // optimistic input text in place.
      void submitPrompt(text);
      return;
    }
    if (!ready) return;
    void submitPrompt(text);
    setText('');
  };

  const handleFiles = (files: FileList | null) => {
    if (!files || files.length === 0) return;
    void addAttachments(Array.from(files));
  };

  return (
    <div className="absolute bottom-10 left-0 w-full px-8 pointer-events-none flex justify-center z-30">
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          handleFiles(e.dataTransfer?.files ?? null);
        }}
        className={`w-full max-w-3xl pointer-events-auto bg-white border rounded-lg shadow-sm flex flex-col focus-within:border-blue-500 transition-colors ${
          dragging ? 'border-blue-500 ring-2 ring-blue-100' : 'border-[#e5e7eb]'
        }`}
      >
        <div className="flex items-center gap-2 rounded-t-lg border-b border-[#f1f2f4] bg-[#fbfcfd] px-3.5 py-1.5">
          {busy ? (
            <>
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-blue-600" />
              <span className="font-mono text-xs text-gray-600">
                运行中 · Steer 队列 {steerQueueCount}
              </span>
            </>
          ) : (
            <span className="font-mono text-xs text-gray-500">Ready</span>
          )}
          {attachments.length > 0 && (
            <span className="font-mono text-xs text-gray-400">
              附件 {attachments.length}/{ATTACHMENT_MAX_COUNT}
            </span>
          )}
        </div>

        {attachments.length > 0 && (
          <div className="flex flex-wrap gap-2 px-3.5 pt-2.5">
            {attachments.map((entry) => {
              const percent =
                entry.size > 0
                  ? Math.min(100, Math.round((entry.uploadedBytes / entry.size) * 100))
                  : 0;
              const failed = entry.status === 'failed';
              return (
                <div
                  key={entry.localId}
                  className={`flex items-center gap-2 rounded border px-2 py-1 font-mono text-[11px] ${
                    failed
                      ? 'border-red-200 bg-red-50/70 text-red-700'
                      : 'border-gray-200 bg-[#f8f9fa] text-gray-600'
                  }`}
                  title={entry.error ?? entry.name}
                >
                  <span className="material-symbols-outlined text-[14px]">
                    {failed ? 'broken_image' : 'image'}
                  </span>
                  <span className="max-w-[160px] truncate">{entry.name}</span>
                  <span className="tabular-nums text-gray-400">{formatBytes(entry.size)}</span>
                  {entry.status === 'uploading' && (
                    <>
                      <span className="tabular-nums text-blue-600">{percent}%</span>
                      <span className="h-1 w-10 overflow-hidden rounded bg-gray-200">
                        <span
                          className="block h-full bg-blue-500"
                          style={{ width: `${percent}%` }}
                        />
                      </span>
                    </>
                  )}
                  {failed && <span className="text-red-600">上传失败</span>}
                  <button
                    type="button"
                    onClick={() => removeAttachment(entry.localId)}
                    title={entry.status === 'uploading' ? '取消上传' : '移除附件'}
                    className="cursor-pointer text-gray-400 hover:text-gray-700"
                  >
                    <span className="material-symbols-outlined text-[14px]">
                      {entry.status === 'uploading' ? 'cancel' : 'close'}
                    </span>
                  </button>
                </div>
              );
            })}
          </div>
        )}

        {attachmentError !== null && (
          <div
            role="alert"
            className="mx-3.5 mt-2 rounded border border-red-200 bg-red-50/70 px-2 py-1 font-mono text-[11px] leading-relaxed text-red-700"
          >
            {attachmentError}
          </div>
        )}

        <form onSubmit={handleSubmit} className="flex items-center px-3 py-2.5">
          <span className="text-gray-400 mr-2 text-xs font-mono font-semibold">›</span>
          <input
            type="text"
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                handleSubmit();
              }
            }}
            placeholder={
              busy ? '运行中：输入内容回车可插话排队' : 'Build anything (/ for commands, @ for files)'
            }
            className="flex-1 bg-transparent border-none text-gray-900 text-xs placeholder:text-gray-400 focus:outline-none font-sans"
          />
          <input
            ref={fileInputRef}
            type="file"
            accept="image/*"
            multiple
            className="hidden"
            onChange={(e) => {
              handleFiles(e.target.files);
              e.target.value = '';
            }}
          />
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            title={`添加图片附件（最多 ${ATTACHMENT_MAX_COUNT} 张，每张 4 MB）`}
            className="ml-1 flex h-7 w-7 shrink-0 cursor-pointer items-center justify-center rounded-full text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-800"
          >
            <span className="material-symbols-outlined text-[16px]">attach_file</span>
          </button>
          {busy ? (
            <button
              type="button"
              onClick={() => {
                void cancelActiveTurn();
              }}
              title="停止当前轮次 (Ctrl+C)"
              className="ml-1 flex h-7 w-7 shrink-0 cursor-pointer items-center justify-center rounded-full bg-[#dc2626] text-white transition-colors hover:bg-red-700"
            >
              <span className="material-symbols-outlined text-[16px]">stop</span>
            </button>
          ) : (
            <button
              type="submit"
              disabled={!canSend}
              title={uploading ? '附件仍在上传中' : 'Send (Enter)'}
              className="ml-1 flex h-7 w-7 shrink-0 cursor-pointer items-center justify-center rounded-full bg-[#2563eb] text-white transition-colors hover:bg-blue-700 disabled:opacity-40"
            >
              <span className="material-symbols-outlined text-[16px]">arrow_upward</span>
            </button>
          )}
        </form>
      </div>
    </div>
  );
};
