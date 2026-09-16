/**
 * Offline browser acceptance for the space a *folded* turn takes: synthetic state
 * only, no host, daemon or credentials.
 *
 * Run from web/: node tests/transcriptFoldSpace.verify.ts
 *
 * A collapsed turn paints one header -- the "已工作 N 秒" strip of its first step --
 * and hides every other step of its fold, so a long turn owns N rows of which a few
 * are on screen. Hidden rows previously retained both wrapper padding and, when
 * unmounted, estimated heights. Use a hidden tail much longer than overscan while
 * keeping every narration visible, so comparing the last painted row to activity
 * cannot mistake offscreen visible content for blank space. Also cover cached
 * expanded heights and incremental appends. Hidden rows must not enter the size
 * model at all.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { CdpClient, launchBrowser, closeBrowser, openPage, evaluate } from './helpers/cdp.ts';
import { httpProbe } from './helpers/httpProbe.ts';

const webRoot = path.resolve(import.meta.dirname, '..');
const output = path.resolve(webRoot, '..', '.tmp', 'transcript-fold-space');
fs.mkdirSync(output, { recursive: true });
process.env.TEMP = output;
process.env.TMP = output;

/** Model steps in the folded turn: each one is a thought plus a tool batch. */
const STEPS = 160;
/** Steps after which the turn narrates; the text rows are the only painted steps. */
const NARRATES = [2, 6, 10];

const fixture = `
import React from 'react';
import {createRoot} from 'react-dom/client';
import {App} from '/src/App.tsx';
import {useConsoleStore as store} from '/src/stores/useConsoleStore.ts';
import '/src/index.css';

const tool = (id) => ({id, callId:null, name:'execute', label:'最终定向回归汇总', category:'other',
  path:null, status:'completed', preview:null, error:false, sub:false, parentId:null,
  subagentStatus:null, subagentName:null, icon:'build', duration:'done'});
const messages = [];
// Settled history above, so the running turn sits at the bottom of a long session.
for (let t = 1; t <= 30; t += 1) {
  const turnId = 'h' + t;
  messages.push({id:'hu'+t, type:'user', timestamp:'09:0'+t, content:'第 '+t+' 轮提问', turnId,
    work:{startedAt:0, elapsed:41, ended:true}});
  messages.push({id:'hth'+t+'a', type:'thought', timestamp:'09:0'+t, turnId, duration:'1.0s',
    content:'思考内容'});
  messages.push({id:'htg'+t, type:'tool_group', timestamp:'09:0'+t, turnId, expanded:false,
    parallel:false, tools:[tool('tool'+t)]});
  messages.push({id:'ha'+t+'a', type:'assistant', timestamp:'09:0'+t, turnId,
    content:'我先定位 Web 控制台前端渲染相关代码。'});
  messages.push({id:'hth'+t+'b', type:'thought', timestamp:'09:0'+t, turnId, duration:'1.0s',
    content:'思考内容 2'});
  messages.push({id:'ha'+t+'b', type:'assistant', timestamp:'09:0'+t, turnId,
    content:'第二段说明文字。'});
}
// The running turn: STEPS model steps, narration after a few of them, one fold.
messages.push({id:'lu', type:'user', timestamp:'09:41', content:'最后一轮提问', turnId:'live',
  work:{startedAt:Date.now() - 41000, ended:false}});
const narrates = ${JSON.stringify(NARRATES)};
for (let s = 1; s <= ${STEPS}; s += 1) {
  messages.push({id:'lth'+s, type:'thought', timestamp:'09:41', turnId:'live',
    duration:'1.0s', content:'思考内容 ' + s});
  messages.push({id:'ltg'+s, type:'tool_group', timestamp:'09:41', turnId:'live',
    expanded:false, parallel:false, tools:[tool('ltool'+s)]});
  if (narrates.includes(s)) {
    messages.push({id:'la'+s, type:'assistant', timestamp:'09:41', turnId:'live',
      content:'第 ' + s + ' 段说明：我先定位 Web 控制台前端渲染相关代码。'});
  }
}
messages.push({id:'lthlast', type:'thought', timestamp:'09:41', turnId:'live',
  duration:'streaming', content:'继续思考', streaming:true});
store.setState({
  initClient: () => {}, pairingState: 'paired', connectionState: 'connected',
  workspacePath: '/sample/synapse', activeProjectId: 'sample',
  projects: [{project_id:'sample',workspace_name:'synapse',workspace_path:'/sample/synapse',git_branch:'main'}],
  expandedProjectIds: ['sample'], currentSession: {project_id:'sample',thread_id:'s0'},
  sessionTitle: '折叠行间距验收', sessionsTotal: 1,
  sessions: [{thread_id:'s0',title:'折叠行间距验收',updated_at:new Date().toISOString(),time_label:'今天'}],
  modelName: 'model-a', availableModels: ['model-a'], thinkingLevel: 'medium',
  thinkingLevels: ['medium'], canSetThinking: true,
  submitPrompt: async () => {}, gitBranch: 'main', gitDirty: false,
  historyAvailable: true, historyHasMore: false, historyLoading: false,
  loadEarlierHistory: () => {},
  runtimeStatus: 'running', activeTurnId: 'live',
  activity: {phase:'reasoning', detail:'model thinking', startedAt: Date.now(), active:true},
  messages,
});
/** Mounted rows of the folded turn, in list order, with their painted height. */
window.__rows = () => {
  const port = document.querySelector('.console-gutter');
  const portTop = port.getBoundingClientRect().top;
  return [...document.querySelectorAll('.console-gutter [data-index]')].map((el) => ({
    i: Number(el.getAttribute('data-index')),
    top: Math.round(el.getBoundingClientRect().top - portTop + port.scrollTop),
    h: el.offsetHeight,
    text: (el.textContent || '').trim(),
  }));
};
window.__statusTop = () => {
  const port = document.querySelector('.console-gutter');
  const line = [...document.querySelectorAll('.console-gutter .font-mono')]
    .find((el) => (el.textContent || '').includes('model thinking'));
  return line === undefined ? null
    : Math.round(line.getBoundingClientRect().top - port.getBoundingClientRect().top + port.scrollTop);
};
/** The last painted row of the turn, in the same coordinate space. */
window.__lastPaintedBottom = () => {
  const painted = window.__rows().filter((r) => r.text !== '');
  const last = painted[painted.length - 1];
  return last === undefined ? null : last.top + last.h;
};
/** Click the running turn's own fold header (the newest one on screen). */
window.__toggleFold = () => {
  const rows = [...document.querySelectorAll('.console-gutter [data-index]')]
    .filter((el) => (el.textContent || '').includes('已工作'));
  const row = rows[rows.length - 1];
  const button = row === undefined ? undefined : row.querySelector('button');
  if (button === undefined) return false;
  button.click();
  return true;
};
window.__setExpanded = (expanded) => store.setState({messages: store.getState().messages.map(
  (m) => m.id === 'lu' ? {...m, workExpanded: expanded} : m)});
window.__append = () => store.setState({messages: [...store.getState().messages,
  {id:'appended-text', type:'assistant', timestamp:'09:42', turnId:'live', content:'新增段说明'},
  ...Array.from({length:160}, (_, i) => ({id:'appended-thought'+i, type:'thought',
    timestamp:'09:42', turnId:'live', duration:'1s', content:'隐藏步骤'})),
]});
window.__bottom = () => {
  const port = document.querySelector('.console-gutter');
  port.scrollTop = port.scrollHeight;
};
createRoot(document.getElementById('root')).render(React.createElement(App));
`;

const server = await createServer({
  configFile: false, envDir: false, root: webRoot,
  plugins: [react(), {
    name: 'transcript-fold-space-fixture',
    configureServer(server) {
      server.middlewares.use('/transcript-fold-space-fixture', async (_req, res) => {
        const html = await server.transformIndexHtml('/transcript-fold-space-fixture',
          '<!doctype html><html><head><meta charset="utf-8"></head><body><div id="root"></div><script type="module" src="/fold-space-entry.js"></script></body></html>');
        res.setHeader('Content-Type', 'text/html'); res.end(html);
      });
    },
    resolveId(id) { if (id === '/fold-space-entry.js') return '\0transcript-fold-space-fixture'; },
    load(id) { if (id === '\0transcript-fold-space-fixture') return fixture; },
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
  const page = await openPage(client, `${server.resolvedUrls.local[0]}transcript-fold-space-fixture`);
  const run = (expression: string) => evaluate(client!, page, expression);
  const settle = () => new Promise(resolve => setTimeout(resolve, 250));
  const check = async (label: string, expression: string) => {
    assert.equal(await run(expression), true, label); checks++; console.log(`PASS ${label}`);
  };
  await client.send('Emulation.setDeviceMetricsOverride', {width:1280,height:900,deviceScaleFactor:1,mobile:false}, page.sessionId);
  for (let i = 0; i < 100; i += 1) {
    if (await run(`document.querySelectorAll('.console-gutter [data-index]').length > 0`)) break;
    await settle();
  }
  await settle();
  await settle();

  /** The folded turn's own rows, and the segments of narration among them. */
  const probe = `(() => {
    const rows = window.__rows();
    const header = rows.findLast((r) => r.text.startsWith('已工作'));
    const segments = rows.filter((r) => /^(第 [0-9]+ 段说明|新增段说明)/.test(r.text));
    const hidden = rows.filter((r) => r.text === '');
    return {header, segments, hidden: hidden.length, blanks: hidden.filter((r) => r.h > 0).length};
  })()`;

  // --- the long hidden tail must not reserve any virtual height ---------------
  await run('window.__bottom()');
  await settle();
  console.log('Collapsed geometry', await run(probe));
  await check('the running turn is folded into one header', `(() => {
    const p = ${probe};
    return p.header !== undefined && p.segments.length === ${NARRATES.length};
  })()`);
  await check('hidden steps are excluded from the virtual window', `(() => {
    const p = ${probe};
    return p.hidden === 0;
  })()`);

  // --- a hidden step occupies nothing ----------------------------------------
  await check('a step the fold hides measures zero', `(() => {
    const p = ${probe};
    return p.blanks === 0;
  })()`);
  await check('narration follows the header by one row gap only', `(() => {
    const p = ${probe};
    const first = p.segments[0];
    return first.top - (p.header.top + p.header.h) <= 20;
  })()`);
  await check('two segments of the folded turn are one row apart', `(() => {
    const s = ${probe}.segments;
    for (let k = 1; k < s.length; k += 1) {
      if (s[k].top - (s[k - 1].top + s[k - 1].h) > 20) return false;
    }
    return true;
  })()`);
  await check('the status line sits at the bottom of the last painted row', `(() => {
    const bottom = window.__lastPaintedBottom();
    return bottom !== null && window.__statusTop() === bottom;
  })()`);

  // --- opening the fold paints the steps instead of leaving the blanks -------
  await run('window.__setExpanded(true)');
  await settle();
  await settle();
  await check('an open fold keeps the status line at the bottom of the turn', `(() => {
    const bottom = window.__lastPaintedBottom();
    return bottom !== null && window.__statusTop() === bottom;
  })()`);
  await check('an open fold paints the steps it was hiding', `(() => {
    return window.__rows().some((r) => r.text.includes('最终定向回归汇总'));
  })()`);

  await run('window.__setExpanded(false)');
  await settle();
  await run('window.__bottom()');
  await settle();
  await check('collapsing measured steps leaves no cached blank height', `(() => {
    const p = ${probe};
    return p.segments.length === ${NARRATES.length} && p.hidden === 0
      && window.__statusTop() === window.__lastPaintedBottom();
  })()`);
  await run('window.__append()');
  await settle();
  await run('window.__bottom()');
  await settle();
  await check('streamed hidden steps do not push status away from the final narration', `(() => {
    const p = ${probe};
    const last = p.segments[p.segments.length - 1];
    return last?.text.includes('新增段说明') && p.hidden === 0
      && window.__statusTop() === last.top + last.h;
  })()`);
  console.log(`ALL ${checks} CHECKS PASSED`);
} finally {
  client?.close();
  if (browser) await closeBrowser(browser);
  await server.close();
}
