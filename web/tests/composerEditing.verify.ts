/**
 * Browser acceptance for the composer's rich editing.
 *
 * The static guards (`composerKeyboardGuard.test.ts`, `composerSerialization.test.ts`)
 * pin the *source* and the projection; neither can prove that a real browser
 * delivers the keystrokes the way the component expects, or that the caret ends
 * up where the next character has to go.  This script drives the real app with
 * real key events against a synthetic store, so no host, daemon or credential is
 * involved.
 *
 * What it has to settle, in order of how easy it is to get wrong:
 *
 * - `Shift+Enter` must add a line *without* submitting, and the line must survive
 *   into the prompt the store receives.
 * - `Enter` must submit the draft, and an image pasted into the editor must reach
 *   the store's own upload path — never the prompt text.
 * - Typing `@` must open the suggestion list, the arrows must walk it, and the
 *   pick must *replace* the `@` and the query (a stray `@` in the prompt is the
 *   exact bug this feature exists to avoid).
 *
 * Run from web/:
 *   $env:NODE_OPTIONS=''; $env:NODE_USE_ENV_PROXY=''; node --test tests/composerEditing.verify.ts
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { CdpClient, launchBrowser, closeBrowser, openPage, evaluate } from './helpers/cdp.ts';
import { httpProbe } from './helpers/httpProbe.ts';

const webRoot = path.resolve(import.meta.dirname, '..');
const output = path.resolve(webRoot, '..', '.tmp', 'composer-editing');
fs.mkdirSync(output, { recursive: true });
// Keep even the browser's throwaway profile inside the workspace.
process.env.TEMP = output;
process.env.TMP = output;

/**
 * Synthetic store: `submitPrompt` records what the composer produced, and
 * `addAttachments` records what the paste path handed over, so both halves of the
 * contract are observable without a runtime.
 */
const fixture = `
import React from 'react';
import {createRoot} from 'react-dom/client';
import {App} from '/src/App.tsx';
import {useConsoleStore as store} from '/src/stores/useConsoleStore.ts';
import '/src/index.css';

window.__submitted = [];
window.__attached = [];
let attSeq = 0;
store.setState({
  initClient: () => {}, pairingState: 'paired', connectionState: 'connected',
  workspacePath: '/sample/synapse', activeProjectId: 'sample',
  projects: [{project_id:'sample',workspace_name:'synapse',workspace_path:'/sample/synapse',git_branch:'main'}],
  expandedProjectIds: ['sample'], currentSession: {project_id:'sample',thread_id:'s0'},
  sessionTitle: '输入框验收', sessionsTotal: 1,
  sessions: [{thread_id:'s0',title:'输入框验收',updated_at:new Date().toISOString(),time_label:'今天'}],
  modelName: 'model-b', availableModels: ['model-b'], thinkingLevel: 'medium',
  thinkingLevels: ['medium'], canSetThinking: false,
  runtimeStatus: 'idle', attachments: [],
  // The one entry point every image route goes through: record the files, then
  // publish a row so the editor mounts its pill exactly as it would live.
  addAttachments: async (sources) => {
    window.__attached.push(...sources.map((s) => s.name));
    const rows = sources.map((s) => ({
      localId: 'att-' + (++attSeq), name: s.name || 'image', mime: 'image/png',
      size: s.size, status: 'ready', uploadedBytes: s.size, attachmentId: 'ref-' + attSeq,
      error: null, source: s,
    }));
    store.setState((s) => ({attachments: [...s.attachments, ...rows]}));
  },
  submitPrompt: async (text) => { window.__submitted.push(text); },
  // The mention file list reads one workspace directory through the store, which
  // forwards to the client; a fixed tree keeps the run independent of the machine
  // it happens to run on.
  client: {
    // The store's RPC gate reads the connection state before forwarding, so the
    // synthetic client has to answer it.
    getState: () => 'connected',
    listArtifacts: async (_session, p) => ({
      path: p ?? '.', nextCursor: null, truncated: false,
      entries: (p === null || p === '.' || p === '')
        ? [{ path: 'src', kind: 'directory', size: 0, media_type: 'inode/directory', revision: null, modified_at: null },
           { path: 'README.md', kind: 'file', size: 12, media_type: 'text/markdown', revision: 'r1', modified_at: null },
           { path: 'alpha.ts', kind: 'file', size: 12, media_type: 'text/plain', revision: 'r1', modified_at: null }]
        : [{ path: p + '/agent.py', kind: 'file', size: 12, media_type: 'text/x-python', revision: 'r1', modified_at: null }],
    }),
  },
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
      name: 'composer-editing-fixture',
      configureServer(server) {
        server.middlewares.use('/composer-fixture', async (_req, res) => {
          const html = await server.transformIndexHtml(
            '/composer-fixture',
            '<!doctype html><html><head><meta charset="utf-8"></head><body><div id="root"></div><script type="module" src="/fixture-entry.js"></script></body></html>',
          );
          res.setHeader('Content-Type', 'text/html');
          res.end(html);
        });
      },
      resolveId(id) {
        if (id === '/fixture-entry.js') return '\0composer-fixture';
        return undefined;
      },
      load(id) {
        if (id === '\0composer-fixture') return fixture;
        return undefined;
      },
    },
  ],
  server: { host: '127.0.0.1', port: 0 },
});

let browser: Awaited<ReturnType<typeof launchBrowser>> | undefined;
let client: CdpClient | undefined;
let checks = 0;

try {
  await server.listen();
  browser = await launchBrowser();
  const version = JSON.parse(
    (await httpProbe({ url: `http://127.0.0.1:${browser.port}/json/version` })).body,
  ) as { webSocketDebuggerUrl: string };
  client = await CdpClient.connect(version.webSocketDebuggerUrl);
  const page = await openPage(client, `${server.resolvedUrls.local[0]}composer-fixture`);
  const run = async (expression: string): Promise<unknown> => {
    return evaluate(client!, page, expression);
  };
  const settle = () => new Promise((resolve) => setTimeout(resolve, 120));
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

  /**
   * One real key press through the browser's input pipeline.
   *
   * A key that produces text has to be dispatched as `keyDown` with that text: a
   * `rawKeyDown` never runs the control's default action, so the editor would
   * never see the insertion.  `rawKeyDown` is reserved for the bare modifier.
   */
  const press = async (
    key: string,
    code: string,
    vk: number,
    modifiers = 0,
    text?: string,
  ): Promise<void> => {
    const down =
      text === undefined
        ? { type: 'rawKeyDown', key, code, windowsVirtualKeyCode: vk, modifiers }
        : { type: 'keyDown', key, code, text, unmodifiedText: text, windowsVirtualKeyCode: vk, modifiers };
    await client!.send('Input.dispatchKeyEvent', down, page.sessionId);
    await client!.send(
      'Input.dispatchKeyEvent',
      { type: 'keyUp', key, code, windowsVirtualKeyCode: vk, modifiers },
      page.sessionId,
    );
    await settle();
  };
  const enter = (shift = false) =>
    press('Enter', 'Enter', 13, shift ? 8 : 0, '\r');
  const type = async (text: string): Promise<void> => {
    await client!.send('Input.insertText', { text }, page.sessionId);
    await settle();
  };
  /**
   * A clipboard paste that carries text and no file.
   *
   * That is what a bitmap-only clipboard looks like to the page (a screenshot the
   * browser exposes as markup rather than as a `File`), and it is the shape that
   * used to leave a blank line above the image pasted next.
   */
  const pasteText = async (text: string): Promise<void> => {
    await run(`(() => {
      const transfer = new DataTransfer();
      transfer.setData('text/plain', ${JSON.stringify(text)});
      const event = new ClipboardEvent('paste', {bubbles: true, cancelable: true});
      Object.defineProperty(event, 'clipboardData', {value: transfer});
      document.querySelector('#console-composer').dispatchEvent(event);
      return true;
    })()`);
    await settle();
  };
  const clearDraft = async (): Promise<void> => {
    const result = await run(`(() => {
      try {
        const editor = document.querySelector('#console-composer');
        if (editor === null) return 'no editor';
        editor.focus();
        // A collapsed caret in an empty editor has nothing to delete, and
        // selecting the contents of a node with no children throws.
        if (editor.childNodes.length === 0) return true;
        const range = document.createRange();
        range.selectNodeContents(editor);
        const selection = getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        document.execCommand('delete');
        return true;
      } catch (err) {
        return 'clear failed: ' + String(err && err.message ? err.message : err);
      }
    })()`);
    assert.equal(result, true, `clearing the draft failed: ${String(result)}`);
    await settle();
  };

  await wait(`!!document.querySelector('#console-composer')`);
  await client.send(
    'Emulation.setDeviceMetricsOverride',
    { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false },
    page.sessionId,
  );
  await settle();

  // --- the editor is a multi-line surface, not a single-line input ----------
  await check(
    'the composer is an editable multi-line surface',
    `(() => {
      const editor = document.querySelector('#console-composer');
      return editor !== null && editor.getAttribute('contenteditable') === 'true'
        && editor.getAttribute('aria-multiline') === 'true' && editor.tagName !== 'INPUT';
    })()`,
  );
  await check(
    'the empty composer shows its placeholder',
    `document.querySelector('#console-composer').closest('form').innerText.includes('Build anything')`,
  );

  // --- Shift+Enter adds a line; Enter submits ------------------------------
  await run(`document.querySelector('#console-composer').focus()`);
  await type('第一行');
  await press('Enter', 'Enter', 13, 8);
  await type('第二行');
  await check(
    'Shift+Enter breaks the line instead of submitting',
    `window.__submitted.length === 0 && document.querySelector('#console-composer').innerText.includes('第二行')`,
  );
  await check(
    'the editor keeps two lines',
    `document.querySelector('#console-composer').innerText.split('\\n').length === 2`,
  );
  await enter();
  await wait(`window.__submitted.length === 1`);
  await check(
    'Enter submits the multi-line draft as one prompt',
    `window.__submitted[0] === '第一行\\n第二行'`,
  );
  await check(
    'the draft is cleared once the store took it',
    `document.querySelector('#console-composer').innerText.trim() === ''`,
  );

  // --- @ opens the list, the arrows walk it, the pick replaces the @ -------
  await clearDraft();
  await run(`document.querySelector('#console-composer').focus()`);
  await type('请看 ');
  await press('@', 'Digit2', 50, 8, '@');
  await wait(`!!document.querySelector('#composer-mention-list')`);
  await check(
    'typing @ opens the suggestion list next to the caret',
    `(() => {
      const list = document.querySelector('#composer-mention-list');
      const rows = list.querySelectorAll('[role="option"]');
      return rows.length > 0 && list.getBoundingClientRect().width > 0;
    })()`,
  );
  await check(
    'the list groups the static sources even before the files arrive',
    `document.querySelector('#composer-mention-list').innerText.includes('Agent 技能')`,
  );
  // The workspace files are read asynchronously, so the group appears a beat
  // after the list opens; a reader sees the static groups first, then this one.
  try {
    await wait(
      `document.querySelector('#composer-mention-list').innerText.includes('工作区文件')`,
    );
  } catch (err) {
    console.log(
      'LIST CONTENT >>>',
      await run(`document.querySelector('#composer-mention-list').innerText`),
    );
    console.log(
      'STORE >>>',
      JSON.stringify(
        await run(`(() => {
          const s = window.fixtureStore.getState();
          return {
            hasAction: typeof s.listArtifacts,
            clientState: s.client ? s.client.getState() : 'no-client',
            pairing: s.pairingState,
            session: s.currentSession,
            blocked: s.rpcBlockedReason,
          };
        })()`),
      ),
    );
    console.log(
      'DIRECT >>>',
      JSON.stringify(
        await run(`(async () => {
          const s = window.fixtureStore.getState();
          try {
            const page = await s.listArtifacts(s.currentSession, '.', null, 200);
            return { ok: true, entries: page.entries.map((e) => e.path) };
          } catch (err) {
            return { ok: false, message: String(err && err.message ? err.message : err) };
          }
        })()`),
      ),
    );
    throw err;
  }
  await check(
    'the workspace files join the list once the directory is read',
    `document.querySelector('#composer-mention-list').innerText.includes('README.md')`,
  );
  await check(
    'the editor keeps the focus and names the active row',
    `document.activeElement === document.querySelector('#console-composer')
      && !!document.querySelector('#console-composer').getAttribute('aria-activedescendant')`,
  );
  const firstActive = await run(
    `document.querySelector('#console-composer').getAttribute('aria-activedescendant')`,
  );
  await press('ArrowDown', 'ArrowDown', 40);
  const secondActive = await run(
    `document.querySelector('#console-composer').getAttribute('aria-activedescendant')`,
  );
  assert.notEqual(secondActive, firstActive, 'ArrowDown must move the active row');
  checks += 1;
  console.log('PASS ArrowDown moves the active suggestion');

  await press('ArrowUp', 'ArrowUp', 38);
  const backActive = await run(
    `document.querySelector('#console-composer').getAttribute('aria-activedescendant')`,
  );
  assert.equal(backActive, firstActive, 'ArrowUp must walk back to the first row');
  checks += 1;
  console.log('PASS ArrowUp walks back');

  await enter();
  await wait(`!document.querySelector('#composer-mention-list')`);
  await check(
    'picking a suggestion removes the @ and its query',
    `(() => {
      const text = document.querySelector('#console-composer').innerText;
      return !text.includes('@') && text.startsWith('请看');
    })()`,
  );
  await check(
    'the pick leaves one atomic pill behind',
    `document.querySelectorAll('#console-composer [data-composer-pill]').length === 1`,
  );
  await check(
    'the pick leaves the pill where the @ was, not at the end of the draft',
    `(() => {
      const editor = document.querySelector('#console-composer');
      const nodes = [...editor.childNodes];
      const index = nodes.findIndex((n) => n.hasAttribute && n.hasAttribute('data-composer-pill'));
      if (index < 0) return false;
      return nodes.slice(0, index).map((n) => n.textContent).join('').includes('请看');
    })()`,
  );
  await check(
    'the pick leaves no placeholder node behind',
    `(() => {
      const editor = document.querySelector('#console-composer');
      return ![...editor.childNodes].some(
        (n) => n.nodeType === 1 && !n.hasAttribute('data-composer-pill') && n.textContent === '',
      );
    })()`,
  );
  await check(
    'accepting a suggestion does not submit the turn',
    `window.__submitted.length === 1`,
  );
  await type(' 请阅读');
  await enter();
  await wait(`window.__submitted.length === 2`);
  await check(
    'the mention reaches the prompt as its token, not as its label',
    `(() => {
      const text = window.__submitted[1];
      return text.startsWith('请看 @') && text.endsWith('请阅读') && !text.includes('✕');
    })()`,
  );
  await check(
    'the pill markup never leaks into the prompt',
    `(() => {
      const text = window.__submitted[1];
      return !text.includes('data-composer-pill') && !text.includes('aria-label');
    })()`,
  );

  // --- an image paste is an attachment, never prompt text ------------------
  await clearDraft();
  await run(`document.querySelector('#console-composer').focus()`);
  await type('这是截图');
  await check(
    'a pasted image reaches the upload path and not the prompt',
    `(() => {
      const png = new File([new Uint8Array([137,80,78,71,13,10,26,10])], 'shot.png', {type:'image/png'});
      const transfer = new DataTransfer();
      transfer.items.add(png);
      const event = new ClipboardEvent('paste', {bubbles: true, cancelable: true});
      Object.defineProperty(event, 'clipboardData', {value: transfer});
      document.querySelector('#console-composer').dispatchEvent(event);
      return true;
    })()`,
  );
  await settle();
  await wait(`window.__attached.length > 0`);
  await check(
    'one paste uploads the image exactly once',
    `window.__attached.length === 1`,
  );
  await wait(`document.querySelectorAll('#console-composer [data-composer-pill]').length === 1`);
  await check(
    'the accepted image appears as one inline pill after the text it was pasted into',
    `(() => {
      const editor = document.querySelector('#console-composer');
      const nodes = [...editor.childNodes];
      const index = nodes.findIndex((n) => n.hasAttribute && n.hasAttribute('data-composer-pill'));
      if (index < 0) return false;
      return nodes.slice(0, index).map((n) => n.textContent).join('') === '这是截图';
    })()`,
  );
  await check(
    'the pasted image pushes no blank line above itself',
    `document.querySelector('#console-composer').getBoundingClientRect().height < 60`,
  );
  await type('后');
  await check(
    'the caret stays after the image, so typing continues there',
    `(() => {
      const editor = document.querySelector('#console-composer');
      const nodes = [...editor.childNodes];
      const index = nodes.findIndex((n) => n.hasAttribute && n.hasAttribute('data-composer-pill'));
      if (index < 0) return false;
      return nodes.slice(index + 1).map((n) => n.textContent).join('').includes('后');
    })()`,
  );
  await check(
    'the pasted file went to addAttachments under its own name',
    `window.__attached[0] === 'shot.png'`,
  );
  await enter();
  await wait(`window.__submitted.length === 3`);
  await check(
    'the image pill contributes no text to the prompt',
    `window.__submitted[2] === '这是截图后'`,
  );

  // --- a text paste keeps its own lines and never adds a blank one ---------
  await clearDraft();
  await run(`document.querySelector('#console-composer').focus()`);
  await pasteText('\n');
  await check(
    'a whitespace-only paste leaves the draft alone',
    `(() => {
      const editor = document.querySelector('#console-composer');
      return editor.childNodes.length === 0 && editor.getBoundingClientRect().height < 60;
    })()`,
  );
  await pasteText('第一行\n第二行');
  await check(
    'a text paste keeps exactly its own lines',
    `document.querySelector('#console-composer').innerText.split('\\n').length === 2`,
  );
  await enter();
  await wait(`window.__submitted.length === 4`);
  await check(
    'the pasted text reaches the prompt with its newline intact',
    `window.__submitted[3] === '第一行\\n第二行'`,
  );

  // --- the list closes on Escape and on a caret that leaves the query ------
  await clearDraft();
  await run(`document.querySelector('#console-composer').focus()`);
  await press('@', 'Digit2', 50, 8, '@');
  await wait(`!!document.querySelector('#composer-mention-list')`);
  await press('Escape', 'Escape', 27);
  await check(
    'Escape closes the suggestion list without touching the typed text',
    `!document.querySelector('#composer-mention-list')
      && document.querySelector('#console-composer').innerText.includes('@')`,
  );
  // An `@` that does not start a token (an email address) must not open it.
  await clearDraft();
  await run(`document.querySelector('#console-composer').focus()`);
  await type('mailto:someone@example.com');
  await settle();
  await check(
    'an @ inside a word does not open the list',
    `!document.querySelector('#composer-mention-list')`,
  );
} finally {
  if (client !== undefined) await closeBrowser(browser!);
  await server.close();
}

console.log(`\n${checks} composer editing checks passed`);
assert.ok(checks > 0, 'no checks ran');
