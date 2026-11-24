/**
 * Lifecycle controller for the external ACP (e.g. `agy`) usage bottom-bar entry.
 *
 * Mirrors `codexUsageController.ts` without the credit-redemption write path:
 * - discovers whether the bound external agent supports usage reads (`supported`),
 * - keeps the entry hidden (`available: false`) for native sessions and for
 *   external agents without a usage surface,
 * - caches reads for 300s, ticks every 60s while visible, and drops stale
 *   replies whenever the session, agent, client, or connection changes.
 *
 * Pure TypeScript (no React / zustand / DOM) so it runs under `node --test`.
 */
import type { SessionRef } from '../runtime-client/types.ts';
import type { ExternalUsageView } from '../runtime-client/externalUsage.ts';
import {
  EXTERNAL_USAGE_READ_ERROR,
  externalUsageErrorText,
} from './externalUsageView.ts';

export interface ExternalUsagePort {
  getExternalUsage(session: SessionRef, force?: boolean): Promise<ExternalUsageView>;
}

export interface ExternalUsageContext {
  client: ExternalUsagePort | null;
  session: SessionRef;
  /** Bound external agent id (`'agy'`, etc.), or `null` for native sessions. */
  agentId: string | null;
  connected: boolean;
}

export interface ExternalUsageState {
  /** Whether the bottom bar entry should be rendered at all. */
  available: boolean;
  agentId: string;
  usage: ExternalUsageView | null;
  loading: boolean;
  error: string | null;
}

export interface ExternalUsageControllerOptions {
  read: () => ExternalUsageContext;
  subscribe: (listener: () => void) => () => void;
  onState: (state: ExternalUsageState) => void;
  now?: () => number;
  schedule?: (run: () => void, delayMs: number) => number;
  cancel?: (handle: number) => void;
  isVisible?: () => boolean;
  cacheTtlMs?: number;
  refreshIntervalMs?: number;
}

export const EXTERNAL_USAGE_CACHE_TTL_MS = 300_000;
export const EXTERNAL_USAGE_REFRESH_INTERVAL_MS = 60_000;

const clientIdentities = new WeakMap<object, number>();
let nextClientIdentity = 1;

function clientIdentity(client: ExternalUsagePort | null): number {
  if (client === null) return 0;
  const key = client as unknown as object;
  const existing = clientIdentities.get(key);
  if (existing !== undefined) return existing;
  const assigned = nextClientIdentity++;
  clientIdentities.set(key, assigned);
  return assigned;
}

function sameSession(left: SessionRef, right: SessionRef): boolean {
  return left.project_id === right.project_id && left.thread_id === right.thread_id;
}

export class ExternalUsageController {
  private readonly options: ExternalUsageControllerOptions;
  private state: ExternalUsageState;
  private epoch = 0;
  private key = '';
  private usageAt = 0;
  private inFlightEpoch: number | null = null;
  private latestReadId = 0;
  private probedUnsupported = false;
  private started = 0;
  private unsubscribe: (() => void) | null = null;
  private timer: number | null = null;

  constructor(options: ExternalUsageControllerOptions) {
    this.options = options;
    this.state = {
      available: false,
      agentId: '',
      usage: null,
      loading: false,
      error: null,
    };
  }

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
    this.retire();
  }

  getState(): ExternalUsageState {
    return this.state;
  }

  isAvailable(): boolean {
    return this.state.available;
  }

  refresh(): void {
    void this.loadUsage(this.epoch, true);
  }

  private sync(): void {
    const context = this.options.read();
    const key = this.keyOf(context);
    if (key === this.key) return;
    this.key = key;
    this.epoch += 1;
    this.usageAt = 0;
    this.inFlightEpoch = null;
    this.latestReadId += 1;
    this.probedUnsupported = false;
    this.patch({
      available: false,
      agentId: context.agentId ?? '',
      usage: null,
      loading: false,
      error: null,
    });
    if (!this.eligible(context)) return;
    void this.loadUsage(this.epoch, false);
  }

  private retire(): void {
    this.epoch += 1;
    this.key = '';
    this.inFlightEpoch = null;
    this.latestReadId += 1;
    this.probedUnsupported = false;
    this.patch({ loading: false });
  }

  private schedule(): number | null {
    const run = () => {
      this.timer = this.schedule();
      this.tick();
    };
    return this.options.schedule?.(run, this.refreshIntervalMs) ?? null;
  }

  private tick(): void {
    if (this.options.isVisible !== undefined && !this.options.isVisible()) return;
    const context = this.options.read();
    if (!this.eligible(context)) return;
    if (this.probedUnsupported) return;
    void this.loadUsage(this.epoch, false);
  }

  private isLive(epoch: number, context: ExternalUsageContext): boolean {
    if (epoch !== this.epoch || this.started === 0) return false;
    if (!this.eligible(context)) return false;
    return this.keyOf(this.options.read()) === this.key;
  }

  private async loadUsage(epoch: number, force: boolean): Promise<boolean> {
    if (this.inFlightEpoch === epoch) return false;
    const context = this.options.read();
    const client = context.client;
    if (client === null || !this.isLive(epoch, context)) return false;
    if (!force && this.state.usage !== null && this.now() - this.usageAt < this.cacheTtlMs) {
      return true;
    }
    const readId = ++this.latestReadId;
    this.inFlightEpoch = epoch;
    this.patch({ loading: true, error: null });
    try {
      const view = await client.getExternalUsage(context.session, force);
      if (readId !== this.latestReadId || !this.isLive(epoch, context)) return false;
      if (!sameSession(view.session, context.session)) {
        this.patch({ loading: false, error: EXTERNAL_USAGE_READ_ERROR });
        return false;
      }
      if (!view.supported) {
        this.probedUnsupported = true;
        this.patch({
          available: false,
          usage: null,
          loading: false,
          error: null,
        });
        return false;
      }
      this.usageAt = this.now();
      this.patch({
        available: true,
        agentId: view.agent_id,
        usage: view,
        loading: false,
        error: null,
      });
      return true;
    } catch (error) {
      if (readId !== this.latestReadId || !this.isLive(epoch, context)) return false;
      this.patch({
        loading: false,
        error: externalUsageErrorText(error),
      });
      return false;
    } finally {
      if (this.inFlightEpoch === epoch) {
        this.inFlightEpoch = null;
      }
    }
  }

  private get cacheTtlMs(): number {
    return this.options.cacheTtlMs ?? EXTERNAL_USAGE_CACHE_TTL_MS;
  }

  private get refreshIntervalMs(): number {
    return this.options.refreshIntervalMs ?? EXTERNAL_USAGE_REFRESH_INTERVAL_MS;
  }

  private now(): number {
    return this.options.now?.() ?? Date.now();
  }

  private eligible(context: ExternalUsageContext): boolean {
    return (
      context.client !== null &&
      context.connected &&
      context.agentId !== null &&
      context.agentId !== '' &&
      context.session.thread_id !== '' &&
      context.session.project_id !== ''
    );
  }

  private keyOf(context: ExternalUsageContext): string {
    return [
      clientIdentity(context.client),
      `${context.session.project_id}/${context.session.thread_id}`,
      context.agentId ?? '',
      context.connected ? '1' : '0',
    ].join('|');
  }

  private patch(patch: Partial<ExternalUsageState>): void {
    this.state = { ...this.state, ...patch };
    this.options.onState(this.state);
  }
}
