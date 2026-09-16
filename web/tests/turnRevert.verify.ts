/**
 * Real-browser acceptance for undoing one file of one turn.
 *
 * Synthetic state only: no host, no daemon, no repository, no real write.  It mounts the
 * console with a turn that changed four files (one of them binary, one already reverted by
 * an earlier session) and a stub runtime that restores one file and refuses the other, then
 * checks what the reader sees: the undo offer, the arm-then-confirm, the card turning into
 * 已撤销 with its counts gone, and the refusal being reported instead of swallowed.
 *
 * Run from web/: node --test tests/turnRevert.verify.ts
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { CdpClient, launchBrowser, closeBrowser, openPage, evaluate } from './helpers/cdp.ts';
import { httpProbe } from './helpers/httpProbe.ts';

const webRoot = path.resolve(import.meta.dirname, '..');
const output = path.resolve(webRoot, '..', '.tmp', 'turn-revert');
fs.mkdirSync(output, { recursive: true });
process.env.TEMP = output;
process.env.TMP = output;

const fixture = `
import React from 'react';
import {createRoot} from 'react-dom/client';
import {App} from '/src/App.tsx';
import {useConsoleStore as store} from '/src/stores/useConsoleStore.ts';
import {RpcCallError} from '/src/runtime-client/SynapseRuntimeClient.ts';
import '/src/index.css';

const changes = [
  {path:'src/synapse/runtime/turn_reverts.py', status:'added', insertions:120, deletions:0, binary:false, reverted:false},
  {path:'web/src/stores/historyMapper.ts', status:'modified', insertions:12, deletions:3, binary:false, reverted:false},
  {path:'assets/logo.png', status:'modified', insertions:0, deletions:0, binary:true, reverted:false},
  {path:'docs/web-console/index.md', status:'modified', insertions:4, deletions:1, binary:false, reverted:true},
];

const messages = [
  {id:'u1', type:'user', timestamp:'10:00', turnId:'t1', content:'把撤销做出来',
    work:{startedAt:0, elapsed:31, ended:true}, workExpanded:false},
  {id:'a1', type:'assistant', timestamp:'10:00', turnId:'t1', content:'做完了。'},
  {id:'changes-t1', type:'changes', timestamp:'10:00', turnId:'t1', changes, changesTotal:4},
];

window.__calls = [];
store.setState({
  initClient: () => {}, pairingState: 'paired', connectionState: 'connected',
  client: {
    getState: () => 'connected',
    gitStatus: async () => ({branch:'main', dirty:true, files:[]}),
    revertTurnChange: async (params) => {
      window.__calls.push(params);
      if (params.path.includes('historyMapper')) {
        throw new RpcCallError('refused', -32000, 'revert_content_drift');
      }
      return {turn_id: params.turn_id, path: params.path, action: 'restore', bytes_written: 12};
    },
  },
  workspacePath: '/sample/synapse', activeProjectId: 'p1',
  projects: [{project_id:'p1',workspace_name:'synapse',workspace_path:'/sample/synapse',git_branch:'main'}],
  expandedProjectIds: ['p1'], currentSession: {project_id:'p1',thread_id:'s1'},
  sessionTitle: '撤销', sessionsTotal: 1,
  sessions: [{thread_id:'s1',title:'撤销',updated_at:new Date().toISOString(),time_label:'今天'}],
  modelName: 'model-a', availableModels: ['model-a'], thinkingLevel: 'medium',
  thinkingLevels: ['medium'], canSetThinking: true,
  submitPrompt: async () => {}, gitBranch: 'main', gitDirty: true, gitStatus: null,
  historyAvailable: true, historyHasMore: false, historyLoading: false,
  loadEarlierHistory: () => {}, runtimeStatus: 'idle', activeTurnId: null, activity: null,
  messages, revertError: null,
});

const texts = (selector) => [...document.querySelectorAll(selector)].map((el) => (el.textContent || '').trim());
window.__probe = () => ({
  offers: [...document.querySelectorAll('.console-gutter button[aria-label^="撤销 "]')].map((b) => b.getAttribute('aria-label')),
  confirms: [...document.querySelectorAll('.console-gutter button[aria-label^="确认撤销 "]')].map((b) => b.getAttribute('aria-label')),
  armed: (document.querySelector('.console-gutter')?.textContent || '').includes('恢复到本轮开始前？'),
  badges: texts('.console-gutter span').filter((text) => text === '已撤销').length,
  // The whole row, not just its review button: the badge sits beside the button.
  cards: [...document.querySelectorAll('.console-gutter button[aria-label^="审查 "]')].map((b) => (b.parentElement?.textContent || '').trim()),
  calls: window.__calls,
  alert: (document.querySelector('.console-gutter [role="alert"]')?.textContent || '').trim(),
});
window.__click = (label) => {
  const button = document.querySelector('.console-gutter button[aria-label="' + label + '"]');
  if (button === null) return false;
  button.click();
  return true;
};
createRoot(document.getElementById('root')).render(React.createElement(App));
`;

const server = await createServer({
  configFile: false, envDir: false, root: webRoot,
  plugins: [react(), {
    name: 'turn-revert-fixture',
    configureServer(server) {
      server.middlewares.use('/turn-revert-fixture', async (_req, res) => {
        const html = await server.transformIndexHtml('/turn-revert-fixture',
          '<!doctype html><html><head><meta charset="utf-8"></head><body><div id="root"></div><script type="module" src="/revert-entry.js"></script></body></html>');
        res.setHeader('Content-Type', 'text/html'); res.end(html);
      });
    },
    resolveId(id) { if (id === '/revert-entry.js') return '\0turn-revert-fixture'; },
    load(id) { if (id === '\0turn-revert-fixture') return fixture; },
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
  const page = await openPage(client, `${server.resolvedUrls.local[0]}turn-revert-fixture`);
  const run = (expression: string) => evaluate(client!, page, expression);
  const settle = () => new Promise((resolve) => setTimeout(resolve, 250));
  const check = async (label: string, expression: string) => {
    assert.equal(await run(expression), true, label); checks++; console.log(`PASS ${label}`);
  };
  await client.send('Emulation.setDeviceMetricsOverride', { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false }, page.sessionId);
  for (let i = 0; i < 100; i += 1) {
    if (await run(`document.querySelectorAll('.console-gutter [data-index]').length > 0`)) break;
    await settle();
  }
  await settle();

  await check('only a file the runtime kept a copy of is offered for undo', `(() => {
    const p = window.__probe();
    return p.offers.length === 2
      && p.offers.some((label) => label.includes('turn_reverts.py'))
      && p.offers.some((label) => label.includes('historyMapper.ts'))
      && !p.offers.some((label) => label.includes('logo.png'));
  })()`);
  await check('a file already undone is marked, and not offered again', `(() => {
    const p = window.__probe();
    return p.badges === 1
      && !p.offers.some((label) => label.includes('index.md'))
      && p.cards.some((text) => text.includes('index.md') && text.includes('已撤销'));
  })()`);
  await check(
    'one click only arms it',
    `window.__click('撤销 src/synapse/runtime/turn_reverts.py 的本轮改动')`,
  );
  await settle();
  await check('armed, not yet carried out', `(() => {
    const p = window.__probe();
    return p.armed === true && p.confirms.length === 1 && p.calls.length === 0;
  })()`);
  await settle();
  await check('the second click asks the runtime for that turn and path', `(() => {
    if (!window.__click('确认撤销 src/synapse/runtime/turn_reverts.py 的本轮改动')) return false;
    return true;
  })()`);
  await settle();
  await check('it asks with the turn and the one path', `(() => {
    const calls = window.__probe().calls;
    return calls.length === 1 && calls[0].turn_id === 't1'
      && calls[0].path === 'src/synapse/runtime/turn_reverts.py';
  })()`);
  await check('the card turns into 已撤销 and drops its counts', `(() => {
    const p = window.__probe();
    const card = p.cards.find((text) => text.includes('turn_reverts.py')) || '';
    return card.includes('已撤销') && !card.includes('+120') && p.badges === 2;
  })()`);
  await check(
    'the other file can be armed as well',
    `window.__click('撤销 web/src/stores/historyMapper.ts 的本轮改动')`,
  );
  await settle();
  await check(
    'the second click carries that one out too',
    `window.__click('确认撤销 web/src/stores/historyMapper.ts 的本轮改动')`,
  );
  await settle();
  await check('the refusal is visible in the transcript', `(() => {
    const p = window.__probe();
    return p.alert.includes('historyMapper.ts') && p.alert.includes('又被改过');
  })()`);
  await check('the refused file keeps its counts and is not marked', `(() => {
    const p = window.__probe();
    const card = p.cards.find((text) => text.includes('historyMapper.ts')) || '';
    return card.includes('+12') && card.includes('-3') && !card.includes('已撤销');
  })()`);
  const image = (await client.send('Page.captureScreenshot', { format: 'png' }, page.sessionId)) as { data: string };
  fs.writeFileSync(path.join(output, 'revert.png'), Buffer.from(image.data, 'base64'));
  console.log(`ALL ${checks} CHECKS PASSED; screenshots: ${output}`);
} finally {
  client?.close();
  if (browser) await closeBrowser(browser);
  await server.close();
}
