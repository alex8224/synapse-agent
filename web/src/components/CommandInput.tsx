import React, { useRef, useState } from 'react';
import { AttachmentPreview } from './AttachmentPreview.tsx';
import { ModelControls } from './ModelControls.tsx';
import { useConsoleStore } from '../stores/useConsoleStore';
import { useShallow } from 'zustand/react/shallow';
import { formatBytes } from '../runtime-client/artifacts.ts';
import { ATTACHMENT_MAX_COUNT } from '../runtime-client/attachments.ts';

/**
 * Floating command card: the prompt line, then one control row inside the same
 * rounded box — add-image on the left, the model and reasoning level the next
 * turn will run on, and the primary action on the right.  Those two pickers used
 * to sit in the status bar; they configure the next turn, so they belong next to
 * the input that starts it.
 *
 * The single round button is the primary action for the current state: `↑`
 * sends when idle, and becomes an enabled `■` stop button while a turn is
 * running (the previous behaviour left it looking disabled because the input
 * was empty, with no way to interrupt from the UI).  Typing while busy still
 * queues a steer — that path is Enter, not the button.
 *
 * Images are added with the `+` (file picker), by pasting them into the card, or
 * by dropping them onto it — all three end in the same `handleFiles`
 * path.  Only the image types the runtime accepts are taken, at most eight per
 * submit and 4 MB each; every refusal is shown next to the composer instead of
 * being silently dropped.  A chunk still uploading disables sending, and an
 * attachment-only turn may be submitted with empty text.  Each pending row is the
 * picked image itself (`AttachmentPreview`) rather than a file-name chip, and
 * hovering it enlarges the copy, so what will be sent is verifiable before the
 * turn is submitted.
 */
export const CommandInput: React.FC = () => {
  const [text, setText] = useState('');
  const [dragging, setDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const {
    runtimeStatus,
    submitPrompt,
    cancelActiveTurn,
    attachments,
    attachmentError,
    addAttachments,
    removeAttachment,
  } = useConsoleStore(
    // Only the fields the composer paints: a reasoning delta must not re-render
    // it (and must not touch the text the user is typing).
    useShallow((state) => ({
      runtimeStatus: state.runtimeStatus,
      submitPrompt: state.submitPrompt,
      cancelActiveTurn: state.cancelActiveTurn,
      attachments: state.attachments,
      attachmentError: state.attachmentError,
      addAttachments: state.addAttachments,
      removeAttachment: state.removeAttachment,
    })),
  );

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
    // The last row of the workspace column, not a floating card: the transcript
    // keeps its own height above it, so the newest streamed line is visible at the
    // bottom of the scrollport instead of behind the input.  It keeps the shared
    // gutters and the shared `console-column` width, and the transcript shows no
    // scrollbar, so the card's edges line up with the chat column above it.
    <div className="console-gutter pointer-events-none z-30 flex w-full shrink-0 justify-center pb-3">
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
        onPaste={(e) => {
          // Only an image paste is intercepted: a text paste keeps its default
          // behaviour, because the composer is an ordinary text input.
          const files = e.clipboardData?.files;
          if (!files || files.length === 0) return;
          e.preventDefault();
          handleFiles(files);
        }}
        className={`console-column pointer-events-auto flex flex-col rounded-lg border bg-white shadow-sm transition-colors focus-within:border-blue-500 ${
          dragging ? 'border-blue-500 ring-2 ring-blue-100' : 'border-[#e5e7eb]'
        }`}
      >
        {attachments.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 px-3.5 pt-2.5">
            {attachments.map((entry) => {
              const percent =
                entry.size > 0
                  ? Math.min(100, Math.round((entry.uploadedBytes / entry.size) * 100))
                  : 0;
              const failed = entry.status === 'failed';
              return (
                <div
                  key={entry.localId}
                  // The chip is the image, not a file row: the name, type and size
                  // live in the tooltip and the hover preview, and a failure is
                  // still spelled out in the alert below the composer.
                  className={`relative rounded border p-1 ${
                    failed ? 'border-red-200 bg-red-50/70' : 'border-gray-200 bg-[#f8f9fa]'
                  }`}
                  title={entry.error ?? `${entry.name} · ${entry.mime} · ${formatBytes(entry.size)}`}
                >
                  <AttachmentPreview
                    source={entry.source}
                    label={entry.name}
                    mime={entry.mime}
                    size={entry.size}
                    failed={failed}
                  />
                  {entry.status === 'uploading' && (
                    <span className="pointer-events-none absolute inset-1 flex items-center justify-center rounded bg-white/70 font-mono text-[10px] tabular-nums text-blue-700">
                      {percent}%
                    </span>
                  )}
                  <button
                    type="button"
                    onClick={() => removeAttachment(entry.localId)}
                    title={entry.status === 'uploading' ? '取消上传' : '移除附件'}
                    className="absolute -right-1.5 -top-1.5 flex h-4 w-4 cursor-pointer items-center justify-center rounded-full border border-gray-200 bg-white text-gray-500 shadow-sm transition-colors hover:text-gray-900"
                  >
                    <span className="material-symbols-outlined text-[12px]">
                      {entry.status === 'uploading' ? 'cancel' : 'close'}
                    </span>
                  </button>
                </div>
              );
            })}
            {/* The cap belongs with the chips: there is no separate status row. */}
            <span className="font-mono text-[10px] text-gray-400">
              {attachments.length}/{ATTACHMENT_MAX_COUNT}
            </span>
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

        <form onSubmit={handleSubmit} className="flex flex-col">
          <input
            id="console-composer"
            name="prompt"
            type="text"
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                handleSubmit();
              }
            }}
            // The busy state lives in the placeholder (as in the reference
            // composer) instead of a status row; the steer count is already shown
            // on the transcript's own status strip.
            // No "/ for commands, @ for files" hint either: neither a command
            // palette nor file mention exists in this console.
            placeholder={busy ? '继续输入以排队后续修改' : 'Build anything'}
            className="w-full bg-transparent px-3.5 pb-1 pt-3 text-sm text-gray-900 placeholder:text-gray-400 focus:outline-none font-sans"
          />
          <input
            ref={fileInputRef}
            id="console-attachments"
            name="attachments"
            type="file"
            accept="image/*"
            multiple
            className="hidden"
            onChange={(e) => {
              handleFiles(e.target.files);
              e.target.value = '';
            }}
          />
          {/* Control row: add on the left, what the next turn runs on the right. */}
          <div className="flex items-center gap-2 px-2 pb-1.5">
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              title={`添加图片附件（也可直接粘贴或拖入；最多 ${ATTACHMENT_MAX_COUNT} 张，每张 4 MB）`}
              className="flex h-7 w-7 shrink-0 cursor-pointer items-center justify-center rounded-md text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-800"
            >
              <span className="material-symbols-outlined text-[18px]">add</span>
            </button>
            <div className="ml-auto flex min-w-0 items-center gap-3">
              <ModelControls />
            </div>
          {busy ? (
            <button
              type="button"
              onClick={() => {
                void cancelActiveTurn();
              }}
              title="停止当前轮次 (Ctrl+C)"
              className="flex h-7 w-7 shrink-0 cursor-pointer items-center justify-center rounded-full bg-[#dc2626] text-white transition-colors hover:bg-red-700"
            >
              <span className="material-symbols-outlined text-[16px]">stop</span>
            </button>
          ) : (
            <button
              type="submit"
              disabled={!canSend}
              title={uploading ? '附件仍在上传中' : 'Send (Enter)'}
              className="flex h-7 w-7 shrink-0 cursor-pointer items-center justify-center rounded-full bg-[#2563eb] text-white transition-colors hover:bg-blue-700 disabled:opacity-40"
            >
              <span className="material-symbols-outlined text-[16px]">arrow_upward</span>
            </button>
          )}
          </div>
        </form>
      </div>
    </div>
  );
};
