/**
 * Resolve one attachment's bytes into a browser object URL, *by id*.
 *
 * The durable history projection carries attachment metadata only, so a row
 * that has no local pick (a screenshot frame, or a reloaded user turn) can only
 * be previewed by reading its bytes back through `runtime.attachments.read`.
 * This is the one hook both the transcript thumbnail (`AttachmentThumb`) and the
 * composer's own by-id thumbnail / hover preview use, so the two paths cannot
 * diverge: the bounded, generation-guarded `createAttachmentLoader` does the
 * read, the browser `URL` pair turns the bytes into an object URL, and the
 * loader revokes every URL it created when it is disposed.
 *
 * Two properties are load-bearing:
 *
 * - **Primitive dependencies.**  The effect keys on the session ids and the
 *   attachment's own scalar fields, never on the descriptor or session *object*
 *   identities.  A parent re-render that hands over a fresh literal (the
 *   composer builds one per render) must not start a second read, so hovering or
 *   typing cannot re-read the same bytes.
 * - **Async generation isolation.**  A read started for one key may only publish
 *   while that key is still current: `dispose()` fences it, and the resolved
 *   resource is tagged with its key, so a session switch derives back to
 *   "loading" during render instead of showing the previous session's blob.
 */
import { useEffect, useState } from 'react';
import { useConsoleStore } from '../stores/useConsoleStore.ts';
import {
  createAttachmentLoader,
  type AttachmentResource,
  type TranscriptAttachment,
} from '../stores/historyAttachments.ts';

/**
 * A descriptor that resolves to nothing, so a caller can invoke the hook
 * unconditionally (a hover preview is only a by-id read when the hovered row has
 * no local blob).
 */
export const NO_ATTACHMENT: TranscriptAttachment = {
  attachmentId: '',
  imageId: null,
  name: '',
  mime: '',
  size: 0,
  revision: null,
};

export function useAttachmentResource(attachment: TranscriptAttachment): AttachmentResource | null {
  const client = useConsoleStore((state) => state.client);
  const projectId = useConsoleStore((state) => state.currentSession.project_id);
  const threadId = useConsoleStore((state) => state.currentSession.thread_id);
  const { attachmentId, imageId, name, mime, size, revision } = attachment;
  // Tag the resolved resource with the key it belongs to, so a session switch
  // derives back to "loading" during render (no synchronous setState in the
  // effect) and never shows the previous session's blob URL.
  const [resolved, setResolved] = useState<{ key: string; resource: AttachmentResource } | null>(
    null,
  );
  const key = `${projectId}:${threadId}:${attachmentId}`;

  useEffect(() => {
    if (!client || attachmentId === '') return;
    const loader = createAttachmentLoader({
      read: client,
      urls: {
        create: (bytes, mime) => URL.createObjectURL(new Blob([bytes.slice()], { type: mime })),
        revoke: (url) => URL.revokeObjectURL(url),
      },
    });
    let active = true;
    void loader
      .load(
        { attachmentId, imageId, name, mime, size, revision },
        { project_id: projectId, thread_id: threadId },
      )
      .then((next) => {
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
    // Primitive deps only: a fresh descriptor object (or session object) must not
    // re-run the read.  `key` already folds in the session, so a switch still
    // disposes the old loader and fences its in-flight read.
  }, [client, key, attachmentId, imageId, name, mime, size, revision, projectId, threadId]);

  return resolved !== null && resolved.key === key ? resolved.resource : null;
}
