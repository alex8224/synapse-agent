/**
 * Real-browser acceptance for reading an untracked file in the Git Explorer.
 *
 * Synthetic state only: no host, no daemon, no repository.  The stub runtime answers
 * `runtime.git.diff` the way the real one does -- a new-file diff for an untracked path,
 * and nothing for the worktree side of a path that is only staged -- and the check is what
 * the reader sees: the untracked file's own content, painted as a diff, instead of an empty
 * pane telling them to go and look somewhere else.
 *
 * Run from web/: node --test tests/gitExplorerUntracked.verify.ts
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { CdpClient, launchBrowser, closeBrowser, openPage, evaluate } from './helpers/cdp.ts';
import { httpProbe } from './helpers/httpProbe.ts';

const webRoot = path.resolve(import.meta.dirname, '..');
const output = path.resolve(webRoot, '..', '.tmp', 'git-explorer-untracked');
fs.mkdirSync(output, { recursive: true });
process.env.TEMP = output;
process.env.TMP = output;

const fixture = `
import React from 'react';
import {createRoot} from 'react-dom/client';
import {App} from '/src/App.tsx';
import {useConsoleStore as store} from '/src/stores/useConsoleStore.ts';
import '/src/index.css';

window.__diffCalls = [];
const NEW_FILE_DIFF = '--- /dev/null\\n+++ b/tmp_hello.py\\n@@ -0,0 +1,2 @@\\n+print("hi")\\n+print("there")\\n';

store.setState({
  initClient: () => {}, pairingState: 'paired', connectionState: 'connected',
  client: {
    getState: () => 'connected',
    gitStatus: async () => ({
      branch: 'feature/x', upstream: null, ahead: 0, behind: 0, dirty: true,
      files: [
        {path: 'tmp_hello.py', indexStatus: '?', worktreeStatus: '?'},
        {path: 'staged_only.txt', indexStatus: 'A', worktreeStatus: ' '},
        {path: 'docs/long-line.md', indexStatus: ' ', worktreeStatus: 'M'},
      ],
      truncated: false, insertions: null, deletions: null,
    }),
    gitDiff: async (_session, filePath, staged) => {
      window.__diffCalls.push({path: filePath, staged});
      if (filePath === 'tmp_hello.py' && staged !== true) {
        return {path: filePath, text: NEW_FILE_DIFF, binary: false, truncated: false, empty: false};
      }
      if (filePath === 'docs/long-line.md') {
        const text = '--- a/docs/long-line.md\\n+++ b/docs/long-line.md\\n@@ -1,2 +1,2 @@\\n ' + 'context'.repeat(100) + '\\n-' + 'old'.repeat(300) + '\\n+' + 'new'.repeat(300) + '\\n';
        return {path: filePath, text, binary: false, truncated: false, empty: false};
      }
      return {path: filePath, text: '', binary: false, truncated: false, empty: true};
    },
  },
  workspacePath: '/sample/synapse', activeProjectId: 'p1',
  projects: [{project_id:'p1',workspace_name:'synapse',workspace_path:'/sample/synapse',git_branch:'feature/x'}],
  expandedProjectIds: ['p1'], currentSession: {project_id:'p1',thread_id:'s1'},
  sessionTitle: 'explorer', sessionsTotal: 1,
  sessions: [{thread_id:'s1',title:'explorer',updated_at:new Date().toISOString(),time_label:'今天'}],
  modelName: 'model-a', availableModels: ['model-a'], thinkingLevel: 'medium',
  thinkingLevels: ['medium'], canSetThinking: true,
  submitPrompt: async () => {}, gitBranch: 'feature/x', gitDirty: true, gitStatus: null,
  historyAvailable: true, historyHasMore: false, historyLoading: false,
  loadEarlierHistory: () => {}, runtimeStatus: 'idle', activeTurnId: null, activity: null,
  messages: [], gitExplorer: {path: 'tmp_hello.py'},
});

const pane = () => {
  const dialog = document.querySelector('[aria-label="Git Explorer"]');
  return (dialog?.textContent || '').replace(/\\s+/g, ' ').trim();
};
window.__probe = () => ({
  open: document.querySelector('[aria-label="Git Explorer"]') !== null,
  files: [...document.querySelectorAll('#git-file-list button')].map((b) => (b.textContent || '').trim()),
  pane: pane(),
  calls: window.__diffCalls,
});
window.__clickFile = (index) => {
  const rows = [...document.querySelectorAll('#git-file-list button')];
  const row = rows[index];
  if (row === undefined) return false;
  row.click();
  return true;
};
window.__toggleStaged = () => {
  const box = document.getElementById('git-explorer-staged');
  if (box === null) return false;
  box.click();
  return true;
};
createRoot(document.getElementById('root')).render(React.createElement(App));
`;

const server = await createServer({
  configFile: false, envDir: false, root: webRoot,
  plugins: [react(), {
    name: 'git-explorer-untracked-fixture',
    configureServer(server) {
      server.middlewares.use('/git-explorer-untracked', async (_req, res) => {
        const html = await server.transformIndexHtml('/git-explorer-untracked',
          '<!doctype html><html><head><meta charset="utf-8"></head><body><div id="root"></div><script type="module" src="/explorer-entry.js"></script></body></html>');
        res.setHeader('Content-Type', 'text/html'); res.end(html);
      });
    },
    resolveId(id) { if (id === '/explorer-entry.js') return '\0git-explorer-untracked-fixture'; },
    load(id) { if (id === '\0git-explorer-untracked-fixture') return fixture; },
  }],
  server: { host: '127.0.0.1', port: 0 },
});

let browser;
let client;
let checks = 0;
try {
  await server.listen();
  browser = await launchBrowser();
  const version = JSON.parse((await httpProbe({ url: `http://127.0.0.1:${browser.port}/json/version` })).body);
  client = await CdpClient.connect(version.webSocketDebuggerUrl);
  const page = await openPage(client, `${server.resolvedUrls.local[0]}git-explorer-untracked`);
  const run = (expression: string) => evaluate(client!, page, expression);
  const settle = () => new Promise((resolve) => setTimeout(resolve, 250));
  const check = async (label: string, expression: string) => {
    assert.equal(await run(expression), true, label); checks++; console.log(`PASS ${label}`);
  };
  await client.send('Emulation.setDeviceMetricsOverride', { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false }, page.sessionId);
  for (let i = 0; i < 100; i += 1) {
    if (await run(`window.__probe().open === true`)) break;
    await settle();
  }
  await settle();

  await check('the explorer lists the untracked file', `(() => {
    const p = window.__probe();
    return p.open === true && p.files.length === 3 && p.files[0].includes('tmp_hello.py');
  })()`);
  await check('the untracked file is read as a new-file diff', `(() => {
    const p = window.__probe();
    return p.calls.some((call) => call.path === 'tmp_hello.py' && call.staged === false);
  })()`);
  await check('its content is painted, not an empty pane', `(() => {
    const p = window.__probe();
    // The professional viewer paints the new-file diff: the path in its toolbar,
    // the hunk banner and the added lines.  The raw "+++ b/..." header is now
    // behind the 文件头 toggle instead of being drawn as a plain pre.
    return p.pane.includes('tmp_hello.py')
      && p.pane.includes('@@ -0,0 +1,2 @@')
      && p.pane.includes('+print("hi")')
      && p.pane.includes('+print("there")');
  })()`);
  await check('and it no longer sends the reader to another panel', `(() => {
    const p = window.__probe();
    return !p.pane.includes('未跟踪文件请用') && !p.pane.includes('没有差异');
  })()`);
  await check('the staged side of a staged-only file has no diff', `window.__clickFile(1)`);
  await settle();
  await check('and says so about the baseline it compared with', `(() => {
    const p = window.__probe();
    return p.calls.some((call) => call.path === 'staged_only.txt')
      && p.pane.includes('没有差异（该文件与所选基线一致');
  })()`);
  await check('the staged toggle asks git for the index instead', `window.__toggleStaged()`);
  await settle();
  await check('an untracked file has nothing staged, and the pane says so', `(() => {
    const p = window.__probe();
    return p.calls.some((call) => call.path === 'staged_only.txt' && call.staged === true)
      && p.pane.includes('没有差异（该文件与所选基线一致');
  })()`);
  // Use real pointer sequences: HTMLElement.click() bypasses the header drag
  // handler and cannot detect a click on an SVG being stolen by pointer capture.
  const pointerClick = async (selector: string) => {
    const point = await run(`(() => {
      const el = document.querySelector(${JSON.stringify(selector)});
      const r = el.getBoundingClientRect();
      return {x: r.x + r.width / 2, y: r.y + r.height / 2};
    })()`) as { x: number; y: number };
    await client!.send('Input.dispatchMouseEvent', { type: 'mousePressed', ...point, button: 'left', clickCount: 1 }, page.sessionId);
    await client!.send('Input.dispatchMouseEvent', { type: 'mouseReleased', ...point, button: 'left', clickCount: 1 }, page.sessionId);
    await settle();
  };
  await pointerClick('[aria-label="收起文件列表"] svg');
  await check('clicking the sidebar SVG collapses the file list', `getComputedStyle(document.getElementById('git-file-list')).display === 'none'`);
  await pointerClick('[aria-label="展开文件列表"] svg');
  await check('clicking the sidebar SVG restores the file list', `getComputedStyle(document.getElementById('git-file-list')).display !== 'none'`);
  await run('window.__clickFile(2)');
  await settle();
  const toolbarVisible = `(() => {
    const button = document.querySelector('[title="分栏视图 (Split / Side-by-side)"]');
    const r = button.getBoundingClientRect();
    const win = document.querySelector('.responsive-file-window').getBoundingClientRect();
    return r.left >= win.left && r.right <= win.right
      && button.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
  })()`;
  await check('long lines do not push Split outside the window', toolbarVisible);
  await check('unified long lines scroll inside the diff only', `(() => {
    const scroller = document.querySelector('[aria-label="Diff 视图模式"]').parentElement.parentElement.parentElement.querySelector('.overflow-auto');
    return scroller.scrollWidth > scroller.clientWidth
      && document.querySelector('.responsive-file-window').scrollWidth <= document.querySelector('.responsive-file-window').clientWidth + 1;
  })()`);
  await pointerClick('[title="分栏视图 (Split / Side-by-side)"]');
  await check('Split activates and long lines stay within each half', `(() => {
    const button = document.querySelector('[title="分栏视图 (Split / Side-by-side)"]');
    const cells = [...document.querySelectorAll('[aria-label="Git Explorer"] [class~="w-1/2"]')];
    return button.getAttribute('aria-pressed') === 'true' && cells.length > 0
      && cells.every(cell => cell.scrollWidth <= cell.clientWidth + 1);
  })()`);
  await run('window.__clickFile(0)');
  await run('window.__toggleStaged()');
  await settle();
  await check('Split remains available and selected after changing files', `document.querySelector('[title="分栏视图 (Split / Side-by-side)"]').getAttribute('aria-pressed') === 'true'`);
  await run('window.__clickFile(2)');
  await settle();
  await pointerClick('[aria-label="收起文件列表"] svg');
  await check('Split stays visible with the sidebar collapsed', toolbarVisible);
  await pointerClick('[aria-label="展开文件列表"] svg');
  await client.send('Emulation.setDeviceMetricsOverride', { width: 600, height: 900, deviceScaleFactor: 1, mobile: false }, page.sessionId);
  await settle();
  await run('window.__clickFile(2)');
  await settle();
  await check('Split stays visible on a narrow viewport', toolbarVisible);
  // Return to desktop before exercising the other title-bar SVG controls.
  await client.send('Emulation.setDeviceMetricsOverride', { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false }, page.sessionId);
  await settle();
  await pointerClick('[title="刷新"] svg');
  await check('refresh SVG re-reads the current diff', `window.__diffCalls.filter(c => c.path === 'docs/long-line.md').length >= 3`);
  await pointerClick('[aria-label="最大化窗口"] svg');
  await check('maximize SVG works', `document.querySelector('[aria-label="还原窗口"]') !== null`);
  await pointerClick('[aria-label="还原窗口"] svg');
  await check('restore SVG works', `document.querySelector('[aria-label="最大化窗口"]') !== null`);
  const image = (await client.send('Page.captureScreenshot', { format: 'png' }, page.sessionId)) as { data: string };
  fs.writeFileSync(path.join(output, 'untracked.png'), Buffer.from(image.data, 'base64'));
  await pointerClick('[aria-label="关闭"] svg');
  await check('close SVG works', `document.querySelector('[aria-label="Git Explorer"]') === null`);
  console.log(`ALL ${checks} CHECKS PASSED; screenshots: ${output}`);
} finally {
  client?.close();
  if (browser) await closeBrowser(browser);
  await server.close();
}
