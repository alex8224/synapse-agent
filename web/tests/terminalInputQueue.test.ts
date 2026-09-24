/**
 * Offline tests for the serial PTY input queue.
 *
 * The queue exists because the native write command is async on a blocking
 * worker: concurrent invokes can reach the PTY out of order. The contract these
 * tests pin is narrow and absolute — input is written one call at a time, in
 * arrival order, never dropped and never reordered, a failed write does not
 * strand the input behind it, and `dispose` stops the queue without leaking the
 * input it never sent.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { TerminalInputQueue } from '../src/components/terminal/terminalInputQueue.ts';

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

test('the first keystroke is written on its own, before anything merges', async () => {
  const gate = deferred();
  const calls: string[] = [];
  const queue = new TerminalInputQueue((data) => {
    calls.push(data);
    return gate.promise;
  });

  queue.enqueue('ls');
  queue.enqueue(' ');
  queue.enqueue('-la');

  // The write is already in flight; the rest is still pending, so it must not
  // have been sent yet (and must not have been folded into the sent chunk).
  assert.deepEqual(calls, ['ls']);
  assert.equal(queue.pendingLength, 4);

  gate.resolve();
  await settle();

  // ` -la` merges into one write, but the bytes and their order are intact.
  assert.deepEqual(calls, ['ls', ' -la']);
  assert.equal(calls.join(''), 'ls -la');
});

test('a write never starts before the previous one settles', async () => {
  const gate = deferred();
  const order: string[] = [];
  let inFlight = 0;
  let maxInFlight = 0;
  const queue = new TerminalInputQueue(async (data) => {
    inFlight += 1;
    maxInFlight = Math.max(maxInFlight, inFlight);
    order.push(`start:${data}`);
    if (data === 'a') await gate.promise;
    order.push(`end:${data}`);
    inFlight -= 1;
  });

  queue.enqueue('a');
  queue.enqueue('b');
  await settle();
  assert.deepEqual(order, ['start:a']);

  gate.resolve();
  await settle();
  assert.equal(maxInFlight, 1);
  assert.deepEqual(order, ['start:a', 'end:a', 'start:b', 'end:b']);
});

test('a failed write is reported and does not strand the input behind it', async () => {
  const errors: unknown[] = [];
  const calls: string[] = [];
  const queue = new TerminalInputQueue(
    (data) => {
      calls.push(data);
      return data === 'bad' ? Promise.reject(new Error('pty gone')) : Promise.resolve();
    },
    { onError: (error) => errors.push(error) },
  );

  queue.enqueue('bad');
  queue.enqueue('next');
  await settle();

  assert.deepEqual(calls, ['bad', 'next']);
  assert.equal(errors.length, 1);
  assert.match(String(errors[0]), /pty gone/);
});

test('a rejected write never becomes an unhandled rejection', async () => {
  const unhandled: unknown[] = [];
  const onUnhandled = (reason: unknown): void => {
    unhandled.push(reason);
  };
  process.on('unhandledRejection', onUnhandled);
  try {
    const queue = new TerminalInputQueue(() => Promise.reject(new Error('boom')));
    queue.enqueue('x');
    await settle();
    assert.deepEqual(unhandled, []);
  } finally {
    process.off('unhandledRejection', onUnhandled);
  }
});

test('dispose drops the unsent input and refuses later keystrokes', async () => {
  const gate = deferred();
  const calls: string[] = [];
  const queue = new TerminalInputQueue((data) => {
    calls.push(data);
    return gate.promise;
  });

  queue.enqueue('sent');
  queue.enqueue('dropped');
  queue.dispose();
  gate.resolve();
  await settle();

  queue.enqueue('after-dispose');
  await settle();

  assert.deepEqual(calls, ['sent']);
  assert.equal(queue.pendingLength, 0);
});

test('dispose keeps the in-flight failure reportable but writes nothing more', async () => {
  const gate = deferred();
  const errors: unknown[] = [];
  const calls: string[] = [];
  const queue = new TerminalInputQueue(
    (data) => {
      calls.push(data);
      return gate.promise;
    },
    { onError: (error) => errors.push(error) },
  );

  queue.enqueue('sent');
  queue.enqueue('dropped');
  queue.dispose();
  gate.reject(new Error('closed'));
  await settle();

  assert.deepEqual(calls, ['sent']);
  assert.equal(errors.length, 1);
});

test('empty input is a no-op', async () => {
  const calls: string[] = [];
  const queue = new TerminalInputQueue((data) => {
    calls.push(data);
    return Promise.resolve();
  });

  queue.enqueue('');
  await settle();
  assert.deepEqual(calls, []);
});

test('a throwing error reporter neither strands input nor rejects', async () => {
  const unhandled: unknown[] = [];
  const onUnhandled = (reason: unknown): void => {
    unhandled.push(reason);
  };
  process.on('unhandledRejection', onUnhandled);
  try {
    const calls: string[] = [];
    const queue = new TerminalInputQueue(
      (data) => {
        calls.push(data);
        return data === 'bad' ? Promise.reject(new Error('pty gone')) : Promise.resolve();
      },
      {
        onError: () => {
          throw new Error('reporter blew up');
        },
      },
    );

    queue.enqueue('bad');
    queue.enqueue('next');
    await settle();

    assert.deepEqual(calls, ['bad', 'next']);
    assert.deepEqual(unhandled, []);
  } finally {
    process.off('unhandledRejection', onUnhandled);
  }
});
