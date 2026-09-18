/**
 * Offline browser acceptance for the turn rail's jumps: synthetic state only, no
 * host, daemon or credentials.
 *
 * Run from web/: node tests/turnRailJump.verify.ts
 *
 * A rail row stands for a turn, and clicking it scrolls that turn's *start* to
 * the top of the transcript -- every row, including the bottom one.  The four
 * turns below are each taller than the scrollport, so "the row jumped to its
 * turn" and "the row jumped to the bottom" cannot be confused for one another.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { CdpClient, launchBrowser, closeBrowser, openPage, evaluate } from './helpers/cdp.ts';
import { httpProbe } from './helpers/httpProbe.ts';

const webRoot = path.resolve(import.meta.dirname, '..');
const output = path.resolve(webRoot, '..', '.tmp', 'turn-rail-jump');
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

// Four turns, each taller than the scrollport, so "start of a turn" and "end of
// the transcript" are far apart and the difference is measurable.
const body = (marker) => Array.from({length: 24}, (_, i) => marker + ' 第 ' + (i + 1) + ' 段：' + '内容'.repeat(24)).join('\\n\\n');
const messages = [];
for (let i = 1; i <= 4; i += 1) {
  messages.push({id: 'u' + i, type: 'user', timestamp: '10:0' + i, content: '第 ' + i + ' 个问题'});
  messages.push({id: 'a' + i, type: 'assistant', timestamp: '10:0' + i, content: body('A' + i)});
}
store.setState({
  initClient: () => {}, pairingState: 'paired', connectionState: 'connected',
  workspacePath: '/sample/synapse', activeProjectId: 'sample',
  projects: [{project_id:'sample',workspace_name:'synapse',workspace_path:'/sample/synapse',git_branch:'main'}],
  expandedProjectIds: ['sample'], currentSession: {project_id:'sample',thread_id:'s0'},
  sessionTitle: '轮次导航验收', sessionsTotal: 1,
  sessions: [{thread_id:'s0',title:'轮次导航验收',updated_at:new Date().toISOString(),time_label:'今天'}],
  modelName: 'model-a', availableModels: ['model-a'], thinkingLevel: 'medium',
  thinkingLevels: ['medium'], canSetThinking: true,
  submitPrompt: async () => {}, gitBranch: 'main', gitDirty: false,
  historyAvailable: true, historyHasMore: false, historyLoading: false,
  messages,
});
createRoot(document.getElementById('root')).render(React.createElement(App));
`;

const server = await createServer({
  configFile: false, envDir: false, root: webRoot,
  plugins: [react(), {
    name: 'turn-rail-jump-fixture',
    configureServer(server) {
      server.middlewares.use('/turn-rail-fixture', async (_req, res) => {
        const html = await server.transformIndexHtml('/turn-rail-fixture',
          '<!doctype html><html><head><meta charset="utf-8"></head><body><div id="root"></div><script type="module" src="/fixture-entry.js"></script></body></html>');
        res.setHeader('Content-Type', 'text/html'); res.end(html);
      });
    },
    resolveId(id) { if (id === '/fixture-entry.js') return '\0turn-rail-fixture'; },
    load(id) { if (id === '\0turn-rail-fixture') return fixture; },
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
  const page = await openPage(client, `${server.resolvedUrls.local[0]}turn-rail-fixture`);
  const run = (expression: string) => evaluate(client!, page, expression);
  const settle = () => new Promise(resolve => setTimeout(resolve, 120));
  const check = async (label: string, expression: string) => {
    assert.equal(await run(expression), true, label); checks++; console.log(`PASS ${label}`);
  };
  const wait = async (expression: string) => {
    for (let i = 0; i < 120; i++) { if (await run(expression)) return; await settle(); }
    throw new Error(`fixture not ready: ${expression}`);
  };
  /** Click one rail row and wait for the smooth scroll to come to rest. */
  const clickRow = async (row: number) => {
    await run(`document.querySelectorAll('[data-turn-rail] button')[${row}].click()`);
    let last = -1;
    for (let i = 0; i < 150; i += 1) {
      const top = await run(`document.querySelector('.console-gutter').scrollTop`) as number;
      if (top === last) return;
      last = top;
      await new Promise(resolve => setTimeout(resolve, 60));
    }
    throw new Error('the transcript never came to rest');
  };
  /**
   * A jumped-to turn must land *below* the header, not behind its acrylic.
   *
   * The scroller is pulled up behind the header on purpose (the material needs
   * something to blur), so the top of the scrollport is 40px up under the bar:
   * a `block: 'start'` alignment without `scroll-padding-top` put the turn's
   * user message exactly there, half hidden.
   */
  const landsBelowChrome = (turnId: string) => `(() => {
    const port = document.querySelector('.console-gutter');
    const anchor = port.querySelector('[data-turn-id="${turnId}"]');
    const bar = document.querySelector('header').getBoundingClientRect().bottom;
    const top = anchor.getBoundingClientRect().top;
    return top >= bar && top <= bar + 40;
  })()`;
  const bottomDistance = `(() => {
    const port = document.querySelector('.console-gutter');
    return Math.round(port.scrollHeight - port.scrollTop - port.clientHeight);
  })()`;
  /** Height of one turn, read off the gap between two consecutive anchors. */
  const turnHeight = `(() => {
    const port = document.querySelector('.console-gutter');
    const from = port.querySelector('[data-turn-id="u3"]').getBoundingClientRect().top;
    const to = port.querySelector('[data-turn-id="u4"]').getBoundingClientRect().top;
    return Math.round(to - from);
  })()`;

  await wait(`document.querySelectorAll('[data-turn-rail] button').length === 4`);
  await client.send('Emulation.setDeviceMetricsOverride', {width:1280,height:900,deviceScaleFactor:1,mobile:false}, page.sessionId);
  await settle();
  await check('the rail has one row per turn', `document.querySelectorAll('[data-turn-rail] button').length === 4`);
  await check('the transcript really scrolls', `(() => { const p = document.querySelector('.console-gutter'); return p.scrollHeight > p.clientHeight * 3; })()`);
  await check('a turn is taller than the scrollport, so a jump is measurable',
    `${turnHeight} > document.querySelector('.console-gutter').clientHeight`);

  // --- every row lands on that turn's user message ----------------------------
  await clickRow(0);
  await check('the top row lands on the first turn\'s user message', `${landsBelowChrome('u1')}`);
  await check('the top row is at the top of the transcript', `document.querySelector('.console-gutter').scrollTop < 100`);
  await clickRow(2);
  await check('a middle row lands on that turn\'s user message', `${landsBelowChrome('u3')}`);
  await check('a middle row is not the bottom of the transcript', `${bottomDistance} > 100`);
  await clickRow(3);
  await check('the bottom row lands on the last turn\'s user message', `${landsBelowChrome('u4')}`);
  await check('the bottom row is not the end of the transcript', `${bottomDistance} > 100`);

  console.log(`ALL ${checks} CHECKS PASSED`);
} finally {
  client?.close();
  if (browser) await closeBrowser(browser);
  await server.close();
}
