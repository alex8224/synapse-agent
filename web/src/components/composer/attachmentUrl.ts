import { useEffect, useRef, useState } from 'react';
import type { AttachmentUploadSource } from '../../runtime-client/attachments.ts';

/**
 * One object URL per blob, created once and released when the blob changes.
 *
 * The composer already holds the picked bytes, so the pill's thumbnail is a
 * local object URL: nothing is read from the daemon and no upload has to finish
 * first, which is what makes it usable as a "did I paste the right screenshot?"
 * check.  The URL is treated as the external resource it is — created once per
 * blob (never per render, so hovering cannot leak one) and revoked when the row
 * goes away, so unmount, a session switch and submitting the turn all release
 * the blob instead of leaking it.
 *
 * A source that is not a `Blob` (an injected double) yields `null` rather than
 * throwing, and the caller degrades to an icon.
 */
export function useAttachmentObjectUrl(source: AttachmentUploadSource): string | null {
  const blob = source instanceof Blob ? source : null;
  const urlRef = useRef<string | null>(null);
  const [url, setUrl] = useState<string | null>(null);

  useEffect(() => {
    if (blob === null) {
      setUrl(null);
      return;
    }
    urlRef.current ??= URL.createObjectURL(blob);
    setUrl(urlRef.current);
    // Runs after the assignment above on every update (React flushes all
    // cleanups before all effects), so the URL is never revoked while in use.
    return () => {
      if (urlRef.current === null) return;
      URL.revokeObjectURL(urlRef.current);
      urlRef.current = null;
    };
  }, [blob]);

  return url;
}
