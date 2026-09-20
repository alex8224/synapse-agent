import React, { useState } from 'react';
import { Image20Regular, ImageOff20Regular } from '@fluentui/react-icons';
import { formatBytes } from '../runtime-client/artifacts.ts';
import { ImageLightbox } from './ImageLightbox.tsx';
import { useAttachmentResource } from './useAttachmentResource.ts';
import type { TranscriptAttachment } from '../stores/historyAttachments.ts';

/**
 * One read-only thumbnail of a user turn's image attachment.
 *
 * The image keeps its aspect ratio (`object-contain`, bounded) instead of being
 * cropped into a square: a wide screenshot is unreadable once cropped, and the
 * whole point of the thumbnail is to show *which* image was sent.  Clicking it
 * opens the same blob full size in `ImageLightbox`.
 */
export const AttachmentThumb: React.FC<{ attachment: TranscriptAttachment }> = ({
  attachment,
}) => {
  const resource = useAttachmentResource(attachment);
  const [open, setOpen] = useState(false);
  const label = attachment.name || `image#${attachment.imageId ?? '?'}`;
  const meta = `${attachment.mime} · ${formatBytes(attachment.size)}`;
  const title = `${label} · ${meta}`;
  const ready = resource !== null && resource.status === 'ready' ? resource : null;

  return (
    <>
      {ready !== null ? (
        <button
          type="button"
          onClick={() => setOpen(true)}
          title={`${title}（点击放大）`}
          className="block cursor-zoom-in overflow-hidden rounded-control border border-line bg-sunken transition-colors hover:border-gray-300"
        >
          <img
            src={ready.url}
            alt={label}
            className="min-h-16 min-w-16 max-h-40 max-w-[22rem] object-contain"
          />
        </button>
      ) : (
        <div
          className="flex h-16 w-16 items-center justify-center rounded-control border border-line bg-sunken"
          title={title}
        >
          {resource?.status === 'error' || resource?.status === 'unsupported' ? (
            <ImageOff20Regular aria-hidden="true" className="text-gray-400" style={{ fontSize: '18px' }} />
          ) : (
            <Image20Regular aria-hidden="true" className="text-gray-400" style={{ fontSize: '18px' }} />
          )}
        </div>
      )}
      {open && ready !== null && (
        <ImageLightbox src={ready.url} label={label} meta={meta} onClose={() => setOpen(false)} />
      )}
    </>
  );
};
