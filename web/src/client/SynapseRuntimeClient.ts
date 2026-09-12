/**
 * Compatibility re-export of the shared runtime client (protocol core).
 *
 * The JSON-RPC client implementation moved to `src/runtime-client/` so the
 * browser console and a future desktop shell share one protocol core without
 * sharing the browser bootstrap (pairing / HTTP host discovery) or any process
 * lifecycle.  This module is a thin alias: every symbol, including the
 * `SocketLike` transport seam, keeps its previous import path so existing
 * callers and the offline node tests are unaffected.
 *
 * New code should import from `../runtime-client/SynapseRuntimeClient.ts`.
 */
export * from '../runtime-client/SynapseRuntimeClient.ts';