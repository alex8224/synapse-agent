import React, { useEffect } from 'react';
import { Dismiss16Regular } from '@fluentui/react-icons';
import { Portal } from './Portal.tsx';

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
    // `Portal`: the lightbox belongs to the window, not to the transcript row that
    // opened it.
    <Portal>
      <div
        role="dialog"
        aria-modal="true"
        aria-label="图片预览"
        onClick={onClose}
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-md p-6 scrim-in"
      >
      <div
        onClick={(event) => event.stopPropagation()}
        className="flex max-h-full max-w-4xl flex-col rounded-card border border-line/80 material-flyout flyout-in p-3 shadow-flyout"
      >
        <div className="mb-2 flex items-center gap-3 border-b border-gray-100 pb-1.5">
          <span className="truncate font-mono text-[11px] font-semibold text-gray-900">{label}</span>
          <span className="shrink-0 font-mono text-[10px] text-gray-400">{meta}</span>
          <button
            type="button"
            onClick={onClose}
            title="关闭 (Esc)"
            className="ui-icon-button ui-compact ml-auto text-gray-400 hover:text-gray-700"
          >
            <Dismiss16Regular aria-hidden="true" />
          </button>
        </div>
        <img src={src} alt={label} className="max-h-[75vh] max-w-full object-contain" />
      </div>
      </div>
    </Portal>
  );
};
