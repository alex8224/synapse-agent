/**
 * Offline tests for the workspace-artifact wire decoders and display helpers.
 *
 * The decoders are the only place untrusted artifact payloads enter the UI, so
 * these tests pin the whitelist (extra keys are rejected), the type checks, the
 * verbatim `data_base64` pass-through, and the bounded text classification.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  MalformedArtifactError,
  artifactLanguage,
  basenameOf,
  decodeBase64Text,
  extensionOf,
  filterArtifactEntries,
  formatBytes,
  isTextArtifact,
  joinArtifactPath,
  parentArtifactPath,
  parseArtifactChunk,
  parseArtifactMetadata,
  parseArtifactPage,
} from '../src/client/artifacts.ts';

const SESSION = { project_id: 'p', thread_id: 't' };

function metadata(overrides: Record<string, unknown> = {}) {
  return {
    ref: { session: SESSION, path: 'src/app.py' },
    path: 'src/app.py',
    kind: 'file',
    size: 12,
    modified_at: '2026-01-01T00:00:00Z',
    media_type: 'text/x-python',
    revision: 'r1',
    ...overrides,
  };
}

test('parseArtifactMetadata copies only the whitelisted fields', () => {
  const entry = parseArtifactMetadata(metadata());
  assert.deepEqual(entry, {
    path: 'src/app.py',
    kind: 'file',
    size: 12,
    modified_at: '2026-01-01T00:00:00Z',
    media_type: 'text/x-python',
    revision: 'r1',
  });
  assert.deepEqual(Object.keys(entry).sort(), [
    'kind',
    'media_type',
    'modified_at',
    'path',
    'revision',
    'size',
  ]);
  // An unexpected key is a protocol violation, never silently dropped.
  assert.throws(
    () => parseArtifactMetadata(metadata({ secret: 'must not be copied' })),
    MalformedArtifactError,
  );
});

test('parseArtifactMetadata rejects a malformed projection', () => {
  const bad: Array<[string, unknown]> = [
    ['not an object', 'nope'],
    ['array', []],
    ['extra ref key', metadata({ ref: { session: SESSION, path: 'src/app.py', x: 1 } })],
    ['ref path mismatch', metadata({ ref: { session: SESSION, path: 'other.py' } })],
    ['unknown kind', metadata({ kind: 'symlink' })],
    ['negative size', metadata({ size: -1 })],
    ['size not int', metadata({ size: 1.5 })],
    ['modified_at not str', metadata({ modified_at: 7 })],
    ['media_type missing', metadata({ media_type: undefined })],
  ];
  for (const [label, payload] of bad) {
    assert.throws(() => parseArtifactMetadata(payload), MalformedArtifactError, label);
  }
  const nullOptional = parseArtifactMetadata(metadata({ modified_at: null, revision: null }));
  assert.equal(nullOptional.modified_at, null);
  assert.equal(nullOptional.revision, null);
});

test('parseArtifactPage decodes entries and the next cursor', () => {
  const page = parseArtifactPage({
    session: SESSION,
    path: 'src',
    entries: [metadata(), metadata({ path: 'src/lib', ref: { session: SESSION, path: 'src/lib' }, kind: 'directory' })],
    next_cursor: 'c1',
  });
  assert.equal(page.path, 'src');
  assert.equal(page.entries.length, 2);
  assert.equal(page.entries[1]?.kind, 'directory');
  assert.equal(page.nextCursor, 'c1');
  assert.equal(parseArtifactPage({ session: SESSION, path: 'src', entries: [], next_cursor: null }).nextCursor, null);
  assert.throws(() => parseArtifactPage({ session: SESSION, path: 'src', entries: {} , next_cursor: null }), MalformedArtifactError);
});

test('parseArtifactChunk passes data_base64 through verbatim', () => {
  const base64 = Buffer.from('hello world').toString('base64');
  const chunk = parseArtifactChunk({
    ref: { session: SESSION, path: 'src/app.py' },
    offset: 0,
    data_base64: base64,
    byte_length: 11,
    next_offset: 11,
    eof: true,
    metadata: metadata(),
  });
  assert.equal(chunk.data_base64, base64, 'the transport must not decode or re-encode');
  assert.equal(decodeBase64Text(chunk.data_base64), 'hello world');
  assert.equal(chunk.eof, true);
  assert.equal(chunk.nextOffset, 11);
  assert.throws(
    () => parseArtifactChunk({ ref: { session: SESSION, path: 'a' }, offset: 0, data_base64: 'AA==', byte_length: 1, next_offset: 1, eof: 'yes', metadata: metadata() }),
    MalformedArtifactError,
  );
  assert.throws(() => parseArtifactChunk(null), MalformedArtifactError);
});

test('decodeBase64Text reports malformed base64 instead of blanking out', () => {
  assert.throws(() => decodeBase64Text('not base64!!'), MalformedArtifactError);
  // Invalid UTF-8 bytes degrade to the replacement character rather than throwing.
  assert.equal(decodeBase64Text(Buffer.from([0xff, 0x41]).toString('base64')), '\uFFFDA');
});

test('formatBytes is bounded to one decimal', () => {
  assert.equal(formatBytes(0), '0 B');
  assert.equal(formatBytes(1023), '1023 B');
  assert.equal(formatBytes(1024), '1.0 KiB');
  assert.equal(formatBytes(1536), '1.5 KiB');
  assert.equal(formatBytes(1024 * 1024), '1.0 MiB');
  assert.equal(formatBytes(1024 * 1024 * 1024), '1.0 GiB');
  assert.equal(formatBytes(-5), '0 B');
});

test('isTextArtifact trusts the media type and only falls back to the extension', () => {
  assert.equal(isTextArtifact('text/plain', 'a.bin'), true);
  assert.equal(isTextArtifact('application/json', 'a.bin'), true);
  assert.equal(isTextArtifact('application/octet-stream', 'src/app.py'), true);
  assert.equal(isTextArtifact('application/octet-stream', 'assets/hero.png'), false);
  assert.equal(isTextArtifact('image/png', 'src/app.py'), false);
  // Dotfiles and extension-less text files get no MIME type of their own: the
  // fallback must recognise them, or the panel refuses them as binary.
  assert.equal(isTextArtifact('application/octet-stream', '.gitignore'), true);
  assert.equal(isTextArtifact('application/octet-stream', '.dockerignore'), true);
  assert.equal(isTextArtifact('application/octet-stream', 'LICENSE'), true);
  assert.equal(isTextArtifact('application/octet-stream', 'Dockerfile'), true);
  assert.equal(isTextArtifact('', 'Makefile'), true);
  // An unknown name stays refused rather than being decoded into mojibake.
  assert.equal(isTextArtifact('application/octet-stream', 'artifact.bin'), false);
  assert.equal(isTextArtifact('application/octet-stream', 'mystery'), false);
});

test('path helpers stay inside the workspace root', () => {
  assert.equal(joinArtifactPath('.', 'src'), 'src');
  assert.equal(joinArtifactPath('src', 'lib'), 'src/lib');
  assert.equal(parentArtifactPath('src/lib/a.py'), 'src/lib');
  assert.equal(parentArtifactPath('src'), '.');
  assert.equal(parentArtifactPath('.'), '.');
  assert.equal(extensionOf('src/app.py'), 'py');
  assert.equal(extensionOf('Makefile'), '');
  // A dotfile is its own extension: the server reports octet-stream for it, so
  // this token is the only thing the text fallback can match on.
  assert.equal(extensionOf('.gitignore'), 'gitignore');
  assert.equal(extensionOf('.dockerignore'), 'dockerignore');
  assert.equal(extensionOf('config/.env'), 'env');
  assert.equal(extensionOf('archive.tar.gz'), 'gz');
  assert.equal(basenameOf('src/lib/app.py'), 'app.py');
  assert.equal(basenameOf('LICENSE'), 'LICENSE');
});

test('artifactLanguage switches to diff highlighting in diff mode', () => {
  assert.equal(artifactLanguage('src/app.py', false), 'py');
  assert.equal(artifactLanguage('src/app.py', true), 'diff');
});

test('filterArtifactEntries narrows only the loaded entries', () => {
  const entries = [
    { path: 'src/a.ts', kind: 'file', size: 1, modified_at: null, media_type: 'text/plain', revision: 'r1' },
    { path: 'src/b.py', kind: 'file', size: 1, modified_at: null, media_type: 'text/plain', revision: 'r1' },
    { path: 'docs/c.md', kind: 'file', size: 1, modified_at: null, media_type: 'text/plain', revision: 'r1' },
  ];
  assert.deepEqual(filterArtifactEntries(entries, 'SRC/').map((e) => e.path), ['src/a.ts', 'src/b.py']);
  assert.deepEqual(filterArtifactEntries(entries, '  ').length, 3, 'a blank query keeps everything');
  assert.deepEqual(filterArtifactEntries(entries, 'zzz'), []);
});

test('filterArtifactEntries never mutates or reorders its input', () => {
  const entries = [
    { path: 'b', kind: 'file', size: 1, modified_at: null, media_type: 'text/plain', revision: null },
    { path: 'a', kind: 'file', size: 1, modified_at: null, media_type: 'text/plain', revision: null },
  ];
  const filtered = filterArtifactEntries(entries, 'a');
  assert.deepEqual(filtered.map((e) => e.path), ['a']);
  assert.deepEqual(entries.map((e) => e.path), ['b', 'a']);
});
