import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  CONSOLE_CSRF_HEADER,
  CONSOLE_CSRF_HEADER_VALUE,
} from '../src/client/bootstrap.ts';
import {
  TURN_SUGGESTIONS_PATH,
  fetchTurnSuggestions,
  parseTurnSuggestionsPayload,
} from '../src/client/turnSuggestions.ts';
import type { TranscriptMessage } from '../src/stores/historyMapper.ts';
import {
  buildRuleSuggestions,
  computeTurnSuggestionsFromState,
  extractLastTurnSnapshot,
  mergeSuggestedActions,
  normalizeModelSuggestions,
} from '../src/stores/turnSuggestions.ts';

test('extractLastTurnSnapshot returns null while running, waiting approval, or streaming', () => {
  const settledMessages: TranscriptMessage[] = [
    { id: 'u-1', type: 'user', timestamp: '10:00', content: '重构模块', turnId: 't-1' },
    { id: 'a-1', type: 'assistant', timestamp: '10:01', content: '已完成重构。', turnId: 't-1' },
  ];
  assert.equal(extractLastTurnSnapshot([], 'idle'), null);
  assert.equal(extractLastTurnSnapshot(settledMessages, 'running'), null);
  assert.equal(extractLastTurnSnapshot(settledMessages, 'idle', { turn_id: 't-1', actions: [] }), null);

  const streamingMessages: TranscriptMessage[] = [
    { id: 'u-1', type: 'user', timestamp: '10:00', content: '重构模块', turnId: 't-1' },
    {
      id: 'a-1',
      type: 'assistant',
      timestamp: '10:01',
      content: '正在输出...',
      streaming: true,
      turnId: 't-1',
    },
  ];
  assert.equal(extractLastTurnSnapshot(streamingMessages, 'idle'), null);
});

test('extractLastTurnSnapshot captures conclusion, changes, and test execution from the last turn', () => {
  const messages: TranscriptMessage[] = [
    { id: 'u-1', type: 'user', timestamp: '10:00', content: '第一轮问题', turnId: 't-1' },
    { id: 'a-1', type: 'assistant', timestamp: '10:01', content: '第一轮回答', turnId: 't-1' },
    { id: 'u-2', type: 'user', timestamp: '10:02', content: '修改配置并确认', turnId: 't-2' },
    { id: 'a-2-pre', type: 'assistant', timestamp: '10:02', content: '我先读取文件。', turnId: 't-2' },
    {
      id: 'tg-2',
      type: 'tool_group',
      timestamp: '10:02',
      turnId: 't-2',
      finished: true,
      tools: [
        {
          id: 'tool-1',
          callId: 'call-1',
          name: 'patch',
          category: 'edit',
          label: '更新配置文件',
          path: 'src/config.py',
          status: 'completed',
          preview: null,
          error: false,
          sub: false,
          parentId: null,
          subagentStatus: null,
          subagentName: null,
          icon: 'edit',
        },
      ],
    },
    {
      id: 'c-2',
      type: 'changes',
      timestamp: '10:03',
      turnId: 't-2',
      changesTotal: 1,
      changes: [{ path: 'src/config.py', status: 'modified', insertions: 5, deletions: 2, binary: false, reverted: false }],
    },
    {
      id: 'a-2-final',
      type: 'assistant',
      timestamp: '10:03',
      content: '已完成配置修改，是否需要我现在运行测试？',
      turnId: 't-2',
    },
  ];

  const snapshot = extractLastTurnSnapshot(messages, 'idle');
  assert.ok(snapshot !== null);
  assert.equal(snapshot.turnId, 't-2');
  assert.equal(snapshot.userPrompt, '修改配置并确认');
  assert.equal(snapshot.conclusion, '已完成配置修改，是否需要我现在运行测试？');
  assert.equal(snapshot.outcome, 'completed');
  assert.equal(snapshot.hasChanges, true);
  assert.equal(snapshot.ranTests, false);
});

test('buildRuleSuggestions produces recovery actions on failed or cancelled turns', () => {
  const messages: TranscriptMessage[] = [
    { id: 'u-1', type: 'user', timestamp: '10:00', content: '执行部署检查', turnId: 't-1' },
    {
      id: 'fail-t-1-9',
      type: 'info',
      infoLevel: 'warning',
      timestamp: '10:01',
      content: '连接超时',
      turnId: 't-1',
    },
  ];
  const snapshot = extractLastTurnSnapshot(messages, 'idle');
  assert.ok(snapshot !== null);
  assert.equal(snapshot.outcome, 'failed');

  const rules = buildRuleSuggestions(snapshot);
  assert.deepEqual(
    rules.map((r) => r.label),
    ['继续', '重试本轮'],
  );
  assert.equal(rules[1].prompt, '执行部署检查');
});

test('mergeSuggestedActions combines rules and model suggestions with deduplication and cap', () => {
  const messages: TranscriptMessage[] = [
    { id: 'u-1', type: 'user', timestamp: '10:00', content: '实现登录模块', turnId: 't-1' },
    {
      id: 'c-1',
      type: 'changes',
      timestamp: '10:01',
      turnId: 't-1',
      changes: [{ path: 'src/auth.py', status: 'modified', insertions: 20, deletions: 1, binary: false, reverted: false }],
    },
    {
      id: 'a-1',
      type: 'assistant',
      timestamp: '10:01',
      content: '登录模块已实现，请确认是否继续编写集成测试？',
      turnId: 't-1',
    },
  ];

  const { actions } = computeTurnSuggestionsFromState(
    messages,
    'idle',
    null,
    null,
    [
      // Duplicate of rule's "运行测试验证" bucket — should be deduplicated
      { label: '运行测试', prompt: '运行 pytest 测试套件', mode: 'submit' },
      // Specific contextual action from the model — should be kept
      { label: '补充边缘用例测试', prompt: '请为登录模块补充密码错误与过期 Token 的单测', mode: 'submit' },
      // Duplicate of rule's "继续" bucket — should be deduplicated
      { label: '继续执行', prompt: '继续', mode: 'submit' },
    ],
  );

  assert.ok(actions.length <= 4);
  const labels = actions.map((a) => a.label);
  assert.equal(labels[0], '确认执行');
  assert.ok(labels.includes('补充边缘用例测试'));
  // Only one test-running action should survive deduplication
  const testLabels = labels.filter((l) => l.includes('测试'));
  assert.deepEqual(testLabels, ['运行测试', '补充边缘用例测试']);
});

test('normalizeModelSuggestions and mergeSuggestedActions respect priority ordering', () => {
  const rules = [
    {
      id: 'rule-continue-default',
      label: '继续',
      prompt: '继续',
      source: 'rule' as const,
      priority: 40,
      mode: 'submit' as const,
    },
  ];
  const models = normalizeModelSuggestions([
    { label: '采用方案 A', prompt: '请按照方案 A 进行修改', mode: 'submit' },
    { label: '采用方案 B', prompt: '请按照方案 B 进行修改', mode: 'fill' },
  ]);
  const merged = mergeSuggestedActions(rules, models);
  assert.deepEqual(
    merged.map((m) => ({ label: m.label, source: m.source, mode: m.mode })),
    [
      { label: '采用方案 A', source: 'model', mode: 'submit' },
      { label: '采用方案 B', source: 'model', mode: 'fill' },
      { label: '继续', source: 'rule', mode: 'submit' },
    ],
  );
});

test('fetchTurnSuggestions sends CSRF header and degrades to empty list on error', async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const items = await fetchTurnSuggestions(
    {
      user_prompt: '分析性能瓶颈',
      conclusion: '定位到 SQL 查询缺少索引。',
      project_id: 'proj-1',
      model: 'gpt-4.1',
    },
    async (url, init) => {
      calls.push({ url: String(url), init });
      return new Response(
        JSON.stringify({
          suggestions: [
            { label: '添加索引迁移', prompt: '请生成添加该索引的迁移文件', mode: 'submit' },
            { label: '', prompt: 'invalid' },
          ],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      );
    },
  );

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, TURN_SUGGESTIONS_PATH);
  const headers = calls[0].init?.headers as Record<string, string>;
  assert.equal(headers[CONSOLE_CSRF_HEADER], CONSOLE_CSRF_HEADER_VALUE);
  assert.deepEqual(items, [
    { label: '添加索引迁移', prompt: '请生成添加该索引的迁移文件', mode: 'submit' },
  ]);

  const onError = await fetchTurnSuggestions(
    { user_prompt: 'x', conclusion: 'y' },
    async () => new Response('error', { status: 500 }),
  );
  assert.deepEqual(onError, []);
  assert.deepEqual(parseTurnSuggestionsPayload(null), []);
});
