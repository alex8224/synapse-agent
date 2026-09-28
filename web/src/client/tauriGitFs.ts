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
import {
  GIT_LOG_PAGE_SIZE,
  parseGitCommitDetail,
  parseGitDiff,
  parseGitLog,
  parseGitRefs,
  parseGitStatus,
  type GitCommitDetailView,
  type GitDiffView,
  type GitLogView,
  type GitRefsView,
  type GitStatusView,
} from '../runtime-client/git.ts';
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
 * Resolve a path to its absolute filesystem form given a workspace root.
 */
export function toAbsolutePath(path: string, workspacePath?: string): string {
  const cleanPath = path.replace(/\\/g, '/');
  const ws = (workspacePath || '').replace(/\\/g, '/').replace(/\/+$/, '');
  const isAbs =
    /^[a-zA-Z]:[/\\]/.test(cleanPath) || cleanPath.startsWith('\\\\') || (cleanPath.startsWith('/') && !ws);
  if (isAbs) return cleanPath;
  return ws ? `${ws}/${cleanPath.replace(/^\/+/, '')}` : cleanPath;
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
export async function openPathWithDefault(
  path: string,
  workspacePath?: string,
  client?: SynapseRuntimeClient | null,
  session?: SessionRef,
): Promise<void> {
  const internals = getTauriInternals();
  const absPath = toAbsolutePath(path, workspacePath);
  if (isTauri() && internals) {
    try {
      await internals.invoke('tauri_open_path', {
        workspace: workspacePath || undefined,
        path: absPath,
      });
      return;
    } catch (err) {
      console.warn('Tauri native open_path failed:', err);
    }
  }

  if (client && session?.thread_id) {
    try {
      const cleanRel = path.replace(/\\/g, '/').replace(/^\/+/, '');
      await client.openExternal({ session, path: cleanRel });
    } catch (err) {
      console.warn('Runtime client openExternal failed:', err);
    }
  }
}

/**
 * Reveal file or directory in OS file manager (Explorer / Finder) via Tauri native IPC.
 */
export async function revealInFileManager(
  path: string,
  workspacePath?: string,
  client?: SynapseRuntimeClient | null,
  session?: SessionRef,
): Promise<void> {
  const internals = getTauriInternals();
  const absPath = toAbsolutePath(path, workspacePath);
  if (isTauri() && internals) {
    try {
      await internals.invoke('tauri_reveal_in_folder', {
        workspace: workspacePath || undefined,
        path: absPath,
      });
      return;
    } catch (err) {
      console.warn('Tauri native reveal_in_folder failed:', err);
    }
  }

  if (client && session?.thread_id) {
    try {
      const cleanRel = path.replace(/\\/g, '/').replace(/^\/+/, '');
      await client.openExternal({ session, path: cleanRel, mode: 'reveal' });
    } catch (err) {
      console.warn('Runtime client reveal failed:', err);
    }
  }
}

/**
 * Open file in VS Code: prefers native Tauri IPC with fallback to runtime client or vscode:// protocol.
 */
export async function openInVsCode(
  path: string,
  workspacePath?: string,
  client?: SynapseRuntimeClient | null,
  session?: SessionRef,
): Promise<void> {
  const internals = getTauriInternals();
  const absPath = toAbsolutePath(path, workspacePath);
  const cleanRel = path.replace(/\\/g, '/').replace(/^\/+/, '');

  if (isTauri() && internals) {
    try {
      await internals.invoke('tauri_open_in_vscode', {
        workspace: workspacePath || undefined,
        path: absPath,
      });
      return;
    } catch (err) {
      console.warn('Tauri native open_in_vscode failed:', err);
    }
  }

  if (client && session?.thread_id) {
    try {
      await client.openExternal({ session, path: cleanRel, appId: 'vscode' });
      return;
    } catch {
      // Degrade to URL scheme
    }
  }

  if (absPath) {
    const formatted = absPath.startsWith('/') ? absPath : `/${absPath}`;
    window.open(`vscode://file${encodeURI(formatted)}`, '_self');
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
      const res = await internals.invoke<unknown>('tauri_git_status', {
        workspace: workspacePath || undefined,
      });
      return parseGitStatus(res);
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
      const res = await internals.invoke<unknown>('tauri_git_diff', {
        workspace: workspacePath || undefined,
        path: filePath,
      });
      return parseGitDiff(res);
    } catch (err) {
      console.warn('Tauri 2 native git diff error, falling back to RPC:', err);
    }
  }

  if (!client) {
    throw new Error('Runtime client not available');
  }
  return await client.gitDiff(session, filePath);
}

/** Raised when a history read is asked for outside the desktop shell. */
export class GitHistoryUnavailableError extends Error {
  constructor(message = 'git 历史只在桌面壳中可用') {
    super(message);
    this.name = 'GitHistoryUnavailableError';
  }
}

/**
 * Whether the desktop shell can answer git history at all.
 *
 * The history panel is native-only by decision: the shell reads the repository
 * itself, and there is no runtime RPC behind it to fall back to (the daemon
 * serves `git.status` / `git.diff` only). A plain browser therefore hides the
 * panel instead of showing one that could never load -- the same rule the
 * artifact calls follow, which is why both expose a probe rather than a
 * fallback.
 */
export function hasNativeGitHistory(): boolean {
  return isTauri() && getTauriInternals() !== null;
}

/** The native internals, or the typed error that says why there are none. */
function requireGitHistory(): TauriInternals {
  const internals = getTauriInternals();
  if (!isTauri() || !internals) {
    throw new GitHistoryUnavailableError();
  }
  return internals;
}

/** Branches, tags, stashes and worktrees of the session's workspace. */
export async function fetchGitRefs(workspacePath?: string): Promise<GitRefsView> {
  const internals = requireGitHistory();
  const res = await internals.invoke<unknown>('tauri_git_refs', {
    workspace: workspacePath || undefined,
  });
  return parseGitRefs(res);
}

/** What one page of history is asked for. */
export interface GitLogRequest {
  /** A branch, a tag or a sha; `null` means the current `HEAD`. */
  rev?: string | null;
  limit?: number;
  skip?: number;
  /** Only the commits on this branch itself, not the ones merged into it. */
  firstParent?: boolean;
  path?: string | null;
}

/** One page of a revision's history, newest first. */
export async function fetchGitLog(
  request: GitLogRequest = {},
  workspacePath?: string,
): Promise<GitLogView> {
  const internals = requireGitHistory();
  const res = await internals.invoke<unknown>('tauri_git_log', {
    workspace: workspacePath || undefined,
    rev: request.rev ?? undefined,
    limit: request.limit ?? GIT_LOG_PAGE_SIZE,
    skip: request.skip ?? 0,
    // The shell's command is `rename_all = "snake_case"`, so this key stays
    // snake_case instead of being camelCased on the way out.
    first_parent: request.firstParent ?? false,
    path: request.path ?? undefined,
  });
  return parseGitLog(res);
}

/**
 * One commit: its metadata, the files it changed against its first parent, and
 * -- when a path is given -- that file's diff inside the commit.
 */
export async function fetchGitCommit(
  sha: string,
  path?: string,
  workspacePath?: string,
): Promise<GitCommitDetailView> {
  const internals = requireGitHistory();
  const res = await internals.invoke<unknown>('tauri_git_commit', {
    workspace: workspacePath || undefined,
    sha,
    path: path ?? undefined,
  });
  return parseGitCommitDetail(res);
}

/** One stash's diff. Read-only: `git stash show -p` never applies or drops it. */
export async function fetchGitStashDiff(
  index: number,
  workspacePath?: string,
): Promise<GitDiffView> {
  const internals = requireGitHistory();
  const res = await internals.invoke<unknown>('tauri_git_stash', {
    workspace: workspacePath || undefined,
    index,
  });
  return parseGitDiff(res);
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
