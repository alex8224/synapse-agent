/**
 * Compatibility re-export of the shared recovery decoders.
 *
 * The strict `runtime.session.reconcile` decoders and `isCoveredTurn` moved to
 * `src/runtime-client/recoverability.ts` (shared protocol core).  Existing
 * callers, including `src/stores/useConsoleStore.ts`, keep importing them from
 * here.
 */
export * from '../runtime-client/recoverability.ts';