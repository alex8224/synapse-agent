/**
 * Offline tests for the bounded bare-name locator.  Pure: a stub lister stands
 * in for the runtime client, so no socket is opened.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { locateArtifact, type ArtifactLister, type LocateLimits } from '../src/runtime-client/artifactLocate.ts';
import type { ArtifactEntry, ArtifactPageView } from '../src/runtime-client/artifacts.ts';
import type { SessionRef } from '../src/runtime-client/types.ts';

const session: SessionRef = { project_id: 'p', thread_id: 't' };

function entry(path: string, kind: 'file' | 'directory'): ArtifactEntry {
  return { path, kind, size: 0, modified_at: null, media_type: 'text/plain', revision: null };
}

function stub(tree: Record<string, ArtifactEntry[]>): { client: ArtifactLister; calls: string[] } {
  const calls: string[] = [];
  const client: ArtifactLister = {
    async listArtifacts(_session, path): Promise<ArtifactPageView> {
      calls.push(path);
      return { path, entries: tree[path] ?? [], nextCursor: null };
    },
  };
  return { client, calls };
}

test('locates a nested file by basename', async () => {
  const { client } = stub({
    '.': [entry('src', 'directory'), entry('README.md', 'file')],
    src: [entry('src/app.py', 'file')],
  });
  assert.equal(await locateArtifact(client, session, 'app.py'), 'src/app.py');
});

test('is case-insensitive on the basename', async () => {
  const { client } = stub({
    '.': [entry('src', 'directory')],
    src: [entry('src/app.py', 'file')],
  });
  assert.equal(await locateArtifact(client, session, 'APP.PY'), 'src/app.py');
});

test('prefers the shallowest match', async () => {
  const { client } = stub({
    '.': [entry('a.py', 'file'), entry('sub', 'directory')],
    sub: [entry('sub/a.py', 'file')],
  });
  assert.equal(await locateArtifact(client, session, 'a.py'), 'a.py');
});

test('returns null when nothing matches', async () => {
  const { client } = stub({ '.': [entry('a.txt', 'file')] });
  assert.equal(await locateArtifact(client, session, 'missing.py'), null);
});

test('stops at the directory cap', async () => {
  const limits: LocateLimits = { maxDirectories: 1, maxEntries: 100, maxDepth: 6, pageSize: 200 };
  const { client } = stub({
    '.': [entry('a', 'directory')],
    a: [entry('a/deep.py', 'file')],
  });
  assert.equal(await locateArtifact(client, session, 'deep.py', limits), null);
});

test('a directory that cannot be listed is skipped, not fatal', async () => {
  const client: ArtifactLister = {
    async listArtifacts(_session, path): Promise<ArtifactPageView> {
      if (path === '.') {
        return { path, entries: [entry('bad', 'directory'), entry('good', 'directory')], nextCursor: null };
      }
      if (path === 'bad') throw new Error('denied');
      return { path, entries: [entry('good/ok.py', 'file')], nextCursor: null };
    },
  };
  assert.equal(await locateArtifact(client, session, 'ok.py'), 'good/ok.py');
});
