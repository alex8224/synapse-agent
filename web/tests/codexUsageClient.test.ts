/**
 * Contract tests for the Codex usage surface: the three strict decoders, the
 * optional `runtime.config.get` gate, and the three `SynapseRuntimeClient`
 * methods that carry them over a real (injected) socket.
 *
 * The decoders are the *only* place a wire payload becomes a view object, so the
 * tests pin the rules that keep the panel honest:
 *
 *  - every declared field is required and typed; an unexpected key (including a
 *    credential-shaped one such as `access_token`) is rejected rather than
 *    forwarded;
 *  - numbers are bounded (a window is a minute count, a timestamp is Unix
 *    seconds inside a plausible calendar range) and text is length-capped;
 *  - `codex_usage_enabled` is read tolerantly — a peer that predates it hides the
 *    entry instead of enabling it;
 *  - the write carries `confirmed: true` and the caller's own `command_id`, and
 *    the request goes out with exactly the declared params.
 *
 * No host, daemon, token file or OAuth state is involved: the transport is a fake
 * socket.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { SynapseRuntimeClient } from '../src/runtime-client/SynapseRuntimeClient.ts';
import type { SocketLike } from '../src/runtime-client/SynapseRuntimeClient.ts';
import {
  CODEX_RESET_CONSUME_METHOD,
  CODEX_RESET_CREDITS_METHOD,
  CODEX_USAGE_METHOD,
  MalformedCodexUsagePayloadError,
  parseCodexConsumeResult,
  parseCodexResetCreditsView,
  parseCodexUsageView,
  readCodexUsageConfig,
} from '../src/runtime-client/codexUsage.ts';

const SESSION = { project_id: 'proj', thread_id: 'thr' };

const USAGE = {
  session: SESSION,
  model: 'gpt-5-codex',
  primary: { used_percent: 18, window_minutes: 300, reset_at: 1_800_000_000 },
  secondary: { used_percent: null, window_minutes: null, reset_at: null },
  captured_at: 1_799_900_000,
  available_reset_count: 2,
};

const CREDITS = {
  session: SESSION,
  model: 'gpt-5-codex',
  available_count: 1,
  credits: [
    {
      id: 'credit-1',
      reset_type: 'weekly',
      status: 'available',
      granted_at: 1_799_000_000,
      expires_at: null,
      title: 'Weekly reset',
      description: null,
    },
  ],
};

const CONSUME = {
  session: SESSION,
  model: 'gpt-5-codex',
  command_id: 'cmd-1',
  outcome: 'reset',
};

// --- the decoders -----------------------------------------------------------

test('a declared usage payload decodes field by field', () => {
  assert.deepEqual(parseCodexUsageView(USAGE), USAGE);
});

test('a window is nullable field by field, not nullable as a whole', () => {
  const view = parseCodexUsageView({
    ...USAGE,
    primary: { used_percent: null, window_minutes: 10080, reset_at: null },
    secondary: null,
  });
  // The real window length is preserved: the panel labels `7d`, never a guess.
  assert.deepEqual(view.primary, { used_percent: null, window_minutes: 10080, reset_at: null });
  assert.equal(view.secondary, null);
});

test('a declared reset-credit payload decodes, including the nullable text fields', () => {
  assert.deepEqual(parseCodexResetCreditsView(CREDITS), CREDITS);
});

test('a consume result accepts exactly the five declared outcomes', () => {
  for (const outcome of ['reset', 'alreadyRedeemed', 'nothingToReset', 'noCredit', 'unknown']) {
    assert.equal(parseCodexConsumeResult({ ...CONSUME, outcome }).outcome, outcome);
  }
  // A future verb is *not* forwarded as an open string: it has to be treated as
  // unknown, which is what a rejection maps to.
  assert.throws(
    () => parseCodexConsumeResult({ ...CONSUME, outcome: 'partial' }),
    MalformedCodexUsagePayloadError,
  );
});

test('an unexpected key is rejected, so a credential-shaped field never reaches the UI', () => {
  const cases: unknown[] = [
    { ...USAGE, access_token: 'secret' },
    { ...USAGE, primary: { ...USAGE.primary, account_id: 'acct' } },
    { ...CREDITS, credits: [{ ...CREDITS.credits[0], authorization: 'Bearer x' }] },
    { ...CONSUME, idempotency_key: 'k' },
    { ...USAGE, session: { ...SESSION, workspace: '/w' } },
  ];
  for (const payload of cases) {
    const parse = (value: unknown) =>
      'credits' in (value as object)
        ? parseCodexResetCreditsView(value)
        : 'outcome' in (value as object)
          ? parseCodexConsumeResult(value)
          : parseCodexUsageView(value);
    assert.throws(() => parse(payload), MalformedCodexUsagePayloadError);
  }
});

test('a missing declared field is rejected instead of defaulting', () => {
  const withoutCapturedAt: Record<string, unknown> = { ...USAGE };
  delete withoutCapturedAt.captured_at;
  assert.throws(() => parseCodexUsageView(withoutCapturedAt), MalformedCodexUsagePayloadError);
  assert.throws(
    () => parseCodexResetCreditsView({ ...CREDITS, available_count: undefined }),
    MalformedCodexUsagePayloadError,
  );
  assert.throws(() => parseCodexConsumeResult({ ...CONSUME, command_id: '' }), MalformedCodexUsagePayloadError);
});

test('numbers are bounded: non-finite, negative, oversized and off-calendar values fail', () => {
  const badWindows = [
    { used_percent: Number.NaN, window_minutes: 300, reset_at: null },
    { used_percent: Number.POSITIVE_INFINITY, window_minutes: 300, reset_at: null },
    { used_percent: -1, window_minutes: 300, reset_at: null },
    { used_percent: 101, window_minutes: 300, reset_at: null },
    { used_percent: 1, window_minutes: 0, window_minutesX: 0, reset_at: null },
    { used_percent: 1, window_minutes: 2_000_000, reset_at: null },
    { used_percent: 1, window_minutes: 300, reset_at: 1e300 },
  ];
  for (const primary of badWindows) {
    assert.throws(
      () => parseCodexUsageView({ ...USAGE, primary }),
      MalformedCodexUsagePayloadError,
      JSON.stringify(primary),
    );
  }
  assert.throws(
    () => parseCodexUsageView({ ...USAGE, captured_at: -1 }),
    MalformedCodexUsagePayloadError,
  );
  assert.throws(
    () => parseCodexResetCreditsView({ ...CREDITS, available_count: 10_000 }),
    MalformedCodexUsagePayloadError,
  );
});

test('text is length-capped, so one payload cannot paint an unbounded label', () => {
  assert.throws(
    () => parseCodexResetCreditsView({ ...CREDITS, model: 'm'.repeat(300) }),
    MalformedCodexUsagePayloadError,
  );
  assert.throws(
    () =>
      parseCodexResetCreditsView({
        ...CREDITS,
        credits: [{ ...CREDITS.credits[0], description: 'd'.repeat(2000) }],
      }),
    MalformedCodexUsagePayloadError,
  );
  assert.throws(
    () => parseCodexResetCreditsView({ ...CREDITS, credits: new Array(201).fill(CREDITS.credits[0]) }),
    MalformedCodexUsagePayloadError,
  );
});

test('the optional config gate is tolerant, and only a literal true enables the entry', () => {
  assert.deepEqual(readCodexUsageConfig({ current_model: 'gpt-5-codex', codex_usage_enabled: true }), {
    enabled: true,
    model: 'gpt-5-codex',
  });
  // A peer that predates the field: hidden, and no usage RPC is ever sent.
  assert.deepEqual(readCodexUsageConfig({ current_model: 'gpt-5-codex' }), {
    enabled: false,
    model: 'gpt-5-codex',
  });
  for (const value of [false, null, 'true', 1, undefined]) {
    assert.equal(readCodexUsageConfig({ codex_usage_enabled: value }).enabled, false);
  }
  for (const value of [null, undefined, 'nope', 42, []]) {
    assert.equal(readCodexUsageConfig(value).enabled, false);
  }
  assert.equal(readCodexUsageConfig({ current_model: '' }).model, null);
});

// --- the transport ----------------------------------------------------------

interface SentFrame {
  id: number;
  method: string;
  params: any;
}

const CAPABILITIES = {
  legacy_v1: true,
  raw_cursor: true,
  watch_resume: true,
  approval_resume: true,
};

/** Fake transport: answers the handshake, then one scripted reply per method. */
class CodexSocket implements SocketLike {
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: any }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((ev?: { code?: number; reason?: string }) => void) | null = null;
  sent: SentFrame[] = [];
  replies: Record<string, unknown> = {};

  send(data: string): void {
    const request = JSON.parse(data) as SentFrame;
    this.sent.push(request);
    this.push({
      jsonrpc: '2.0',
      id: request.id,
      meta: { wire_version: '1' },
      result:
        request.method === 'runtime.protocol.negotiate'
          ? { wire_version: '1', supported_versions: ['1'], capabilities: CAPABILITIES }
          : this.replies[request.method],
    });
  }

  push(frame: unknown): void {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }

  serverOpen(): void {
    this.readyState = 1;
    this.onopen?.();
  }

  close(): void {
    this.readyState = 3;
  }
}

async function openClient(replies: Record<string, unknown>): Promise<{
  client: SynapseRuntimeClient;
  socket: CodexSocket;
}> {
  const socket = new CodexSocket();
  socket.replies = replies;
  const client = new SynapseRuntimeClient({ url: 'ws://core', socketFactory: () => socket });
  const connecting = client.connect();
  socket.serverOpen();
  await connecting;
  return { client, socket };
}

test('the three methods send exactly the declared params and decode the reply', async () => {
  const { client, socket } = await openClient({
    [CODEX_USAGE_METHOD]: USAGE,
    [CODEX_RESET_CREDITS_METHOD]: CREDITS,
    [CODEX_RESET_CONSUME_METHOD]: CONSUME,
  });

  assert.deepEqual(await client.getCodexUsage(SESSION), USAGE);
  // `sent[0]` is the handshake every connect performs; the business frames follow.
  const frames = () => socket.sent.filter((frame) => frame.method !== 'runtime.protocol.negotiate');
  assert.equal(frames()[0].method, CODEX_USAGE_METHOD);
  assert.deepEqual(frames()[0].params, { session: SESSION, force: false });

  assert.deepEqual(await client.getCodexResetCredits(SESSION, true), CREDITS);
  assert.equal(frames()[1].method, CODEX_RESET_CREDITS_METHOD);
  assert.deepEqual(frames()[1].params, { session: SESSION, force: true });

  const consumed = await client.consumeCodexResetCredit({
    session: SESSION,
    expected_model: 'gpt-5-codex',
    credit_id: 'credit-1',
    command_id: 'cmd-1',
    confirmed: true,
  });
  assert.deepEqual(consumed, CONSUME);
  assert.equal(frames()[2].method, CODEX_RESET_CONSUME_METHOD);
  assert.deepEqual(frames()[2].params, {
    session: SESSION,
    expected_model: 'gpt-5-codex',
    credit_id: 'credit-1',
    command_id: 'cmd-1',
    confirmed: true,
  });

  client.disconnect();
});

test('a malformed reply rejects instead of reaching the caller half-shaped', async () => {
  const { client } = await openClient({
    [CODEX_USAGE_METHOD]: { ...USAGE, available_reset_count: 'many' },
  });
  await assert.rejects(
    () => client.getCodexUsage(SESSION),
    MalformedCodexUsagePayloadError,
  );
  client.disconnect();
});

test('a disconnected client rejects a consume instead of pretending it was sent', async () => {
  const { client, socket } = await openClient({});
  socket.close();
  await assert.rejects(() =>
    client.consumeCodexResetCredit({
      session: SESSION,
      expected_model: 'gpt-5-codex',
      credit_id: 'credit-1',
      command_id: 'cmd-1',
      confirmed: true,
    }),
  );
});
