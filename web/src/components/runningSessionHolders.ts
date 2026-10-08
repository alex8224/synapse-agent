import type { SessionStatusKind } from '../stores/sessionViews.ts';

export type HolderStatus = SessionStatusKind | 'completed';

export interface RunningSessionItem {
  key: string;
  projectId: string;
  threadId: string;
  status: HolderStatus;
  isCurrent: boolean;
  title: string;
  projectName: string;
  identicon?: string;
  currentActivity?: string;
  startedAt?: number;
}

export interface SessionHolderPalette {
  bg: string;
  border: string;
  text: string;
  dot: string;
}

export const MAX_RECENT_ENDED = 5;

export interface RecentEndedSession {
  key: string;
  projectId: string;
  threadId: string;
  endedAt: number;
}

export const RECENT_ENDED_STORAGE_KEY = 'synapse:recent_ended_sessions';

export function loadRecentEndedSessions(): RecentEndedSession[] {
  try {
    if (typeof window === 'undefined' || !window.localStorage) return [];
    const raw = window.localStorage.getItem(RECENT_ENDED_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) {
      return parsed.slice(0, MAX_RECENT_ENDED);
    }
  } catch {
    // ignore
  }
  return [];
}

export function saveRecentEndedSessions(list: RecentEndedSession[]): void {
  try {
    if (typeof window === 'undefined' || !window.localStorage) return;
    window.localStorage.setItem(RECENT_ENDED_STORAGE_KEY, JSON.stringify(list.slice(0, MAX_RECENT_ENDED)));
  } catch {
    // ignore
  }
}

export function recordEndedSession(
  existing: RecentEndedSession[],
  item: { projectId: string; threadId: string; key?: string },
): RecentEndedSession[] {
  const key = item.key || `${item.projectId}:${item.threadId}`;
  const filtered = existing.filter((rec) => rec.key !== key);
  const updated: RecentEndedSession[] = [
    {
      key,
      projectId: item.projectId,
      threadId: item.threadId,
      endedAt: Date.now(),
    },
    ...filtered,
  ].slice(0, MAX_RECENT_ENDED);
  return updated;
}

/**
 * 为不同的会话 holder 分配跟随当前主题动态变化的背景色与文本对比色。
 * 严禁使用固定写死的十六进制或暗色类，所有色系均严格解析自 index.css 的 Fluent 语义化变量：
 * - fluent-light: 背景呈现柔和优雅的浅色微调层（如 rgb(235, 243, 252)），文本保持深色高对比（如 rgb(15, 108, 189)）；
 * - fluent-dark: 背景呈现沉稳内敛的深色卡片层（如 rgb(8, 35, 56)），文本呈现明亮鲜艳的高对比度色（如 rgb(71, 158, 245)）；
 * - 随主题切换瞬间自动变换，彻底解决死板固定背景无法适应多样主题色的问题。
 */
export function getSessionHolderPalette(key: string, isCurrent: boolean, status: HolderStatus): SessionHolderPalette {
  if (status === 'approval') {
    return {
      bg: 'bg-amber-50',
      border: 'border-2 border-amber-500 animate-pulse ring-1 ring-amber-500/20',
      text: 'text-amber-600',
      dot: 'bg-amber-500',
    };
  }

  let hash = 0;
  for (let i = 0; i < key.length; i++) {
    hash = (hash << 5) - hash + key.charCodeAt(i);
    hash |= 0;
  }
  const idx = Math.abs(hash) % 5;

  const palettes: SessionHolderPalette[] = [
    {
      bg: 'bg-blue-50',
      border: isCurrent ? 'border-2 border-blue-500 ring-1 ring-blue-500/25' : 'border border-blue-500/35 hover:border-blue-500',
      text: 'text-blue-500',
      dot: 'bg-blue-500',
    },
    {
      bg: 'bg-purple-50',
      border: isCurrent ? 'border-2 border-purple-500 ring-1 ring-purple-500/25' : 'border border-purple-500/35 hover:border-purple-500',
      text: 'text-purple-600',
      dot: 'bg-purple-500',
    },
    {
      bg: 'bg-green-50',
      border: isCurrent ? 'border-2 border-green-500 ring-1 ring-green-500/25' : 'border border-green-500/35 hover:border-green-500',
      text: 'text-green-600',
      dot: 'bg-green-500',
    },
    {
      bg: 'bg-red-50',
      border: isCurrent ? 'border-2 border-red-500 ring-1 ring-red-500/25' : 'border border-red-500/35 hover:border-red-500',
      text: 'text-red-600',
      dot: 'bg-red-500',
    },
    {
      bg: 'bg-surface',
      border: isCurrent ? 'border-2 border-blue-500 ring-1 ring-blue-500/25' : 'border border-line hover:border-gray-400',
      text: isCurrent ? 'text-blue-500' : 'text-gray-900',
      dot: 'bg-blue-500',
    },
  ];

  const p = palettes[idx];
  if (status === 'completed') {
    return {
      ...p,
      border: isCurrent
        ? 'border-2 border-blue-500 ring-1 ring-blue-500/25'
        : 'border border-line hover:border-blue-400',
      dot: 'bg-emerald-500',
    };
  }

  return p;
}

/**
 * 同步会话展示顺序：
 * 保持已有会话的原有相对位置不变。当会话被选中（isCurrent 变为 true）或在后台运行时，
 * 绝不会将其移到顶部或打乱既有槽位，就像固定位置的书签一样。
 * 新出现的会话追加到末尾；已结束的会话从列表中移出。
 */
export function syncSessionOrder(
  existingOrder: string[],
  activeKeys: string[],
): string[] {
  const activeSet = new Set(activeKeys);
  const result: string[] = [];
  const seen = new Set<string>();

  for (const key of existingOrder) {
    if (activeSet.has(key) && !seen.has(key)) {
      result.push(key);
      seen.add(key);
    }
  }

  for (const key of activeKeys) {
    if (!seen.has(key)) {
      result.push(key);
      seen.add(key);
    }
  }

  return result;
}

// 模块级会话出现顺序缓存，即使侧栏折叠/展开导致组件重新挂载，也保持图标位置绝对稳定
export let globalSessionOrder: string[] = [];

export function setGlobalSessionOrder(order: string[]): void {
  globalSessionOrder = order;
}

export function resetGlobalSessionOrderForTest(): void {
  globalSessionOrder = [];
}
