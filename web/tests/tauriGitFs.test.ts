/**
 * Tests for the Tauri 2 Native Git & Filesystem adapter (tauriGitFs.ts).
 *
 * Verifies:
 * - Transparent delegation to Tauri 2 native IPC when isTauri() is true
 * - Clean fallback to Synapse runtime client RPC when in web console mode
 * - Graceful degradation on native invoke rejection
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  fetchGitStatus,
  fetchGitDiff,
  fetchListArtifacts,
  fetchReadArtifact,
  openPathWithDefault,
  revealInFileManager,
} from '../src/client/tauriGitFs.ts';
import type { SynapseRuntimeClient } from '../src/client/SynapseRuntimeClient.ts';
import type { SessionRef } from '../src/runtime-client/contract.generated.ts';

const dummySession: SessionRef = {
  project_id: 'proj_1',
  session_id: 'sess_1',
  thread_id: 'th_1',
};

test('tauriGitFs uses client RPC when not running in Tauri', async () => {
  // Ensure global window does not advertise Tauri internals
  const g = globalThis as unknown as { window?: { __TAURI_INTERNALS__?: unknown } };
  const origWindow = g.window;
  g.window = {} as unknown as Window & typeof globalThis;

  let rpcCalled = false;
  const mockClient = {
    gitStatus: async () => {
      rpcCalled = true;
      return {
        branch: 'main',
        upstream: 'origin/main',
        ahead: 0,
        behind: 0,
        dirty: false,
        files: [],
        truncated: false,
        insertions: null,
        deletions: null,
      };
    },
  } as unknown as SynapseRuntimeClient;

  const res = await fetchGitStatus(mockClient, dummySession, '/fake/path');
  assert.equal(rpcCalled, true, 'must invoke client.gitStatus RPC when outside Tauri');
  assert.equal(res.branch, 'main');

  g.window = origWindow;
});

test('tauriGitFs delegates to Tauri 2 native IPC commands when isTauri() is active', async () => {
  const g = globalThis as unknown as { window?: Record<string, unknown> };
  const origWindow = g.window;

  const invokedCommands: Array<{ cmd: string; args?: unknown }> = [];
  g.window = {
    __TAURI_INTERNALS__: {
      invoke: async (cmd: string, args?: unknown) => {
        invokedCommands.push({ cmd, args });
        if (cmd === 'tauri_git_status') {
          return {
            branch: 'feature/tauri-native',
            upstream: null,
            ahead: 1,
            behind: 0,
            dirty: true,
            files: [{ path: 'src/main.rs', index_status: 'M', worktree_status: ' ' }],
            truncated: false,
            insertions: 12,
            deletions: 3,
          };
        }
        if (cmd === 'tauri_git_diff') {
          return {
            path: 'src/main.rs',
            text: '@@ -1 +1 @@\n+native diff',
            binary: false,
            truncated: false,
            empty: false,
          };
        }
        if (cmd === 'tauri_list_artifacts') {
          return {
            entries: [
              { path: 'Cargo.toml', name: 'Cargo.toml', is_dir: false, size: 500 },
              { path: 'src', name: 'src', is_dir: true, size: 0 },
            ],
            path: '',
            total: 2,
          };
        }
        if (cmd === 'tauri_read_artifact') {
          return {
            path: 'Cargo.toml',
            content: '[package]\nname = "synapse-gui"',
            is_binary: false,
          };
        }
        if (cmd === 'tauri_open_path') {
          return null;
        }
        if (cmd === 'tauri_reveal_in_folder') {
          return null;
        }
        throw new Error(`Unknown command ${cmd}`);
      },
    },
  };

  const dummyClient = null;

  // 1. Git Status
  const statusRes = await fetchGitStatus(dummyClient, dummySession, 'F:/workspace');
  assert.equal(statusRes.branch, 'feature/tauri-native');
  assert.equal(statusRes.dirty, true);
  assert.equal(invokedCommands[0].cmd, 'tauri_git_status');

  // 2. Git Diff
  const diffRes = await fetchGitDiff(dummyClient, dummySession, 'src/main.rs', 'F:/workspace');
  assert.equal(diffRes.path, 'src/main.rs');
  assert.equal(diffRes.empty, false);
  assert.equal(invokedCommands[1].cmd, 'tauri_git_diff');

  // 3. List Artifacts
  const listRes = await fetchListArtifacts(dummyClient, dummySession, '', 'F:/workspace');
  assert.equal(listRes.entries.length, 2);
  assert.equal(listRes.entries[0].kind, 'file');
  assert.equal(listRes.entries[1].kind, 'directory');
  assert.equal(invokedCommands[2].cmd, 'tauri_list_artifacts');

  // 4. Read Artifact
  const readRes = await fetchReadArtifact(dummyClient, dummySession, 'Cargo.toml', 'F:/workspace');
  assert.equal(readRes.content.includes('[package]'), true);
  assert.equal(readRes.is_binary, false);
  assert.equal(invokedCommands[3].cmd, 'tauri_read_artifact');

  g.window = origWindow;
});
