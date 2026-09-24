/**
 * Tauri 2 Native Git & Filesystem Bridge (bypassing backend RPCs when running in Tauri).
 *
 * Implements:
 * - Direct execution of git status, diff, directory listing, and file reads via Tauri 2 native IPC
 * - Seamless fallback to backend RPCs when running in standard browser/web console mode
 *
 * The artifact calls (`fetchListArtifacts` / `fetchStatArtifact` /
 * `fetchReadArtifact`) are the one exception to the fallback rule: they read the
 * reader's own filesystem, so a native failure is surfaced instead of being
 * quietly replaced by the ignore-filtered runtime RPC.  Git stays best-effort.
 */
import { isTauri } from './tauri.ts';
import type { GitStatusView, GitDiffView } from '../runtime-client/git.ts';
import { ARTIFACT_CHUNK_BYTES, ARTIFACT_LIST_LIMIT } from '../runtime-client/artifacts.ts';
import type {
  ArtifactChunkView,
  ArtifactEntry,
  ArtifactPageView,
} from '../runtime-client/artifacts.ts';
import type { SynapseRuntimeClient } from './SynapseRuntimeClient.ts';
import type { SessionRef } from '../runtime-client/contract.generated.ts';

interface TauriInternals {
  invoke: <T = unknown>(cmd: string, args?: Record<string, unknown>) => Promise<T>;
}

/** One raw directory entry as `tauri_list_artifacts` returns it. */
interface RawArtifactEntry {
  path: string;
  name: string;
  is_dir: boolean;
  size: number;
  modified_at?: string | null;
  revision?: string | null;
}

interface RawArtifactPage {
  entries: RawArtifactEntry[];
  path: string;
  total: number;
}

/** One raw stat record as `tauri_stat_artifact` returns it. */
interface RawArtifactStat {
  path: string;
  is_dir: boolean;
  size: number;
  modified_at?: string | null;
  revision?: string | null;
}

/** One raw read chunk as `tauri_read_artifact` returns it. */
interface RawArtifactChunk {
  path: string;
  offset: number;
  data_base64: string;
  byte_length: number;
  next_offset: number;
  eof: boolean;
  size: number;
  modified_at?: string | null;
  revision?: string | null;
}

/** Whether the native artifact commands are reachable from this webview. */
export function hasNativeArtifactSurface(): boolean {
  return isTauri() && getTauriInternals() !== null;
}

/**
 * Map one native record to the entry shape the file panel renders.
 *
 * The native surface reports no MIME type, exactly like the server's
 * `application/octet-stream` default: the panel's own extension fallback then
 * decides text / image / binary, so nothing is decoded that should not be.
 */
function toArtifactEntry(raw: RawArtifactEntry | RawArtifactStat): ArtifactEntry {
  return {
    path: raw.path,
    kind: raw.is_dir ? 'directory' : 'file',
    size: raw.size,
    modified_at: raw.modified_at ?? null,
    media_type: raw.is_dir ? 'inode/directory' : 'application/octet-stream',
    revision: raw.revision ?? null,
  };
}

/**
 * Open file or directory using system default program via Tauri native IPC.
 */
export async function openPathWithDefault(path: string, workspacePath?: string): Promise<void> {
  const internals = getTauriInternals();
  if (isTauri() && internals) {
    try {
      await internals.invoke('tauri_open_path', {
        workspace: workspacePath || undefined,
        path,
      });
      return;
    } catch (err) {
      console.warn('Tauri native open_path failed:', err);
    }
  }
}

/**
 * Reveal file or directory in OS file manager (Explorer / Finder) via Tauri native IPC.
 */
export async function revealInFileManager(path: string, workspacePath?: string): Promise<void> {
  const internals = getTauriInternals();
  if (isTauri() && internals) {
    try {
      await internals.invoke('tauri_reveal_in_folder', {
        workspace: workspacePath || undefined,
        path,
      });
      return;
    } catch (err) {
      console.warn('Tauri native reveal_in_folder failed:', err);
    }
  }
}

function getTauriInternals(): TauriInternals | null {
  if (typeof window === 'undefined') return null;
  const w = window as unknown as { __TAURI_INTERNALS__?: TauriInternals };
  return w.__TAURI_INTERNALS__ ?? null;
}

/**
 * Read git status: prefers Tauri 2 native command when available.
 */
export async function fetchGitStatus(
  client: SynapseRuntimeClient | null,
  session: SessionRef,
  workspacePath?: string,
): Promise<GitStatusView> {
  const internals = getTauriInternals();
  if (isTauri() && internals) {
    try {
      const res = await internals.invoke<GitStatusView>('tauri_git_status', {
        workspace: workspacePath || undefined,
      });
      return res;
    } catch (err) {
      console.warn('Tauri 2 native git status error, falling back to RPC:', err);
    }
  }

  if (!client) {
    throw new Error('Runtime client not available');
  }
  return await client.gitStatus(session);
}

/**
 * Read git diff: prefers Tauri 2 native command when available.
 */
export async function fetchGitDiff(
  client: SynapseRuntimeClient | null,
  session: SessionRef,
  filePath: string,
  workspacePath?: string,
): Promise<GitDiffView> {
  const internals = getTauriInternals();
  if (isTauri() && internals) {
    try {
      const res = await internals.invoke<GitDiffView>('tauri_git_diff', {
        workspace: workspacePath || undefined,
        path: filePath,
      });
      return res;
    } catch (err) {
      console.warn('Tauri 2 native git diff error, falling back to RPC:', err);
    }
  }

  if (!client) {
    throw new Error('Runtime client not available');
  }
  return await client.gitDiff(session, filePath);
}

/**
 * List one workspace directory: the Tauri 2 native command when available,
 * otherwise the runtime RPC.
 *
 * The native listing is deliberately *not* filtered by the workspace ignore
 * policy (`.gitignore` / `deny_fs_paths`): the desktop file tree is the reader's
 * own filesystem view, so build output such as `rust/synapse-gui/target` stays
 * visible.
 * For the same reason a native failure is reported instead of falling back to
 * the filtered RPC, which would silently hide exactly those paths.
 *
 * `cursor`/`limit` are the RPC's paging contract and are forwarded verbatim; the
 * native listing answers one whole directory and reports `nextCursor: null`.
 */
export async function fetchListArtifacts(
  client: SynapseRuntimeClient | null,
  session: SessionRef,
  subpath?: string,
  workspacePath?: string,
  cursor: string | null = null,
  limit: number = ARTIFACT_LIST_LIMIT,
): Promise<ArtifactPageView> {
  const internals = getTauriInternals();
  if (isTauri() && internals) {
    const raw = await internals.invoke<RawArtifactPage>('tauri_list_artifacts', {
      workspace: workspacePath || undefined,
      subpath: subpath || undefined,
    });
    return {
      entries: raw.entries.map(toArtifactEntry),
      // The native listing answers one directory in a single page.
      nextCursor: null,
      path: raw.path,
    };
  }

  if (!client) {
    throw new Error('Runtime client not available');
  }
  return await client.listArtifacts(session, subpath || '.', cursor, limit);
}

/**
 * Stat one workspace artifact: the Tauri 2 native command when available,
 * otherwise the runtime RPC.
 */
export async function fetchStatArtifact(
  client: SynapseRuntimeClient | null,
  session: SessionRef,
  path: string,
  workspacePath?: string,
): Promise<ArtifactEntry> {
  const internals = getTauriInternals();
  if (isTauri() && internals) {
    const raw = await internals.invoke<RawArtifactStat>('tauri_stat_artifact', {
      workspace: workspacePath || undefined,
      path,
    });
    return toArtifactEntry(raw);
  }

  if (!client) {
    throw new Error('Runtime client not available');
  }
  return await client.statArtifact(session, path);
}

/**
 * Read one bounded byte range of a workspace artifact: the Tauri 2 native
 * command when available, otherwise the runtime RPC.
 *
 * Both sides clamp `limit` to their own ceiling, so one call never pulls a whole
 * build artifact into memory; `nextOffset` / `eof` drive the caller's loop
 * exactly like the RPC chunk does.  A native chunk whose fingerprint no longer
 * matches the caller's expectation is refused here, so the desktop surface
 * reports a changed file the same way the server reports `artifact_changed`.
 */
export async function fetchReadArtifact(
  client: SynapseRuntimeClient | null,
  session: SessionRef,
  path: string,
  offset = 0,
  limit: number = ARTIFACT_CHUNK_BYTES,
  expectedRevision: string | null = null,
  workspacePath?: string,
): Promise<ArtifactChunkView> {
  const internals = getTauriInternals();
  if (isTauri() && internals) {
    const raw = await internals.invoke<RawArtifactChunk>('tauri_read_artifact', {
      workspace: workspacePath || undefined,
      path,
      offset,
      limit,
    });
    if (expectedRevision !== null && (raw.revision ?? null) !== expectedRevision) {
      throw new Error('读取期间文件已变化，请重新读取');
    }
    return {
      path: raw.path,
      offset: raw.offset,
      data_base64: raw.data_base64,
      byteLength: raw.byte_length,
      nextOffset: raw.next_offset,
      eof: raw.eof,
      metadata: toArtifactEntry({
        path: raw.path,
        is_dir: false,
        size: raw.size,
        modified_at: raw.modified_at,
        revision: raw.revision,
      }),
    };
  }

  if (!client) {
    throw new Error('Runtime client not available');
  }
  return await client.readArtifact(session, path, offset, limit, expectedRevision);
}
