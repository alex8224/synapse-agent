/**
 * Compatibility re-export of the shared runtime wire DTOs.
 *
 * The JSON-RPC 2.0 types and the runtime DTOs moved to
 * `src/runtime-client/types.ts`, the shared protocol core used by the browser
 * console and (later) a desktop shell.  Browser-only code may keep importing
 * them from here; shared/desktop code imports the core module directly.
 */
export * from '../runtime-client/types.ts';