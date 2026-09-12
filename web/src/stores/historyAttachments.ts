/**
 * History-attachment projection and the lazy, bounded blob-URL loader.
 *
 * The durable history projection carries attachment *metadata* only
 * (`HistoryAttachment`: opaque id, `[image#N]` placeholder, name, mime, size,
 * revision) — never base64 bytes.  A user turn's thumbnails are therefore
 * loaded on demand through `runtime.attachments.read`, one bounded window at a
 * time, and turned into a browser object URL.
 *
 * Everything here is deliberately DOM-free except the injected
 * `AttachmentUrlFactory`: the mapping, the size/mime ceiling and the
 * generation-guarded loader are pure, so they run under the Node test runner
 * with a fake URL factory.  The console injects
 * `URL.createObjectURL` / `URL.revokeObjectURL`, and the loader revokes every
 * URL it created on `dispose()` (unmount / session change), so a switched-away
 * session can never leak a blob or publish a stale one.
 */
import type { SessionRef } from '../client/types.ts';
import {
  ATTACHMENT_MAX_BYTES,
  isAllowedAttachmentMime,
  readAttachmentBytes,
} from '../runtime-client/attachments.ts';
import type { AttachmentReadTarget } from '../runtime-client/attachments.ts';

/** One attachment as the transcript renders it (history and live composer alike). */
export interface TranscriptAttachment {
  attachmentId: string;
  /** Per-turn `[image#N]` placeholder; `null` for a live, not-yet-persisted turn. */
  imageId: number | null;
  name: string;
  mime: string;
  size: number;
  revision: string | null;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function asInt(value: unknown): number | null {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null;
}

/**
 * Map one `HistoryEvent.attachments` projection to the render model.
 *
 * A peer that predates the attachments field sends no array at all, which is
 * exactly "no attachments" (`[]`) — never a decode failure and never a fake
 * placeholder.  Individual malformed rows are dropped rather than rendered as
 * a broken thumbnail.
 */
export function mapHistoryAttachments(raw: unknown): TranscriptAttachment[] {
  if (!Array.isArray(raw)) return [];
  const out: TranscriptAttachment[] = [];
  for (const entry of raw) {
    const record = asRecord(entry);
    if (record === null) continue;
    const attachmentId = record['attachment_id'];
    const name = record['name'];
    const mime = record['mime'];
    const size = asInt(record['size']);
    const imageId = asInt(record['image_id']);
    const revision = record['revision'];
    if (typeof attachmentId !== 'string' || attachmentId === '') continue;
    if (typeof name !== 'string' || typeof mime !== 'string') continue;
    if (size === null || imageId === null) continue;
    if (revision !== null && revision !== undefined && typeof revision !== 'string') continue;
    out.push({
      attachmentId,
      imageId,
      name,
      mime,
      size,
      revision: typeof revision === 'string' ? revision : null,
    });
  }
  return out;
}

/**
 * Whether a metadata row may be fetched and displayed at all.
 *
 * The ceiling is the same 4 MB the upload path enforces, and only the image
 * MIME types the runtime accepts are rendered; anything else is refused before
 * a single read is issued.
 */
export function attachmentWithinLimits(attachment: TranscriptAttachment): boolean {
  if (!isAllowedAttachmentMime(attachment.mime)) return false;
  if (!Number.isFinite(attachment.size) || attachment.size <= 0) return false;
  return attachment.size <= ATTACHMENT_MAX_BYTES;
}

/** One resolved thumbnail resource. */
export type AttachmentResource =
  | { status: 'loading' }
  | { status: 'ready'; url: string }
  | { status: 'unsupported'; reason: string }
  | { status: 'error'; reason: string };

/** Injected object-URL lifecycle (the browser `URL` static pair). */
export interface AttachmentUrlFactory {
  create(bytes: Uint8Array, mime: string): string;
  revoke(url: string): void;
}

export interface AttachmentLoader {
  /** Resolve (and cache) the object URL for one attachment of one session. */
  load(attachment: TranscriptAttachment, session: SessionRef): Promise<AttachmentResource>;
  /** Revoke every created URL and fence any in-flight read (unmount / switch). */
  dispose(): void;
}

export interface AttachmentLoaderDeps {
  read: AttachmentReadTarget;
  urls: AttachmentUrlFactory;
  /** Optional tighter ceiling; never above the shared 4 MB upload cap. */
  maxBytes?: number;
}

interface LoaderEntry {
  resource: AttachmentResource;
  url: string | null;
  generation: number;
}

/**
 * Create one generation-guarded attachment loader.
 *
 * A read started in generation *g* may only publish (and only create a URL)
 * while the loader is still in generation *g*.  `dispose()` bumps the
 * generation, revokes every URL it created and clears the cache, so a late
 * chunk read from a switched-away session is discarded instead of leaking a
 * blob URL or overwriting the new session's thumbnails.
 */
export function createAttachmentLoader(deps: AttachmentLoaderDeps): AttachmentLoader {
  let generation = 0;
  const entries = new Map<string, LoaderEntry>();

  const dispose = (): void => {
    generation += 1;
    for (const entry of entries.values()) {
      if (entry.url !== null) deps.urls.revoke(entry.url);
    }
    entries.clear();
  };

  const load = async (
    attachment: TranscriptAttachment,
    session: SessionRef,
  ): Promise<AttachmentResource> => {
    const key = attachment.attachmentId;
    const cached = entries.get(key);
    if (cached !== undefined && cached.generation === generation) {
      return cached.resource;
    }
    if (!attachmentWithinLimits(attachment)) {
      const unsupported: AttachmentResource = {
        status: 'unsupported',
        reason: `附件不可预览（${attachment.mime || '未知类型'} / ${attachment.size} 字节）`,
      };
      entries.set(key, { resource: unsupported, url: null, generation });
      return unsupported;
    }
    const startedAt = generation;
    const loading: AttachmentResource = { status: 'loading' };
    entries.set(key, { resource: loading, url: null, generation: startedAt });
    try {
      const bytes = await readAttachmentBytes(deps.read, {
        session,
        attachmentId: attachment.attachmentId,
        maxBytes: deps.maxBytes,
      });
      // Superseded while reading (unmount / session change): never create a URL
      // for a generation that is already gone.
      if (startedAt !== generation) return loading;
      const url = deps.urls.create(bytes, attachment.mime);
      const ready: AttachmentResource = { status: 'ready', url };
      entries.set(key, { resource: ready, url, generation: startedAt });
      return ready;
    } catch (err) {
      const failed: AttachmentResource = {
        status: 'error',
        reason: err instanceof Error ? err.message : '附件读取失败',
      };
      if (startedAt === generation) {
        entries.set(key, { resource: failed, url: null, generation: startedAt });
      }
      return failed;
    }
  };

  return { load, dispose };
}
