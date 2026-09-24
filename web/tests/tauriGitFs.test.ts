/**
 * Tests for the Tauri 2 Native Git & Filesystem adapter (tauriGitFs.ts).
 *
 * Verifies:
 * - Transparent delegation to Tauri 2 native IPC when isTauri() is true
 * - Clean fallback to Synapse runtime client RPC when in web console mode
 * - Graceful degradation on native invoke rejection
 * - The artifact calls are native-only on the desktop build: a native failure is
 *   reported instead of silently reading the ignore-filtered RPC, and one read
 *   chunk keeps the caller's revision contract
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  fetchGitStatus,
  fetchGitDiff,
  fetchListArtifacts,
  fetchReadArtifact,
  fetchStatArtifact,
  openPathWithDefault,
  revealInFileManager,
} from '../src/client/tauriGitFs.ts';
import { decodeBase64Text } from '../src/runtime-client/artifacts.ts';
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
  const fileText = '[package]\nname = "synapse-gui"';
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
              { path: 'Cargo.toml', name: 'Cargo.toml', is_dir: false, size: 500, revision: '500:1' },
              { path: 'src', name: 'src', is_dir: true, size: 0, revision: '0:2' },
            ],
            path: '',
            total: 2,
          };
        }
        if (cmd === 'tauri_stat_artifact') {
          return {
            path: 'Cargo.toml',
            is_dir: false,
            size: fileText.length,
            modified_at: '1s',
            revision: '500:1',
          };
        }
        if (cmd === 'tauri_read_artifact') {
          return {
            path: 'Cargo.toml',
            offset: 0,
            data_base64: btoa(fileText),
            byte_length: fileText.length,
            next_offset: fileText.length,
            eof: true,
            size: fileText.length,
            modified_at: '1s',
            revision: '500:1',
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
  assert.equal(listRes.entries[0].revision, '500:1');
  assert.equal(listRes.entries[1].kind, 'directory');
  assert.equal(listRes.nextCursor, null);
  assert.equal(invokedCommands[2].cmd, 'tauri_list_artifacts');

  // 4. Stat Artifact
  const statRes = await fetchStatArtifact(dummyClient, dummySession, 'Cargo.toml', 'F:/workspace');
  assert.equal(statRes.kind, 'file');
  assert.equal(statRes.size, fileText.length);
  assert.equal(statRes.revision, '500:1');
  assert.equal(invokedCommands[3].cmd, 'tauri_stat_artifact');

  // 5. Read Artifact (one bounded chunk, not the whole file)
  const readRes = await fetchReadArtifact(
    dummyClient,
    dummySession,
    'Cargo.toml',
    0,
    4096,
    null,
    'F:/workspace',
  );
  assert.equal(readRes.eof, true);
  assert.equal(readRes.byteLength, fileText.length);
  assert.equal(readRes.nextOffset, fileText.length);
  assert.equal(decodeBase64Text(readRes.data_base64), fileText);
  assert.equal(readRes.metadata.revision, '500:1');
  assert.equal(invokedCommands[4].cmd, 'tauri_read_artifact');
  assert.deepEqual(invokedCommands[4].args, {
    workspace: 'F:/workspace',
    path: 'Cargo.toml',
    offset: 0,
    limit: 4096,
  });

  g.window = origWindow;
});

test('a native artifact failure is reported, never replaced by the filtered RPC', async () => {
  const g = globalThis as unknown as { window?: Record<string, unknown> };
  const origWindow = g.window;
  g.window = {
    __TAURI_INTERNALS__: {
      invoke: async (cmd: string) => {
        throw new Error(`${cmd} is not allowed`);
      },
    },
  };

  let rpcCalled = false;
  const mockClient = {
    listArtifacts: async () => {
      rpcCalled = true;
      throw new Error('the filtered RPC must not be reached');
    },
    statArtifact: async () => {
      rpcCalled = true;
      throw new Error('the filtered RPC must not be reached');
    },
    readArtifact: async () => {
      rpcCalled = true;
      throw new Error('the filtered RPC must not be reached');
    },
  } as unknown as SynapseRuntimeClient;

  await assert.rejects(() => fetchListArtifacts(mockClient, dummySession, '', 'F:/workspace'));
  await assert.rejects(() => fetchStatArtifact(mockClient, dummySession, 'target/a.exe', 'F:/workspace'));
  await assert.rejects(() => fetchReadArtifact(mockClient, dummySession, 'target/a.exe'));
  assert.equal(rpcCalled, false, 'the desktop tree must not silently read the ignore-filtered RPC');

  g.window = origWindow;
});

test('a native chunk whose fingerprint moved is refused', async () => {
  const g = globalThis as unknown as { window?: Record<string, unknown> };
  const origWindow = g.window;
  g.window = {
    __TAURI_INTERNALS__: {
      invoke: async () => ({
        path: 'Cargo.toml',
        offset: 0,
        data_base64: btoa('changed'),
        byte_length: 7,
        next_offset: 7,
        eof: true,
        size: 7,
        modified_at: '9s',
        revision: '7:9',
      }),
    },
  };

  // The caller stat'ed revision `500:1`; bytes from a different revision must
  // never be stitched into the view.
  await assert.rejects(() =>
    fetchReadArtifact(null, dummySession, 'Cargo.toml', 0, 4096, '500:1', 'F:/workspace'),
  );
  // With no expectation (the first read) the same chunk is accepted.
  const chunk = await fetchReadArtifact(null, dummySession, 'Cargo.toml', 0, 4096, null, 'F:/workspace');
  assert.equal(chunk.eof, true);

  g.window = origWindow;
});
