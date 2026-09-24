/**
 * Offline browser acceptance for the transcript's virtual window: synthetic state
 * only, no host, daemon or credentials.
 *
 * Run from web/: node tests/transcriptVirtual.verify.ts
 *
 * A long session used to mount every row in one commit, which froze the console.
 * The transcript now mounts only the rows near the viewport, and this script pins
 * the consequences that matter: a bounded number of mounted rows, a bottom-anchored
 * open, older rows arriving on demand, a rail jump that reaches a row which is not
 * mounted yet, and a prepended history page that does not move what is on screen.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { CdpClient, launchBrowser, closeBrowser, openPage, evaluate } from './helpers/cdp.ts';
import { httpProbe } from './helpers/httpProbe.ts';

const webRoot = path.resolve(import.meta.dirname, '..');
const output = path.resolve(webRoot, '..', '.tmp', 'transcript-virtual');
fs.mkdirSync(output, { recursive: true });
process.env.TEMP = output;
process.env.TMP = output;

/** Rows in the fixture: far past the point where mounting all of them was usable. */
const ROWS = 1200;
/** Rows prepended by the stub "load earlier" path. */
const EARLIER = 200;

const fixture = `
import React from 'react';
import {createRoot} from 'react-dom/client';
import {App} from '/src/App.tsx';
import {useConsoleStore as store} from '/src/stores/useConsoleStore.ts';
import {transcriptTurns, turnRailTickSlots} from '/src/stores/turnRail.ts';
import '/src/index.css';

const ROWS = ${ROWS};
const EARLIER = ${EARLIER};
const row = (i, tag) => ({
  id: tag + i,
  type: i % 3 === 0 ? 'user' : 'assistant',
  timestamp: 'Turn ' + i,
  content: '第 ' + i + ' 段：' + '内容'.repeat(40) + '\\n\\n' + '补充'.repeat(40),
  ...(i % 3 === 0 ? {work: {ended: true}} : {}),
});
let next = 0;
let messages = Array.from({length: ROWS}, (_, i) => row(i + 1, 'm'));
// The real "load earlier" path, with the RPC replaced by a synchronous prepend so
// the acceptance can run with no daemon.  It goes through the transcript's own
// button, so the anchor the transcript captures is the one under test.
const loadEarlierHistory = () => {
  next += 1;
  const older = Array.from({length: EARLIER}, (_, i) => row(-(next * EARLIER) + i, 'e'));
  store.setState({messages: [...older, ...store.getState().messages], historyHasMore: true});
};
store.setState({
  initClient: () => {}, pairingState: 'paired', connectionState: 'connected',
  workspacePath: '/sample/synapse', activeProjectId: 'sample',
  projects: [{project_id:'sample',workspace_name:'synapse',workspace_path:'/sample/synapse',git_branch:'main'}],
  expandedProjectIds: ['sample'], currentSession: {project_id:'sample',thread_id:'s0'},
  sessionTitle: '虚拟滚动验收', sessionsTotal: 1,
  sessions: [{thread_id:'s0',title:'虚拟滚动验收',updated_at:new Date().toISOString(),time_label:'今天'}],
  modelName: 'model-a', availableModels: ['model-a'], thinkingLevel: 'medium',
  thinkingLevels: ['medium'], canSetThinking: true,
  submitPrompt: async () => {}, gitBranch: 'main', gitDirty: false,
  // Starts exhausted: reaching the top must not load a page on its own until the
  // prepend case below asks for one.
  historyAvailable: true, historyHasMore: false, historyLoading: false,
  loadEarlierHistory,
  messages,
});
window.__rows = () => store.getState().messages.length;
let submitted = 0;
window.__submit = (type = 'user') => store.setState({messages: [...store.getState().messages, {
  id: 'submitted-after-jump-' + (++submitted), type, content: '继续', timestamp: 'now',
}]});
window.__more = () => store.setState({historyHasMore: true});
window.__indices = () => [...document.querySelectorAll('.console-gutter [data-index]')]
  .map((el) => Number(el.getAttribute('data-index')));
window.__indexOf = (id) => store.getState().messages.findIndex((m) => m.id === id);
// The rail's own rules, so the acceptance does not have to guess which turn a
// rail button stands for (empty slots render no button, so the button order is
// the non-empty slot order).
window.__anchorForButton = (k) => {
  const turns = transcriptTurns(store.getState().messages);
  const railRows = Math.min(24, Math.max(turns.length, 1));
  const slots = turnRailTickSlots(turns.length, railRows).filter((s) => s.length > 0);
  const indices = slots[k];
  return indices === undefined ? null : turns[indices[0]].anchorId;
};
window.__swap = (count) => {
  const t0 = performance.now();
  const swapped = Array.from({length: count}, (_, i) => row(i + 1, 'm'));
  store.setState({messages: swapped});
  return new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(
    () => resolve(performance.now() - t0))));
};
createRoot(document.getElementById('root')).render(React.createElement(App));
`;

const server = await createServer({
  configFile: false, envDir: false, root: webRoot,
  plugins: [react(), {
    name: 'transcript-virtual-fixture',
    configureServer(server) {
      server.middlewares.use('/transcript-virtual-fixture', async (_req, res) => {
        const html = await server.transformIndexHtml('/transcript-virtual-fixture',
          '<!doctype html><html><head><meta charset="utf-8"></head><body><div id="root"></div><script type="module" src="/fixture-entry.js"></script></body></html>');
        res.setHeader('Content-Type', 'text/html'); res.end(html);
      });
    },
    resolveId(id) { if (id === '/fixture-entry.js') return '\0transcript-virtual-fixture'; },
    load(id) { if (id === '\0transcript-virtual-fixture') return fixture; },
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
  const page = await openPage(client, `${server.resolvedUrls.local[0]}transcript-virtual-fixture`);
  const run = (expression: string) => evaluate(client!, page, expression);
  const settle = () => new Promise(resolve => setTimeout(resolve, 120));
  const check = async (label: string, expression: string) => {
    assert.equal(await run(expression), true, label); checks++; console.log(`PASS ${label}`);
  };
  const wait = async (expression: string) => {
    for (let i = 0; i < 200; i++) { if (await run(expression)) return; await settle(); }
    throw new Error(`fixture not ready: ${expression}`);
  };

  /** Rows the transcript actually mounted (one `data-index` wrapper each). */
  const mounted = `document.querySelectorAll('.console-gutter [data-index]').length`;
  const port = `document.querySelector('.console-gutter')`;

  await client.send('Emulation.setDeviceMetricsOverride', {width:1280,height:900,deviceScaleFactor:1,mobile:false}, page.sessionId);
  await wait(`${mounted} > 0`);
  await settle();

  // --- the window is bounded --------------------------------------------------
  await check('the fixture really is a long session', `window.__rows() === ${ROWS}`);
  await check('the transcript does not mount every row', `${mounted} < 80`);
  await check('the scroll height still covers every row',
    `${port}.scrollHeight > ${ROWS} * 20`);
  await check('the window is a small fraction of the session',
    `${mounted} * 10 < window.__rows()`);

  // --- the open is bottom-anchored -------------------------------------------
  await check('the newest row is on screen when the session opens', `(() => {
    const port = document.querySelector('.console-gutter');
    return port.scrollHeight - port.scrollTop - port.clientHeight < 40;
  })()`);
  await check('the newest row is the mounted one', `(() => {
    const rows = [...document.querySelectorAll('.console-gutter [data-index]')];
    const last = rows[rows.length - 1];
    return last.getAttribute('data-index') === String(window.__rows() - 1);
  })()`);

  // --- older rows arrive on demand -------------------------------------------
  // A wheel event arms the reader latch exactly as a real gesture does, so the
  // follow releases the view instead of dragging it back to the bottom.
  await run(`(() => {
    const port = document.querySelector('.console-gutter');
    port.dispatchEvent(new WheelEvent('wheel', {deltaY: -1, bubbles: true}));
    port.scrollTop = Math.round((port.scrollHeight - port.clientHeight) * 0.25);
  })()`);
  await settle();
  await settle();
  await check('scrolling up mounts a window that moved with the viewport', `(() => {
    const ids = window.__indices();
    return Math.min(...ids) > 0 && Math.max(...ids) < window.__rows() - 1;
  })()`);
  await check('the window stays bounded after scrolling', `${mounted} < 80`);
  await check('the newest row is no longer mounted', `(() => {
    return !window.__indices().includes(window.__rows() - 1);
  })()`);

  // --- a rail jump reaches a row that is not mounted yet ----------------------
  // The rail has to scroll by index: an off-screen turn has no `[data-turn-id]`
  // element for it to find, so a DOM lookup would silently do nothing.
  await check('a rail target exists whose row is not mounted', `(() => {
    const mounted = new Set(window.__indices());
    const buttons = [...document.querySelectorAll('[data-turn-rail] button')];
    for (let k = buttons.length - 1; k >= 0; k -= 1) {
      const anchor = window.__anchorForButton(k);
      if (anchor === null) continue;
      const index = window.__indexOf(anchor);
      if (index > 0 && !mounted.has(index)) {
        window.__railButton = k;
        window.__railTarget = index;
        return true;
      }
    }
    return false;
  })()`);
  await run(`document.querySelectorAll('[data-turn-rail] button')[window.__railButton].click()`);
  // The jump animates, so the landing is read only once the scroller comes to rest.
  let lastTop = -1;
  for (let i = 0; i < 150; i += 1) {
    const top = await run(`document.querySelector('.console-gutter').scrollTop`) as number;
    if (top === lastTop) break;
    lastTop = top;
    await new Promise(resolve => setTimeout(resolve, 60));
  }
  const targetRow = `document.querySelector('.console-gutter [data-index="' + window.__railTarget + '"]')`;
  const landing = `(() => {
    const el = ${targetRow};
    const port = document.querySelector('.console-gutter');
    if (el === null) return 'not mounted';
    return Math.round(el.getBoundingClientRect().top - port.getBoundingClientRect().top);
  })()`;
  console.log(`INFO the jumped-to row landed ${await run(landing)} px below the viewport top`);
  await check('the rail jump mounts the row it jumped to', `${targetRow} !== null`);
  // The scroller runs up behind the header on purpose, so a jump has to land
  // *below* the chrome: `scroll-padding-top` is the reservation the jump applies.
  await check('the jumped-to row lands below the chrome, not behind it', `(() => {
    const el = ${targetRow};
    const port = document.querySelector('.console-gutter');
    const reserved = Number.parseFloat(getComputedStyle(port).scrollPaddingTop) || 0;
    const offset = el.getBoundingClientRect().top - port.getBoundingClientRect().top;
    return offset >= reserved - 2 && offset <= reserved + 4;
  })()`);

  // Worker parsing can change row heights after the first apparently settled
  // landing. The same rail anchor must survive those late measurements.
  for (let i = 0; i < 5; i += 1) {
    await settle();
    await check('the rail anchor survives late Markdown measurements', `(() => {
      const el = ${targetRow};
      const port = document.querySelector('.console-gutter');
      const reserved = Number.parseFloat(getComputedStyle(port).scrollPaddingTop) || 0;
      const offset = el.getBoundingClientRect().top - port.getBoundingClientRect().top;
      return offset >= reserved - 2 && offset <= reserved + 4;
    })()`);
  }

  // --- a prepended page does not move what is on screen ----------------------
  // First mount the top while paging is disabled. Capture the same MESSAGE
  // before the load, not an index sampled after the synchronous prepend already
  // happened (indices change, and async formatting may still be in flight).
  await run(`(() => {
    const port = document.querySelector('.console-gutter');
    port.dispatchEvent(new WheelEvent('wheel', {deltaY: -1, bubbles: true}));
    port.scrollTop = 100;
  })()`);
  await settle();
  await settle();
  await run(`window.__more()`);
  await settle();
  await run(`(() => {
    const port = document.querySelector('.console-gutter');
    port.dispatchEvent(new WheelEvent('wheel', {deltaY: -1, bubbles: true}));
    port.scrollTop = 0;
    const first = document.querySelector('.console-gutter [data-index="0"]');
    window.__probeTop = first.getBoundingClientRect().top;
  })()`);
  await settle();
  // The load is triggered by reaching the top; if the scroll event did not fire
  // (already at 0), the button is the manual path and must behave the same.
  await run(`(() => {
    if (window.__rows() !== ${ROWS}) return;
    const button = [...document.querySelectorAll('.console-gutter button')]
      .find((b) => b.textContent.includes('加载更早历史'));
    if (button) button.click();
  })()`);
  for (let i = 0; i < 60; i += 1) {
    if (await run(`window.__rows() === ${ROWS} + ${EARLIER}`)) break;
    await new Promise(resolve => setTimeout(resolve, 60));
  }
  await check('the prepended page actually arrived',
    `window.__rows() === ${ROWS} + ${EARLIER}`);
  for (let i = 0; i < 5; i += 1) {
    await settle();
    await check('the prepend keeps the reading row in place through late parsing', `(() => {
      const index = window.__indexOf('m1');
      const el = document.querySelector('.console-gutter [data-index="' + index + '"]');
      if (el === null) return false;
      return Math.abs(el.getBoundingClientRect().top - window.__probeTop) <= 8;
    })()`);
  }
  await check('the view is still not at the bottom after the prepend', `(() => {
    const port = document.querySelector('.console-gutter');
    return port.scrollHeight - port.scrollTop - port.clientHeight > 40;
  })()`);
  await check('the window stays bounded after the prepend', `${mounted} < 80`);

  // --- the cost of a swap is bounded, not proportional to the session --------
  const elapsed = await run(`window.__swap(${ROWS})`) as number;
  console.log(`INFO a ${ROWS}-row swap committed in ${Math.round(elapsed)} ms`);
  assert.ok(elapsed < 1500, `a ${ROWS}-row swap must not freeze the frame (took ${elapsed} ms)`);
  checks++;

  // A new prompt/explicit follow must supersede a retained rail anchor; without
  // clearing it, the next measurement would drag the reader back to old history.
  await run(`document.querySelector('[data-turn-rail] button').click()`);
  await settle();
  await run(`window.__submit()`);
  await settle();
  await settle();
  await check('a new prompt supersedes the rail jump and follows the newest row', `(() => {
    const port = document.querySelector('.console-gutter');
    return port.scrollHeight - port.scrollTop - port.clientHeight < 40;
  })()`);
  await run(`document.querySelector('[data-turn-rail] button').click()`);
  await settle();
  await run(`window.dispatchEvent(new Event('transcript:jump-bottom')); window.__submit('assistant')`);
  await settle();
  await settle();
  await check('explicit follow supersedes the rail jump', `(() => {
    const port = document.querySelector('.console-gutter');
    return port.scrollHeight - port.scrollTop - port.clientHeight < 40;
  })()`);

  console.log(`ALL ${checks} CHECKS PASSED`);
} finally {
  client?.close();
  if (browser) await closeBrowser(browser);
  await server.close();
}
