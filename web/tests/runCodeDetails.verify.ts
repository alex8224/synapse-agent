/**
 * Real-browser acceptance for the `run_code` detail card in a tool batch: synthetic
 * state only (no host, daemon or user config).  It mounts `ToolGroupRow` directly and
 * drives its own `onToggleSubagent` callback, the one that flips `subagentExpansions`.
 * Run from web/: node --test tests/runCodeDetails.verify.ts
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { CdpClient, launchBrowser, closeBrowser, openPage, evaluate } from './helpers/cdp.ts';
import { httpProbe } from './helpers/httpProbe.ts';

const webRoot = path.resolve(import.meta.dirname, '..');
const output = path.resolve(webRoot, '..', '.tmp', 'run-code-details');
fs.mkdirSync(output, { recursive: true });
process.env.TEMP = output;
process.env.TMP = output;

const fixture = `
import React from 'react';
import {createRoot} from 'react-dom/client';
import {ToolGroupRow} from '/src/components/transcriptRows/ToolGroupRow.tsx';
import {runCodeFailureReason} from '/src/stores/transcriptLabels.ts';
import '/src/index.css';

const NL = String.fromCharCode(10);
const BS = String.fromCharCode(92);
const PYTHON = 'result = await tools.call("read_file", {})' + NL + 'return result';
const PYTHON_LIVE = 'await tools.read_file(file_path="/a.py")' + NL + 'return 1';
const OK_OUTPUT = JSON.stringify({logs:['hello'], value:null, error:null});
const EXPECTED_OUTPUT = JSON.stringify(JSON.parse(OK_OUTPUT), null, 2);
const FAIL_OUTPUT = JSON.stringify({logs:[], value:null, error:{kind:'timeout', message:'expired'}});
const TRUNCATED = '{"logs":["partial';
// A live batch carries only the bounded repr; the backslash-n must decode back to a newline.
const livePreview = "{'code': 'await tools.read_file(file_path=" + JSON.stringify('/a.py') + ")" + BS + "nreturn 1', 'intent': 'read'}";

const runCode = (over) => Object.assign({id:'rc', callId:'rc', name:'run_code', label:'统计日志', category:'other', path:null, status:'completed', preview:null, error:false, args:null, argsPreview:null, sub:false, parentId:null, subagentStatus:null, subagentName:null, icon:'code', duration:'done'}, over);
const child = () => ({id:'c1', callId:'c1', name:'read_file', label:'读取 /a.py', category:'read', path:'/a.py', status:'completed', preview:'print(1)', error:false, args:{file_path:'/a.py'}, sub:true, parentId:'rc', subagentStatus:null, subagentName:null, icon:'code', duration:'done'});
const SCENARIOS = {
  completedWithChild: () => [runCode({preview:OK_OUTPUT, args:{code:PYTHON}}), child()],
  completedNoChild: () => [runCode({preview:OK_OUTPUT, args:{code:PYTHON}})],
  running: () => [runCode({status:'running', args:null, argsPreview:livePreview})],
  failed: () => [runCode({status:'ok', error:false, preview:FAIL_OUTPUT, args:{code:PYTHON}})],
  truncated: () => [runCode({preview:TRUNCATED, args:{code:PYTHON}})],
};

let current = SCENARIOS.completedWithChild();

function Harness() {
  const [tools, setTools] = React.useState(current);
  const [subagentExpansions, setSubagentExpansions] = React.useState({});
  const actions = React.useMemo(() => ({
    onToggleExpand: () => {},
    onToggleTool: () => {},
    // The one callback under test: opening a run_code card flips its expansion key.
    onToggleSubagent: (_id, key) => setSubagentExpansions((prev) => Object.assign({}, prev, {[key]: !prev[key]})),
    onReviewFile: () => {},
    onRevertFile: () => {},
    onFork: () => {},
  }), []);
  React.useEffect(() => {
    window.__mountScenario = (name) => { current = SCENARIOS[name](); setSubagentExpansions({}); setTools(current.slice()); };
    window.__patchRunCode = (patch) => {
      current = current.map((t) => (t.name === 'run_code' ? Object.assign({}, t, patch) : t));
      setTools(current.slice());
    };
  }, []);
  const message = React.useMemo(() => ({id:'m1', type:'tool_group', timestamp:'10:00', turnId:'t1', tools}), [tools]);
  return React.createElement(ToolGroupRow, {message, toolExpansions:{}, subagentExpansions, actions});
}
createRoot(document.getElementById('root')).render(React.createElement(Harness));

window.__PYTHON = PYTHON; window.__PYTHON_LIVE = PYTHON_LIVE; window.__OK_OUTPUT = OK_OUTPUT;
window.__EXPECTED_OUTPUT = EXPECTED_OUTPUT; window.__TRUNCATED = TRUNCATED;
window.__EXPECTED_REASON = runCodeFailureReason(FAIL_OUTPUT);
window.__toggleCard = () => {
  const b = document.querySelector('button[title="展开代码、输出与调用步骤"]') || document.querySelector('button[title="收起代码、输出与调用步骤"]');
  if (b === null) return false; b.click(); return true;
};
window.__probe = () => {
  const b = document.querySelector('button[title="展开代码、输出与调用步骤"]') || document.querySelector('button[title="收起代码、输出与调用步骤"]');
  if (b === null) return {card:false};
  const card = b.parentElement;
  const sec = (label) => card.querySelector('[aria-label="' + label + '"]');
  const input = sec('run_code 输入代码'), output = sec('run_code 执行输出');
  const inCode = input && input.querySelector('pre code'), outCode = output && output.querySelector('pre code');
  const spans = (el, cls) => el ? el.querySelectorAll('span.' + cls).length : 0;
  const pill = [...card.querySelectorAll('span')].find((s) => /bg-(red-100|blue-50|green-100)/.test(s.className));
  const reason = card.querySelector('div.text-red-600');
  return {card:true, expanded: b.getAttribute('aria-expanded') === 'true', codeBlocks: card.querySelectorAll('pre code').length,
    hasInput: input !== null, hasOutput: output !== null, inputText: inCode ? inCode.textContent : null,
    outputText: outCode ? outCode.textContent : null, inputKeyword: spans(inCode, 'text-violet-700'),
    inputString: spans(inCode, 'text-emerald-700'), outputKeyword: spans(outCode, 'text-violet-700'),
    outputString: spans(outCode, 'text-emerald-700'), nested: (card.textContent || '').includes('嵌套工具调用'),
    pill: pill ? (pill.textContent || '').trim() : null, reason: reason ? (reason.textContent || '').trim() : null,
    text: (card.textContent || '').trim()};
};
`;

const server = await createServer({
  configFile: false, envDir: false, root: webRoot,
  plugins: [react(), {
    name: 'run-code-details-fixture',
    configureServer(server) {
      server.middlewares.use('/run-code-details-fixture', async (_req, res) => {
        const html = await server.transformIndexHtml('/run-code-details-fixture',
          '<!doctype html><html><head><meta charset="utf-8"></head><body><div id="root"></div><script type="module" src="/run-code-entry.js"></script></body></html>');
        res.setHeader('Content-Type', 'text/html'); res.end(html);
      });
    },
    resolveId(id) { if (id === '/run-code-entry.js') return '\0run-code-details-fixture'; },
    load(id) { if (id === '\0run-code-details-fixture') return fixture; },
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
  const page = await openPage(client, `${server.resolvedUrls.local[0]}run-code-details-fixture`);
  const run = (expression: string) => evaluate(client!, page, expression);
  const settle = () => new Promise((r) => setTimeout(r, 250));
  const check = async (label: string, expression: string) => {
    assert.equal(await run(expression), true, label); checks += 1; console.log(`PASS ${label}`);
  };
  const open = async (name: string) => { await run(`window.__mountScenario('${name}')`); await settle(); await run('window.__toggleCard()'); await settle(); };
  await client.send('Emulation.setDeviceMetricsOverride', { width: 1000, height: 800, deviceScaleFactor: 1, mobile: false }, page.sessionId);
  for (let i = 0; i < 100; i += 1) { if (await run(`typeof window.__probe === 'function' && window.__probe().card === true`)) break; await settle(); }

  await check('runCodeFailureReason decodes a JSON error envelope', `window.__EXPECTED_REASON === 'timeout: expired'`);

  // --- a completed call with a nested step ---------------------------------
  await run(`window.__mountScenario('completedWithChild')`); await settle();
  await check('a collapsed run_code card paints no input or output code', `(() => { const p = window.__probe(); return p.expanded === false && p.codeBlocks === 0 && !p.hasInput && !p.hasOutput; })()`);
  await run('window.__toggleCard()'); await settle();
  await check('an open card shows the exact Python input and pretty JSON output', `(() => { const p = window.__probe(); return p.inputText === window.__PYTHON && p.outputText === window.__EXPECTED_OUTPUT; })()`);
  await check('input and output are tokenized into keyword and string spans', `(() => { const p = window.__probe(); return p.inputKeyword > 0 && p.inputString > 0 && p.outputKeyword > 0 && p.outputString > 0; })()`);
  await check('the nested call the sandbox made is shown', `(() => { const p = window.__probe(); return p.nested === true && p.text.includes('read_file'); })()`);
  const shot = (await client.send('Page.captureScreenshot', { format: 'png' }, page.sessionId)) as { data: string };
  fs.writeFileSync(path.join(output, 'run-code-expanded.png'), Buffer.from(shot.data, 'base64'));
  await run('window.__toggleCard()'); await settle();
  await check('collapsing the card unmounts both code blocks', `(() => { const p = window.__probe(); return p.expanded === false && p.codeBlocks === 0; })()`);

  // --- a completed call with no nested step --------------------------------
  await open('completedNoChild');
  await check('a run_code call with no steps still shows parent input and output', `(() => { const p = window.__probe(); return p.nested === false && p.inputText === window.__PYTHON && p.outputText === window.__EXPECTED_OUTPUT; })()`);

  // --- a call still in flight ----------------------------------------------
  await run(`window.__mountScenario('running')`); await settle();
  await check('a running card keeps its body unmounted while collapsed', `window.__probe().codeBlocks === 0`);
  await run('window.__toggleCard()'); await settle();
  await check('a running card shows the live argsPreview input', `window.__probe().inputText === window.__PYTHON_LIVE`);
  await check('a running card waits for output instead of inventing it', `(() => { const p = window.__probe(); return p.outputText === null && p.text.includes('执行中，等待输出…'); })()`);
  await run(`window.__patchRunCode({preview: window.__OK_OUTPUT, status: 'completed'})`); await settle();
  await check('a live output update paints the JSON without collapsing the card', `(() => { const p = window.__probe(); return p.expanded === true && p.inputText === window.__PYTHON_LIVE && p.outputText === window.__EXPECTED_OUTPUT; })()`);

  // --- a failure envelope whose status lies --------------------------------
  await open('failed');
  await check('a failed envelope turns red even when status is ok and error is false', `window.__probe().pill === '失败'`);
  console.log('Failed-card probe', await run('window.__probe()'));
  await check('the failed envelope reason is visible in the card', `(() => { const p = window.__probe(); return window.__EXPECTED_REASON !== '' && p.text.includes(window.__EXPECTED_REASON); })()`);

  // --- a truncated JSON output ---------------------------------------------
  await open('truncated');
  await check('a truncated JSON output stays visible verbatim', `window.__probe().outputText === window.__TRUNCATED`);

  console.log(`ALL ${checks} CHECKS PASSED; screenshots: ${output}`);
} finally {
  client?.close();
  if (browser) await closeBrowser(browser);
  await server.close();
}
