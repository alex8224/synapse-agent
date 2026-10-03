/**
 * Real-browser acceptance for the collaboration surface (CDP).
 *
 * What only a browser can prove: that a reader can name another session from the
 * composer, watch the request move, read the receipt, look at the peer's session
 * without leaving, and that the controls they are offered match what the runtime
 * will actually do.
 *
 * Nothing here is synthetic except the chat model. The run starts an isolated
 * daemon (its own state dir, catalog and mailbox), the *real*
 * `synapse-web-console` host, the real dev server, and drives the production
 * console over CDP. The scripted model only chooses tools; the collaboration
 * coordinator, delivery runtime, wire methods, transcript projection and every
 * component under test are production code. The peer's reply is held behind a
 * gate file so the waiting state is observed deterministically instead of racing
 * a fast turn.
 *
 * Run from web/ (or the repository root):
 *   $env:NODE_OPTIONS=''; $env:NODE_USE_ENV_PROXY=''
 *   node tests/collaborationUi.verify.ts
 */
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { CdpClient, closeBrowser, evaluate, launchBrowser, openPage } from './helpers/cdp.ts';
import { httpProbe } from './helpers/httpProbe.ts';
import type { BrowserHandle, PageHandle } from './helpers/cdp.ts';

const WEB_ROOT = path.resolve(import.meta.dirname, '..');
const REPO_ROOT = path.resolve(WEB_ROOT, '..');
const ACCEPTANCE = path.join(REPO_ROOT, 'scripts', 'verify_session_collaboration.py');
const OUTPUT = path.join(REPO_ROOT, '.tmp', 'collaboration-ui');
fs.mkdirSync(OUTPUT, { recursive: true });

function log(message: string): void {
  console.log(`  ${message}`);
}

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.on('error', reject);
    probe.listen(0, '127.0.0.1', () => {
      const port = (probe.address() as net.AddressInfo).port;
      probe.close(() => resolve(port));
    });
  });
}

function waitForPort(host: string, port: number, timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const attempt = (): void => {
      const socket = net.connect({ host, port });
      socket.once('connect', () => {
        socket.destroy();
        resolve();
      });
      socket.once('error', () => {
        socket.destroy();
        if (Date.now() > deadline) reject(new Error(`no listener on ${host}:${port}`));
        else setTimeout(attempt, 200);
      });
    };
    attempt();
  });
}

function waitForFile(file: string, timeoutMs: number): Promise<string> {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const attempt = (): void => {
      if (fs.existsSync(file)) {
        resolve(fs.readFileSync(file, 'utf8'));
        return;
      }
      if (Date.now() > deadline) {
        reject(new Error(`fixture file never appeared: ${path.basename(file)}`));
        return;
      }
      setTimeout(attempt, 200);
    };
    attempt();
  });
}

/** Kill a process and everything it spawned (a venv python.exe has a child). */
function killTree(pid: number | undefined): void {
  if (pid === undefined) return;
  spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore' });
}

interface Spawned {
  proc: ReturnType<typeof spawn>;
  stdout: () => string;
  stderr: () => string;
}

function start(
  command: string,
  args: string[],
  options: { cwd?: string; env?: Record<string, string>; shell?: boolean } = {},
): Spawned {
  const proc = spawn(command, args, {
    cwd: options.cwd ?? REPO_ROOT,
    env: { ...process.env, NODE_OPTIONS: '', NODE_USE_ENV_PROXY: '', ...options.env },
    shell: options.shell ?? false,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let stdout = '';
  let stderr = '';
  proc.stdout?.on('data', (chunk: Buffer) => {
    stdout += chunk.toString('utf8');
  });
  proc.stderr?.on('data', (chunk: Buffer) => {
    stderr += chunk.toString('utf8');
  });
  return { proc, stdout: () => stdout, stderr: () => stderr };
}

const run = (client: CdpClient, page: PageHandle, expression: string): Promise<unknown> =>
  evaluate(client, page, expression);

async function waitFor(
  client: CdpClient,
  page: PageHandle,
  expression: string,
  label: string,
  timeoutMs = 25_000,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if ((await run(client, page, expression)) === true) return;
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  throw new Error(`timed out waiting for ${label}`);
}

async function settle(ms = 250): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, ms));
}

/** Text of the collaboration strip, or null while it is not painted at all. */
const NOTICE_TEXT = `(() => {
  const notice = document.querySelector('[data-testid="collaboration-notice"]');
  return notice === null ? null : notice.innerText;
})()`;

const NOTICE_BUTTONS = `(() => {
  const notice = document.querySelector('[data-testid="collaboration-notice"]');
  if (notice === null) return [];
  return [...notice.querySelectorAll('button')].map((b) => (b.textContent || '').trim());
})()`;

const DOCK_TEXT = `(() => {
  // The dock is a landmark that also carries the tab strip, and a collapsed
  // one is still in the DOM: read the widest visible dock, not the tab button
  // that happens to share the tab's label.
  const docks = [...document.querySelectorAll('[role="complementary"]')]
    .map((dock) => dock.innerText)
    .filter((text) => text.trim() !== '');
  return docks.length === 0
    ? document.body.innerText
    : docks.sort((a, b) => b.length - a.length)[0];
})()`;

/** Open the dock (if collapsed) and switch to one tab. */
async function openDockTab(client: CdpClient, page: PageHandle, label: string): Promise<void> {
  await run(client, page, `(() => {
    const toggle = document.querySelector('[aria-label="展开/收起辅助工作台"]');
    if (toggle !== null && toggle.getAttribute('aria-expanded') !== 'true') toggle.click();
    return true;
  })()`);
  await settle(400);
  await click(client, page, `[aria-label="${label}"]`);
  await settle(600);
}

async function click(client: CdpClient, page: PageHandle, selector: string): Promise<void> {
  const clicked = await run(client, page, `(() => {
    const node = document.querySelector(${JSON.stringify(selector)});
    if (node === null) return false;
    node.click();
    return true;
  })()`);
  assert.equal(clicked, true, `could not click ${selector}`);
  await settle();
}

async function press(client: CdpClient, page: PageHandle, key: string, code: string, vk: number): Promise<void> {
  const text = key === 'Enter' ? '\r' : undefined;
  await client.send(
    'Input.dispatchKeyEvent',
    { type: 'keyDown', key, code, text, unmodifiedText: text, windowsVirtualKeyCode: vk },
    page.sessionId,
  );
  await client.send(
    'Input.dispatchKeyEvent',
    { type: 'keyUp', key, code, windowsVirtualKeyCode: vk },
    page.sessionId,
  );
  await settle();
}

async function main(): Promise<void> {
  const receiveOnly = process.argv.includes('--receive-only');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'collab-ui-'));
  const gate = path.join(root, 'release');
  const fixtures = path.join(root, 'fixtures.json');
  const daemonLog = path.join(OUTPUT, 'daemon.log');
  const hostLog = path.join(OUTPUT, 'host.log');

  let daemon: Spawned | undefined;
  let host: Spawned | undefined;
  let dev: Spawned | undefined;
  let browser: BrowserHandle | undefined;
  let client: CdpClient | undefined;
  try {
    console.log('=== collaboration surface (real host, real daemon, headless browser) ===');

    // 1. Fixture projects: two read-only workspaces and a marker file. Defined by
    //    the Python harness so the browser run and the pytest regression share it.
    const prepared = spawnSync(
      'uv',
      ['run', '--no-sync', 'python', ACCEPTANCE, '--prepare', root],
      { cwd: REPO_ROOT, encoding: 'utf8' },
    );
    assert.equal(prepared.status, 0, `fixture preparation failed: ${prepared.stderr}`);
    const marker = (JSON.parse(prepared.stdout.trim()) as { marker: string }).marker;
    log(`fixture marker      : ${marker}`);

    // 2. Isolated daemon with a scripted model and a held reply.
    daemon = start('uv', [
      'run', '--no-sync', 'python', ACCEPTANCE,
      '--scripted', '--gate', gate, '--fixtures', fixtures, '--serve', root,
    ]);
    const fixturePayload = JSON.parse(await waitForFile(fixtures, 60_000)) as {
      sessions: { source: { project_id: string; thread_id: string }; target: { project_id: string; thread_id: string } };
    };
    const daemonMeta = JSON.parse(
      await waitForFile(path.join(root, 'daemon', 'daemon.json'), 30_000),
    ) as { port: number };
    log(`daemon port         : ${daemonMeta.port}`);
    log(`source session      : ${fixturePayload.sessions.source.thread_id}`);
    log(`target session      : ${fixturePayload.sessions.target.thread_id}`);

    // 3. The real console host, pointed at that daemon (never one it starts).
    host = start('uv', [
      'run', '--no-sync', 'python', '-m', 'synapse.web_console.entry',
      '--workspace', path.join(root, 'source'),
      '--host', '127.0.0.1', '--port', '0',
      '--runtime-host', '127.0.0.1', '--runtime-port', String(daemonMeta.port),
      '--state-dir', path.join(root, 'daemon'),
      '--token-file', path.join(root, 'daemon', 'token'),
      '--catalog-path', path.join(root, 'catalog.sqlite'),
      '--project-scope', 'all', '--no-pairing',
      // The workspace is a throwaway fixture, so the built console has to be
      // named explicitly instead of being discovered under it.
      '--static-dir', path.join(WEB_ROOT, 'dist'),
    ]);
    const deadline = Date.now() + 60_000;
    while (!host.stdout().includes('\n') && Date.now() < deadline) await settle(200);
    assert.ok(
      host.stdout().includes('\n'),
      'the console host never announced itself; see the host log under .tmp/collaboration-ui',
    );
    const hostMeta = JSON.parse(host.stdout().split(/\r?\n/)[0] ?? '{}') as {
      port: number;
      url: string;
      pairing_required: boolean;
    };
    assert.ok(hostMeta.port > 0, 'the console host did not announce a port');
    const hostOrigin = `http://127.0.0.1:${hostMeta.port}`;
    log(`console host        : ${hostOrigin} (pairing required: ${hostMeta.pairing_required})`);

    // 4. The dev server is the production console's own frontend, proxied to that
    //    host: same components, same wire, no bundled copy.
    const devPort = await freePort();
    dev = start(
      'npm',
      ['run', 'dev', '--', '--host', '127.0.0.1', '--port', String(devPort), '--strictPort'],
      { cwd: WEB_ROOT, env: { SYNAPSE_WEB_CONSOLE_URL: hostOrigin }, shell: true },
    );
    await waitForPort('127.0.0.1', devPort, 90_000);
    const consoleUrl = `http://127.0.0.1:${devPort}/`;
    log(`console (dev proxy) : ${consoleUrl}  ->  ${hostOrigin}`);

    browser = await launchBrowser();
    const version = JSON.parse(
      (await httpProbe({ url: `http://127.0.0.1:${browser.port}/json/version` })).body,
    ) as { webSocketDebuggerUrl: string };
    client = await CdpClient.connect(version.webSocketDebuggerUrl);
    const page = await openPage(client, consoleUrl);
    await client.send(
      'Emulation.setDeviceMetricsOverride',
      { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false },
      page.sessionId,
    );

    const sourceRow = `button[title^="UI source"]`;
    await waitFor(client, page, `document.querySelector('${sourceRow}') !== null`, 'the session list');
    await click(client, page, sourceRow);
    await waitFor(
      client,
      page,
      `document.querySelector('[aria-label="当前项目"]') !== null`,
      'the source project to be active',
    );
    console.log('PASS session list is loaded and the source session is selected');

    // -- the composer can name the other session -----------------------------
    // The flyout offers only sessions the sidebar has already loaded, so the
    // reader expands the other project first. This is a real step of the flow,
    // not test scaffolding: without it the flyout has nothing to offer.
    const expanded = await run(client, page, `(() => {
      const rows = [...document.querySelectorAll('[aria-label="项目会话列表"] button')];
      const row = rows.find((b) => (b.getAttribute('title') || '').endsWith('target'));
      if (row === undefined) return false;
      if (row.getAttribute('aria-expanded') === 'true') return true;
      row.click();
      return true;
    })()`);
    assert.equal(expanded, true, 'the other project row is not in the sidebar');
    await waitFor(
      client,
      page,
      `[...document.querySelectorAll('[aria-label="项目会话列表"] button')]
        .some((b) => (b.getAttribute('title') || '').startsWith('UI target'))`,
      'the other session to appear in the sidebar',
    );

    await run(client, page, `document.querySelector('#console-composer').focus()`);
    await client.send('Input.insertText', { text: '@' }, page.sessionId);
    await settle();
    await client.send('Input.insertText', { text: 'target' }, page.sessionId);
    await waitFor(
      client,
      page,
      `(() => {
        const box = document.querySelector('[aria-label="引用插入"]');
        const group = box === null ? null : box.querySelector('[aria-label="会话"]');
        return group !== null && group.querySelectorAll('[role="option"]').length > 0;
      })()`,
      'the session group in the mention flyout',
    );
    const offer = (await run(client, page, `(() => {
      const box = document.querySelector('[aria-label="引用插入"]');
      const group = box.querySelector('[aria-label="会话"]');
      return [...group.querySelectorAll('[role="option"]')].map((o) => (o.textContent || '').trim());
    })()`)) as string[];
    assert.ok(
      offer.some((row) => row.includes('UI target')),
      `the flyout did not offer the other session: ${JSON.stringify(offer)}`,
    );
    assert.ok(
      offer.every((row) => !row.includes('UI source')),
      `the flyout offered the current session: ${JSON.stringify(offer)}`,
    );
    await press(client, page, 'Enter', 'Enter', 13);
    const pill = await run(client, page, `(() => {
      const node = document.querySelector('[aria-label^="移除引用"]');
      return node === null ? null : node.getAttribute('aria-label');
    })()`);
    assert.ok(
      typeof pill === 'string' && pill.includes('UI target'),
      `the pill did not carry the other session: ${String(pill)}`,
    );
    log(`mention pill        : ${String(pill)}`);
    console.log('PASS `@` offers another session (and never the current one), and inserts its pill');

    // -- the request is sent by the agent, not by the console ----------------
    await client.send(
      'Input.insertText',
      { text: '请让对方读取它工作区的 /FACT.txt，并交付其中的标记。' },
      page.sessionId,
    );
    await settle();
    await click(client, page, '[aria-label="发送消息"]');
    await waitFor(client, page, `${NOTICE_TEXT} !== null`, 'the collaboration strip');
    await waitFor(
      client,
      page,
      `${NOTICE_TEXT}.includes('${fixturePayload.sessions.target.thread_id}')`,
      'the peer session id in the strip',
    );
    const pending = (await run(client, page, NOTICE_TEXT)) as string;
    assert.ok(
      /等待对方处理|对方处理中|等待补充/.test(pending),
      `the strip did not report the request as live: ${pending}`,
    );
    const buttons = (await run(client, page, NOTICE_BUTTONS)) as string[];
    assert.ok(buttons.includes('仅接收结果'), `no receive-only control: ${JSON.stringify(buttons)}`);
    assert.ok(buttons.includes('撤销此项交办'), `no stop control: ${JSON.stringify(buttons)}`);
    console.log(`PASS the strip reports the live request and offers both controls: ${pending.split('\n')[0]}`);

    // -- the dock reads the same request without leaving the session ---------
    await openDockTab(client, page, '协作');
    const dock = (await run(client, page, DOCK_TEXT)) as string;
    assert.ok(dock.includes('我请对方'), `the dock did not orient the request: ${dock}`);
    assert.ok(dock.includes('协作请求'), `the dock did not label the message kind: ${dock}`);
    assert.ok(dock.includes('FACT.txt'), `the dock did not show the request body: ${dock}`);
    assert.ok(dock.includes('协作 ') && dock.includes('交办 '), 'missing stable identities');
    assert.ok(dock.includes('尚未交付'), 'missing result processing state');
    console.log('PASS the dock shows the request, its direction and its body');

    if (!receiveOnly) {
      // Wait until the source's previous turn is actually released, while the
      // target is still held by the gate. A collaboration must not lock its sender.
      await settle(2000);
      await run(client, page, `document.querySelector('#console-composer').focus()`);
      await client.send('Input.insertText', { text: '继续一项不依赖对方的独立工作。' }, page.sessionId);
      await click(client, page, '[aria-label="发送消息"]');
      await waitFor(client, page, `document.body.innerText.includes('No session reference was provided.')`,
        'the independent source turn to execute');
      assert.equal(fs.existsSync(gate), false, 'the target must still be processing');
      console.log('PASS the commander executes independent work while the target is still processing');
    }

    if (receiveOnly) {
      await run(client, page, `(() => {
        const strip = document.querySelector('[data-testid="collaboration-notice"]');
        [...strip.querySelectorAll('button')].find((button) => button.innerText === '仅接收结果').click();
      })()`);
      await waitFor(client, page, `!${NOTICE_BUTTONS}.includes('仅接收结果')`, 'receive-only applied');
      fs.writeFileSync(gate, '');
      // No source turn will end to trigger a read, and no manual refresh is made.
      await waitFor(client, page, `${NOTICE_TEXT}.includes('已收到结论')`, 'passive receipt', 60_000);
      await settle(2500);
      await openDockTab(client, page, '协作');
      const result = (await run(client, page, DOCK_TEXT)) as string;
      assert.ok(result.includes(marker), 'passive result body is missing');
      assert.ok(result.includes('结果已到达，待处理'), 'passive result was incorrectly processed');
      console.log('PASS receive-only receipt refreshes automatically without continuing the source');
      return;
    }

    // -- the receipt, and the automatic continuation -------------------------
    fs.writeFileSync(gate, '');
    await waitFor(
      client,
      page,
      `${NOTICE_TEXT}.includes('已收到结论')`,
      'the receipt in the strip',
      60_000,
    );
    await waitFor(
      client,
      page,
      `document.body.innerText.includes('${marker}')`,
      'the source transcript to consume the marker',
      60_000,
    );
    const answered = (await run(client, page, NOTICE_TEXT)) as string;
    log(`strip after receipt : ${answered.replace(/\n/g, ' | ')}`);
    console.log('PASS the receipt arrives and the source session continues with it');

    await openDockTab(client, page, '协作');
    await waitFor(
      client,
      page,
      `${DOCK_TEXT}.includes('结论')`,
      'the delivered reply in the dock',
    );
    const delivered = (await run(client, page, DOCK_TEXT)) as string;
    assert.ok(delivered.includes(marker), 'the dock must show the delivered conclusion');
    console.log('PASS the dock shows the delivered conclusion, not just a state');

    // -- the peer's own session is previewable, read-only --------------------
    // Checked once the peer is idle: a history read of a *running* session is
    // refused by the transport, and the panel then shows that raw refusal
    // instead of a transcript (see the acceptance notes).
    await openDockTab(client, page, '对方会话');
    // The peer row labels its inner span "<project> / <thread>"; the sidebar's
    // session row ends with the same thread id, so the separator is what tells
    // them apart (clicking the sidebar row would leave this session instead).
    const peerRow = `(() => {
      const label = [...document.querySelectorAll('span[title]')].find((s) => {
        const title = s.getAttribute('title') || '';
        return title.includes(' / ') && title.endsWith('${fixturePayload.sessions.target.thread_id}');
      });
      return label === undefined ? undefined : label.closest('button');
    })()`;
    await waitFor(client, page, `${peerRow} !== undefined`, 'the peer row');
    await run(client, page, `(() => {
      const row = ${peerRow};
      if (row === undefined) return false;
      row.click();
      return true;
    })()`);
    await settle(600);
    await waitFor(
      client,
      page,
      `${DOCK_TEXT}.includes('协作交办 · 来自')`,
      'the peer transcript preview',
    );
    const peerPanel = (await run(client, page, DOCK_TEXT)) as string;
    assert.ok(
      peerPanel.includes('进入完整会话'),
      'the preview must offer the one explicit way out',
    );
    assert.ok(peerPanel.includes(marker), 'the preview must show what the peer delivered');
    console.log('PASS the peer session is previewable read-only, with an explicit way in');

    await waitFor(client, page, `${NOTICE_BUTTONS}.every((text) => text !== '仅接收结果' && text !== '撤销此项交办')`,
      'completed controls disappear');
    const liveSummary = await run(client, page, `(() => {
      const node = [...document.querySelectorAll('main span')].find((n) => n.innerText.startsWith('协作结论 · 来自'));
      return node?.innerText;
    })()`);
    assert.ok(typeof liveSummary === 'string', 'missing live collaboration summary');
    await client.send('Page.reload', {}, page.sessionId);
    await waitFor(client, page, `document.querySelector('main')?.innerText.includes('协作结论 · 来自')`,
      'durable collaboration summary');
    const historyPresentation = await run(client, page, `(() => {
      const main = document.querySelector('main');
      const node = [...main.querySelectorAll('span')].find((n) => n.innerText.startsWith('协作结论 · 来自'));
      return {summary:node?.innerText, raw:main.innerText.includes('[会话协作消息：')};
    })()`);
    assert.deepEqual(historyPresentation, {summary:liveSummary,raw:false});
    console.log('PASS collaboration summary is identical live and after reload, without technical envelopes');

    // -- layout, and no browser-level failure --------------------------------
    for (const width of [1280, 1024, 820]) {
      await client.send(
        'Emulation.setDeviceMetricsOverride',
        { width, height: 900, deviceScaleFactor: 1, mobile: false },
        page.sessionId,
      );
      await settle(200);
      assert.equal(
        await run(client, page, 'document.documentElement.scrollWidth <= innerWidth'),
        true,
        `horizontal overflow at ${width}px with the dock open`,
      );
    }
    console.log('PASS no horizontal overflow at 1280/1024/820px with the dock open');

    const exceptions = client.events.filter((event) => event.method === 'Runtime.exceptionThrown');
    assert.deepEqual(exceptions, [], 'the browser raised an exception');
    console.log('PASS no browser exception');
    console.log(`fixtures kept at ${root}`);
  } finally {
    if (client !== undefined) client.close();
    if (browser !== undefined) await closeBrowser(browser);
    if (dev !== undefined) killTree(dev.proc.pid);
    if (host !== undefined) killTree(host.proc.pid);
    if (daemon !== undefined) {
      fs.writeFileSync(path.join(root, 'stop'), '');
      await settle(2000);
      killTree(daemon.proc.pid);
      fs.writeFileSync(daemonLog, daemon.stdout() + daemon.stderr());
    }
    // Written outside the daemon branch: a host that failed before the daemon
    // announced itself is exactly the case whose log matters.
    if (host !== undefined) fs.writeFileSync(hostLog, host.stdout() + host.stderr());
  }
}

await main();
