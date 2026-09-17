/**
 * What the `@` flyout can offer, and how each entry reaches the model.
 *
 * Three kinds share one shape so the flyout, the keyboard walk and the pill
 * renderer never branch on the source:
 *
 * - `file`    — a workspace-relative path, resolved live through
 *               `runtime.artifacts.list` (the one mention source the runtime
 *               actually exposes to the browser).
 * - `skill`   — a repository Agent Skill.  The list below mirrors the `skills/`
 *               directory that ships with the checkout; the Python side owns the
 *               real catalog (`synapse.content.skills_catalog`, served by
 *               `/skills`), and the console has no RPC for it yet, so this is a
 *               deliberately static projection.  Adding a skill to `skills/`
 *               means adding one row here until that RPC exists.
 * - `context` — a runtime fact the turn should read (`git:diff`, the recent
 *               conversation).  Also static, for the same reason.
 *
 * The `token` is the only part that reaches the model: it is appended to the
 * prompt verbatim, which keeps the wire contract (`text` plus attachment refs)
 * exactly as it was and leaves the interpretation to the agent.
 */

import type { ComposerPillKind } from './composerDocument.ts';

/** One offerable mention. */
export interface MentionEntry {
  kind: Exclude<ComposerPillKind, 'image'>;
  /** Stable identity, used as the React key and to keep the highlight stable. */
  id: string;
  /** Visible primary label. */
  label: string;
  /** Secondary line: the full path, or what the reference stands for. */
  detail: string;
  /** Exact text appended to the prompt. */
  token: string;
}

/** The section a kind is rendered under, in flyout order. */
export const MENTION_GROUP_TITLE: Record<MentionEntry['kind'], string> = {
  file: '工作区文件',
  skill: 'Agent 技能',
  context: '运行上下文',
};

/** Short label shown at the right edge of a row. */
export const MENTION_KIND_LABEL: Record<MentionEntry['kind'], string> = {
  file: '文件',
  skill: '技能',
  context: '上下文',
};

/**
 * Repository skills, mirroring `skills/<name>/SKILL.md`.
 *
 * `token` uses the `@skill:<name>` form so the agent can tell a skill reference
 * apart from a path without guessing at a prefix convention.
 */
export const SKILL_MENTIONS: readonly MentionEntry[] = [
  {
    kind: 'skill',
    id: 'skill:cua-driver',
    label: 'cua-driver',
    detail: '操作本机 Windows 真实桌面：枚举窗口、读取无障碍树与截图、点击与输入',
    token: '@skill:cua-driver',
  },
  {
    kind: 'skill',
    id: 'skill:file-cleanup',
    label: 'file-cleanup',
    detail: '扫描并清理磁盘空间：定位大目录，区分可立即清理与需确认项',
    token: '@skill:file-cleanup',
  },
  {
    kind: 'skill',
    id: 'skill:project-session-reader',
    label: 'project-session-reader',
    detail: '读取或检索指定项目的 .synapse 会话库：列出会话、全文搜索、分页读取',
    token: '@skill:project-session-reader',
  },
  {
    kind: 'skill',
    id: 'skill:session-cache-analysis',
    label: 'session-cache-analysis',
    detail: '分析会话的 prompt 缓存命中率，区分增量未命中与整段驱逐',
    token: '@skill:session-cache-analysis',
  },
  {
    kind: 'skill',
    id: 'skill:session-crash-repair',
    label: 'session-crash-repair',
    detail: '检测并修复异常退出后残留不一致的 LangGraph checkpoint 会话',
    token: '@skill:session-crash-repair',
  },
];

/** Runtime facts a turn can be pointed at. */
export const CONTEXT_MENTIONS: readonly MentionEntry[] = [
  {
    kind: 'context',
    id: 'context:git_diff',
    label: 'git:diff',
    detail: '当前工作区尚未提交的代码变更',
    token: '@context:git_diff',
  },
  {
    kind: 'context',
    id: 'context:session_recent',
    label: 'session:recent',
    detail: '当前会话最近几轮的关键结论',
    token: '@context:session_recent',
  },
];

/** Rank a row against the typed query; `-1` means "no match". */
function score(entry: MentionEntry, needle: string): number {
  if (needle === '') return 0;
  const label = entry.label.toLowerCase();
  const detail = entry.detail.toLowerCase();
  const token = entry.token.toLowerCase();
  if (label === needle) return 100;
  if (label.startsWith(needle)) return 80;
  if (label.includes(needle)) return 60;
  if (token.includes(needle)) return 40;
  if (detail.includes(needle)) return 20;
  return -1;
}

/**
 * Filter and rank the static sources against one query.
 *
 * Files are not here: they arrive asynchronously from the runtime, so the caller
 * merges them separately (`rankMentions` is applied to the whole list at the
 * end so a file and a skill compete on the same scale).
 */
export function rankMentions(entries: readonly MentionEntry[], query: string): MentionEntry[] {
  const needle = query.trim().toLowerCase();
  return entries
    .map((entry) => ({ entry, rank: score(entry, needle) }))
    .filter((row) => row.rank >= 0)
    .sort((a, b) => b.rank - a.rank)
    .map((row) => row.entry);
}

/** Build a file mention from a workspace-relative path. */
export function fileMention(path: string): MentionEntry {
  const label = path.slice(path.lastIndexOf('/') + 1) || path;
  return { kind: 'file', id: `file:${path}`, label, detail: path, token: `@${path}` };
}

/** Build a skill mention from an RPC skill entry. */
export function skillMention(entry: {
  name: string;
  description: string;
  path: string;
  source: string;
}): MentionEntry {
  return {
    kind: 'skill',
    id: `skill:${entry.name}`,
    label: entry.name,
    detail: entry.description || entry.path,
    token: `@skill:${entry.name}`,
  };
}
