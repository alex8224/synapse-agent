/**
 * Compatibility re-export of the pure recovery decision logic.
 *
 * `decideResumeAfterDrop` / `resyncReasonLabel` only depend on the
 * `runtime.session.reconcile` DTO and on no view model, so they moved into the
 * shared protocol core (`src/runtime-client/recoveryDecider.ts`) together with
 * the runtime client.  Console code may keep importing them from this path;
 * shared/desktop code imports the core module directly.
 */
export * from '../runtime-client/recoveryDecider.ts';