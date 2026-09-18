/**
 * What one streamed reasoning turn costs the console store.
 *
 * Not part of `npm test` (the runner only globs `tests/*.test.ts`): this is the
 * measuring instrument for the streaming-cost work, run on demand.
 *
 * It folds the same event stream through the two paths and reports what each one
 * costs, so the difference is visible without a live model:
 *
 *   per-event  one `setState` per chunk -- the path before delta batching
 *   coalesced  the same chunks merged over the display window (`DELTA_COALESCE_MS`)
 *
 * The subscriber stands in for one React commit: it re-parses the Markdown of the
 * row that grew (what `Markdown` does when its `text` prop changes) and runs the
 * unmemoized transcript scans (`TurnRail`, `TodoPanel`).
 *
 * Usage:
 *   node --expose-gc tests/streamingDelta.bench.ts [--deltas=4000] [--rate=250]
 */
import { createStore } from 'zustand/vanilla';
import { parseMarkdown } from '../src/markdown/parse.ts';
import {
  coalesceLiveEvents,
  DELTA_COALESCE_MS,
  foldLiveEvents,
  type LiveEventEntry,
} from '../src/stores/liveDeltaBatch.ts';
import type { LiveReducibleState } from '../src/stores/liveEventReducer.ts';
import type { TranscriptMessage } from '../src/stores/historyMapper.ts';
import type { RuntimeEvent } from '../src/client/types.ts';

function numberArg(name: string, fallback: number): number {
  const raw = process.argv.find((arg) => arg.startsWith(`--${name}=`));
  if (raw === undefined) return fallback;
  const value = Number(raw.slice(name.length + 3));
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

/** Provider chunk size (characters of reasoning text per event). */
const CHUNK_CHARS = numberArg('chars', 8);
/** Reasoning chunks in the modelled turn. */
const DELTAS = numberArg('deltas', 4000);
/** Chunks per second of wall time (a fast reasoning stream). */
const RATE = numberArg('rate', 250);
/** Rows already in the transcript, so the reducer's scans are O(n). */
const PRIOR_MESSAGES = numberArg('prior', 200);

/** Chunks that arrive inside one display window. */
const CHUNKS_PER_WINDOW = Math.max(1, Math.round((RATE * DELTA_COALESCE_MS) / 1000));

function priorMessages(count: number): TranscriptMessage[] {
  const messages: TranscriptMessage[] = [];
  for (let i = 0; i < count; i += 1) {
    const turn = `p${Math.floor(i / 3)}`;
    const role = i % 3;
    if (role === 0) {
      messages.push({ id: `user-${turn}`, type: 'user', content: `question ${turn}`, timestamp: '00:00' });
    } else if (role === 1) {
      messages.push({
        id: `thought-${turn}-1`,
        type: 'thought',
        content: 'x'.repeat(2000),
        timestamp: '00:00',
        duration: '2.0s',
        expanded: false,
      });
    } else {
      messages.push({
        id: `ans-${turn}-1`,
        type: 'assistant',
        content: 'y'.repeat(2000),
        timestamp: '00:00',
        streaming: false,
      });
    }
  }
  return messages;
}

function baseState(): LiveReducibleState {
  return {
    messages: priorMessages(PRIOR_MESSAGES),
    activeTurnId: 't1',
    runtimeStatus: 'running',
    steerQueueCount: 0,
    pendingApproval: null,
    activity: { phase: 'thinking', detail: '', startedAt: 0, active: true },
    usage: null,
    metricsLabel: '',
  };
}

function reasoningDelta(sequence: number, text: string): RuntimeEvent {
  return {
    sequence,
    turn_sequence: sequence,
    turn_id: 't1',
    kind: 'reasoning_delta',
    payload: { text },
    version: 1,
  };
}

interface Metrics {
  mode: string;
  storeUpdates: number;
  notifications: number;
  parseCalls: number;
  parsedChars: number;
  foldMs: number;
  parseMs: number;
  totalMs: number;
  heapDeltaBytes: number;
}

function run(mode: 'per-event' | 'coalesced'): Metrics {
  const store = createStore<LiveReducibleState>(() => baseState());
  let notifications = 0;
  let parseCalls = 0;
  let parsedChars = 0;
  let parseMs = 0;

  // One React commit for the row that grew, plus the unmemoized transcript scans.
  store.subscribe((state) => {
    notifications += 1;
    const last = state.messages[state.messages.length - 1];
    const text = last?.content ?? '';
    if (text.length > 0) {
      const started = performance.now();
      parseMarkdown(text);
      parseMs += performance.now() - started;
      parseCalls += 1;
      parsedChars += text.length;
    }
    let seen = 0;
    for (const message of state.messages) if (message.type === 'user') seen += 1;
    if (seen < 0) throw new Error('unreachable');
  });

  globalThis.gc?.();
  const heapBefore = process.memoryUsage().heapUsed;
  const startedAt = performance.now();
  let foldMs = 0;
  let storeUpdates = 0;
  const queue: LiveEventEntry[] = [];

  for (let i = 0; i < DELTAS; i += 1) {
    const entry: LiveEventEntry = {
      event: reasoningDelta(i + 1, 'z'.repeat(CHUNK_CHARS)),
      subscription_id: 's1',
    };
    if (mode === 'per-event') {
      const foldStarted = performance.now();
      store.setState((state) => foldLiveEvents(state, [entry]));
      foldMs += performance.now() - foldStarted;
      storeUpdates += 1;
      continue;
    }
    queue.push(entry);
    const last = i === DELTAS - 1;
    if (queue.length < CHUNKS_PER_WINDOW && !last) continue;
    const merged = coalesceLiveEvents(queue);
    queue.length = 0;
    const foldStarted = performance.now();
    store.setState((state) => foldLiveEvents(state, merged));
    foldMs += performance.now() - foldStarted;
    storeUpdates += 1;
  }

  const totalMs = performance.now() - startedAt;
  const heapDeltaBytes = process.memoryUsage().heapUsed - heapBefore;
  return {
    mode,
    storeUpdates,
    notifications,
    parseCalls,
    parsedChars,
    foldMs,
    parseMs,
    totalMs,
    heapDeltaBytes,
  };
}

function mb(bytes: number): string {
  return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
}

const streamSeconds = DELTAS / RATE;
const rows = [run('per-event'), run('coalesced')];

console.log(
  `\nreasoning turn: ${DELTAS} chunks x ${CHUNK_CHARS} chars ` +
    `(${DELTAS * CHUNK_CHARS} chars), ${RATE} chunks/s -> ${streamSeconds.toFixed(1)}s of stream, ` +
    `${PRIOR_MESSAGES} rows already in the transcript, window ${DELTA_COALESCE_MS}ms ` +
    `(${CHUNKS_PER_WINDOW} chunks)\n`,
);

const header = ['mode', 'store updates', 'commits', 'md parses', 'parsed chars', 'fold ms', 'parse ms', 'total ms', 'ms/stream-s', 'heap'];
console.log(header.join(' | '));
for (const row of rows) {
  console.log(
    [
      row.mode,
      String(row.storeUpdates),
      String(row.notifications),
      String(row.parseCalls),
      String(row.parsedChars),
      row.foldMs.toFixed(0),
      row.parseMs.toFixed(0),
      row.totalMs.toFixed(0),
      (row.totalMs / streamSeconds).toFixed(1),
      mb(row.heapDeltaBytes),
    ].join(' | '),
  );
}
