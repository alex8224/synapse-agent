/**
 * Offline browser acceptance for the image preview's zoom and pan.
 *
 * Run from web/: node tests/imageZoom.verify.ts
 *
 * The requirement is that a preview never shrinks a picture into illegibility:
 * a 2200px-wide chart must open at *at least 75%* of its own pixels -- far more
 * than the 872px the panel used to cap it at -- and the reader must be able to
 * zoom and pan from there.  The fixture draws a real 2200x1350 canvas image, so
 * `naturalWidth` is a real number and "75%" is measurable in the DOM.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { CdpClient, launchBrowser, closeBrowser, openPage, evaluate } from './helpers/cdp.ts';
import { httpProbe } from './helpers/httpProbe.ts';

const webRoot = path.resolve(import.meta.dirname, '..');
const output = path.resolve(webRoot, '..', '.tmp', 'image-zoom');
fs.mkdirSync(output, { recursive: true });
// Keep even the browser's throwaway profile inside the workspace.
process.env.TEMP = output;
process.env.TMP = output;

const fixture = `
import React from 'react';
import {createRoot} from 'react-dom/client';
import {ImageLightbox} from '/src/components/ImageLightbox.tsx';
import '/src/index.css';

// A real 2200x1350 image, drawn at runtime: the natural size the preview has to
// respect is then a fact of the page, not a number in the fixture.
const canvas = document.createElement('canvas');
canvas.width = 2200; canvas.height = 1350;
const g = canvas.getContext('2d');
g.fillStyle = '#4C78A8'; g.fillRect(0, 0, 2200, 1350);
g.fillStyle = '#FFFFFF'; g.font = '150px sans-serif'; g.fillText('2200 x 1350', 90, 260);
for (let i = 0; i < 18; i += 1) {
  g.fillStyle = i % 2 ? '#F58518' : '#54A24B';
  g.fillRect(90 + i * 115, 1100 - i * 35, 70, 220 + i * 35);
}
const src = canvas.toDataURL('image/png');

window.__closed = 0;
const App = () => React.createElement(ImageLightbox, {
  src,
  label: 'token_report.png',
  meta: '2200x1350 · 138 KB',
  onClose: () => { window.__closed += 1; },
});
createRoot(document.getElementById('root')).render(React.createElement(App));
`;

const server = await createServer({
  configFile: false, envDir: false, root: webRoot,
  plugins: [react(), {
    name: 'image-zoom-fixture',
    configureServer(server) {
      server.middlewares.use('/image-zoom-fixture', async (_req, res) => {
        const html = await server.transformIndexHtml('/image-zoom-fixture',
          '<!doctype html><html><head><meta charset="utf-8"></head><body><div id="root"></div><script type="module" src="/fixture-entry.js"></script></body></html>');
        res.setHeader('Content-Type', 'text/html'); res.end(html);
      });
    },
    resolveId(id) { if (id === '/fixture-entry.js') return '\0image-zoom-fixture'; },
    load(id) { if (id === '\0image-zoom-fixture') return fixture; },
  }],
  server: { host: '127.0.0.1', port: 0 },
});

const NATURAL_WIDTH = 2200;
/** The requirement under test. */
const FLOOR = 0.75;

let browser;
let client;
let checks = 0;
try {
  await server.listen();
  browser = await launchBrowser();
  const version = JSON.parse((await httpProbe({url:`http://127.0.0.1:${browser.port}/json/version`})).body);
  client = await CdpClient.connect(version.webSocketDebuggerUrl);
  const page = await openPage(client, `${server.resolvedUrls.local[0]}image-zoom-fixture`);
  const run = (expression: string) => evaluate(client!, page, expression);
  const settle = (ms = 120) => new Promise(resolve => setTimeout(resolve, ms));
  const check = async (label: string, expression: string) => {
    assert.equal(await run(expression), true, label); checks++; console.log(`PASS ${label}`);
  };
  const wait = async (expression: string) => {
    for (let i = 0; i < 200; i++) { if (await run(expression)) return; await settle(); }
    throw new Error(`fixture not ready: ${expression}`);
  };
  const stage = `document.querySelector('[data-image-stage]')`;
  const image = `document.querySelector('[data-image-stage] img')`;
  const scale = `parseFloat(${stage}.dataset.imageScale)`;
  const renderedWidth = `${image}.getBoundingClientRect().width`;
  const offsetX = `(() => { const m = /translate\\(calc\\(-50% \\+ (-?[\\d.]+)px/.exec(${image}.style.transform); return m ? parseFloat(m[1]) : null; })()`;

  await client.send('Emulation.setDeviceMetricsOverride',
    {width:1920,height:1080,deviceScaleFactor:1,mobile:false}, page.sessionId);
  await wait(`${image}.naturalWidth === ${NATURAL_WIDTH} && ${scale} < 0.99`);
  // The panel's entrance animation scales it: measuring mid-animation would read
  // a rectangle a couple of pixels short of the style size.
  await wait(`document.getAnimations().every((a) => a.playState !== 'running')`);
  await settle();

  // --- the requirement: at least 75% of the image's own pixels -----------------
  await check('the preview opens at 75% or more of the image', `${scale} >= ${FLOOR} - 1e-6`);
  await check('the rendered image really is at least 75% wide',
    `${renderedWidth} >= ${NATURAL_WIDTH * FLOOR} - 1`);
  await check('and it is far larger than the old 872px panel cap', `${renderedWidth} > 872`);
  await check('the image either fits the stage or sits exactly on the 75% floor',
    `(() => {
       const st = ${stage};
       const img = ${image}.getBoundingClientRect();
       const fits = img.width <= st.clientWidth + 1 && img.height <= st.clientHeight + 1;
       return fits || Math.abs(${scale} - ${FLOOR}) < 1e-6;
     })()`);
  await check('the opening scale never exceeds 1:1',
    `${scale} <= 1 + 1e-6`);

  // --- wheel zoom, anchored at the pointer -------------------------------------
  const rect = await run(`(() => { const r = ${stage}.getBoundingClientRect(); return {x: r.left + r.width * 0.75, y: r.top + r.height * 0.25}; })()`) as { x: number; y: number };
  const wheel = async (deltaY: number) => {
    await client!.send('Input.dispatchMouseEvent',
      {type:'mouseWheel', x: Math.round(rect.x), y: Math.round(rect.y), deltaX:0, deltaY, button:'none'},
      page.sessionId);
    await settle();
  };
  const before = await run(scale) as number;
  await wheel(-120);
  const zoomedIn = await run(scale) as number;
  await check('a wheel notch up zooms in', `(${zoomedIn}) > ${before}`);
  await check('and it is exactly one 5 percentage point step',
    `Math.abs(${zoomedIn} - (${before} + 0.05)) < 1e-6`);
  // A trackpad sends a stream of small deltas: they must add up to a notch
  // instead of stepping on every event.
  await wheel(-20);
  await wheel(-20);
  await check('small deltas under a whole notch do not step',
    `Math.abs(${await run(scale)} - ${zoomedIn}) < 1e-6`);
  await wheel(-60);
  await check('the carried distance is spent once it reaches a notch',
    `Math.abs(${await run(scale)} - (${zoomedIn} + 0.05)) < 1e-6`);
  const peak = await run(scale) as number;
  await check('the zoomed image is now larger than the stage, so it can be panned',
    `${renderedWidth} > ${stage}.clientWidth`);
  await wheel(120);
  await wheel(20);
  await check('a wheel notch down zooms back out', `${await run(scale)} < ${peak}`);
  for (let i = 0; i < 30; i += 1) await wheel(120);
  await check('zooming out stops at the supported floor (10%)', `${await run(scale)} >= 0.1 - 1e-9`);
  await check('and it did reach that floor rather than stopping early',
    `${await run(scale)} <= 0.1 + 1e-6`);

  // --- keyboard ------------------------------------------------------------------
  const key = async (keyName: string, code: string, vk: number) => {
    await client!.send('Input.dispatchKeyEvent',
      {type:'keyDown', key: keyName, code, windowsVirtualKeyCode: vk, text: keyName.length === 1 ? keyName : undefined},
      page.sessionId);
    await client!.send('Input.dispatchKeyEvent',
      {type:'keyUp', key: keyName, code, windowsVirtualKeyCode: vk}, page.sessionId);
    await settle();
  };
  const beforeKey = await run(scale) as number;
  await key('+', 'Equal', 187);
  await check('the + key zooms in', `${await run(scale)} > ${beforeKey}`);
  await check('and the keyboard step is 5 points too',
    `Math.abs(${await run(scale)} - (${beforeKey} + 0.05)) < 1e-6`);
  await key('1', 'Digit1', 49);
  await check('the 1 key goes to 1:1', `Math.abs(${scale} - 1) < 1e-6`);
  await key('+', 'Equal', 187);
  await check('a step from 1:1 lands on 105%', `Math.abs(${await run(scale)} - 1.05) < 1e-6`);
  await key('-', 'Minus', 189);
  await check('and stepping back returns to 1:1', `Math.abs(${await run(scale)} - 1) < 1e-6`);
  await key('0', 'Digit0', 48);
  await check('the 0 key fits the whole image', `${await run(scale)} < 1`);

  // --- panning --------------------------------------------------------------------
  await key('1', 'Digit1', 49);
  await key('+', 'Equal', 187);
  await key('+', 'Equal', 187);
  const pannable = await run(`${renderedWidth} > ${stage}.clientWidth`) as boolean;
  assert.equal(pannable, true, 'the fixture must be pannable at this zoom');
  const offsetBefore = await run(offsetX) as number;
  const center = await run(`(() => { const r = ${stage}.getBoundingClientRect(); return {x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2)}; })()`) as { x: number; y: number };
  await client.send('Input.dispatchMouseEvent', {type:'mousePressed', x: center.x, y: center.y, button:'left', clickCount:1, buttons:1}, page.sessionId);
  await client.send('Input.dispatchMouseEvent', {type:'mouseMoved', x: center.x - 160, y: center.y, button:'left', buttons:1}, page.sessionId);
  await settle();
  await client.send('Input.dispatchMouseEvent', {type:'mouseReleased', x: center.x - 160, y: center.y, button:'left', buttons:0}, page.sessionId);
  await settle();
  const offsetAfter = await run(offsetX) as number;
  await check('dragging moves the image', `${offsetAfter} !== ${offsetBefore}`);
  // Drag far past the edge: the offset must stop at the pan limit, never leaving
  // a gap where the stage shows through.
  await client.send('Input.dispatchMouseEvent', {type:'mousePressed', x: center.x, y: center.y, button:'left', clickCount:1, buttons:1}, page.sessionId);
  await client.send('Input.dispatchMouseEvent', {type:'mouseMoved', x: center.x - 6000, y: center.y, button:'left', buttons:1}, page.sessionId);
  await settle();
  await client.send('Input.dispatchMouseEvent', {type:'mouseReleased', x: center.x - 6000, y: center.y, button:'left', buttons:0}, page.sessionId);
  await settle();
  await check('the image cannot be dragged out of the stage',
    `(() => {
       const img = ${image}.getBoundingClientRect();
       const st = ${stage}.getBoundingClientRect();
       return img.right >= st.right - 1 && img.left <= st.left + 1;
     })()`);

  const shot = (await client.send('Page.captureScreenshot', {format:'png'}, page.sessionId)) as { data: string };
  const shotPath = path.join(output, 'image-zoom.png');
  fs.writeFileSync(shotPath, Buffer.from(shot.data, 'base64'));
  console.log(`screenshot     : ${shotPath}`);

  // --- dismissal -------------------------------------------------------------------
  await key('Escape', 'Escape', 27);
  await check('Escape closes the preview', `window.__closed === 1`);

  console.log(`ALL ${checks} CHECKS PASSED`);
} finally {
  client?.close();
  if (browser) await closeBrowser(browser);
  await server.close();
}
