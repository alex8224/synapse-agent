/**
 * Serial PTY input queue.
 *
 * The native terminal commands are `async` and run the blocking PTY write on a
 * `spawn_blocking` worker, so two `invoke('tauri_terminal_write')` calls started
 * back-to-back can reach the PTY out of order. A terminal must never reorder or
 * drop input, so every keystroke xterm hands us goes through one queue per PTY
 * and is written strictly one call at a time: the next write only starts after
 * the previous one has settled.
 *
 * While a write is in flight the queue keeps appending to a single pending
 * string, and the next write sends that whole run at once. The coalescing only
 * merges input that has not been sent yet, so it preserves both the bytes and
 * their order — it just turns a burst of one-character writes into one call.
 *
 * A rejected write must not poison the queue: the failure is reported once to
 * the caller and the queue keeps draining the input that follows. `dispose` is
 * the teardown path — it drops the not-yet-sent input and refuses further
 * writes, so a torn-down session can never leak keystrokes into a new one.
 */

/** The single PTY write the queue serializes, e.g. `writeTerminal(id, data)`. */
export type TerminalWriteFn = (data: string) => Promise<unknown>;

export interface TerminalInputQueueOptions {
  /** Called once per failed write; the queue keeps going afterwards. */
  onError?: (error: unknown) => void;
}

export class TerminalInputQueue {
  private readonly write: TerminalWriteFn;
  private readonly onError: ((error: unknown) => void) | undefined;
  private pending = '';
  private flushing = false;
  private disposed = false;

  constructor(write: TerminalWriteFn, options: TerminalInputQueueOptions = {}) {
    this.write = write;
    this.onError = options.onError;
  }

  /** Append input in arrival order; it is sent after everything queued before it. */
  enqueue(data: string): void {
    if (this.disposed || data.length === 0) return;
    this.pending += data;
    if (!this.flushing) void this.flush();
  }

  /** Drop input that has not been sent yet and refuse all further writes. */
  dispose(): void {
    this.disposed = true;
    this.pending = '';
  }

  /** Length of the input waiting behind the in-flight write (diagnostics/tests). */
  get pendingLength(): number {
    return this.pending.length;
  }

  /**
   * Drain the pending input one write at a time. Never rejects: a failed write
   * is handed to `onError` and the loop moves on, so a single bad write cannot
   * strand the keystrokes that arrived after it.
   */
  private async flush(): Promise<void> {
    this.flushing = true;
    try {
      while (!this.disposed && this.pending.length > 0) {
        const chunk = this.pending;
        this.pending = '';
        try {
          await this.write(chunk);
        } catch (error) {
          // The failure is reported, never rethrown: a bad write must not strand
          // the input behind it, and `flush` itself must never reject. A throwing
          // reporter is itself swallowed for the same reason.
          try {
            this.onError?.(error);
          } catch {}
        }
      }
    } finally {
      this.flushing = false;
    }
  }
}
