/**
 * Per-PTY serial command queue.
 *
 * The native terminal commands (`tauri_terminal_write` / `_resize` / `_close`)
 * are `async` and run the blocking PTY call on a `spawn_blocking` worker, so two
 * `invoke`s started back-to-back can reach the PTY out of order. A lone
 * keystroke tolerates that, but a session lifecycle does not: a resize racing a
 * write, or an older resize landing after a newer one, leaves the PTY at the
 * wrong size, and a write can outlive the view that issued it.
 *
 * `TerminalInputQueue` only serializes keystrokes from a single view instance.
 * It cannot order across effect rebuilds — an appearance/theme change recreates
 * the view and therefore a fresh input queue for the same PTY — so the ordering
 * guarantee has to live one level down, in the bridge. This queue is keyed by
 * PTY id, so every write, resize and close for one PTY shares a single FIFO lane
 * no matter which component instance issued it.
 *
 * `run` returns a promise that settles with that command's own outcome: a
 * previous command's failure is swallowed so it cannot poison the lane, but the
 * failing command's own caller still observes its rejection. A lane is removed
 * from the map as soon as it settles with no successor queued, so per-session
 * state does not accumulate for the lifetime of the page. Different PTY ids run
 * on independent lanes and never block each other.
 */

/** The single PTY command the queue serializes, e.g. `() => writeTerminal(id, data)`. */
export type TerminalCommandOperation<T> = () => Promise<T>;

export class TerminalCommandQueue {
  private readonly lanes = new Map<number, Promise<void>>();

  /** Number of live PTY lanes (diagnostics/tests). */
  get laneCount(): number {
    return this.lanes.size;
  }

  /**
   * Queue `operation` behind everything already running for `id`. The returned
   * promise resolves/rejects with `operation`'s own outcome; the lane's stored
   * tail never rejects, so one failure cannot strand or poison the commands that
   * follow it.
   */
  run<T>(id: number, operation: TerminalCommandOperation<T>): Promise<T> {
    const previous = this.lanes.get(id) ?? Promise.resolve();
    // A prior failure is swallowed here so it only surfaces to its own caller,
    // never to the command queued behind it.
    const result = previous.catch(() => undefined).then(operation);
    // Store a settled (never-rejecting) continuation as the lane tail. Attaching
    // both handlers also marks `result` as handled, so a caller that ignores the
    // returned promise cannot trigger an unhandled rejection.
    const lane = result.then(
      () => undefined,
      () => undefined,
    );
    this.lanes.set(id, lane);
    void lane.then(() => {
      // Only a tail that is still current may clean up: a successor, if any, has
      // already replaced the map entry and owns the teardown now.
      if (this.lanes.get(id) === lane) {
        this.lanes.delete(id);
      }
    });
    return result;
  }
}
