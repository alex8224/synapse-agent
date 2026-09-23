/**
 * Offline browser acceptance for the diagram card and its viewer.
 *
 * Run from web/: node tests/mermaidViewer.verify.ts
 *
 * The requirement is that a large diagram can actually be seen.  mermaid pins
 * its own SVG to the container width (`width="100%"` plus an inline
 * `max-width`, which no stylesheet rule can outrank), so the card has to freeze
 * it back to the diagram's own `viewBox` size -- these checks measure the real
 * DOM to prove it did:
 *
 * - a wide diagram is drawn at its own width once the card is switched to 1:1,
 *   and the stage scrolls to it (before the fix it could never scroll, because
 *   the drawing was always exactly as wide as the stage);
 * - a tall diagram is bounded by the stage's height cap and scrolls inside the
 *   card, instead of stretching the transcript row to the drawing's height;
 * - the viewer opens at 75% or more of the diagram's own pixels, zooms by 5
 *   percentage points per wheel notch at the pointer, reaches 1:1 from the
 *   keyboard, and dismisses on Escape;
 * - while it is open, every `id` in the document is still unique (the enlarged
 *   copy is retargeted, so the two mermaid `<style>` blocks cannot collide).
 *
 * The fixture draws both diagrams through the real component, so mermaid really
 * runs and the sizes are facts of the page rather than numbers in the fixture.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { CdpClient, launchBrowser, closeBrowser, openPage, evaluate } from './helpers/cdp.ts';
import { httpProbe } from './helpers/httpProbe.ts';

const webRoot = path.resolve(import.meta.dirname, '..');
const output = path.resolve(webRoot, '..', '.tmp', 'mermaid-viewer');
fs.mkdirSync(output, { recursive: true });
// Keep even the browser's throwaway profile inside the workspace.
process.env.TEMP = output;
process.env.TMP = output;

/** The card's stage caps its height at `max-h-[28rem]`. */
const STAGE_CAP = 28 * 16;
/** The viewer's opening floor, from `imageZoom.ts`. */
const FLOOR = 0.75;

const fixture = `
import React from 'react';
import {createRoot} from 'react-dom/client';
import {MermaidBlock} from '/src/components/MermaidBlock.tsx';
import '/src/index.css';

// Twelve boxes left to right: far wider than the 720px card, and wide enough
// that "fit to the card" and "its own size" are visibly different numbers.
const WIDE = ['graph LR']
  .concat(Array.from({length: 12}, (_, i) => '  N' + i + '[阶段 ' + (i + 1) + ' 的处理步骤]'))
  .concat(Array.from({length: 11}, (_, i) => '  N' + i + ' --> N' + (i + 1)))
  .join('\\n');

// Eight participants and a long message stream: the tall case, the one that
// used to make a single transcript row as tall as the whole drawing.
const TALL = ['sequenceDiagram', '  participant U as 用户', '  participant A as 主 Agent',
  '  participant R as Runtime Service', '  participant W as Workflow Manager',
  '  participant P as Python 脚本', '  participant S as 调度器', '  participant G as Agent Runtime',
  '  participant J as Journal']
  .concat(Array.from({length: 24}, (_, i) =>
    '  ' + (i % 4 === 0 ? 'U->>A' : i % 4 === 1 ? 'A->>R' : i % 4 === 2 ? 'R->>W' : 'W->>J') +
    ': 第 ' + (i + 1) + ' 步：保存节点与输入校验'))
  .join('\\n');

const App = () => React.createElement('div', null,
  React.createElement('div', {id: 'wide', style: {width: '720px'}},
    React.createElement(MermaidBlock, {code: WIDE, streaming: false})),
  React.createElement('div', {id: 'tall', style: {width: '720px'}},
    React.createElement(MermaidBlock, {code: TALL, streaming: false})),
);
createRoot(document.getElementById('root')).render(React.createElement(App));
`;

const server = await createServer({
  configFile: false, envDir: false, root: webRoot,
  // Pre-bundle mermaid: discovering it at runtime would reload the page in the
  // middle of the checks (mermaid is only imported dynamically by the card).
  optimizeDeps: { include: ['mermaid'] },
  plugins: [react(), {
    name: 'mermaid-viewer-fixture',
    configureServer(server) {
      server.middlewares.use('/mermaid-viewer-fixture', async (_req, res) => {
        const html = await server.transformIndexHtml('/mermaid-viewer-fixture',
          '<!doctype html><html><head><meta charset="utf-8"></head><body><div id="root"></div><script type="module" src="/fixture-entry.js"></script></body></html>');
        res.setHeader('Content-Type', 'text/html'); res.end(html);
      });
    },
    resolveId(id) { if (id === '/fixture-entry.js') return '\0mermaid-viewer-fixture'; },
    load(id) { if (id === '\0mermaid-viewer-fixture') return fixture; },
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
  const page = await openPage(client, `${server.resolvedUrls.local[0]}mermaid-viewer-fixture`);
  const run = (expression: string) => evaluate(client!, page, expression);
  const settle = (ms = 150) => new Promise(resolve => setTimeout(resolve, ms));
  const check = async (label: string, expression: string) => {
    assert.equal(await run(expression), true, label); checks++; console.log(`PASS ${label}`);
  };
  const wait = async (expression: string) => {
    for (let i = 0; i < 300; i++) { if (await run(expression)) return; await settle(); }
    throw new Error(`fixture not ready: ${expression}`);
  };
  // Every selector is null-safe: the first polls run before React has mounted
  // (and while vite is still pre-bundling mermaid, which reloads the page).
  const card = (id: string) => `document.querySelector('#${id}')`;
  const stage = (id: string) => `${card(id)}?.querySelector('[data-diagram-stage]')`;
  const svg = (id: string) => `${stage(id)}?.querySelector('.mermaid-diagram svg')`;
  const naturalWidth = (id: string) => `parseFloat(${svg(id)}.getAttribute('width'))`;
  const naturalHeight = (id: string) => `parseFloat(${svg(id)}.getAttribute('height'))`;
  const renderedWidth = (id: string) => `${svg(id)}.getBoundingClientRect().width`;
  const zoomStage = `document.querySelector('[data-mermaid-stage]')`;
  const scale = `parseFloat(${zoomStage}?.dataset.mermaidScale)`;
  const zoomBox = `document.querySelector('.mermaid-zoom-box')`;
  // While the viewer is open it holds the only copy of the diagram.
  const zoomSvg = `document.querySelector('.mermaid-zoom-box svg')`;
  const zoomNaturalWidth = `parseFloat(${zoomSvg}?.getAttribute('width'))`;

  await client.send('Emulation.setDeviceMetricsOverride',
    {width: 1920, height: 1080, deviceScaleFactor: 1, mobile: false}, page.sessionId);
  // Both diagrams are drawn by the real mermaid runtime, asynchronously.
  await wait(`document.querySelector('#root')?.childElementCount > 0`);
  await wait(`!!${svg('wide')} && !!${svg('tall')}`);
  await wait(`document.getAnimations().every((a) => a.playState !== 'running')`);
  await settle();

  // --- the SVG carries its own size, not mermaid's container-width sizing -----
  await check('the frozen SVG states the diagram own width',
    `Number.isFinite(${naturalWidth('wide')}) && ${naturalWidth('wide')} > 500`);
  await check('and its own height',
    `Number.isFinite(${naturalHeight('wide')}) && ${naturalHeight('wide')} > 0`);
  await check('the inline max-width that pinned it to the column is gone',
    `!/max-width/.test(${svg('wide')}.getAttribute('style') || '')`);
  await check('the wide diagram really is wider than the card',
    `${naturalWidth('wide')} > ${stage('wide')}.clientWidth + 100`);

  // --- fit (the default) still shows a fitting diagram whole -------------------
  await check('by default the wide diagram is fitted to the card, not clipped',
    `${renderedWidth('wide')} <= ${stage('wide')}.clientWidth + 1`);
  await check('and the card never upscales a diagram',
    `${renderedWidth('wide')} <= ${naturalWidth('wide')} + 1`);

  // --- 1:1: the drawing is now as wide as it says it is, and scrolls ----------
  await run(`${card('wide')}.querySelector('[data-diagram-actual]').click()`);
  await settle();
  await check('1:1 draws the wide diagram at its own width',
    `Math.abs(${renderedWidth('wide')} - ${naturalWidth('wide')}) < 1.5`);
  await check('so the stage scrolls to it instead of shrinking it',
    `${stage('wide')}.scrollWidth > ${stage('wide')}.clientWidth + 100`);
  await run(`${card('wide')}.querySelector('[data-diagram-actual]').click()`);
  await settle();
  await check('switching back re-fits the diagram to the card',
    `${renderedWidth('wide')} <= ${stage('wide')}.clientWidth + 1`);

  // --- the tall diagram is bounded by the stage, not by the row ---------------
  await check('a tall diagram is capped at the stage height',
    `${stage('tall')}.clientHeight <= ${STAGE_CAP} + 1`);
  await check('the cap is what bounds it: fitted, the drawing is taller than the cap',
    `${naturalHeight('tall')} * (${stage('tall')}.clientWidth / ${naturalWidth('tall')}) > ${STAGE_CAP} + 20`);
  await check('and it scrolls inside the card',
    `${stage('tall')}.scrollHeight > ${stage('tall')}.clientHeight + 1`);
  await check('the card reports the size it drew',
    `!!${card('tall')}.querySelector('[data-diagram-size]')`);

  // --- the viewer ---------------------------------------------------------------
  const stageHeightBefore = await run(`${stage('tall')}.clientHeight`) as number;
  await run(`${card('tall')}.querySelector('[data-diagram-zoom]').click()`);
  await wait(`!!${zoomStage} && !!${zoomBox}`);
  await wait(`document.getAnimations().every((a) => a.playState !== 'running')`);
  await settle();
  await check('the viewer opens', `!!document.querySelector('[role="dialog"][aria-modal="true"]')`);
  await check('the diagram is now rendered once, in the viewer', `!${svg('tall')} && !!${zoomSvg}`);
  await check('it opens at 75% or more of the diagram own pixels', `${scale} >= ${FLOOR} - 1e-6`);
  await check('and never above 1:1', `${scale} <= 1 + 1e-6`);
  await check('the enlarged copy is drawn at that scale',
    `Math.abs(${zoomBox}.getBoundingClientRect().width - ${zoomNaturalWidth} * ${scale}) < 1.5`);
  await check('the tall diagram does not fit, so it can be panned',
    `${zoomBox}.getBoundingClientRect().height > ${zoomStage}.clientHeight + 1`);
  await check('every id in the document is still unique',
    `(() => { const ids = [...document.querySelectorAll('[id]')].map((n) => n.id);
              return new Set(ids).size === ids.length; })()`);
  // A sequence diagram carries `actor0`...`root-7` besides its own root id, so
  // the check above is only meaningful while exactly one copy is in the DOM.
  await check('and the diagram under test really does carry inner ids',
    `${zoomSvg}.querySelectorAll('[id]').length >= 8`);

  const rect = await run(`(() => { const r = ${zoomStage}.getBoundingClientRect(); return {x: r.left + r.width * 0.75, y: r.top + r.height * 0.25}; })()`) as { x: number; y: number };
  const wheel = async (deltaY: number) => {
    await client!.send('Input.dispatchMouseEvent',
      {type:'mouseWheel', x: Math.round(rect.x), y: Math.round(rect.y), deltaX:0, deltaY, button:'none'},
      page.sessionId);
    await settle();
  };
  const before = await run(scale) as number;
  await wheel(-120);
  await check('a wheel notch up zooms in', `${await run(scale)} > ${before}`);
  await check('and it is exactly one 5 percentage point step',
    `Math.abs(${await run(scale)} - (${before} + 0.05)) < 1e-6`);

  const key = async (keyName: string, code: string, vk: number) => {
    await client!.send('Input.dispatchKeyEvent',
      {type:'keyDown', key: keyName, code, windowsVirtualKeyCode: vk, text: keyName.length === 1 ? keyName : undefined},
      page.sessionId);
    await client!.send('Input.dispatchKeyEvent',
      {type:'keyUp', key: keyName, code, windowsVirtualKeyCode: vk}, page.sessionId);
    await settle();
  };
  await key('1', 'Digit1', 49);
  await check('the 1 key goes to 1:1', `Math.abs(${scale} - 1) < 1e-6`);
  await check('at 1:1 the diagram is drawn at its own size',
    `Math.abs(${zoomBox}.getBoundingClientRect().width - ${zoomNaturalWidth}) < 1.5`);
  await key('0', 'Digit0', 48);
  await check('the 0 key fits the whole diagram', `${await run(scale)} < 1`);

  const shot = (await client.send('Page.captureScreenshot', {format:'png'}, page.sessionId)) as { data: string };
  const shotPath = path.join(output, 'mermaid-viewer.png');
  fs.writeFileSync(shotPath, Buffer.from(shot.data, 'base64'));
  console.log(`screenshot     : ${shotPath}`);

  await key('Escape', 'Escape', 27);
  await check('Escape closes the viewer', `${zoomStage} === null`);
  await check('the diagram is back in the card', `!!${svg('tall')}`);
  await check('and the card is exactly as tall as it was before the viewer opened',
    `Math.abs(${stage('tall')}.clientHeight - ${stageHeightBefore}) <= 1`);

  console.log(`ALL ${checks} CHECKS PASSED`);
} finally {
  client?.close();
  if (browser) await closeBrowser(browser);
  await server.close();
}
