/**
 * Browser acceptance for the project-todo board and its arrange flow.
 *
 * Offline and synthetic: no host, no daemon, no credentials.  The real `App` is
 * mounted against a *recording* runtime client, so every RPC the console makes is
 * observed on the page's own call log, and the two claims this board makes are
 * checked against the rendered DOM:
 *
 * 1. the board is legible in **both** themes -- the chip/priority/date colours are
 *    read back from `getComputedStyle` and their WCAG contrast is computed against
 *    the surface they actually sit on (the dock is translucent, so the chain is
 *    composited, not guessed);
 * 2. arranging tasks onto sessions **never runs anything** -- a multi-selection can
 *    take one shared session or one session per task, and either way the only RPCs
 *    are `session.create` (metadata-only) and `task.update`: no turn is submitted,
 *    no goal is armed, and the console stays on the session it was showing.
 *
 * Run from web/: node tests/rightDockTasks.verify.ts
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { CdpClient, launchBrowser, closeBrowser, openPage, evaluate } from './helpers/cdp.ts';
import { httpProbe } from './helpers/httpProbe.ts';

const webRoot = path.resolve(import.meta.dirname, '..');
const output = path.resolve(webRoot, '..', '.tmp', 'tasks-acceptance');
fs.mkdirSync(output, { recursive: true });
process.env.TEMP = output;
process.env.TMP = output;

const fixture = `
import React from 'react';
import {createRoot} from 'react-dom/client';
import {App} from '/src/App.tsx';
import {useConsoleStore as store} from '/src/stores/useConsoleStore.ts';
import {useRightDockStore} from '/src/stores/useRightDockStore.ts';
import {initAppearance, useAppearanceStore} from '/src/stores/appearance.ts';
import '/src/index.css';

/** The backlog the fake runtime serves, one task per status plus a bound one. */
const tasks = [
  {id:'t1', title:'优化代办 UI', description:'在不同主题下的显示效果', status:'in_progress',
   priority:'low', labels:['ui'], assigned_thread_id:null, created_at_ms:1, updated_at_ms:Date.parse('2026-10-16T09:53:00'), completed_at_ms:null},
  {id:'t2', title:'补齐多选', description:'批量安排到会话', status:'todo',
   priority:'high', labels:['ui'], assigned_thread_id:null, created_at_ms:1, updated_at_ms:Date.parse('2026-10-16T09:54:00'), completed_at_ms:null},
  {id:'t3', title:'已完成的历史项', description:'', status:'done',
   priority:'medium', labels:[], assigned_thread_id:null, created_at_ms:1, updated_at_ms:Date.parse('2026-10-16T09:55:00'), completed_at_ms:2},
];

const calls = [];
let created = 0;
const record = (method, params) => { calls.push({method, params}); };

/** The backlog each project serves; the fake runtime is the only source. */
const tasksByProject = {
  sample: tasks,
  B: [
    {id:'b1', title:'B 任务一', description:'', status:'todo', priority:'low', labels:[],
     assigned_thread_id:null, created_at_ms:1, updated_at_ms:Date.parse('2026-10-16T10:00:00'), completed_at_ms:null},
    {id:'b2', title:'B 任务二', description:'', status:'done', priority:'medium', labels:[],
     assigned_thread_id:null, created_at_ms:1, updated_at_ms:Date.parse('2026-10-16T10:01:00'), completed_at_ms:2},
  ],
};

/**
 * Deferred RPC gate.  While "hold" is set, a gated method parks its promise in
 * "queue" instead of settling, so the fixture can move the project/client
 * boundary *between* an RPC being issued and its result landing -- the window the
 * board's lifecycle guards exist for.  Only the fixture opens the gate.
 */
const gate = {hold:false, queue:[]};
const GATED = new Set(['updateProjectTask', 'deleteProjectTask', 'createSession']);

/**
 * One-shot update faults the fixture can arm: 'not_found' (the backend's own
 * "row vanished" result) and 'error' (a thrown transport failure).  They prove the
 * board keeps its draft and never paints a fake success -- the fixture is a
 * stand-in for the backend, not a validation of it.
 */
const faults = {update:null};

const serve = (method, params) => {
  if (method === 'listProjectTasks') {
    const list = tasksByProject[params.project_id] ?? [];
    return {project_id:params.project_id, tasks:list, total_count:list.length};
  }
  if (method === 'createProjectTask') {
    const list = tasksByProject[params.project_id] ?? (tasksByProject[params.project_id] = []);
    const task = {
      id:'new-' + (list.length + 1), title:params.title, description:params.description ?? '',
      status:'todo', priority:params.priority ?? 'medium', labels:params.labels ?? [],
      assigned_thread_id:null, created_at_ms:Date.now(), updated_at_ms:Date.now(), completed_at_ms:null,
    };
    list.unshift(task);
    return {project_id:params.project_id, task, status:'ok'};
  }
  if (method === 'createSession') {
    created += 1;
    return {command_id:'c', session:{project_id:params.project_id, thread_id:'bg-' + created}, created:true, title:''};
  }
  if (method === 'updateProjectTask') {
    if (faults.update === 'not_found') {
      faults.update = null;
      return {project_id:params.project_id, task:null, status:'not_found'};
    }
    if (faults.update === 'error') {
      faults.update = null;
      throw new Error('injected update failure');
    }
    const task = (tasksByProject[params.project_id] ?? []).find((t) => t.id === params.task_id);
    if (!task) return {project_id:params.project_id, task:null, status:'not_found'};
    if (params.title != null) task.title = params.title;
    if (params.description != null) task.description = params.description;
    if (params.priority != null) task.priority = params.priority;
    if (params.labels != null) task.labels = params.labels;
    if (params.status != null) {
      // Mirror the backend's terminal transition: entering done stamps the
      // completion time, leaving done clears it, staying done keeps it.
      const wasDone = task.status === 'done';
      task.status = params.status;
      if (params.status === 'done' && !wasDone) task.completed_at_ms = Date.now();
      else if (params.status !== 'done' && wasDone) task.completed_at_ms = null;
    }
    if (params.assigned_thread_id != null) task.assigned_thread_id = params.assigned_thread_id;
    task.updated_at_ms = Date.now();
    return {project_id:params.project_id, task:{...task}, status:'ok'};
  }
  if (method === 'deleteProjectTask') {
    const list = tasksByProject[params.project_id] ?? [];
    const index = list.findIndex((t) => t.id === params.task_id);
    if (index >= 0) list.splice(index, 1);
    return {project_id:params.project_id, task_id:params.task_id, deleted:index >= 0};
  }
  // The mounted CollaborationNotice reads this on every session/settle change; an
  // empty page keeps it from painting rows this fixture has none of.
  if (method === 'listCollaborations') return {requests:[], scope_id:null, scope_version:0};
  // The right dock mounts its active tab (files) even while closed, so its
  // directory read runs on load; an empty page keeps it off this fixture's path.
  if (method === 'listArtifacts') return {entries:[], nextCursor:null, path:'.'};
  if (method === 'listSessions') return {items:[{thread_id:'s0', title:'当前会话', updated_at:new Date().toISOString()}], next_offset:null, total:1};
  return {};
};

/** A recording stand-in for SynapseRuntimeClient: only the shapes this flow reads. */
const makeClient = () => new Proxy({}, {
  get(_target, prop) {
    if (prop === 'then') return undefined;
    return (params) => {
      const method = String(prop);
      record(method, params);
      if (gate.hold && GATED.has(method)) {
        return new Promise((resolve) => { gate.queue.push(() => resolve(serve(method, params))); });
      }
      return Promise.resolve(serve(method, params));
    };
  }
});
const client = makeClient();

const switched = [];
const goals = [];
window.acceptance = {
  calls, switched, goals, tasks, tasksByProject, client,
  /**
   * Move the header to another project the way activateProject does: the old
   * backlog is dropped before the new one is read through the load effect.
   */
  switchTo: (projectId) => {
    store.setState({activeProjectId:projectId, projectTasks:[], projectTasksLoading:false, projectTasksError:null});
  },
  /** Re-pair onto the same project: a new client object, same project id. */
  setClient: () => { store.setState({client: makeClient()}); },
  /** Park the next gated RPCs so a boundary can move while one is in flight. */
  hold: () => { gate.hold = true; gate.queue.length = 0; },
  releaseFirst: () => { const next = gate.queue.shift(); if (next) next(); },
  queueLength: () => gate.queue.length,
  resume: () => { gate.hold = false; while (gate.queue.length) gate.queue.shift()(); },
  /** Type into the controlled create form (fixture-only; the console never does). */
  type: (selector, value) => {
    const el = document.querySelector(selector);
    if (!el) throw new Error('no field for ' + selector);
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value);
    el.dispatchEvent(new Event('input', {bubbles:true}));
  },
  /** Set a native <select> value and fire the change event React listens for. */
  choose: (selector, value) => {
    const el = document.querySelector(selector);
    if (!el) throw new Error('no select for ' + selector);
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(el, value);
    el.dispatchEvent(new Event('change', {bubbles:true}));
  },
  /** Arm a one-shot update fault for the next write ('not_found' | 'error'). */
  faultUpdate: (kind) => { faults.update = kind; },
  /** Resize the dock: the board must stay usable at the narrowest content width. */
  setDockWidth: (width) => { useRightDockStore.getState().setWidth(width); },
};
window.appearanceStore = useAppearanceStore;

store.setState({
  initClient: () => {}, pairingState: 'paired', connectionState: 'connected', client,
  workspacePath: '/sample/synapse', activeProjectId: 'sample',
  projects: [{project_id:'sample', workspace_name:'synapse', workspace_path:'/sample/synapse', git_branch:'main'}],
  expandedProjectIds: ['sample'],
  currentSession: {project_id:'sample', thread_id:'s0'}, sessionTitle: '当前会话',
  sessions: [{thread_id:'s0', title:'当前会话', updated_at:new Date().toISOString(), time_label:'刚刚'}],
  sessionsTotal: 1,
  // The two writes this flow must never make: a session switch and a goal.
  switchSession: async (threadId) => { switched.push(threadId); },
  setGoal: async (objective) => { goals.push(objective); return true; },
  messages: [],
});
initAppearance();
try {
  createRoot(document.getElementById('root')).render(React.createElement(App));
} catch (error) {
  window.__fixtureError = String(error && error.stack || error);
}
`;

const server = await createServer({
  configFile: false,
  envDir: false,
  root: webRoot,
  plugins: [
    react(),
    {
      name: 'tasks-acceptance-fixture',
      configureServer(vite) {
        vite.middlewares.use('/tasks-fixture', async (_req, res) => {
          const html = await vite.transformIndexHtml(
            '/tasks-fixture',
            '<!doctype html><html><head><meta charset="utf-8">'
              + '<script>window.addEventListener("error",(e)=>{window.__fixtureError=window.__fixtureError||(e.error&&e.error.stack)||e.message||"error";});'
              + 'window.addEventListener("unhandledrejection",(e)=>{window.__fixtureError=window.__fixtureError||String(e.reason);});</script>'
              + '</head><body><div id="root"></div><script type="module" src="/tasks-fixture-entry.js"></script></body></html>',
          );
          res.setHeader('Content-Type', 'text/html');
          res.end(html);
        });
      },
      resolveId(id) {
        if (id === '/tasks-fixture-entry.js') return '\0tasks-fixture';
      },
      load(id) {
        if (id === '\0tasks-fixture') return fixture;
      },
    },
  ],
  server: { host: '127.0.0.1', port: 0 },
});

/** WCAG relative luminance + contrast, and the composited surface an element sits on. */
const PAGE_HELPERS = `
window.acc = (() => {
  const parse = (css) => (css.match(/[\\d.]+/g) ?? []).map(Number);
  const lum = ([r, g, b]) => {
    const ch = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b);
  };
  const surface = (el) => {
    const chain = [];
    for (let node = el; node; node = node.parentElement) {
      const [r, g, b, a = 1] = parse(getComputedStyle(node).backgroundColor);
      if (a > 0) chain.push([r, g, b, a]);
      if (a >= 1) break;
    }
    let out = [255, 255, 255];
    for (const [r, g, b, a] of chain.reverse()) {
      out = [r * a + out[0] * (1 - a), g * a + out[1] * (1 - a), b * a + out[2] * (1 - a)];
    }
    return out;
  };
  const contrast = (el) => {
    const fg = parse(getComputedStyle(el).color).slice(0, 3);
    const bg = surface(el);
    const a = lum(fg), b = lum(bg);
    return Math.round(((Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)) * 100) / 100;
  };
  const byText = (selector, text) =>
    [...document.querySelectorAll(selector)].find((el) => el.textContent.trim() === text);
  const chip = (text) =>
    [...document.querySelectorAll('span')]
      .filter((el) => el.children.length === 0)
      .find((el) => el.textContent.trim() === text);
  // The button whose text is 'text' inside the form that owns 'fieldSelector':
  // precise enough to tell the create/edit form's own 取消/保存 apart from any
  // other dialog's.
  const formButton = (fieldSelector, text) => {
    const field = document.querySelector(fieldSelector);
    if (!field) return null;
    return [...field.closest('form').querySelectorAll('button')]
      .find((b) => b.textContent.trim() === text) ?? null;
  };
  return {contrast, surface, byText, chip, formButton, calls: () => window.acceptance.calls.map((c) => c.method)};
})();
`;

let browser: Awaited<ReturnType<typeof launchBrowser>> | undefined;
let client: CdpClient | undefined;
let checks = 0;
const measured: string[] = [];

try {
  await server.listen();
  browser = await launchBrowser();
  const version = JSON.parse(
    (await httpProbe({ url: `http://127.0.0.1:${browser.port}/json/version` })).body,
  ) as { webSocketDebuggerUrl: string };
  client = await CdpClient.connect(version.webSocketDebuggerUrl);
  const page = await openPage(client, `${server.resolvedUrls.local[0]}tasks-fixture`);
  const run = (expression: string): Promise<unknown> => evaluate(client!, page, expression);
  const settle = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 250));
  const check = async (label: string, expression: string): Promise<void> => {
    assert.equal(await run(expression), true, label);
    checks += 1;
    console.log(`PASS ${label}`);
  };
  const wait = async (expression: string): Promise<void> => {
    for (let i = 0; i < 160; i += 1) {
      if (await run(expression)) return;
      const failure = await run(`window.__fixtureError ?? null`);
      if (failure !== null) throw new Error(`fixture threw: ${String(failure)}`);
      await settle();
    }
    const dump = await run(
      `JSON.stringify({root: document.getElementById('root')?.children.length ?? -1, body: document.body.innerHTML.slice(0, 500)})`,
    );
    throw new Error(`fixture not ready: ${expression} :: ${String(dump)}`);
  };
  const click = async (expression: string): Promise<void> => {
    await run(expression);
    await settle();
  };
  const shot = async (name: string): Promise<void> => {
    const image = (await client!.send('Page.captureScreenshot', { format: 'png' }, page.sessionId)) as {
      data: string;
    };
    fs.writeFileSync(path.join(output, `${name}.png`), Buffer.from(image.data, 'base64'));
  };
  const theme = async (value: string): Promise<void> => {
    await run(`window.appearanceStore?.getState?.().setAppearance(${JSON.stringify(value)})`);
    await settle();
  };
  const ratio = async (expression: string): Promise<number> => {
    const value = await run(`window.acc.contrast(${expression})`);
    assert.equal(typeof value, 'number', `no contrast for ${expression}`);
    return value as number;
  };

  await wait(`!!document.querySelector('#console-composer')`);
  await client.send(
    'Emulation.setDeviceMetricsOverride',
    { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false },
    page.sessionId,
  );
  await run(PAGE_HELPERS);
  // The board is behind the top bar's own entry: opening it is the reader's path.
  await click(`document.querySelector('[aria-label="打开项目待办看板"]').click()`);
  await wait(`!!window.acc.chip('进行中 (In Progress)')`);

  // --- 1. the board is legible in both themes --------------------------------
  const CHIPS: Array<[string, string]> = [
    ['进行中 chip', `window.acc.chip('进行中 (In Progress)')`],
    ['待办 chip', `window.acc.chip('待办 (To Do)')`],
    ['已完成 chip', `window.acc.chip('已完成 (Done)')`],
    ['优先级 chip', `window.acc.chip('低 (Low)')`],
    ['紧急 chip', `window.acc.chip('高 (High)')`],
    ['任务标题', `window.acc.byText('h4', '优化代办 UI')`],
    ['任务描述', `window.acc.byText('p', '在不同主题下的显示效果')`],
    ['标签', `window.acc.byText('span', '#ui')`],
    ['筛选：选中', `window.acc.byText('button', '全部 (3)')`],
    ['筛选：未选中', `window.acc.byText('button', '待办 (1)')`],
  ];

  for (const mode of ['light', 'dark'] as const) {
    await theme(mode);
    await check(`system ${mode} is the ${mode} theme`, `document.documentElement.dataset.theme === 'fluent-${mode}'`);
    for (const [label, expression] of CHIPS) {
      const value = await ratio(expression);
      measured.push(`${mode.padEnd(5)} ${label.padEnd(12)} ${value.toFixed(2)}:1`);
      // A chip label is 10px, so the floor is the small-text floor with a hair of
      // slack for the translucent dock it sits on; the pre-fix pair measured 2.16:1
      // in the dark theme, which is what "看不清" was.
      assert.ok(value >= 4.0, `${label} in ${mode} measured ${value}:1`);
      checks += 1;
    }
    await shot(`board-${mode}`);
  }
  await theme('light');
  const lightChip = await run(`getComputedStyle(window.acc.chip('进行中 (In Progress)')).color`);
  const darkChip = await (async () => {
    await theme('dark');
    const value = await run(`getComputedStyle(window.acc.chip('进行中 (In Progress)')).color`);
    await theme('light');
    return value;
  })();
  await check('the two themes resolve the same chip to different steps', `${JSON.stringify(lightChip)} !== ${JSON.stringify(darkChip)}`);

  // --- 2. multi-select and the arrange flow ----------------------------------
  await check('every card carries a selection checkbox', `document.querySelectorAll('input.ui-check').length === 3`);
  await check('the selection toolbar is hidden while nothing is ticked', `!window.acc.byText('span', '已选 2 项')`);
  await click(`document.querySelectorAll('input.ui-check')[0].click()`);
  await click(`document.querySelectorAll('input.ui-check')[1].click()`);
  await check('ticking two cards shows the batch toolbar', `!!window.acc.byText('span', '已选 2 项')`);
  await check('the toolbar offers one shared session', `!!window.acc.byText('button', '安排到一个会话')`);
  await check('the toolbar offers one session per task', `!!window.acc.byText('button', '每项并行会话')`);
  await check(
    'the toolbar offers a four-state batch selector and delete',
    `!!document.querySelector('select[aria-label="批量设置状态"]')
      && [...document.querySelector('select[aria-label="批量设置状态"]').options].map((o) => o.value).join(',') === ',todo,in_progress,blocked,done'
      && !!window.acc.byText('button', '删除')`,
  );
  // The two cards are already ticked above; these label the ticked ones by title
  // (re-clicking them here would toggle the selection back off before arranging).
  await check(
    'each card is addressable by its own title',
    `!!document.querySelector('input.ui-check[aria-label="选择待办：优化代办 UI"]')
      && !!document.querySelector('input.ui-check[aria-label="选择待办：补齐多选"]')`,
  );

  const before = await run(`window.acceptance.calls.length`);
  await click(`window.acc.byText('button', '安排到一个会话').click()`);
  await wait(`window.acceptance.calls.some((c) => c.method === 'updateProjectTask')`);
  await settle();
  await check('a shared arrange creates exactly one session', `window.acceptance.calls.filter((c) => c.method === 'createSession').length === 1`);
  await check(
    'both tasks are bound to that one session',
    `window.acceptance.tasks.filter((t) => t.assigned_thread_id === 'bg-1').map((t) => t.id).join(',') === 't1,t2'`,
  );
  await check(
    'both tasks were marked in progress',
    `window.acceptance.tasks.filter((t) => t.status === 'in_progress').length === 2`,
  );
  await check(
    'arranging runs nothing: no turn, no goal, no session switch',
    // The console also reads a few background panels on mount (collaboration,
    // files, screenshot tool, speech), so the allow-list is every non-turn call
    // the board's own flow plus those reads make; a `turn`, a goal or a session
    // switch anywhere in it is the failure this pins.
    `window.acceptance.calls.every((c) => ['createSession', 'updateProjectTask', 'listProjectTasks', 'listSessions', 'listCollaborations', 'listArtifacts', 'sttStatus', 'getScreenshotStatus', 'getState', 'connect'].includes(c.method))
      && window.acceptance.goals.length === 0 && window.acceptance.switched.length === 0`,
  );
  await check(
    'the console stays on the session the reader was looking at',
    `window.acceptance.client !== undefined && document.querySelector('[aria-label="打开项目待办看板"]') !== null
      && window.acceptance.switched.length === 0`,
  );
  await check('the board says the arrangement did not run', `/未执行/.test(document.body.innerText)`);
  await check('the ticked cards now show 会话中', `document.querySelectorAll('span').length > 0 && document.body.innerText.split('会话中').length - 1 === 2`);
  await check('a bound card offers navigation, not execution', `document.body.innerText.includes('前往会话') && !document.body.innerText.includes('开会话执行')`);
  await check('the selection is cleared after arranging', `!window.acc.byText('span', '已选 2 项')`);
  await shot('board-arranged');

  // Navigating to a bound session is a view switch, not a new session.
  const sessionsBefore = await run(`window.acceptance.calls.filter((c) => c.method === 'createSession').length`);
  await click(`window.acc.byText('button', '前往会话').click()`);
  await check('前往会话 switches the view instead of creating a session',
    `window.acceptance.calls.filter((c) => c.method === 'createSession').length === ${sessionsBefore}
      && window.acceptance.switched.join(',') === 'bg-1'`);
  checks += 1;

  // Parallel mode: one session per task, still nothing running.
  await click(`window.acc.byText('button', '全选').click()`);
  await check('全选 ticks the visible list', `!!window.acc.byText('span', '已选 3 项')`);
  const createdBefore = await run(`window.acceptance.calls.filter((c) => c.method === 'createSession').length`);
  await click(`window.acc.byText('button', '每项并行会话').click()`);
  await wait(`window.acceptance.calls.filter((c) => c.method === 'createSession').length > ${createdBefore}`);
  await settle();
  await check(
    // Only the still-unbound task opens a session; the two already sharing one keep it.
    'parallel mode opens a session only for the unbound task',
    `window.acceptance.calls.filter((c) => c.method === 'createSession').length === ${createdBefore} + 1
      && window.acceptance.tasks.find((t) => t.id === 't3').assigned_thread_id === 'bg-2'
      && window.acceptance.tasks.filter((t) => t.assigned_thread_id === 'bg-1').length === 2`,
  );
  await check(
    'parallel mode still runs nothing',
    `window.acceptance.goals.length === 0 && window.acceptance.switched.length === 1
      && window.acceptance.calls.every((c) => !/turn/i.test(c.method))`,
  );
  await check('the pre-arrange call log was non-trivial', `${before} > 0`);
  await shot('board-parallel');

  // --- 3. project/client lifecycle isolation --------------------------------
  // The store already fences its own backlog writes by project/client/pairing,
  // but the board's *local* draft, selection, filter, busy flag and notice have
  // no owner across a boundary.  These checks drive the real component through a
  // switch while an RPC is parked, which is exactly the window a late callback
  // would otherwise write into the project that replaced it.
  const A_TASK_1 = 'input.ui-check[aria-label="选择待办：优化代办 UI"]';
  const A_TASK_2 = 'input.ui-check[aria-label="选择待办：补齐多选"]';
  const A_TASK_3 = 'input.ui-check[aria-label="选择待办：已完成的历史项"]';
  const B_TASK_1 = 'input.ui-check[aria-label="选择待办：B 任务一"]';
  const DRAFT = 'input[placeholder="待办标题..."]';

  // (a) batch status: the loop must stop at the boundary, never fall through to B.
  await click(`document.querySelector('${A_TASK_1}').click()`);
  await click(`document.querySelector('${A_TASK_2}').click()`);
  await check('two A cards are ticked for the isolation run', `!!window.acc.byText('span', '已选 2 项')`);
  await run(`window.acceptance.hold()`);
  const updatesBefore = await run(`window.acceptance.calls.filter((c) => c.method === 'updateProjectTask').length`);
  await run(`window.acceptance.choose('select[aria-label="批量设置状态"]', 'in_progress')`);
  await click(`document.querySelector('button[aria-label="应用批量状态"]').click()`);
  await wait(`window.acceptance.queueLength() === 1`);
  await check(
    'the first A update is parked, addressed to A',
    `window.acceptance.calls.filter((c) => c.method === 'updateProjectTask').length === ${updatesBefore} + 1
      && window.acceptance.calls.at(-1).params.project_id === 'sample'`,
  );
  await run(`window.acceptance.switchTo('B')`);
  await wait(`!!window.acc.byText('h4', 'B 任务一')`);
  await check('the board followed the switch to B', `!!window.acc.byText('h4', 'B 任务二')`);
  await check('the old selection went with the old project', `!window.acc.byText('span', '已选 2 项')`);
  // A fresh B draft and a fresh B tick: neither may be touched by A's late result.
  await click(`window.acc.byText('button', '新建').click()`);
  await run(`window.acceptance.type('${DRAFT}', 'B 草稿')`);
  await click(`document.querySelector('${B_TASK_1}').click()`);
  await check(
    'B has its own draft and its own tick',
    `document.querySelector('${DRAFT}').value === 'B 草稿' && !!window.acc.byText('span', '已选 1 项')`,
  );
  await run(`window.acceptance.releaseFirst()`);
  await settle();
  await settle();
  await check(
    'no second operation is sent after the switch',
    `window.acceptance.queueLength() === 0
      && window.acceptance.calls.filter((c) => c.method === 'updateProjectTask').length === ${updatesBefore} + 1`,
  );
  await check(
    'nothing was addressed to B',
    `window.acceptance.calls.every((c) => c.method !== 'updateProjectTask' || c.params.project_id === 'sample')`,
  );
  await check(
    "the old callback did not clear B's draft or tick",
    `document.querySelector('${DRAFT}').value === 'B 草稿' && !!window.acc.byText('span', '已选 1 项')`,
  );
  await check('the old run published no notice', `!/已安排|未能安排/.test(document.body.innerText)`);
  await run(`window.acceptance.resume()`);
  await shot('isolation-batch-status');

  // (b) batch delete: the stale run must not wipe B's new tick.
  await run(`window.acceptance.switchTo('sample')`);
  await wait(`!!window.acc.byText('h4', '优化代办 UI')`);
  await click(`document.querySelector('${A_TASK_1}').click()`);
  await click(`document.querySelector('${A_TASK_2}').click()`);
  await run(`window.acceptance.hold()`);
  const deletesBefore = await run(`window.acceptance.calls.filter((c) => c.method === 'deleteProjectTask').length`);
  await click(`window.acc.byText('button', '删除').click()`);
  await wait(`window.acceptance.queueLength() === 1`);
  await run(`window.acceptance.switchTo('B')`);
  await wait(`!!window.acc.byText('h4', 'B 任务一')`);
  await click(`document.querySelector('${B_TASK_1}').click()`);
  await check('B has a tick before the stale delete lands', `!!window.acc.byText('span', '已选 1 项')`);
  await run(`window.acceptance.releaseFirst()`);
  await settle();
  await settle();
  await check(
    'the stale delete stops the loop and never reaches B',
    `window.acceptance.queueLength() === 0
      && window.acceptance.calls.filter((c) => c.method === 'deleteProjectTask').length === ${deletesBefore} + 1
      && window.acceptance.calls.filter((c) => c.method === 'deleteProjectTask').every((c) => c.params.project_id === 'sample')`,
  );
  await check("the stale delete did not clear B's tick", `!!window.acc.byText('span', '已选 1 项')`);
  await run(`window.acceptance.resume()`);

  // (c) arrange: a stale run must not be reported as "no sessions available".
  await run(`window.acceptance.switchTo('sample')`);
  await wait(`!!window.acc.byText('h4', '补齐多选')`);
  await click(`document.querySelector('${A_TASK_2}').click()`);
  await click(`document.querySelector('${A_TASK_3}').click()`);
  await run(`window.acceptance.hold()`);
  const sessionsBeforeIso = await run(`window.acceptance.calls.filter((c) => c.method === 'createSession').length`);
  await click(`window.acc.byText('button', '安排到一个会话').click()`);
  await wait(`window.acceptance.queueLength() === 1`);
  await run(`window.acceptance.switchTo('B')`);
  await wait(`!!window.acc.byText('h4', 'B 任务一')`);
  await run(`window.acceptance.releaseFirst()`);
  await settle();
  await settle();
  await check(
    'a stale arrange is not reported as "no sessions"',
    `!/未能安排/.test(document.body.innerText) && !/已安排/.test(document.body.innerText)`,
  );
  await check(
    'the stale arrange created nothing for B',
    `window.acceptance.calls.filter((c) => c.method === 'createSession').length === ${sessionsBeforeIso} + 1
      && window.acceptance.calls.filter((c) => c.method === 'createSession').every((c) => c.params.project_id === 'sample')`,
  );
  await run(`window.acceptance.resume()`);

  // (d) a round trip resets the board-local form, filter and selection.
  await run(`window.acceptance.switchTo('sample')`);
  await wait(`!!window.acc.byText('h4', '补齐多选')`);
  await click(`window.acc.byText('button', '新建').click()`);
  await run(`window.acceptance.type('${DRAFT}', 'A 草稿')`);
  await click(`window.acc.byText('button', '进行中 (2)').click()`);
  await click(`document.querySelector('${A_TASK_2}').click()`);
  await check(
    'A carries a draft, a filter and a tick',
    `document.querySelector('${DRAFT}').value === 'A 草稿' && !!window.acc.byText('span', '已选 1 项')`,
  );
  await run(`window.acceptance.switchTo('B')`);
  await wait(`!!window.acc.byText('h4', 'B 任务一')`);
  await check(
    'switching to B closes the form, clears the tick and resets the filter',
    `!document.querySelector('${DRAFT}')
      && !window.acc.byText('span', '已选 1 项')
      && window.acc.byText('button', '全部 (2)').className.includes('bg-accent')`,
  );
  await run(`window.acceptance.switchTo('sample')`);
  await wait(`!!window.acc.byText('h4', '补齐多选')`);
  await check(
    'the round trip back to A is just as clean',
    `!document.querySelector('${DRAFT}')
      && !window.acc.byText('span', '已选 1 项')
      && window.acc.byText('button', '全部 (2)').className.includes('bg-accent')`,
  );
  await shot('isolation-round-trip');

  // (e) a new runtime client on the same project resets the board and reloads.
  await click(`window.acc.byText('button', '新建').click()`);
  await run(`window.acceptance.type('${DRAFT}', 'A 草稿')`);
  await click(`document.querySelector('${A_TASK_2}').click()`);
  await check(
    'A carries a draft and a tick before the client changes',
    `document.querySelector('${DRAFT}').value === 'A 草稿' && !!window.acc.byText('span', '已选 1 项')`,
  );
  const loadsBefore = await run(`window.acceptance.calls.filter((c) => c.method === 'listProjectTasks').length`);
  await run(`window.acceptance.setClient()`);
  await wait(`window.acceptance.calls.filter((c) => c.method === 'listProjectTasks').length > ${loadsBefore}`);
  await settle();
  await check(
    'a new client for the same project clears the old local state',
    `!document.querySelector('${DRAFT}') && !window.acc.byText('span', '已选 1 项')`,
  );
  await check('the new client re-reads the backlog', `!!window.acc.byText('h4', '补齐多选')`);
  await run(`window.acceptance.resume()`);

  // --- 4. per-card edit form and the four-state selector ---------------------
  // Runs last: it renames a task, so nothing after it depends on the fixture's
  // original labels.  The board is on project `sample` with two cards.
  const EDIT_TITLE = 'input[aria-label="编辑待办标题"]';
  const EDIT_DESC = 'textarea[aria-label="编辑待办描述"]';
  const EDIT_PRIO = 'select[aria-label="待办优先级"]';
  const EDIT_LABELS = 'input[aria-label="待办标签"]';
  const EDIT_T2 = 'button[aria-label="编辑待办：补齐多选"]';
  const SAVE_EDIT = `window.acc.formButton('${EDIT_TITLE}', '保存修改').click()`;
  const CANCEL_EDIT = `window.acc.formButton('${EDIT_TITLE}', '取消').click()`;

  await check(
    'every card carries an always-visible edit entry named by its task',
    `document.querySelectorAll('button[aria-label^="编辑待办："]').length === 2
      && getComputedStyle(document.querySelector('${EDIT_T2}')).opacity === '1'`,
  );
  await check(
    'the edit entry is keyboard focusable',
    `(() => { const b = document.querySelector('${EDIT_T2}'); b.focus(); return document.activeElement === b; })()`,
  );
  await check(
    'every card carries a four-state status selector named by its task',
    `document.querySelectorAll('select[aria-label^="待办状态："]').length === 2
      && [...document.querySelector('select[aria-label="待办状态：已完成的历史项"]').options].map((o) => o.value).join(',') === 'todo,in_progress,blocked,done'`,
  );
  await check(
    'the edit entry and status selector do not overflow the card',
    `[...document.querySelectorAll('select[aria-label^="待办状态："]')].every((s) => {
      const card = s.closest('.rounded-card');
      return card.scrollWidth <= card.clientWidth + 2;
    })`,
  );

  // Prefill: the form opens on the saved values, description included in full.
  await click(`document.querySelector('${EDIT_T2}').click()`);
  await wait(`!!document.querySelector('${EDIT_TITLE}')`);
  await check(
    'the edit form is prefilled with the saved title/description/priority/labels',
    `document.querySelector('${EDIT_TITLE}').value === '补齐多选'
      && document.querySelector('${EDIT_DESC}').value === '批量安排到会话'
      && document.querySelector('${EDIT_PRIO}').value === 'high'
      && document.querySelector('${EDIT_LABELS}').value === 'ui'`,
  );
  await check(
    'the form switches to edit mode (heading and submit label)',
    `/编辑待办/.test(document.querySelector('${EDIT_TITLE}').closest('form').innerText)
      && /保存修改/.test(document.querySelector('${EDIT_TITLE}').closest('form').innerText)`,
  );

  const longDesc = '长描述：这里应当完整可读，不被卡片上的三行截断。'.repeat(40);
  await run(`window.acceptance.type('${EDIT_DESC}', ${JSON.stringify(longDesc)})`);
  await check(
    'a long description lands in full, not truncated to three lines',
    `document.querySelector('${EDIT_DESC}').value.length === ${longDesc.length}`,
  );
  await check(
    'the long description scrolls inside the form instead of stretching the dock',
    `(() => {
      const ta = document.querySelector('${EDIT_DESC}');
      const r = ta.closest('form').getBoundingClientRect();
      return ta.scrollHeight > ta.clientHeight
        && getComputedStyle(ta).overflowY === 'auto'
        && r.top >= -1 && r.bottom <= window.innerHeight + 1;
    })()`,
  );

  // Cancel: no write, and reopening shows the saved values.
  const updatesBeforeCancel = await run(`window.acceptance.calls.filter((c) => c.method === 'updateProjectTask').length`);
  await click(CANCEL_EDIT);
  await check(
    'cancel closes the form and issues no write',
    `!document.querySelector('${EDIT_DESC}')
      && window.acceptance.calls.filter((c) => c.method === 'updateProjectTask').length === ${updatesBeforeCancel}`,
  );
  await click(`document.querySelector('${EDIT_T2}').click()`);
  await wait(`!!document.querySelector('${EDIT_DESC}')`);
  await check(
    'reopening shows the saved values, not the discarded edit',
    `document.querySelector('${EDIT_DESC}').value === '批量安排到会话'`,
  );

  // Save four changed fields, clearing description and labels.
  await run(`window.acceptance.type('${EDIT_TITLE}', '补齐多选（改）')`);
  await run(`window.acceptance.type('${EDIT_DESC}', '')`);
  await run(`window.acceptance.choose('${EDIT_PRIO}', 'critical')`);
  await run(`window.acceptance.type('${EDIT_LABELS}', '')`);
  const updatesBeforeSave = await run(`window.acceptance.calls.filter((c) => c.method === 'updateProjectTask').length`);
  await click(SAVE_EDIT);
  await wait(`window.acceptance.calls.filter((c) => c.method === 'updateProjectTask').length === ${updatesBeforeSave} + 1`);
  await settle();
  await check('a successful save closes the edit form', `!document.querySelector('${EDIT_TITLE}')`);
  await check(
    'only the changed fields are sent, and clearing sends empty values',
    `(() => {
      const p = window.acceptance.calls.filter((c) => c.method === 'updateProjectTask').at(-1).params;
      const defined = Object.keys(p).filter((k) => p[k] !== undefined);
      return p.title === '补齐多选（改）'
        && p.description === ''
        && p.priority === 'critical'
        && Array.isArray(p.labels) && p.labels.length === 0
        && p.status === undefined && p.assigned_thread_id === undefined
        && ['title', 'description', 'priority', 'labels'].every((k) => defined.includes(k));
    })()`,
  );
  await check(
    'the returned entity refreshed the card',
    `!!window.acc.byText('h4', '补齐多选（改）') && !window.acc.byText('span', '#ui')`,
  );
  await click(`document.querySelector('button[aria-label="编辑待办：补齐多选（改）"]').click()`);
  await wait(`!!document.querySelector('${EDIT_TITLE}')`);
  await check(
    'reopening after the save shows the new saved values',
    `document.querySelector('${EDIT_TITLE}').value === '补齐多选（改）'
      && document.querySelector('${EDIT_DESC}').value === ''
      && document.querySelector('${EDIT_PRIO}').value === 'critical'
      && document.querySelector('${EDIT_LABELS}').value === ''`,
  );

  // Empty title: no write.
  const updatesBeforeEmpty = await run(`window.acceptance.calls.filter((c) => c.method === 'updateProjectTask').length`);
  await run(`window.acceptance.type('${EDIT_TITLE}', '   ')`);
  await check(
    'a blank title disables the save control',
    `window.acc.formButton('${EDIT_TITLE}', '保存修改').disabled === true`,
  );
  await click(SAVE_EDIT);
  await settle();
  await check(
    'a blank title issues no write and keeps the form open',
    `window.acceptance.calls.filter((c) => c.method === 'updateProjectTask').length === ${updatesBeforeEmpty}
      && !!document.querySelector('${EDIT_TITLE}')`,
  );
  await run(`window.acceptance.type('${EDIT_TITLE}', '补齐多选（改）')`);
  await click(CANCEL_EDIT);

  // Failure: the draft survives with a visible error, and a retry succeeds.
  await click(`document.querySelector('button[aria-label="编辑待办：补齐多选（改）"]').click()`);
  await wait(`!!document.querySelector('${EDIT_TITLE}')`);
  await run(`window.acceptance.type('${EDIT_DESC}', '失败后应保留的草稿')`);
  await run(`window.acceptance.faultUpdate('not_found')`);
  await click(SAVE_EDIT);
  await settle();
  await settle();
  await check(
    'a not_found write keeps the draft and shows an error instead of faking success',
    `document.querySelector('${EDIT_DESC}').value === '失败后应保留的草稿'
      && !!document.querySelector('${EDIT_TITLE}').closest('form').querySelector('[role="alert"]')`,
  );
  const updatesBeforeRetry = await run(`window.acceptance.calls.filter((c) => c.method === 'updateProjectTask').length`);
  await click(SAVE_EDIT);
  await wait(`window.acceptance.calls.filter((c) => c.method === 'updateProjectTask').length === ${updatesBeforeRetry} + 1`);
  await settle();
  await check(
    'retrying after the failure saves and closes the form',
    `!document.querySelector('${EDIT_TITLE}')
      && window.acceptance.tasks.find((t) => t.id === 't2').description === '失败后应保留的草稿'`,
  );

  // A pending save cannot be submitted twice.
  await click(`document.querySelector('button[aria-label="编辑待办：补齐多选（改）"]').click()`);
  await wait(`!!document.querySelector('${EDIT_TITLE}')`);
  await run(`window.acceptance.type('${EDIT_DESC}', '并发保存草稿')`);
  await run(`window.acceptance.hold()`);
  const updatesBeforeDup = await run(`window.acceptance.calls.filter((c) => c.method === 'updateProjectTask').length`);
  await click(SAVE_EDIT);
  await wait(`window.acceptance.queueLength() === 1`);
  await click(SAVE_EDIT);
  await settle();
  await check(
    'a pending save cannot be submitted twice',
    `window.acceptance.calls.filter((c) => c.method === 'updateProjectTask').length === ${updatesBeforeDup} + 1
      && window.acceptance.queueLength() === 1`,
  );
  await run(`window.acceptance.resume()`);
  await settle();
  await check(
    'the pending save lands once and closes the form',
    `!document.querySelector('${EDIT_TITLE}')
      && window.acceptance.tasks.find((t) => t.id === 't2').description === '并发保存草稿'`,
  );
  await shot('edit-form');

  // Four-state cycle on one card, including done -> todo reopening it.
  const STATUS_T3 = 'select[aria-label="待办状态：已完成的历史项"]';
  const setStatus = async (value: string): Promise<void> => {
    await run(`window.acceptance.choose('${STATUS_T3}', ${JSON.stringify(value)})`);
    await wait(`window.acceptance.tasks.find((t) => t.id === 't3').status === ${JSON.stringify(value)}`);
    await settle();
  };
  await setStatus('blocked');
  await check('a card can move to 受阻', `document.querySelector('${STATUS_T3}').value === 'blocked'`);
  await setStatus('done');
  await check(
    'moving to 已完成 stamps the completion time',
    `window.acceptance.tasks.find((t) => t.id === 't3').completed_at_ms != null
      && document.querySelector('${STATUS_T3}').value === 'done'`,
  );
  await setStatus('todo');
  await check(
    'done -> 待办 reopens the task and clears the completion time',
    `window.acceptance.tasks.find((t) => t.id === 't3').completed_at_ms === null
      && document.querySelector('${STATUS_T3}').value === 'todo'`,
  );

  // A failed status write restores the server value (no fake update).
  await run(`window.acceptance.faultUpdate('not_found')`);
  await run(`window.acceptance.choose('${STATUS_T3}', 'blocked')`);
  await settle();
  await settle();
  await check(
    'a failed status write is not painted; the selector keeps the server value',
    `window.acceptance.tasks.find((t) => t.id === 't3').status === 'todo'
      && document.querySelector('${STATUS_T3}').value === 'todo'`,
  );

  // Batch status: every state reachable from one select, blocked and todo included.
  const BATCH = 'select[aria-label="批量设置状态"]';
  const BATCH_APPLY = 'button[aria-label="应用批量状态"]';
  await click(`document.querySelector('input.ui-check[aria-label="选择待办：补齐多选（改）"]').click()`);
  await click(`document.querySelector('input.ui-check[aria-label="选择待办：已完成的历史项"]').click()`);
  await wait(`!!document.querySelector('${BATCH}')`);
  await check(
    'the batch selector offers all four states and starts unset',
    `document.querySelector('${BATCH}').value === ''
      && [...document.querySelector('${BATCH}').options].map((o) => o.value).join(',') === ',todo,in_progress,blocked,done'`,
  );
  await check('the apply control is disabled until a target is chosen', `document.querySelector('${BATCH_APPLY}').disabled === true`);
  await run(`window.acceptance.choose('${BATCH}', 'blocked')`);
  await check('choosing a target enables apply', `document.querySelector('${BATCH_APPLY}').disabled === false`);
  await click(`document.querySelector('${BATCH_APPLY}').click()`);
  await wait(`window.acceptance.tasks.filter((t) => t.status === 'blocked').length === 2`);
  await settle();
  await check(
    'batch 受阻 reaches every selected card',
    `window.acceptance.tasks.every((t) => t.status === 'blocked')
      && document.querySelector('${STATUS_T3}').value === 'blocked'`,
  );
  await run(`window.acceptance.choose('${BATCH}', 'todo')`);
  await click(`document.querySelector('${BATCH_APPLY}').click()`);
  await wait(`window.acceptance.tasks.every((t) => t.status === 'todo')`);
  await settle();
  await check(
    'batch 待办 reaches every selected card',
    `window.acceptance.tasks.every((t) => t.status === 'todo')
      && document.querySelector('${STATUS_T3}').value === 'todo'`,
  );
  await shot('status-batch');

  // The create path still works after the form was generalised.
  await click(`window.acc.byText('button', '新建').click()`);
  await wait(`!!document.querySelector('input[aria-label="待办标题"]')`);
  await run(`window.acceptance.type('input[aria-label="待办标题"]', '新待办')`);
  await run(`window.acceptance.type('input[aria-label="待办标签"]', 'UI, ui, 测试')`);
  const createsBefore = await run(`window.acceptance.calls.filter((c) => c.method === 'createProjectTask').length`);
  await click(`window.acc.formButton('input[aria-label="待办标题"]', '保存待办').click()`);
  await wait(`window.acceptance.calls.filter((c) => c.method === 'createProjectTask').length === ${createsBefore} + 1`);
  await settle();
  await check(
    'creating a task still works, canonicalising labels',
    `!document.querySelector('input[aria-label="待办标题"]')
      && (() => {
        const p = window.acceptance.calls.filter((c) => c.method === 'createProjectTask').at(-1).params;
        return p.title === '新待办' && p.labels.join(',') === 'ui,测试';
      })()`,
  );
  await shot('create-form');

  // Editing and status changes throughout still run nothing.
  await check(
    'editing and status changes never start a turn or arm a goal',
    `window.acceptance.goals.length === 0 && window.acceptance.calls.every((c) => !/turn/i.test(c.method))`,
  );

  // --- 5. edit isolation across the project boundary -------------------------
  // A save parked in B must not close or clear the edit draft the reader opens in
  // A after switching -- the local half of the same fence the batch runs proved.
  await run(`window.acceptance.switchTo('B')`);
  await wait(`!!window.acc.byText('h4', 'B 任务一')`);
  await click(`document.querySelector('button[aria-label="编辑待办：B 任务一"]').click()`);
  await wait(`!!document.querySelector('${EDIT_TITLE}')`);
  await run(`window.acceptance.type('${EDIT_DESC}', 'B 的编辑草稿')`);
  await run(`window.acceptance.hold()`);
  const bUpdates = await run(`window.acceptance.calls.filter((c) => c.method === 'updateProjectTask').length`);
  await click(SAVE_EDIT);
  await wait(`window.acceptance.queueLength() === 1`);
  await run(`window.acceptance.switchTo('sample')`);
  await wait(`!!window.acc.byText('h4', '补齐多选（改）')`);
  await check("the switch closed B's edit form and cleared its draft", `!document.querySelector('${EDIT_TITLE}')`);
  await click(`document.querySelector('button[aria-label="编辑待办：补齐多选（改）"]').click()`);
  await wait(`!!document.querySelector('${EDIT_TITLE}')`);
  await run(`window.acceptance.type('${EDIT_DESC}', 'A 的新编辑草稿')`);
  await check('A now has its own edit draft', `document.querySelector('${EDIT_DESC}').value === 'A 的新编辑草稿'`);
  await run(`window.acceptance.releaseFirst()`);
  await settle();
  await settle();
  await check(
    "B's late save neither closes nor clears A's edit draft",
    `document.querySelector('${EDIT_DESC}').value === 'A 的新编辑草稿' && !!document.querySelector('${EDIT_TITLE}')`,
  );
  await check(
    'the late save was addressed to B only',
    `window.acceptance.calls.filter((c) => c.method === 'updateProjectTask').length === ${bUpdates} + 1
      && window.acceptance.calls.filter((c) => c.method === 'updateProjectTask').at(-1).params.project_id === 'B'`,
  );
  await run(`window.acceptance.resume()`);
  await click(CANCEL_EDIT);
  await shot('edit-isolation');

  // --- 6. the narrowest dock the board can be laid out in --------------------
  // The dock's content box is `min-w-[320px]`, so 320px is the real floor (a
  // narrower dock clips rather than reflows).  Edit + status + arrange all share
  // one card row, so this is where they would overflow first.
  await run(`window.acceptance.setDockWidth(320)`);
  await settle();
  await check(
    'at the narrowest dock the board and its cards do not overflow horizontally',
    `(() => {
      const board = document.querySelector('select[aria-label^="待办状态："]').closest('div.flex.h-full');
      const cards = [...document.querySelectorAll('button[aria-label^="编辑待办："]')]
        .map((b) => b.closest('.rounded-card'));
      return !!board && cards.length > 0
        && board.scrollWidth <= board.clientWidth + 2
        && cards.every((c) => c.scrollWidth <= c.clientWidth + 2);
    })()`,
  );
  await check(
    'at the narrowest dock the edit entry and status selector stay inside it',
    `(() => {
      const dock = document.querySelector('aside');
      const bounds = dock.getBoundingClientRect();
      const controls = [
        ...document.querySelectorAll('button[aria-label^="编辑待办："]'),
        ...document.querySelectorAll('select[aria-label^="待办状态："]'),
      ];
      return controls.length >= 2 && controls.every((el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.left >= bounds.left - 2 && r.right <= bounds.right + 2;
      });
    })()`,
  );
  await shot('edit-narrow-dock');
  await run(`window.acceptance.setDockWidth(400)`);

  console.log('\nmeasured contrast (WCAG, small-text floor 4.5):');
  for (const line of measured) console.log(`  ${line}`);
  console.log(`\nALL ${checks} CHECKS PASSED; screenshots: .tmp/tasks-acceptance/`);
} finally {
  client?.close();
  if (browser) await closeBrowser(browser);
  await server.close();
}
