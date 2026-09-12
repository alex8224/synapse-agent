/**
 * Offline tests for the history-attachment projection and the lazy,
 * generation-guarded blob-URL loader.
 *
 * No DOM: the loader's object-URL factory is injected, so the whole
 * read -> object-URL -> revoke lifecycle runs under the Node test runner.  The
 * properties pinned here are the ones the transcript depends on:
 *
 * - an older server that omits `HistoryEvent.attachments` renders no thumbnails
 *   (`[]`), and an individually malformed row is dropped, never rendered;
 * - `mapHistoryEvents` keeps an attachment-only user turn (empty text);
 * - only the accepted image types within the 4 MB ceiling are ever fetched;
 * - the loader caches a resolved URL, revokes every URL it created on dispose
 *   (unmount / session change), and a read that resolves after dispose never
 *   creates a URL (stale generation guard).
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  attachmentWithinLimits,
  createAttachmentLoader,
  mapHistoryAttachments,
} from '../src/stores/historyAttachments.ts';
import type { TranscriptAttachment } from '../src/stores/historyAttachments.ts';
import { mapHistoryEvents } from '../src/stores/historyMapper.ts';
import { ATTACHMENT_MAX_BYTES, encodeBase64 } from '../src/runtime-client/attachments.ts';
import type {
  AttachmentChunkView,
  AttachmentReadTarget,
} from '../src/runtime-client/attachments.ts';

const SESSION = { project_id: 'proj', thread_id: 'thr' };
const ATTACHMENT_ID = 'b'.repeat(32);

function meta(overrides: Partial<TranscriptAttachment> = {}): TranscriptAttachment {
  return {
    attachmentId: ATTACHMENT_ID,
    imageId: 1,
    name: 'shot.png',
    mime: 'image/png',
    size: 3,
    revision: 'rev-1',
    ...overrides,
  };
}

// --- projection -------------------------------------------------------------

test('an old server without attachments maps to no thumbnails', () => {
  assert.deepEqual(mapHistoryAttachments(undefined), []);
  assert.deepEqual(mapHistoryAttachments(null), []);
  assert.deepEqual(mapHistoryAttachments('nope'), []);
  assert.deepEqual(mapHistoryAttachments({}), []);
});

test('valid attachment rows are mapped and malformed rows are dropped', () => {
  const mapped = mapHistoryAttachments([
    {
      attachment_id: ATTACHMENT_ID,
      image_id: 1,
      name: 'shot.png',
      mime: 'image/png',
      size: 2048,
      revision: 'rev-1',
    },
    { attachment_id: '', image_id: 2, name: 'x', mime: 'image/png', size: 1, revision: null },
    { attachment_id: 'c'.repeat(32), image_id: 3, name: 'y', mime: 'image/png', size: -1, revision: null },
    { attachment_id: 'd'.repeat(32), image_id: 4, name: 'z', mime: 'image/png', size: 5, revision: 7 },
    'garbage',
  ]);
  assert.deepEqual(mapped, [
    {
      attachmentId: ATTACHMENT_ID,
      imageId: 1,
      name: 'shot.png',
      mime: 'image/png',
      size: 2048,
      revision: 'rev-1',
    },
  ]);
});

test('an attachment-only user turn is kept in the transcript projection', () => {
  const events = [
    {
      kind: 'user',
      text: '',
      tool_calls: [],
      tool_results: [],
      attachments: [
        {
          attachment_id: ATTACHMENT_ID,
          image_id: 1,
          name: 'shot.png',
          mime: 'image/png',
          size: 2048,
          revision: null,
        },
      ],
    },
    { kind: 'answer', text: 'ok', tool_calls: [], tool_results: [] },
  ];
  const messages = mapHistoryEvents(events as never, { startTurn: 1, pageTag: 'latest' });
  const user = messages.find((message) => message.type === 'user');
  assert.ok(user);
  assert.equal(user.content, '');
  assert.equal(user.attachments?.length, 1);
  assert.equal(user.attachments?.[0].attachmentId, ATTACHMENT_ID);
});

test('a text-only turn carries no attachments field', () => {
  const events = [{ kind: 'user', text: 'hello', tool_calls: [], tool_results: [] }];
  const messages = mapHistoryEvents(events as never, { startTurn: 1, pageTag: 'latest' });
  assert.equal(messages[0].attachments, undefined);
});

// --- limits -----------------------------------------------------------------

test('only accepted image types within the 4 MB ceiling are renderable', () => {
  assert.equal(attachmentWithinLimits(meta()), true);
  assert.equal(attachmentWithinLimits(meta({ mime: 'text/plain' })), false);
  assert.equal(attachmentWithinLimits(meta({ size: 0 })), false);
  assert.equal(attachmentWithinLimits(meta({ size: ATTACHMENT_MAX_BYTES + 1 })), false);
  assert.equal(attachmentWithinLimits(meta({ size: ATTACHMENT_MAX_BYTES })), true);
});

// --- loader -----------------------------------------------------------------

function chunkOf(bytes: Uint8Array, attachmentId = ATTACHMENT_ID): AttachmentChunkView {
  return {
    attachmentId,
    offset: 0,
    data_base64: encodeBase64(bytes),
    byteLength: bytes.length,
    nextOffset: bytes.length,
    eof: true,
    metadata: {
      attachmentId,
      size: bytes.length,
      mime: 'image/png',
      revision: null,
      display_name: 'shot.png',
      created_at: '2026-01-01T00:00:00Z',
      finalized: true,
    },
  };
}

class FakeReads implements AttachmentReadTarget {
  calls: string[] = [];
  private readonly responses: Array<Promise<AttachmentChunkView>>;

  constructor(responses: Array<Promise<AttachmentChunkView>>) {
    this.responses = responses;
  }

  async readAttachment(
    _session: typeof SESSION,
    attachmentId: string,
  ): Promise<AttachmentChunkView> {
    this.calls.push(attachmentId);
    const next = this.responses.shift();
    if (!next) throw new Error('no queued read');
    return next;
  }
}

function recordingUrls() {
  const created: string[] = [];
  const revoked: string[] = [];
  return {
    created,
    revoked,
    factory: {
      create: (bytes: Uint8Array, mime: string) => {
        const url = `blob:${mime}:${bytes.length}:${created.length}`;
        created.push(url);
        return url;
      },
      revoke: (url: string) => {
        revoked.push(url);
      },
    },
  };
}

test('a load creates one object URL, caches it and dispose revokes it', async () => {
  const reads = new FakeReads([Promise.resolve(chunkOf(new Uint8Array([1, 2, 3])))]);
  const urls = recordingUrls();
  const loader = createAttachmentLoader({ read: reads, urls: urls.factory });

  const first = await loader.load(meta(), SESSION);
  assert.equal(first.status, 'ready');
  assert.deepEqual(reads.calls, [ATTACHMENT_ID]);
  assert.equal(urls.created.length, 1);

  // A second load of the same attachment is served from the cache (no re-read,
  // no second URL).
  const second = await loader.load(meta(), SESSION);
  assert.deepEqual(second, first);
  assert.equal(reads.calls.length, 1);
  assert.equal(urls.created.length, 1);

  loader.dispose();
  assert.deepEqual(urls.revoked, urls.created);
});

test('an out-of-limit attachment is refused without any read', async () => {
  const reads = new FakeReads([]);
  const urls = recordingUrls();
  const loader = createAttachmentLoader({ read: reads, urls: urls.factory });

  const resource = await loader.load(meta({ mime: 'text/plain' }), SESSION);
  assert.equal(resource.status, 'unsupported');
  assert.deepEqual(reads.calls, []);
  assert.deepEqual(urls.created, []);
  loader.dispose();
});

test('a read that resolves after dispose never creates a URL (stale generation)', async () => {
  let release: (value: AttachmentChunkView) => void = () => {};
  const pending = new Promise<AttachmentChunkView>((resolve) => {
    release = resolve;
  });
  const reads = new FakeReads([pending]);
  const urls = recordingUrls();
  const loader = createAttachmentLoader({ read: reads, urls: urls.factory });

  const inFlight = loader.load(meta(), SESSION);
  loader.dispose();
  release(chunkOf(new Uint8Array([9, 9])));
  const resource = await inFlight;

  assert.equal(resource.status, 'loading');
  assert.deepEqual(urls.created, []);
  assert.deepEqual(urls.revoked, []);
});

test('a failed read surfaces an error resource and creates no URL', async () => {
  const reads = new FakeReads([Promise.reject(new Error('read refused'))]);
  const urls = recordingUrls();
  const loader = createAttachmentLoader({ read: reads, urls: urls.factory });

  const resource = await loader.load(meta(), SESSION);
  assert.equal(resource.status, 'error');
  assert.match(resource.status === 'error' ? resource.reason : '', /read refused/);
  assert.deepEqual(urls.created, []);
  loader.dispose();
});
