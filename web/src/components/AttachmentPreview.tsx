import React, { useEffect, useRef } from 'react';
import { formatBytes } from '../runtime-client/artifacts.ts';
import type { AttachmentUploadSource } from '../runtime-client/attachments.ts';

/**
 * Pre-submit preview of one composer attachment: the chip is the image itself,
 * and hovering it reveals a larger copy.
 *
 * The composer already holds the picked bytes, so this preview is a local object
 * URL: nothing is read from the daemon and no upload has to finish first, which
 * is what makes it usable as a "did I paste the right screenshot?" check.
 *
 * The URL is created and revoked here — never in the store, which stays DOM-free
 * — and treated as the external resource it is: one URL per blob is handed to
 * both `<img>` nodes (chip and enlarged copy) and revoked when the row goes
 * away, so unmount, a session switch and submitting the turn (all of which drop
 * the row) release the blob instead of leaking it.  No state is involved, so
 * nothing re-renders when the URL arrives and the enlarged copy costs no second
 * read.
 */
export const AttachmentPreview: React.FC<{
  source: AttachmentUploadSource;
  label: string;
  mime: string;
  size: number;
  failed: boolean;
}> = ({ source, label, mime, size, failed }) => {
  // A `File`/`Blob` in the browser; an injected double has no preview and keeps
  // the icon instead of throwing.
  const blob = source instanceof Blob ? source : null;
  const thumbRef = useRef<HTMLImageElement | null>(null);
  const zoomRef = useRef<HTMLImageElement | null>(null);
  const urlRef = useRef<string | null>(null);

  useEffect(() => {
    if (blob === null) return;
    urlRef.current ??= URL.createObjectURL(blob);
    const url = urlRef.current;
    if (thumbRef.current !== null) thumbRef.current.src = url;
    if (zoomRef.current !== null) zoomRef.current.src = url;
  }, [blob]);

  // Runs after the assignment above on every update (React flushes all cleanups
  // before all effects), so the URL is never revoked while still in use.
  useEffect(
    () => () => {
      if (urlRef.current === null) return;
      URL.revokeObjectURL(urlRef.current);
      urlRef.current = null;
    },
    [blob],
  );

  if (failed) {
    return (
      <span className="material-symbols-outlined text-[18px] text-red-500">broken_image</span>
    );
  }
  if (blob === null) {
    return <span className="material-symbols-outlined text-[18px] text-gray-400">image</span>;
  }
  return (
    <div className="group relative shrink-0">
      <img
        ref={thumbRef}
        alt={label}
        title={label}
        // Aspect-preserving on purpose: the point of the preview is to confirm
        // *which* image was pasted, and a square crop hides most of a screenshot.
        className="h-12 w-auto min-w-8 max-w-[8rem] rounded border border-gray-200 bg-gray-50 object-contain"
      />
      {/* Hover-only, and rendered up front so the shared URL reaches it: a modal
          on hover would be far more disruptive than a floating copy. */}
      <div className="absolute bottom-full left-0 z-50 mb-2 hidden w-max rounded-card border border-gray-200 material-flyout flyout-in p-2 shadow-flyout group-hover:block">
        <img
          ref={zoomRef}
          alt={label}
          className="max-h-64 max-w-[28rem] rounded object-contain"
        />
        <div className="mt-1.5 flex items-center gap-2 font-mono text-[10px]">
          <span className="max-w-[18rem] truncate text-gray-800">{label}</span>
          <span className="shrink-0 text-gray-400">
            {mime} · {formatBytes(size)}
          </span>
        </div>
      </div>
    </div>
  );
};
