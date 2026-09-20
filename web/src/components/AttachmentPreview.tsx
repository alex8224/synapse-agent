import React, { useEffect, useRef } from 'react';
import { Image20Regular, ImageOff20Regular } from '@fluentui/react-icons';
import type { AttachmentUploadSource } from '../runtime-client/attachments.ts';

/**
 * Pre-submit preview of one composer attachment: the thumbnail itself, sized to
 * sit inside the composer's inline image pill.
 *
 * The composer already holds the picked bytes, so this preview is a local object
 * URL: nothing is read from the daemon and no upload has to finish first, which
 * is what makes it usable as a "did I paste the right screenshot?" check.
 *
 * The URL is created and revoked here — never in the store, which stays DOM-free
 * — and treated as the external resource it is: one URL per blob, revoked when
 * the row goes away, so unmount, a session switch and submitting the turn (all
 * of which drop the row) release the blob instead of leaking it.  No state is
 * involved, so nothing re-renders when the URL arrives.
 *
 * The enlarged copy is *not* rendered here: it is a flyout the composer portals
 * and positions against this thumbnail (`ImagePreviewFlyout`), because the pill
 * lives one line tall inside a scrolling editor and an absolutely positioned
 * copy would be clipped by it.
 */
export const AttachmentPreview: React.FC<{
  /**
   * The local pick, when there is one.  A screenshot row is already finalized
   * and carries no local bytes, so the preview degrades to the image glyph and
   * the pill's by-id preview (`AttachmentThumb`) shows the real picture.
   */
  source?: AttachmentUploadSource;
  label: string;
  /** Carried by the caller for the pill's own tooltip; unused by the thumbnail. */
  mime: string;
  /** Same: the flyout prints it, the thumbnail only shows the picture. */
  size: number;
  failed: boolean;
}> = ({ source, label, failed }) => {
  // A `File`/`Blob` in the browser; an injected double has no preview and keeps
  // the icon instead of throwing.
  const blob = source instanceof Blob ? source : null;
  const thumbRef = useRef<HTMLImageElement | null>(null);
  const urlRef = useRef<string | null>(null);

  useEffect(() => {
    if (blob === null) return;
    urlRef.current ??= URL.createObjectURL(blob);
    if (thumbRef.current !== null) thumbRef.current.src = urlRef.current;
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
      <ImageOff20Regular aria-hidden="true" className="text-red-500" style={{ fontSize: '14px' }} />
    );
  }
  if (blob === null) {
    return (
      <Image20Regular aria-hidden="true" className="text-gray-400" style={{ fontSize: '14px' }} />
    );
  }
  return (
    <img
      ref={thumbRef}
      alt={label}
      title={label}
      // Aspect-preserving on purpose: the point of the preview is to confirm
      // *which* image was pasted, and a square crop hides most of a screenshot.
      className="h-5 w-5 shrink-0 cursor-zoom-in rounded-[3px] border border-line bg-canvas object-contain"
    />
  );
};
