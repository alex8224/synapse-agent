/**
 * Development proxy boundary tests (C-11 / phase-5 A7).
 *
 * The Vite config is imported for real (cache-busted per case) so the assertions
 * cover the loaded configuration object, not a copy of it.  They pin:
 *
 * - an unusable `SYNAPSE_WEB_CONSOLE_URL` fails config load instead of quietly
 *   proxying the console API somewhere else;
 * - only `/api` and `/runtime-ws` are proxied, and only to the console host;
 * - the forwarded request carries a rewritten `Origin` (so the host's strict
 *   same-origin check accepts it) and no credential of any kind;
 * - the dev server keeps its default loopback bind.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

interface ProxyEntry {
  target?: string;
  ws?: boolean;
  changeOrigin?: boolean;
  headers?: Record<string, string>;
}

interface LoadedConfig {
  server?: {
    host?: string;
    proxy?: Record<string, ProxyEntry>;
  };
}

async function loadConfig(consoleUrl: string | undefined, tag: string): Promise<LoadedConfig> {
  const previous = process.env.SYNAPSE_WEB_CONSOLE_URL;
  if (consoleUrl === undefined) delete process.env.SYNAPSE_WEB_CONSOLE_URL;
  else process.env.SYNAPSE_WEB_CONSOLE_URL = consoleUrl;
  try {
    const loaded = (await import(`../vite.config.ts?case=${tag}`)) as { default: LoadedConfig };
    return loaded.default;
  } finally {
    if (previous === undefined) delete process.env.SYNAPSE_WEB_CONSOLE_URL;
    else process.env.SYNAPSE_WEB_CONSOLE_URL = previous;
  }
}

test('C-11 a non-loopback SYNAPSE_WEB_CONSOLE_URL fails config load', async () => {
  const rejected = [
    'http://10.0.0.5:8080',
    'https://evil.example:8443',
    'http://0.0.0.0:8080',
    'http://127.0.0.2:8080',
    'http://localhost.:8080',
    'http://[::]:8080',
    'http://user:pass@127.0.0.1:8080',
    'http://127.0.0.1:8080/api',
    'ftp://127.0.0.1:8080',
    'ws://127.0.0.1:8080',
    'not a url',
  ];
  for (const [index, value] of rejected.entries()) {
    await assert.rejects(
      loadConfig(value, `reject-${index}`),
      (error: unknown) => error instanceof Error && error.message.includes('SYNAPSE_WEB_CONSOLE_URL'),
      `expected ${value} to be refused`,
    );
  }
});

test('C-11 the default proxy only targets the loopback console host', async () => {
  const config = await loadConfig(undefined, 'default');
  const proxy = config.server?.proxy ?? {};
  assert.deepEqual(Object.keys(proxy).sort(), ['/api', '/runtime-ws']);

  for (const [path, entry] of Object.entries(proxy)) {
    assert.equal(entry.target, 'http://127.0.0.1:8080', `${path} target`);
    assert.equal(entry.changeOrigin, true, `${path} rewrites the Host header`);
    assert.equal(entry.headers?.Origin, 'http://127.0.0.1:8080', `${path} rewrites Origin`);
  }
  // Only the websocket endpoint upgrades; `/api` stays a plain HTTP proxy.
  assert.equal(proxy['/runtime-ws'].ws, true);
  assert.equal(proxy['/api'].ws, undefined);

  // The dev server keeps its default (loopback) bind.
  assert.equal(config.server?.host, undefined);

  // No credential is ever injected into a forwarded request.
  const serialized = JSON.stringify(proxy);
  assert.equal(/authorization/i.test(serialized), false);
  assert.equal(/token/i.test(serialized), false);
  assert.equal(serialized.includes('Bearer'), false);
});

test('C-11 an explicit loopback target is normalized into an explicit origin', async () => {
  const config = await loadConfig('http://localhost:9000', 'loopback');
  const proxy = config.server?.proxy ?? {};
  assert.equal(proxy['/api'].target, 'http://localhost:9000');
  assert.equal(proxy['/api'].headers?.Origin, 'http://localhost:9000');
  assert.equal(proxy['/runtime-ws'].target, 'http://localhost:9000');
  assert.equal(proxy['/runtime-ws'].headers?.Origin, 'http://localhost:9000');
});
