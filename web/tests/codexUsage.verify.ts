/**
 * Offline browser acceptance for the Codex usage entry.
 *
 * The entry cannot be exercised by the Node tests alone: its whole point is that
 * it may *not exist*, and that a confirmation stands between the user and a write
 * that really spends one of the account's reset credits.  So this fixture serves
 * the *real* `BottomBar` host, the real `codexUsageItem` and the real
 * `codexUsage` store/controller, and replaces only the runtime client with a mock
 * that records every call — no daemon, no OAuth, no token file, and no real credit
 * is ever consumed.
 *
 * It checks, in a real browser:
 *
 *  1. the availability source starts the controller while the entry is still
 *     hidden (nothing here mounts a Trigger first), so the entry appears once the
 *     mocked runtime confirms an enabled OAuth profile;
 *  2. the strip paints the window's *own* length (`7d`, not the TUI's `1d`);
 *  3. opening the panel reads the credit rows, and the 兑换 control only *raises*
 *     the confirmation — zero requests until 确认兑换, and 取消 sends nothing;
 *  4. confirming sends exactly one write with `confirmed: true` and a minted
 *     command id, and the spent credit leaves the list before the refresh lands;
 *  5. flipping the source to false removes the entry, its separator and its open
 *     panel in the same interaction, and sends no usage RPC while disabled;
 *  6. on the 360px band the entry is reachable through the existing 更多 menu,
 *     and losing its availability removes the row *and* the 更多 entry itself.
 *
 * Run from web/:
 *   $env:NODE_OPTIONS=''; $env:NODE_USE_ENV_PROXY=''; node tests/codexUsage.verify.ts
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { CdpClient, closeBrowser, evaluate, launchBrowser, openPage } from './helpers/cdp.ts';
import type { BrowserHandle } from './helpers/cdp.ts';
import { httpProbe } from './helpers/httpProbe.ts';

const webRoot = path.resolve(import.meta.dirname, '..');
const output = path.resolve(webRoot, '..', '.tmp', 'codex-usage');
fs.mkdirSync(output, { recursive: true });
process.env.TEMP = output;
process.env.TMP = output;

/**
 * The fixture's manifest: the real activity entry plus the real Codex entry.
 *
 * Nothing else is painted, so the left track is exactly the two controls the
 * separator assertions need, and the Codex entry is the only thing that can
 * overflow into 更多 on a phone window.
 */
const manifest = `
import { activityItem } from '/src/components/bottomBar/activityItem.tsx';
import { codexUsageItem } from '/src/components/bottomBar/codexUsageItem.tsx';

export const BOTTOM_BAR_ITEMS = [activityItem, codexUsageItem];
`;

/** The fixture page: real host, real store, mocked runtime client. */
const fixture = `
import React from 'react';
import {createRoot} from 'react-dom/client';
import {BottomBar} from '/src/components/BottomBar.tsx';
import {useConsoleStore} from '/src/stores/useConsoleStore.ts';
import {useCodexUsageStore} from '/src/stores/codexUsage.ts';
import '/src/index.css';

const SESSION = { project_id: 'sample', thread_id: 's0' };
const NOW = Math.floor(Date.now() / 1000);
const win = (used, minutes, resetIn) => ({
  used_percent: used, window_minutes: minutes, reset_at: NOW + resetIn,
});

// Every call the mocked runtime receives.  A real consume never happens.
const calls = { config: [], usage: [], credits: [], consume: [] };
let enabled = true;
// The credits the mocked runtime has seen consumed, and an optional delay on the
// rows read so the "spent credit is gone before the refresh lands" rule can be
// observed in a real browser.
const consumed = new Set();
// A gate the test can hold open, so the panel can be observed while the rows
// read is still in flight.
let creditsGate = null;
let releaseCredits = null;

const client = {
  async getRuntimeConfig() {
    calls.config.push(1);
    return { current_model: 'gpt-5-codex', codex_usage_enabled: enabled };
  },
  async getCodexUsage() {
    calls.usage.push(1);
    return {
      session: SESSION, model: 'gpt-5-codex',
      primary: win(18, 300, 5400), secondary: win(40, 10080, 200000),
      captured_at: NOW, available_reset_count: 2,
    };
  },
  async getCodexResetCredits() {
    calls.credits.push(1);
    if (creditsGate !== null) await creditsGate;
    const rows = [
      { id: 'credit-a', reset_type: 'weekly', status: 'available', granted_at: NOW,
        expires_at: null, title: 'Weekly reset', description: null },
      { id: 'credit-b', reset_type: 'weekly', status: 'redeemed', granted_at: NOW,
        expires_at: null, title: 'Already used', description: null },
    ].filter((credit) => !consumed.has(credit.id));
    return {
      session: SESSION, model: 'gpt-5-codex',
      available_count: rows.filter((credit) => credit.status === 'available').length,
      credits: rows,
    };
  },
  async consumeCodexResetCredit(params) {
    calls.consume.push(params);
    consumed.add(params.credit_id);
    return { session: SESSION, model: params.expected_model, command_id: params.command_id, outcome: 'reset' };
  },
};

useConsoleStore.setState({
  initClient: () => {}, pairingState: 'paired', connectionState: 'connected', client,
  currentSession: SESSION, sessionTitle: 'Codex 用量验收', modelName: 'gpt-5-codex',
  activeSubscriptionId: 'mock-watch',
  runtimeStatus: 'idle',
});

window.fixture = {
  calls,
  setEnabled: (value) => { enabled = value; },
  switchModel: (model) => useConsoleStore.setState({ modelName: model }),
  holdCredits: () => { creditsGate = new Promise((resolve) => { releaseCredits = resolve; }); },
  releaseCredits: () => {
    const release = releaseCredits;
    creditsGate = null;
    releaseCredits = null;
    if (release !== null) release();
  },
  codex: useCodexUsageStore,
};

createRoot(document.getElementById('root')).render(
  React.createElement(
    'div',
    {style:{height:'100vh',display:'flex',flexDirection:'column',justifyContent:'flex-end'}},
    React.createElement(BottomBar),
  ),
);
`;

const server = await createServer({
  configFile: false,
  envDir: false,
  root: webRoot,
  plugins: [
    react(),
    {
      name: 'codex-usage-fixture',
      // `vite:resolve` resolves an existing file before a normal plugin's
      // `resolveId` runs, so the manifest swap has to be a `pre` plugin.
      enforce: 'pre',
      configureServer(vite) {
        vite.middlewares.use('/codex-usage-fixture', async (_req, res) => {
          const html = await vite.transformIndexHtml(
            '/codex-usage-fixture',
            '<!doctype html><html><head><meta charset="utf-8"></head><body><div id="root"></div><script type="module" src="/fixture-entry.js"></script></body></html>',
          );
          res.setHeader('Content-Type', 'text/html');
          res.end(html);
        });
      },
      resolveId(id) {
        if (id === '/fixture-entry.js') return '\0codex-usage-entry';
        if (id.endsWith('/bottomBar/manifest.tsx')) return '\0codex-usage-manifest';
      },
      load(id) {
        if (id === '\0codex-usage-entry') return fixture;
        if (id === '\0codex-usage-manifest') return manifest;
      },
    },
  ],
  server: { host: '127.0.0.1', port: 0 },
});

let browser: BrowserHandle | undefined;
let client: CdpClient | undefined;
let checks = 0;
try {
  await server.listen();
  browser = await launchBrowser();
  const version = JSON.parse(
    (await httpProbe({ url: `http://127.0.0.1:${browser.port}/json/version` })).body,
  ) as { webSocketDebuggerUrl: string };
  client = await CdpClient.connect(version.webSocketDebuggerUrl);
  const page = await openPage(client, `${server.resolvedUrls.local[0]}codex-usage-fixture`);
  const run = (expression: string) => evaluate(client!, page, expression);
  const settle = (ms = 160) => new Promise((resolve) => setTimeout(resolve, ms));
  const check = async (label: string, expression: string) => {
    assert.equal(await run(expression), true, label);
    checks++;
    console.log(`PASS ${label}`);
  };
  const wait = async (expression: string) => {
    for (let i = 0; i < 150; i++) {
      if (await run(expression)) return;
      await settle();
    }
    throw new Error(`fixture not ready: ${expression}`);
  };
  const click = async (selector: string) => {
    await run(`document.querySelector(${JSON.stringify(selector)}).click(), true`);
    await settle();
  };
  const shot = async (name: string) => {
    const image = (await client!.send('Page.captureScreenshot', { format: 'png' }, page.sessionId)) as {
      data: string;
    };
    fs.writeFileSync(path.join(output, `${name}.png`), Buffer.from(image.data, 'base64'));
  };

  const ENTRY = `document.querySelector('[data-entry="codex-usage"]')`;
  const MORE = `document.querySelector('[data-entry="more"]')`;
  const DIALOG = `document.querySelector('[role="dialog"][aria-label="Codex 用量与重置额度"]')`;
  const ALERT = `document.querySelector('[role="alertdialog"]')`;
  const LEFT_ENTRIES = `document.querySelectorAll('[data-region="left"] [data-entry]').length`;
  const LEFT_SEPARATORS = `document.querySelectorAll('[data-region="left"] [data-separator]').length`;
  const CONSUMED = `window.fixture.calls.consume.length`;
  const USAGE_CALLS = `window.fixture.calls.usage.length`;
  const CREDIT = (id: string) => `document.querySelector('[data-credit="${id}"] button')`;

  await client.send(
    'Emulation.setDeviceMetricsOverride',
    { width: 1280, height: 800, deviceScaleFactor: 1, mobile: false },
    page.sessionId,
  );

  // --- 1. the hidden entry discovers the verdict through its source ----------
  // Nothing was painted before this: the strip's gate subscribed, which started
  // the controller, which asked the mocked runtime for the config gate.
  await wait(`!!${ENTRY}`);
  await check('the entry appears once the mocked runtime confirms OAuth', `!!${ENTRY}`);
  await check('the gate was asked exactly once', `window.fixture.calls.config.length === 1`);
  await check('and the usage windows were read once', `${USAGE_CALLS} === 1`);
  await check('no consume was sent while painting', `${CONSUMED} === 0`);
  await check(
    'the line labels each window from its own length (7d, never a hard-coded 1d)',
    `${ENTRY}.textContent.includes('5h 82%') && ${ENTRY}.textContent.includes('7d 60%')`,
  );
  await check('the line advertises the redeemable count', `${ENTRY}.textContent.includes('resets 2')`);
  await check('the wide strip paints both entries with one separator', `${LEFT_ENTRIES} === 2 && ${LEFT_SEPARATORS} === 1`);
  await shot('wide-strip');

  // --- 2. the panel reads the credit rows ------------------------------------
  await click('[data-entry="codex-usage"]');
  await wait(`!!${DIALOG}`);
  await check('the trigger opens the entry panel', `!!${DIALOG}`);
  await check('opening the panel reads the credit rows once', `window.fixture.calls.credits.length === 1`);
  await check(
    'the panel names both windows with their real lengths',
    `${DIALOG}.textContent.includes('5h 窗口') && ${DIALOG}.textContent.includes('7d 窗口')`,
  );
  await check('every credit row is painted', `document.querySelectorAll('[data-credit]').length === 2`);
  await check('a redeemed credit cannot be offered', `${CREDIT('credit-b')}.disabled === true`);
  await check('an available credit can', `${CREDIT('credit-a')}.disabled === false`);

  // --- 3. the confirmation stands between the user and the write -------------
  await click('[data-credit="credit-a"] button');
  await check('the redeem control raises a confirmation instead of sending', `!!${ALERT}`);
  await check(
    'the confirmation says the credit is really spent',
    `${ALERT}.textContent.includes('真实消耗 1 次账户级')`,
  );
  await check('and nothing was sent', `${CONSUMED} === 0`);
  await shot('confirm');

  await run(
    `Array.from(${ALERT}.querySelectorAll('button')).find((b) => b.textContent === '取消').click(), true`,
  );
  await settle();
  await check('cancelling drops the confirmation', `!${ALERT}`);
  await check('and still sent nothing', `${CONSUMED} === 0`);

  await click('[data-credit="credit-a"] button');
  // The refreshed rows are made slow on purpose: the spent credit must be gone
  // from the painted list *before* the refresh lands, so the awaiting rows can
  // never offer it a second time.
  await run(`window.fixture.holdCredits(), true`);
  await run(
    `Array.from(${ALERT}.querySelectorAll('button')).find((b) => b.textContent === '确认兑换').click(), true`,
  );
  await wait(`${CONSUMED} === 1`);
  await check('confirming sends exactly one write', `${CONSUMED} === 1`);
  await check(
    'the write carries `confirmed` and a minted command id',
    `window.fixture.calls.consume[0].confirmed === true && window.fixture.calls.consume[0].command_id.length > 0`,
  );
  await check(
    'the write names the credit and the model the user saw',
    `window.fixture.calls.consume[0].credit_id === 'credit-a' && window.fixture.calls.consume[0].expected_model === 'gpt-5-codex'`,
  );
  await check('the confirmation is gone once the write is out', `!${ALERT}`);
  await check('the spent credit left the list before the refresh', `!${CREDIT('credit-a')}`);
  await check(
    'the refresh is in flight, and the panel says so instead of offering the credit',
    `window.fixture.calls.credits.length === 2 && !${DIALOG}.textContent.includes('已兑换 1 次重置额度')`,
  );
  await run(`window.fixture.releaseCredits(), true`);
  await wait(`${DIALOG}.textContent.includes('已兑换 1 次重置额度')`);
  await check(
    'the refreshed rows are the runtime\'s own',
    `document.querySelectorAll('[data-credit]').length === 1 && ${DIALOG}.textContent.includes('0 次可用')`,
  );
  await check(
    'the panel reports the redeem and the refreshed rows',
    `${DIALOG}.textContent.includes('已兑换 1 次重置额度')`,
  );

  // --- 4. the source flipping to false takes the entry and its panel ---------
  await check('the panel is still open before the verdict changes', `!!${DIALOG}`);
  await check('the redeem refreshed the usage windows', `${USAGE_CALLS} === 2`);
  await run(
    `window.fixture.setEnabled(false), window.fixture.switchModel('other-model'), true`,
  );
  await wait(`!${ENTRY}`);
  await check('a disabled verdict removes the entry', `!${ENTRY}`);
  await check('its open panel goes with it', `!${DIALOG}`);
  await check('no separator is left orphaned', `${LEFT_ENTRIES} === 1 && ${LEFT_SEPARATORS} === 0`);
  await check('no usage RPC is sent while disabled', `${USAGE_CALLS} === 2`);
  await check('the gate was re-asked for the new context', `window.fixture.calls.config.length === 2`);
  await check('and no consume happened', `${CONSUMED} === 1`);
  await shot('hidden');

  // --- 5. and back on ---------------------------------------------------------
  await run(`window.fixture.setEnabled(true), window.fixture.switchModel('gpt-5-codex'), true`);
  await wait(`!!${ENTRY}`);
  await check('an enabled verdict paints the entry again', `!!${ENTRY}`);
  await check('the separator comes back with it', `${LEFT_ENTRIES} === 2 && ${LEFT_SEPARATORS} === 1`);
  await check('the usage windows are read for the new context', `${USAGE_CALLS} === 3`);

  // --- 6. the phone band reaches it through 更多 ------------------------------
  await client.send(
    'Emulation.setDeviceMetricsOverride',
    { width: 360, height: 740, deviceScaleFactor: 1, mobile: false },
    page.sessionId,
  );
  await settle(500);
  await wait(`!!${MORE}`);
  await check('the 360px band moves the entry into the 更多 menu', `!${ENTRY}`);
  await check('the 更多 entry is painted instead', `!!${MORE}`);
  await check('the strip keeps the one entry it can afford', `${LEFT_ENTRIES} === 1`);
  await click('[data-entry="more"]');
  await wait(`!!document.querySelector('[role="menu"]')`);
  await check(
    'the 更多 menu carries the Codex row',
    `Array.from(document.querySelectorAll('[role="menu"] button')).some((b) => b.textContent.includes('Codex 用量'))`,
  );
  await run(
    `Array.from(document.querySelectorAll('[role="menu"] button')).find((b) => b.textContent.includes('Codex 用量')).click(), true`,
  );
  await wait(`!!${DIALOG}`);
  await check('the menu row opens the same panel', `!!${DIALOG}`);
  await check('the menu is gone once the panel replaced it', `!document.querySelector('[role="menu"]')`);
  await shot('phone-panel');

  await run(`window.fixture.setEnabled(false), window.fixture.switchModel('other-model'), true`);
  await wait(`!${DIALOG}`);
  await check('a disabled verdict closes the panel opened from 更多', `!${DIALOG}`);
  await check('the 更多 row is gone', `!document.querySelector('[role="menu"]')`);
  await check('and with nothing left to overflow, 更多 itself is gone', `!${MORE}`);
  await check('the entry is still not painted', `!${ENTRY}`);

  console.log(`ALL ${checks} CHECKS PASSED; screenshots: .tmp/codex-usage/`);
} finally {
  client?.close();
  if (browser !== undefined) await closeBrowser(browser);
  await server.close();
}
