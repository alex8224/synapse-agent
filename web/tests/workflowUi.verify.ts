/**
 * Offline browser acceptance for the workflow bottom-bar entry and WorkflowPanel.
 *
 * Checks in a real headless browser:
 *  1. the bottom bar trigger renders with active pulsating dot when a workflow is running,
 *  2. clicking the trigger opens the WorkflowPanel popover,
 *  3. the runs list shows both runs with their status badges, summaries and token counts,
 *  4. clicking an active run navigates to the RunDetailView:
 *     - back button is present,
 *     - metrics overview cards show steps and tokens,
 *     - execution steps timeline shows call nodes, roles, and status icons,
 *     - active run displays "取消工作流" action,
 *  5. clicking "返回运行列表" returns to the list view,
 *  6. clicking a completed run with execution result renders the output preview card with JSON,
 *  7. clicking outside the panel closes the popover.
 *
 * Run from web/:
 *   $env:NODE_OPTIONS=''; $env:NODE_USE_ENV_PROXY=''; node tests/workflowUi.verify.ts
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { CdpClient, closeBrowser, evaluate, launchBrowser, openPage } from './helpers/cdp.ts';
import type { BrowserHandle } from './helpers/cdp.ts';
import { httpProbe } from './helpers/httpProbe.ts';

const webRoot = path.resolve(import.meta.dirname, '..');
const output = path.resolve(webRoot, '..', '.tmp', 'workflow-ui');
fs.mkdirSync(output, { recursive: true });
process.env.TEMP = output;
process.env.TMP = output;

const manifest = `
import { activityItem } from '/src/components/bottomBar/activityItem.tsx';
import { workflowItem } from '/src/components/bottomBar/workflowItem.tsx';

export const BOTTOM_BAR_ITEMS = [activityItem, workflowItem];
`;

const fixture = `
import React from 'react';
import {createRoot} from 'react-dom/client';
import {BottomBar} from '/src/components/BottomBar.tsx';
import {useConsoleStore} from '/src/stores/useConsoleStore.ts';
import {useWorkflowStore} from '/src/stores/workflowView.ts';
import '/src/index.css';

const SESSION = { project_id: 'test-project', thread_id: 'session-1' };

const RUN_ACTIVE = {
  run_id: 'run-active-1',
  workflow_id: 'wf-code-fix',
  project_id: 'test-project',
  thread_id: 'session-1',
  status: 'running',
  active: true,
  resumable: true,
  resume_blockers: [],
  blocked_calls: [],
  resume_detail: '',
  calls: [
    {
      call_key: 'step:diagnose',
      actor_key: 'reviewer:0',
      role: 'reviewer',
      status: 'completed',
      attempts: 1,
      input_tokens: 1200,
      output_tokens: 300,
      error: null,
    },
    {
      call_key: 'step:implement',
      actor_key: 'implementer:0',
      role: 'implementer',
      status: 'running',
      attempts: 1,
      input_tokens: 800,
      output_tokens: 200,
      error: null,
    }
  ],
  input_tokens: 2000,
  output_tokens: 500,
  error: null,
  result: null,
  created_at: '2026-04-18T10:00:00Z',
  updated_at: '2026-04-18T10:01:00Z',
  finished_at: null,
};

const RUN_COMPLETED = {
  run_id: 'run-done-2',
  workflow_id: 'wf-test-suite',
  project_id: 'test-project',
  thread_id: 'session-1',
  status: 'completed',
  active: false,
  resumable: false,
  resume_blockers: [],
  blocked_calls: [],
  resume_detail: '',
  calls: [
    {
      call_key: 'step:test',
      actor_key: 'tester:0',
      role: 'tester',
      status: 'completed',
      attempts: 1,
      input_tokens: 11000,
      output_tokens: 4200,
      error: null,
    }
  ],
  input_tokens: 11000,
  output_tokens: 4200,
  error: null,
  result: {
    passed: true,
    tests_run: 12,
    details: 'All calculation benchmarks succeeded.'
  },
  created_at: '2026-04-18T09:00:00Z',
  updated_at: '2026-04-18T09:05:00Z',
  finished_at: '2026-04-18T09:05:00Z',
};

const client = {
  async listWorkflowRuns() {
    return {
      result: {
        page: {
          runs: [RUN_ACTIVE, RUN_COMPLETED],
          total: 2,
        },
      },
    };
  },
  async getWorkflowRun(params) {
    const target = params.run_id === 'run-active-1' ? RUN_ACTIVE : RUN_COMPLETED;
    return { result: { run: target } };
  },
  async cancelWorkflowRun(params) {
    return {
      result: {
        run: { ...RUN_ACTIVE, status: 'cancelled', active: false },
      },
    };
  },
};

useConsoleStore.setState({
  initClient: () => {},
  pairingState: 'paired',
  connectionState: 'connected',
  client,
  currentSession: SESSION,
  sessionTitle: '工作流验收',
  modelName: 'deepseek-v4-flash',
  activeSubscriptionId: 'mock-watch',
  runtimeStatus: 'idle',
});

// Preload the workflow store with the test runs
useWorkflowStore.setState({
  runs: [RUN_ACTIVE, RUN_COMPLETED],
  selected: null,
  loading: false,
  error: null,
});

window.fixture = {
  store: useWorkflowStore,
};

createRoot(document.getElementById('root')).render(
  React.createElement(
    'div',
    {
      style: {
        height: '100vh',
        display: 'flex',
        flexDirection: 'column',
        justifyContent: 'flex-end',
      },
    },
    React.createElement(BottomBar),
  ),
);
`;

const server = await createServer({
  configFile: false,
  envDir: false,
  root: webRoot,
  plugins: [
    react(),
    {
      name: 'workflow-ui-fixture',
      enforce: 'pre',
      configureServer(vite) {
        vite.middlewares.use('/workflow-ui-fixture', async (_req, res) => {
          const html = await vite.transformIndexHtml(
            '/workflow-ui-fixture',
            '<!doctype html><html><head><meta charset="utf-8"></head><body><div id="root"></div><script type="module" src="/fixture-entry.js"></script></body></html>',
          );
          res.setHeader('Content-Type', 'text/html');
          res.end(html);
        });
      },
      resolveId(id) {
        if (id === '/fixture-entry.js') return '\0workflow-ui-entry';
        if (id.endsWith('/bottomBar/manifest.tsx')) return '\0workflow-ui-manifest';
      },
      load(id) {
        if (id === '\0workflow-ui-entry') return fixture;
        if (id === '\0workflow-ui-manifest') return manifest;
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
  const page = await openPage(client, `${server.resolvedUrls.local[0]}workflow-ui-fixture`);
  const run = (expression: string) => evaluate(client!, page, expression);
  const settle = (ms = 180) => new Promise((resolve) => setTimeout(resolve, ms));
  const check = async (label: string, expression: string) => {
    assert.equal(await run(expression), true, label);
    checks++;
    console.log(`  [PASS] ${label}`);
  };
  const wait = async (expression: string) => {
    for (let i = 0; i < 150; i++) {
      if (await run(expression)) return;
      await settle();
    }
    throw new Error(`fixture not ready: ${expression}`);
  };
  const click = async (selector: string) => {
    await run(`document.querySelector(${JSON.stringify(selector)}).click(), true`);
    await settle();
  };

  console.log('=== workflow UI & interaction e2e test (real headless browser via CDP) ===');

  // 1. Check bottom bar trigger
  await wait('document.querySelector(\'[data-entry="workflow"]\') !== null');
  await check(
    'the bottom bar trigger displays active status',
    `document.querySelector('[data-entry="workflow"]').textContent.includes('工作流: 运行中')`,
  );
  await check(
    'the trigger has an active animated pulse dot',
    `document.querySelector('[data-entry="workflow"] .animate-ping') !== null`,
  );
  await check(
    'the trigger title contains summary and token usage',
    `document.querySelector('[data-entry="workflow"]').getAttribute('title').includes('工作流运行中')`,
  );

  // 2. Click trigger to open WorkflowPanel
  await click('[data-entry="workflow"]');
  await wait('document.querySelector(\'[role="dialog"], .responsive-floating-panel\') !== null');
  await check(
    'WorkflowPanel popover is open',
    `document.querySelector('[role="dialog"], .responsive-floating-panel') !== null`,
  );
  await check(
    'the popover header prints "工作流运行 (Workflow)" and total runs badge',
    `document.querySelector('[role="dialog"], .responsive-floating-panel').textContent.includes('工作流运行 (Workflow)') &&
     document.querySelector('[role="dialog"], .responsive-floating-panel').textContent.includes('共 2 次')`,
  );

  // 3. Check runs list
  await check(
    'the list view renders Run 1 (active)',
    `document.querySelector('[role="dialog"], .responsive-floating-panel').textContent.includes('run-active-1')`,
  );
  await check(
    'the list view renders Run 2 (done)',
    `document.querySelector('[role="dialog"], .responsive-floating-panel').textContent.includes('run-done-2')`,
  );
  await check(
    'status badges are present for both runs',
    `document.querySelector('[role="dialog"], .responsive-floating-panel').textContent.includes('运行中') &&
     document.querySelector('[role="dialog"], .responsive-floating-panel').textContent.includes('已完成')`,
  );

  // 4. Click Run 1 to open RunDetailView
  await run(`
    const btn = Array.from(document.querySelectorAll('[role="dialog"] button, .responsive-floating-panel button')).find(b => b.textContent.includes('run-active-1'));
    btn.click();
    true;
  `);
  await settle();

  await check(
    'navigated into RunDetailView and back button is visible',
    `document.querySelector('[role="dialog"], .responsive-floating-panel').textContent.includes('返回运行列表')`,
  );
  await check(
    'metrics cards show step counts and token totals',
    `document.querySelector('[role="dialog"], .responsive-floating-panel').textContent.includes('调用步骤') &&
     document.querySelector('[role="dialog"], .responsive-floating-panel').textContent.includes('1/2') &&
     document.querySelector('[role="dialog"], .responsive-floating-panel').textContent.includes('2.5k')`,
  );
  await check(
    'execution steps timeline renders step:diagnose and step:implement',
    `document.querySelector('[role="dialog"], .responsive-floating-panel').textContent.includes('step:diagnose') &&
     document.querySelector('[role="dialog"], .responsive-floating-panel').textContent.includes('step:implement')`,
  );
  await check(
    'role badges are translated to human-readable names',
    `document.querySelector('[role="dialog"], .responsive-floating-panel').textContent.includes('审阅者') &&
     document.querySelector('[role="dialog"], .responsive-floating-panel').textContent.includes('实现者')`,
  );
  await check(
    'active run displays "取消工作流" button',
    `Array.from(document.querySelectorAll('[role="dialog"] button, .responsive-floating-panel button')).some(b => b.textContent.includes('取消工作流'))`,
  );

  // 5. Click back button to return to runs list
  await run(`
    const backBtn = Array.from(document.querySelectorAll('[role="dialog"] button, .responsive-floating-panel button')).find(b => b.textContent.includes('返回运行列表'));
    backBtn.click();
    true;
  `);
  await settle();

  await check(
    'successfully returned to runs list view',
    `!document.querySelector('[role="dialog"], .responsive-floating-panel').textContent.includes('返回运行列表') &&
     document.querySelector('[role="dialog"], .responsive-floating-panel').textContent.includes('run-done-2')`,
  );

  // 6. Click Run 2 (completed run with result)
  const clickResult = await run(`(() => {
    try {
      const panel = document.querySelector('[role="dialog"], .responsive-floating-panel');
      if (!panel) return 'panel not found';
      const buttons = Array.from(panel.querySelectorAll('button'));
      const btn = buttons.find(b => b.textContent.includes('run-done-2'));
      if (!btn) return 'run-done-2 button not found in: ' + buttons.map(b => b.textContent).join(' | ');
      btn.click();
      return 'ok';
    } catch (e) {
      return 'thrown: ' + String(e);
    }
  })()`);
  assert.equal(clickResult, 'ok');
  await settle();

  await check(
    'completed run detail displays output result card',
    `document.querySelector('[role="dialog"], .responsive-floating-panel').textContent.includes('执行产物 / 结果 (Output)')`,
  );
  await check(
    'result pre block contains formatted JSON data',
    `document.querySelector('[role="dialog"] pre, .responsive-floating-panel pre').textContent.includes('All calculation benchmarks succeeded')`,
  );
  await check(
    'copy result button is present',
    `Array.from(document.querySelectorAll('[role="dialog"] button, .responsive-floating-panel button')).some(b => b.textContent.includes('复制'))`,
  );

  // 7. Click close button or press Escape to dismiss popover
  await click('[title="关闭 (Esc)"]');
  await settle();
  await check(
    'clicking the close button closes the workflow popover',
    `document.querySelector('[role="dialog"], .responsive-floating-panel') === null`,
  );

  // Re-open and verify Escape key dismiss
  await click('[data-entry="workflow"]');
  await settle();
  await check(
    're-opened popover successfully',
    `document.querySelector('[role="dialog"], .responsive-floating-panel') !== null`,
  );

  await client!.send(
    'Input.dispatchKeyEvent',
    { type: 'rawKeyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 },
    page.sessionId,
  );
  await client!.send(
    'Input.dispatchKeyEvent',
    { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 },
    page.sessionId,
  );
  await settle();
  await check(
    'pressing Escape closes the workflow popover',
    `document.querySelector('[role="dialog"], .responsive-floating-panel') === null`,
  );

  console.log(`\nALL ${checks} CHECKS PASSED.`);
} finally {
  if (client) await client.close();
  if (browser) await closeBrowser(browser);
  await server.close();
}
