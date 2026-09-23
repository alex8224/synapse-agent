/**
 * Lifecycle controller for the Codex usage entry.
 *
 * The strip's Codex entry is not a plain manifest entry: it only exists while the
 * *server* says the session's effective model is an enabled Codex OAuth provider,
 * and that verdict has to be discovered before anything can be painted.  This
 * controller owns that discovery plus the read/consume lifecycle, and it is
 * deliberately free of React, zustand and the DOM: every host effect (time,
 * timers, ids, page visibility, the RPC port) is injected, so the deferred-result
 * races, the cache/TTL behaviour and the confirmation flow are exercised by the
 * Node test runner with a fake clock and a fake transport.
 *
 * What it observes, and nothing else: the runtime client, the current session,
 * the session's model and the connection flag (`read()`), plus a `subscribe()`
 * that fires when one of them changes.  It never writes to the console store —
 * the model/capability state there stays owned by the console.
 *
 * Lifecycle rules that matter:
 *
 *  - the entry's availability source drives `start()` / `stop()` (see
 *    `stores/codexUsage.ts`), so a hidden entry still discovers OAuth — a source
 *    that only started on mount could never become visible again;
 *  - every observable change bumps an *epoch*; a response that resolves after it
 *    is dropped, so `A -> B -> A`, a reconnect on the same client and a logout
 *    can never paint a stale window or re-offer a spent credit;
 *  - the two reads are single-flight and 300s-cached, and the periodic tick is
 *    skipped while the page is hidden, so a timer can never stack a request;
 *  - a redeem is two-step: `requestReset()` only raises a confirmation, and
 *    `confirmReset()` sends the one write.  A failed or unparseable consume is
 *    never retried and never replayed under a fresh command id — the user is told
 *    to refresh and check the account instead;
 *  - a new context is *unavailable* until its own gate answers, so a model switch
 *    can never paint the previous profile's permission (or leave its panel open);
 *  - `stop()` retires every chain in flight and makes the next `start()` re-gate;
 *    a write that was already sent keeps its command id in `unresolved` until an
 *    authoritative credits read settles it, so it can never be replayed;
 *  - a credits read in flight blocks a new confirmation, and a consume retires the
 *    read it supersedes, so the post-consume refresh is a real GET instead of
 *    being swallowed as "already in flight".
 */
import type { SessionRef } from '../runtime-client/types.ts';
import {
  readCodexUsageConfig,
  type CodexConsumeOutcome,
  type CodexConsumeResult,
  type CodexResetCreditsView,
  type CodexUsageView,
  type ConsumeCodexResetParams,
} from '../runtime-client/codexUsage.ts';
import {
  CODEX_USAGE_ALREADY_REDEEMED_NOTICE,
  CODEX_USAGE_CONSUME_ERROR,
  CODEX_USAGE_NO_CREDIT_NOTICE,
  CODEX_USAGE_NOTHING_TO_RESET_NOTICE,
  CODEX_USAGE_OFFLINE_NOTICE,
  CODEX_USAGE_READ_ERROR,
  CODEX_USAGE_REDEEMED_NOTICE,
  CODEX_USAGE_REFRESH_FAILED_NOTICE,
  CODEX_USAGE_UNKNOWN_OUTCOME_NOTICE,
  codexUsageErrorText,
  isCreditRedeemable,
} from './codexUsageView.ts';

/** The RPC surface this controller needs; `SynapseRuntimeClient` satisfies it. */
export interface CodexUsagePort {
  getRuntimeConfig(params: { session: SessionRef }): Promise<unknown>;
  getCodexUsage(session: SessionRef, force?: boolean): Promise<CodexUsageView>;
  getCodexResetCredits(session: SessionRef, force?: boolean): Promise<CodexResetCreditsView>;
  consumeCodexResetCredit(params: ConsumeCodexResetParams): Promise<CodexConsumeResult>;
}

/** The observed context: what the controller keys its epoch on. */
export interface CodexUsageContext {
  client: CodexUsagePort | null;
  session: SessionRef;
  /** The session's model as the console knows it (`open.view` / a rebind). */
  model: string;
  /**
   * The confirmed model-binding revision (`useConsoleStore.modelRevision`).
   *
   * The console publishes a model switch optimistically, *before* its rebind
   * lands, so a gate issued on `model` alone would be answered by the previous
   * profile — and because the confirmed value is the same string, nothing would
   * ever notify this controller again: the entry would stay hidden until the
   * context changed for some other reason.  The revision is what makes the
   * rebind's completion observable.
   */
  revision: number;
  connected: boolean;
}

/** One raised confirmation.  Nothing has been sent while this exists. */
export interface PendingReset {
  creditId: string;
  /** Minted once, bound to this confirmation; never regenerated for a replay. */
  commandId: string;
  /** The epoch key the confirmation belongs to. */
  origin: string;
  /** The model the user saw (`expected_model`). */
  model: string;
}

export interface CodexUsageState {
  /** The entry may be painted at all (server: enabled Codex OAuth provider). */
  available: boolean;
  /** Effective model, as the server reports it. */
  model: string;
  usage: CodexUsageView | null;
  credits: CodexResetCreditsView | null;
  usageLoading: boolean;
  creditsLoading: boolean;
  /** Credential-free read error for the panel, or `null`. */
  error: string | null;
  creditsError: string | null;
  /** The last redeem's outcome notice, or `null`. */
  notice: string | null;
  pending: PendingReset | null;
  consuming: boolean;
  lastOutcome: CodexConsumeOutcome | null;
  /**
   * A write that was sent and whose outcome is not authoritatively known yet.
   *
   * It is recorded *before* the request goes out and cleared by a definite
   * outcome or by a credits read issued after it: while it is set, the credit it
   * names is never offered again (a retry would mint a second command id for a
   * credit the account may already have spent).
   */
  unresolved: UnresolvedReset | null;
}

/** One write that may have landed: the credit, and the key it was sent with. */
export interface UnresolvedReset {
  creditId: string;
  /** The idempotency key that was sent; never minted again for this credit. */
  commandId: string;
}

export interface CodexUsageControllerOptions {
  /** Read the observed context (never throws). */
  read: () => CodexUsageContext;
  /** Fire when `read()`'s value may have changed. */
  subscribe: (listener: () => void) => () => void;
  /** Publish a new state snapshot. */
  onState: (state: CodexUsageState) => void;
  now?: () => number;
  schedule?: (run: () => void, delayMs: number) => number;
  cancel?: (handle: number) => void;
  /** Mint the consume idempotency key (one per confirmation). */
  newCommandId: () => string;
  /** `document.visibilityState === 'visible'`; defaults to always visible. */
  isVisible?: () => boolean;
  cacheTtlMs?: number;
  refreshIntervalMs?: number;
}

/** The TUI's own read cache (`USAGE_CACHE_TTL_SECONDS`). */
export const CODEX_USAGE_CACHE_TTL_MS = 300_000;
/** Background refresh cadence; every tick is skipped while hidden. */
export const CODEX_USAGE_REFRESH_INTERVAL_MS = 60_000;

/**
 * Stable identity per client object.
 *
 * A logout/login builds a *new* `SynapseRuntimeClient`, and that has to count as
 * a new context even though the session id may repeat; a reconnect reuses the
 * same object and is caught by the connection flag in the key instead.
 */
const clientIdentities = new WeakMap<object, number>();
let nextClientIdentity = 1;

function clientIdentity(client: CodexUsagePort | null): number {
  if (client === null) return 0;
  const key = client as unknown as object;
  const existing = clientIdentities.get(key);
  if (existing !== undefined) return existing;
  const assigned = nextClientIdentity++;
  clientIdentities.set(key, assigned);
  return assigned;
}

/**
 * The identity of one read: a monotonically increasing id, plus the epoch the
 * running read belongs to.
 *
 * Two rules need it.  Single-flight is per *epoch*, so a superseded context never
 * blocks the new one — its reply is going to be discarded anyway.  And a write has
 * to be able to retire a read that was issued *before* it: without that, the
 * pre-consume reply would paint the spent credit back and the post-consume refresh
 * would be swallowed as "already in flight" (see `runConsume`).
 */
class ReadGeneration {
  private latest = 0;
  private inFlightId: number | null = null;
  private inFlightEpoch: number | null = null;

  /** The id of the newest issued read (`0` before the first one). */
  get current(): number {
    return this.latest;
  }

  /** Whether a read for this epoch is already running (single-flight). */
  busy(epoch: number): boolean {
    return this.inFlightEpoch === epoch;
  }

  begin(epoch: number): number {
    const id = ++this.latest;
    this.inFlightId = id;
    this.inFlightEpoch = epoch;
    return id;
  }

  settle(id: number): void {
    if (this.inFlightId !== id) return;
    this.inFlightId = null;
    this.inFlightEpoch = null;
  }

  /** Whether `id` is still the newest read; an older reply is dropped. */
  isCurrent(id: number): boolean {
    return id === this.latest;
  }

  /** Retire whatever is running: its reply is dropped and a new read is allowed. */
  invalidate(): void {
    this.latest += 1;
    this.inFlightId = null;
    this.inFlightEpoch = null;
  }
}

export class CodexUsageController {
  private readonly options: CodexUsageControllerOptions;
  private state: CodexUsageState;
  private epoch = 0;
  /** The epoch key currently observed; `''` before the first sync. */
  private key = '';
  private usageAt = 0;
  private creditsAt = 0;
  /**
   * The identity of each read chain: single-flight per epoch, and retired by a
   * write so a reply that predates it can never paint over the write's result.
   */
  private readonly usageReads = new ReadGeneration();
  private readonly creditsReads = new ReadGeneration();
  /**
   * The newest credits-read id at the moment the last write was sent.
   *
   * Only a read *issued after* that write may settle `unresolved`: a reply that
   * was already on the wire when the write went out cannot prove anything about it.
   */
  private unresolvedReadFloor = 0;
  /** The config gate already answered for the current key (either verdict). */
  private gated = false;
  private consuming = false;
  private started = 0;
  private unsubscribe: (() => void) | null = null;
  private timer: number | null = null;

  constructor(options: CodexUsageControllerOptions) {
    this.options = options;
    this.state = {
      available: false,
      model: '',
      usage: null,
      credits: null,
      usageLoading: false,
      creditsLoading: false,
      error: null,
      creditsError: null,
      notice: null,
      pending: null,
      consuming: false,
      lastOutcome: null,
      unresolved: null,
    };
  }

  // -- lifecycle --------------------------------------------------------------

  /** Reference-counted: the availability source starts and stops it. */
  start(): void {
    this.started += 1;
    if (this.started > 1) return;
    this.unsubscribe = this.options.subscribe(() => this.sync());
    this.timer = this.schedule();
    this.sync();
  }

  stop(): void {
    if (this.started === 0) return;
    this.started -= 1;
    if (this.started > 0) return;
    this.unsubscribe?.();
    this.unsubscribe = null;
    if (this.timer !== null) {
      this.options.cancel?.(this.timer);
      this.timer = null;
    }
    // Nothing observes this entry any more: every chain still in flight is retired
    // (its reply must not paint, and no follow-up read may be issued for it), and
    // the next `start()` re-runs the discovery from scratch.
    this.retire();
  }

  getState(): CodexUsageState {
    return this.state;
  }

  isAvailable(): boolean {
    return this.state.available;
  }

  // -- panel actions ----------------------------------------------------------

  /** The panel opened: credits are loaded on demand (and 300s-cached). */
  openPanel(): void {
    if (!this.state.available) return;
    void this.loadCredits(this.epoch, false);
  }

  /** The panel closed: a half-raised confirmation never survives it. */
  closePanel(): void {
    if (this.state.pending === null) return;
    this.patch({ pending: null });
  }

  /** The panel's refresh button: both views, cache bypassed. */
  refresh(): void {
    const epoch = this.epoch;
    void this.refreshAll(epoch, true);
  }

  /**
   * Raise the confirmation for one credit.  Sends nothing: the user has to press
   * the confirm control, which is the only path to `confirmReset()`.
   *
   * Two states refuse it outright: the credit rows are being replaced under the
   * user's feet (`creditsLoading`), and a credit whose earlier write is still
   * unresolved — the account may already have spent it, so a second confirmation
   * would be the first step of a replay.
   */
  requestReset(creditId: string): void {
    if (this.consuming || this.state.creditsLoading || this.state.pending !== null) return;
    if (this.state.unresolved !== null) return;
    const credit = this.state.credits?.credits.find((row) => row.id === creditId);
    if (credit === undefined) return;
    if (!isCreditRedeemable(credit, this.nowSeconds())) return;
    this.patch({
      pending: {
        creditId,
        commandId: this.options.newCommandId(),
        origin: this.key,
        model: this.state.model,
      },
      notice: null,
      lastOutcome: null,
    });
  }

  /** Drop the confirmation.  Zero requests. */
  cancelReset(): void {
    if (this.state.pending === null) return;
    this.patch({ pending: null });
  }

  /**
   * Send the one write.  Refuses when the confirmation is gone, the credit is no
   * longer redeemable, the rows are being re-read, the write is already
   * unresolved, or the chain is no longer live — in the last cases without sending
   * anything.
   */
  confirmReset(): void {
    const pending = this.state.pending;
    if (pending === null || this.consuming || this.state.creditsLoading) return;
    if (this.state.unresolved !== null) {
      this.patch({ pending: null });
      return;
    }
    // The confirmation belongs to the context it was raised in.
    if (pending.origin !== this.key) {
      this.patch({ pending: null });
      return;
    }
    const credit = this.state.credits?.credits.find((row) => row.id === pending.creditId);
    if (credit === undefined || !isCreditRedeemable(credit, this.nowSeconds())) {
      this.patch({ pending: null });
      return;
    }
    const context = this.options.read();
    const client = context.client;
    // Every RPC is preceded by the same liveness check: the epoch is current, the
    // controller is running, the observed context is the eligible one and the
    // entry is enabled.  A confirmation raised before a model switch or a logout
    // must not reach the new session or the new client.
    if (client === null || !this.isLive(this.epoch, context)) {
      this.patch({ pending: null, notice: CODEX_USAGE_OFFLINE_NOTICE });
      return;
    }
    this.consuming = true;
    // The key is recorded *before* the request leaves, and only an authoritative
    // credits read (or a definite outcome) may clear it: a write whose reply is
    // lost, or which lands after its context moved on, must never be replayed
    // under a freshly minted id.
    this.unresolvedReadFloor = this.creditsReads.current;
    this.patch({
      pending: null,
      consuming: true,
      notice: null,
      lastOutcome: null,
      unresolved: { creditId: pending.creditId, commandId: pending.commandId },
    });
    void this.runConsume(this.epoch, client, context.session, pending);
  }

  // -- context synchronisation ------------------------------------------------

  private sync(): void {
    const context = this.options.read();
    const key = this.keyOf(context);
    if (key === this.key) return;
    this.key = key;
    this.epoch += 1;
    this.usageAt = 0;
    this.creditsAt = 0;
    this.gated = false;
    this.usageReads.invalidate();
    this.creditsReads.invalidate();
    this.consuming = false;
    // A new context inherits no result, no confirmation, no notice and — crucially
    // — no *permission*: the entry is hidden until this context's own gate answers,
    // so a model switch can never paint the previous profile's verdict (or leave
    // its panel open next to a list that belongs to another profile).  The
    // unresolved-write record deliberately survives: it is about the account, and
    // dropping it would let a retry mint a fresh command id for a credit that may
    // already be spent.
    this.patch({
      available: false,
      usage: null,
      credits: null,
      usageLoading: false,
      creditsLoading: false,
      error: null,
      creditsError: null,
      notice: null,
      pending: null,
      consuming: false,
      lastOutcome: null,
      model: context.model,
    });
    if (!this.eligible(context)) {
      return;
    }
    void this.gate(this.epoch);
  }

  /**
   * Retire every chain in flight.
   *
   * The epoch moves on, so a reply that lands afterwards is dropped instead of
   * painting a result — or continuing a chain with a follow-up read — for a
   * context nobody is observing any more.  `key` is cleared so the next `start()`
   * re-gates even though the observed context looks unchanged.
   */
  private retire(): void {
    this.epoch += 1;
    this.key = '';
    this.gated = false;
    this.usageReads.invalidate();
    this.creditsReads.invalidate();
    this.patch({ usageLoading: false, creditsLoading: false });
  }

  private schedule(): number | null {
    const run = () => {
      this.timer = this.schedule();
      this.tick();
    };
    return this.options.schedule?.(run, this.refreshIntervalMs) ?? null;
  }

  /**
   * One background tick: discovery while hidden, a cache-respecting refresh while
   * shown.  A hidden page does no work at all (and sends nothing).
   */
  private tick(): void {
    if (this.options.isVisible !== undefined && !this.options.isVisible()) return;
    const context = this.options.read();
    if (!this.eligible(context)) return;
    if (!this.state.available) {
      // Only a gate that never *answered* is retried: a profile the server has
      // already judged non-OAuth must not be re-asked every minute.
      if (!this.gated) void this.gate(this.epoch);
      return;
    }
    if (this.state.pending !== null || this.consuming) return;
    void this.loadUsage(this.epoch, false);
  }

  /**
   * Ask the server whether this session's effective model is an enabled Codex
   * OAuth provider, then read the usage windows.  This is the *only* place the
   * entry can become available.
   */
  private async gate(epoch: number): Promise<void> {
    const context = this.options.read();
    const client = context.client;
    if (client === null) return;
    // The gate is the one chain allowed to run while the entry is still hidden.
    if (!this.isLive(epoch, context, true)) return;
    try {
      const view = await client.getRuntimeConfig({ session: context.session });
      if (!this.isLive(epoch, context, true)) return;
      const gate = readCodexUsageConfig(view);
      this.gated = true;
      this.patch({
        available: gate.enabled,
        model: gate.model ?? context.model,
        error: null,
      });
      if (!gate.enabled) {
        // Not an OAuth profile (or a peer too old to advertise it): no usage RPC
        // is ever sent, and the entry stays hidden.
        this.patch({ usage: null, credits: null, pending: null });
        return;
      }
      await this.loadUsage(epoch, false);
    } catch (error) {
      if (!this.isLive(epoch, context, true)) return;
      this.patch({ available: false, error: codexUsageErrorText(error) });
    }
  }

  /**
   * Whether the chain started for `epoch` may still issue a request and paint its
   * reply.
   *
   * Every RPC — and every *follow-up* read after an await — goes through this: the
   * epoch is current, the controller is running, the context it read is still the
   * observed, eligible one (same client, session, model and connection), and (for
   * everything but the gate itself) the entry is enabled.  That is what keeps a
   * chain that outlived its context from reading for the *new* session or client.
   */
  private isLive(epoch: number, context: CodexUsageContext, isGate = false): boolean {
    if (epoch !== this.epoch || this.started === 0) return false;
    if (!isGate && !this.state.available) return false;
    if (!this.eligible(context)) return false;
    return this.keyOf(this.options.read()) === this.key;
  }

  private async loadUsage(epoch: number, force: boolean): Promise<boolean> {
    if (this.usageReads.busy(epoch)) return false;
    const context = this.options.read();
    const client = context.client;
    if (client === null || !this.isLive(epoch, context)) return false;
    if (!force && this.state.usage !== null && this.now() - this.usageAt < this.cacheTtlMs) {
      return true;
    }
    const readId = this.usageReads.begin(epoch);
    this.patch({ usageLoading: true, error: null });
    try {
      const view = await client.getCodexUsage(context.session, force);
      if (!this.usageReads.isCurrent(readId) || !this.isLive(epoch, context)) return false;
      if (!sameSession(view.session, context.session)) {
        // A view of another session is not this read's answer: never commit it.
        this.patch({ usageLoading: false, error: CODEX_USAGE_READ_ERROR });
        return false;
      }
      this.usageAt = this.now();
      this.patch({ usage: view, usageLoading: false, error: null, model: view.model });
      return true;
    } catch (error) {
      if (!this.usageReads.isCurrent(readId) || !this.isLive(epoch, context)) return false;
      this.patch({ usageLoading: false, error: codexUsageErrorText(error) });
      return false;
    } finally {
      this.usageReads.settle(readId);
    }
  }

  private async loadCredits(epoch: number, force: boolean): Promise<boolean> {
    if (this.creditsReads.busy(epoch)) return false;
    const context = this.options.read();
    const client = context.client;
    if (client === null || !this.isLive(epoch, context)) return false;
    if (!force && this.state.credits !== null && this.now() - this.creditsAt < this.cacheTtlMs) {
      return true;
    }
    const readId = this.creditsReads.begin(epoch);
    this.patch({ creditsLoading: true, creditsError: null });
    try {
      const view = await client.getCodexResetCredits(context.session, force);
      if (!this.creditsReads.isCurrent(readId) || !this.isLive(epoch, context)) return false;
      if (!sameSession(view.session, context.session)) {
        this.patch({ creditsLoading: false, creditsError: CODEX_USAGE_READ_ERROR });
        return false;
      }
      this.creditsAt = this.now();
      const patch: Partial<CodexUsageState> = {
        credits: view,
        creditsLoading: false,
        creditsError: null,
      };
      // A read issued *after* a write is the authoritative verdict on it: the
      // account's own answer settles a record whose outcome was unknown.  A read
      // that was already on the wire when the write went out proves nothing, and
      // neither does one that lands while the write is still unresolved in flight.
      if (!this.consuming && this.state.unresolved !== null && readId > this.unresolvedReadFloor) {
        patch.unresolved = null;
      }
      this.patch(patch);
      return true;
    } catch (error) {
      if (!this.creditsReads.isCurrent(readId) || !this.isLive(epoch, context)) return false;
      this.patch({ creditsLoading: false, creditsError: codexUsageErrorText(error) });
      return false;
    } finally {
      this.creditsReads.settle(readId);
    }
  }

  private async refreshAll(epoch: number, force: boolean): Promise<void> {
    await Promise.all([this.loadUsage(epoch, force), this.loadCredits(epoch, force)]);
  }

  // -- the one write ----------------------------------------------------------

  private async runConsume(
    epoch: number,
    client: CodexUsagePort,
    session: SessionRef,
    pending: PendingReset,
  ): Promise<void> {
    try {
      const result = await client.consumeCodexResetCredit({
        session,
        expected_model: pending.model,
        credit_id: pending.creditId,
        command_id: pending.commandId,
        confirmed: true,
      });
      if (!this.isLive(epoch, this.options.read())) {
        // The context moved on (or the entry was hidden) while the write was in
        // flight.  The write *was* sent, so `unresolved` stays: nothing may offer
        // this credit again under a fresh command id until a read issued after the
        // write says what the account thinks.
        return;
      }
      if (!this.matchesRequest(result, session, pending)) {
        // A reply that does not describe *this* request is not an outcome.  Commit
        // nothing, and keep the record as unknown rather than acting on someone
        // else's result.
        this.consuming = false;
        this.patch({
          consuming: false,
          lastOutcome: 'unknown',
          notice: CODEX_USAGE_UNKNOWN_OUTCOME_NOTICE,
        });
        return;
      }
      this.consuming = false;
      // A definite outcome is the authoritative answer to this write; the literal
      // `unknown` one is not, so its record stays until a read settles it.
      this.patch({
        consuming: false,
        lastOutcome: result.outcome,
        ...(result.outcome === 'unknown' ? {} : { unresolved: null }),
      });
      if (result.outcome === 'reset') {
        // The credit is spent.  Drop it from the painted list *now*, so the
        // await for the refreshed rows can never re-offer what was just used,
        // then refresh both views and say which half succeeded.  Both reads are
        // retired first: a reply that was already on the wire describes the
        // pre-consume account, so it must not paint (and must not be mistaken for
        // the post-consume refresh being in flight already).
        this.dropCredit(pending.creditId);
        this.usageReads.invalidate();
        this.creditsReads.invalidate();
        const [usageOk, creditsOk] = await Promise.all([
          this.loadUsage(epoch, true), this.loadCredits(epoch, true),
        ]);
        if (!this.isLive(epoch, this.options.read())) return;
        this.patch({
          notice: usageOk && creditsOk ? CODEX_USAGE_REDEEMED_NOTICE : CODEX_USAGE_REFRESH_FAILED_NOTICE,
        });
        return;
      }
      this.patch({ notice: outcomeNotice(result.outcome) });
      if (result.outcome !== 'nothingToReset' && result.outcome !== 'unknown') {
        this.creditsReads.invalidate();
        await this.loadCredits(epoch, true);
      }
    } catch (error) {
      if (!this.isLive(epoch, this.options.read())) return;
      this.consuming = false;
      // Never retried, never replayed under a new command id: the account may
      // already have been charged, so the user has to refresh and check.  The
      // record stays (it was set when the write was sent) and keeps the credit
      // out of reach until an authoritative read clears it.
      this.patch({
        consuming: false,
        lastOutcome: 'unknown',
        notice: `${CODEX_USAGE_UNKNOWN_OUTCOME_NOTICE}（${codexUsageErrorText(
          error,
          CODEX_USAGE_CONSUME_ERROR,
        )}）`,
      });
    }
  }

  /**
   * Whether a consume reply describes the request that was sent.
   *
   * The server echoes the session, the model it acted on and the command id; any
   * mismatch means the reply is not evidence about *this* credit, so it is treated
   * as an unknown outcome instead of committing it.
   */
  private matchesRequest(
    result: CodexConsumeResult,
    session: SessionRef,
    pending: PendingReset,
  ): boolean {
    return (
      sameSession(result.session, session) &&
      result.model === pending.model &&
      result.command_id === pending.commandId
    );
  }

  /** Remove a spent credit from the painted rows and the advertised count. */
  private dropCredit(creditId: string): void {
    const credits = this.state.credits;
    if (credits === null) return;
    const rows = credits.credits.filter((row) => row.id !== creditId);
    if (rows.length === credits.credits.length) return;
    this.patch({
      credits: {
        ...credits,
        credits: rows,
        available_count: Math.max(0, credits.available_count - 1),
      },
    });
  }

  // -- helpers ----------------------------------------------------------------

  private get cacheTtlMs(): number {
    return this.options.cacheTtlMs ?? CODEX_USAGE_CACHE_TTL_MS;
  }

  private get refreshIntervalMs(): number {
    return this.options.refreshIntervalMs ?? CODEX_USAGE_REFRESH_INTERVAL_MS;
  }

  private now(): number {
    return this.options.now?.() ?? Date.now();
  }

  /** Unix *seconds*, which is what the wire timestamps are. */
  private nowSeconds(): number {
    return Math.floor(this.now() / 1000);
  }

  private eligible(context: CodexUsageContext): boolean {
    return (
      context.client !== null &&
      context.connected &&
      context.session.thread_id !== '' &&
      context.session.project_id !== ''
    );
  }

  private keyOf(context: CodexUsageContext): string {
    return [
      clientIdentity(context.client),
      this.sessionKeyOf(context),
      context.model,
      context.revision,
      context.connected ? '1' : '0',
    ].join('|');
  }

  private sessionKeyOf(context: CodexUsageContext): string {
    return `${context.session.project_id}/${context.session.thread_id}`;
  }

  private patch(patch: Partial<CodexUsageState>): void {
    this.state = { ...this.state, ...patch };
    this.options.onState(this.state);
  }
}

/** Structural session equality: the wire type is a plain pair of ids. */
function sameSession(left: SessionRef, right: SessionRef): boolean {
  return left.project_id === right.project_id && left.thread_id === right.thread_id;
}

/** The notice one non-`reset` outcome deserves. */
function outcomeNotice(outcome: CodexConsumeOutcome): string {
  switch (outcome) {
    case 'alreadyRedeemed':
      return CODEX_USAGE_ALREADY_REDEEMED_NOTICE;
    case 'nothingToReset':
      return CODEX_USAGE_NOTHING_TO_RESET_NOTICE;
    case 'noCredit':
      return CODEX_USAGE_NO_CREDIT_NOTICE;
    default:
      return CODEX_USAGE_UNKNOWN_OUTCOME_NOTICE;
  }
}
