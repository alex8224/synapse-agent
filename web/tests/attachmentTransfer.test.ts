/**
 * Offline tests for the attachment transfer helper and the six typed core
 * methods (`runtime.attachments.begin/append/finish/abort/stat/read`).
 *
 * No daemon and no host: the helper runs against an injected fake transfer
 * target, and the core client runs over an injected `SocketLike`.  The
 * properties pinned here are the ones the composer and the transcript depend
 * on:
 *
 * - the base64 codec is a strict, DOM-free round trip (padding, empty input,
 *   malformed payloads rejected instead of silently truncated);
 * - a picked file is filtered against the count/type/size limits with a visible
 *   reason per refusal;
 * - an upload streams bounded chunks whose offsets advance from the server's
 *   own `next_offset`, reports progress, and finalizes with the declared
 *   size/mime — never one whole-payload frame;
 * - a failed or cancelled upload aborts its partial bytes best-effort and
 *   rethrows (the composer never degrades to sending the filename as a prompt);
 * - a read is bounded (window, assembled ceiling, eof) and fails closed when
 *   the server stops advancing the offset;
 * - the six core methods send the exact wire frames the daemon expects.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  ATTACHMENT_CHUNK_BYTES,
  ATTACHMENT_MAX_BYTES,
  ATTACHMENT_MAX_COUNT,
  ATTACHMENT_READ_BYTES,
  AttachmentUploadCancelledError,
  MalformedAttachmentError,
  decodeBase64,
  encodeBase64,
  parseAttachmentChunk,
  parseAttachmentMetadata,
  readAttachmentBytes,
  selectAttachmentCandidates,
  uploadAttachment,
} from '../src/runtime-client/attachments.ts';
import type {
  AttachmentReadTarget,
  AttachmentTransferTarget,
} from '../src/runtime-client/attachments.ts';
import { SynapseRuntimeClient } from '../src/runtime-client/SynapseRuntimeClient.ts';
import type { SocketLike } from '../src/runtime-client/SynapseRuntimeClient.ts';

const SESSION = { project_id: 'proj', thread_id: 'thr' };
const ATTACHMENT_ID = 'a'.repeat(32);

const tick = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

function bytesOf(length: number, seed = 0): Uint8Array {
  const out = new Uint8Array(length);
  for (let index = 0; index < length; index += 1) out[index] = (index * 31 + seed) & 255;
  return out;
}

// --- base64 codec -----------------------------------------------------------

test('the base64 codec is a strict DOM-free round trip', () => {
  assert.equal(encodeBase64(new Uint8Array([])), '');
  assert.equal(encodeBase64(new Uint8Array([0x66])), 'Zg==');
  assert.equal(encodeBase64(new Uint8Array([0x66, 0x6f])), 'Zm8=');
  assert.equal(encodeBase64(new Uint8Array([0x66, 0x6f, 0x6f])), 'Zm9v');
  assert.equal(encodeBase64(new Uint8Array([0x66, 0x6f, 0x6f, 0x62, 0x61, 0x72])), 'Zm9vYmFy');

  const all = bytesOf(256);
  assert.deepEqual(decodeBase64(encodeBase64(all)), all);
  const odd = bytesOf(4097, 7);
  assert.deepEqual(decodeBase64(encodeBase64(odd)), odd);
});

test('the base64 decoder rejects malformed payloads instead of truncating', () => {
  assert.throws(() => decodeBase64('Zm9vY'), MalformedAttachmentError);
  assert.throws(() => decodeBase64('Zm9v!'), MalformedAttachmentError);
  assert.throws(() => decodeBase64('Z=9v'), MalformedAttachmentError);
});

// --- local candidate filtering ----------------------------------------------

test('picked files are filtered against the count, type and size limits', () => {
  const candidates = [
    { name: 'ok.png', mime: 'image/png', size: 1024 },
    { name: 'notes.txt', mime: 'text/plain', size: 10 },
    { name: 'huge.png', mime: 'image/png', size: ATTACHMENT_MAX_BYTES + 1 },
    { name: 'empty.gif', mime: 'image/gif', size: 0 },
    { name: 'photo.JPEG', mime: 'image/jpeg; charset=binary', size: 2048 },
  ];
  const { accepted, errors } = selectAttachmentCandidates(0, candidates);
  assert.deepEqual(
    accepted.map((entry) => entry.name),
    ['ok.png', 'photo.JPEG'],
  );
  assert.equal(errors.length, 3);
  assert.match(errors[0], /不支持的图片类型/);
  assert.match(errors[1], /超过上限/);
  assert.match(errors[2], /文件为空/);
});

test('the per-submit count cap refuses the ninth image with a visible reason', () => {
  const candidate = { name: 'x.png', mime: 'image/png', size: 10 };
  const { accepted, errors } = selectAttachmentCandidates(ATTACHMENT_MAX_COUNT, [candidate]);
  assert.deepEqual(accepted, []);
  assert.equal(errors.length, 1);
  assert.match(errors[0], new RegExp(String(ATTACHMENT_MAX_COUNT)));
});

// --- upload helper ----------------------------------------------------------

interface BeginCall {
  session: unknown;
  size: number;
  mime: string;
  displayName: string;
}

class FakeTransferTarget implements AttachmentTransferTarget {
  begun: BeginCall | null = null;
  appends: Array<{ attachmentId: string; expectedOffset: number; bytes: Uint8Array }> = [];
  finished: Array<{ attachmentId: string; size: number; mime: string }> = [];
  aborted: string[] = [];
  failAppendAt = -1;

  async beginAttachment(
    session: typeof SESSION,
    size: number,
    mime: string,
    displayName = '',
  ) {
    this.begun = { session, size, mime, displayName };
    return {
      attachmentId: ATTACHMENT_ID,
      chunk_bytes: ATTACHMENT_CHUNK_BYTES,
      chunk_base64_chars: 4 * Math.ceil(ATTACHMENT_CHUNK_BYTES / 3),
      expires_at: '2026-01-01T00:00:00Z',
      next_offset: 0,
    };
  }

  async appendAttachmentChunk(
    session: typeof SESSION,
    attachmentId: string,
    expectedOffset: number,
    dataBase64: string,
  ) {
    if (this.appends.length === this.failAppendAt) throw new Error('append rejected');
    const bytes = decodeBase64(dataBase64);
    this.appends.push({ attachmentId, expectedOffset, bytes });
    const nextOffset = expectedOffset + bytes.length;
    return {
      ref: { session, attachment_id: attachmentId },
      received_bytes: nextOffset,
      next_offset: nextOffset,
    };
  }

  async finishAttachment(
    _session: typeof SESSION,
    attachmentId: string,
    expectedSize: number,
    expectedMime: string,
  ) {
    this.finished.push({ attachmentId, size: expectedSize, mime: expectedMime });
    return { attachmentId, size: expectedSize, mime: expectedMime, revision: 'rev-1' };
  }

  async abortAttachment(session: typeof SESSION, attachmentId: string) {
    this.aborted.push(attachmentId);
    return { ref: { session, attachment_id: attachmentId }, removed: true };
  }
}

test('an upload streams bounded chunks and finalizes with the declared size/mime', async () => {
  const target = new FakeTransferTarget();
  const payload = bytesOf(ATTACHMENT_CHUNK_BYTES + 4096, 3);
  const progress: number[] = [];

  const finished = await uploadAttachment(target, {
    session: SESSION,
    bytes: payload,
    mime: 'image/png',
    displayName: 'shot.png',
    onProgress: (uploaded, total) => {
      assert.equal(total, payload.length);
      progress.push(uploaded);
    },
  });

  assert.deepEqual(target.begun, {
    session: SESSION,
    size: payload.length,
    mime: 'image/png',
    displayName: 'shot.png',
  });
  assert.equal(target.appends.length, 2);
  assert.deepEqual(
    target.appends.map((entry) => entry.expectedOffset),
    [0, ATTACHMENT_CHUNK_BYTES],
  );
  assert.equal(target.appends[0].bytes.length, ATTACHMENT_CHUNK_BYTES);
  assert.equal(target.appends[1].bytes.length, 4096);
  // The reassembled stream is byte-identical to the picked file.
  const reassembled = new Uint8Array(payload.length);
  reassembled.set(target.appends[0].bytes, 0);
  reassembled.set(target.appends[1].bytes, ATTACHMENT_CHUNK_BYTES);
  assert.deepEqual(reassembled, payload);
  assert.deepEqual(progress, [ATTACHMENT_CHUNK_BYTES, payload.length]);
  assert.deepEqual(target.finished, [
    { attachmentId: ATTACHMENT_ID, size: payload.length, mime: 'image/png' },
  ]);
  assert.deepEqual(target.aborted, []);
  assert.equal(finished.attachmentId, ATTACHMENT_ID);
  assert.equal(finished.size, payload.length);
  assert.equal(finished.revision, 'rev-1');
  assert.equal(finished.finalized, true);
});

test('a refused chunk aborts the partial upload and rethrows', async () => {
  const target = new FakeTransferTarget();
  target.failAppendAt = 1;
  const payload = bytesOf(ATTACHMENT_CHUNK_BYTES + 10, 5);

  await assert.rejects(
    uploadAttachment(target, { session: SESSION, bytes: payload, mime: 'image/png' }),
    /append rejected/,
  );
  assert.deepEqual(target.aborted, [ATTACHMENT_ID]);
  assert.deepEqual(target.finished, []);
});

test('cancelling between chunks aborts the partial upload and never finishes it', async () => {
  const target = new FakeTransferTarget();
  const payload = bytesOf(ATTACHMENT_CHUNK_BYTES * 3, 9);
  let cancelled = false;

  await assert.rejects(
    uploadAttachment(target, {
      session: SESSION,
      bytes: payload,
      mime: 'image/webp',
      isCancelled: () => cancelled,
      onProgress: () => {
        cancelled = true;
      },
    }),
    (err: unknown) => err instanceof AttachmentUploadCancelledError,
  );
  assert.equal(target.appends.length, 1);
  assert.deepEqual(target.aborted, [ATTACHMENT_ID]);
  assert.deepEqual(target.finished, []);
});

test('a locally unacceptable image is refused before any begin frame', async () => {
  const target = new FakeTransferTarget();
  await assert.rejects(
    uploadAttachment(target, {
      session: SESSION,
      bytes: bytesOf(ATTACHMENT_MAX_BYTES + 1, 1),
      mime: 'image/png',
    }),
    /超过上限/,
  );
  assert.equal(target.begun, null);
});

// --- bounded read helper ----------------------------------------------------

class FakeReadTarget implements AttachmentReadTarget {
  calls: Array<{ offset: number; limit: number }> = [];
  stoppedAdvancing = false;
  private readonly payload: Uint8Array;

  constructor(payload: Uint8Array) {
    this.payload = payload;
  }

  async readAttachment(
    _session: typeof SESSION,
    attachmentId: string,
    offset = 0,
    limit = ATTACHMENT_READ_BYTES,
  ) {
    this.calls.push({ offset, limit });
    const end = Math.min(offset + limit, this.payload.length);
    const window = this.payload.subarray(offset, end);
    const nextOffset = this.stoppedAdvancing ? offset : end;
    return {
      attachmentId,
      offset,
      data_base64: encodeBase64(window),
      byteLength: window.length,
      nextOffset,
      eof: end >= this.payload.length,
      metadata: {
        attachmentId,
        size: this.payload.length,
        mime: 'image/png',
        revision: null,
        display_name: 'x.png',
        created_at: '2026-01-01T00:00:00Z',
        finalized: true,
      },
    };
  }
}

test('a read assembles bounded windows until eof', async () => {
  const payload = bytesOf(ATTACHMENT_READ_BYTES * 2 + 5, 11);
  const target = new FakeReadTarget(payload);
  const bytes = await readAttachmentBytes(target, {
    session: SESSION,
    attachmentId: ATTACHMENT_ID,
  });
  assert.deepEqual(bytes, payload);
  assert.deepEqual(
    target.calls.map((call) => call.limit),
    [ATTACHMENT_READ_BYTES, ATTACHMENT_READ_BYTES, ATTACHMENT_READ_BYTES],
  );
  assert.deepEqual(
    target.calls.map((call) => call.offset),
    [0, ATTACHMENT_READ_BYTES, ATTACHMENT_READ_BYTES * 2],
  );
});

test('a read fails closed when the server stops advancing the offset', async () => {
  const target = new FakeReadTarget(bytesOf(ATTACHMENT_READ_BYTES + 10, 2));
  target.stoppedAdvancing = true;
  await assert.rejects(
    readAttachmentBytes(target, { session: SESSION, attachmentId: ATTACHMENT_ID }),
    MalformedAttachmentError,
  );
});

test('a read never assembles past its ceiling', async () => {
  const target = new FakeReadTarget(bytesOf(ATTACHMENT_READ_BYTES * 2, 4));
  await assert.rejects(
    readAttachmentBytes(target, {
      session: SESSION,
      attachmentId: ATTACHMENT_ID,
      maxBytes: ATTACHMENT_READ_BYTES + 1,
    }),
    MalformedAttachmentError,
  );
});

// --- strict wire decoders ---------------------------------------------------

test('the metadata and chunk decoders reject an unexpected field set', () => {
  const metadata = {
    ref: { session: SESSION, attachment_id: ATTACHMENT_ID },
    size: 12,
    mime: 'image/png',
    revision: null,
    display_name: 'x.png',
    created_at: '2026-01-01T00:00:00Z',
    finalized: true,
  };
  const parsed = parseAttachmentMetadata(metadata);
  assert.equal(parsed.attachmentId, ATTACHMENT_ID);
  assert.equal(parsed.size, 12);
  assert.throws(
    () => parseAttachmentMetadata({ ...metadata, extra: 1 }),
    MalformedAttachmentError,
  );
  assert.throws(
    () =>
      parseAttachmentChunk({
        ref: { session: SESSION, attachment_id: ATTACHMENT_ID },
        offset: 0,
        data_base64: 'Zm9v',
        byte_length: 3,
        next_offset: 3,
        eof: true,
        metadata,
        surprise: true,
      }),
    MalformedAttachmentError,
  );
});

// --- typed core methods -----------------------------------------------------

interface SentFrame {
  id: number;
  method: string;
  params: any;
}

class FakeSocket implements SocketLike {
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: any }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  sent: SentFrame[] = [];
  respond: (request: SentFrame) => unknown = () => ({});

  send(data: string): void {
    const parsed = JSON.parse(data) as SentFrame;
    this.sent.push(parsed);
    if (parsed.method === 'runtime.protocol.negotiate') {
      this.push({
        jsonrpc: '2.0',
        id: parsed.id,
        result: {
          wire_version: '1',
          supported_versions: ['1'],
          capabilities: { legacy_v1: true, raw_cursor: true, watch_resume: true, approval_resume: true },
        },
      });
      return;
    }
    this.push({ jsonrpc: '2.0', id: parsed.id, result: this.respond(parsed) });
  }

  close(): void {
    this.readyState = 3;
  }

  push(frame: unknown): void {
    if (this.onmessage) this.onmessage({ data: JSON.stringify(frame) });
  }

  serverOpen(): void {
    this.readyState = 1;
    this.onopen?.();
  }

  businessFrames(): SentFrame[] {
    return this.sent.filter((frame) => frame.method !== 'runtime.protocol.negotiate');
  }
}

async function openClient() {
  const sockets: FakeSocket[] = [];
  const client = new SynapseRuntimeClient({
    url: 'ws://loopback',
    socketFactory: () => {
      const socket = new FakeSocket();
      sockets.push(socket);
      return socket;
    },
  });
  const promise = client.connect();
  await tick();
  const socket = sockets[0];
  socket.serverOpen();
  await promise;
  return { client, socket };
}

function refOf(attachmentId: string) {
  return { session: SESSION, attachment_id: attachmentId };
}

function metadataOf(size: number) {
  return {
    ref: refOf(ATTACHMENT_ID),
    size,
    mime: 'image/png',
    revision: 'rev-1',
    display_name: 'shot.png',
    created_at: '2026-01-01T00:00:00Z',
    finalized: true,
  };
}

test('begin/append/finish/abort/stat/read send the exact wire frames', async () => {
  const { client, socket } = await openClient();
  socket.respond = (request) => {
    switch (request.method) {
      case 'runtime.attachments.begin':
        return {
          ref: refOf(ATTACHMENT_ID),
          chunk_bytes: ATTACHMENT_CHUNK_BYTES,
          chunk_base64_chars: 4 * Math.ceil(ATTACHMENT_CHUNK_BYTES / 3),
          expires_at: '2026-01-01T00:00:00Z',
          next_offset: 0,
        };
      case 'runtime.attachments.append':
        return {
          ref: refOf(ATTACHMENT_ID),
          received_bytes: 3,
          next_offset: 3,
        };
      case 'runtime.attachments.finish':
        return {
          ref: refOf(ATTACHMENT_ID),
          size: request.params.expected_size,
          mime: request.params.expected_mime,
          revision: 'rev-1',
        };
      case 'runtime.attachments.abort':
        return { ref: refOf(ATTACHMENT_ID), removed: true };
      case 'runtime.attachments.stat':
        return metadataOf(3);
      default:
        return {
          ref: refOf(ATTACHMENT_ID),
          offset: request.params.offset,
          data_base64: 'Zm9v',
          byte_length: 3,
          next_offset: request.params.offset + 3,
          eof: true,
          metadata: metadataOf(3),
        };
    }
  };

  const begun = await client.beginAttachment(SESSION, 3, 'image/png', 'shot.png');
  assert.equal(begun.attachmentId, ATTACHMENT_ID);
  const begin = socket.businessFrames()[0];
  assert.equal(begin.method, 'runtime.attachments.begin');
  assert.deepEqual(begin.params, {
    session: SESSION,
    size: 3,
    mime: 'image/png',
    display_name: 'shot.png',
  });

  const appended = await client.appendAttachmentChunk(SESSION, ATTACHMENT_ID, 0, 'Zm9v');
  assert.equal(appended.next_offset, 3);
  const append = socket.businessFrames()[1];
  assert.equal(append.method, 'runtime.attachments.append');
  assert.deepEqual(append.params, {
    ref: refOf(ATTACHMENT_ID),
    expected_offset: 0,
    data_base64: 'Zm9v',
  });

  const finished = await client.finishAttachment(SESSION, ATTACHMENT_ID, 3, 'image/png');
  assert.equal(finished.revision, 'rev-1');
  const finish = socket.businessFrames()[2];
  assert.equal(finish.method, 'runtime.attachments.finish');
  assert.deepEqual(finish.params, {
    ref: refOf(ATTACHMENT_ID),
    expected_size: 3,
    expected_mime: 'image/png',
  });

  const aborted = await client.abortAttachment(SESSION, ATTACHMENT_ID);
  assert.equal(aborted.removed, true);
  assert.deepEqual(socket.businessFrames()[3].params, { ref: refOf(ATTACHMENT_ID) });

  const stat = await client.statAttachment(SESSION, ATTACHMENT_ID);
  assert.equal(stat.size, 3);
  assert.equal(stat.attachmentId, ATTACHMENT_ID);

  const chunk = await client.readAttachment(SESSION, ATTACHMENT_ID, 0);
  assert.equal(chunk.byteLength, 3);
  assert.equal(chunk.eof, true);
  const read = socket.businessFrames()[5];
  assert.equal(read.method, 'runtime.attachments.read');
  assert.deepEqual(read.params, { ref: refOf(ATTACHMENT_ID), offset: 0, limit: ATTACHMENT_READ_BYTES });
});

test('begin omits display_name when none is given', async () => {
  const { client, socket } = await openClient();
  socket.respond = () => ({
    ref: refOf(ATTACHMENT_ID),
    chunk_bytes: ATTACHMENT_CHUNK_BYTES,
    chunk_base64_chars: 4,
    expires_at: '2026-01-01T00:00:00Z',
    next_offset: 0,
  });
  await client.beginAttachment(SESSION, 10, 'image/png');
  assert.deepEqual(Object.keys(socket.businessFrames()[0].params).sort(), ['mime', 'session', 'size']);
});
