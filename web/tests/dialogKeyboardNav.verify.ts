/**
 * Offline browser acceptance for keyboard navigation in the console's dialogs
 * and popovers: synthetic state only, no host, daemon or credentials.
 *
 * Run from web/: node tests/dialogKeyboardNav.verify.ts
 *
 * Every box in the console is opened by a *trigger* -- a header chip, the
 * composer's `+`, F2 / F5 / F6, the sidebar's settings button -- and the trigger
 * keeps the focus.  A box that only listens for keydown on itself therefore never
 * sees a keystroke: its rows are real `<button>`s, but nothing ever delivers
 * ArrowDown / Enter to them, so picking a row stays mouse-only.  These checks
 * drive the real app with real key events and assert that focus enters the box,
 * that the arrows walk its rows, and that Enter / Space activates the focused row.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { CdpClient, launchBrowser, closeBrowser, openPage, evaluate } from './helpers/cdp.ts';
import { httpProbe } from './helpers/httpProbe.ts';

const webRoot = path.resolve(import.meta.dirname, '..');
const output = path.resolve(webRoot, '..', '.tmp', 'dialog-keyboard-nav');
fs.mkdirSync(output, { recursive: true });
// Keep even the browser's throwaway profile inside the workspace.
process.env.TEMP = output;
process.env.TMP = output;

const fixture = `
import React from 'react';
import {createRoot} from 'react-dom/client';
import {App} from '/src/App.tsx';
import {useConsoleStore as store} from '/src/stores/useConsoleStore.ts';
import '/src/index.css';

const listing = (p, entries) => ({path: p, parent: null, entries, truncated: false, roots: ['/']});
window.__mcpToggles = [];
store.setState({
  initClient: () => {}, pairingState: 'paired', connectionState: 'connected',
  workspacePath: '/sample/synapse', activeProjectId: 'sample',
  projects: [{project_id:'sample',workspace_name:'synapse',workspace_path:'/sample/synapse',git_branch:'main'}],
  expandedProjectIds: ['sample'], currentSession: {project_id:'sample',thread_id:'s0'},
  sessionTitle: '键盘导航验收', sessionsTotal: 1,
  sessions: [{thread_id:'s0',title:'键盘导航验收',updated_at:new Date().toISOString(),time_label:'今天'}],
  modelName: 'model-b', availableModels: ['model-a','model-b','model-c'],
  thinkingLevel: 'medium', thinkingLevels: ['low','medium','high'], canSetThinking: true,
  setModel: async (name) => { store.setState({modelName: name}); return true; },
  setThinkingLevel: async (level) => { store.setState({thinkingLevel: level}); return true; },
  submitPrompt: async () => {},
  gitBranch: 'main', gitDirty: true,
  client: {
    gitStatus: async () => ({
      branch: 'main', upstream: null, ahead: 0, behind: 0, dirty: true,
      files: [
        {path:'first.ts',indexStatus:' ',worktreeStatus:'M'},
        {path:'second.ts',indexStatus:'A',worktreeStatus:' '},
        {path:'third.ts',indexStatus:' ',worktreeStatus:'D'}
      ],
      truncated: false, insertions: 3, deletions: 2
    }),
    gitDiff: async (_session, p) => ({
      path: p, text: 'DIFF-FOR ' + p + '\\n', binary: false, truncated: false, empty: false
    })
  },
  listDirectories: async (p) => (p === null || p === '/sample')
    ? listing('/sample', [{name:'alpha',path:'/sample/alpha'},{name:'beta',path:'/sample/beta'}])
    : listing(p, [{name:'inner',path:p + '/inner'}]),
  addProject: async () => null,
  mcpServers: [{name:'demo-server',transport:'stdio',enabled:true,tool_prefix:null}],
  mcpRuntime: {'demo-server':{attached:true,includeTools:[],discovered:['read_file','write_file','search'],loaded:['read_file','write_file','search']}},
  mcpRuntimeKnown: true, mcpConnecting: false, mcpWarnings: [],
  toggleMcpServer: async (name) => { window.__mcpToggles.push(name); },
  refreshMcpRuntime: async () => {},
  saveMcpTools: async () => {}
});
window.fixtureStore = store;
createRoot(document.getElementById('root')).render(React.createElement(App));
`;

const server = await createServer({
  configFile: false, envDir: false, root: webRoot,
  plugins: [react(), {
    name: 'dialog-keyboard-nav-fixture',
    configureServer(server) {
      server.middlewares.use('/dialog-nav-fixture', async (_req, res) => {
        const html = await server.transformIndexHtml('/dialog-nav-fixture',
          '<!doctype html><html><head><meta charset="utf-8"></head><body><div id="root"></div><script type="module" src="/fixture-entry.js"></script></body></html>');
        res.setHeader('Content-Type', 'text/html'); res.end(html);
      });
    },
    resolveId(id) { if (id === '/fixture-entry.js') return '\0dialog-nav-fixture'; },
    load(id) { if (id === '\0dialog-nav-fixture') return fixture; },
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
  const page = await openPage(client, `${server.resolvedUrls.local[0]}dialog-nav-fixture`);
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
  const shot = async (name: string) => {
    const image = await client!.send('Page.captureScreenshot', {format:'png'}, page.sessionId) as {data:string};
    fs.writeFileSync(path.join(output, `${name}.png`), Buffer.from(image.data, 'base64'));
  };
  /**
   * One real key press, delivered through the browser's input pipeline.  A key
   * that produces text (Enter, Space) must be dispatched as `keyDown` with that
   * text: a `rawKeyDown` never runs the control's default activation, so Enter
   * would not press the focused button.
   */
  const press = async (key: string, code: string, vk: number, text?: string) => {
    const down = text === undefined
      ? {type:'rawKeyDown',key,code,windowsVirtualKeyCode:vk}
      : {type:'keyDown',key,code,text,unmodifiedText:text,windowsVirtualKeyCode:vk};
    await client!.send('Input.dispatchKeyEvent', down, page.sessionId);
    await client!.send('Input.dispatchKeyEvent', {type:'keyUp',key,code,windowsVirtualKeyCode:vk}, page.sessionId);
    await settle();
  };

  await wait(`!!document.querySelector('#console-composer')`);
  await client.send('Emulation.setDeviceMetricsOverride', {width:1440,height:900,deviceScaleFactor:1,mobile:false}, page.sessionId);
  await settle();
  // Nothing focused: the state the console is in once the operator clicks.
  await run(`document.activeElement.blur()`);
  await check('no dialog is open and nothing holds the focus', `document.activeElement === document.body && !document.querySelector('[role="dialog"]')`);

  // --- the model picker (F2) -------------------------------------------------
  await press('F2', 'F2', 113);
  await wait(`!!document.querySelector('#model-picker')`);
  await check('F2 opens the model picker', `!!document.querySelector('#model-picker')`);
  await check('opening the picker moves focus onto its filter field', `document.activeElement === document.querySelector('#model-filter')`);
  await shot('model-picker');
  await press('ArrowDown', 'ArrowDown', 40);
  await check('ArrowDown walks from the filter into the rows', `document.activeElement === document.querySelectorAll('#model-picker button')[0]`);
  await press('ArrowDown', 'ArrowDown', 40);
  await check('ArrowDown moves on to the next row', `document.activeElement === document.querySelectorAll('#model-picker button')[1]`);
  await press('ArrowUp', 'ArrowUp', 38);
  await check('ArrowUp moves back up', `document.activeElement === document.querySelectorAll('#model-picker button')[0]`);
  await press('Enter', 'Enter', 13, '\r');
  await check('Enter applies the focused row', `window.fixtureStore.getState().modelName === 'model-a'`);
  await check('the picker closes once a row is taken', `!document.querySelector('#model-picker')`);
  // F2 opened it with nothing focused, so there is no trigger to hand the focus
  // back to; the picker must not leave the focus on a row it just removed.
  await check('no focus is left behind on a removed row', `document.activeElement === document.body`);

  // --- the reasoning picker --------------------------------------------------
  await focusOn('[aria-controls="thinking-picker"]');
  await click('[aria-controls="thinking-picker"]');
  await wait(`!!document.querySelector('#thinking-picker')`);
  await check('the reasoning picker opens on the level in force',
    `document.activeElement === [...document.querySelectorAll('#thinking-picker button')].find(b => b.getAttribute('aria-pressed') === 'true')`);
  await press('ArrowUp', 'ArrowUp', 38);
  await check('ArrowUp reaches the level above', `document.activeElement.textContent.trim() === 'low'`);
  await press('Enter', 'Enter', 13, '\r');
  await check('Enter applies the focused level', `window.fixtureStore.getState().thinkingLevel === 'low'`);
  await check('the reasoning picker closes once a level is taken', `!document.querySelector('#thinking-picker')`);
  await check('focus goes back to the trigger that opened it', `document.activeElement === document.querySelector('[aria-controls="thinking-picker"]')`);

  // --- the MCP panel (F5) ----------------------------------------------------
  await press('F5', 'F5', 116);
  await wait(`!!document.querySelector('[aria-label="MCP 工具与服务器"]')`);
  await check('F5 opens the MCP panel on its first server row', `document.activeElement === document.querySelector('#mcp-server-list button')`);
  await press('Enter', 'Enter', 13, '\r');
  await check('Enter toggles the focused server (the row used to be a click-only div)',
    `window.__mcpToggles.length === 1 && window.__mcpToggles[0] === 'demo-server'`);
  await press('ArrowDown', 'ArrowDown', 40);
  await check('ArrowDown reaches the tool-list expander', `document.activeElement.getAttribute('title') === '展开工具列表'`);
  await press('Enter', 'Enter', 13, '\r');
  await wait(`!!document.querySelector('#mcp-tool-demo-server-read_file')`);
  await press('ArrowDown', 'ArrowDown', 40);
  await check('ArrowDown reaches the first tool checkbox', `document.activeElement === document.querySelector('#mcp-tool-demo-server-read_file')`);
  await press(' ', 'Space', 32, ' ');
  await check('Space toggles the focused tool', `document.querySelector('#mcp-tool-demo-server-read_file').checked === false`);
  await check('the tool count follows the keyboard toggle',
    `[...document.querySelectorAll('[aria-label="MCP 工具与服务器"] span')].some(s => s.textContent.trim() === '2/3 已选')`);
  await shot('mcp-panel');
  await press('Escape', 'Escape', 27);
  await check('Escape closes the panel', `!document.querySelector('[aria-label="MCP 工具与服务器"]')`);

  // --- the add-project dialog (the composer's `+`) ---------------------------
  await click('[aria-label="添加项目"]');
  await wait(`!!document.querySelector('#add-project-list button')`);
  await check('the add-project dialog opens on its first directory row', `document.activeElement === document.querySelector('#add-project-list button')`);
  await shot('add-project-dialog');
  // The arrows walk the box's controls in reading order, so a row is followed by
  // its own "选择" button before the next directory row.
  await press('ArrowDown', 'ArrowDown', 40);
  await check('ArrowDown walks to the focused row\'s own action', `document.activeElement.textContent.trim() === '选择'`);
  await press('ArrowDown', 'ArrowDown', 40);
  await check('ArrowDown reaches the next directory', `document.activeElement.textContent.includes('beta')`);
  await press('Enter', 'Enter', 13, '\r');
  await wait(`document.querySelector('#add-project-list button').textContent.includes('inner')`);
  await check('drilling in keeps the keyboard inside the dialog', `document.activeElement === document.querySelector('#add-project-list button')`);
  await press('Escape', 'Escape', 27);
  await check('the add-project dialog closes on Escape', `!document.querySelector('#add-project-list')`);

  // --- the git explorer (the branch chip) ------------------------------------
  await click('[aria-label="查看 Git 变更"]');
  await wait(`!!document.querySelector('#git-file-list button')`);
  await check('the git explorer opens on its first changed file', `document.activeElement === document.querySelector('#git-file-list button')`);
  await shot('git-explorer');
  await press('ArrowDown', 'ArrowDown', 40);
  await check('ArrowDown moves to the next changed file', `document.activeElement.textContent.includes('second.ts')`);
  await press('Enter', 'Enter', 13, '\r');
  await wait(`document.body.textContent.includes('DIFF-FOR second.ts')`);
  await check('Enter shows the focused file\'s diff', `document.body.textContent.includes('DIFF-FOR second.ts')`);
  await press('Escape', 'Escape', 27);
  await check('the git explorer closes on Escape', `!document.querySelector('#git-file-list')`);

  // --- the settings and goal dialogs ----------------------------------------
  await focusOn('[aria-label="打开设置"]');
  await click('[aria-label="打开设置"]');
  await wait(`!!document.querySelector('[role="dialog"][aria-label="设置"]')`);
  await check('the settings dialog takes the focus it was opened with',
    `document.querySelector('[role="dialog"][aria-label="设置"]').contains(document.activeElement)`);
  await click('[aria-label="关闭设置"]');
  await check('closing settings returns the focus to its trigger', `document.activeElement === document.querySelector('[aria-label="打开设置"]')`);
  await press('F6', 'F6', 117);
  await wait(`!!document.querySelector('[role="dialog"][aria-label="目标 (Goal)"]')`);
  await check('the goal dialog takes the focus and lands on its objective field',
    `document.activeElement === document.querySelector('#goal-objective')`);

  console.log(`ALL ${checks} CHECKS PASSED; screenshots: .tmp/dialog-keyboard-nav/`);
} finally {
  client?.close();
  if (browser) await closeBrowser(browser);
  await server.close();
}
