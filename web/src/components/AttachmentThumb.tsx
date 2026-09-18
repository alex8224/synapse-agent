import React, { useEffect, useState } from 'react';
import { Image20Regular, ImageOff20Regular } from '@fluentui/react-icons';
import { useConsoleStore } from '../stores/useConsoleStore';
import { formatBytes } from '../runtime-client/artifacts.ts';
import { ImageLightbox } from './ImageLightbox.tsx';
import {
  createAttachmentLoader,
  type AttachmentResource,
  type TranscriptAttachment,
} from '../stores/historyAttachments.ts';

/**
 * Resolve one attachment thumbnail through the bounded, generation-guarded
 * loader.
 *
 * The bytes are read on demand (never carried in the history projection) and
 * turned into an object URL by the browser.  The effect key is the session plus
 * the attachment id, so a session switch re-runs it: the previous loader is
 * disposed (revoking its URL) and a late read from the old generation is
 * discarded instead of leaking a blob or overwriting the new thumbnails.
 */
function useAttachmentResource(attachment: TranscriptAttachment): AttachmentResource | null {
  const client = useConsoleStore((state) => state.client);
  const session = useConsoleStore((state) => state.currentSession);
  // The resolved resource is tagged with the key it belongs to, so a session
  // switch derives back to "loading" during render (no synchronous setState in
  // the effect) and never shows the previous session's blob URL.
  const [resolved, setResolved] = useState<{ key: string; resource: AttachmentResource } | null>(
    null,
  );
  const key = `${session.project_id}:${session.thread_id}:${attachment.attachmentId}`;

  useEffect(() => {
    if (!client) return;
    const loader = createAttachmentLoader({
      read: client,
      urls: {
        create: (bytes, mime) => URL.createObjectURL(new Blob([bytes.slice()], { type: mime })),
        revoke: (url) => URL.revokeObjectURL(url),
      },
    });
    let active = true;
    void loader.load(attachment, session).then((next) => {
      if (!active) return;
      setResolved({ key, resource: next });
    });
    // Disposing revokes every URL this loader created, so unmount and a session
    // change both release the blob; a read that lands afterwards is discarded by
    // the loader's generation guard and never reaches the DOM.
    return () => {
      active = false;
      loader.dispose();
    };
  }, [client, key, attachment, session]);

  return resolved !== null && resolved.key === key ? resolved.resource : null;
}

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
