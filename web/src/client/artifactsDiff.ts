/**
 * Compatibility re-export of the shared line-diff helper.
 *
 * `diffLines` moved to `src/runtime-client/artifactsDiff.ts` (shared protocol
 * core, dependency-free).  The file panel keeps importing it from here.
 */
export * from '../runtime-client/artifactsDiff.ts';