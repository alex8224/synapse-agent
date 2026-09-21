/**
 * Source guard (C-12): the frontend must contain no credential path at all.
 *
 * The console holds no daemon token, never reads a token file, never puts a
 * credential in a URL, never injects an `Authorization` header and never
 * persists credentials in browser storage. Only transcriptCache may store
 * bounded view metadata (behavior/privacy tests in turnWork.test.ts), and only
 * the appearance store may keep the reader's theme choice; neither value is a
 * secret. These are static assertions over the
 * shipped sources (`src/**` + `vite.config.ts`), so a future edit that
 * reintroduces one of those paths fails here.
 *
 * The rules run over the TypeScript token stream rather than the raw file text:
 * comments and other trivia never trip the guard, while real code (a header
 * literal, a query string, an identifier) still does.
 */
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import ts from 'typescript';

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

// Generated artifacts are rendered from the Python contract registry, never
// hand-written: `contract.generated.ts` names protocol *capabilities* such as
// `AUTHORIZATION_CAPABILITIES`, which is not a credential path.  It is still
// scanned here: the rules match whole tokens, so protocol constant names pass
// while a real credential path in the generated file would not.
const files = [
  ...sourceFiles(join(webRoot, 'src')),
  join(webRoot, 'vite.config.ts'),
];

/**
 * Files allowed to name a storage API.
 *
 * C-12 forbids *credential* persistence, so the two non-secret UI stores below
 * are exempt from that single rule: `transcriptCache` keeps bounded per-session
 * view metadata, `appearance` keeps the reader's theme choice, and `usagePrefs`
 * keeps the usage dashboard's filter choices (project scope, range, heat metric).
 * Every other rule still applies to them, and no other file in `src/**` may touch
 * browser storage at all.
 */
const storageExempt = new Set([
  join(webRoot, 'src', 'stores', 'transcriptCache.ts'),
  join(webRoot, 'src', 'stores', 'appearance.ts'),
  join(webRoot, 'src', 'stores', 'usagePrefs.ts'),
]);

const forbidden: Array<{ name: string; pattern: RegExp }> = [
  { name: 'token file read', pattern: /test_token/i },
  // Whole word only: `AUTHORIZATION_CAPABILITIES` / `AuthorizationCapability`
  // are protocol names, not a credential header.
  { name: 'credential header injection', pattern: /\bauthorization\b/i },
  { name: 'credential in a URL', pattern: /token=/i },
  { name: 'credential in the socket URL', pattern: /runtime-ws\?/i },
  { name: 'browser credential persistence', pattern: /localstorage|sessionstorage/i },
  { name: 'pairing code from the environment', pattern: /SYNAPSE_WEB_PAIRING_CODE/ },
];

/** Token texts of a source file, with comments and other trivia removed. */
function tokenTexts(fileName: string, text: string): string[] {
  const scriptKind = fileName.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const source = ts.createSourceFile(fileName, text, ts.ScriptTarget.Latest, false, scriptKind);
  const tokens: string[] = [];
  const visit = (node: ts.Node): void => {
    // JSDoc blocks hang off the declaration they document, so they would be
    // read as tokens; they are comments and must be skipped like any trivia.
    if (ts.isJSDoc(node)) {
      return;
    }
    const children = node.getChildren(source);
    if (children.length === 0) {
      tokens.push(node.getText(source));
      return;
    }
    for (const child of children) {
      visit(child);
    }
  };
  visit(source);
  return tokens;
}

/** Names of the credential rules a source text violates. */
function credentialViolations(fileName: string, text: string): string[] {
  const names = new Set<string>();
  for (const token of tokenTexts(fileName, text)) {
    for (const rule of forbidden) {
      if (rule.pattern.test(token)) {
        names.add(rule.name);
      }
    }
  }
  return [...names];
}

test('C-12 frontend sources contain no credential path', () => {
  assert.ok(files.length > 10, 'the source guard must actually scan the frontend');
  for (const file of files) {
    const source = readFileSync(file, 'utf8');
    const viewCache = file === join(webRoot, 'src', 'stores', 'transcriptCache.ts');
    if (viewCache) assert.equal(/localStorage/.test(source), false);
    const violations = credentialViolations(file, source).filter(
      (rule) => !(storageExempt.has(file) && rule === 'browser credential persistence'),
    );
    assert.deepEqual(
      violations,
      [],
      `${file.replace(webRoot, '')} must not contain a credential path`,
    );
  }
});

test('C-12 comments and protocol constant names are not credential paths', () => {
  const allowed = [
    '// Protocol feature flags are not authorization capabilities: only the',
    '/* Authorization capabilities enforced by the ACL layer (18). */',
    'export const AUTHORIZATION_CAPABILITIES = ["session.read"] as const;',
    'export type AuthorizationCapability = (typeof AUTHORIZATION_CAPABILITIES)[number];',
  ].join('\n');
  assert.deepEqual(credentialViolations('allowed.ts', allowed), []);
});

test('C-12 real header literals and credential URLs still fail', () => {
  assert.deepEqual(credentialViolations('header.ts', 'const h = { Authorization: token };\n'), [
    'credential header injection',
  ]);
  assert.deepEqual(credentialViolations('upper.ts', "headers.set('AUTHORIZATION', token);\n"), [
    'credential header injection',
  ]);
  assert.deepEqual(credentialViolations('url.ts', 'const u = `/runtime-ws?token=${token}`;\n'), [
    'credential in a URL',
    'credential in the socket URL',
  ]);
  assert.deepEqual(credentialViolations('file.ts', "readFileSync('test_token.txt');\n"), [
    'token file read',
  ]);
});

test('C-12 the shipped HTML contains no credential path', () => {
  // The pre-paint theme script in `index.html` is not covered by the token walk
  // above (that one reads TypeScript), so the shipped HTML is checked textually
  // with the same rules minus the storage rule the script legitimately needs.
  const html = readFileSync(join(webRoot, 'index.html'), 'utf8');
  for (const rule of forbidden) {
    if (rule.name === 'browser credential persistence') continue;
    assert.equal(rule.pattern.test(html), false, `index.html must not contain ${rule.name}`);
  }
});

test('C-02 the console only ever addresses the runtime socket through the fixed path', () => {
  // The protocol client moved to the shared core; `src/client/SynapseRuntimeClient.ts`
  // is now a compatibility re-export, so the guard must read the implementation.
  const client = readFileSync(
    join(webRoot, 'src', 'runtime-client', 'SynapseRuntimeClient.ts'),
    'utf8',
  );
  // The socket factory takes the URL built by the store; nothing appends a
  // query string or a credential to it.
  assert.equal(/runtime-ws\?/.test(client), false);
  const bootstrap = readFileSync(join(webRoot, 'src', 'client', 'bootstrap.ts'), 'utf8');
  assert.equal(bootstrap.includes("`${scheme}//${location.host}/runtime-ws`"), true);
});

test('C-13 the console project list is a runtime RPC, not a host HTTP business route', () => {
  const store = readFileSync(join(webRoot, 'src', 'stores', 'useConsoleStore.ts'), 'utf8');
  // The store enumerates projects through the shared client...
  assert.equal(store.includes('listProjects('), true);
  // ...and no longer calls the deprecated host endpoint.
  assert.equal(/\bfetchProjects\b/.test(store), false);

  const client = readFileSync(
    join(webRoot, 'src', 'runtime-client', 'SynapseRuntimeClient.ts'),
    'utf8',
  );
  assert.equal(client.includes("'runtime.project.list'"), true);

  // The host route survives only as an explicitly deprecated compatibility shim.
  const bootstrap = readFileSync(join(webRoot, 'src', 'client', 'bootstrap.ts'), 'utf8');
  assert.equal(bootstrap.includes('@deprecated'), true);
});
