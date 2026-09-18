/**
 * Real-browser acceptance for a turn's change cards.
 *
 * Synthetic state only: no host, no daemon, no repository.  It mounts the console with
 * a turn that changed three of five files and checks what the reader sees -- the cards,
 * their own counts, the "showing N of M" line -- and that clicking one opens the git
 * explorer on that file.  Run from web/: node --test tests/transcriptChanges.verify.ts
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { CdpClient, launchBrowser, closeBrowser, openPage, evaluate } from './helpers/cdp.ts';
import { httpProbe } from './helpers/httpProbe.ts';

const webRoot = path.resolve(import.meta.dirname, '..');
const output = path.resolve(webRoot, '..', '.tmp', 'transcript-changes');
fs.mkdirSync(output, { recursive: true });
process.env.TEMP = output;
process.env.TMP = output;

const fixture = `
import React from 'react';
import {createRoot} from 'react-dom/client';
import {App} from '/src/App.tsx';
import {useConsoleStore as store} from '/src/stores/useConsoleStore.ts';
import '/src/index.css';

const changes = [
  {path:'src/synapse/runtime/workspace_changes.py', status:'added', insertions:120, deletions:0, binary:false},
  {path:'web/src/components/transcriptRows/ChangesRow.tsx', status:'modified', insertions:12, deletions:3, binary:false},
  {path:'docs/web-console/index.md', status:'deleted', insertions:0, deletions:38, binary:false},
];

const messages = [
  {id:'u1', type:'user', timestamp:'10:00', turnId:'t1', content:'把改动卡片做出来',
    work:{startedAt:0, elapsed:31, ended:true}, workExpanded:false},
  {id:'th1', type:'thought', timestamp:'10:00', turnId:'t1', duration:'1.2s', expanded:false,
    content:'先看服务端有没有信号。'},
  {id:'a1', type:'assistant', timestamp:'10:00', turnId:'t1', content:'做完了。'},
  {id:'changes-t1', type:'changes', timestamp:'10:00', turnId:'t1', changes, changesTotal:5},
];

store.setState({
  initClient: () => {}, pairingState: 'paired', connectionState: 'connected',
  workspacePath: '/sample/synapse', activeProjectId: 'p1',
  projects: [{project_id:'p1',workspace_name:'synapse',workspace_path:'/sample/synapse',git_branch:'main'}],
  expandedProjectIds: ['p1'], currentSession: {project_id:'p1',thread_id:'s1'},
  sessionTitle: '改动卡片', sessionsTotal: 1,
  sessions: [{thread_id:'s1',title:'改动卡片',updated_at:new Date().toISOString(),time_label:'今天'}],
  modelName: 'model-a', availableModels: ['model-a'], thinkingLevel: 'medium',
  thinkingLevels: ['medium'], canSetThinking: true,
  submitPrompt: async () => {}, gitBranch: 'main', gitDirty: true, gitStatus: null,
  historyAvailable: true, historyHasMore: false, historyLoading: false,
  loadEarlierHistory: () => {}, runtimeStatus: 'idle', activeTurnId: null, activity: null,
  messages,
});

/** Everything the reader can see of the change block, plus where the explorer is. */
window.__probe = () => {
  const cards = [...document.querySelectorAll('.console-gutter button[aria-label^="审查 "]')].map((b) => ({
    label: b.getAttribute('aria-label'),
    text: (b.textContent || '').trim(),
  }));
  const headers = [...document.querySelectorAll('.console-gutter div')]
    .map((el) => el.textContent || '')
    .filter((text) => text.includes('本轮工作区改动'));
  const header = headers.length === 0 ? null
    : headers.sort((a, b) => a.length - b.length)[0].slice(0, 60);
  return {
    painted: cards.length > 0,
    header,
    cards,
    explorerOpen: document.querySelector('[aria-label="Git Explorer"]') !== null,
    explorerPath: store.getState().gitExplorer === null ? null : store.getState().gitExplorer.path,
  };
};
window.__clickCard = (index) => {
  const cards = [...document.querySelectorAll('.console-gutter button[aria-label^="审查 "]')];
  const card = cards[index];
  if (card === undefined) return false;
  card.click();
  return true;
};
createRoot(document.getElementById('root')).render(React.createElement(App));
`;

const server = await createServer({
  configFile: false, envDir: false, root: webRoot,
  plugins: [react(), {
    name: 'transcript-changes-fixture',
    configureServer(server) {
      server.middlewares.use('/transcript-changes-fixture', async (_req, res) => {
        const html = await server.transformIndexHtml('/transcript-changes-fixture',
          '<!doctype html><html><head><meta charset="utf-8"></head><body><div id="root"></div><script type="module" src="/changes-entry.js"></script></body></html>');
        res.setHeader('Content-Type', 'text/html'); res.end(html);
      });
    },
    resolveId(id) { if (id === '/changes-entry.js') return '\0transcript-changes-fixture'; },
    load(id) { if (id === '\0transcript-changes-fixture') return fixture; },
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
  const page = await openPage(client, `${server.resolvedUrls.local[0]}transcript-changes-fixture`);
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

  await check('the turn paints its change block', `window.__probe().painted === true`);
  await check('a folded turn still shows what it changed', `(() => {
    const p = window.__probe();
    return p.painted && p.cards.length === 3;
  })()`);
  await check('each card names its file and its own counts', `(() => {
    const cards = window.__probe().cards;
    return cards[0].text.includes('workspace_changes.py') && cards[0].text.includes('+120')
      && cards[1].text.includes('+12') && cards[1].text.includes('-3')
      && cards[2].text.includes('-38');
  })()`);
  await check('a bounded list says how many it is not showing', `(() => {
    const p = window.__probe();
    return p.header.includes('5 个文件') && p.header.includes('显示前 3 个');
  })()`);
  await check('clicking a card asks for that file', `(() => {
    if (!window.__clickCard(1)) return false;
    const p = window.__probe();
    return p.explorerPath === 'web/src/components/transcriptRows/ChangesRow.tsx';
  })()`);
  await settle();
  await check('the git explorer opens for it', `window.__probe().explorerOpen === true`);
  const image = (await client.send('Page.captureScreenshot', { format: 'png' }, page.sessionId)) as { data: string };
  fs.writeFileSync(path.join(output, 'changes.png'), Buffer.from(image.data, 'base64'));
  console.log(`ALL ${checks} CHECKS PASSED; screenshots: ${output}`);
} finally {
  client?.close();
  if (browser) await closeBrowser(browser);
  await server.close();
}
