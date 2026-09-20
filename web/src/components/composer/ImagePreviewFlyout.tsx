import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Portal } from '../Portal.tsx';
import { formatBytes } from '../../runtime-client/artifacts.ts';

/**
 * The enlarged copy of one composer image.
 *
 * It is portalled and positioned against the *thumbnail's* rectangle rather than
 * the pill's: the pill is one line tall inside a scrolling editor, and anchoring
 * to it put the preview either behind the composer's own acrylic or a screen
 * away from the image it describes.  It sits 8px above the thumbnail by default
 * and flips below only when the viewport has no room above, and it follows the
 * thumbnail on scroll/resize so a scrolled editor never leaves it behind.
 *
 * Size is the point of the preview, so the box takes what the viewport allows
 * (up to 92vw wide and 70vh tall) and the image is scaled to it in both
 * directions: a wide screenshot is no longer squeezed into a 21rem strip (a
 * 1900x88 capture used to render 336x15), and a small image is enlarged instead
 * of being shown at a size that cannot be read — but never past `MAX_UPSCALE`,
 * because past that the preview is only blur.
 */
/** Width a small image is enlarged towards (when the source allows it). */
const ENLARGE_TO_WIDTH = 320;
/** Ceiling on that enlargement: 2x, so a 80x40 capture reads at 160x80. */
const MAX_UPSCALE = 2;

export const ImagePreviewFlyout: React.FC<{
  anchor: HTMLElement | null;
  url: string | null;
  /** No `url` yet because the by-id bytes are still being read. */
  pending?: boolean;
  /** No `url` because the read failed (or the type is not previewable). */
  failed?: boolean;
  label: string;
  mime: string;
  size: number;
}> = ({ anchor, url, pending = false, failed = false, label, mime, size }) => {
  const boxRef = useRef<HTMLDivElement | null>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);
  const [position, setPosition] = useState<{ top: number; left: number } | null>(null);

  /**
   * Size the image: natural size by default, enlarged towards
   * `ENLARGE_TO_WIDTH` when it is smaller, and never past `MAX_UPSCALE` times
   * its own pixels.  The classes cap the box (92vw / 70vh) from the other side.
   */
  const fitImage = useCallback((): void => {
    const img = imgRef.current;
    if (img === null || img.naturalWidth === 0) return;
    const natural = img.naturalWidth;
    img.style.width = `${Math.max(natural, Math.min(ENLARGE_TO_WIDTH, natural * MAX_UPSCALE))}px`;
  }, []);

  useEffect(() => {
    if (anchor === null) {
      setPosition(null);
      return;
    }
    const place = (): void => {
      if (!anchor.isConnected) {
        setPosition(null);
        return;
      }
      const box = boxRef.current;
      const rect = anchor.getBoundingClientRect();
      const width = box?.offsetWidth ?? 360;
      const height = box?.offsetHeight ?? 220;
      const gap = 8;
      const margin = 12;

      let top = rect.top - height - gap;
      // Only flip when there is genuinely no room above; a short window would
      // otherwise hide the preview behind the composer.
      if (top < margin) top = rect.bottom + gap;
      let left = rect.left + rect.width / 2 - width / 2;
      if (left < margin) left = margin;
      if (left + width > window.innerWidth - margin) {
        left = window.innerWidth - width - margin;
      }
      setPosition({ top: Math.round(top), left: Math.round(left) });
    };
    place();
    // Only a real image can be sized; a loading/error placeholder is placed too.
    if (url !== null) fitImage();
    // The image's own load changes the box height, so place again once it lands.
    const raf = window.requestAnimationFrame(place);
    window.addEventListener('scroll', place, true);
    window.addEventListener('resize', place);
    return () => {
      window.cancelAnimationFrame(raf);
      window.removeEventListener('scroll', place, true);
      window.removeEventListener('resize', place);
    };
  }, [anchor, url, fitImage]);

  // Nothing to show: no anchor yet, no URL, and neither a pending nor a failed
  // read to explain the gap.
  if (position === null || (url === null && !pending && !failed)) return null;

  return (
    <Portal>
      <div
        ref={boxRef}
        aria-hidden="true"
        style={{ top: position.top, left: position.left }}
        className="pointer-events-none fixed z-50 w-max max-w-[94vw] rounded-card border border-line/80 material-flyout flyout-in p-2 shadow-flyout"
      >
        {url !== null ? (
          <img
            ref={imgRef}
            src={url}
            alt={label}
            onLoad={fitImage}
            data-preview-image=""
            className="max-h-[70vh] max-w-[92vw] rounded-control bg-sunken object-contain"
          />
        ) : (
          <div className="flex min-h-16 min-w-32 items-center justify-center rounded-control bg-sunken px-4 py-3 text-[11px] text-gray-500">
            {failed ? '无法预览该图片' : '正在加载预览…'}
          </div>
        )}
        <div className="mt-1.5 flex items-center gap-2 font-mono text-[10px]">
          <span className="max-w-[14rem] truncate text-gray-800">{label}</span>
          <span className="shrink-0 text-gray-400">
            {mime} · {formatBytes(size)}
          </span>
        </div>
      </div>
    </Portal>
  );
};
