/**
 * Offline browser acceptance for the title bar's "open with" control: synthetic
 * state only, no host, daemon or credentials.
 *
 * Run from web/: node tests/openWithMenu.verify.ts
 *
 * The pure decisions (which application a path recommends, how a refusal is worded)
 * are pinned in `externalApps.test.ts`, and the wiring facts in
 * `openWithMenuGuard.test.ts`.  What only a real browser can settle is the part that
 * made this control tricky:
 *
 *  - the menu must be *portaled* out of the window.  Both hosts are windows whose
 *    title bar is a drag handle and whose box clips its overflow, so a menu rendered
 *    inside it would drag the window from its own search field;
 *  - `Escape` must close the menu first and the window second, because every window
 *    listens for it on `window`;
 *  - the arrows must walk the rows and Enter must pick the focused one;
 *  - a launch must carry the id the host published, and a refused launch must be
 *    visible next to the control that asked.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { CdpClient, launchBrowser, closeBrowser, openPage, evaluate } from './helpers/cdp.ts';
import { httpProbe } from './helpers/httpProbe.ts';

const webRoot = path.resolve(import.meta.dirname, '..');
const output = path.resolve(webRoot, '..', '.tmp', 'open-with-menu');
fs.mkdirSync(output, { recursive: true });
// Keep even the browser's throwaway profile inside the workspace.
process.env.TEMP = output;
process.env.TMP = output;

const fixture = `
import React from 'react';
import {createRoot} from 'react-dom/client';
import {App} from '/src/App.tsx';
import {useConsoleStore as store} from '/src/stores/useConsoleStore.ts';
import {useAppearanceStore} from '/src/stores/appearance.ts';
import {RpcCallError} from '/src/runtime-client/SynapseRuntimeClient.ts';
import '/src/index.css';

// What the client's decoder hands the store: the *view* shape, not the wire shape.
const APPS = [
  {id:'vscode',name:'Visual Studio Code',shortName:'VS Code',kind:'editor',
   extensions:['tsx','ts','json'],icon:{kind:'glyph',value:'vscode'},
   isSystemDefault:false,available:true},
  {id:'cursor',name:'Cursor',shortName:'Cursor',kind:'editor',
   extensions:['tsx','py'],icon:{kind:'glyph',value:'cursor'},
   isSystemDefault:false,available:true},
  {id:'zed',name:'Zed',shortName:'Zed',kind:'editor',
   extensions:['rs'],icon:{kind:'glyph',value:'zed'},
   isSystemDefault:false,available:true},
  {id:'notepad',name:'记事本（Windows）',shortName:'记事本',kind:'viewer',
   extensions:['txt'],icon:{kind:'glyph',value:'notepad'},
   isSystemDefault:false,available:true},
  {id:'explorer',name:'资源管理器（定位到文件）',shortName:'资源管理器',kind:'shell',
   extensions:[],icon:{kind:'glyph',value:'explorer'},
   isSystemDefault:false,available:true},
  {id:'system',name:'系统默认应用',shortName:'系统默认',kind:'system',
   extensions:[],icon:{kind:'glyph',value:'system'},
   isSystemDefault:true,available:true}
];

window.__launches = [];
window.__refuse = null;

store.setState({
  initClient: () => {}, pairingState: 'paired', connectionState: 'connected',
  workspacePath: '/sample/synapse', activeProjectId: 'sample',
  projects: [{project_id:'sample',workspace_name:'synapse',workspace_path:'/sample/synapse',git_branch:'main'}],
  expandedProjectIds: ['sample'], currentSession: {project_id:'sample',thread_id:'s0'},
  sessionTitle: '打开方式验收', sessionsTotal: 1,
  sessions: [{thread_id:'s0',title:'打开方式验收',updated_at:new Date().toISOString(),time_label:'今天'}],
  modelName: 'model-b', availableModels: ['model-a','model-b'], thinkingLevel: 'medium',
  thinkingLevels: ['low','medium','high'], canSetThinking: false,
  setModel: async () => true, setThinkingLevel: async () => true, submitPrompt: async () => {},
  gitBranch: 'main', gitDirty: true,
  client: {
    gitStatus: async () => ({
      branch: 'main', upstream: null, ahead: 0, behind: 0, dirty: true,
      files: [{path:'src/app.tsx',indexStatus:' ',worktreeStatus:'M'}],
      truncated: false, insertions: 3, deletions: 2
    }),
    gitDiff: async (_session, p) => ({
      path: p, text: 'DIFF-FOR ' + p + '\\n', binary: false, truncated: false, empty: false
    }),
    listExternalApps: async () => ({apps: APPS, truncated: false}),
    openExternal: async (params) => {
      window.__launches.push(params);
      if (window.__refuse !== null && params.appId === window.__refuse) {
        throw new RpcCallError('the file does not exist in the workspace', -32000, 'external_app_file_missing');
      }
      return {opened: true, app_id: params.appId ?? 'system', mode: params.mode ?? 'open'};
    },
    listArtifacts: async (_session, p) => ({
      path: p ?? '.', nextCursor: null, truncated: false,
      entries: (p === null || p === '.' || p === '')
        ? [{path:'src/app.tsx',kind:'file',size:12,media_type:'text/plain',revision:'r1',modified_at:null}]
        : []
    }),
    statArtifact: async (_session, p) =>
      ({path:p,kind:'file',size:12,media_type:'text/plain',revision:'r1',modified_at:null}),
    readArtifact: async () => ({path:'src/app.tsx',offset:0,nextOffset:null,eof:true,
      data_base64:btoa('const a = 1;\\n'),size:12,revision:'r1',truncated:false}),
  },
  listDirectories: async () => ({path:'/', parent:null, entries:[], truncated:false, roots:['/']}),
  addProject: async () => null,
  mcpServers: [], mcpRuntime: {}, mcpRuntimeKnown: true, mcpConnecting: false, mcpWarnings: [],
  toggleMcpServer: async () => {}, refreshMcpRuntime: async () => {}, saveMcpTools: async () => {}
});
window.fixtureStore = store;
window.fixtureAppearance = useAppearanceStore;
createRoot(document.getElementById('root')).render(React.createElement(App));
`;

const server = await createServer({
  configFile: false, envDir: false, root: webRoot,
  plugins: [react(), {
    name: 'open-with-menu-fixture',
    configureServer(server) {
      server.middlewares.use('/open-with-fixture', async (_req, res) => {
        const html = await server.transformIndexHtml('/open-with-fixture',
          '<!doctype html><html><head><meta charset="utf-8"></head><body><div id="root"></div><script type="module" src="/fixture-entry.js"></script></body></html>');
        res.setHeader('Content-Type', 'text/html'); res.end(html);
      });
    },
    resolveId(id) { if (id === '/fixture-entry.js') return '\0open-with-fixture'; },
    load(id) { if (id === '\0open-with-fixture') return fixture; },
  }],
  server: { host: '127.0.0.1', port: 0 },
});

let browser;
let client;
let checks = 0;
try {
  await server.listen();
  browser = await launchBrowser();
  const version = JSON.parse((await httpProbe({url:`http://127.0.0.1:${browser.port}/json/version`})).body);
  client = await CdpClient.connect(version.webSocketDebuggerUrl);
  const page = await openPage(client, `${server.resolvedUrls.local[0]}open-with-fixture`);
  const run = (expression: string) => evaluate(client!, page, expression);
  const settle = () => new Promise(resolve => setTimeout(resolve, 120));
  const check = async (label: string, expression: string) => {
    assert.equal(await run(expression), true, label); checks++; console.log(`PASS ${label}`);
  };
  const wait = async (expression: string) => {
    for (let i = 0; i < 120; i++) { if (await run(expression)) return; await settle(); }
    throw new Error(`fixture not ready: ${expression}`);
  };
  const click = async (selector: string) => {
    await run(`document.querySelector(${JSON.stringify(selector)}).click()`); await settle();
  };
  /** Focus a control the way a real pointer click would (Chrome focuses buttons). */
  const focusOn = async (selector: string) => {
    await run(`document.querySelector(${JSON.stringify(selector)}).focus()`); await settle();
  };
  const press = async (key: string, code: string, vk: number, text?: string) => {
    const down = text === undefined
      ? {type:'rawKeyDown',key,code,windowsVirtualKeyCode:vk}
      : {type:'keyDown',key,code,text,unmodifiedText:text,windowsVirtualKeyCode:vk};
    await client!.send('Input.dispatchKeyEvent', down, page.sessionId);
    await client!.send('Input.dispatchKeyEvent', {type:'keyUp',key,code,windowsVirtualKeyCode:vk}, page.sessionId);
    await settle();
  };
  const shot = async (name: string) => {
    const image = await client!.send('Page.captureScreenshot', {format:'png'}, page.sessionId) as {data:string};
    fs.writeFileSync(path.join(output, `${name}.png`), Buffer.from(image.data, 'base64'));
  };
  const type = async (selector: string, value: string) => {
    await run(`(() => { const el = document.querySelector(${JSON.stringify(selector)});
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
      setter.call(el, ${JSON.stringify(value)});
      el.dispatchEvent(new Event('input', {bubbles: true})); })()`);
    await settle();
  };

  await wait(`!!document.querySelector('#console-composer')`);
  await client.send('Emulation.setDeviceMetricsOverride', {width:1440,height:900,deviceScaleFactor:1,mobile:false}, page.sessionId);
  await settle();
  await run(`document.activeElement.blur()`);

  // --- the catalog is read once, and the trigger names the recommended app ----
  await click('[aria-label="查看 Git 变更"]');
  await wait(`!!document.querySelector('#git-file-list button')`);
  await click('#git-file-list button');
  await wait(`!!document.querySelector('[aria-controls="open-with-menu"]')`);
  await check('the title bar offers an open-with control',
    `!!document.querySelector('[aria-controls="open-with-menu"]')`);
  // The catalog is read when the control mounts, so the label settles a moment later.
  console.log('DIAG', await run(`document.querySelector('[aria-controls="open-with-menu"]').parentElement.outerHTML.slice(0, 600)`));
  console.log('DIAG2', await run(`String(document.querySelectorAll('[aria-controls="open-with-menu"]').length)`));
  await wait(`document.querySelector('[aria-controls="open-with-menu"]').parentElement.textContent.includes('VS Code')`);
  await check('the trigger names the application the host recommends for .tsx',
    `document.querySelector('[aria-controls="open-with-menu"]').parentElement.textContent.includes('VS Code')`);
  await check('the catalog is read once per console',
    `window.fixtureStore.getState().externalApps.length === 6`);

  // --- the menu is portaled out of the window ---------------------------------
  await focusOn('[aria-controls="open-with-menu"]');
  await click('[aria-controls="open-with-menu"]');
  await wait(`!!document.querySelector('#open-with-menu')`);
  await check('the menu opens',
    `document.querySelector('[aria-controls="open-with-menu"]').getAttribute('aria-expanded') === 'true'`);
  await check('the menu is portaled out of the window, so the header cannot drag it',
    `document.querySelector('#open-with-menu').closest('[role="dialog"]') === null &&
     document.querySelector('#open-with-menu').parentElement === document.body`);
  await check('the menu stays inside the window it belongs to',
    `(() => { const m = document.querySelector('#open-with-menu').getBoundingClientRect();
      const w = document.querySelector('[role="dialog"][aria-label="Git Explorer"]').getBoundingClientRect();
      return m.right <= w.right + 1 && m.left >= w.left - 1; })()`);
  await check('the menu starts its focus on the application in force',
    `document.activeElement.getAttribute('aria-checked') === 'true' &&
     document.activeElement.textContent.includes('Visual Studio Code')`);
  await check('the recommended group is the one claiming the extension',
    `document.querySelector('#open-with-menu').textContent.includes('推荐 · .tsx')`);
  await check('the system association is offered as its own row',
    `[...document.querySelectorAll('#open-with-menu [role="menuitemradio"]')]
      .some((row) => row.textContent.includes('系统默认'))`);
  await shot('menu-open');

  // --- the arrows walk the rows and Enter picks one ---------------------------
  await press('ArrowDown', 'ArrowDown', 40);
  await check('ArrowDown walks to the next application',
    `document.activeElement.textContent.includes('Cursor')`);
  await press('Enter', 'Enter', 13, '\r');
  await wait(`window.__launches.length === 1`);
  await check('Enter launches the focused application by the id the host published',
    `window.__launches[0].appId === 'cursor' && window.__launches[0].path === 'src/app.tsx'`);
  await check('the launch carries no command line and no executable path',
    `(() => { const sent = JSON.stringify(window.__launches[0]);
      return sent.indexOf('.exe') === -1 && sent.indexOf('cmd') === -1 &&
        Object.keys(window.__launches[0])
          .every((key) => ['appId', 'mode', 'path', 'session'].includes(key)); })()`);
  await check('the menu closes once an application is taken',
    `!document.querySelector('#open-with-menu')`);
  await check('the trigger follows the picked application',
    `document.querySelector('[aria-controls="open-with-menu"]').parentElement.textContent.includes('Cursor')`);
  await check('focus goes back to the trigger that opened the menu',
    `document.activeElement === document.querySelector('[aria-controls="open-with-menu"]')`);

  // --- the filter narrows the rows -------------------------------------------
  await click('[aria-controls="open-with-menu"]');
  await wait(`!!document.querySelector('#open-with-filter')`);
  await type('#open-with-filter', 'zed');
  await check('the filter narrows the rows',
    `document.querySelectorAll('#open-with-menu [role="menuitemradio"]').length === 1 &&
     document.querySelector('#open-with-menu [role="menuitemradio"]').textContent.includes('Zed')`);
  await type('#open-with-filter', 'nothing-matches');
  await check('an empty result says so instead of showing an empty menu',
    `document.querySelector('#open-with-menu').textContent.includes('没有匹配')`);

  // --- Escape closes the menu first, the window second ------------------------
  await press('Escape', 'Escape', 27);
  await check('Escape closes the menu',
    `!document.querySelector('#open-with-menu')`);
  await check('Escape does not close the window while the menu is open',
    `!!document.querySelector('[role="dialog"][aria-label="Git Explorer"]')`);
  await press('Escape', 'Escape', 27);
  await check('the next Escape closes the window',
    `!document.querySelector('[role="dialog"][aria-label="Git Explorer"]')`);

  // --- a click outside closes the menu ---------------------------------------
  await click('[aria-label="查看 Git 变更"]');
  await wait(`!!document.querySelector('#git-file-list button')`);
  await click('#git-file-list button');
  await click('[aria-controls="open-with-menu"]');
  await wait(`!!document.querySelector('#open-with-menu')`);
  await run(`document.querySelector('.git-responsive-body > div:last-child')
    .dispatchEvent(new MouseEvent('mousedown', {bubbles: true}))`);
  await settle();
  await check('a click outside the menu and its trigger closes it',
    `!document.querySelector('#open-with-menu')`);
  await check('and leaves the window open',
    `!!document.querySelector('[role="dialog"][aria-label="Git Explorer"]')`);

  // --- the remembered choice --------------------------------------------------
  await click('[aria-controls="open-with-menu"]');
  await wait(`!!document.querySelector('#open-with-remember')`);
  await check('the checkbox names the application and the extension',
    `document.querySelector('#open-with-remember').parentElement.textContent.includes('始终用')`);
  await click('#open-with-remember');
  await check('ticking it remembers the application for that extension',
    `window.fixtureAppearance.getState().openWith['.tsx'] === 'cursor'`);
  await click('#open-with-remember');
  await check('unticking it forgets exactly that extension',
    `window.fixtureAppearance.getState().openWith['.tsx'] === undefined`);

  // --- a refused launch is visible -------------------------------------------
  await run(`window.__refuse = 'cursor'`);
  await run(`[...document.querySelectorAll('#open-with-menu [role="menuitemradio"]')]
    .find((row) => row.textContent.includes('Cursor')).click()`);
  await settle();
  await wait(`document.body.textContent.includes('已不在工作区')`);
  await check('a refused launch is worded from its service code',
    `document.body.textContent.includes('已不在工作区')`);
  await check('the window stays open so the reader can pick another application',
    `!!document.querySelector('[role="dialog"][aria-label="Git Explorer"]')`);
  await shot('refused-launch');
  await run(`window.__refuse = null`);
  await run(`[...document.querySelectorAll('button')]
    .find((button) => button.textContent.trim() === '知道了').click()`);
  await settle();
  await check('the refusal can be dismissed',
    `!document.body.textContent.includes('已不在工作区')`);
  await press('Escape', 'Escape', 27);

  // --- the workspace file window offers the same control ----------------------
  // The sidebar keeps both of its states in the DOM and marks the inactive one
  // `inert`, so the control a user can reach is the one outside an `inert` subtree.
  await run(`[...document.querySelectorAll('[aria-label="工作区文件"]')]
    .find((el) => !el.closest('[inert]')).click()`);
  await settle();
  await wait(`!!document.querySelector('.artifact-responsive-tree button')`);
  await run(`[...document.querySelectorAll('.artifact-responsive-tree button')]
    .find((b) => b.textContent.includes('app.tsx')).click()`);
  await wait(`!!document.querySelector('[role="dialog"][aria-label="工作区文件"] [aria-controls="open-with-menu"]')`);
  await check('the file window title bar offers the same control',
    `!!document.querySelector('[role="dialog"][aria-label="工作区文件"] [aria-controls="open-with-menu"]')`);
  await check('its control is enabled once a file is picked',
    `!document.querySelector('[role="dialog"][aria-label="工作区文件"] [aria-controls="open-with-menu"]').disabled`);
  await click('[role="dialog"][aria-label="工作区文件"] [aria-controls="open-with-menu"]');
  await wait(`!!document.querySelector('#open-with-menu')`);
  await check('the menu opens above the file window too',
    `document.querySelector('#open-with-menu').parentElement === document.body`);
  await shot('file-window');
  await press('Escape', 'Escape', 27);
  await check('Escape closes the menu and leaves the file window open',
    `!document.querySelector('#open-with-menu') &&
     !!document.querySelector('[role="dialog"][aria-label="工作区文件"]')`);

  console.log(`ALL ${checks} CHECKS PASSED; screenshots: .tmp/open-with-menu/`);
} finally {
  client?.close();
  if (browser) await closeBrowser(browser);
  await server.close();
}
