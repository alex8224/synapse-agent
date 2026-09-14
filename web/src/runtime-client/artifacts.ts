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
 * directly with the Node test runner.  The bounded image read and the
 * generation-guarded image loader are pure in the same sense: the object-URL
 * lifecycle is injected (`ArtifactUrlFactory`), so the loader is exercised with
 * a fake factory and never reaches for `URL` / `Blob` itself.
 */
import type { ArtifactChunk, ArtifactMetadata, ArtifactPage } from './contract.generated.ts';
import type { SessionRef } from './contract.generated.ts';

/** One chunk of a read never asks for more than this (the server caps at 1 MiB). */
export const ARTIFACT_CHUNK_BYTES = 64 * 1024;
/** Hard cap on the bytes held for one file, so a huge file is never fully loaded. */
export const ARTIFACT_MAX_LOADED_BYTES = 256 * 1024;
/**
 * Absolute ceiling for one file, even with explicit "continue reading" clicks.
 * Past the soft cap every additional chunk needs a user action, and past this
 * ceiling the panel refuses with a visible reason instead of growing forever.
 */
export const ARTIFACT_HARD_MAX_BYTES = 4 * 1024 * 1024;
/** One list page (the server accepts at most 1000). */
export const ARTIFACT_LIST_LIMIT = 200;

export class MalformedArtifactError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'MalformedArtifactError';
  }
}

/**
 * One artifact metadata row as the file panel renders it.
 *
 * Derived from the generated wire `ArtifactMetadata`: the opaque `ref` is
 * dropped (the decoder already proves `ref.path` matches `path`) and `kind` is
 * narrowed to the two kinds the panel understands.
 */
export type ArtifactEntry = Omit<ArtifactMetadata, 'ref' | 'kind'> & {
  kind: 'file' | 'directory';
};

/** One `runtime.artifacts.list` page as the panel holds it (cursor renamed). */
export type ArtifactPageView = Omit<ArtifactPage, 'session' | 'entries' | 'next_cursor'> & {
  entries: ArtifactEntry[];
  nextCursor: string | null;
};

/** One `runtime.artifacts.read` chunk as the panel holds it (`ref` -> `path`). */
export type ArtifactChunkView = Omit<
  ArtifactChunk,
  'ref' | 'byte_length' | 'next_offset' | 'metadata'
> & {
  path: string;
  byteLength: number;
  nextOffset: number;
  metadata: ArtifactEntry;
};

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

/**
 * Case-insensitive path-substring filter over entries that are already loaded.
 *
 * Deliberately local: the panel only ever filters the entries it has paged in, so
 * filtering can never trigger an unbounded directory scan.  The panel states the
 * loaded/total counts next to the filtered list so a match outside the loaded
 * page is never mistaken for "no such file".
 */
export function filterArtifactEntries(entries: ArtifactEntry[], query: string): ArtifactEntry[] {
  const needle = query.trim().toLowerCase();
  if (needle === '') return entries;
  return entries.filter((entry) => entry.path.toLowerCase().includes(needle));
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
 * Decode one base64 chunk into its raw bytes.
 *
 * Malformed base64 surfaces as a typed error the panel renders, never as a
 * silently truncated payload -- the image preview would otherwise show a
 * half-decoded file with no explanation.
 */
export function decodeBase64Bytes(dataBase64: string): Uint8Array {
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
  return bytes;
}

/**
 * Decode one base64 chunk as UTF-8 text.
 *
 * Invalid byte sequences degrade to U+FFFD instead of throwing, and malformed
 * base64 surfaces as a typed error the panel renders (never a silent blank).
 */
export function decodeBase64Text(dataBase64: string): string {
  const bytes = decodeBase64Bytes(dataBase64);
  return new TextDecoder('utf-8', { fatal: false }).decode(bytes);
}

/** Lower-case basename of a POSIX artifact path. */
export function basenameOf(path: string): string {
  return path.slice(path.lastIndexOf('/') + 1);
}

/**
 * Lower-case extension of a POSIX artifact path (`''` when it has none).
 *
 * A dotfile is its own extension (`.gitignore` -> `gitignore`).  The server
 * reports `application/octet-stream` for every name it cannot map to a MIME
 * type, which includes every dotfile, so without this branch the text fallback
 * below could never recognise `.gitignore` / `.dockerignore` and would refuse
 * them as binary.
 */
export function extensionOf(path: string): string {
  const name = basenameOf(path);
  const dot = name.lastIndexOf('.');
  if (dot < 0) return '';
  if (dot === 0) return name.slice(1).toLowerCase();
  return name.slice(dot + 1).toLowerCase();
}

const TEXT_EXTENSIONS = new Set([
  'txt', 'md', 'markdown', 'rst', 'log', 'csv', 'tsv', 'json', 'jsonl', 'yaml', 'yml',
  'toml', 'ini', 'cfg', 'conf', 'env', 'xml', 'html', 'htm', 'css', 'scss', 'js', 'jsx',
  'mjs', 'cjs', 'ts', 'tsx', 'py', 'pyi', 'rs', 'go', 'java', 'kt', 'c', 'h', 'cc', 'cpp',
  'hpp', 'cs', 'rb', 'php', 'sh', 'bash', 'zsh', 'ps1', 'bat', 'cmd', 'sql', 'graphql',
  'diff', 'patch', 'gitignore', 'gitattributes', 'gitmodules', 'editorconfig', 'lock',
  'dockerignore', 'npmignore', 'prettierrc', 'eslintrc', 'babelrc', 'nvmrc', 'python-version',
]);

/**
 * Extension-less text files, matched by lower-case basename.
 *
 * These carry no extension at all, so `extensionOf` yields `''` and the
 * extension set above cannot decide them.  Kept deliberately short: an unknown
 * name stays refused rather than risk decoding a binary into mojibake.
 */
const TEXT_FILENAMES = new Set([
  'license', 'licence', 'notice', 'authors', 'contributors', 'codeowners',
  'readme', 'changelog', 'changes', 'contributing', 'install', 'copying',
  'dockerfile', 'containerfile', 'makefile', 'justfile', 'procfile', 'vagrantfile',
  'gemfile', 'rakefile', 'brewfile', 'cmakelists',
]);

/**
 * Whether a chunk of this artifact can be shown as text.
 *
 * `media_type` is authoritative when the server reports a text type; the
 * extension (then the extension-less basename) is only a fallback for the
 * `application/octet-stream` default, so a binary artifact is never decoded
 * into mojibake.
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
  const extension = extensionOf(path);
  if (extension !== '') return TEXT_EXTENSIONS.has(extension);
  return TEXT_FILENAMES.has(basenameOf(path).toLowerCase());
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

/**
 * User-facing reason for an artifact RPC failure.
 *
 * The wire only carries a generic `message` ("runtime service error"); the
 * machine-readable reason travels separately in `data.service_code`.  Mapping it
 * here turns a path the workspace policy forbids (a `.gitignore`d build output,
 * say) into a sentence the reader can act on, instead of a generic error.
 */
export function artifactErrorMessage(serviceCode: string | null, fallback: string): string {
  switch (serviceCode) {
    case 'artifact_forbidden':
      return '该路径被工作区忽略规则排除（例如 .gitignore 中的构建产物），只读文件管理器无法读取';
    case 'artifact_not_found':
      return '文件不存在（可能已被移动或删除）';
    case 'artifact_unavailable':
      return '工作区当前不可用';
    case 'invalid_artifact_path':
      return '路径无效';
    case 'invalid_artifact_cursor':
      return '分页游标已失效，请重新加载目录';
    case 'artifact_changed':
      return '读取期间文件已变化，请重新读取';
    case 'artifact_overflow':
      return '目录条目过多，已超出单次扫描上限';
    default:
      return fallback;
  }
}

// --- image preview -----------------------------------------------------------

/**
 * Ceiling for one previewed image: 4 MiB.
 *
 * Checked against the stat metadata *before* the first read, so a 40 MB scan is
 * refused with a reason instead of being pulled through the bounded chunk loop
 * only to be thrown away.
 */
export const ARTIFACT_IMAGE_MAX_BYTES = 4 * 1024 * 1024;

/**
 * Raster image types the viewer renders.
 *
 * SVG is deliberately absent.  It is a document that can carry script and
 * external references, and it would be rendered from a blob URL where the
 * console's own sanitizer (`GeneratedHtml`) never sees it, so an `.svg`
 * artifact stays refused as binary like any other non-previewable file.
 */
export const ARTIFACT_IMAGE_MIME: readonly string[] = [
  'image/png',
  'image/jpeg',
  'image/jpg',
  'image/gif',
  'image/webp',
  'image/bmp',
];

const IMAGE_MIME_SET = new Set(ARTIFACT_IMAGE_MIME);

/** Raster extensions accepted when the server reports no usable media type. */
const IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp']);

/**
 * Whether an artifact is a raster image this viewer can preview.
 *
 * `media_type` is authoritative, exactly as in `isTextArtifact`; the extension
 * is only a fallback for the `application/octet-stream` default, so a file the
 * server could not classify is still previewed when its name says `png`.
 */
export function isImageArtifact(mediaType: string, path: string): boolean {
  const type = mediaType.toLowerCase().split(';')[0].trim();
  if (IMAGE_MIME_SET.has(type)) return true;
  if (type !== 'application/octet-stream' && type !== '') return false;
  return IMAGE_EXTENSIONS.has(extensionOf(path));
}

/** How the file viewer presents one artifact. */
export type ArtifactPreviewKind = 'image' | 'markdown' | 'text' | 'binary';

/**
 * Pick the viewer for one artifact: an image preview, a rendered Markdown
 * document, plain highlighted text, or the "binary, not read" refusal.
 */
export function artifactPreviewKind(mediaType: string, path: string): ArtifactPreviewKind {
  if (isImageArtifact(mediaType, path)) return 'image';
  if (!isTextArtifact(mediaType, path)) return 'binary';
  const extension = extensionOf(path);
  return extension === 'md' || extension === 'markdown' ? 'markdown' : 'text';
}

/**
 * Why this image cannot be previewed, or `null` when it can.
 *
 * The ceiling is clamped to the shared 4 MiB, so a caller can tighten it but
 * never raise it.
 */
export function artifactImageRefusal(
  size: number,
  maxBytes: number = ARTIFACT_IMAGE_MAX_BYTES,
): string | null {
  const ceiling = Math.min(maxBytes, ARTIFACT_IMAGE_MAX_BYTES);
  if (!Number.isInteger(size) || size <= 0) return '图片大小无效，无法预览';
  if (size > ceiling) return `图片 ${formatBytes(size)} 超过预览上限 ${formatBytes(ceiling)}`;
  return null;
}

/** The bounded read operation the image helpers need (the client satisfies this). */
export interface ArtifactReadTarget {
  readArtifact(
    session: SessionRef,
    path: string,
    offset: number,
    limit: number,
    expectedRevision: string | null,
  ): Promise<ArtifactChunkView>;
}

/** Options of one `readArtifactImageBytes` call. */
export interface ReadArtifactImageOptions {
  session: SessionRef;
  path: string;
  /** Size from the artifact metadata; the read refuses anything larger up front. */
  size: number;
  /** Revision every chunk must carry (`null` when the server reports none). */
  revision: string | null;
  /** Tighter ceiling for the assembled bytes; never above the shared 4 MiB. */
  maxBytes?: number;
}

function concatArtifactBytes(chunks: readonly Uint8Array[], total: number): Uint8Array {
  const out = new Uint8Array(total);
  let cursor = 0;
  for (const chunk of chunks) {
    out.set(chunk, cursor);
    cursor += chunk.length;
  }
  return out;
}

/**
 * Read one image artifact as bytes, one bounded, validated chunk at a time.
 *
 * Every chunk is checked before its bytes are kept:
 *
 * - `byte_length` must match the decoded payload (a truncated base64 string
 *   would otherwise show as a partially decoded image);
 * - the chunk must belong to the requested path and, when a revision is known,
 *   to the requested revision, so bytes from two revisions can never be
 *   stitched into one picture;
 * - the offset must move forward, so a peer that stops advancing fails closed
 *   instead of looping;
 * - the assembled total may never exceed the ceiling, and the loop is bounded
 *   by the ceiling even if every read returns zero bytes.
 *
 * The result is a plain `Uint8Array`, so this stays DOM-free: the console turns
 * it into an object URL through the injected factory below.
 */
export async function readArtifactImageBytes(
  target: ArtifactReadTarget,
  options: ReadArtifactImageOptions,
): Promise<Uint8Array> {
  const ceiling = Math.min(options.maxBytes ?? ARTIFACT_IMAGE_MAX_BYTES, ARTIFACT_IMAGE_MAX_BYTES);
  if (options.size > ceiling) {
    throw new MalformedArtifactError('image exceeds the client preview ceiling');
  }
  const chunks: Uint8Array[] = [];
  let offset = 0;
  let total = 0;
  const maxChunks = Math.ceil(ceiling / ARTIFACT_CHUNK_BYTES) + 1;
  for (let index = 0; index < maxChunks; index += 1) {
    const chunk = await target.readArtifact(
      options.session,
      options.path,
      offset,
      ARTIFACT_CHUNK_BYTES,
      options.revision,
    );
    if (chunk.path !== options.path) {
      throw new MalformedArtifactError('chunk path does not match the requested artifact');
    }
    if (options.revision !== null && chunk.metadata.revision !== options.revision) {
      throw new MalformedArtifactError('chunk revision does not match the expected revision');
    }
    const bytes = decodeBase64Bytes(chunk.data_base64);
    if (bytes.length !== chunk.byteLength) {
      throw new MalformedArtifactError('chunk byte_length does not match its payload');
    }
    if (bytes.length > 0) {
      chunks.push(bytes);
      total += bytes.length;
    }
    if (total > ceiling) {
      throw new MalformedArtifactError('image exceeds the client preview ceiling');
    }
    if (chunk.eof) return concatArtifactBytes(chunks, total);
    if (chunk.nextOffset <= offset) {
      throw new MalformedArtifactError('read did not advance the chunk offset');
    }
    offset = chunk.nextOffset;
  }
  throw new MalformedArtifactError('image read exceeded its bounded chunk budget');
}

/** Injected object-URL lifecycle (the browser `URL` static pair). */
export interface ArtifactUrlFactory {
  create(bytes: Uint8Array, mime: string): string;
  revoke(url: string): void;
}

/** One resolved image preview. */
export type ArtifactImageResource =
  | { status: 'loading' }
  | { status: 'ready'; url: string }
  | { status: 'refused'; reason: string }
  | { status: 'error'; error: unknown };

export interface ArtifactImageLoader {
  /** Resolve (and cache) the object URL for one image artifact of one session. */
  load(entry: ArtifactEntry, session: SessionRef): Promise<ArtifactImageResource>;
  /** Revoke every created URL and fence any in-flight read (switch / unmount). */
  dispose(): void;
}

export interface ArtifactImageLoaderDeps {
  read: ArtifactReadTarget;
  urls: ArtifactUrlFactory;
  /** Optional tighter ceiling; never above the shared 4 MiB. */
  maxBytes?: number;
}

interface ArtifactImageEntry {
  resource: ArtifactImageResource;
  url: string | null;
  generation: number;
}

/**
 * Create one generation-guarded image loader.
 *
 * A read started in generation *g* may only publish (and only create a URL)
 * while the loader is still in generation *g*.  `dispose()` bumps the
 * generation, revokes every URL it created and clears the cache, so a late
 * chunk read from a file the reader already switched away from is discarded
 * instead of leaking a blob URL or replacing the picture on screen.
 */
export function createArtifactImageLoader(deps: ArtifactImageLoaderDeps): ArtifactImageLoader {
  let generation = 0;
  const entries = new Map<string, ArtifactImageEntry>();

  const dispose = (): void => {
    generation += 1;
    for (const entry of entries.values()) {
      if (entry.url !== null) deps.urls.revoke(entry.url);
    }
    entries.clear();
  };

  const load = async (
    entry: ArtifactEntry,
    session: SessionRef,
  ): Promise<ArtifactImageResource> => {
    // The revision is part of the key: re-reading a file that changed on disk
    // must not serve the previous picture from the cache.
    const key = `${entry.path}@${entry.revision ?? ''}`;
    const cached = entries.get(key);
    if (cached !== undefined && cached.generation === generation) return cached.resource;
    const refusal = artifactImageRefusal(entry.size, deps.maxBytes ?? ARTIFACT_IMAGE_MAX_BYTES);
    if (refusal !== null) {
      const resource: ArtifactImageResource = { status: 'refused', reason: refusal };
      entries.set(key, { resource, url: null, generation });
      return resource;
    }
    const startedAt = generation;
    const loading: ArtifactImageResource = { status: 'loading' };
    entries.set(key, { resource: loading, url: null, generation: startedAt });
    try {
      const bytes = await readArtifactImageBytes(deps.read, {
        session,
        path: entry.path,
        size: entry.size,
        revision: entry.revision,
        maxBytes: deps.maxBytes,
      });
      // Superseded while reading (another file, unmount): never create a URL for
      // a generation that is already gone.
      if (startedAt !== generation) return loading;
      const url = deps.urls.create(bytes, entry.media_type);
      const ready: ArtifactImageResource = { status: 'ready', url };
      entries.set(key, { resource: ready, url, generation: startedAt });
      return ready;
    } catch (error) {
      const failed: ArtifactImageResource = { status: 'error', error };
      if (startedAt === generation) {
        entries.set(key, { resource: failed, url: null, generation: startedAt });
      }
      return failed;
    }
  };

  return { load, dispose };
}
