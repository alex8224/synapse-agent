/** Offline browser acceptance: synthetic state only, no host, daemon or credentials.
 * Run from web/: node tests/fluentAppearance.verify.ts
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { CdpClient, launchBrowser, closeBrowser, openPage, evaluate } from './helpers/cdp.ts';
import { httpProbe } from './helpers/httpProbe.ts';

const webRoot = path.resolve(import.meta.dirname, '..');
const output = path.resolve(webRoot, '..', '.tmp', 'fluent-acceptance');
fs.mkdirSync(output, { recursive: true });
// Keep even the browser's throwaway profile inside the workspace.
process.env.TEMP = output;
process.env.TMP = output;
const fixture = `
import React from 'react';
import {createRoot} from 'react-dom/client';
import {App} from '/src/App.tsx';
import {useConsoleStore as store} from '/src/stores/useConsoleStore.ts';
import {initAppearance, useAppearanceStore} from '/src/stores/appearance.ts';
import '/src/index.css';
const titles = ['Fluent 主界面适配', '检查模型与工具调用', '整理项目文档'];
store.setState({
  initClient: () => {}, pairingState: 'paired', connectionState: 'connected',
  workspacePath: '/sample/synapse', activeProjectId: 'sample',
  projects: [{project_id:'sample',workspace_name:'synapse',workspace_path:'/sample/synapse',git_branch:'main'}],
  expandedProjectIds:['sample'], currentSession:{project_id:'sample',thread_id:'s0'},
  sessionTitle:titles[0], sessionsTotal:3,
  sessions:titles.map((title,i)=>({thread_id:'s'+i,title,updated_at:new Date().toISOString(),time_label:'今天'})),
  modelName:'sample-model-with-a-long-name-for-layout', availableModels:['sample-model-with-a-long-name-for-layout','sample-small'],
  thinkingLevel:'medium',thinkingLevels:['low','medium','high'],canSetThinking:true,
  setModel:async(name)=>{store.setState({modelName:name});return true},
  setThinkingLevel:async(level)=>{store.setState({thinkingLevel:level});return true},
  submitPrompt:async()=>{}, cancelActiveTurn:async()=>{store.setState({runtimeStatus:'idle'})},
  gitBranch:'main',gitDirty:false,
  messages:[
    {id:'u1',type:'user',timestamp:'10:00',content:'让主界面使用统一的 Fluent 设计语言。'},
    {id:'a1',type:'assistant',timestamp:'10:00',content:'## 主界面预览\\n\\n保留原有功能布局，统一导航、会话列表和输入区。\\n\\n- 浅色与深色使用同一套控件\\n- 当前会话有独立的选中标记\\n- 代码与遥测保留专用字体\\n\\n这是合成验收数据，不是真实会话。'}
  ]
});
initAppearance();
window.fixtureStore = store;
window.appearanceStore = useAppearanceStore;
createRoot(document.getElementById('root')).render(React.createElement(App));
`;
const server = await createServer({
  configFile: false, envDir: false, root: webRoot,
  plugins: [react(), {
    name: 'fluent-acceptance-fixture',
    configureServer(server) {
      server.middlewares.use('/fluent-fixture', async (_req, res) => {
        const html = await server.transformIndexHtml('/fluent-fixture',
          '<!doctype html><html><head><meta charset="utf-8"></head><body><div id="root"></div><script type="module" src="/fixture-entry.js"></script></body></html>');
        res.setHeader('Content-Type', 'text/html'); res.end(html);
      });
    },
    resolveId(id) { if (id === '/fixture-entry.js') return '\0fluent-fixture'; },
    load(id) { if (id === '\0fluent-fixture') return fixture; },
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
  const page = await openPage(client, `${server.resolvedUrls.local[0]}fluent-fixture`);
  const run = (expression: string) => evaluate(client!, page, expression);
  const settle = () => new Promise(resolve => setTimeout(resolve, 250));
  const check = async (label: string, expression: string) => {
    assert.equal(await run(expression), true, label); checks++; console.log(`PASS ${label}`);
  };
  const wait = async (expression: string) => {
    for (let i=0; i<120; i++) { if (await run(expression)) return; await settle(); }
    throw new Error(`fixture not ready: ${expression}`);
  };
  const click = async (selector: string) => {
    await run(`document.querySelector(${JSON.stringify(selector)}).click()`); await settle();
  };
  const shot = async (name: string) => {
    const image = await client!.send('Page.captureScreenshot', {format:'png'}, page.sessionId) as {data:string};
    fs.writeFileSync(path.join(output, `${name}.png`), Buffer.from(image.data, 'base64'));
  };
  const viewport = async (width: number, height: number) => {
    await client!.send('Emulation.setDeviceMetricsOverride', {width,height,deviceScaleFactor:1,mobile:false}, page.sessionId); await settle();
  };
  const media = async (value: string) => {
    await client!.send('Emulation.setEmulatedMedia', {features:[{name:'prefers-color-scheme',value}]}, page.sessionId); await settle();
  };
  await wait(`!!document.querySelector('#console-composer')`);
  await viewport(1440,900);
  await media('light');
  await check('system light selects Fluent light', `document.documentElement.dataset.theme === 'fluent-light'`);
  await check('shared control height and font reach the DOM', `getComputedStyle(document.querySelector('header button')).height === '32px' && getComputedStyle(document.querySelector('header')).fontFamily.includes('Segoe UI')`);
  await check('main navigation uses bundled SVG icons', `document.querySelectorAll('nav svg').length > 5 && !document.querySelector('nav .material-symbols-outlined')`);
  await check('composer is capped and centred in the workspace', `(()=>{const r=document.querySelector('.ui-composer').getBoundingClientRect();const p=document.querySelector('main').getBoundingClientRect();return Math.abs(r.width-768)<=2&&Math.abs(r.left+r.width/2-(p.left+p.width/2))<2})()`);
  await check('the primary action is round', `getComputedStyle(document.querySelector('[aria-label="发送消息"]')).borderRadius === '9999px'`);
  await check('selection has a three-pixel leading marker', `getComputedStyle(document.querySelector('[data-selected="true"]'),'::before').width === '3px'`);
  const idleBorder = await run(`getComputedStyle(document.querySelector('.ui-composer')).borderTopColor`);
  await run(`document.querySelector('#console-composer').focus()`); await settle();
  const focusedBorder = await run(`getComputedStyle(document.querySelector('.ui-composer')).borderTopColor`);
  await check('focus moves the card border to the accent role', `${JSON.stringify(focusedBorder)} !== ${JSON.stringify(idleBorder)}`);
  await check('the focused field draws no ring of its own', `getComputedStyle(document.querySelector('#console-composer')).outlineStyle === 'none'`);
  await check('the focused card keeps its rounded corners', `getComputedStyle(document.querySelector('.ui-composer')).borderRadius === '8px'`);
  await run(`document.querySelector('#console-composer').blur()`); await settle();
  await check('empty input disables send', `document.querySelector('[aria-label="发送消息"]').disabled`);
  await shot('fluent-light');
  await media('dark');
  await check('system dark selects Fluent dark', `document.documentElement.dataset.theme === 'fluent-dark'`);
  await shot('fluent-dark');
  // Both sidebar states stay mounted; the inactive one is `inert`, so open
  // settings through the reachable trigger rather than the hidden rail copy.
  await run(`[...document.querySelectorAll('[aria-label="打开设置"]')].find((el) => !el.closest('[inert]')).click()`);
  await settle();
  await run(`[...document.querySelectorAll('[aria-label="主题"] button')].find(b=>b.textContent==='浅色').click()`); await settle();
  await check('explicit light overrides system dark', `document.documentElement.dataset.theme === 'fluent-light'`);
  await check('settings portal is centred outside the rail', `(()=>{const r=document.querySelector('[role="dialog"][aria-label="设置"]').getBoundingClientRect(); return Math.abs(r.left+r.width/2-innerWidth/2)<2})()`);
  await shot('fluent-settings');
  await click('[aria-label="关闭设置"]');
  await click('[aria-label="切换侧栏"]');
  await click('[aria-label="打开设置"]');
  await check('collapsed rail still opens settings', `!!document.querySelector('[role="dialog"][aria-label="设置"]')`);
  await click('[aria-label="关闭设置"]');
  await click('[aria-label="切换侧栏"]');
  // --- the one-click theme toggle --------------------------------------------
  // The sidebar's toggle flips the palette in one click, and it does so from a
  // "follow the system" preference too: it resolves the operating system first and
  // then stores the opposite as an explicit choice.
  await run(`window.appearanceStore.getState().setAppearance('system')`); await settle();
  await media('dark');
  await check('system dark is the starting palette',
    `document.documentElement.dataset.theme === 'fluent-dark'`);
  const themeToggle =
    `[...document.querySelectorAll('[aria-label^="切换到"]')].find((el) => !el.closest('[inert]'))`;
  await check('the toggle offers the theme it switches to',
    `${themeToggle}.getAttribute('aria-label') === '切换到浅色主题'`);
  await check('and names its chord', `${themeToggle}.title.endsWith('(Ctrl + Shift + L)')`);
  await run(`${themeToggle}.click()`); await settle();
  await check('one click flips the palette', `document.documentElement.dataset.theme === 'fluent-light'`);
  await check('the flip is stored, so the system stops overriding it',
    `window.appearanceStore.getState().appearance === 'light' && localStorage.getItem('synapse.console.appearance') === 'light'`);
  await check('the button now offers the way back',
    `${themeToggle}.getAttribute('aria-label') === '切换到深色主题'`);
  await shot('theme-toggle');
  // The same flip from the keyboard, with nothing focused: Ctrl(2) + Shift(8).
  await run(`document.activeElement.blur()`);
  await client.send('Input.dispatchKeyEvent', {type:'keyDown',key:'L',code:'KeyL',modifiers:10,text:'L',unmodifiedText:'L',windowsVirtualKeyCode:76}, page.sessionId);
  await client.send('Input.dispatchKeyEvent', {type:'keyUp',key:'L',code:'KeyL',modifiers:10,windowsVirtualKeyCode:76}, page.sessionId);
  await settle();
  await check('Ctrl+Shift+L does the same from anywhere',
    `document.documentElement.dataset.theme === 'fluent-dark' && window.appearanceStore.getState().appearance === 'dark'`);
  await run(`document.querySelector('#session-search').focus()`);
  await check('keyboard focus is visible', `getComputedStyle(document.querySelector('#session-search')).outlineWidth === '2px'`);
  await click('[aria-controls="model-picker"]');
  await check('model choices are focusable buttons', `document.querySelectorAll('#model-picker button').length === 2`);
  await run(`document.querySelectorAll('#model-picker button')[1].focus()`);
  await check('model choice receives focus', `document.activeElement === document.querySelectorAll('#model-picker button')[1]`);
  await client.send('Input.dispatchKeyEvent', {type:'keyDown',key:'Enter',code:'Enter',text:'\r',unmodifiedText:'\r',windowsVirtualKeyCode:13}, page.sessionId);
  await client.send('Input.dispatchKeyEvent', {type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13}, page.sessionId); await settle();
  await check('keyboard selection updates the model and closes picker', `window.fixtureStore.getState().modelName === 'sample-small' && !document.querySelector('#model-picker')`);
  await click('[aria-controls="thinking-picker"]');
  await run(`window.fixtureStore.setState({canSetThinking:false})`); await settle();
  await check('read-only thinking options are disabled', `[...document.querySelectorAll('#thinking-picker button')].every(b=>b.disabled)`);
  await run(`document.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}))`); await settle();
  await run(`window.fixtureStore.setState({runtimeStatus:'running'})`); await settle();
  await check('running turn has an enabled stop button', `document.querySelector('[aria-label="停止当前轮次"]').disabled === false`);
  await click('[aria-label="停止当前轮次"]');
  await run(`window.fixtureStore.setState({modelName:'a-very-long-model-name-to-exercise-the-compact-toolbar'})`);
  for (const width of [1920,900,640]) {
    await viewport(width, 900);
    await check(`no document overflow at ${width}`, `document.documentElement.scrollWidth === innerWidth`);
    await check(`composer controls fit at ${width}`, `(()=>{const c=document.querySelector('.ui-composer').getBoundingClientRect();return [...document.querySelectorAll('.ui-composer-toolbar button')].every(b=>{const r=b.getBoundingClientRect();return r.left>=c.left && r.right<=c.right})})()`);
    await check(`composer is centred on the chat column at ${width}`, `(()=>{const a=document.querySelector('main .console-column').getBoundingClientRect(),b=document.querySelector('.ui-composer').getBoundingClientRect();return Math.abs(a.left+a.width/2-(b.left+b.width/2))<2})()`);
    await check(`composer never exceeds the chat column at ${width}`, `(()=>{const a=document.querySelector('main .console-column').getBoundingClientRect(),b=document.querySelector('.ui-composer').getBoundingClientRect();return b.width<=a.width&&b.width<=768&&(a.width<=768||b.width<a.width)})()`);
    await click('[aria-controls="model-picker"]');
    await check(`model picker fits the workspace at ${width}`, `(()=>{const r=document.querySelector('#model-picker').getBoundingClientRect(),p=document.querySelector('main').getBoundingClientRect();return r.left>=p.left&&r.right<=p.right&&r.top>=p.top})()`);
    await run(`document.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}))`); await settle();
  }
  await shot('fluent-compact');
  await client.send('Emulation.setEmulatedMedia', {features:[{name:'prefers-reduced-motion',value:'reduce'}]}, page.sessionId);
  await click('[aria-controls="model-picker"]');
  await check('reduced motion disables flyout animation', `getComputedStyle(document.querySelector('#model-picker')).animationName === 'none'`);
  console.log(`ALL ${checks} CHECKS PASSED; screenshots: .tmp/fluent-acceptance/`);
} finally {
  client?.close();
  if (browser) await closeBrowser(browser);
  await server.close();
}
