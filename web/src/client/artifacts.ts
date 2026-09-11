/**
 * Strict, dependency-free decoders for the workspace artifact wire surface
 * (`runtime.artifacts.stat/list/read`) plus the pure helpers the file panel
 * needs.
 *
 * Every decoder copies a fixed whitelist of fields and throws
 * `MalformedArtifactError` for anything else, so a hostile or newer peer can
 * never smuggle extra keys into React state.  `data_base64` is passed through
 * untouched: turning bytes into text is a display concern (`decodeBase64Text`),
 * never a transport one.  Pure formatting/decoding lives here so it is exercised
 * directly with the Node test runner.
 */

/** One chunk of a read never asks for more than this (the server caps at 1 MiB). */
export const ARTIFACT_CHUNK_BYTES = 64 * 1024;
/** Hard cap on the bytes held for one file, so a huge file is never fully loaded. */
export const ARTIFACT_MAX_LOADED_BYTES = 256 * 1024;
/** One list page (the server accepts at most 1000). */
export const ARTIFACT_LIST_LIMIT = 200;

export class MalformedArtifactError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'MalformedArtifactError';
  }
}

export interface ArtifactEntry {
  path: string;
  kind: 'file' | 'directory';
  size: number;
  modified_at: string | null;
  media_type: string;
  revision: string | null;
}

export interface ArtifactPageView {
  path: string;
  entries: ArtifactEntry[];
  nextCursor: string | null;
}

export interface ArtifactChunkView {
  path: string;
  offset: number;
  /** Verbatim base64 from the wire; decode only when rendering text. */
  data_base64: string;
  byteLength: number;
  nextOffset: number;
  eof: boolean;
  metadata: ArtifactEntry;
}

const KINDS = new Set(['file', 'directory']);

const METADATA_KEYS = [
  'ref',
  'path',
  'kind',
  'size',
  'modified_at',
  'media_type',
  'revision',
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
    throw new MalformedArtifactError(`${label} is not an object`);
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
    throw new MalformedArtifactError(`${label} has an unexpected field set`);
  }
}

function requireString(value: unknown, label: string): string {
  if (typeof value !== 'string') throw new MalformedArtifactError(`${label} must be a string`);
  return value;
}

function optionalString(value: unknown, label: string): string | null {
  if (value === null || value === undefined) return null;
  return requireString(value, label);
}

function requireInt(value: unknown, label: string, minimum = 0): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < minimum) {
    throw new MalformedArtifactError(`${label} must be an integer >= ${minimum}`);
  }
  return value;
}

/** Strict whitelist copy of one artifact metadata projection. */
export function parseArtifactMetadata(payload: unknown): ArtifactEntry {
  const record = asRecord(payload, 'artifact metadata');
  exactKeys(record, METADATA_KEYS, 'artifact metadata');
  const ref = asRecord(record['ref'], 'artifact ref');
  exactKeys(ref, ['session', 'path'], 'artifact ref');
  const path = requireString(record['path'], 'path');
  if (requireString(ref['path'], 'ref.path') !== path) {
    throw new MalformedArtifactError('ref.path must match path');
  }
  const kind = requireString(record['kind'], 'kind');
  if (!KINDS.has(kind)) {
    throw new MalformedArtifactError('kind must be "file" or "directory"');
  }
  return {
    path,
    kind: kind as 'file' | 'directory',
    size: requireInt(record['size'], 'size'),
    modified_at: optionalString(record['modified_at'], 'modified_at'),
    media_type: requireString(record['media_type'], 'media_type'),
    revision: optionalString(record['revision'], 'revision'),
  };
}

/** Strict whitelist copy of one `runtime.artifacts.list` page. */
export function parseArtifactPage(payload: unknown): ArtifactPageView {
  const record = asRecord(payload, 'artifact page');
  exactKeys(record, ['session', 'path', 'entries', 'next_cursor'], 'artifact page');
  const entries = record['entries'];
  if (!Array.isArray(entries)) {
    throw new MalformedArtifactError('entries must be an array');
  }
  return {
    path: requireString(record['path'], 'path'),
    entries: entries.map(parseArtifactMetadata),
    nextCursor: optionalString(record['next_cursor'], 'next_cursor'),
  };
}

/** Strict whitelist copy of one `runtime.artifacts.read` chunk. */
export function parseArtifactChunk(payload: unknown): ArtifactChunkView {
  const record = asRecord(payload, 'artifact chunk');
  exactKeys(record, CHUNK_KEYS, 'artifact chunk');
  const ref = asRecord(record['ref'], 'artifact ref');
  exactKeys(ref, ['session', 'path'], 'artifact ref');
  const eof = record['eof'];
  if (typeof eof !== 'boolean') {
    throw new MalformedArtifactError('eof must be a boolean');
  }
  return {
    path: requireString(ref['path'], 'ref.path'),
    offset: requireInt(record['offset'], 'offset'),
    data_base64: requireString(record['data_base64'], 'data_base64'),
    byteLength: requireInt(record['byte_length'], 'byte_length'),
    nextOffset: requireInt(record['next_offset'], 'next_offset'),
    eof,
    metadata: parseArtifactMetadata(record['metadata']),
  };
}

/** `1024` -> `1.0 KiB`, `1536` -> `1.5 KiB`; bounded to one decimal. */
export function formatBytes(size: number): string {
  const total = Math.max(0, Math.trunc(size));
  if (total < 1024) return `${total} B`;
  const kib = total / 1024;
  if (kib < 1024) return `${kib.toFixed(1)} KiB`;
  const mib = kib / 1024;
  if (mib < 1024) return `${mib.toFixed(1)} MiB`;
  return `${(mib / 1024).toFixed(1)} GiB`;
}

/**
 * Decode one base64 chunk as UTF-8 text.
 *
 * Invalid byte sequences degrade to U+FFFD instead of throwing, and malformed
 * base64 surfaces as a typed error the panel renders (never a silent blank).
 */
export function decodeBase64Text(dataBase64: string): string {
  let binary: string;
  try {
    binary = atob(dataBase64);
  } catch {
    throw new MalformedArtifactError('chunk is not valid base64');
  }
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return new TextDecoder('utf-8', { fatal: false }).decode(bytes);
}

/** Lower-case extension of a POSIX artifact path (`''` when it has none). */
export function extensionOf(path: string): string {
  const name = path.slice(path.lastIndexOf('/') + 1);
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : '';
}

const TEXT_EXTENSIONS = new Set([
  'txt', 'md', 'markdown', 'rst', 'log', 'csv', 'tsv', 'json', 'jsonl', 'yaml', 'yml',
  'toml', 'ini', 'cfg', 'conf', 'env', 'xml', 'html', 'htm', 'css', 'scss', 'js', 'jsx',
  'mjs', 'cjs', 'ts', 'tsx', 'py', 'pyi', 'rs', 'go', 'java', 'kt', 'c', 'h', 'cc', 'cpp',
  'hpp', 'cs', 'rb', 'php', 'sh', 'bash', 'zsh', 'ps1', 'bat', 'cmd', 'sql', 'graphql',
  'diff', 'patch', 'gitignore', 'editorconfig', 'lock',
]);

/**
 * Whether a chunk of this artifact can be shown as text.
 *
 * `media_type` is authoritative when the server reports a text type; the
 * extension is only a fallback for the `application/octet-stream` default, so a
 * binary artifact is never decoded into mojibake.
 */
export function isTextArtifact(mediaType: string, path: string): boolean {
  const type = mediaType.toLowerCase();
  if (type.startsWith('text/')) return true;
  if (
    type === 'application/json' ||
    type === 'application/xml' ||
    type === 'application/x-yaml' ||
    type === 'application/toml' ||
    type === 'application/javascript'
  ) {
    return true;
  }
  if (type !== 'application/octet-stream' && type !== '') return false;
  return TEXT_EXTENSIONS.has(extensionOf(path));
}

/** Highlight language for the viewer; diff mode colors added/removed lines. */
export function artifactLanguage(path: string, diffMode: boolean): string {
  return diffMode ? 'diff' : extensionOf(path);
}

/** Child path of `base` (`.` is the workspace root). */
export function joinArtifactPath(base: string, name: string): string {
  return base === '.' || base === '' ? name : `${base}/${name}`;
}

/** Parent of a POSIX artifact path; the root's parent is the root itself. */
export function parentArtifactPath(path: string): string {
  const cut = path.lastIndexOf('/');
  return cut <= 0 ? '.' : path.slice(0, cut);
}
