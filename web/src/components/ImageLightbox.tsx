import React, { useEffect } from 'react';

/**
 * Full-size view of one transcript image.
 *
 * It renders the object URL the thumbnail already resolved, so opening it costs
 * no extra read and the blob stays owned (and revoked) by the thumbnail's
 * loader.  The overlay is `fixed`, like the goal dialog, so the transcript's
 * scroll container cannot clip it; Escape, the close button and a click on the
 * backdrop all dismiss it, and a click inside the panel does not.
 */
export const ImageLightbox: React.FC<{
  src: string;
  label: string;
  meta: string;
  onClose: () => void;
}> = ({ src, label, meta, onClose }) => {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="图片预览"
      onClick={onClose}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-6"
    >
      <div
        onClick={(event) => event.stopPropagation()}
        className="flex max-h-full max-w-4xl flex-col rounded-lg border border-gray-200 bg-surface p-3 shadow-xl"
      >
        <div className="mb-2 flex items-center gap-3 border-b border-gray-100 pb-1.5">
          <span className="truncate font-mono text-[11px] font-semibold text-gray-900">{label}</span>
          <span className="shrink-0 font-mono text-[10px] text-gray-400">{meta}</span>
          <button
            type="button"
            onClick={onClose}
            title="关闭 (Esc)"
            className="material-symbols-outlined ml-auto cursor-pointer text-[16px] text-gray-400 hover:text-gray-700"
          >
            close
          </button>
        </div>
        <img src={src} alt={label} className="max-h-[75vh] max-w-full object-contain" />
      </div>
    </div>
  );
};
