/**
 * The artifact surface is the one place that decides *where* a file view reads
 * from: the Tauri native bridge on the desktop build, the read-only
 * `runtime.artifacts.*` RPC in the browser console.
 *
 * Three properties of that decision are contracts rather than plumbing, and none
 * of them is reachable through the UI tests:
 *
 * - paging survives the indirection, so the panel's "load more" still sends its
 *   cursor (dropping it would re-append page one forever);
 * - readiness is observable, so a view that mounts while the socket is still
 *   connecting is not left holding a permanently empty surface;
 * - the desktop build needs no runtime connection at all, and never reaches for
 *   the ignore-filtered RPC behind the native surface.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createArtifactSurface } from '../src/client/artifactSurface.ts';
import type { SynapseRuntimeClient } from '../src/client/SynapseRuntimeClient.ts';
import type { SessionRef } from '../src/runtime-client/contract.generated.ts';

const SESSION: SessionRef = { project_id: 'proj_1', thread_id: 'th_1' };
const WORKSPACE = 'F:/project/synapse';

interface ListCall {
  session: SessionRef;
  path: string;
  cursor: string | null;
  limit: number;
}

/** A client that records what the surface asked for. */
function recordingClient(): {
  client: SynapseRuntimeClient;
  lists: ListCall[];
  reads: Array<{ path: string; offset: number; limit: number; revision: string | null }>;
  stats: string[];
} {
  const lists: ListCall[] = [];
  const reads: Array<{ path: string; offset: number; limit: number; revision: string | null }> = [];
  const stats: string[] = [];
  const client = {
    getState: () => 'connected',
    listArtifacts: async (
      session: SessionRef,
      path: string,
      cursor: string | null,
      limit: number,
    ) => {
      lists.push({ session, path, cursor, limit });
      return { path, entries: [], nextCursor: 'next-page' };
    },
    statArtifact: async (_session: SessionRef, path: string) => {
      stats.push(path);
      return {
        path,
        kind: 'file' as const,
        size: 7,
        modified_at: null,
        media_type: 'text/plain',
        revision: 'r1',
      };
    },
    readArtifact: async (
      _session: SessionRef,
      path: string,
      offset: number,
      limit: number,
      revision: string | null,
    ) => {
      reads.push({ path, offset, limit, revision });
      return {
        path,
        offset,
        data_base64: '',
        byteLength: 0,
        nextOffset: offset,
        eof: true,
        metadata: {
          path,
          kind: 'file' as const,
          size: 7,
          modified_at: null,
          media_type: 'text/plain',
          revision,
        },
      };
    },
  } as unknown as SynapseRuntimeClient;
  return { client, lists, reads, stats };
}

/** Run `body` with the host advertising a Tauri webview, then restore it. */
async function withTauri(
  invoke: (cmd: string, args?: unknown) => Promise<unknown>,
  body: () => Promise<void>,
): Promise<void> {
  const g = globalThis as unknown as { window?: Record<string, unknown> };
  const original = g.window;
  g.window = { __TAURI_INTERNALS__: { invoke } };
  try {
    await body();
  } finally {
    g.window = original;
  }
}

/** Run `body` with no Tauri internals, i.e. a plain browser console. */
async function withoutTauri(body: () => Promise<void>): Promise<void> {
  const g = globalThis as unknown as { window?: Record<string, unknown> };
  const original = g.window;
  g.window = {};
  try {
    await body();
  } finally {
    g.window = original;
  }
}

test('the browser surface forwards the paging cursor and page size', async () => {
  await withoutTauri(async () => {
    const { client, lists } = recordingClient();
    const surface = createArtifactSurface(client, WORKSPACE, true);
    assert.ok(surface, 'a connected client is a usable surface');

    const page = await surface.listArtifacts(SESSION, 'src', 'cursor-1', 50);
    assert.deepEqual(lists, [
      { session: SESSION, path: 'src', cursor: 'cursor-1', limit: 50 },
    ]);
    assert.equal(page.nextCursor, 'next-page');

    // The root is the one path the RPC spells `.`.
    await surface.listArtifacts(SESSION, '.', null, 200);
    assert.equal(lists[1].path, '.');
    assert.equal(lists[1].cursor, null);
  });
});

test('the surface is null until the RPC handshake finished, and then appears', async () => {
  await withoutTauri(async () => {
    const { client } = recordingClient();
    assert.equal(createArtifactSurface(client, WORKSPACE, false), null);
    assert.equal(createArtifactSurface(null, WORKSPACE, true), null);
    assert.equal(createArtifactSurface(null, WORKSPACE, false), null);
    assert.ok(createArtifactSurface(client, WORKSPACE, true));
  });
});

test('the desktop surface needs no runtime connection and never reads the RPC', async () => {
  await withTauri(
    async (cmd, args) => {
      if (cmd !== 'tauri_list_artifacts') throw new Error(`unexpected ${cmd}`);
      assert.deepEqual(args, { workspace: WORKSPACE, subpath: 'rust/synapse-gui/target' });
      return {
        entries: [
          { path: 'rust/synapse-gui/target/debug', name: 'debug', is_dir: true, size: 0 },
        ],
        path: 'rust/synapse-gui/target',
        total: 1,
      };
    },
    async () => {
      const { client, lists } = recordingClient();
      // The socket is still connecting: the native bridge does not care.
      const surface = createArtifactSurface(client, WORKSPACE, false);
      assert.ok(surface, 'the native surface needs no runtime connection');

      const page = await surface.listArtifacts(SESSION, 'rust/synapse-gui/target', null, 200);
      assert.deepEqual(
        page.entries.map((entry) => [entry.path, entry.kind]),
        [['rust/synapse-gui/target/debug', 'directory']],
      );
      assert.equal(page.nextCursor, null, 'one directory is one native page');
      assert.deepEqual(lists, [], 'the ignore-filtered RPC must not be consulted');
    },
  );
});
