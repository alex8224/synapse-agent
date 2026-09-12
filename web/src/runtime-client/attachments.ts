/**
 * Pure, DOM-free helpers for the session-scoped image attachment wire surface
 * (`runtime.attachments.begin/append/finish/abort/stat/read`).
 *
 * Images never travel inline through a transport frame: the client declares a
 * size and MIME type, streams bounded base64 chunks, finalizes the upload, and
 * afterwards references the opaque attachment id.  This module owns the client
 * half of that contract — the frozen limits, the strict decoders, the bounded
 * `Uint8Array` chunk upload and the bounded read — and nothing else.
 *
 * Host independence: the base64 codec is implemented here over `Uint8Array`
 * so the shared protocol core never reaches for the DOM (`atob` / `btoa` /
 * `Blob`) or the Node `Buffer`.  Turning bytes into a displayable URL stays a
 * browser concern (`URL.createObjectURL`), and the console injects that through
 * the store's attachment loader.
 */
import type {
  AbortAttachmentResult,
  AppendAttachmentChunkResult,
  AttachmentChunk,
  AttachmentMetadata,
  BeginAttachmentResult,
  FinishAttachmentResult,
  SessionRef,
} from './contract.generated.ts';

/** One image may not exceed 4 MB (mirrors `MAX_ATTACHMENT_BYTES`). */
export const ATTACHMENT_MAX_BYTES = 4_000_000;
/** Per-submit image budget (mirrors `MAX_ATTACHMENTS_PER_SUBMIT`). */
export const ATTACHMENT_MAX_COUNT = 8;
/** One upload chunk decodes to at most 256 KiB (mirrors `MAX_CHUNK_BYTES`). */
export const ATTACHMENT_CHUNK_BYTES = 256 * 1024;
/** Bounded read window for a finalized attachment (mirrors `DEFAULT_READ_BYTES`). */
export const ATTACHMENT_READ_BYTES = 64 * 1024;

/**
 * MIME types the runtime accepts (mirrors `IMAGE_MIME_ALLOWED`).
 *
 * The console never guesses a type from the file extension: the browser's own
 * `File.type` is what the server verifies against the decoded payload, so an
 * empty or non-image type is refused before any byte is uploaded.
 */
export const ATTACHMENT_ALLOWED_MIME: readonly string[] = [
  'image/png',
  'image/jpeg',
  'image/jpg',
  'image/webp',
  'image/gif',
  'image/bmp',
];

const ALLOWED_MIME_SET = new Set(ATTACHMENT_ALLOWED_MIME);

/** A wire projection the decoders rejected (never a raw payload). */
export class MalformedAttachmentError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'MalformedAttachmentError';
  }
}

/** A candidate refused locally, before any byte is uploaded. */
export class AttachmentLocalValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'AttachmentLocalValidationError';
  }
}

/** A transfer the caller cancelled; the partial upload was aborted best-effort. */
export class AttachmentUploadCancelledError extends Error {
  constructor(message = 'attachment upload cancelled') {
    super(message);
    this.name = 'AttachmentUploadCancelledError';
  }
}

/** One durable attachment metadata row as the UI holds it (`ref` flattened). */
export type AttachmentEntry = Omit<AttachmentMetadata, 'ref'> & { attachmentId: string };

/** One `runtime.attachments.read` chunk as the UI holds it (`ref` flattened). */
export type AttachmentChunkView = Omit<
  AttachmentChunk,
  'ref' | 'byte_length' | 'next_offset' | 'metadata'
> & {
  attachmentId: string;
  byteLength: number;
  nextOffset: number;
  metadata: AttachmentEntry;
};

/** One `runtime.attachments.begin` result (`ref` flattened). */
export type BeginAttachmentView = Omit<BeginAttachmentResult, 'ref'> & { attachmentId: string };

/** One `runtime.attachments.finish` result (`ref` flattened). */
export type FinishAttachmentView = Omit<FinishAttachmentResult, 'ref'> & { attachmentId: string };

/** The bounded transfer operations the helpers below need (the client satisfies this). */
export interface AttachmentTransferTarget {
  beginAttachment(
    session: SessionRef,
    size: number,
    mime: string,
    displayName?: string,
  ): Promise<BeginAttachmentView>;
  appendAttachmentChunk(
    session: SessionRef,
    attachmentId: string,
    expectedOffset: number,
    dataBase64: string,
  ): Promise<AppendAttachmentChunkResult>;
  finishAttachment(
    session: SessionRef,
    attachmentId: string,
    expectedSize: number,
    expectedMime: string,
  ): Promise<FinishAttachmentView>;
  abortAttachment(session: SessionRef, attachmentId: string): Promise<AbortAttachmentResult>;
}

/** The bounded read operation the helpers below need (the client satisfies this). */
export interface AttachmentReadTarget {
  readAttachment(
    session: SessionRef,
    attachmentId: string,
    offset?: number,
    limit?: number,
  ): Promise<AttachmentChunkView>;
}

/** The structural shape of a picked/dropped file the composer can upload. */
export interface AttachmentUploadSource {
  name: string;
  type: string;
  size: number;
  arrayBuffer(): Promise<ArrayBuffer>;
}

/** One locally validated upload candidate. */
export interface AttachmentCandidate {
  name: string;
  mime: string;
  size: number;
}

// --- base64 codec (DOM-free, Buffer-free) ------------------------------------

const B64_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';

const B64_LOOKUP = (() => {
  const table = new Int16Array(128).fill(-1);
  for (let index = 0; index < B64_ALPHABET.length; index += 1) {
    table[B64_ALPHABET.charCodeAt(index)] = index;
  }
  // `=` is padding: it is only legal in the last group and never a data value.
  table[61] = -2;
  return table;
})();

/** Encode one byte range as standard base64 (no line breaks, padded). */
export function encodeBase64(bytes: Uint8Array): string {
  let out = '';
  const length = bytes.length;
  let index = 0;
  for (; index + 2 < length; index += 3) {
    const n = (bytes[index] << 16) | (bytes[index + 1] << 8) | bytes[index + 2];
    out +=
      B64_ALPHABET[(n >> 18) & 63] +
      B64_ALPHABET[(n >> 12) & 63] +
      B64_ALPHABET[(n >> 6) & 63] +
      B64_ALPHABET[n & 63];
  }
  const remainder = length - index;
  if (remainder === 1) {
    const n = bytes[index] << 16;
    out += B64_ALPHABET[(n >> 18) & 63] + B64_ALPHABET[(n >> 12) & 63] + '==';
  } else if (remainder === 2) {
    const n = (bytes[index] << 16) | (bytes[index + 1] << 8);
    out +=
      B64_ALPHABET[(n >> 18) & 63] +
      B64_ALPHABET[(n >> 12) & 63] +
      B64_ALPHABET[(n >> 6) & 63] +
      '=';
  }
  return out;
}

function decodeBase64Char(code: number): number {
  if (code > 127) throw new MalformedAttachmentError('base64 payload has an invalid character');
  const value = B64_LOOKUP[code];
  if (value < 0) throw new MalformedAttachmentError('base64 payload has an invalid character');
  return value;
}

/**
 * Decode one standard base64 string into bytes.
 *
 * Strict on purpose: a length that is not a multiple of four, a stray `=` in a
 * non-final group, or any non-alphabet character throws a typed error instead
 * of silently producing a truncated image.
 */
export function decodeBase64(text: string): Uint8Array {
  if (text.length % 4 !== 0) {
    throw new MalformedAttachmentError('base64 length must be a multiple of four');
  }
  let padding = 0;
  if (text.endsWith('==')) padding = 2;
  else if (text.endsWith('=')) padding = 1;
  const outLength = (text.length / 4) * 3 - padding;
  const out = new Uint8Array(outLength);
  let cursor = 0;
  for (let index = 0; index < text.length; index += 4) {
    const last = index + 4 === text.length;
    const c0 = decodeBase64Char(text.charCodeAt(index));
    const c1 = decodeBase64Char(text.charCodeAt(index + 1));
    // `==` pads the last two lanes, `=` only the last one; a padded lane is
    // zeroed (its bits are not data), a real lane is decoded strictly.
    const c2 = last && padding === 2 ? 0 : decodeBase64Char(text.charCodeAt(index + 2));
    const c3 = last && padding >= 1 ? 0 : decodeBase64Char(text.charCodeAt(index + 3));
    const n = (c0 << 18) | (c1 << 12) | (c2 << 6) | c3;
    if (cursor < outLength) out[cursor++] = (n >> 16) & 255;
    if (cursor < outLength) out[cursor++] = (n >> 8) & 255;
    if (cursor < outLength) out[cursor++] = n & 255;
  }
  return out;
}

// --- local candidate validation ----------------------------------------------

/** Whether a MIME type is one of the image types the runtime accepts. */
export function isAllowedAttachmentMime(mime: string): boolean {
  const normalized = (mime || '').split(';')[0].trim().toLowerCase();
  return ALLOWED_MIME_SET.has(normalized);
}

/** Why a candidate cannot be uploaded, or `null` when it is acceptable. */
export function validateAttachmentCandidate(size: number, mime: string): string | null {
  if (!isAllowedAttachmentMime(mime)) {
    return `不支持的图片类型（${mime || '未知'}）`;
  }
  if (!Number.isFinite(size) || size <= 0) return '文件为空';
  if (size > ATTACHMENT_MAX_BYTES) {
    return `图片超过上限（${size} > ${ATTACHMENT_MAX_BYTES} 字节）`;
  }
  return null;
}

/**
 * Filter picked/dropped files against the per-submit count, type and size
 * limits.  Every refusal carries a visible reason; nothing is silently dropped.
 *
 * Generic over the candidate so a caller may carry its own payload (the picked
 * `File` handle) alongside the validated `name` / `mime` / `size`.
 */
export function selectAttachmentCandidates<T extends AttachmentCandidate>(
  existingCount: number,
  candidates: readonly T[],
): { accepted: T[]; errors: string[] } {
  const accepted: T[] = [];
  const errors: string[] = [];
  let count = Math.max(0, Math.trunc(existingCount));
  for (const candidate of candidates) {
    if (count >= ATTACHMENT_MAX_COUNT) {
      errors.push(`${candidate.name}：最多 ${ATTACHMENT_MAX_COUNT} 张图片`);
      continue;
    }
    const problem = validateAttachmentCandidate(candidate.size, candidate.mime);
    if (problem !== null) {
      errors.push(`${candidate.name}：${problem}`);
      continue;
    }
    accepted.push(candidate);
    count += 1;
  }
  return { accepted, errors };
}

/** Read one upload source into bytes (the browser `File` satisfies the shape). */
export async function readUploadSource(source: AttachmentUploadSource): Promise<Uint8Array> {
  return new Uint8Array(await source.arrayBuffer());
}

// --- strict wire decoders -----------------------------------------------------

const METADATA_KEYS = [
  'ref',
  'size',
  'mime',
  'revision',
  'display_name',
  'created_at',
  'finalized',
] as const;

const CHUNK_KEYS = [
  'ref',
  'offset',
  'data_base64',
  'byte_length',
  'next_offset',
  'eof',
  'metadata',
] as const;

function asRecord(value: unknown, label: string): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new MalformedAttachmentError(`${label} is not an object`);
  }
  return value as Record<string, unknown>;
}

function exactKeys(
  record: Record<string, unknown>,
  keys: readonly string[],
  label: string,
): void {
  const actual = Object.keys(record);
  if (actual.length !== keys.length || keys.some((key) => !(key in record))) {
    throw new MalformedAttachmentError(`${label} has an unexpected field set`);
  }
}

function requireString(value: unknown, label: string): string {
  if (typeof value !== 'string') throw new MalformedAttachmentError(`${label} must be a string`);
  return value;
}

function optionalString(value: unknown, label: string): string | null {
  if (value === null || value === undefined) return null;
  return requireString(value, label);
}

function requireInt(value: unknown, label: string, minimum = 0): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < minimum) {
    throw new MalformedAttachmentError(`${label} must be an integer >= ${minimum}`);
  }
  return value;
}

function requireBoolean(value: unknown, label: string): boolean {
  if (typeof value !== 'boolean') throw new MalformedAttachmentError(`${label} must be a boolean`);
  return value;
}

/** Opaque attachment id of one `ref` object (`{session, attachment_id}`). */
function requireAttachmentRef(value: unknown, label: string): string {
  const ref = asRecord(value, label);
  exactKeys(ref, ['session', 'attachment_id'], label);
  return requireString(ref['attachment_id'], `${label}.attachment_id`);
}

/** Strict whitelist copy of one attachment metadata projection. */
export function parseAttachmentMetadata(payload: unknown): AttachmentEntry {
  const record = asRecord(payload, 'attachment metadata');
  exactKeys(record, METADATA_KEYS, 'attachment metadata');
  const attachmentId = requireAttachmentRef(record['ref'], 'attachment ref');
  return {
    attachmentId,
    size: requireInt(record['size'], 'size'),
    mime: requireString(record['mime'], 'mime'),
    revision: optionalString(record['revision'], 'revision'),
    display_name: requireString(record['display_name'], 'display_name'),
    created_at: requireString(record['created_at'], 'created_at'),
    finalized: requireBoolean(record['finalized'], 'finalized'),
  };
}

/** Strict whitelist copy of one `runtime.attachments.read` chunk. */
export function parseAttachmentChunk(payload: unknown): AttachmentChunkView {
  const record = asRecord(payload, 'attachment chunk');
  exactKeys(record, CHUNK_KEYS, 'attachment chunk');
  const attachmentId = requireAttachmentRef(record['ref'], 'attachment ref');
  return {
    attachmentId,
    offset: requireInt(record['offset'], 'offset'),
    data_base64: requireString(record['data_base64'], 'data_base64'),
    byteLength: requireInt(record['byte_length'], 'byte_length'),
    nextOffset: requireInt(record['next_offset'], 'next_offset'),
    eof: requireBoolean(record['eof'], 'eof'),
    metadata: parseAttachmentMetadata(record['metadata']),
  };
}

/** Strict whitelist copy of one `runtime.attachments.begin` result. */
export function parseBeginAttachmentResult(payload: unknown): BeginAttachmentView {
  const record = asRecord(payload, 'begin attachment result');
  exactKeys(
    record,
    ['ref', 'chunk_bytes', 'chunk_base64_chars', 'expires_at', 'next_offset'],
    'begin attachment result',
  );
  return {
    attachmentId: requireAttachmentRef(record['ref'], 'attachment ref'),
    chunk_bytes: requireInt(record['chunk_bytes'], 'chunk_bytes'),
    chunk_base64_chars: requireInt(record['chunk_base64_chars'], 'chunk_base64_chars'),
    expires_at: requireString(record['expires_at'], 'expires_at'),
    next_offset: requireInt(record['next_offset'], 'next_offset'),
  };
}

/** Strict whitelist copy of one `runtime.attachments.finish` result. */
export function parseFinishAttachmentResult(payload: unknown): FinishAttachmentView {
  const record = asRecord(payload, 'finish attachment result');
  exactKeys(record, ['ref', 'size', 'mime', 'revision'], 'finish attachment result');
  return {
    attachmentId: requireAttachmentRef(record['ref'], 'attachment ref'),
    size: requireInt(record['size'], 'size'),
    mime: requireString(record['mime'], 'mime'),
    revision: requireString(record['revision'], 'revision'),
  };
}

/** Strict whitelist copy of one `runtime.attachments.append` result. */
export function parseAppendAttachmentChunkResult(payload: unknown): AppendAttachmentChunkResult {
  const record = asRecord(payload, 'append attachment result');
  exactKeys(record, ['ref', 'received_bytes', 'next_offset'], 'append attachment result');
  return {
    ref: {
      session: asRecord(record['ref'], 'attachment ref')['session'] as SessionRef,
      attachment_id: requireAttachmentRef(record['ref'], 'attachment ref'),
    },
    received_bytes: requireInt(record['received_bytes'], 'received_bytes'),
    next_offset: requireInt(record['next_offset'], 'next_offset'),
  };
}

/** Strict whitelist copy of one `runtime.attachments.abort` result. */
export function parseAbortAttachmentResult(payload: unknown): AbortAttachmentResult {
  const record = asRecord(payload, 'abort attachment result');
  exactKeys(record, ['ref', 'removed'], 'abort attachment result');
  return {
    ref: {
      session: asRecord(record['ref'], 'attachment ref')['session'] as SessionRef,
      attachment_id: requireAttachmentRef(record['ref'], 'attachment ref'),
    },
    removed: requireBoolean(record['removed'], 'removed'),
  };
}

// --- bounded transfer helpers -------------------------------------------------

/** Options of one `uploadAttachment` call. */
export interface UploadAttachmentOptions {
  session: SessionRef;
  bytes: Uint8Array;
  mime: string;
  displayName?: string;
  /** Called after every accepted chunk with the byte counts (never a percent). */
  onProgress?: (uploadedBytes: number, totalBytes: number) => void;
  /** Polled between chunks; a `true` cancels and aborts the partial upload. */
  isCancelled?: () => boolean;
}

async function bestEffortAbort(
  target: AttachmentTransferTarget,
  session: SessionRef,
  attachmentId: string,
): Promise<void> {
  try {
    await target.abortAttachment(session, attachmentId);
  } catch {
    // Best effort only: the server sweeps an unfinished upload on its own TTL,
    // so a failed abort must never mask the original transfer error.
  }
}

/**
 * Stream one image through `begin` -> bounded `append` chunks -> `finish`.
 *
 * Never sends the whole payload in one frame: each chunk is at most
 * `ATTACHMENT_CHUNK_BYTES` and the offset comes from the server's own
 * `next_offset`, so an out-of-order or short chunk is surfaced instead of
 * being papered over.  A cancelled or failed transfer aborts its partial
 * upload best-effort and rethrows the original error.
 */
export async function uploadAttachment(
  target: AttachmentTransferTarget,
  options: UploadAttachmentOptions,
): Promise<AttachmentEntry> {
  const { session, bytes, mime, displayName, onProgress, isCancelled } = options;
  const total = bytes.length;
  const problem = validateAttachmentCandidate(total, mime);
  if (problem !== null) throw new AttachmentLocalValidationError(problem);
  const begun = await target.beginAttachment(session, total, mime, displayName ?? '');
  const attachmentId = begun.attachmentId;
  let offset = 0;
  try {
    while (offset < total) {
      if (isCancelled?.()) throw new AttachmentUploadCancelledError();
      const end = Math.min(offset + ATTACHMENT_CHUNK_BYTES, total);
      const chunk = bytes.subarray(offset, end);
      const appended = await target.appendAttachmentChunk(
        session,
        attachmentId,
        offset,
        encodeBase64(chunk),
      );
      if (appended.next_offset <= offset) {
        throw new MalformedAttachmentError('append did not advance the upload offset');
      }
      offset = appended.next_offset;
      onProgress?.(Math.min(offset, total), total);
    }
    if (isCancelled?.()) throw new AttachmentUploadCancelledError();
    const finished = await target.finishAttachment(session, attachmentId, total, mime);
    return {
      attachmentId: finished.attachmentId,
      size: finished.size,
      mime: finished.mime,
      revision: finished.revision,
      display_name: displayName ?? '',
      created_at: '',
      finalized: true,
    };
  } catch (err) {
    await bestEffortAbort(target, session, attachmentId);
    throw err;
  }
}

/** Options of one `readAttachmentBytes` call. */
export interface ReadAttachmentOptions {
  session: SessionRef;
  attachmentId: string;
  /** Absolute ceiling for the assembled bytes (defaults to `ATTACHMENT_MAX_BYTES`). */
  maxBytes?: number;
}

/**
 * Read one finalized attachment as bytes, one bounded window at a time.
 *
 * Bounded by construction: the window is `ATTACHMENT_READ_BYTES`, the assembled
 * total may never exceed `maxBytes` (itself clamped to `ATTACHMENT_MAX_BYTES`),
 * and a server that stops advancing the offset fails closed instead of looping
 * forever.  The result is a plain `Uint8Array`, so this stays DOM-free; the
 * console turns it into an object URL.
 */
export async function readAttachmentBytes(
  target: AttachmentReadTarget,
  options: ReadAttachmentOptions,
): Promise<Uint8Array> {
  const { session, attachmentId } = options;
  const ceiling = Math.min(
    options.maxBytes ?? ATTACHMENT_MAX_BYTES,
    ATTACHMENT_MAX_BYTES,
  );
  const chunks: Uint8Array[] = [];
  let offset = 0;
  let total = 0;
  // Bounded iteration count: even a zero-length read can never spin.
  const maxChunks = Math.ceil(ceiling / ATTACHMENT_READ_BYTES) + 1;
  for (let index = 0; index < maxChunks; index += 1) {
    const chunk = await target.readAttachment(session, attachmentId, offset, ATTACHMENT_READ_BYTES);
    const bytes = decodeBase64(chunk.data_base64);
    if (bytes.length !== chunk.byteLength) {
      throw new MalformedAttachmentError('chunk byte_length does not match its payload');
    }
    if (bytes.length > 0) {
      chunks.push(bytes);
      total += bytes.length;
    }
    if (total > ceiling) {
      throw new MalformedAttachmentError('attachment exceeds the client read ceiling');
    }
    if (chunk.eof) {
      return concatBytes(chunks, total);
    }
    if (chunk.nextOffset <= offset) {
      throw new MalformedAttachmentError('read did not advance the chunk offset');
    }
    offset = chunk.nextOffset;
  }
  throw new MalformedAttachmentError('attachment read exceeded its bounded chunk budget');
}

function concatBytes(chunks: readonly Uint8Array[], total: number): Uint8Array {
  const out = new Uint8Array(total);
  let cursor = 0;
  for (const chunk of chunks) {
    out.set(chunk, cursor);
    cursor += chunk.length;
  }
  return out;
}
