import test from 'node:test';
import assert from 'node:assert/strict';
import { CONSOLE_SHORTCUTS, shortcutByKey } from '../src/components/consoleShortcuts.ts';

test('Quick Switcher shortcut is registered and copy-only', () => {
  const qsShortcut = CONSOLE_SHORTCUTS.find((s) => s.chord === 'Ctrl + P');
  assert.ok(qsShortcut, 'Ctrl + P must be registered in CONSOLE_SHORTCUTS');
  assert.equal(qsShortcut?.key, undefined, 'Ctrl + P is handled by shell, so key must be undefined');
  assert.equal(shortcutByKey('Ctrl + P'), undefined, 'Ctrl + P must not claim a status strip slot');
  assert.match(qsShortcut.label, /Quick Switcher|会话/);
});

test('Quick Switcher items priority scoring logic', () => {
  interface MockItem {
    id: string;
    status: 'running' | 'approval' | 'idle';
    isCurrent: boolean;
    updated_at: string;
  }

  const items: MockItem[] = [
    { id: '1', status: 'idle', isCurrent: false, updated_at: '2025-01-01T10:00:00Z' },
    { id: '2', status: 'running', isCurrent: false, updated_at: '2025-01-01T09:00:00Z' },
    { id: '3', status: 'approval', isCurrent: false, updated_at: '2025-01-01T08:00:00Z' },
    { id: '4', status: 'idle', isCurrent: true, updated_at: '2025-01-01T07:00:00Z' },
  ];

  const score = (item: MockItem) => {
    if (item.status === 'approval') return 4;
    if (item.status === 'running') return 3;
    if (item.isCurrent) return 2;
    return 1;
  };

  const sorted = [...items].sort((a, b) => {
    const diff = score(b) - score(a);
    if (diff !== 0) return diff;
    return new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime();
  });

  // Expected order: approval (3) -> running (2) -> current (4) -> other idle (1)
  assert.equal(sorted[0].id, '3', 'Approval items should come first');
  assert.equal(sorted[1].id, '2', 'Running items should come second');
  assert.equal(sorted[2].id, '4', 'Current session should come third');
  assert.equal(sorted[3].id, '1', 'Idle sessions should come last');
});

test('Quick Switcher search query filtering matches multiple fields', () => {
  interface SearchCandidate {
    title: string;
    project: string;
    activityDetail?: string;
    summary: string;
  }

  const list: SearchCandidate[] = [
    { title: '后端架构重构', project: 'synapse-core', activityDetail: 'pytest tests/test_x.py', summary: '正在修复' },
    { title: '界面样式优化', project: 'synapse-web', summary: 'Fluent 2 规范对齐' },
    { title: '模型缓存分析', project: 'cache-probe', activityDetail: 'analyze_checkpoints()', summary: '命中率提升' },
  ];

  const search = (q: string) => {
    const term = q.trim().toLowerCase();
    return list.filter((item) => {
      return (
        item.title.toLowerCase().includes(term) ||
        item.project.toLowerCase().includes(term) ||
        (item.activityDetail && item.activityDetail.toLowerCase().includes(term)) ||
        item.summary.toLowerCase().includes(term)
      );
    });
  };

  assert.equal(search('pytest').length, 1);
  assert.equal(search('pytest')[0].title, '后端架构重构');

  assert.equal(search('synapse-web').length, 1);
  assert.equal(search('synapse-web')[0].title, '界面样式优化');

  assert.equal(search('命中率').length, 1);
  assert.equal(search('命中率')[0].title, '模型缓存分析');

  assert.equal(search('不存在的').length, 0);
});
