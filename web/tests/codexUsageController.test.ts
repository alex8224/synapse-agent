
test('a model switch is re-gated when the rebind is confirmed, not on the publish', async () => {
  // `useConsoleStore.setModel` publishes the target model optimistically, *before*
  // its `runtime.session.rebind` lands, so a gate issued on that publish is answered
  // by the profile the session is still bound to.  The confirmed value is the same
  // string, so `model` alone would never notify this controller again: the entry
  // would stay hidden until the context changed for some unrelated reason (a reload
  // in practice).  The confirmed-binding revision is what closes that gap.
  const h = build();
  h.transport.configReply = { current_model: 'deepseek-v4-flash', codex_usage_enabled: false };
  h.controller.start();
  h.context.set({ connected: true });
  await flush();
  assert.equal(h.last().available, false);
  assert.equal(h.transport.configCalls.length, 1);

  // The optimistic publish: a new context, but the server has not rebound yet.
  h.context.set({ model: 'codex-gpt-5.6-sol' });
  await flush();
  assert.equal(h.transport.configCalls.length, 2);
  assert.equal(h.last().available, false, 'that verdict describes the previous profile');
  assert.equal(h.transport.usageCalls.length, 0);

  // The rebind lands: the model string is unchanged, only the revision moves.
  const usage = deferred<CodexUsageView>();
  h.transport.usageReplies.push(usage);
  h.transport.configReply = { current_model: 'codex-gpt-5.6-sol', codex_usage_enabled: true };
  h.context.set({ revision: 1 });
  await flush();
  assert.equal(h.transport.configCalls.length, 3, 'the confirmed binding is re-asked');
  assert.equal(h.last().available, true);
  assert.equal(h.transport.usageCalls.length, 1);
  usage.resolve(USAGE('codex-gpt-5.6-sol'));
  await flush();
  assert.equal(h.last().usage?.model, 'codex-gpt-5.6-sol');
});
/**
 * Behaviour tests for the Codex usage controller.
 *
 * The controller is the only place that decides *whether* the entry exists, what
 * it shows and whether a redeem may be sent, and every one of those decisions
 * depends on timing: a response that resolves after the user switched model, a
 * timer that fires while a read is in flight, a confirmation raised in a context
 * that has already moved on.  So it is built with injected time, timers, ids and
 * transport, and driven here with a fake clock and deferred RPC replies — no DOM,
 * no zustand, no WebSocket, and above all no real credit is ever consumed.
 *
 * What is pinned:
 *
 *  - the entry is hidden until the *server* confirms an enabled OAuth profile, and
 *    a non-OAuth profile (or a peer without the new field) never triggers a usage
 *    RPC;
 *  - the controller is started by its availability source, so it discovers the
 *    verdict while the entry is still hidden (no Trigger-mount deadlock);
 *  - every observable change bumps an epoch, so `A -> B -> A`, a reconnect on the
 *    same client and a logout all discard the older reply;
 *  - reads are single-flight and 300s-cached, and the periodic tick is skipped
 *    while the page is hidden;
 *  - redeeming is two-step: raising the confirmation sends nothing, cancelling
 *    sends nothing, and only confirming sends the one write — with `confirmed`
 *    true and the command id minted for that confirmation;
 *  - an unknown outcome or a dropped connection is never retried and never
 *    replayed under a fresh command id; a spent credit leaves the painted list
 *    before the refresh is awaited, so it cannot be offered twice.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { CodexUsageController } from '../src/stores/codexUsageController.ts';
import type {
  CodexUsageContext,
  CodexUsagePort,
  CodexUsageState,
} from '../src/stores/codexUsageController.ts';
import type {
  CodexConsumeResult,
  CodexResetCreditsView,
  CodexUsageView,
  ConsumeCodexResetParams,
} from '../src/runtime-client/codexUsage.ts';

const SESSION = { project_id: 'proj', thread_id: 'thr' };
const START_MS = 1_700_000_000_000;

const USAGE = (model: string, capturedAt = 1_799_900_000): CodexUsageView => ({
  session: SESSION,
  model,
  primary: { used_percent: 18, window_minutes: 300, reset_at: 1_800_000_000 },
  secondary: { used_percent: 40, window_minutes: 10080, reset_at: 1_800_500_000 },
  captured_at: capturedAt,
  available_reset_count: 2,
});

const CREDITS: CodexResetCreditsView = {
  session: SESSION,
  model: 'gpt-5-codex',
  available_count: 2,
  credits: [
    {
      id: 'credit-a',
      reset_type: 'weekly',
      status: 'available',
      granted_at: 1_799_000_000,
      expires_at: null,
      title: 'Weekly',
      description: null,
    },
    {
      id: 'credit-b',
      reset_type: 'weekly',
      status: 'available',
      granted_at: 1_799_000_000,
      // Already past the fake clock's now (1_700_000_000s), so it is not offered.
      expires_at: 1_699_000_000,
      title: null,
      description: null,
    },
  ],
};

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** Fake transport: every call is recorded and answered from a scripted queue. */
class FakeTransport implements CodexUsagePort {
  configCalls: Array<{ session: typeof SESSION }> = [];
  usageCalls: Array<{ session: typeof SESSION; force: boolean }> = [];
  creditsCalls: Array<{ session: typeof SESSION; force: boolean }> = [];
  consumeCalls: ConsumeCodexResetParams[] = [];

  configReply: unknown = { current_model: 'gpt-5-codex', codex_usage_enabled: true };
  configReplies: Array<Deferred<unknown>> = [];
  usageReplies: Array<Deferred<CodexUsageView>> = [];
  creditsReplies: Array<Deferred<CodexResetCreditsView>> = [];
  consumeReplies: Array<Deferred<CodexConsumeResult>> = [];

  async getRuntimeConfig(params: { session: typeof SESSION }): Promise<unknown> {
    this.configCalls.push(params);
    const queued = this.configReplies.shift();
    return queued === undefined ? this.configReply : queued.promise;
  }

  async getCodexUsage(session: typeof SESSION, force = false): Promise<CodexUsageView> {
    this.usageCalls.push({ session, force });
    const queued = this.usageReplies.shift();
    if (queued === undefined) throw new Error('no scripted usage reply');
    return queued.promise;
  }

  async getCodexResetCredits(
    session: typeof SESSION,
    force = false,
  ): Promise<CodexResetCreditsView> {
    this.creditsCalls.push({ session, force });
    const queued = this.creditsReplies.shift();
    if (queued === undefined) throw new Error('no scripted credits reply');
    return queued.promise;
  }

  async consumeCodexResetCredit(params: ConsumeCodexResetParams): Promise<CodexConsumeResult> {
    this.consumeCalls.push(params);
    const queued = this.consumeReplies.shift();
    if (queued === undefined) throw new Error('no scripted consume reply');
    return queued.promise;
  }
}

/** A mutable context plus the change notifications the controller subscribes to. */
class ContextHarness {
  context: CodexUsageContext;
  listeners = new Set<() => void>();

  constructor(transport: FakeTransport | null, model = 'gpt-5-codex', connected = false) {
    this.context = { client: transport, session: { ...SESSION }, model, revision: 0, connected };
  }

  get = (): CodexUsageContext => this.context;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  set(patch: Partial<CodexUsageContext>): void {
    this.context = { ...this.context, ...patch };
    for (const listener of this.listeners) listener();
  }
}

/** A fake clock: the controller's own timer port, advanced by hand. */
class FakeClock {
  ms = START_MS;
  pending: Array<{ handle: number; run: () => void }> = [];
  private next = 0;

  now = (): number => this.ms;
  schedule = (run: () => void, _delayMs: number): number => {
    const handle = ++this.next;
    this.pending.push({ handle, run });
    return handle;
  };
  cancel = (handle: number): void => {
    this.pending = this.pending.filter((timer) => timer.handle !== handle);
  };

  advance(ms: number): void {
    this.ms += ms;
  }

  /** Fire every timer that is currently armed, once each. */
  async fire(): Promise<void> {
    const due = this.pending;
    this.pending = [];
    for (const timer of due) timer.run();
    await flush();
  }
}

/** Let every already-resolved promise chain run to completion. */
async function flush(): Promise<void> {
  for (let i = 0; i < 6; i++) await new Promise((resolve) => setTimeout(resolve, 0));
}

interface Harness {
  controller: CodexUsageController;
  clock: FakeClock;
  context: ContextHarness;
  transport: FakeTransport;
  states: CodexUsageState[];
  last: () => CodexUsageState;
}

function build(options: { visible?: () => boolean; cacheTtlMs?: number } = {}): Harness {
  const transport = new FakeTransport();
  const clock = new FakeClock();
  const context = new ContextHarness(transport);
  const states: CodexUsageState[] = [];
  let ids = 0;
  const controller = new CodexUsageController({
    read: context.get,
    subscribe: context.subscribe,
    onState: (state) => states.push(state),
    now: clock.now,
    schedule: clock.schedule,
    cancel: clock.cancel,
    newCommandId: () => `cmd-${++ids}`,
    isVisible: options.visible ?? (() => true),
    cacheTtlMs: options.cacheTtlMs,
    refreshIntervalMs: 1000,
  });
  return {
    controller,
    clock,
    context,
    transport,
    states,
    last: () => controller.getState(),
  };
}

/** Connect, confirm the gate, and resolve one usage read. */
async function openWithUsage(h: Harness, usage = USAGE('gpt-5-codex')): Promise<void> {
  const usageReply = deferred<CodexUsageView>();
  h.transport.usageReplies.push(usageReply);
  h.context.set({ connected: true });
  await flush();
  assert.equal(h.transport.configCalls.length, 1, 'the gate is read once');
  assert.equal(h.transport.usageCalls.length, 1, 'and then the usage read');
  usageReply.resolve(usage);
  await flush();
  assert.equal(h.last().available, true);
}

// --- discovery --------------------------------------------------------------

test('the entry stays hidden until the server confirms an enabled OAuth profile', async () => {
  const h = build();
  h.controller.start();
  assert.equal(h.last().available, false, 'no client yet');
  assert.equal(h.transport.configCalls.length, 0, 'and nothing was asked');

  await openWithUsage(h);
  assert.equal(h.last().usage?.model, 'gpt-5-codex');
  assert.equal(h.transport.configCalls.length, 1);
});

test('a non-OAuth profile is judged once and never sends a usage RPC', async () => {
  const h = build();
  h.transport.configReply = { current_model: 'claude-sonnet', codex_usage_enabled: false };
  h.controller.start();
  h.context.set({ connected: true });
  await flush();
  assert.equal(h.last().available, false);
  assert.equal(h.transport.configCalls.length, 1);
  assert.equal(h.transport.usageCalls.length, 0, 'no usage RPC for a non-OAuth profile');

  // The periodic tick must not re-ask a question the server already answered.
  await h.clock.fire();
  await h.clock.fire();
  assert.equal(h.transport.configCalls.length, 1);
  assert.equal(h.transport.usageCalls.length, 0);
});

test('a peer that predates the gate field hides the entry and sends no usage RPC', async () => {
  const h = build();
  h.transport.configReply = { current_model: 'gpt-5-codex' };
  h.controller.start();
  h.context.set({ connected: true });
  await flush();
  assert.equal(h.last().available, false);
  assert.equal(h.transport.usageCalls.length, 0);
});

test('a failed gate read is retried by the tick, and only the unanswered gate is', async () => {
  const h = build();
  h.transport.configReplies.push({
    promise: Promise.reject(new Error('boom')),
    resolve: () => {},
    reject: () => {},
  } as Deferred<unknown>);
  h.controller.start();
  h.context.set({ connected: true });
  await flush();
  assert.equal(h.last().available, false);
  assert.equal(h.transport.configCalls.length, 1);

  await h.clock.fire();
  assert.equal(h.transport.configCalls.length, 2, 'the unanswered gate is retried');
});

test('the availability source starts the controller, so a hidden entry can appear', async () => {
  // The controller is only started through `subscribe`, exactly as the strip's
  // availability source does it: nothing here mounts a Trigger.
  const h = build();
  const unsubscribe = h.context.subscribe(() => h.controller.start());
  unsubscribe();
  h.controller.start();
  h.context.set({ connected: true });
  await flush();
  assert.equal(h.transport.configCalls.length, 1);
});

// --- epochs -----------------------------------------------------------------

test('A -> B -> A discards the older reply instead of painting it', async () => {
  const h = build();
  h.controller.start();
  const firstA = deferred<CodexUsageView>();
  h.transport.usageReplies.push(firstA);
  h.context.set({ connected: true });
  await flush();
  assert.equal(h.transport.usageCalls.length, 1);

  // Switch to B, then back to A before the first A reply lands.
  const firstB = deferred<CodexUsageView>();
  h.transport.usageReplies.push(firstB);
  h.context.set({ model: 'model-b' });
  await flush();
  assert.equal(h.transport.usageCalls.length, 2);

  const secondA = deferred<CodexUsageView>();
  h.transport.usageReplies.push(secondA);
  h.context.set({ model: 'gpt-5-codex' });
  await flush();
  assert.equal(h.transport.usageCalls.length, 3);

  // The stale A reply is dropped even though the context is A again.
  firstA.resolve(USAGE('gpt-5-codex', 1));
  firstB.resolve(USAGE('model-b', 2));
  await flush();
  assert.equal(h.last().usage, null, 'an older epoch never paints');

  secondA.resolve(USAGE('gpt-5-codex', 3));
  await flush();
  assert.equal(h.last().usage?.captured_at, 3, 'only the newest epoch paints');
});

test('a reconnect on the same client discards the reply from before the drop', async () => {
  const h = build();
  h.controller.start();
  const before = deferred<CodexUsageView>();
  h.transport.usageReplies.push(before);
  h.context.set({ connected: true });
  await flush();
  assert.equal(h.transport.usageCalls.length, 1);

  h.context.set({ connected: false });
  await flush();
  assert.equal(h.last().available, false, 'a dropped connection hides the entry');

  const after = deferred<CodexUsageView>();
  h.transport.usageReplies.push(after);
  h.context.set({ connected: true });
  await flush();
  assert.equal(h.transport.usageCalls.length, 2);

  before.resolve(USAGE('gpt-5-codex', 1));
  await flush();
  assert.equal(h.last().usage, null);
  after.resolve(USAGE('gpt-5-codex', 2));
  await flush();
  assert.equal(h.last().usage?.captured_at, 2);
});

test('a logout (a new client) drops the previous client\'s results', async () => {
  const h = build();
  h.controller.start();
  await openWithUsage(h);
  assert.notEqual(h.last().usage, null);

  const replacement = new FakeTransport();
  const replacementUsage = deferred<CodexUsageView>();
  replacement.usageReplies.push(replacementUsage);
  h.context.set({ client: replacement });
  // The context change is applied synchronously, so the previous client's view is
  // gone before the replacement's read is even issued.
  assert.equal(h.last().usage, null, 'the previous client\'s view is gone');
  await flush();
  assert.equal(replacement.configCalls.length, 1, 'the new client is gated again');
  assert.equal(replacement.usageCalls.length, 1);
  replacementUsage.resolve(USAGE('gpt-5-codex', 9));
  await flush();
  assert.equal(h.last().usage?.captured_at, 9, 'and the replacement\'s own read paints');
});

test('the effective model the server reports is what the panel names', async () => {
  const h = build();
  h.transport.configReply = { current_model: 'gpt-5.1-codex', codex_usage_enabled: true };
  h.controller.start();
  h.transport.usageReplies.push(deferred<CodexUsageView>());
  h.context.set({ connected: true });
  await flush();
  assert.equal(h.last().model, 'gpt-5.1-codex');
});

// --- cache and timers -------------------------------------------------------

test('the read cache is 300s, and the tick never stacks a second read', async () => {
  const h = build();
  h.controller.start();
  await openWithUsage(h);
  assert.equal(h.transport.usageCalls.length, 1);

  // Inside the TTL the tick is a no-op.
  h.clock.advance(100_000);
  await h.clock.fire();
  assert.equal(h.transport.usageCalls.length, 1, 'a cached read is not repeated');

  // Past the TTL it refreshes exactly once.
  h.clock.advance(201_000);
  const inFlight = deferred<CodexUsageView>();
  h.transport.usageReplies.push(inFlight);
  await h.clock.fire();
  assert.equal(h.transport.usageCalls.length, 2);
  await h.clock.fire();
  await h.clock.fire();
  assert.equal(h.transport.usageCalls.length, 2, 'an in-flight read is not stacked');
  inFlight.resolve(USAGE('gpt-5-codex', 5));
  await flush();
  assert.equal(h.last().usage?.captured_at, 5);
});

test('a hidden page sends nothing on the tick', async () => {
  let visible = false;
  const h = build({ visible: () => visible });
  h.controller.start();
  await openWithUsage(h);
  h.clock.advance(400_000);

  await h.clock.fire();
  assert.equal(h.transport.usageCalls.length, 1, 'hidden: no background read');
  visible = true;
  await h.clock.fire();
  assert.equal(h.transport.usageCalls.length, 2, 'visible again: the TTL is honoured');
});

test('stop() cancels the timer and the subscription', async () => {
  const h = build();
  h.controller.start();
  await openWithUsage(h);
  h.controller.stop();
  assert.equal(h.clock.pending.length, 0, 'the timer is cancelled');

  h.context.set({ model: 'other-model' });
  await flush();
  assert.equal(h.transport.configCalls.length, 1, 'no longer observing the context');
});

// --- credits ----------------------------------------------------------------

test('opening the panel loads the credits once, and the refresh control forces both', async () => {
  const h = build();
  h.controller.start();
  await openWithUsage(h);

  const credits = deferred<CodexResetCreditsView>();
  h.transport.creditsReplies.push(credits);
  h.controller.openPanel();
  await flush();
  assert.equal(h.transport.creditsCalls.length, 1);
  assert.equal(h.transport.creditsCalls[0].force, false);
  credits.resolve(CREDITS);
  await flush();
  assert.equal(h.last().credits?.available_count, 2);

  h.controller.openPanel();
  await flush();
  assert.equal(h.transport.creditsCalls.length, 1, 'the cache covers the second open');

  const usageReply = deferred<CodexUsageView>();
  const creditsReply = deferred<CodexResetCreditsView>();
  h.transport.usageReplies.push(usageReply);
  h.transport.creditsReplies.push(creditsReply);
  h.controller.refresh();
  await flush();
  // `refreshAll` reads the windows first and the rows after, so the credits call
  // is only issued once the usage read has settled.
  assert.deepEqual(
    h.transport.usageCalls.map((call) => call.force),
    [false, true],
  );
  usageReply.resolve(USAGE('gpt-5-codex', 6));
  await flush();
  assert.deepEqual(
    h.transport.creditsCalls.map((call) => call.force),
    [false, true],
  );
  creditsReply.resolve({ ...CREDITS, available_count: 3, credits: [] });
  await flush();
  assert.equal(h.last().usage?.captured_at, 6);
  assert.equal(h.last().credits?.available_count, 3);
});

// --- the confirmation -------------------------------------------------------

test('raising and cancelling the confirmation sends nothing at all', async () => {
  const h = build();
  h.controller.start();
  await openWithUsage(h);
  const credits = deferred<CodexResetCreditsView>();
  h.transport.creditsReplies.push(credits);
  h.controller.openPanel();
  await flush();
  credits.resolve(CREDITS);
  await flush();

  h.controller.requestReset('credit-a');
  assert.equal(h.last().pending?.creditId, 'credit-a');
  assert.equal(h.last().pending?.commandId, 'cmd-1');
  assert.equal(h.transport.consumeCalls.length, 0, 'a confirmation is not a request');

  h.controller.cancelReset();
  assert.equal(h.last().pending, null);
  assert.equal(h.transport.consumeCalls.length, 0, 'cancelling is not a request');
});

test('a credit that is not available, or already expired, cannot be raised', async () => {
  const h = build();
  h.controller.start();
  await openWithUsage(h);
  const credits = deferred<CodexResetCreditsView>();
  h.transport.creditsReplies.push(credits);
  h.controller.openPanel();
  await flush();
  credits.resolve(CREDITS);
  await flush();

  // `credit-b` expires before the clock's now.
  h.controller.requestReset('credit-b');
  assert.equal(h.last().pending, null, 'an expired credit is never offered');
  h.controller.requestReset('missing');
  assert.equal(h.last().pending, null);
  assert.equal(h.transport.consumeCalls.length, 0);
});

test('confirming sends exactly one write, with `confirmed` and the minted command id', async () => {
  const h = build();
  h.controller.start();
  await openWithUsage(h);
  const credits = deferred<CodexResetCreditsView>();
  h.transport.creditsReplies.push(credits);
  h.controller.openPanel();
  await flush();
  credits.resolve(CREDITS);
  await flush();

  h.controller.requestReset('credit-a');
  const consume = deferred<CodexConsumeResult>();
  h.transport.consumeReplies.push(consume);
  h.controller.confirmReset();
  assert.equal(h.transport.consumeCalls.length, 1);
  assert.deepEqual(h.transport.consumeCalls[0], {
    session: SESSION,
    expected_model: 'gpt-5-codex',
    credit_id: 'credit-a',
    command_id: 'cmd-1',
    confirmed: true,
  });
  // A double click (or a slow network) must not reach a second write.
  h.controller.confirmReset();
  h.controller.requestReset('credit-a');
  assert.equal(h.transport.consumeCalls.length, 1);

  // The spent credit is dropped before the refresh is awaited, so the awaiting
  // rows can never offer it again.
  const usageReply = deferred<CodexUsageView>();
  const creditsReply = deferred<CodexResetCreditsView>();
  h.transport.usageReplies.push(usageReply);
  h.transport.creditsReplies.push(creditsReply);
  consume.resolve({ session: SESSION, model: 'gpt-5-codex', command_id: 'cmd-1', outcome: 'reset' });
  await flush();
  assert.deepEqual(
    h.last().credits?.credits.map((credit) => credit.id),
    ['credit-b'],
  );
  assert.equal(h.last().credits?.available_count, 1);
  assert.equal(h.last().lastOutcome, 'reset');

  usageReply.resolve(USAGE('gpt-5-codex', 7));
  creditsReply.resolve({ ...CREDITS, available_count: 1, credits: [CREDITS.credits[1]] });
  await flush();
  assert.match(h.last().notice ?? '', /已兑换 1 次重置额度/);
});

test('a successful redeem whose refresh fails says which half succeeded', async () => {
  const h = build();
  h.controller.start();
  await openWithUsage(h);
  const credits = deferred<CodexResetCreditsView>();
  h.transport.creditsReplies.push(credits);
  h.controller.openPanel();
  await flush();
  credits.resolve(CREDITS);
  await flush();

  h.controller.requestReset('credit-a');
  const consume = deferred<CodexConsumeResult>();
  h.transport.consumeReplies.push(consume);
  h.controller.confirmReset();

  h.transport.usageReplies.push({ promise: Promise.reject(new Error('read failed')), resolve: () => {}, reject: () => {} } as Deferred<CodexUsageView>);
  h.transport.creditsReplies.push({ promise: Promise.reject(new Error('read failed')), resolve: () => {}, reject: () => {} } as Deferred<CodexResetCreditsView>);
  consume.resolve({ session: SESSION, model: 'gpt-5-codex', command_id: 'cmd-1', outcome: 'reset' });
  await flush();
  assert.match(h.last().notice ?? '', /兑换已成功，但刷新失败/);
  assert.equal(h.last().lastOutcome, 'reset');
});

test('an already-redeemed outcome refreshes the rows and never retries', async () => {
  const h = build();
  h.controller.start();
  await openWithUsage(h);
  const credits = deferred<CodexResetCreditsView>();
  h.transport.creditsReplies.push(credits);
  h.controller.openPanel();
  await flush();
  credits.resolve(CREDITS);
  await flush();

  h.controller.requestReset('credit-a');
  const consume = deferred<CodexConsumeResult>();
  h.transport.consumeReplies.push(consume);
  h.controller.confirmReset();

  const refreshed = deferred<CodexResetCreditsView>();
  h.transport.creditsReplies.push(refreshed);
  consume.resolve({
    session: SESSION,
    model: 'gpt-5-codex',
    command_id: 'cmd-1',
    outcome: 'alreadyRedeemed',
  });
  await flush();
  assert.equal(h.transport.consumeCalls.length, 1);
  assert.equal(h.transport.creditsCalls.length, 2, 'the rows are re-read');
  refreshed.resolve({ ...CREDITS, available_count: 1, credits: [CREDITS.credits[1]] });
  await flush();
  assert.match(h.last().notice ?? '', /已被兑换过/);
});

test('a dropped connection or an unknown outcome is never retried or replayed', async () => {
  const h = build();
  h.controller.start();
  await openWithUsage(h);
  const credits = deferred<CodexResetCreditsView>();
  h.transport.creditsReplies.push(credits);
  h.controller.openPanel();
  await flush();
  credits.resolve(CREDITS);
  await flush();

  h.controller.requestReset('credit-a');
  const consume = deferred<CodexConsumeResult>();
  h.transport.consumeReplies.push(consume);
  h.controller.confirmReset();
  consume.reject(Object.assign(new Error('connection lost'), { name: 'ConnectionLostError' }));
  await flush();

  assert.equal(h.last().lastOutcome, 'unknown');
  assert.match(h.last().notice ?? '', /结果未知/);
  assert.match(h.last().notice ?? '', /刷新核对/);
  assert.equal(h.transport.consumeCalls.length, 1, 'no automatic retry');
  assert.deepEqual(
    h.last().unresolved,
    { creditId: 'credit-a', commandId: 'cmd-1' },
    'the key that was sent stays on record',
  );

  // Ticking must not replay the write, and asking again must not mint a second
  // command id for a credit whose first write may already have landed: the record
  // has to be settled by an authoritative read first.
  await h.clock.fire();
  h.controller.requestReset('credit-a');
  assert.equal(h.last().pending, null, 'the unresolved credit is not offered again');
  h.controller.confirmReset();
  assert.equal(h.transport.consumeCalls.length, 1);
  assert.equal(h.last().pending, null);
});

test('an offline confirmation sends nothing and says so', async () => {
  const h = build();
  h.controller.start();
  await openWithUsage(h);
  const credits = deferred<CodexResetCreditsView>();
  h.transport.creditsReplies.push(credits);
  h.controller.openPanel();
  await flush();
  credits.resolve(CREDITS);
  await flush();
  h.controller.requestReset('credit-a');

  // The transport goes away without the controller being told (a drop between the
  // confirmation and the confirm click): the confirm must refuse, not send.
  h.context.context = { ...h.context.context, connected: false };
  h.controller.confirmReset();
  assert.equal(h.transport.consumeCalls.length, 0);
  assert.match(h.last().notice ?? '', /未发送兑换请求/);
});

test('a model switch clears the raised confirmation instead of carrying it over', async () => {
  const h = build();
  h.controller.start();
  await openWithUsage(h);
  const credits = deferred<CodexResetCreditsView>();
  h.transport.creditsReplies.push(credits);
  h.controller.openPanel();
  await flush();
  credits.resolve(CREDITS);
  await flush();
  h.controller.requestReset('credit-a');
  assert.notEqual(h.last().pending, null);

  h.transport.usageReplies.push(deferred<CodexUsageView>());
  h.context.set({ model: 'model-b' });
  await flush();
  assert.equal(h.last().pending, null, 'the confirmation belongs to its own context');
  assert.equal(h.last().credits, null, 'and so do the rows');
  h.controller.confirmReset();
  assert.equal(h.transport.consumeCalls.length, 0);
});

test('a session switch hides the entry until the new session is gated', async () => {
  const h = build();
  h.controller.start();
  await openWithUsage(h);
  assert.equal(h.last().available, true);

  h.transport.usageReplies.push(deferred<CodexUsageView>());
  h.context.set({ session: { project_id: 'proj', thread_id: 'other' } });
  assert.equal(h.last().available, false, 'the previous session\'s verdict is not reused');
  await flush();
  assert.equal(h.transport.configCalls.length, 2);
  assert.equal(h.transport.usageCalls.length, 2);
});

test('closing the panel drops a half-raised confirmation', async () => {
  const h = build();
  h.controller.start();
  await openWithUsage(h);
  const credits = deferred<CodexResetCreditsView>();
  h.transport.creditsReplies.push(credits);
  h.controller.openPanel();
  await flush();
  credits.resolve(CREDITS);
  await flush();
  h.controller.requestReset('credit-a');
  h.controller.closePanel();
  assert.equal(h.last().pending, null);
  assert.equal(h.transport.consumeCalls.length, 0);
});

// --- the unresolved write ---------------------------------------------------

test('an unresolved write survives a reconnect and only a fresh read settles it', async () => {
  const h = build();
  h.controller.start();
  await openWithUsage(h);
  const credits = deferred<CodexResetCreditsView>();
  h.transport.creditsReplies.push(credits);
  h.controller.openPanel();
  await flush();
  credits.resolve(CREDITS);
  await flush();

  h.controller.requestReset('credit-a');
  const consume = deferred<CodexConsumeResult>();
  h.transport.consumeReplies.push(consume);
  h.controller.confirmReset();
  consume.reject(Object.assign(new Error('connection lost'), { name: 'ConnectionLostError' }));
  await flush();
  assert.deepEqual(h.last().unresolved, { creditId: 'credit-a', commandId: 'cmd-1' });

  // The socket drops and comes back on the same client.  The record is about the
  // *account*, not the connection: a new epoch must not lose it.
  h.context.set({ connected: false });
  await flush();
  const reconnected = deferred<CodexUsageView>();
  h.transport.usageReplies.push(reconnected);
  h.context.set({ connected: true });
  await flush();
  assert.equal(h.last().available, true, 'the new epoch gates for itself');
  reconnected.resolve(USAGE('gpt-5-codex', 4));
  await flush();
  assert.deepEqual(
    h.last().unresolved,
    { creditId: 'credit-a', commandId: 'cmd-1' },
    'the reconnect keeps the pending record',
  );

  // Nothing may be raised for it while the record stands...
  h.controller.requestReset('credit-a');
  assert.equal(h.last().pending, null, 'and the credit is not offered again');
  assert.equal(h.transport.consumeCalls.length, 1, 'still exactly one write');

  // ...until a credits read issued *after* the write says what the account thinks.
  const authoritative = deferred<CodexResetCreditsView>();
  h.transport.creditsReplies.push(authoritative);
  h.controller.openPanel();
  await flush();
  assert.equal(h.transport.creditsCalls.length, 2);
  authoritative.resolve(CREDITS);
  await flush();
  assert.equal(h.last().unresolved, null, 'the account answered: the record is settled');
  h.controller.requestReset('credit-a');
  assert.equal(h.last().pending?.commandId, 'cmd-2', 'a settled credit can be confirmed again');
  assert.equal(h.transport.consumeCalls.length, 1, 'and the controller still sends nothing by itself');
});

// --- reads that outlive their write -----------------------------------------

test('a credits read in flight raises nothing, and a consume refreshes with fresh GETs', async () => {
  const h = build();
  h.controller.start();
  await openWithUsage(h);
  const first = deferred<CodexResetCreditsView>();
  h.transport.creditsReplies.push(first);
  h.controller.openPanel();
  await flush();
  first.resolve(CREDITS);
  await flush();

  // The refresh control starts a credits read.  While it is in flight the rows
  // under the button are about to be replaced, so nothing may be raised.
  const usageReply = deferred<CodexUsageView>();
  const inFlight = deferred<CodexResetCreditsView>();
  h.transport.usageReplies.push(usageReply);
  h.transport.creditsReplies.push(inFlight);
  h.controller.refresh();
  await flush();
  usageReply.resolve(USAGE('gpt-5-codex', 4));
  await flush();
  assert.equal(h.last().creditsLoading, true);
  h.controller.requestReset('credit-a');
  assert.equal(h.last().pending, null, 'a read in flight raises no confirmation');
  assert.equal(h.transport.consumeCalls.length, 0);

  inFlight.resolve(CREDITS);
  await flush();
  h.controller.requestReset('credit-a');
  assert.equal(h.last().pending?.commandId, 'cmd-1');

  // The write goes out.  Two reads that started before it are still on the wire —
  // the refresh chain issues its credits read while its usage read is already in
  // flight, so it is not stacked.  Neither may paint, and the post-consume refresh
  // must be a *new* GET rather than "already in flight".
  const consume = deferred<CodexConsumeResult>();
  h.transport.consumeReplies.push(consume);
  h.controller.confirmReset();
  assert.equal(h.transport.consumeCalls.length, 1);

  const staleUsage = deferred<CodexUsageView>();
  const staleCredits = deferred<CodexResetCreditsView>();
  h.transport.usageReplies.push(staleUsage);
  h.transport.creditsReplies.push(staleCredits);
  h.controller.refresh();
  await flush();
  assert.equal(h.transport.usageCalls.length, 3);
  h.controller.refresh();
  await flush();
  assert.equal(h.transport.usageCalls.length, 3, 'the second refresh does not stack a usage read');
  assert.equal(h.transport.creditsCalls.length, 3, 'but it does read the rows');

  const freshUsage = deferred<CodexUsageView>();
  const freshCredits = deferred<CodexResetCreditsView>();
  h.transport.usageReplies.push(freshUsage);
  h.transport.creditsReplies.push(freshCredits);
  consume.resolve({ session: SESSION, model: 'gpt-5-codex', command_id: 'cmd-1', outcome: 'reset' });
  await flush();
  assert.equal(h.transport.usageCalls.length, 4, 'the post-consume usage read is a new GET');
  freshUsage.resolve(USAGE('gpt-5-codex', 6));
  await flush();
  assert.equal(h.transport.creditsCalls.length, 4, 'and so is the rows read');

  // Both pre-consume replies land last: they describe the account as it was before
  // the reset, so they must be dropped instead of painting the credit back.
  staleUsage.resolve(USAGE('gpt-5-codex', 5));
  staleCredits.resolve(CREDITS);
  await flush();
  assert.equal(h.last().usage?.captured_at, 6, 'the retired usage reply never paints over the new one');
  assert.deepEqual(
    h.last().credits?.credits.map((credit) => credit.id),
    ['credit-b'],
    'and the spent credit stays spent',
  );
  assert.equal(h.last().credits?.available_count, 1);

  freshCredits.resolve({ ...CREDITS, available_count: 1, credits: [CREDITS.credits[1]] });
  await flush();
  assert.equal(h.last().usage?.captured_at, 6);
  assert.equal(h.last().credits?.available_count, 1);
  assert.match(h.last().notice ?? '', /已兑换 1 次重置额度/);
});

test('a refresh whose usage await lands in another context reads nothing for it', async () => {
  const h = build();
  h.controller.start();
  await openWithUsage(h);

  const staleUsage = deferred<CodexUsageView>();
  h.transport.usageReplies.push(staleUsage);
  h.controller.refresh();
  await flush();
  assert.equal(h.transport.usageCalls.length, 2);

  // The model moves while the refresh's usage read is still in flight: the retired
  // chain must not spend a credits request on the context it never started in.
  const newUsage = deferred<CodexUsageView>();
  h.transport.usageReplies.push(newUsage);
  h.context.set({ model: 'model-b' });
  await flush();
  assert.equal(h.transport.usageCalls.length, 3, 'the new context gates and reads for itself');
  assert.equal(h.transport.creditsCalls.length, 0);

  staleUsage.resolve(USAGE('gpt-5-codex', 1));
  await flush();
  assert.equal(h.transport.creditsCalls.length, 0, 'the retired chain reads nothing');
  assert.equal(h.last().usage, null);
  newUsage.resolve(USAGE('model-b', 2));
  await flush();
  assert.equal(h.last().usage?.captured_at, 2);
});

// --- stop and restart -------------------------------------------------------

test('stop retires the chains in flight, so a late gate reply paints nothing', async () => {
  const h = build();
  h.controller.start();
  await openWithUsage(h);

  const lateGate = deferred<unknown>();
  h.transport.configReplies.push(lateGate);
  h.context.set({ model: 'model-b' });
  await flush();
  assert.equal(h.transport.configCalls.length, 2);
  h.controller.stop();
  lateGate.resolve({ current_model: 'model-b', codex_usage_enabled: true });
  await flush();
  assert.equal(h.transport.usageCalls.length, 1, 'the retired gate issues no usage read');
  assert.equal(h.last().available, false, 'and paints no verdict');
  assert.equal(h.last().usage, null);
});

test('a restart re-gates the same context instead of trusting the retired verdict', async () => {
  const h = build();
  h.controller.start();
  await openWithUsage(h);
  h.controller.stop();

  // The context moves and comes back while nothing observes it: the restart may not
  // reuse the verdict of the epoch that was retired.
  h.context.set({ model: 'model-b' });
  h.context.set({ model: 'gpt-5-codex' });
  const usage = deferred<CodexUsageView>();
  h.transport.usageReplies.push(usage);
  h.controller.start();
  await flush();
  assert.equal(h.transport.configCalls.length, 2, 'the restart asks the server again');
  assert.equal(h.transport.usageCalls.length, 2);
  assert.equal(h.last().available, true);
  usage.resolve(USAGE('gpt-5-codex', 7));
  await flush();
  assert.equal(h.last().usage?.captured_at, 7);
});

// --- a new context has no permission yet ------------------------------------

test('a model switch hides the entry until the new profile is gated', async () => {
  const h = build();
  h.controller.start();
  await openWithUsage(h);
  assert.equal(h.last().available, true);

  const nextUsage = deferred<CodexUsageView>();
  h.transport.usageReplies.push(nextUsage);
  h.context.set({ model: 'model-b' });
  assert.equal(h.last().available, false, 'the previous profile\'s verdict is not carried over');
  assert.equal(h.last().usage, null);
  assert.equal(h.last().credits, null);
  await flush();
  assert.equal(h.last().available, true, 'the new profile answers for itself');
  nextUsage.resolve(USAGE('model-b', 8));
  await flush();
  assert.equal(h.last().usage?.captured_at, 8);
});

// --- replies that are not this request's ------------------------------------

test('a consume reply with another session, model or command id is not committed', async () => {
  const foreign = [
    { session: { project_id: 'proj', thread_id: 'other' }, model: 'gpt-5-codex', command_id: 'cmd-1' },
    { session: SESSION, model: 'model-b', command_id: 'cmd-1' },
    { session: SESSION, model: 'gpt-5-codex', command_id: 'cmd-9' },
  ];
  for (const reply of foreign) {
    const h = build();
    h.controller.start();
    await openWithUsage(h);
    const credits = deferred<CodexResetCreditsView>();
    h.transport.creditsReplies.push(credits);
    h.controller.openPanel();
    await flush();
    credits.resolve(CREDITS);
    await flush();

    h.controller.requestReset('credit-a');
    const consume = deferred<CodexConsumeResult>();
    h.transport.consumeReplies.push(consume);
    h.controller.confirmReset();
    consume.resolve({ ...reply, outcome: 'reset' });
    await flush();

    const label = JSON.stringify(reply);
    assert.equal(h.last().lastOutcome, 'unknown', label);
    assert.deepEqual(h.last().unresolved, { creditId: 'credit-a', commandId: 'cmd-1' }, label);
    assert.equal(h.last().credits?.available_count, 2, `nothing is dropped for ${label}`);
    assert.equal(h.transport.creditsCalls.length, 1, `no refresh is issued for ${label}`);
    assert.equal(h.transport.consumeCalls.length, 1, label);
  }
});

test('a usage or credits reply for another session is never committed', async () => {
  const usageHarness = build();
  usageHarness.controller.start();
  const usage = deferred<CodexUsageView>();
  usageHarness.transport.usageReplies.push(usage);
  usageHarness.context.set({ connected: true });
  await flush();
  usage.resolve({
    ...USAGE('gpt-5-codex', 9),
    session: { project_id: 'proj', thread_id: 'other' },
  });
  await flush();
  assert.equal(usageHarness.last().usage, null, 'another session\'s windows are not this session\'s');
  assert.equal(usageHarness.last().available, true, 'the gate still answered');
  assert.match(usageHarness.last().error ?? '', /读取 Codex 用量失败/);

  const creditsHarness = build();
  creditsHarness.controller.start();
  await openWithUsage(creditsHarness);
  const credits = deferred<CodexResetCreditsView>();
  creditsHarness.transport.creditsReplies.push(credits);
  creditsHarness.controller.openPanel();
  await flush();
  credits.resolve({ ...CREDITS, session: { project_id: 'proj', thread_id: 'other' } });
  await flush();
  assert.equal(creditsHarness.last().credits, null);
  assert.match(creditsHarness.last().creditsError ?? '', /读取 Codex 用量失败/);
});
