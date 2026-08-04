const root = document.documentElement;
const themeButton = document.querySelector('.theme-switch');
const toast = document.querySelector('#toast');
const viewTitle = document.querySelector('#view-title');
const panels = [...document.querySelectorAll('.panel-view')];
const navItems = [...document.querySelectorAll('[data-view]')];
const nodes = {
  architecture: { name: 'Architecture Scan', profile: 'researcher', model: 'gpt-5-mini', timeout: 180, prompt: '分析 {{input.objective}} 的架构边界、关键模块和调用链。给出带文件路径的证据，并返回结构化 finding-set/v1 artifact。', tools: ['read_file', 'search_files', 'find_files'] },
  tests: { name: 'Test Signal', profile: 'tester', model: 'gpt-5-mini', timeout: 240, prompt: '定位与 {{input.objective}} 相关的测试，先运行最窄验证。报告失败根因、命令和最小修复建议。', tools: ['read_file', 'search_files', 'find_files', 'execute'] },
  security: { name: 'Security Review', profile: 'security', model: 'claude-sonnet', timeout: 180, prompt: '审查来自 Architecture Scan 的边界和数据流。关注权限、凭据、输入验证和危险 shell 操作。', tools: ['read_file', 'search_files', 'find_files'] },
  synthesis: { name: 'Evidence Report', profile: 'writer', model: 'gpt-5', timeout: 120, prompt: '汇总上游 artifacts。按风险、证据和建议形成一份简洁审计报告；不要复制原始工具输出。', tools: ['read_file', 'find_files'] },
};
let selectedId = 'architecture';
let running = false;
let runStarted = 0;
let events = [];

function showToast(text) { toast.textContent = text; toast.classList.add('show'); window.setTimeout(() => toast.classList.remove('show'), 1800); }
function setView(view) {
  panels.forEach((panel) => panel.classList.toggle('hidden', panel.dataset.panel !== view));
  document.querySelectorAll('.nav-item').forEach((item) => item.classList.toggle('active', item.dataset.view === view));
  const labels = { workflow: 'Repository Audit', agents: 'Agent Registry', runs: '运行记录', artifacts: 'Artifacts' };
  viewTitle.innerHTML = `${labels[view]} ${view === 'workflow' ? '<span>v0.3</span>' : ''}`;
  if (view === 'agents') renderAgents();
  if (view === 'runs') renderEvents();
}
navItems.forEach((item) => item.addEventListener('click', () => setView(item.dataset.view)));

themeButton.addEventListener('click', () => {
  root.dataset.theme = root.dataset.theme === 'dark' ? 'light' : 'dark';
  document.querySelector('meta[name="theme-color"]').content = root.dataset.theme === 'dark' ? '#0a1012' : '#edf2ed';
});

const inspectorName = document.querySelector('#inspector-name');
const agentSelect = document.querySelector('#agent-select');
const modelSelect = document.querySelector('#model-select');
const timeoutInput = document.querySelector('#timeout-input');
const promptInput = document.querySelector('#prompt-input');
const promptCount = document.querySelector('#prompt-count');
const toolChips = [...document.querySelectorAll('.tool-chip')];
function renderInspector() {
  const node = nodes[selectedId];
  inspectorName.textContent = node.name;
  agentSelect.value = node.profile; modelSelect.value = node.model; timeoutInput.value = node.timeout; promptInput.value = node.prompt;
  promptCount.textContent = `${node.prompt.length} chars`;
  toolChips.forEach((chip) => { const active = node.tools.includes(chip.textContent); chip.classList.toggle('enabled', active); chip.classList.toggle('disabled', !active); });
}
function markDirty() { const state = document.querySelector('#save-state'); state.innerHTML = '<i></i>未保存'; state.classList.add('dirty'); }
function selectNode(id) {
  if (!nodes[id]) return;
  selectedId = id;
  document.querySelectorAll('.agent-node').forEach((node) => node.classList.toggle('selected', node.dataset.node === id));
  renderInspector();
}
document.querySelectorAll('.node').forEach((node) => node.addEventListener('click', (event) => { if (event.target.closest('.node-menu')) return; selectNode(node.dataset.node); }));
promptInput.addEventListener('input', () => { nodes[selectedId].prompt = promptInput.value; promptCount.textContent = `${promptInput.value.length} chars`; markDirty(); });
agentSelect.addEventListener('change', () => { nodes[selectedId].profile = agentSelect.value; markDirty(); });
modelSelect.addEventListener('change', () => { nodes[selectedId].model = modelSelect.value; document.querySelector(`[data-node="${selectedId}"] footer span`).textContent = modelSelect.value; markDirty(); });
timeoutInput.addEventListener('input', () => { nodes[selectedId].timeout = Number(timeoutInput.value); markDirty(); });
toolChips.forEach((chip) => chip.addEventListener('click', () => { const tool = chip.textContent; const list = nodes[selectedId].tools; const index = list.indexOf(tool); if (index >= 0) list.splice(index, 1); else list.push(tool); renderInspector(); markDirty(); }));
document.querySelector('#save-node').addEventListener('click', () => { const state = document.querySelector('#save-state'); state.innerHTML = '<i></i>已保存'; state.classList.remove('dirty'); showToast(`${nodes[selectedId].name} 配置已保存`); });
document.querySelector('#tool-policy').addEventListener('click', () => showToast('原型：此处将打开 Tool Policy 编辑器'));
document.querySelector('#delete-node').addEventListener('click', () => showToast('原型保留核心节点，未执行删除'));
document.querySelector('#fit-view').addEventListener('click', () => { document.querySelectorAll('.agent-node').forEach((node) => node.classList.remove('selected')); selectNode('architecture'); showToast('画布已重置'); });
document.querySelector('#add-node').addEventListener('click', () => showToast('原型：将从 Registry 拖入新的 Agent 节点'));

function renderAgents() {
  const colors = ['cyan', 'violet', 'orange', 'lime'];
  const descriptions = ['代码库探索与证据采集', '聚焦测试、回归和验证', '安全、权限与危险操作审查', '汇聚 artifact，输出结构化报告'];
  document.querySelector('#agent-grid').innerHTML = Object.entries(nodes).map(([id, agent], index) => `<article class="agent-card" data-agent="${id}"><header><span class="node-avatar ${colors[index]}">${agent.name[0]}</span><div><h3>${agent.profile}</h3><p>${agent.name}</p></div></header><p>${descriptions[index]}</p><footer><span>${agent.model}</span><span>${agent.tools.length} tools · ${agent.timeout}s</span></footer></article>`).join('');
  document.querySelectorAll('.agent-card').forEach((card) => card.addEventListener('click', () => { selectNode(card.dataset.agent); setView('workflow'); showToast('已在画布中定位对应节点'); }));
}
document.querySelector('#new-agent').addEventListener('click', () => showToast('原型：打开 Agent Spec 向导'));

function addEvent(node, status, detail) { events.unshift({ time: new Date().toLocaleTimeString('zh-CN', { hour12: false }), node, status, detail }); renderEvents(); }
function renderEvents() {
  const log = document.querySelector('#event-log');
  if (!events.length) { log.innerHTML = '<div class="empty-state">尚无运行事件。点击“运行工作流”查看统一事件流。</div>'; return; }
  log.innerHTML = events.map((event) => `<div class="event-row"><time>${event.time}</time><span class="event-type">${event.node}</span><span>${event.detail}</span><span class="event-status ${event.status}">${event.status}</span></div>`).join('');
}
function updateRunLane(index, status, label) {
  const lane = document.querySelectorAll('.run-lane')[index]; if (!lane) return;
  const dot = lane.querySelector('.lane-dot'); dot.className = `lane-dot ${status}`; lane.querySelector('b').textContent = label;
}
function finishRun() {
  running = false; document.querySelector('#run-button').disabled = false; document.querySelector('#run-button').textContent = '▶ 运行工作流';
  document.querySelector('.pulse').classList.remove('running'); document.querySelector('#runtime-time').textContent = '完成于刚刚';
  document.querySelector('#summary-status').textContent = '运行成功'; document.querySelector('#summary-time').textContent = '12.8s'; document.querySelector('#summary-tokens').textContent = '8,642'; document.querySelector('#summary-tools').textContent = '11'; document.querySelector('#summary-artifacts').textContent = '4';
  addEvent('workflow', 'ok', 'repository-audit 成功完成，生成 4 个结构化 artifacts'); showToast('工作流运行完成');
}
function runWorkflow() {
  if (running) return; running = true; events = []; runStarted = Date.now();
  document.querySelector('#run-button').disabled = true; document.querySelector('#run-button').textContent = '运行中…'; document.querySelector('.pulse').classList.add('running'); document.querySelector('#runtime-time').textContent = '正在执行';
  document.querySelectorAll('.lane-dot').forEach((dot) => dot.className = 'lane-dot idle'); document.querySelectorAll('.run-lane b').forEach((label, index) => label.textContent = index < 2 ? '已排队' : '等待依赖');
  addEvent('workflow', 'running', '已校验 DAG、Agent Profile、工具策略和资源预算');
  const stages = [
    [0, 'running', '运行中', 'architecture', 'running', 'Architecture Scan 已启动（gpt-5-mini）'],
    [1, 'running', '运行中', 'tests', 'running', 'Test Signal 已启动（并行执行）'],
    [0, 'done', '完成 · 3.4s', 'architecture', 'ok', '生成 architecture-findings.json，含 4 条证据'],
    [1, 'done', '完成 · 6.8s', 'tests', 'ok', '目标测试完成，生成 test-signal.json'],
    [2, 'running', '运行中', 'security', 'running', 'Security Review 已获得 architecture artifact'],
    [2, 'done', '完成 · 2.6s', 'security', 'ok', 'Security Review 完成，未发现阻断性问题'],
    [3, 'running', '汇聚中', 'synthesis', 'running', 'Evidence Report 正在消费 3 个上游 artifacts'],
    [3, 'done', '完成 · 1.9s', 'synthesis', 'ok', '生成最终审计报告'],
  ];
  stages.forEach((stage, index) => window.setTimeout(() => { updateRunLane(stage[0], stage[1], stage[2]); addEvent(stage[3], stage[4], stage[5]); if (index === stages.length - 1) finishRun(); }, 600 + index * 670));
}
document.querySelector('#run-button').addEventListener('click', runWorkflow);
document.querySelector('#open-run').addEventListener('click', () => setView('runs'));
document.querySelector('#clear-events').addEventListener('click', () => { events = []; renderEvents(); showToast('演示事件已清空'); });

const yamlModal = document.querySelector('#yaml-modal');
const yamlOutput = document.querySelector('#yaml-output');
function workflowYAML() { return `id: repository-audit\nversion: 0.3\nruntime:\n  max_parallel: 2\n  failure_policy: block_downstream\nnodes:\n${Object.entries(nodes).map(([id, n]) => `  - id: ${id}\n    agent: ${n.profile}\n    model: ${n.model}\n    timeout_seconds: ${n.timeout}\n    tools: [${n.tools.join(', ')}]\n    prompt: >\n      ${n.prompt}`).join('\n')}\nedges:\n  - [architecture, security]\n  - [tests, synthesis]\n  - [security, synthesis]`; }
document.querySelector('#export-button').addEventListener('click', () => { yamlOutput.textContent = workflowYAML(); yamlModal.showModal(); });
document.querySelectorAll('.close-modal').forEach((button) => button.addEventListener('click', () => yamlModal.close()));
document.querySelector('#copy-yaml').addEventListener('click', async () => { try { await navigator.clipboard.writeText(yamlOutput.textContent); showToast('YAML 已复制'); } catch { showToast('浏览器未允许剪贴板访问'); } });

renderInspector(); renderEvents();
