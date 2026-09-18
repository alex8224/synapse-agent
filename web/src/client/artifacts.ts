/**
 * Compatibility re-export of the shared workspace-artifact wire surface.
 *
 * The strict `runtime.artifacts.*` decoders and the pure artifact helpers moved
 * to `src/runtime-client/artifacts.ts` (shared protocol core).  The panel keeps
 * importing them from here; nothing in this file is a browser concern.
 */
export * from '../runtime-client/artifacts.ts';