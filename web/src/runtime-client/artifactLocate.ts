/**
 * Bounded, best-effort location of a bare file name inside the workspace.
 *
 * The artifact surface only lists one directory at a time, so a bare name from
 * the transcript (`agent.py`, with no directory) has no path to open.  This
 * walks directories breadth-first from the workspace root and returns the first
 * file whose basename matches, stopping at hard caps so the walk can never turn
 * into an unbounded scan.  When nothing matches (or the caps are hit) the caller
 * falls back to showing the name as a filter instead.
 */
import { ARTIFACT_LIST_LIMIT, basenameOf } from './artifacts.ts';
import type { ArtifactPageView } from './artifacts.ts';
import type { SessionRef } from './types.ts';

/** The slice of the runtime client this walk needs (kept minimal for tests). */
export interface ArtifactLister {
  listArtifacts(
    session: SessionRef,
    path: string,
    cursor: string | null,
    limit: number,
  ): Promise<ArtifactPageView>;
}

export interface LocateLimits {
  /** Directories visited before giving up. */
  maxDirectories: number;
  /** Entries seen before giving up. */
  maxEntries: number;
  /** Directory depth walked below the workspace root. */
  maxDepth: number;
  /** Page size for each list call. */
  pageSize: number;
}

export const LOCATE_LIMITS: LocateLimits = {
  maxDirectories: 120,
  maxEntries: 4000,
  maxDepth: 6,
  pageSize: ARTIFACT_LIST_LIMIT,
};

/**
 * First workspace file whose basename equals `name`, or `null` when none is
 * found within the caps.  A directory that cannot be listed is skipped rather
 * than aborting the walk.
 */
export async function locateArtifact(
  client: ArtifactLister,
  session: SessionRef,
  name: string,
  limits: LocateLimits = LOCATE_LIMITS,
): Promise<string | null> {
  const target = basenameOf(name).toLowerCase();
  if (target === '') return null;
  const queue: Array<{ path: string; depth: number }> = [{ path: '.', depth: 0 }];
  let directories = 0;
  let entries = 0;
  while (queue.length > 0 && directories < limits.maxDirectories && entries < limits.maxEntries) {
    const current = queue.shift();
    if (current === undefined) break;
    directories += 1;
    let page: ArtifactPageView;
    try {
      page = await client.listArtifacts(session, current.path, null, limits.pageSize);
    } catch {
      continue;
    }
    entries += page.entries.length;
    for (const entry of page.entries) {
      if (entry.kind === 'file' && basenameOf(entry.path).toLowerCase() === target) {
        return entry.path;
      }
    }
    if (current.depth < limits.maxDepth) {
      for (const entry of page.entries) {
        if (entry.kind === 'directory') {
          queue.push({ path: entry.path, depth: current.depth + 1 });
        }
      }
    }
  }
  return null;
}
