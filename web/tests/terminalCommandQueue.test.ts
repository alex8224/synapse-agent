/**
 * Offline tests for the per-PTY serial command queue.
 *
 * The native terminal commands are async on a blocking worker, so concurrent
 * invokes can reach the PTY out of order. `TerminalCommandQueue` is the bridge
 * level guarantee that every write/resize/close for one PTY runs strictly one at
 * a time, in arrival order, across the component instances that issue them.
 *
 * The contract these tests pin:
 * - one PTY id is a single FIFO lane; a stale resize cannot land after a newer
 *   one, and a close cannot overtake an earlier write
 * - two callers that share an id (e.g. an old and a rebuilt view) serialize
 * - a failed command rejects only its own caller and does not poison the lane
 * - a caller that ignores the returned promise never triggers an unhandled
 *   rejection
 * - different PTY ids run concurrently and never block each other
 * - a settled lane is dropped from the map so per-session state cannot pile up
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { TerminalCommandQueue } from '../src/client/terminalCommandQueue.ts';

/** Let every queued microtask settle (and one macrotask, for good measure). */
const settle = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));

interface Deferred {
  promise: Promise<void>;
  resolve: () => void;
  reject: (reason?: unknown) => void;
}

function deferred(): Deferred {
  let resolve!: () => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<void>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

test('one PTY runs write -> resize -> resize -> write -> close strictly in order', async () => {
  const queue = new TerminalCommandQueue();
  const labels = ['write-1', 'resize-80x24', 'resize-120x30', 'write-2', 'close'];
  const gates = new Map<string, Deferred>();
  const order: string[] = [];
  let inFlight = 0;
  let maxInFlight = 0;

  const promises = labels.map((label) => {
    const gate = deferred();
    gates.set(label, gate);
    return queue.run(7, async () => {
      inFlight += 1;
      maxInFlight = Math.max(maxInFlight, inFlight);
      order.push(`start:${label}`);
      await gate.promise;
      order.push(`end:${label}`);
      inFlight -= 1;
    });
  });

  await settle();
  // Only the head of the lane may have started; nothing overlaps.
  assert.deepEqual(order, ['start:write-1']);
  assert.equal(maxInFlight, 1);
  assert.equal(queue.laneCount, 1);

  // Releasing each command must unblock exactly the next one, in order.
  for (const label of labels) {
    gates.get(label)!.resolve();
    await settle();
  }

  assert.deepEqual(order, [
    'start:write-1',
    'end:write-1',
    'start:resize-80x24',
    'end:resize-80x24',
    'start:resize-120x30',
    'end:resize-120x30',
    'start:write-2',
    'end:write-2',
    'start:close',
    'end:close',
  ]);
  assert.equal(maxInFlight, 1);
  await Promise.all(promises);
});

test('two callers sharing one PTY id serialize on the same lane', async () => {
  const queue = new TerminalCommandQueue();
  const gate = deferred();
  const order: string[] = [];
  let inFlight = 0;
  let maxInFlight = 0;

  const op = (label: string) =>
    queue.run(3, async () => {
      inFlight += 1;
      maxInFlight = Math.max(maxInFlight, inFlight);
      order.push(`start:${label}`);
      await gate.promise;
      order.push(`end:${label}`);
      inFlight -= 1;
    });

  // Simulates a torn-down view and its replacement both targeting PTY 3.
  const fromOldView = op('old-view');
  const fromNewView = op('new-view');

  await settle();
  assert.deepEqual(order, ['start:old-view']);

  gate.resolve();
  await settle();
  assert.deepEqual(order, ['start:old-view', 'end:old-view', 'start:new-view', 'end:new-view']);
  assert.equal(maxInFlight, 1);
  await Promise.all([fromOldView, fromNewView]);
});

test('a failed command rejects its own caller but does not poison the lane', async () => {
  const queue = new TerminalCommandQueue();
  const order: string[] = [];

  const failing = queue.run(5, async () => {
    order.push('write');
    throw new Error('pty gone');
  });
  const following = queue.run(5, async () => {
    order.push('resize');
    return 'ok';
  });

  await assert.rejects(failing, /pty gone/);
  assert.equal(await following, 'ok');
  assert.deepEqual(order, ['write', 'resize']);
});

test('an ignored rejected command never becomes an unhandled rejection', async () => {
  const unhandled: unknown[] = [];
  const onUnhandled = (reason: unknown): void => {
    unhandled.push(reason);
  };
  process.on('unhandledRejection', onUnhandled);
  try {
    const queue = new TerminalCommandQueue();
    // The caller (e.g. a fire-and-forget resize) does not attach a handler.
    queue.run(1, () => Promise.reject(new Error('boom')));
    await settle();
    assert.deepEqual(unhandled, []);
  } finally {
    process.off('unhandledRejection', onUnhandled);
  }
});

test('different PTY ids run concurrently and never block each other', async () => {
  const queue = new TerminalCommandQueue();
  const gate = deferred();
  const order: string[] = [];
  let inFlight = 0;
  let maxInFlight = 0;

  const op = (id: number, label: string) =>
    queue.run(id, async () => {
      inFlight += 1;
      maxInFlight = Math.max(maxInFlight, inFlight);
      order.push(`start:${label}`);
      await gate.promise;
      order.push(`end:${label}`);
      inFlight -= 1;
    });

  const a = op(1, 'pty-1');
  const b = op(2, 'pty-2');

  await settle();
  assert.deepEqual(order, ['start:pty-1', 'start:pty-2']);
  assert.equal(maxInFlight, 2);
  assert.equal(queue.laneCount, 2);

  gate.resolve();
  await settle();
  await Promise.all([a, b]);
});

test('a settled lane is removed from the map; a live lane is kept', async () => {
  const queue = new TerminalCommandQueue();
  const gate = deferred();

  const pending = queue.run(1, () => gate.promise);
  await settle();
  assert.equal(queue.laneCount, 1);

  gate.resolve();
  await settle();
  assert.equal(queue.laneCount, 0);

  await pending;
});

test('a lane with a queued successor is not cleaned up early', async () => {
  const queue = new TerminalCommandQueue();
  const gate = deferred();

  const head = queue.run(9, () => gate.promise);
  const successor = queue.run(9, () => Promise.resolve());
  await settle();
  // The successor has replaced the tail, so the lane must still be present.
  assert.equal(queue.laneCount, 1);

  gate.resolve();
  await settle();
  assert.equal(queue.laneCount, 0);

  await Promise.all([head, successor]);
});
