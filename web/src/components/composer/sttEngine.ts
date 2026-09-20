/**
 * Which speech engine the console runs, and when the local one needs building.
 *
 * One rule, kept out of the card so it can be tested without a browser: the
 * console only asks the host to build the local models when the local engine is
 * both *selected* and *usable*.  Asking in any other state would either be
 * pointless (the browser engine is in use) or fail (the models are missing), and
 * the build is expensive enough -- about a minute on a CPU -- that a wrong ask is
 * a visible stall rather than a wasted call.
 */
import type { SttStatusView } from '../../runtime-client/types.ts';

/**
 * Whether the host should be asked to build the local models now.
 *
 * `loaded` is what makes it idempotent from the console's side: once the models
 * are in memory there is nothing to warm, and a second ask would be a round trip
 * that only proves the first one worked.
 */
export function needsWarmUp(status: SttStatusView | null): boolean {
  if (status === null) return false;
  if (status.engine !== 'local') return false;
  return status.available && !status.loaded;
}

/**
 * Whether the local engine is the one that will actually run.
 *
 * The configured mode is not enough: a `local` mode whose models are missing falls
 * back to the browser engine, and the card must say so rather than let the reader
 * wonder why the quality did not change.
 */
export function usesLocalEngine(status: SttStatusView | null): boolean {
  return status !== null && status.engine === 'local' && status.available;
}

/**
 * What a reader should see when an engine write fails.
 *
 * A daemon that predates `runtime.stt.set_engine` answers `method not found`,
 * which is not a speech problem at all -- it is a console that needs restarting,
 * and saying so is the difference between "this feature is broken" and "restart
 * the console".  Every other failure keeps the server's own words.
 */
export function speechEngineErrorMessage(failure: unknown): string {
  const error = failure as { service_code?: unknown; code?: unknown; message?: unknown } | null;
  if (error !== null && (error.service_code === 'method_not_found' || error.code === -32601)) {
    return '运行时版本过旧：请重启控制台后重试';
  }
  const message = typeof error?.message === 'string' ? error.message : '';
  return message === '' ? '设置语音引擎失败' : message;
}
