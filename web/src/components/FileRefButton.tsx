import React from 'react';
import { useConsoleStore } from '../stores/useConsoleStore.ts';

/**
 * A file path the model wrote, rendered as a link-styled button.  Clicking it
 * opens the workspace file manager centered on that file (`FileViewerHost`).
 *
 * Shared by the Markdown renderer (a path in prose or in a code span) and by
 * `MarkdownImage`, which falls back to it when a reference cannot be shown as a
 * picture (an oversized image, a non-image file, an unreadable path).
 */
export const FileRefButton: React.FC<{ text: string; code?: boolean }> = ({
  text,
  code = false,
}) => {
  const openFileViewer = useConsoleStore((state) => state.openFileViewer);
  return (
    <button
      type="button"
      onClick={() => openFileViewer(text)}
      title={`打开文件：${text}`}
      className={
        code
          ? 'inline break-all rounded-control bg-sunken px-1 py-0.5 font-mono text-[0.85em] text-blue-500 underline decoration-dotted underline-offset-2 hover:text-blue-600'
          : 'inline break-all font-mono text-[0.92em] text-blue-500 underline decoration-dotted underline-offset-2 hover:text-blue-600'
      }
    >
      {text}
    </button>
  );
};
