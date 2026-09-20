/**
 * Browser acceptance for the composer's action menu.
 *
 * The static guards (`composerActions.test.ts`) pin the *registry* and the card's
 * source; they cannot prove that a real browser delivers the keystrokes the menu
 * expects, that the popover really hangs above the trigger, or that a disabled
 * row is genuinely inert.  This script drives the real app against a synthetic
 * store with real mouse and key events, so no host, daemon or credential is
 * involved.
 *
 * What it has to settle, in order of how easy it is to get wrong:
 *
 *  - the `+` opens a menu of actions (not the picker directly), it opens
 *    *upward*, and it is portalled outside the composer's `<form>` so activating
 *    a row can never submit the turn,
 *  - the keyboard contract: open from the trigger, arrows walk the rows and wrap,
 *    Home / End jump to the ends, Escape closes and hands focus back, Tab
 *    dismisses without blocking the move and the editor keeps its own keys,
 *  - screenshot status crosses the real decoders and all three finished frames
 *    become composer pills; recovering a completed task requires confirmation,
 *  - the image row opens the composer's own file picker (the one `handleFiles`
 *    path), and "add project" is still its own button.
 *
 * Run from web/:
 *   $env:NODE_OPTIONS=''; $env:NODE_USE_ENV_PROXY=''; node --test tests/composerActions.verify.ts
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { CdpClient, launchBrowser, closeBrowser, openPage, evaluate } from './helpers/cdp.ts';
import type { BrowserHandle } from './helpers/cdp.ts';
import { httpProbe } from './helpers/httpProbe.ts';

const webRoot = path.resolve(import.meta.dirname, '..');
const output = path.resolve(webRoot, '..', '.tmp', 'composer-actions');
fs.mkdirSync(output, { recursive: true });
// Keep even the browser's throwaway profile inside the workspace.
process.env.TEMP = output;
process.env.TMP = output;

/**
 * Synthetic store: `submitPrompt` records what the composer produced, and the
 * file input's `click` is intercepted so "the image row opened the picker" is
 * observable without a real (headless-impossible) file dialog.
 */
const fixture = `
import React from 'react';
import {createRoot} from 'react-dom/client';
import {App} from '/src/App.tsx';
import {useConsoleStore as store} from '/src/stores/useConsoleStore.ts';
import {useScreenshotStore as screenshotStore, SCREENSHOT_IMPORT_WAIT} from '/src/stores/screenshotTask.ts';
import {parseScreenshotStatus, parseScreenshotStart, parseScreenshotToolStatus, parseScreenshotCancel}
  from '/src/runtime-client/screenshot.ts';
import '/src/index.css';

window.__submitted = [];
window.__pickerOpened = 0;
window.__screenshot = { started: 0, opened: 0, cancelled: 0, polls: 0, phase: 'running' };
window.__attachmentReads = [];
window.screenshotStore = screenshotStore;
window.SCREENSHOT_IMPORT_WAIT = SCREENSHOT_IMPORT_WAIT;
const png = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGPgEpEDAABoAD1UCKP3AAAAAElFTkSuQmCC';
const images = ['a', 'b', 'c'].map((id, i) => ({ attachment_id: id.repeat(32), name: '截图 ' + (i + 1) + '.png',
  mime: 'image/png', size: atob(png).length, revision: 'r1' }));
const nativeClick = HTMLInputElement.prototype.click;
HTMLInputElement.prototype.click = function () {
  if (this.type === 'file') { window.__pickerOpened += 1; return; }
  return nativeClick.call(this);
};
const session = {project_id:'sample',thread_id:'s0'};
const screenshotClient = {
  getScreenshotStatus: async (_session, taskId) => {
    window.__screenshot.polls += 1;
    const exists = window.__screenshot.started > 0;
    const phase = window.__screenshot.phase;
    const terminal = phase === 'completed' || phase === 'empty';
    return parseScreenshotStatus({
      session, task_id: exists ? taskId || 'task-1' : '',
      state: exists ? (terminal ? 'completed' : 'running') : 'idle',
      requested: exists ? 3 : 0, captured: exists && phase !== 'running' ? 3 : 0,
      attachments: exists && phase === 'completed' ? images : [],
      available: true, unavailable_reason: '', error_code: null, error_message: null,
    });
  },
  startScreenshotCapture: async () => {
    window.__screenshot.started += 1;
    return parseScreenshotStart({ session, task_id: 'task-1', state: 'queued', requested: 3, settings: {} });
  },
  openScreenshotSettings: async () => {
    window.__screenshot.opened += 1;
    return parseScreenshotToolStatus({ available: true, platform: 'win32', reason: '', version: null, busy: false, active_task_id: null });
  },
  cancelScreenshotCapture: async (_session, taskId) => {
    window.__screenshot.cancelled += 1;
    return parseScreenshotCancel({ session, task_id: taskId, state: 'cancelled', cancelled: true });
  },
  readAttachment: async (_session, attachmentId, offset = 0) => {
    window.__attachmentReads.push(attachmentId + '@' + offset);
    // A slow, chunked read: two windows with a delay each, so a re-render storm
    // during the read is observable (a second read would double the count).
    await new Promise((resolve) => setTimeout(resolve, 40));
    const bytes = atob(png);
    const half = Math.ceil(bytes.length / 2);
    const start = offset;
    const end = Math.min(start + half, bytes.length);
    const slice = bytes.slice(start, end);
    return {
      attachmentId, offset: start, data_base64: btoa(slice), byteLength: slice.length,
      nextOffset: end, eof: end >= bytes.length,
    };
  },
};
store.setState({
  initClient: () => {}, pairingState: 'paired', connectionState: 'connected',
  workspacePath: '/sample/synapse', activeProjectId: 'sample',
  projects: [{project_id:'sample',workspace_name:'synapse',workspace_path:'/sample/synapse',git_branch:'main'}],
  expandedProjectIds: ['sample'], currentSession: {project_id:'sample',thread_id:'s0'},
  sessionTitle: '动作菜单验收', sessionsTotal: 1,
  sessions: [{thread_id:'s0',title:'动作菜单验收',updated_at:new Date().toISOString(),time_label:'今天'}],
  modelName: 'model-b', availableModels: ['model-b'], thinkingLevel: 'medium',
  thinkingLevels: ['medium'], canSetThinking: false,
  runtimeStatus: 'idle', attachments: [],
  submitPrompt: async (text) => { window.__submitted.push(text); },
  addAttachments: async () => {},
  client: { getState: () => 'connected', listArtifacts: async (_session, p) => ({
    path: p ?? '.', nextCursor: null, truncated: false, entries: [],
  }), ...screenshotClient },
});
window.fixtureStore = store;
createRoot(document.getElementById('root')).render(React.createElement(App));
`;

const server = await createServer({
  configFile: false,
  envDir: false,
  root: webRoot,
  plugins: [
    react(),
    {
      name: 'composer-actions-fixture',
      configureServer(vite) {
        vite.middlewares.use('/composer-actions-fixture', async (_req, res) => {
          const html = await vite.transformIndexHtml(
            '/composer-actions-fixture',
            '<!doctype html><html><head><meta charset="utf-8"></head><body><div id="root"></div><script type="module" src="/fixture-entry.js"></script></body></html>',
          );
          res.setHeader('Content-Type', 'text/html');
          res.end(html);
        });
      },
      resolveId(id) {
        if (id === '/fixture-entry.js') return '\0composer-actions-fixture';
        return undefined;
      },
      load(id) {
        if (id === '\0composer-actions-fixture') return fixture;
        return undefined;
      },
    },
  ],
  server: { host: '127.0.0.1', port: 0 },
});

let browser: BrowserHandle | undefined;
let client: CdpClient | undefined;
let checks = 0;

try {
  await server.listen();
  browser = await launchBrowser();
  const version = JSON.parse(
    (await httpProbe({ url: `http://127.0.0.1:${browser.port}/json/version` })).body,
  ) as { webSocketDebuggerUrl: string };
  client = await CdpClient.connect(version.webSocketDebuggerUrl);
  const page = await openPage(client, `${server.resolvedUrls.local[0]}composer-actions-fixture`);
  const run = (expression: string) => evaluate(client!, page, expression);
  const settle = (ms = 140) => new Promise((resolve) => setTimeout(resolve, ms));
  const check = async (label: string, expression: string): Promise<void> => {
    assert.equal(await run(expression), true, label);
    checks += 1;
    console.log(`PASS ${label}`);
  };
  const wait = async (expression: string): Promise<void> => {
    for (let i = 0; i < 160; i += 1) {
      if (await run(expression)) return;
      await settle();
    }
    throw new Error(`fixture not ready: ${expression}`);
  };

  /** One real key press through the browser's input pipeline. */
  const press = async (key: string, code: string, vk: number, text?: string): Promise<void> => {
    const down =
      text === undefined
        ? { type: 'rawKeyDown', key, code, windowsVirtualKeyCode: vk }
        : { type: 'keyDown', key, code, text, unmodifiedText: text, windowsVirtualKeyCode: vk };
    await client!.send('Input.dispatchKeyEvent', down, page.sessionId);
    await client!.send(
      'Input.dispatchKeyEvent',
      { type: 'keyUp', key, code, windowsVirtualKeyCode: vk },
      page.sessionId,
    );
    await settle();
  };

  /** A real mouse click at the centre of `selector`. */
  const click = async (selector: string): Promise<void> => {
    const box = (await run(`(() => {
      const el = document.querySelector(${JSON.stringify(selector)});
      if (el === null) return null;
      const r = el.getBoundingClientRect();
      return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    })()`)) as { x: number; y: number } | null;
    if (box === null) throw new Error(`no element for ${selector}`);
    await client!.send(
      'Input.dispatchMouseEvent',
      { type: 'mousePressed', x: box.x, y: box.y, button: 'left', clickCount: 1 },
      page.sessionId,
    );
    await client!.send(
      'Input.dispatchMouseEvent',
      { type: 'mouseReleased', x: box.x, y: box.y, button: 'left', clickCount: 1 },
      page.sessionId,
    );
    await settle();
  };

  const TRIGGER = '[aria-label="添加内容"]';
  const MENU = '[role="menu"][aria-label="添加内容"]';
  const ROW = (id: string) => `[data-action="${id}"]`;
  const focusedOn = (id: string) => `document.activeElement === document.querySelector('${ROW(id)}')`;

  /** Move the real mouse to the centre of `selector` (fires mouseenter/leave). */
  const mouseMove = async (selector: string): Promise<void> => {
    const box = (await run(`(() => {
      const el = document.querySelector(${JSON.stringify(selector)});
      if (el === null) return null;
      const r = el.getBoundingClientRect();
      return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    })()`)) as { x: number; y: number } | null;
    if (box === null) throw new Error(`no element for ${selector}`);
    await client!.send(
      'Input.dispatchMouseEvent',
      { type: 'mouseMoved', x: box.x, y: box.y, button: 'none' },
      page.sessionId,
    );
    await settle();
  };

  await wait(`!!document.querySelector('#console-composer')`);
  await wait(`!!document.querySelector('${TRIGGER}')`);
  await client.send(
    'Emulation.setDeviceMetricsOverride',
    { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false },
    page.sessionId,
  );
  await settle();

  // --- the trigger -----------------------------------------------------------
  await check(
    'the bottom-left control is a labelled menu trigger',
    `(() => {
      const b = document.querySelector('${TRIGGER}');
      return b !== null && b.tagName === 'BUTTON' && b.getAttribute('aria-haspopup') === 'menu'
        && b.getAttribute('aria-expanded') === 'false' && b.type === 'button';
    })()`,
  );

  // --- keyboard open ---------------------------------------------------------
  await run(`document.querySelector('${TRIGGER}').focus()`);
  await press('Enter', 'Enter', 13, '\r');
  await wait(`!!document.querySelector('${MENU}')`);
  await check(
    'Enter on the trigger opens the menu and focuses the first row',
    `document.querySelector('${TRIGGER}').getAttribute('aria-expanded') === 'true'
      && document.activeElement === document.querySelector('${ROW('add-image')}')`,
  );
  await check(
    'the menu paints every registered action',
    `document.querySelectorAll('${MENU} [role="menuitem"]').length === 3`,
  );
  await check(
    'the menu is portalled outside the composer form',
    `document.querySelector('${MENU}').closest('form') === null`,
  );
  await check(
    'opening the menu does not submit the turn',
    `window.__submitted.length === 0`,
  );
  await check(
    'the menu opens upward, above the trigger',
    `(() => {
      const m = document.querySelector('${MENU}').getBoundingClientRect();
      const t = document.querySelector('${TRIGGER}').getBoundingClientRect();
      return m.height > 0 && m.bottom <= t.top + 1;
    })()`,
  );

  // --- arrows / Home / End ---------------------------------------------------
  await press('ArrowDown', 'ArrowDown', 40);
  await check('ArrowDown walks to the window-screenshot row', focusedOn('window-screenshot'));
  await press('ArrowDown', 'ArrowDown', 40);
  await check('ArrowDown reaches the screenshot-settings row', focusedOn('screenshot-settings'));
  await press('ArrowDown', 'ArrowDown', 40);
  await check('ArrowDown wraps back to the first row', focusedOn('add-image'));
  await press('ArrowUp', 'ArrowUp', 38);
  await check('ArrowUp wraps back to the last row', focusedOn('screenshot-settings'));
  await press('End', 'End', 35);
  await check('End jumps to the last row', focusedOn('screenshot-settings'));
  await press('Home', 'Home', 36);
  await check('Home jumps to the first row', focusedOn('add-image'));

  // --- the runnable screenshot rows -----------------------------------------
  await check(
    'the screenshot rows are runnable, not aria-disabled',
    `(() => {
      const rows = ['window-screenshot', 'screenshot-settings']
        .map((id) => document.querySelector('[data-action="' + id + '"]'));
      return rows.every((r) => r !== null && r.getAttribute('aria-disabled') === 'false');
    })()`,
  );
  await check(
    'each screenshot row explains what it does',
    `document.querySelector('${ROW('window-screenshot')}').innerText.length > 0
      && document.querySelector('${ROW('screenshot-settings')}').innerText.length > 0`,
  );
  // The settings row opens the tool through the runtime and never submits a turn.
  await click(ROW('screenshot-settings'));
  await wait(`window.__screenshot.opened === 1`);
  await wait(`!document.querySelector('${MENU}')`);
  await check(
    'the settings row opens the capture tool through the runtime',
    `window.__screenshot.opened === 1 && window.__submitted.length === 0`,
  );
  // The window row queues a capture; the banner shows progress and the turn is
  // never submitted by activating a menu row.
  await click(TRIGGER);
  await wait(`!!document.querySelector('${MENU}')`);
  await click(ROW('window-screenshot'));
  await wait(`window.__screenshot.started === 1`);
  await check(
    'the window row queues a capture and never submits the turn',
    `window.__screenshot.started === 1 && window.__submitted.length === 0`,
  );
  await wait(`!!document.querySelector('[role="status"]')`);
  await check(
    'the capture banner shows progress above the composer',
    `!!document.querySelector('[role="status"]')`,
  );
  await wait(`window.__screenshot.polls >= 3`);
  await run(`window.__screenshot.phase = 'importing'`);
  await wait(`document.body.innerText.includes('正在回填截图附件')`);
  await check('captured frames remain nonterminal until imported', `window.fixtureStore.getState().attachments.length === 0`);

  // An older daemon can flip `completed` while its frames are still missing.
  // The console must keep importing feedback (never a red error) and re-read.
  await run(`window.__screenshot.phase = 'empty'`);
  await wait(`window.screenshotStore.getState().importing === true`);
  await check('a premature completion keeps importing feedback, not an error',
    `window.screenshotStore.getState().notice === null && !document.body.innerText.includes('尚未回填')`);

  // While the frames are read back in slow chunks, keep re-rendering the composer:
  // the by-id preview must not re-read for each render.
  await run(`window.__attachmentReads = []; window.__rerender = setInterval(() => {
    window.fixtureStore.setState((s) => ({ attachments: s.attachments.slice() }));
  }, 5); true`);
  await run(`window.__screenshot.phase = 'completed'`);
  await wait(`window.fixtureStore.getState().attachments.length === 3`);
  await wait(`document.querySelectorAll('#console-composer [aria-label="移除图片"]').length === 3`);
  await check('an empty completion auto-recovers three frames without a refresh',
    `window.__screenshot.started === 1 && window.screenshotStore.getState().notice === null
      && window.screenshotStore.getState().importing === false`);
  await check('three completed frames become visible composer pills without a send',
    `window.__submitted.length === 0 && !document.body.innerText.includes('正在截图')`);
  await wait(`[...document.querySelectorAll('#console-composer img')].filter(i => i.naturalWidth > 0).length === 3`);
  await run(`clearInterval(window.__rerender)`);
  await check('ready attachment ids load three real image previews',
    `[...document.querySelectorAll('#console-composer img')].every(i => i.naturalWidth === 1)`);
  await check('the slow chunked read stays bounded while the composer re-renders',
    `window.__attachmentReads.length === 6`);

  // Hovering a screenshot pill shows the enlarged copy, resolved by id.
  await mouseMove('#console-composer [aria-label="移除图片"]');
  await wait(`!!document.querySelector('img[data-preview-image]')`);
  await wait(`document.querySelector('img[data-preview-image]').naturalWidth > 0`);
  await check('hovering a screenshot pill shows the enlarged real image',
    `document.querySelector('img[data-preview-image]').naturalWidth === 1`);
  // Move the pointer away so the flyout does not outlive the check.
  await mouseMove('body');
  await wait(`!document.querySelector('img[data-preview-image]')`);

  // A reload drops the original draft identity, not the daemon result. Recovery
  // must offer confirmation and must not start another capture.
  await run(`window.fixtureStore.setState({ attachments: [] }); window.screenshotStore.getState().reset(); window.screenshotStore.getState().refreshTool()`);
  await wait(`document.body.innerText.includes('请确认后加入当前输入框')`);
  await check('recovery keeps all frames pending without recapture',
    `window.screenshotStore.getState().pending.attachments.length === 3 && window.fixtureStore.getState().attachments.length === 0 && window.__screenshot.started === 1`);
  await run(`[...document.querySelectorAll('button')].find(b => b.textContent === '加入输入框').click()`);
  await wait(`window.fixtureStore.getState().attachments.length === 3`);
  await run(`window.screenshotStore.getState().refreshTool()`);
  await check('repeated refresh never adds duplicate images',
    `window.fixtureStore.getState().attachments.length === 3 && window.screenshotStore.getState().pending === null && window.__submitted.length === 0`);

  // --- a completion that never fills times out, then a manual refresh recovers --
  await run(`window.screenshotStore.getState().reset(); window.fixtureStore.setState({ attachments: [] })`);
  await run(`window.__screenshot = { started: 0, opened: 0, cancelled: 0, polls: 0, phase: 'running' }`);
  await run(`window.SCREENSHOT_IMPORT_WAIT.deadlineMs = 1200; window.SCREENSHOT_IMPORT_WAIT.recheckMs = 100`);
  await click(TRIGGER);
  await wait(`!!document.querySelector('${MENU}')`);
  await click(ROW('window-screenshot'));
  await wait(`window.__screenshot.started === 1`);
  await run(`window.__screenshot.phase = 'empty'`);
  await wait(`document.body.innerText.includes('尚未回填')`);
  await check('an empty completion that never fills times out with a manual hint',
    `window.fixtureStore.getState().attachments.length === 0 && document.body.innerText.includes('尚未回填')`);
  // The daemon finally provides the frames; the banner's manual refresh recovers them.
  await run(`window.__screenshot.phase = 'completed'`);
  await run(`[...document.querySelectorAll('button')].find(b => b.textContent === '刷新状态').click()`);
  await wait(`window.fixtureStore.getState().attachments.length === 3`);
  await check('the manual refresh recovers the frames and clears the stale notice',
    `window.screenshotStore.getState().notice === null`);

  // --- a result that lands after a session switch stays confirmable -----------
  await run(`window.screenshotStore.getState().reset(); window.fixtureStore.setState({ attachments: [], currentSession: { project_id: 'sample', thread_id: 's0' } })`);
  await run(`window.__screenshot = { started: 0, opened: 0, cancelled: 0, polls: 0, phase: 'running' }`);
  await click(TRIGGER);
  await wait(`!!document.querySelector('${MENU}')`);
  await click(ROW('window-screenshot'));
  await wait(`window.__screenshot.started === 1`);
  await run(`window.fixtureStore.setState({ currentSession: { project_id: 'sample', thread_id: 's1' } })`);
  await run(`window.__screenshot.phase = 'completed'`);
  await wait(`window.screenshotStore.getState().pending !== null`);
  await check('a result that lands after a session switch stays confirmable, not filled',
    `window.fixtureStore.getState().attachments.length === 0
      && window.screenshotStore.getState().pending.attachments.length === 3`);
  await run(`window.screenshotStore.getState().reset(); window.fixtureStore.setState({ attachments: [], currentSession: { project_id: 'sample', thread_id: 's0' } })`);

  // --- the image row opens the composer's picker -----------------------------
  // The menu closed after the capture row ran, so reopen it for the image row.
  await click(TRIGGER);
  await wait(`!!document.querySelector('${MENU}')`);
  await click(ROW('add-image'));
  await wait(`window.__pickerOpened === 1`);
  await check('the image row opens the composer\'s file picker', `window.__pickerOpened === 1`);
  await wait(`!document.querySelector('${MENU}')`);
  await check(
    'the menu closes after a successful action and returns focus to the trigger',
    `document.activeElement === document.querySelector('${TRIGGER}')`,
  );

  // --- Escape closes and restores focus --------------------------------------
  await click(TRIGGER);
  await wait(`!!document.querySelector('${MENU}')`);
  await press('Escape', 'Escape', 27);
  await wait(`!document.querySelector('${MENU}')`);
  await check(
    'Escape closes the menu and returns focus to the trigger',
    `document.activeElement === document.querySelector('${TRIGGER}')`,
  );

  // --- Tab dismisses, and the editor keeps its own keys -----------------------
  await click(TRIGGER);
  await wait(`!!document.querySelector('${MENU}')`);
  // Observe whether the menu claims the Tab keystroke: it must not.
  await run(`(() => {
    window.__tabPrevented = null;
    window.addEventListener('keydown', (event) => {
      if (event.key === 'Tab') window.__tabPrevented = event.defaultPrevented;
    });
    return true;
  })()`);
  await press('Tab', 'Tab', 9);
  await wait(`!document.querySelector('${MENU}')`);
  await check('Tab dismisses the menu', `!document.querySelector('${MENU}')`);
  await check(
    'Tab is left alone and focus is not pulled back to the trigger',
    `window.__tabPrevented === false
      && document.activeElement !== document.querySelector('${TRIGGER}')`,
  );
  // With the menu gone the editor owns Home / End again: the caret moves.
  await run(`(() => {
    const ed = document.querySelector('#console-composer');
    ed.focus();
    ed.textContent = 'abcdef';
    const range = document.createRange();
    range.selectNodeContents(ed);
    range.collapse(false);
    const selection = getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    return true;
  })()`);
  await press('Home', 'Home', 36);
  await check(
    'after Tab the editor still receives Home (the menu no longer steals it)',
    `(() => {
      const selection = getSelection();
      return document.activeElement === document.querySelector('#console-composer')
        && selection.anchorOffset === 0 && selection.anchorNode !== null;
    })()`,
  );

  // --- focus leaving the menu closes it ---------------------------------------
  await click(TRIGGER);
  await wait(`!!document.querySelector('${MENU}')`);
  await run(`document.querySelector('#console-composer').focus()`);
  await wait(`!document.querySelector('${MENU}')`);
  await check(
    'focus moving into the editor dismisses the menu',
    `!document.querySelector('${MENU}')`,
  );

  // --- an outside click closes ------------------------------------------------
  await click(TRIGGER);
  await wait(`!!document.querySelector('${MENU}')`);
  await run(`document.body.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}))`);
  await settle();
  await check('an outside click closes the menu', `!document.querySelector('${MENU}')`);

  // --- add project is still its own control -----------------------------------
  await check(
    'the add-project button is still beside the menu',
    `(() => {
      const b = [...document.querySelectorAll('.ui-composer-toolbar button')]
        .find((el) => (el.getAttribute('title') || '').startsWith('添加项目'));
      return b !== undefined && b.getAttribute('aria-haspopup') === null;
    })()`,
  );

  // --- narrow window: the menu still fits and still opens upward --------------
  await client.send(
    'Emulation.setDeviceMetricsOverride',
    { width: 360, height: 740, deviceScaleFactor: 1, mobile: false },
    page.sessionId,
  );
  await settle(200);
  await click(TRIGGER);
  await wait(`!!document.querySelector('${MENU}')`);
  await check(
    'on a narrow window the menu stays inside the viewport',
    `(() => {
      const m = document.querySelector('${MENU}').getBoundingClientRect();
      return m.width > 0 && m.left >= -1 && m.right <= innerWidth + 1 && m.top >= -1 && m.bottom <= innerHeight + 1;
    })()`,
  );
  await press('Escape', 'Escape', 27);
  await wait(`!document.querySelector('${MENU}')`);

  console.log(`ALL ${checks} CHECKS PASSED`);
} finally {
  client?.close();
  if (browser) await closeBrowser(browser);
  await server.close();
}
