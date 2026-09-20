/**
 * A small (pill-sized) thumbnail of an attachment that has **no local pick**.
 *
 * A screenshot frame arrives already finalized: the composer holds only its
 * opaque `attachment_id`, so there is no local `File`/`Blob` to build an object
 * URL from.  This component loads the bytes by id through the *same* bounded,
 * generation-guarded loader the history thumbnails use (`createAttachmentLoader`
 * -> `runtime.attachments.read`), so the two preview paths cannot diverge.
 *
 * The hook keys on the attachment's scalar fields, never on the descriptor
 * object: the composer builds a fresh descriptor every render, and a re-render
 * must not start a second read.  The only difference from `AttachmentThumb` is
 * the size it paints — the pill is one line tall, so it stays at a fixed small
 * box and shows a loading glyph / an error glyph instead of a lightbox.
 */
import React from 'react';
import { Image20Regular, ImageOff20Regular } from '@fluentui/react-icons';
import type { TranscriptAttachment } from '../../stores/historyAttachments.ts';
import { useAttachmentResource } from '../useAttachmentResource.ts';

export const AttachmentIdThumb: React.FC<{ attachment: TranscriptAttachment }> = ({ attachment }) => {
  const resource = useAttachmentResource(attachment);
  if (resource !== null && resource.status === 'ready') {
    return (
      <img
        src={resource.url}
        alt={attachment.name}
        title={attachment.name}
        className="h-5 w-5 shrink-0 rounded-[3px] border border-line bg-canvas object-contain"
      />
    );
  }
  if (resource !== null && (resource.status === 'error' || resource.status === 'unsupported')) {
    return <ImageOff20Regular aria-hidden="true" className="text-red-500" style={{ fontSize: '14px' }} />;
  }
  // Still loading (or no client yet): the neutral glyph stands in for the bytes.
  return <Image20Regular aria-hidden="true" className="text-gray-400" style={{ fontSize: '14px' }} />;
};
