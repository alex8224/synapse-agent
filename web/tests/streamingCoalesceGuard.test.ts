/**
 * Source guards for the streaming-cost work.
 *
 * A fast reasoning stream used to cost one store update, one re-render of every
 * subscriber and one Markdown re-parse of the whole accumulated thought *per
 * chunk*.  These assertions pin the three things that changed:
 *
 * - the store batches streamed deltas (`liveDeltaBatch`) instead of folding each
 *   chunk, and lands the queue at every ordering boundary;
 * - no component subscribes to the whole store, so chrome that does not paint the
 *   transcript does not re-render while the agent thinks;
 * - a transcript row and its Markdown are memoized, so only the row that grew is
 *   re-rendered.
 */
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const webRoot = join(here, '..');
const read = (relative: string) => readFileSync(join(webRoot, relative), 'utf8');

const store = read('src/stores/useConsoleStore.ts');

test('the store batches streamed deltas instead of folding each chunk', () => {
  assert.ok(store.includes("from './liveDeltaBatch.ts'"), 'the batching module must be wired in');
  assert.ok(store.includes('isCoalescibleDeltaKind(event.kind)'), 'only text deltas may be queued');
  assert.ok(store.includes('queueDelta('), 'a delta must be queued, not folded on arrival');
  assert.ok(store.includes('flushPendingDeltas();'), 'the queue must be flushed at ordering boundaries');
  assert.ok(store.includes('foldLiveEvents('), 'a run must fold through the batch fold');
  assert.equal(
    store.includes('store.setState((s) => reduceRuntimeEvent(s, event))'),
    false,
    'the per-event fold must be gone from the live path',
  );
});

test('every subscription change lands or drops the queue first', () => {
  const boundaries = store.match(/activeSubscriptionId: null/g) ?? [];
  assert.ok(boundaries.length >= 4, `expected several subscription boundaries, found ${boundaries.length}`);
  const flushes = store.match(/flushPendingDeltas\(\);/g) ?? [];
  assert.ok(
    flushes.length >= 4,
    `queued deltas must be applied before the subscription they belong to is cleared, found ${flushes.length}`,
  );
  assert.ok(store.includes('discardPendingDeltas();'), 'logout must drop the queue, not apply it');
});

test('no component subscribes to the whole store any more', () => {
  const offenders: string[] = [];
  // Recursive: the components are grouped into directories (the transcript's row kinds
  // live under `transcriptRows/`), and a scan that stopped at the top level would stop
  // covering a component the moment it moved into one.
  const walk = (relative: string): void => {
    for (const entry of readdirSync(join(webRoot, relative), { withFileTypes: true })) {
      const path = `${relative}/${entry.name}`;
      if (entry.isDirectory()) { walk(path); continue; }
      if (!entry.name.endsWith('.tsx')) continue;
      if (read(path).includes('useConsoleStore()')) offenders.push(path);
    }
  };
  walk('src/components');
  assert.deepEqual(
    offenders,
    [],
    'a component that selects no fields re-renders on every store change',
  );
  assert.ok(
    read('src/components/Transcript.tsx').includes('useShallow('),
    'the transcript must select the fields it paints',
  );
});

test('a transcript row that did not grow is not re-rendered', () => {
  const transcript = read('src/components/Transcript.tsx');
  assert.ok(transcript.includes('React.memo('), 'rows must be memoized on the message');
  assert.ok(transcript.includes('<TranscriptRow'), 'the transcript must render the memoized row');
  assert.ok(
    read('src/components/Markdown.tsx').includes('React.memo('),
    'Markdown must not re-render text that did not change',
  );
});

test('Markdown parses off-thread and reuses unchanged block renderers', () => {
  const markdown = read('src/components/Markdown.tsx');
  assert.ok(markdown.includes('new Worker('));
  assert.ok(markdown.includes('MarkdownParseQueue'));
  assert.equal(/\bparseMarkdown\s*\(/.test(markdown), false,
    'never restore a synchronous parser on the terminal/input thread');
  assert.ok(markdown.includes('const MarkdownBlock = React.memo('));
  assert.equal(markdown.includes('startTransition('), false,
    'urgent streaming store updates can starve a transition until the turn ends');
  assert.ok(read('src/markdown/parse.worker.ts').includes('parseMarkdown(data.text)'));
});

test('terminal input uses the serial queue and ignores disposed listeners', () => {
  const terminal = read('src/components/terminal/XtermView.tsx');
  assert.ok(terminal.includes('new TerminalInputQueue('));
  assert.match(terminal, /term\.onData\([\s\S]*?inputQueue\?\.enqueue\(data\)/);
  assert.ok(terminal.includes('inputQueue?.dispose()'));
  assert.ok(terminal.includes('if (disposed)'));
  assert.ok(terminal.includes('cancelAnimationFrame(initialFrame)'));
});