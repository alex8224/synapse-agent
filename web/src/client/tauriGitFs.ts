/**
 * Tauri 2 Native Git & Filesystem Bridge (bypassing backend RPCs when running in Tauri).
 *
 * Implements:
 * - Direct execution of git status, diff, directory listing, and file reads via Tauri 2 native IPC
 * - Seamless fallback to backend RPCs when running in standard browser/web console mode
 */
import { isTauri } from './tauri.ts';
import type { GitStatusView, GitDiffView } from '../runtime-client/git.ts';
import type { ArtifactPageView, ArtifactChunkView } from '../runtime-client/artifacts.ts';
import type { SynapseRuntimeClient } from './SynapseRuntimeClient.ts';
import type { SessionRef } from '../runtime-client/contract.generated.ts';

interface TauriInternals {
  invoke: <T = unknown>(cmd: string, args?: Record<string, unknown>) => Promise<T>;
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
 * List workspace artifacts: prefers Tauri 2 native command when available.
 */
export async function fetchListArtifacts(
  client: SynapseRuntimeClient | null,
  session: SessionRef,
  subpath?: string,
  workspacePath?: string,
): Promise<ArtifactPageView> {
  const internals = getTauriInternals();
  if (isTauri() && internals) {
    try {
      interface RawEntry {
        path: string;
        name: string;
        is_dir: boolean;
        size: number;
        modified_at?: string | null;
      }
      interface RawPage {
        entries: RawEntry[];
        path: string;
        total: number;
      }
      const raw = await internals.invoke<RawPage>('tauri_list_artifacts', {
        workspace: workspacePath || undefined,
        subpath: subpath || undefined,
      });

      return {
        entries: raw.entries.map((e) => ({
          path: e.path,
          kind: e.is_dir ? 'directory' : 'file',
          size: e.size,
          modified_at: e.modified_at ?? null,
          media_type: e.is_dir ? 'inode/directory' : 'application/octet-stream',
          revision: null,
        })),
        nextCursor: null,
        path: raw.path,
      };
    } catch (err) {
      console.warn('Tauri 2 native list artifacts error, falling back to RPC:', err);
    }
  }

  if (!client) {
    throw new Error('Runtime client not available');
  }
  return await client.listArtifacts(session, subpath || '.');
}

/**
 * Read workspace artifact text: prefers Tauri 2 native command when available.
 */
export async function fetchReadArtifact(
  client: SynapseRuntimeClient | null,
  session: SessionRef,
  filePath: string,
  workspacePath?: string,
): Promise<{ path: string; content: string; is_binary: boolean }> {
  const internals = getTauriInternals();
  if (isTauri() && internals) {
    try {
      return await internals.invoke<{ path: string; content: string; is_binary: boolean }>(
        'tauri_read_artifact',
        {
          workspace: workspacePath || undefined,
          path: filePath,
        },
      );
    } catch (err) {
      console.warn('Tauri 2 native read artifact error, falling back to RPC:', err);
    }
  }

  if (!client) {
    throw new Error('Runtime client not available');
  }
  const chunk: ArtifactChunkView = await client.readArtifact(session, filePath);
  return {
    path: filePath,
    content: chunk.data_base64 ? atob(chunk.data_base64) : '',
    is_binary: Boolean(chunk.metadata.media_type && !chunk.metadata.media_type.startsWith('text/')),
  };
}
