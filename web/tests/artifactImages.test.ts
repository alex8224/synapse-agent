/**
 * Offline tests for the workspace-image preview helpers.
 *
 * The panel may only ever show a bounded, validated picture, so these pin the
 * whole path that turns a `runtime.artifacts.read` chunk into a blob URL:
 *
 * - the raster whitelist (SVG stays refused) and the 4 MiB ceiling, checked
 *   against the stat metadata *before* the first read;
 * - the sequential chunk loop: `byte_length` must match the decoded payload,
 *   every chunk must belong to the requested path and revision, the offset must
 *   advance, and the assembled total may not exceed the ceiling;
 * - the loader's object-URL lifecycle: one URL per resolved image, revoked on
 *   `dispose()`, and a read that lands after `dispose()` publishes nothing.
 *
 * Everything is injected (a stub read target, a fake URL factory), so no socket
 * and no DOM is involved.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  ARTIFACT_CHUNK_BYTES,
  ARTIFACT_IMAGE_MAX_BYTES,
  ARTIFACT_IMAGE_MIME,
  MalformedArtifactError,
  artifactImageRefusal,
  artifactPreviewKind,
  createArtifactImageLoader,
  isImageArtifact,
  readArtifactImageBytes,
} from '../src/client/artifacts.ts';
import type { ArtifactEntry } from '../src/client/artifacts.ts';
import type { SessionRef } from '../src/runtime-client/types.ts';

const SESSION: SessionRef = { project_id: 'p', thread_id: 't' };
const REVISION = 'rev-1';

function entry(overrides: Partial<ArtifactEntry> = {}): ArtifactEntry {
  return {
    path: 'assets/hero.png',
    kind: 'file',
    size: 6,
    modified_at: null,
    media_type: 'image/png',
    revision: REVISION,
    ...overrides,
  };
}

function base64(bytes: Uint8Array): string {
  return Buffer.from(bytes).toString('base64');
}

interface ReadCall {
  path: string;
  offset: number;
  limit: number;
  revision: string | null;
}

/**
 * A stub read target that serves `bytes` in fixed windows, one chunk per call.
 *
 * `window` is deliberately tiny: every boundary (the last short chunk, the
 * revision carried on each call, the offset walk) is exercised without a
 * megabyte of fixture data.
 */
function stubReader(bytes: Uint8Array, window = 4) {
  const calls: ReadCall[] = [];
  const target = {
    async readArtifact(
      _session: SessionRef,
      path: string,
      offset: number,
      limit: number,
      revision: string | null,
    ) {
      calls.push({ path, offset, limit, revision });
      const slice = bytes.subarray(offset, Math.min(offset + window, bytes.length));
      const nextOffset = offset + slice.length;
      return {
        path,
        offset,
        data_base64: base64(slice),
        byteLength: slice.length,
        nextOffset,
        eof: nextOffset >= bytes.length,
        metadata: entry({ path, size: bytes.length, revision: revision ?? REVISION }),
      };
    },
  };
  return { target, calls };
}

function fakeUrls() {
  const created: Array<{ url: string; bytes: Uint8Array; mime: string }> = [];
  const revoked: string[] = [];
  return {
    created,
    revoked,
    factory: {
      create(bytes: Uint8Array, mime: string): string {
        const url = `blob:fake/${created.length + 1}`;
        created.push({ url, bytes, mime });
        return url;
      },
      revoke(url: string): void {
        revoked.push(url);
      },
    },
  };
}

test('only the raster whitelist is previewed, never SVG', () => {
  assert.deepEqual([...ARTIFACT_IMAGE_MIME], [
    'image/png',
    'image/jpeg',
    'image/jpg',
    'image/gif',
    'image/webp',
    'image/bmp',
  ]);
  for (const mime of ARTIFACT_IMAGE_MIME) {
    assert.equal(isImageArtifact(mime, 'a.bin'), true, mime);
  }
  assert.equal(isImageArtifact('image/svg+xml', 'icon.svg'), false);
  // The media type wins; the extension is only the octet-stream fallback.
  assert.equal(isImageArtifact('text/plain', 'a.png'), false);
  assert.equal(isImageArtifact('application/octet-stream', 'assets/hero.PNG'), true);
  assert.equal(isImageArtifact('application/octet-stream', 'icon.svg'), false);
  assert.equal(isImageArtifact('', 'hero.webp'), true);
  assert.equal(isImageArtifact('', 'notes.txt'), false);
});

test('artifactPreviewKind routes image / markdown / text / binary', () => {
  assert.equal(artifactPreviewKind('image/png', 'a.png'), 'image');
  assert.equal(artifactPreviewKind('application/octet-stream', 'a.gif'), 'image');
  assert.equal(artifactPreviewKind('text/markdown', 'docs/guide.md'), 'markdown');
  assert.equal(artifactPreviewKind('application/octet-stream', 'docs/guide.markdown'), 'markdown');
  assert.equal(artifactPreviewKind('text/x-python', 'src/app.py'), 'text');
  assert.equal(artifactPreviewKind('application/pdf', 'a.pdf'), 'binary');
  // An SVG is a document, not a picture: it stays a binary refusal.
  assert.equal(artifactPreviewKind('image/svg+xml', 'icon.svg'), 'binary');
});

test('an oversized image is refused from its metadata alone', () => {
  assert.equal(artifactImageRefusal(1024), null);
  assert.equal(artifactImageRefusal(ARTIFACT_IMAGE_MAX_BYTES), null);
  assert.match(String(artifactImageRefusal(ARTIFACT_IMAGE_MAX_BYTES + 1)), /超过预览上限/);
  assert.match(String(artifactImageRefusal(0)), /无效/);
  assert.match(String(artifactImageRefusal(12.5)), /无效/);
  // A caller may tighten the ceiling, never raise it.
  assert.match(String(artifactImageRefusal(2048, 1024)), /超过预览上限/);
  assert.equal(artifactImageRefusal(ARTIFACT_IMAGE_MAX_BYTES, ARTIFACT_IMAGE_MAX_BYTES * 4), null);
});

test('an oversized image is rejected before a single read is issued', async () => {
  const calls: ReadCall[] = [];
  const target = {
    async readArtifact() {
      calls.push({ path: 'never', offset: 0, limit: 0, revision: null });
      throw new Error('the read must not happen');
    },
  };
  await assert.rejects(
    readArtifactImageBytes(target, {
      session: SESSION,
      path: 'assets/huge.png',
      size: ARTIFACT_IMAGE_MAX_BYTES + 1,
      revision: REVISION,
    }),
    MalformedArtifactError,
  );
  assert.deepEqual(calls, []);
});

test('an image is assembled from sequential validated chunks', async () => {
  const bytes = new Uint8Array([1, 2, 3, 4, 5, 6, 7, 8, 9]);
  const { target, calls } = stubReader(bytes, 4);
  const read = await readArtifactImageBytes(target, {
    session: SESSION,
    path: 'assets/hero.png',
    size: bytes.length,
    revision: REVISION,
  });
  assert.deepEqual([...read], [...bytes]);
  assert.deepEqual(
    calls.map((call) => call.offset),
    [0, 4, 8],
    'the offsets must walk forward by the bytes actually returned',
  );
  for (const call of calls) {
    assert.equal(call.path, 'assets/hero.png');
    assert.equal(call.revision, REVISION, 'every chunk carries the expected revision');
    assert.equal(call.limit, ARTIFACT_CHUNK_BYTES);
  }
});

test('a chunk whose byte_length disagrees with its payload is rejected', async () => {
  const bytes = new Uint8Array([1, 2, 3, 4]);
  const { target } = stubReader(bytes, 4);
  const lying = {
    async readArtifact() {
      return {
        path: 'assets/hero.png',
        offset: 0,
        data_base64: base64(bytes),
        byteLength: bytes.length + 1,
        nextOffset: bytes.length,
        eof: true,
        metadata: entry({ size: bytes.length }),
      };
    },
  };
  await assert.rejects(
    readArtifactImageBytes(lying, {
      session: SESSION,
      path: 'assets/hero.png',
      size: bytes.length,
      revision: REVISION,
    }),
    /byte_length/,
  );
  // The honest reader still works, so the rejection is about the payload only.
  await readArtifactImageBytes(target, {
    session: SESSION,
    path: 'assets/hero.png',
    size: bytes.length,
    revision: REVISION,
  });
});

test('bytes from another revision or path are never stitched in', async () => {
  const bytes = new Uint8Array([1, 2, 3, 4]);
  const drifted = (overrides: Record<string, unknown>) => ({
    async readArtifact() {
      return {
        path: 'assets/hero.png',
        offset: 0,
        data_base64: base64(bytes),
        byteLength: bytes.length,
        nextOffset: bytes.length,
        eof: true,
        metadata: entry({ size: bytes.length }),
        ...overrides,
      };
    },
  });
  await assert.rejects(
    readArtifactImageBytes(drifted({ path: 'assets/other.png' }), {
      session: SESSION,
      path: 'assets/hero.png',
      size: bytes.length,
      revision: REVISION,
    }),
    /path/,
  );
  await assert.rejects(
    readArtifactImageBytes(
      drifted({ metadata: entry({ size: bytes.length, revision: 'rev-2' }) }),
      { session: SESSION, path: 'assets/hero.png', size: bytes.length, revision: REVISION },
    ),
    /revision/,
  );
});

test('a read that stops advancing fails closed instead of looping', async () => {
  const bytes = new Uint8Array([1, 2, 3, 4]);
  const stuck = {
    async readArtifact() {
      return {
        path: 'assets/hero.png',
        offset: 0,
        data_base64: '',
        byteLength: 0,
        nextOffset: 0,
        eof: false,
        metadata: entry({ size: bytes.length }),
      };
    },
  };
  await assert.rejects(
    readArtifactImageBytes(stuck, {
      session: SESSION,
      path: 'assets/hero.png',
      size: bytes.length,
      revision: REVISION,
    }),
    /advance/,
  );
});

test('the assembled bytes may never exceed the ceiling', async () => {
  const window = new Uint8Array(ARTIFACT_CHUNK_BYTES).fill(9);
  const endless = {
    async readArtifact(_session: SessionRef, path: string, offset: number) {
      return {
        path,
        offset,
        data_base64: base64(window),
        byteLength: window.length,
        nextOffset: offset + window.length,
        eof: false,
        metadata: entry({ size: window.length, revision: REVISION }),
      };
    },
  };
  await assert.rejects(
    readArtifactImageBytes(endless, {
      session: SESSION,
      path: 'assets/hero.png',
      size: window.length,
      revision: REVISION,
      maxBytes: window.length,
    }),
    /ceiling/,
  );
});

test('the chunk budget bounds even a read that advances forever', async () => {
  // One byte per call and an offset that keeps moving: the ceiling is never
  // crossed, so the iteration budget is what has to stop it.
  const drip = {
    async readArtifact(_session: SessionRef, path: string, offset: number) {
      return {
        path,
        offset,
        data_base64: base64(new Uint8Array([7])),
        byteLength: 1,
        nextOffset: offset + 1,
        eof: false,
        metadata: entry({ size: 4, revision: REVISION }),
      };
    },
  };
  await assert.rejects(
    readArtifactImageBytes(drip, {
      session: SESSION,
      path: 'assets/hero.png',
      size: 4,
      revision: REVISION,
      maxBytes: 4,
    }),
    /budget/,
  );
});

test('the loader creates one URL, caches it, and revokes it on dispose', async () => {
  const bytes = new Uint8Array([1, 2, 3, 4, 5, 6]);
  const { target } = stubReader(bytes, 4);
  const urls = fakeUrls();
  const loader = createArtifactImageLoader({ read: target, urls: urls.factory });
  const first = await loader.load(entry(), SESSION);
  assert.equal(first.status, 'ready');
  assert.equal(urls.created.length, 1);
  assert.equal(urls.created[0].mime, 'image/png');
  assert.deepEqual([...urls.created[0].bytes], [...bytes]);
  // A second load of the same revision is served from the cache: one URL only.
  const again = await loader.load(entry(), SESSION);
  assert.deepEqual(again, first);
  assert.equal(urls.created.length, 1);
  loader.dispose();
  assert.deepEqual(urls.revoked, [first.url]);
  // After dispose the cache is empty, so the next load resolves a fresh URL.
  const after = await loader.load(entry(), SESSION);
  assert.equal(after.status, 'ready');
  assert.notEqual(after.url, first.url);
  assert.equal(urls.created.length, 2);
  loader.dispose();
});

test('a load superseded by dispose publishes nothing and leaks no URL', async () => {
  const bytes = new Uint8Array([1, 2, 3, 4]);
  let release = (): void => {};
  const blocked = new Promise<void>((resolve) => {
    release = resolve;
  });
  const target = {
    async readArtifact(_session: SessionRef, path: string, offset: number) {
      await blocked;
      return {
        path,
        offset,
        data_base64: base64(bytes),
        byteLength: bytes.length,
        nextOffset: bytes.length,
        eof: true,
        metadata: entry({ size: bytes.length }),
      };
    },
  };
  const urls = fakeUrls();
  const loader = createArtifactImageLoader({ read: target, urls: urls.factory });
  const pending = loader.load(entry(), SESSION);
  // The reader switched files (or the window closed) while the chunks were in
  // flight: the late result must not create a URL for a dead generation.
  loader.dispose();
  release();
  const resource = await pending;
  assert.equal(resource.status, 'loading');
  assert.deepEqual(urls.created, []);
  assert.deepEqual(urls.revoked, []);
});

test('the loader refuses an oversized image before reading it', async () => {
  const calls: ReadCall[] = [];
  const target = {
    async readArtifact() {
      calls.push({ path: 'never', offset: 0, limit: 0, revision: null });
      throw new Error('the read must not happen');
    },
  };
  const urls = fakeUrls();
  const loader = createArtifactImageLoader({ read: target, urls: urls.factory });
  const resource = await loader.load(
    entry({ path: 'assets/huge.png', size: ARTIFACT_IMAGE_MAX_BYTES + 1 }),
    SESSION,
  );
  assert.equal(resource.status, 'refused');
  assert.match(resource.status === 'refused' ? resource.reason : '', /超过预览上限/);
  assert.deepEqual(calls, []);
  assert.deepEqual(urls.created, []);
});

test('a failed read surfaces its own error and creates no URL', async () => {
  const boom = new Error('artifact changed');
  const target = {
    async readArtifact() {
      throw boom;
    },
  };
  const urls = fakeUrls();
  const loader = createArtifactImageLoader({ read: target, urls: urls.factory });
  const resource = await loader.load(entry(), SESSION);
  assert.equal(resource.status, 'error');
  assert.equal(resource.status === 'error' ? resource.error : null, boom);
  assert.deepEqual(urls.created, []);
  assert.deepEqual(urls.revoked, []);
});
