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
import {RpcCallError} from '/src/client/SynapseRuntimeClient.ts';
import '/src/index.css';

const listing = (p, entries) => ({path: p, parent: null, entries, truncated: false, roots: ['/']});
window.__mcpToggles = [];
store.setState({
  initClient: () => {}, pairingState: 'paired', connectionState: 'connected',
  workspacePath: '/sample/synapse', activeProjectId: 'sample',
  projects: [{project_id:'sample',workspace_name:'synapse',workspace_path:'/sample/synapse',git_branch:'main'}],
  expandedProjectIds: ['sample'], currentSession: {project_id:'sample',thread_id:'s0'},
  sessionTitle: '键盘导航验收', sessionsTotal: 2,
  sessions: [
    {thread_id:'s0',title:'键盘导航验收',updated_at:new Date().toISOString(),time_label:'今天'},
    // The row the delete section below removes: it is deliberately *not* the open
    // session, so accepting the delete does not also have to switch sessions.
    {thread_id:'s1',title:'待删除的会话',updated_at:new Date().toISOString(),time_label:'今天'}
  ],
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
    }),
    // Synthetic session delete: the confirmation's refusal path is a running
    // session, which the daemon reports as a conflict.
    deleteSession: async () => {
      if (window.__deleteRefuse) throw new RpcCallError('conflict', -32000, 'conflict');
      return {deleted: true, retained_history: true};
    },
    // Synthetic workspace tree: one directory and one file at the root, and a
    // single child directory inside any other path.  No host is contacted.
    listArtifacts: async (_session, path) => ({
      path: path ?? '.', nextCursor: null, truncated: false,
      entries: (path === null || path === '.' || path === '')
        ? [{ path: 'alpha', kind: 'directory', size: 0, media_type: 'inode/directory', revision: null, modified_at: null },
           { path: 'readme.md', kind: 'file', size: 12, media_type: 'text/markdown', revision: 'r1', modified_at: null }]
        : [{ path: path + '/inner', kind: 'directory', size: 0, media_type: 'inode/directory', revision: null, modified_at: null }],
    }),
    statArtifact: async (_session, path) =>
      ({ path, kind: 'file', size: 12, media_type: 'text/markdown', revision: 'r1', modified_at: null }),
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
  // The trigger is the labelled text button next to the composer's `+`; it carries
  // its name in `title`, not in an `aria-label`, so the old selector matched nothing
  // and the click threw on `null`.
  await click('[title^="添加项目"]');
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
  // The sidebar keeps both of its states in the DOM and marks the inactive one
  // `inert`, so the control a user can actually reach is the one outside an
  // `inert` subtree.  Selecting the first match would click the hidden rail
  // button, whose focus cannot be restored because `inert` blocks it.
  const settingsTrigger =
    `[...document.querySelectorAll('[aria-label="打开设置"]')].find((el) => !el.closest('[inert]'))`;
  await run(`${settingsTrigger}.focus()`); await settle();
  await run(`${settingsTrigger}.click()`); await settle();
  await wait(`!!document.querySelector('[role="dialog"][aria-label="设置"]')`);
  await check('the settings dialog takes the focus it was opened with',
    `document.querySelector('[role="dialog"][aria-label="设置"]').contains(document.activeElement)`);
  await click('[aria-label="关闭设置"]');
  await check('closing settings returns the focus to its trigger', `document.activeElement === ${settingsTrigger}`);
  await press('F6', 'F6', 117);
  await wait(`!!document.querySelector('[role="dialog"][aria-label="目标 (Goal)"]')`);
  await check('the goal dialog takes the focus and lands on its objective field',
    `document.activeElement === document.querySelector('#goal-objective')`);
  await press('Escape', 'Escape', 27);
  await check('Escape closes the goal dialog', `!document.querySelector('[role="dialog"][aria-label="目标 (Goal)"]')`);

  // --- the sidebar delete confirmation ---------------------------------------
  // It used to be an inline strip at the foot of the sidebar: the question sat a
  // screenful below the row whose trash icon armed it, named nothing about it, and
  // shared its corner (and its amber styling) with the result notice of the
  // previous delete.  It is a portalled modal dialog now, and these checks drive
  // the real thing: which session it names, where the focus lands, what Enter
  // does, and what a refusal leaves on screen.
  const deleteDialog = `document.querySelector('[role="dialog"][aria-labelledby="session-delete-title"]')`;
  const confirmButton =
    `[...document.querySelectorAll('[role="dialog"] button')].find((b) => b.textContent.trim() === '删除')`;
  const trash =
    `[...document.querySelectorAll('[aria-label^="删除会话记录"]')].find((el) => !el.closest('[inert]') && el.getAttribute('aria-label').includes('待删除的会话'))`;
  await run(`${trash}.focus()`); await settle();
  await run(`${trash}.click()`); await settle();
  await wait(`!!${deleteDialog}`);
  await check('the trash icon opens a confirmation of its own', `!!${deleteDialog}`);
  await check('and it is portalled out of the rail, not laid out inside the sidebar',
    `${deleteDialog}.closest('nav') === null`);
  await check('the confirmation names the session it deletes',
    `${deleteDialog}.textContent.includes('待删除的会话') && ${deleteDialog}.textContent.includes('s1')`);
  await check('the initial focus is the dialog close control, never the destructive button',
    `document.activeElement.getAttribute('aria-label') === '关闭删除确认'`);
  await check('the box offers one decision, not three controls saying no',
    `[...document.querySelectorAll('[role="dialog"] button')].length === 2 && !${deleteDialog}.textContent.includes('取消')`);
  // The whole point of the dialog: it appears over the row that opened it instead
  // of ~500px below it at the foot of the sidebar (where it could sit below the
  // fold).  It is centred in the window and fully on screen.
  await check('it is centred in the window and fully on screen',
    `(() => {
       const r = ${deleteDialog}.getBoundingClientRect();
       return Math.abs(r.left + r.width / 2 - window.innerWidth / 2) < 2
         && Math.abs(r.top + r.height / 2 - window.innerHeight / 2) < 2
         && r.top >= 0 && r.bottom <= window.innerHeight;
     })()`);
  await shot('session-delete-dialog');
  await press('Enter', 'Enter', 13, '\r');
  await check('Enter on a freshly opened confirmation dismisses it instead of deleting',
    `!${deleteDialog} && window.fixtureStore.getState().sessions.length === 2`);
  await check('the focus goes back to the trash icon that opened it', `document.activeElement === ${trash}`);

  // The refusal path: the server rejects a running session (`conflict`) and the
  // console never cancels the turn for it.  The dialog stays open with the reason
  // inline, and the row it names is still there.
  await run(`window.__deleteRefuse = true`);
  await run(`${trash}.click()`); await settle();
  await wait(`!!${deleteDialog}`);
  await run(`${confirmButton}.click()`); await settle();
  await check('a refused delete keeps the confirmation open',
    `!!${deleteDialog} && window.fixtureStore.getState().sessions.length === 2`);
  await check('and shows the reason inside it, not in the sidebar behind the scrim',
    `${deleteDialog}.textContent.includes('正在运行中')`);
  await check('the refusal is not also painted into the footer banner',
    `!document.querySelector('nav [role="alert"]')`);

  // The accepted path: the record goes, the confirmation closes, and the result is
  // a plain notice in the sidebar -- never a second question.
  await run(`window.__deleteRefuse = false`);
  await run(`${confirmButton}.click()`); await settle();
  await wait(`!${deleteDialog}`);
  await check('an accepted delete closes the confirmation', `!${deleteDialog}`);
  await check('the deleted row is gone from the tree',
    `window.fixtureStore.getState().sessions.length === 1 && !document.body.textContent.includes('待删除的会话')`);
  await check('the outcome is reported as a notice, not as a prompt',
    `[...document.querySelectorAll('nav [role="status"]')].some((el) => el.textContent.includes('已删除该会话的记录'))`);

  // --- phone band: the file panels become list -> detail ---------------------
  // A 320px rail plus a fixed 320px file list leaves no readable diff, so the
  // phone band stacks the two panes and swaps between them instead.
  await client.send('Emulation.setDeviceMetricsOverride',
    { width: 390, height: 844, deviceScaleFactor: 1, mobile: false }, page.sessionId);
  await client.send('Emulation.setTouchEmulationEnabled', { enabled: true }, page.sessionId);
  await settle();
  await run(`document.querySelector('[aria-label="查看 Git 变更"]').click()`);
  await wait(`!!document.querySelector('#git-file-list button')`);
  const visible = (selector: string) =>
    `document.querySelector(${JSON.stringify(selector)}).getClientRects().length > 0`;
  await check('phone: the file list is the first pane', visible('#git-file-list'));
  await check('phone: no diff pane before a file is picked',
    `document.querySelector('.git-responsive-body').dataset.detail === 'false' && !${visible('.git-responsive-body > div:last-child')}`);
  await run(`document.querySelector('#git-file-list button').click()`);
  await wait(`document.querySelector('.git-responsive-body').dataset.detail === 'true'`);
  await check('phone: picking a file swaps to the diff', visible('.git-responsive-body > div:last-child'));
  await check('phone: the list is hidden while the diff is shown',
    `!${visible('#git-file-list')}`);
  await check('phone: a back affordance is offered', visible('.list-detail-back'));
  await run(`document.querySelector('.list-detail-back').click()`); await settle();
  await check('phone: back returns to the file list',
    `${visible('#git-file-list')} && !${visible('.git-responsive-body > div:last-child')}`);

  // The workspace-file panel follows the same rule, and a *directory* must stay
  // in the list: drilling in re-lists it, so swapping panes there would show an
  // empty preview and hide the contents the reader just asked for.
  await press('Escape', 'Escape', 27);
  await run(`document.querySelector('[aria-label="工作区文件"]').click()`);
  await wait(`!!document.querySelector('.artifact-responsive-tree button')`);
  await check('phone: the file tree is the first pane', visible('.artifact-responsive-tree'));
  await check('phone: the preview pane waits for a file',
    `document.querySelector('.artifact-responsive-body').dataset.mobileDetail === 'false' && !${visible('.artifact-responsive-preview')}`);
  await run(`[...document.querySelectorAll('.artifact-responsive-tree button')].find((b) => b.textContent.includes('alpha')).click()`);
  await settle();
  await check('phone: opening a directory keeps the tree',
    `${visible('.artifact-responsive-tree')} && !${visible('.artifact-responsive-preview')}`);
  await check('phone: the tree lists the directory that was opened',
    `document.querySelector('.artifact-responsive-tree').textContent.includes('inner')`);

  console.log(`ALL ${checks} CHECKS PASSED; screenshots: .tmp/dialog-keyboard-nav/`);
} finally {
  client?.close();
  if (browser) await closeBrowser(browser);
  await server.close();
}
