/**
 * Offline tests for the worker-backed Markdown parse queue.
 *
 * The queue owns the invariants the transcript depends on, and these tests pin
 * them without a browser:
 *
 * - the worker is a single serial lane: one parse in flight, at most one pending
 *   source per mounted document, so a fast stream replaces obsolete work instead
 *   of queueing a copy of every prefix;
 * - a slightly older prefix may still paint (a stream cannot be starved by its
 *   own next chunk), but a *replaced* source must never display;
 * - a worker that cannot be built, posted to, or heard from degrades every
 *   document to the readable full source -- never back onto the UI thread.
 *
 * A real Worker boundary structured-clones every message, so the fake worker
 * clones its responses too: block identity surviving a parse is only meaningful
 * if the worker hands back fresh objects.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { MarkdownParseQueue, PARSE_MAX_CHARS } from '../src/markdown/parseQueue.ts';
import type {
  MarkdownSnapshot,
  ParseRequest,
  ParseResponse,
  ParseWorker,
  ParsedBlock,
} from '../src/markdown/parseQueue.ts';
import { parseMarkdown } from '../src/markdown/parse.ts';

/** Let every queued microtask settle (and one macrotask, for good measure). */
const settle = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));

/** Mirror the worker thread: parse, then stamp each block with its JSON key. */
function parseBlocks(text: string): ParsedBlock[] {
  return parseMarkdown(text).map((block) => ({ key: JSON.stringify(block), block }));
}

type ParseFn = (text: string) => ParsedBlock[] | null;

interface FakeWorkerOptions {
  /** Replace the parser, e.g. to make one source fail per-document. */
  parse?: ParseFn;
  /** Answer posted requests on a microtask (the realistic default). */
  auto?: boolean;
  /** Simulate a `postMessage` that throws (a detached or blocked port). */
  throwOnPost?: boolean;
}

/** A hand-driven stand-in for the module worker that implements `ParseWorker`. */
class FakeWorker implements ParseWorker {
  onmessage: ((event: MessageEvent<ParseResponse>) => void) | null = null;
  onerror: ((event: ErrorEvent) => void) | null = null;
  onmessageerror: ((event: MessageEvent<unknown>) => void) | null = null;

  readonly posted: ParseRequest[] = [];
  terminated = false;
  /** Highest number of unanswered posts seen at once: the queue caps it at 1. */
  maxInFlight = 0;

  inFlight = 0;
  parse: ParseFn;
  auto: boolean;
  throwOnPost: boolean;

  constructor(options: FakeWorkerOptions = {}) {
    this.parse = options.parse ?? parseBlocks;
    this.auto = options.auto ?? true;
    this.throwOnPost = options.throwOnPost ?? false;
  }

  postMessage(request: ParseRequest): void {
    if (this.throwOnPost) throw new Error('postMessage failed');
    if (this.terminated) return;
    this.posted.push(request);
    this.inFlight += 1;
    if (this.inFlight > this.maxInFlight) this.maxInFlight = this.inFlight;
    if (this.auto) queueMicrotask(() => this.deliver(request));
  }

  /** Answer a posted request the way the worker thread would. */
  deliver(request: ParseRequest): void {
    if (this.terminated) return;
    this.inFlight -= 1;
    const response: ParseResponse = { id: request.id, blocks: this.parse(request.text) };
    // The worker boundary structured-clones the payload: fresh block objects.
    this.onmessage?.({ data: structuredClone(response) } as MessageEvent<ParseResponse>);
  }

  terminate(): void {
    this.terminated = true;
  }
}

interface Recorder {
  snapshots: MarkdownSnapshot[];
  receive: (snapshot: MarkdownSnapshot) => void;
}

function recorder(): Recorder {
  const snapshots: MarkdownSnapshot[] = [];
  return { snapshots, receive: (snapshot) => snapshots.push(snapshot) };
}

test('a burst of updates keeps exactly one parse in flight', () => {
  const worker = new FakeWorker({ auto: false });
  const parser = new MarkdownParseQueue(() => worker);
  const { snapshots, receive } = recorder();
  const document = parser.open(receive);

  document.update('a');
  document.update('ab');
  document.update('abc');
  document.update('abcd');

  // Only the first source was posted; the rest collapsed into one pending latest.
  assert.equal(worker.posted.length, 1);
  assert.equal(worker.posted[0].text, 'a');

  worker.deliver(worker.posted[0]);
  // Completing the first starts exactly one more parse (the latest pending).
  assert.equal(worker.posted.length, 2);
  assert.equal(worker.posted[1].text, 'abcd');

  worker.deliver(worker.posted[1]);
  assert.equal(worker.posted.length, 2, 'the lane is idle once nothing is pending');
  assert.equal(snapshots.at(-1)?.source, 'abcd');
});

test('the worker never sees a second postMessage before answering the first', async () => {
  const worker = new FakeWorker();
  const parser = new MarkdownParseQueue(() => worker);
  const { receive } = recorder();
  const document = parser.open(receive);

  document.update('one');
  document.update('one two');
  document.update('one two three');
  await settle();

  assert.equal(worker.maxInFlight, 1);
  assert.equal(worker.posted.length, 2, 'the collapsed prefixes were never posted');
});

test('a second update per document collapses into one pending parse', () => {
  const worker = new FakeWorker({ auto: false });
  const parser = new MarkdownParseQueue(() => worker);
  const a = parser.open(() => {});
  const b = parser.open(() => {});

  a.update('a1');
  a.update('a2');
  b.update('b1');
  b.update('b2');

  assert.deepEqual(worker.posted.map((request) => request.text), ['a1']);
  worker.deliver(worker.posted[0]);
  assert.deepEqual(worker.posted.map((request) => request.text), ['a1', 'a2']);
  worker.deliver(worker.posted[1]);
  assert.deepEqual(worker.posted.map((request) => request.text), ['a1', 'a2', 'b2']);
});

test('documents are parsed in the order they first went pending', async () => {
  const worker = new FakeWorker();
  const parser = new MarkdownParseQueue(() => worker);
  const order: string[] = [];
  const a = parser.open((snapshot) => order.push(`a:${snapshot.source}`));
  const b = parser.open((snapshot) => order.push(`b:${snapshot.source}`));
  const c = parser.open((snapshot) => order.push(`c:${snapshot.source}`));

  a.update('a1');
  b.update('b1');
  c.update('c1');
  await settle();

  assert.deepEqual(order, ['a:a1', 'b:b1', 'c:c1']);
});

test('an older prefix result still paints while the next parse is in flight', async () => {
  const worker = new FakeWorker();
  const parser = new MarkdownParseQueue(() => worker);
  const { snapshots, receive } = recorder();
  const document = parser.open(receive);

  document.update('hello');
  document.update('hello world');
  await settle();

  assert.deepEqual(snapshots.map((snapshot) => snapshot.source), ['hello', 'hello world']);
  assert.ok(snapshots[0].blocks !== null, 'the prefix paints instead of starving the stream');
});

test('a replaced document never paints the obsolete answer', async () => {
  const worker = new FakeWorker();
  const parser = new MarkdownParseQueue(() => worker);
  const { snapshots, receive } = recorder();
  const document = parser.open(receive);

  document.update('hello');
  document.update('goodbye');
  await settle();

  assert.deepEqual(snapshots.map((snapshot) => snapshot.source), ['goodbye']);
});

test('the final streamed text is always delivered', async () => {
  const worker = new FakeWorker();
  const parser = new MarkdownParseQueue(() => worker);
  const { snapshots, receive } = recorder();
  const document = parser.open(receive);

  for (const text of ['H', 'He', 'Hel', 'Hell', 'Hello']) document.update(text);
  await settle();

  assert.equal(snapshots.at(-1)?.source, 'Hello');
  assert.ok(snapshots.at(-1)?.blocks !== null);
});

test('an unchanged block keeps its identity across parses', async () => {
  const worker = new FakeWorker();
  const parser = new MarkdownParseQueue(() => worker);
  const { snapshots, receive } = recorder();
  const document = parser.open(receive);

  document.update('alpha');
  await settle();
  document.update('alpha\n\nbeta');
  await settle();

  const [first, second] = snapshots;
  assert.equal(first.blocks?.length, 1);
  assert.equal(second.blocks?.length, 2);
  // The shared first block is the same object; the appended tail is fresh.
  assert.equal(second.blocks?.[0], first.blocks?.[0]);
  assert.notEqual(second.blocks?.[1], first.blocks?.[0]);
});

test('dispose drops pending work and ignores a late result', () => {
  const worker = new FakeWorker({ auto: false });
  const parser = new MarkdownParseQueue(() => worker);
  const { snapshots, receive } = recorder();
  const document = parser.open(receive);

  document.update('hello');
  document.update('hello world');
  document.dispose();

  assert.equal(worker.terminated, true, 'the last document disposes the worker');
  assert.equal(worker.posted.length, 1, 'the pending source is dropped, not posted');

  // A message already on the wire must not resurrect the unmounted row.
  const response: ParseResponse = { id: worker.posted[0].id, blocks: parseBlocks('hello') };
  worker.onmessage?.({ data: structuredClone(response) } as MessageEvent<ParseResponse>);

  assert.deepEqual(snapshots, []);
});

test('only the last dispose terminates the shared worker', () => {
  const worker = new FakeWorker({ auto: false });
  const parser = new MarkdownParseQueue(() => worker);
  const a = parser.open(() => {});
  const b = parser.open(() => {});

  a.update('a');
  b.update('b');
  a.dispose();
  assert.equal(worker.terminated, false);
  b.dispose();
  assert.equal(worker.terminated, true);
});

test('disposing one document does not cancel another document', async () => {
  const worker = new FakeWorker();
  const parser = new MarkdownParseQueue(() => worker);
  const a = recorder();
  const b = recorder();
  const da = parser.open(a.receive);
  const db = parser.open(b.receive);

  da.update('alpha');
  db.update('beta');
  da.dispose();
  await settle();

  assert.deepEqual(a.snapshots, []);
  assert.equal(b.snapshots.at(-1)?.source, 'beta');
  assert.equal(worker.terminated, false);
});

test('updating a disposed document is a no-op', () => {
  const worker = new FakeWorker({ auto: false });
  const parser = new MarkdownParseQueue(() => worker);
  const { snapshots, receive } = recorder();
  const document = parser.open(receive);

  document.dispose();
  document.update('after dispose');

  assert.deepEqual(snapshots, []);
  assert.equal(worker.posted.length, 0);
});

test('a late error from a replaced worker cannot poison the new one', () => {
  const workers: FakeWorker[] = [];
  const parser = new MarkdownParseQueue(() => {
    const worker = new FakeWorker({ auto: false });
    workers.push(worker);
    return worker;
  });
  const { snapshots, receive } = recorder();
  const first = parser.open(receive);
  first.update('old');
  first.dispose();

  const second = parser.open(receive);
  second.update('new');
  assert.equal(workers.length, 2, 'the disposed worker was replaced, not reused');

  // Worker #1 reports an error after it was replaced: the queue must ignore it.
  workers[0].onerror?.({} as ErrorEvent);
  assert.deepEqual(snapshots, []);

  // Worker #2 still answers normally.
  workers[1].deliver(workers[1].posted[0]);
  assert.equal(snapshots.at(-1)?.source, 'new');
  assert.ok(snapshots.at(-1)?.blocks !== null);
});

test('a worker that cannot be constructed falls back to the full source', () => {
  const parser = new MarkdownParseQueue(() => {
    throw new Error('no Worker API');
  });
  const { snapshots, receive } = recorder();
  const document = parser.open(receive);

  document.update('hello world');
  assert.deepEqual(snapshots, [{ source: 'hello world', blocks: null }]);

  // The failure is sticky: later updates stay on the readable full source.
  document.update('hello world again');
  assert.deepEqual(snapshots.at(-1), { source: 'hello world again', blocks: null });
});

test('a throwing postMessage degrades to the full source', () => {
  const worker = new FakeWorker({ throwOnPost: true });
  const parser = new MarkdownParseQueue(() => worker);
  const { snapshots, receive } = recorder();
  const document = parser.open(receive);

  document.update('hello');

  assert.deepEqual(snapshots, [{ source: 'hello', blocks: null }]);
  assert.equal(worker.terminated, true);
});

test('a worker error degrades every document to the full source', () => {
  const worker = new FakeWorker({ auto: false });
  const parser = new MarkdownParseQueue(() => worker);
  const a = recorder();
  const b = recorder();
  const da = parser.open(a.receive);
  const db = parser.open(b.receive);

  da.update('alpha');
  db.update('beta');
  worker.onerror?.({} as ErrorEvent);

  assert.deepEqual(a.snapshots.at(-1), { source: 'alpha', blocks: null });
  assert.deepEqual(b.snapshots.at(-1), { source: 'beta', blocks: null });
  assert.equal(worker.terminated, true);

  // A failed queue stays on the fallback instead of reviving the worker.
  da.update('alpha again');
  assert.deepEqual(a.snapshots.at(-1), { source: 'alpha again', blocks: null });
});

test('a messageerror degrades to the full source', () => {
  const worker = new FakeWorker({ auto: false });
  const parser = new MarkdownParseQueue(() => worker);
  const { snapshots, receive } = recorder();
  const document = parser.open(receive);

  document.update('hello');
  worker.onmessageerror?.({} as MessageEvent<unknown>);

  assert.deepEqual(snapshots.at(-1), { source: 'hello', blocks: null });
});

test('an oversized document bypasses the worker and renders as plain text', () => {
  let created = 0;
  const parser = new MarkdownParseQueue(() => {
    created += 1;
    return new FakeWorker();
  });
  const { snapshots, receive } = recorder();
  const document = parser.open(receive);

  const text = 'x'.repeat(PARSE_MAX_CHARS + 1);
  document.update(text);

  assert.equal(created, 0, 'no worker is built for a document past the cap');
  assert.deepEqual(snapshots, [{ source: text, blocks: null }]);
});

test('a document exactly at the size limit still goes through the worker', async () => {
  const worker = new FakeWorker();
  const parser = new MarkdownParseQueue(() => worker);
  const { snapshots, receive } = recorder();
  const document = parser.open(receive);

  document.update('x'.repeat(PARSE_MAX_CHARS));
  await settle();

  assert.equal(snapshots.at(-1)?.blocks?.length, 1);
});

test('an empty document short-circuits without touching the worker', () => {
  let created = 0;
  const parser = new MarkdownParseQueue(() => {
    created += 1;
    return new FakeWorker();
  });
  const { snapshots, receive } = recorder();
  const document = parser.open(receive);

  document.update('');

  assert.equal(created, 0);
  assert.deepEqual(snapshots, [{ source: '', blocks: [] }]);
});

test('a per-document parse failure leaves the other documents alone', async () => {
  const worker = new FakeWorker({
    parse: (text) => (text.includes('boom') ? null : parseBlocks(text)),
  });
  const parser = new MarkdownParseQueue(() => worker);
  const a = recorder();
  const b = recorder();
  const da = parser.open(a.receive);
  const db = parser.open(b.receive);

  da.update('boom');
  db.update('fine');
  await settle();

  assert.deepEqual(a.snapshots.at(-1), { source: 'boom', blocks: null });
  assert.equal(b.snapshots.at(-1)?.source, 'fine');
  assert.ok(b.snapshots.at(-1)?.blocks !== null);
  assert.equal(worker.terminated, false, 'a per-document failure is not a queue failure');

  // The failing document recovers on its next parse; nothing was poisoned.
  da.update('recovered');
  await settle();
  assert.ok(a.snapshots.at(-1)?.blocks !== null);
});

test('the failure path never falls back to a synchronous parser', () => {
  // The observable fallback is `blocks: null` (plain text), but that only proves
  // the *result*: this pins the stronger rule that the queue cannot parse on the
  // UI thread at all -- the parser stays behind the worker boundary.
  const source = readFileSync(
    fileURLToPath(new URL('../src/markdown/parseQueue.ts', import.meta.url)),
    'utf8',
  );
  assert.match(source, /import type \{[^}]*Block[^}]*\} from '\.\/parse\.ts'/);
  assert.doesNotMatch(source, /parseMarkdown/);
});
