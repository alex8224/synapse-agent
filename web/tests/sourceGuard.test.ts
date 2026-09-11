/**
 * Source guard (C-12): the frontend must contain no credential path at all.
 *
 * The console holds no daemon token, never reads a token file, never puts a
 * credential in a URL, never injects an `Authorization` header and never
 * persists anything in browser storage.  These are static assertions over the
 * shipped sources (`src/**` + `vite.config.ts`), so a future edit that
 * reintroduces one of those paths fails here.
 */
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const webRoot = join(here, '..');

function sourceFiles(dir: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      found.push(...sourceFiles(full));
    } else if (entry.name.endsWith('.ts') || entry.name.endsWith('.tsx')) {
      found.push(full);
    }
  }
  return found;
}

const files = [...sourceFiles(join(webRoot, 'src')), join(webRoot, 'vite.config.ts')];

const forbidden: Array<{ name: string; pattern: RegExp }> = [
  { name: 'token file read', pattern: /test_token/i },
  { name: 'credential header injection', pattern: /authorization/i },
  { name: 'credential in a URL', pattern: /token=/i },
  { name: 'credential in the socket URL', pattern: /runtime-ws\?/i },
  { name: 'browser credential persistence', pattern: /localstorage|sessionstorage/i },
  { name: 'pairing code from the environment', pattern: /SYNAPSE_WEB_PAIRING_CODE/ },
];

test('C-12 frontend sources contain no credential path', () => {
  assert.ok(files.length > 10, 'the source guard must actually scan the frontend');
  for (const file of files) {
    const text = readFileSync(file, 'utf8');
    for (const rule of forbidden) {
      assert.equal(
        rule.pattern.test(text),
        false,
        `${file.replace(webRoot, '')} must not contain a ${rule.name} (${rule.pattern})`,
      );
    }
  }
});

test('C-02 the console only ever addresses the runtime socket through the fixed path', () => {
  const client = readFileSync(join(webRoot, 'src', 'client', 'SynapseRuntimeClient.ts'), 'utf8');
  // The socket factory takes the URL built by the store; nothing appends a
  // query string or a credential to it.
  assert.equal(/runtime-ws\?/.test(client), false);
  const bootstrap = readFileSync(join(webRoot, 'src', 'client', 'bootstrap.ts'), 'utf8');
  assert.equal(bootstrap.includes("`${scheme}//${location.host}/runtime-ws`"), true);
});
