/**
 * Offline browser acceptance for the 更多 menu (the compact strip's overflow).
 *
 * The strip only paints the 更多 entry while something actually overflowed
 * (`resolveBottomBarLayout`), and no shipped entry declares the `more` policy
 * today, so the real console cannot reach the menu: the keyboard and focus
 * behaviour of `moreEntry.tsx` would otherwise be covered by nothing.  This
 * fixture serves the *real* `BottomBar` host (its one-open-overlay rule, its
 * `FloatingPanel` popover, the real entry modules) with a manifest that moves
 * the real help / MCP / goal entries into `more` the way a phone window would,
 * then drives it with real key events:
 *
 *  1. the 更多 trigger is painted on a narrow window,
 *  2. a keyboard press opens the menu and moves the focus onto its first row,
 *  3. ArrowDown / ArrowUp walk the rows and wrap at both ends,
 *  4. a row that opens a *modal* hands the focus back to the 更多 trigger when
 *     the modal closes — the row itself is unmounted with the menu, so a modal
 *     that remembered the row would strand the focus on `<body>`,
 *  5. a row that opens a *popover* does the same,
 *  6. Escape closes the menu itself and returns the focus to its trigger.
 *
 * The fixture never touches the shipped manifest: it only serves its own list
 * under the specifier the host imports.  No host, daemon or credentials are
 * involved.
 *
 * Run from web/:
 *   $env:NODE_OPTIONS=''; $env:NODE_USE_ENV_PROXY=''; node tests/bottomBarMore.verify.ts
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
const output = path.resolve(webRoot, '..', '.tmp', 'bottom-bar-more');
fs.mkdirSync(output, { recursive: true });
// Keep even the browser's throwaway profile inside the workspace.
process.env.TEMP = output;
process.env.TMP = output;

/**
 * The fixture's manifest: the real entries, moved into 更多.
 *
 * `help` (no trigger, modal), `mcp` (popover) and `goal` (modal) all declare an
 * overlay with content, so the compact policy alone puts them in the menu.  The
 * rows come out in `order` — help (10), mcp (20), goal (30) — which is what the
 * arrow checks below walk.
 */
const manifest = `
import { mcpItem } from '/src/components/bottomBar/mcpItem.tsx';
import { goalItem } from '/src/components/bottomBar/goalItem.tsx';
import { helpItem } from '/src/components/bottomBar/helpItem.tsx';

export const BOTTOM_BAR_ITEMS = [
  { ...mcpItem, compact: 'more' },
  { ...goalItem, compact: 'more' },
  { ...helpItem, compact: 'more' },
];
`;

const fixture = `
import React from 'react';
import {createRoot} from 'react-dom/client';
import {BottomBar} from '/src/components/BottomBar.tsx';
import {useConsoleStore as store} from '/src/stores/useConsoleStore.ts';
import '/src/index.css';

store.setState({
  initClient: () => {}, pairingState: 'paired', connectionState: 'connected',
  workspacePath: '/sample/synapse', activeProjectId: 'sample',
  projects: [{project_id:'sample',workspace_name:'synapse',workspace_path:'/sample/synapse',git_branch:'main'}],
  expandedProjectIds: ['sample'], currentSession: {project_id:'sample',thread_id:'s0'},
  sessionTitle: '更多菜单验收', sessionsTotal: 1,
  sessions: [{thread_id:'s0',title:'更多菜单验收',updated_at:new Date().toISOString(),time_label:'今天'}],
  goal: null,
  mcpServers: [{name:'demo-server',transport:'stdio',enabled:true,tool_prefix:null}],
  mcpRuntime: {'demo-server':{attached:true,includeTools:[],discovered:['read_file'],loaded:['read_file']}},
  mcpRuntimeKnown: true, mcpConnecting: false, mcpWarnings: [],
  toggleMcpServer: async () => {}, refreshMcpRuntime: async () => {}, saveMcpTools: async () => {},
});
window.fixtureStore = store;
// The strip lives at the bottom of the window, so its popover really hangs above
// it instead of off the top edge.
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
      name: 'bottom-bar-more-fixture',
      // `vite:resolve` resolves an existing file before a normal plugin's
      // `resolveId` ever runs, so the manifest swap has to be a `pre` plugin.
      enforce: 'pre',
      configureServer(vite) {
        vite.middlewares.use('/bottom-bar-more-fixture', async (_req, res) => {
          const html = await vite.transformIndexHtml(
            '/bottom-bar-more-fixture',
            '<!doctype html><html><head><meta charset="utf-8"></head><body><div id="root"></div><script type="module" src="/fixture-entry.js"></script></body></html>',
          );
          res.setHeader('Content-Type', 'text/html');
          res.end(html);
        });
      },
      resolveId(id) {
        if (id === '/fixture-entry.js') return '\0bottom-bar-more-entry';
        // The host imports `./bottomBar/manifest.tsx`; the fixture answers with
        // its own list under that specifier, so the shipped manifest is untouched.
        if (id.endsWith('/bottomBar/manifest.tsx')) return '\0bottom-bar-more-manifest';
      },
      load(id) {
        if (id === '\0bottom-bar-more-entry') return fixture;
        if (id === '\0bottom-bar-more-manifest') return manifest;
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
  const page = await openPage(client, `${server.resolvedUrls.local[0]}bottom-bar-more-fixture`);
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
  /**
   * One real key press through the browser's input pipeline.  A key that produces
   * text (Enter) must be dispatched as `keyDown` with that text: a `rawKeyDown`
   * never runs a control's default activation, so Enter would not press the
   * focused button.
   */
  const press = async (key: string, code: string, vk: number, text?: string) => {
    const down =
      text === undefined
        ? { type: 'rawKeyDown', key, code, windowsVirtualKeyCode: vk }
        : { type: 'keyDown', key, code, text, unmodifiedText: text, windowsVirtualKeyCode: vk };
    await client!.send('Input.dispatchKeyEvent', down, page.sessionId);
    await client!.send(
      'Input.dispatchKeyEvent',
      { type: 'keyUp', key, code, windowsVirtualKeyCode: vk },
      page.sessionId,
    );
    await settle();
  };
  const shot = async (name: string) => {
    const image = (await client!.send('Page.captureScreenshot', { format: 'png' }, page.sessionId)) as {
      data: string;
    };
    fs.writeFileSync(path.join(output, `${name}.png`), Buffer.from(image.data, 'base64'));
  };

  const MORE = `document.querySelector('[data-entry="more"]')`;
  const MENU = `document.querySelector('[role="menu"]')`;
  const ROW = (index: number) => `document.querySelectorAll('[role="menu"] button')[${index}]`;
  const FOCUS_IS_MORE = `document.activeElement === ${MORE}`;
  const openMenu = async () => {
    await run(`${MORE}.focus()`);
    await press('Enter', 'Enter', 13, '\r');
    await wait(`!!${MENU}`);
  };

  // --- 1. the compact band paints the 更多 trigger ---------------------------
  await client.send(
    'Emulation.setDeviceMetricsOverride',
    { width: 360, height: 740, deviceScaleFactor: 1, mobile: false },
    page.sessionId,
  );
  await settle(500);
  await wait(`!!${MORE}`);
  console.log('--- 360x740 ---');
  await check('the 更多 trigger is painted on a narrow window', `!!${MORE}`);
  await check(
    'the overflowed entries are not painted in the strip',
    `document.querySelectorAll('footer [data-entry]').length === 1`,
  );
  await check('no menu is open yet', `!${MENU}`);

  // --- 2. the keyboard opens the menu and lands on its first row -------------
  await openMenu();
  await check('a keyboard press opens the 更多 menu', `!!${MENU}`);
  await check('opening the menu moves the focus onto its first row', `document.activeElement === ${ROW(0)}`);
  await check(
    'the first row is the entry the layout ordered first',
    `${ROW(0)}.textContent.includes('快捷键帮助')`,
  );
  await check('the row advertises the key it answers to', `${ROW(0)}.textContent.includes('F1')`);
  await shot('more-menu');

  // --- 3. the arrows walk the rows and wrap ---------------------------------
  await press('ArrowDown', 'ArrowDown', 40);
  await check('ArrowDown reaches the second row', `document.activeElement === ${ROW(1)}`);
  await check('the second row is the MCP entry', `${ROW(1)}.textContent.includes('MCP 服务器')`);
  await press('ArrowDown', 'ArrowDown', 40);
  await check('ArrowDown reaches the third row', `document.activeElement === ${ROW(2)}`);
  await check('the third row is the goal entry', `${ROW(2)}.textContent.includes('目标 (Goal)')`);
  await press('ArrowDown', 'ArrowDown', 40);
  await check('ArrowDown wraps at the end', `document.activeElement === ${ROW(0)}`);
  await press('ArrowUp', 'ArrowUp', 38);
  await check('ArrowUp wraps at the start', `document.activeElement === ${ROW(2)}`);

  // --- 4. a modal row hands the focus back to the 更多 trigger ---------------
  await press('Enter', 'Enter', 13, '\r');
  await wait(`!!document.querySelector('[role="dialog"][aria-label="目标 (Goal)"]')`);
  await check(
    'Enter opens the modal the focused row advertises',
    `!!document.querySelector('[role="dialog"][aria-label="目标 (Goal)"]')`,
  );
  await check(
    'the modal takes the focus',
    `document.activeElement === document.querySelector('#goal-objective')`,
  );
  await check('the menu is gone once the modal replaced it', `!${MENU}`);
  await press('Escape', 'Escape', 27);
  await check(
    'Escape closes the modal',
    `!document.querySelector('[role="dialog"][aria-label="目标 (Goal)"]')`,
  );
  await check('the modal returns the focus to the 更多 trigger, not a removed row', FOCUS_IS_MORE);

  // --- 5. a popover row does the same ---------------------------------------
  await openMenu();
  await check('the menu reopens on its first row', `document.activeElement === ${ROW(0)}`);
  await press('ArrowDown', 'ArrowDown', 40);
  await check('ArrowDown lands on the MCP row again', `document.activeElement === ${ROW(1)}`);
  await press('Enter', 'Enter', 13, '\r');
  await wait(`!!document.querySelector('[role="dialog"][aria-label="MCP 工具与服务器"]')`);
  await check(
    'Enter opens the popover the focused row advertises',
    `!!document.querySelector('[role="dialog"][aria-label="MCP 工具与服务器"]')`,
  );
  await check('the popover takes the focus', `document.activeElement === document.querySelector('#mcp-server-list button')`);
  await press('Escape', 'Escape', 27);
  await check(
    'Escape closes the popover',
    `!document.querySelector('[role="dialog"][aria-label="MCP 工具与服务器"]')`,
  );
  await check('the popover returns the focus to the 更多 trigger', FOCUS_IS_MORE);

  // --- 6. Escape closes the menu itself and hands the focus back ------------
  await openMenu();
  await check('the menu opens a third time', `document.activeElement === ${ROW(0)}`);
  await press('Escape', 'Escape', 27);
  await check('Escape closes the menu', `!${MENU}`);
  await check('the menu hands the focus back to its trigger', FOCUS_IS_MORE);

  console.log(`ALL ${checks} CHECKS PASSED; screenshots: .tmp/bottom-bar-more/`);
} finally {
  client?.close();
  if (browser !== undefined) await closeBrowser(browser);
  await server.close();
}
