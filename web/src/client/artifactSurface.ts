/**
 * The single workspace-artifact surface every file view talks to.
 *
 * On the desktop build (Tauri) all three calls go through the native bridge,
 * which reads the reader's own filesystem: build output the workspace policy
 * ignores (a Rust crate's `target/` directory, `.venv`, ...) is visible, and a
 * native failure is reported instead of being silently replaced by the filtered
 * runtime RPC.
 * In the browser console the surface is the read-only `runtime.artifacts.*`
 * RPC, unchanged.
 *
 * The shape is deliberately the intersection of what the callers already need:
 * `ArtifactLister` (the bounded bare-name walk in `artifactLocate`) and
 * `ArtifactReadTarget` (the bounded image loader in `runtime-client/artifacts`),
 * plus `statArtifact` for opening one path from a transcript reference.
 */
import {
  fetchListArtifacts,
  fetchReadArtifact,
  fetchStatArtifact,
  hasNativeArtifactSurface,
} from './tauriGitFs.ts';
import type { SynapseRuntimeClient } from './SynapseRuntimeClient.ts';
import type {
  ArtifactChunkView,
  ArtifactEntry,
  ArtifactPageView,
} from '../runtime-client/artifacts.ts';
import type { SessionRef } from '../runtime-client/contract.generated.ts';

export interface ArtifactSurface {
  /**
   * List one workspace directory.
   *
   * `cursor`/`limit` only reach the RPC: the native listing answers a whole
   * directory in one page and reports `nextCursor: null`.
   */
  listArtifacts(
    session: SessionRef,
    path: string,
    cursor: string | null,
    limit: number,
  ): Promise<ArtifactPageView>;
  statArtifact(session: SessionRef, path: string): Promise<ArtifactEntry>;
  readArtifact(
    session: SessionRef,
    path: string,
    offset: number,
    limit: number,
    expectedRevision: string | null,
  ): Promise<ArtifactChunkView>;
}

/**
 * Build the artifact surface for this webview, or `null` when there is nothing
 * to read through yet.
 *
 * The native bridge needs no runtime connection at all, so on the desktop build
 * the file views work while the RPC is still connecting.  Without it the surface
 * is the runtime RPC, which needs a client that finished its handshake: calling
 * one mid-connect would only fail, and the caller's gate (`null` = "not ready")
 * keeps the previous behaviour of not starting a doomed read.
 *
 * `rpcReady` is passed in rather than read from `client.getState()` so the
 * caller's `useMemo` can depend on the store's own connection signal: readiness
 * arrives *after* the client object does, and a memo that only watched the
 * client would cache the not-ready `null` forever.
 *
 * `workspacePath` is the attached project's host path.  It is what the native
 * commands resolve against, so it must be the path the session actually runs in;
 * an empty value makes the native side fall back to its own working directory,
 * which is why the callers pass the project path rather than a guess.
 */
export function createArtifactSurface(
  client: SynapseRuntimeClient | null,
  workspacePath: string,
  rpcReady: boolean,
): ArtifactSurface | null {
  if (!hasNativeArtifactSurface() && (client === null || !rpcReady)) {
    return null;
  }
  return {
    listArtifacts: (session, path, cursor, limit) =>
      fetchListArtifacts(client, session, path, workspacePath, cursor, limit),
    statArtifact: (session, path) => fetchStatArtifact(client, session, path, workspacePath),
    readArtifact: (session, path, offset, limit, expectedRevision) =>
      fetchReadArtifact(client, session, path, offset, limit, expectedRevision, workspacePath),
  };
}
