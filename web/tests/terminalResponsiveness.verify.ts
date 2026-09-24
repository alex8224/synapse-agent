/**
 * Offline browser acceptance for terminal responsiveness while the transcript
 * streams.
 *
 * Run from web/ (Node 22.18+ strips the TypeScript with no transpiler step):
 *
 *   $env:NODE_OPTIONS=''; $env:NODE_USE_ENV_PROXY=''
 *   node --test tests/terminalResponsiveness.verify.ts
 *
 * The report is about a stall, not a crash.  While the model streams a long
 * Markdown answer, the bottom xterm panel stopped echoing keystrokes until the
 * turn finished, because the growing answer is re-parsed and re-rendered on the
 * same main thread the terminal shares.  This script mounts the real `App` in an
 * offline Vite fixture, drives a 40 ms Markdown stream through the real console
 * store, and keeps typing into the real xterm for as long as the stream runs.
 *
 * The desktop PTY bridge is the only production surface replaced: a Vite module
 * mock answers `tauri_terminal_*` and echoes each write back as
 * `terminal-data-<id>`, so there is no daemon, no shell and no credential.  The
 * fixture owns its own synthetic transcript and terminal session, and every run
 * artifact stays under the repository's `.tmp/`.
 *
 * Every keystroke must echo in order while both surfaces keep painting. A
 * generous sub-second latency budget also rejects the original ~1s stalls;
 * merely finishing before the turn ends would allow that regression through.
 * The 4x CPU throttle and broad budget leave headroom for machine variation.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { CdpClient, launchBrowser, closeBrowser, openPage, evaluate } from './helpers/cdp.ts';
import { httpProbe } from './helpers/httpProbe.ts';

const fileStartAt = Date.now();
const stamp = (label: string) => console.log(`INFO ${label} at +${Date.now() - fileStartAt} ms`);
const webRoot = path.resolve(import.meta.dirname, '..');
const output = path.resolve(webRoot, '..', '.tmp', 'terminal-responsiveness');
fs.mkdirSync(output, { recursive: true });
// Keep every run artifact (browser profile, temp files) under the repo's .tmp.
process.env.TEMP = output;
process.env.TMP = output;

/** One streamed Markdown update every frame at 25fps, like the real coalescer. */
const TICK_MS = 40;
/** How much Markdown the "model" streams in total (well under Markdown's 200k cap). */
const TARGET_CHARS = 100_000;
/** Gap between keystrokes on the test side; the main thread paces the rest. */
const KEY_GAP_MS = 50;
/** Safety caps so a wedged fixture cannot run forever. */
const MAX_KEYS = 250;
const STREAM_GUARD_MS = 90_000;
/** Amplify the per-update CPU cost so the stall is not hidden by a fast machine. */
const CPU_THROTTLE = 4;
/** Lower bound on the keystrokes the stream should have spanned. */
const KEY_COUNT_MIN = 8;
/** A token only the tail of the streamed answer carries, to prove it all landed. */
const SENTINEL = 'TERMINAL_RESPONSIVENESS_SENTINEL_7f3a';

const VIRTUAL_ENTRY = '\0terminal-responsiveness-fixture';
const VIRTUAL_BRIDGE = '\0tauri-bridge-mock';

/**
 * The `@tauri-apps/api` terminal bridge, mocked only inside the fixture.
 *
 * It is the precise pair of specifiers `client/tauriTerminal.ts` imports
 * (`@tauri-apps/api/core` for `invoke`, `@tauri-apps/api/event` for `listen`),
 * so the real `XtermView` / `useTerminalStore` / `BottomTerminalPanel` run
 * unchanged against an in-page echo instead of a PTY.  Writes are echoed
 * synchronously, so the only thing between a keystroke and the character
 * appearing in the buffer is however long the page's main thread was busy.
 */
const bridgeMock = `
const listeners = new Map();
const echoLog = [];
window.__echoLog = echoLog;

function emit(name, payload) {
  const set = listeners.get(name);
  if (!set) return;
  for (const cb of Array.from(set)) cb({ event: name, payload });
}

export function listen(name, cb) {
  let set = listeners.get(name);
  if (!set) { set = new Set(); listeners.set(name, set); }
  set.add(cb);
  return Promise.resolve(() => { set.delete(cb); });
}

let nextPty = 1000;

export async function invoke(cmd, args) {
  const a = args || {};
  if (cmd === 'tauri_terminal_create') { nextPty += 1; return nextPty; }
  if (cmd === 'tauri_terminal_write') {
    const streaming = typeof window.__streaming === 'function' ? window.__streaming() : false;
    echoLog.push({ data: a.data, at: Date.now(), streaming });
    emit('terminal-data-' + a.id, a.data);
    return null;
  }
  if (cmd === 'tauri_terminal_resize' || cmd === 'tauri_terminal_close') return null;
  throw new Error('unmocked tauri command: ' + cmd);
}
`;

const fixture = `
import React from 'react';
import {createRoot} from 'react-dom/client';
import {App} from '/src/App.tsx';
import {useConsoleStore as store} from '/src/stores/useConsoleStore.ts';
import {useTerminalStore as termStore} from '/src/stores/useTerminalStore.ts';
import {terminalInstances} from '/src/components/terminal/XtermView.tsx';
import '/src/index.css';

// The app only reaches the terminal bridge when it believes it runs inside the
// desktop shell.  No invoke/listen beyond the mocked terminal commands exists,
// and nothing here talks to a host.
window.__TAURI_INTERNALS__ = window.__TAURI_INTERNALS__ || {};
window.__cancelCount = 0;

const TARGET_CHARS = ${TARGET_CHARS};
const TICK_MS = ${TICK_MS};
const SENTINEL = '${SENTINEL}';

// --- the streamed answer ----------------------------------------------------
// Complete, closed blocks only (heading, table, fenced code, list), so the cost
// of parsing and rendering the growing document is reproducible instead of
// depending on where a chunk happened to be cut mid-fence.
function buildMarkdown(target) {
  const chunks = [];
  let len = 0;
  let n = 0;
  while (len < target) {
    n += 1;
    const kind = n % 4;
    let block;
    if (kind === 0) {
      block = '## 阶段 ' + n + '\\n\\n' + ('段落 ' + n + '：' + '内容描述文字'.repeat(30) + '。').repeat(20) + '\\n\\n';
    } else if (kind === 1) {
      const rows = ['| 名称 | 状态 | 说明 |', '| --- | --- | --- |'];
      for (let r = 0; r < 50; r += 1) {
        rows.push('| item-' + n + '-' + r + ' | **完成** | 参见 [链接](https://example.com/' + n + '/' + r + ') |');
      }
      block = rows.join('\\n') + '\\n\\n';
    } else if (kind === 2) {
      const body = [];
      for (let l = 0; l < 56; l += 1) {
        body.push('  const value' + l + ' = compute(' + l + ', ' + n + '); // 步骤 ' + l + ' 的注释文字');
      }
      block = '~~~ts\\n' + body.join('\\n') + '\\n~~~\\n\\n';
    } else {
      block = '- 列表项一，含 **加粗** 与 $x^2$ 数学\\n- 列表项二，含 ~~删除线~~ 与 <br> 换行\\n- 列表项三\\n\\n';
    }
    chunks.push(block);
    len += block.length;
  }
  chunks.push('\\n\\n' + SENTINEL + '\\n');
  return chunks;
}

const chunks = buildMarkdown(TARGET_CHARS);
const fullDoc = chunks.join('');
const fullLength = fullDoc.length;

let revealed = 0;
let chunkIndex = 0;
let streaming = false;
let timer = null;
let lastTickAt = 0;

window.__tickStats = { count: 0, maxGapMs: 0 };

function applyContent(content, stillStreaming) {
  store.setState(function (s) {
    return {
      messages: s.messages.map(function (m) {
        return m.id === 'live-answer'
          ? Object.assign({}, m, {content: content, streaming: stillStreaming})
          : m;
      }),
    };
  });
}

function finishStream() {
  if (timer !== null) { clearInterval(timer); timer = null; }
  streaming = false;
  store.setState({runtimeStatus: 'idle'});
}

function tick() {
  // The gap between two ticks is how long the previous update kept the main
  // thread busy past its 40 ms slot: the cost the terminal has to share.
  const now = performance.now();
  if (lastTickAt > 0) {
    const gap = now - lastTickAt;
    if (gap > window.__tickStats.maxGapMs) window.__tickStats.maxGapMs = gap;
  }
  lastTickAt = now;
  window.__tickStats.count += 1;
  if (chunkIndex >= chunks.length) { finishStream(); return; }
  revealed = Math.min(fullLength, revealed + chunks[chunkIndex].length);
  chunkIndex += 1;
  const done = chunkIndex >= chunks.length;
  applyContent(fullDoc.slice(0, revealed), !done);
  if (done) finishStream();
}

window.__streaming = function () { return streaming; };
window.__revealed = function () { return revealed; };
window.__fullLength = function () { return fullLength; };
window.__startStream = function () {
  if (streaming) return fullLength;
  streaming = true;
  lastTickAt = 0;
  store.setState({runtimeStatus: 'running'});
  timer = setInterval(tick, TICK_MS);
  return fullLength;
};

// --- terminal probes ---------------------------------------------------------
window.__termReady = function () {
  const s = termStore.getState().sessions[0];
  if (!s) return false;
  const t = terminalInstances.get(s.id);
  return Boolean(t && t.element);
};
window.__focusTerminal = function () {
  const ta = document.querySelector('.xterm-helper-textarea');
  if (!ta) return false;
  ta.focus();
  return document.activeElement === ta;
};
window.__blurTerminal = function () {
  const ta = document.querySelector('.xterm-helper-textarea');
  if (ta) ta.blur();
  return document.activeElement !== ta;
};
window.__bufferText = function () {
  const s = termStore.getState().sessions[0];
  const t = s ? terminalInstances.get(s.id) : null;
  if (!t) return '';
  const buf = t.buffer.active;
  const lines = [];
  for (let i = 0; i < buf.length; i += 1) {
    const line = buf.getLine(i);
    lines.push(line ? line.translateToString(true) : '');
  }
  return lines.join('\\n');
};
window.__renderedText = function () {
  const rows = document.querySelectorAll('.markdown-body');
  if (rows.length === 0) return '';
  return rows[rows.length - 1].textContent || '';
};
window.__storedContent = function () {
  const m = store.getState().messages.filter(function (x) { return x.id === 'live-answer'; })[0];
  return m ? (m.content || '').length : -1;
};
window.__setRunning = function () { store.setState({runtimeStatus: 'running'}); };

// --- synthetic session -------------------------------------------------------
const prior = [];
for (let i = 0; i < 30; i += 1) {
  prior.push({id: 'p-user-' + i, type: 'user', content: '问题 ' + i, timestamp: 'Turn ' + i});
  prior.push({id: 'p-ans-' + i, type: 'assistant', content: ('回答 ' + i + '：' + '内容'.repeat(30) + '。\\n\\n').repeat(3), timestamp: 'Turn ' + i});
}
prior.push({id: 'live-answer', type: 'assistant', content: '', timestamp: 'Turn live', streaming: true});

store.setState({
  initClient: function () {},
  pairingState: 'paired',
  connectionState: 'connected',
  workspacePath: '/sample/synapse',
  activeProjectId: 'sample',
  projects: [{project_id: 'sample', workspace_name: 'synapse', workspace_path: '/sample/synapse', git_branch: 'main'}],
  expandedProjectIds: ['sample'],
  currentSession: {project_id: 'sample', thread_id: 's0'},
  sessionTitle: '终端响应性验收',
  sessionsTotal: 1,
  sessions: [{thread_id: 's0', title: '终端响应性验收', updated_at: new Date().toISOString(), time_label: '今天'}],
  modelName: 'model-a',
  availableModels: ['model-a'],
  thinkingLevel: 'medium',
  thinkingLevels: ['medium'],
  canSetThinking: true,
  submitPrompt: async function () {},
  cancelActiveTurn: function () { window.__cancelCount += 1; },
  gitBranch: 'main',
  gitDirty: false,
  historyAvailable: false,
  historyHasMore: false,
  historyLoading: false,
  runtimeStatus: 'idle',
  messages: prior,
});
termStore.setState({open: true});

createRoot(document.getElementById('root')).render(React.createElement(App));
`;

const server = await createServer({
  configFile: false,
  envDir: false,
  root: webRoot,
  plugins: [
    react(),
    {
      name: 'terminal-responsiveness-fixture',
      // Ahead of Vite's own resolver so the two bare `@tauri-apps/api` specifiers
      // resolve to the mock instead of the real (desktop-only) modules.
      enforce: 'pre',
      configureServer(server) {
        server.middlewares.use('/terminal-responsiveness-fixture', async (_req, res) => {
          const html = await server.transformIndexHtml(
            '/terminal-responsiveness-fixture',
            '<!doctype html><html><head><meta charset="utf-8"></head><body><div id="root"></div><script type="module" src="/fixture-entry.js"></script></body></html>',
          );
          res.setHeader('Content-Type', 'text/html');
          res.end(html);
        });
      },
      resolveId(id) {
        if (id === '/fixture-entry.js') return VIRTUAL_ENTRY;
        if (id === '@tauri-apps/api/core' || id === '@tauri-apps/api/event') return VIRTUAL_BRIDGE;
        return null;
      },
      load(id) {
        if (id === VIRTUAL_ENTRY) return fixture;
        if (id === VIRTUAL_BRIDGE) return bridgeMock;
        return null;
      },
    },
  ],
  server: { host: '127.0.0.1', port: 0 },
});

let browser;
let client;
let checks = 0;
const failures: string[] = [];

try {
  const setupAt = Date.now();
  await server.listen();
  console.log(`INFO vite server up in ${Date.now() - setupAt} ms`);
  browser = await launchBrowser();
  console.log(`INFO browser up in ${Date.now() - setupAt} ms`);
  const version = JSON.parse(
    (await httpProbe({ url: `http://127.0.0.1:${browser.port}/json/version` })).body,
  );
  client = await CdpClient.connect(version.webSocketDebuggerUrl);
  const page = await openPage(
    client,
    `${server.resolvedUrls.local[0]}terminal-responsiveness-fixture`,
  );
  const run = (expression: string) => evaluate(client!, page, expression);
  const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
  const check = (label: string, ok: boolean, detail?: string) => {
    checks += 1;
    if (ok) {
      console.log(`PASS ${label}`);
      return;
    }
    const line = detail ? `${label} -- ${detail}` : label;
    console.log(`FAIL ${line}`);
    failures.push(line);
  };
  const wait = async (expression: string, tries: number): Promise<boolean> => {
    for (let i = 0; i < tries; i += 1) {
      if (await run(expression)) return true;
      await sleep(100);
    }
    return false;
  };
  const readEchoLog = async (): Promise<Array<{ data: string; at: number; streaming: boolean }>> =>
    (await run(
      `window.__echoLog.map(function (e) { return {data: e.data, at: e.at, streaming: e.streaming}; })`,
    )) as Array<{ data: string; at: number; streaming: boolean }>;
  const sendKey = async (ch: string): Promise<void> => {
    const code = `Key${ch.toUpperCase()}`;
    const vk = ch.toUpperCase().charCodeAt(0);
    await client!.send(
      'Input.dispatchKeyEvent',
      { type: 'keyDown', key: ch, code, text: ch, unmodifiedText: ch, windowsVirtualKeyCode: vk },
      page.sessionId,
    );
    await client!.send(
      'Input.dispatchKeyEvent',
      { type: 'keyUp', key: ch, code, windowsVirtualKeyCode: vk },
      page.sessionId,
    );
  };
  const sendCtrlC = async (): Promise<void> => {
    await client!.send(
      'Input.dispatchKeyEvent',
      { type: 'rawKeyDown', key: 'c', code: 'KeyC', windowsVirtualKeyCode: 67, modifiers: 2 },
      page.sessionId,
    );
    await client!.send(
      'Input.dispatchKeyEvent',
      { type: 'keyUp', key: 'c', code: 'KeyC', windowsVirtualKeyCode: 67, modifiers: 2 },
      page.sessionId,
    );
  };

  await client.send(
    'Emulation.setDeviceMetricsOverride',
    { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false },
    page.sessionId,
  );

  // --- the terminal is really mounted, with the mocked PTY behind it ---------
  const ready = await wait(`window.__termReady && window.__termReady()`, 300);
  assert.equal(ready, true, 'the terminal never mounted in the fixture');
  console.log(`INFO app + terminal ready in ${Date.now() - setupAt} ms`);
  check('the terminal textarea takes focus', (await run(`window.__focusTerminal()`)) === true);

  // --- idle floor: what a keystroke costs with nothing else running ----------
  await run(`window.__echoLog.length = 0`);
  const idleSent: Array<{ char: string; at: number }> = [];
  for (let i = 0; i < 4; i += 1) {
    const ch = String.fromCharCode(97 + i);
    const at = Date.now();
    await sendKey(ch);
    idleSent.push({ char: ch, at });
    await sleep(30);
  }
  const idleEcho = await readEchoLog();
  const idleLatencies = idleEcho.map((e, i) => e.at - (idleSent[i]?.at ?? e.at));
  const idleMax = idleLatencies.length > 0 ? Math.max(...idleLatencies) : -1;
  console.log(`INFO idle echo latency (ms): ${idleLatencies.join(', ')}`);
  check('the terminal echoes input at rest', idleEcho.length >= 3, `echoes ${idleEcho.length}`);

  // --- amplify the per-update CPU cost --------------------------------------
  await client.send('Emulation.setCPUThrottlingRate', { rate: CPU_THROTTLE }, page.sessionId);

  // --- stream a long answer and keep typing for as long as it runs -----------
  await run(`window.__echoLog.length = 0`);
  await run(`window.__focusTerminal()`);
  const streamStartAt = Date.now();
  const fullLength = (await run(`window.__startStream()`)) as number;
  const alphabet = 'abcdefghijklmnopqrstuvwxyz';
  const sent: Array<{ char: string; at: number; sentMs: number }> = [];
  let streamEnded = false;
  let sawStreamingContent = false;
  let sendMsMax = 0;
  while (
    sent.length < MAX_KEYS &&
    Date.now() - streamStartAt < STREAM_GUARD_MS &&
    !streamEnded
  ) {
    const ch = alphabet[sent.length % alphabet.length];
    const at = Date.now();
    await sendKey(ch);
    const sentMs = Date.now() - at;
    if (sentMs > sendMsMax) sendMsMax = sentMs;
    sent.push({ char: ch, at, sentMs });
    if (sent.length % 10 === 0) {
      streamEnded = (await run(`window.__streaming() === false`)) === true;
      if (!streamEnded && !sawStreamingContent) {
        sawStreamingContent = (await run(`window.__streaming() &&
          window.__renderedText().length > 0`)) === true;
      }
    }
    if (!streamEnded) await sleep(KEY_GAP_MS);
  }
  const inputLoopEndAt = Date.now();
  if (!streamEnded) streamEnded = await wait(`window.__streaming() === false`, 3000);
  const streamEndAt = Date.now();
  const streamWallMs = streamEndAt - streamStartAt;
  const tickStats = (await run(
    `({count: window.__tickStats.count, maxGapMs: Math.round(window.__tickStats.maxGapMs)})`,
  )) as { count: number; maxGapMs: number };

  stamp('stream phase done');
  const echoLog = await readEchoLog();
  const typed = sent.map((s) => s.char).join('');
  const echoed = echoLog.map((e) => e.data).join('');
  const pairCount = Math.min(sent.length, echoLog.length);
  const latencies: number[] = [];
  for (let i = 0; i < pairCount; i += 1) latencies.push(echoLog[i]!.at - sent[i]!.at);
  const sorted = [...latencies].sort((a, b) => a - b);
  const quantile = (q: number): number => {
    if (sorted.length === 0) return -1;
    return sorted[Math.min(sorted.length - 1, Math.max(0, Math.ceil(q * sorted.length) - 1))]!;
  };
  const maxLatency = sorted.length > 0 ? sorted[sorted.length - 1]! : -1;
  const echoDuringStream = echoLog.filter((e) => e.streaming).length;

  console.log(
    `INFO streamed answer: ${fullLength} chars in ${tickStats.count} updates over ${streamWallMs} ms ` +
      `(${TICK_MS} ms slot; worst main-thread slot overrun ${tickStats.maxGapMs} ms)`,
  );
  console.log(`INFO typed ${sent.length} keys, echoed ${echoLog.length}, echoed during stream ${echoDuringStream}, worst CDP key dispatch ${sendMsMax} ms`);
  console.log(
    `INFO echo latency (ms): idle max ${idleMax}, during stream p50 ${quantile(0.5)}, ` +
      `p95 ${quantile(0.95)}, max ${maxLatency}`,
  );
  console.log(`INFO input loop ended at +${inputLoopEndAt - streamStartAt} ms, stream ended at +${streamWallMs} ms`);
  console.log(`INFO terminal buffer tail: ${JSON.stringify((await run(`window.__bufferText()`)).toString().slice(-120))}`);

  // --- neither surface can starve the other ---------------------------------
  check('the streamed answer is long', fullLength > 50_000, `fullLength ${fullLength}`);
  check('the stream ran long enough to matter', streamWallMs > 1_500, `streamWallMs ${streamWallMs}`);
  check('the stream actually completed', streamEnded === true, `streaming still true after ${streamWallMs} ms`);
  check(
    'every keystroke echoed back, in order',
    echoed === typed,
    `typed ${JSON.stringify(typed)} echoed ${JSON.stringify(echoed)}`,
  );
  check(
    'input kept echoing while the stream was still running',
    echoDuringStream >= Math.ceil(KEY_COUNT_MIN * 0.25) && echoDuringStream >= Math.ceil(sent.length * 0.25),
    `echoed during stream ${echoDuringStream}/${sent.length}`,
  );
  check(
    'the worst echo latency stays well inside the stream (no wait-for-the-turn)',
    maxLatency >= 0 && maxLatency <= streamWallMs * 0.5,
    `max ${maxLatency} ms vs stream ${streamWallMs} ms (p95 ${quantile(0.95)} ms)`,
  );

  check('the answer paints while the stream is still running', sawStreamingContent);
  check(
    'terminal input stays responsive during chat rendering',
    quantile(0.95) <= 400 && maxLatency <= 800,
    `p95 ${quantile(0.95)} ms / max ${maxLatency} ms (budget 400 / 800 ms at 4x CPU)`,
  );

  // A worker response is asynchronous: wait for the final paint, not just the
  // store's final delta. The separate assertion above forbids hiding all work
  // until settlement in order to pass the terminal-latency check.
  await wait(`window.__renderedText().includes('${SENTINEL}')`, 100);
  const stored = (await run(`window.__storedContent()`)) as number;
  const rendered = (await run(`window.__renderedText()`)) as string;
  check('the store holds the complete answer', stored === fullLength, `stored ${stored} full ${fullLength}`);
  check(
    'the transcript paints the complete answer',
    rendered.includes(SENTINEL),
    `sentinel present: ${rendered.includes(SENTINEL)} (rendered ${rendered.length} chars)`,
  );

  stamp('stream checks done');
  // --- Ctrl+C belongs to the terminal, not the turn-cancel shortcut ----------
  // `App` installs a window-level Ctrl+C handler that cancels the running turn
  // whenever `runtimeStatus === 'running'`, so a Ctrl+C meant for the shell must
  // not reach it.  The check is two-sided: the chord must reach the terminal (the
  // PTY receives ETX) *and* leave `cancelActiveTurn` untouched, while the same
  // chord outside the terminal still cancels the turn.
  await run(`window.__setRunning()`);
  await run(`window.__echoLog.length = 0`);
  await run(`window.__cancelCount = 0`);
  await run(`window.__focusTerminal()`);
  await sendCtrlC();
  await sleep(250);
  const ctrlEcho = await readEchoLog();
  const cancelInTerminal = (await run(`window.__cancelCount`)) as number;
  check(
    'Ctrl+C reaches the terminal as ETX',
    ctrlEcho.some((e) => e.data.includes('\u0003')),
    `echoes ${JSON.stringify(ctrlEcho.map((e) => e.data))}`,
  );
  check(
    'Ctrl+C in the terminal does not cancel the running turn',
    cancelInTerminal === 0,
    `cancelActiveTurn called ${cancelInTerminal} time(s)`,
  );
  await run(`window.__cancelCount = 0`);
  await run(`window.__blurTerminal()`);
  await sendCtrlC();
  await sleep(250);
  const cancelOutside = (await run(`window.__cancelCount`)) as number;
  check(
    'Ctrl+C outside the terminal still cancels the running turn',
    cancelOutside >= 1,
    `cancelActiveTurn called ${cancelOutside} time(s)`,
  );

  stamp('ctrl+c checks done');
  if (failures.length > 0) {
    throw new Error(`${failures.length} of ${checks} checks failed:\n- ${failures.join('\n- ')}`);
  }
  console.log(`ALL ${checks} CHECKS PASSED`);
} finally {
  const teardownAt = Date.now();
  client?.close();
  if (browser) await closeBrowser(browser);
  console.log(`INFO browser closed at +${Date.now() - teardownAt} ms`);
  await server.close();
  console.log(`INFO server closed at +${Date.now() - teardownAt} ms`);
  stamp('teardown done');
}
