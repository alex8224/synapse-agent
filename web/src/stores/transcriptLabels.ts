/**
 * Pure display labels for transcript rows.
 *
 * The design spec names the reasoning row explicitly (`Thought for Xs`); its
 * leading glyph is a real icon now (`thoughtIcon`), so the label carries words
 * only.  The runtime status strings are English, so they are mapped to the
 * Chinese vocabulary the rest of the console uses.  Unknown values fall back to
 * the raw string instead of being hidden.
 */
import { isHighlightedLanguage } from '../markdown/highlight.ts';
import { extensionOf } from '../runtime-client/artifacts.ts';
import type { ToolItemView } from './historyMapper.ts';

/** Human label for one runtime tool status. */
export function toolStatusLabel(status: string): string {
  const value = (status || '').toLowerCase();
  if (value === 'running') return '运行中';
  if (value === 'pending') return '等待';
  if (value === 'completed') return '完成';
  if (value === 'failed') return '失败';
  if (value === 'error') return '错误';
  if (value === 'cancelled' || value === 'canceled') return '已取消';
  return status;
}

/**
 * Read a string field out of a Python `repr` dictionary by string scanning.
 * Handles escape sequences and both single and double quotes.
 */
export function reprField(preview: string | null | undefined, keys: readonly string[]): string {
  if (!preview) return '';
  for (const key of keys) {
    let at = preview.indexOf(`'${key}': `);
    let keyLen = key.length + 4;
    if (at === -1) {
      at = preview.indexOf(`"${key}": `);
      if (at === -1) continue;
      keyLen = key.length + 4;
    }
    let index = at + keyLen;
    const quote = preview[index];
    if (quote !== "'" && quote !== '"') continue;
    index += 1;
    let value = '';
    while (index < preview.length) {
      const char = preview[index];
      if (char === '\\') {
        const escaped = preview[index + 1];
        if (escaped === undefined) break;
        value += escaped === 'n' ? '\n' : escaped === 't' ? '\t' : escaped === 'r' ? '\r' : escaped;
        index += 2;
        continue;
      }
      if (char === quote) break;
      value += char;
      index += 1;
    }
    if (value.trim() !== '') return value.trim();
  }
  return '';
}

const DETAIL_OMITTED_KEYS = new Set([
  'intent',
  'label',
  'path',
  'file_path',
  'command',
]);

/** Read the subagent's task description / prompt out of arguments or live preview. */
export function subagentPromptText(tool: ToolItemView): string {
  if (tool.args) {
    if (typeof tool.args.description === 'string' && tool.args.description.trim()) {
      return tool.args.description.trim();
    }
    if (typeof tool.args.prompt === 'string' && tool.args.prompt.trim()) {
      return tool.args.prompt.trim();
    }
  }
  if (tool.argsPreview) {
    return reprField(tool.argsPreview, ['description', 'prompt']);
  }
  return '';
}
/**
 * Display intent for a tool row, falling back to meaningful arguments (e.g. query, pattern)
 * when the model did not supply an explicit intent.
 */
export function toolDisplayIntent(tool: ToolItemView): string {
  if (tool.label && tool.label !== tool.name) {
    return tool.label;
  }
  if (tool.args && typeof tool.args === 'object' && !Array.isArray(tool.args)) {
    const args = tool.args as Record<string, unknown>;
    if (typeof args.query === 'string' && args.query.trim()) {
      return `query: ${args.query.trim()}`;
    }
    if (typeof args.pattern === 'string' && args.pattern.trim()) {
      return `pattern: ${args.pattern.trim()}`;
    }
  }
  return '';
}

/**
 * Extract structured call parameters to be inspected in detail fold.
 */
export function toolDetailParams(tool: ToolItemView): Array<{ key: string; value: string }> {
  const result: Array<{ key: string; value: string }> = [];
  if (tool.args && typeof tool.args === 'object' && !Array.isArray(tool.args)) {
    for (const [key, val] of Object.entries(tool.args as Record<string, unknown>)) {
      if (DETAIL_OMITTED_KEYS.has(key) || val === null || val === undefined) continue;
      const raw = typeof val === 'string' ? val : (JSON.stringify(val) ?? '');
      const text = raw.replace(/\s+/g, ' ').trim();
      if (!text) continue;
      result.push({
        key,
        value: text.length > 160 ? `${text.slice(0, 158)}…` : text,
      });
    }
  }
  return result;
}

/**
 * Reasoning row label.
 *
 * `duration` is `undefined` for a projected history row, `streaming` while the
 * live chain is still open, and a formatted duration once it completes.
 */
export function thoughtLabel(duration?: string): string {
  if (duration === 'streaming') return 'Thinking...';
  if (duration === undefined || duration === '' || duration === 'done') return 'Thought';
  return `Thought for ${duration}`;
}

/** Expand/collapse affordance shown next to a collapsible row label. */
export function expandHint(expanded: boolean): string {
  return expanded ? '(收起)' : '(展开)';
}

/**
 * Material Symbols glyph for a reasoning row.
 *
 * A thinking head while the chain is closed, the wired one while it is still
 * running -- the same family as the console's reasoning-level control, so the
 * row reads as "the agent's thinking" rather than as another fold chevron.
 */
export function thoughtIcon(streaming: boolean): string {
  return streaming ? 'neurology' : 'psychology';
}

/**
 * The failure reason a row should print in its detail, or `''`.
 *
 * The runtime's status is not a state word.  For a failure it is the first line of
 * the error (`summarize_tool_result`); for a success it is a body digest
 * (`ok (48 chars, 2 lines)`), which is not worth a reader's attention at all.  The
 * row itself only turns red, so this is what the opened detail shows instead.
 */
export function toolFailureReason(status: string, error: boolean): string {
  if (!error) return '';
  const text = (status ?? '').trim();
  if (text === '') return '';
  if (/^ok( \(\d+ chars, \d+ lines\))?$/.test(text)) return '';
  const lowered = text.toLowerCase();
  if (lowered === 'failed' || lowered === 'error') return '';
  if (lowered === 'cancelled' || lowered === 'canceled') return '';
  return text;
}

/** Argument keys already printed as a field of the row itself. */
const TOOL_ARG_SHOWN_ELSEWHERE = new Set(['intent', 'label']);
/** How many arguments one tool row prints before it stops. */
export const TOOL_ARG_LIMIT = 4;
/** Longest single argument value kept verbatim. */
const TOOL_ARG_VALUE_CHARS = 160;
/** Longest argument line kept in total. */
const TOOL_ARG_LINE_CHARS = 400;

/**
 * One bounded `key=value · key=value` line for a tool row's arguments.
 *
 * The row is a log line, not a place to dump a payload: an argument is whatever
 * the model wrote (a command, a patch, a whole file), so each value is collapsed
 * to one line, truncated, the count of keys is capped and the finished line is
 * capped again.  `intent` is the row's own label, so repeating it here would
 * print the same words twice.  Returns `''` when nothing is left to show.
 */
export function formatToolArgs(args: unknown, limit: number = TOOL_ARG_LIMIT): string {
  if (args === null || typeof args !== 'object' || Array.isArray(args)) return '';
  const parts: string[] = [];
  for (const [key, value] of Object.entries(args as Record<string, unknown>)) {
    if (parts.length >= Math.max(0, limit)) break;
    if (TOOL_ARG_SHOWN_ELSEWHERE.has(key) || value === null || value === undefined) continue;
    const raw = typeof value === 'string' ? value : (JSON.stringify(value) ?? '');
    const text = raw.replace(/\s+/g, ' ').trim();
    if (!text) continue;
    parts.push(`${key}=${text.length > TOOL_ARG_VALUE_CHARS ? text.slice(0, TOOL_ARG_VALUE_CHARS - 1) + '…' : text}`);
  }
  const line = parts.join(' · ');
  return line.length > TOOL_ARG_LINE_CHARS ? line.slice(0, TOOL_ARG_LINE_CHARS - 1) + '…' : line;
}

/**
 * Tool names whose result *is* a file's content (mirrors the runtime's read/edit
 * sets in `runtime/timeline.py::tool_category`).  A search or list row carries a
 * path too, but its body is a result list, not the file.
 */
const FILE_CONTENT_TOOLS = new Set([
  'read_file',
  'read',
  'read_file_lines',
  'write_file',
  'edit_file',
  'write',
  'edit',
  'patch',
  'create_file',
]);

/**
 * Extensions mapped to the language the highlighter knows them by.
 *
 * The extension itself is often already a key (`py`, `tsx`, `yaml`), but the
 * canonical name is what the code block's header prints, so the row reads
 * "python" rather than "py" and the same file never highlights under two names.
 */
const LANGUAGE_BY_EXTENSION: Record<string, string> = {
  py: 'python',
  pyi: 'python',
  js: 'javascript',
  jsx: 'javascript',
  mjs: 'javascript',
  cjs: 'javascript',
  ts: 'typescript',
  tsx: 'typescript',
  json: 'json',
  jsonl: 'json',
  sh: 'bash',
  bash: 'bash',
  zsh: 'bash',
  ps1: 'powershell',
  yaml: 'yaml',
  yml: 'yaml',
  toml: 'toml',
  ini: 'ini',
  rs: 'rust',
  go: 'go',
  java: 'java',
  c: 'c',
  h: 'c',
  cpp: 'cpp',
  cc: 'cpp',
  hpp: 'cpp',
  sql: 'sql',
  css: 'css',
  scss: 'css',
  html: 'html',
  xml: 'xml',
  diff: 'diff',
  patch: 'diff',
};

/**
 * Whether a tool result is a unified diff rather than a whole file.
 *
 * An edit result is a patch, so it highlights as one whatever the file is.  Both
 * file headers are required (or a hunk header), so a document that merely starts
 * with a `---` rule is not mistaken for a diff.
 */
function looksLikeDiff(text: string): boolean {
  const head = text.slice(0, 400);
  return /^@@ /m.test(head) || (/^--- /m.test(head) && /^\+\+\+ /m.test(head));
}

/**
 * Language a tool row's body should be highlighted as, or `''` for plain text.
 *
 * Only a file-content tool qualifies: its body is the file, so the language comes
 * from the path's extension.  A language the highlighter cannot tokenize stays
 * plain rather than being guessed at.
 */
export function toolPreviewLanguage(
  name: string,
  path: string | null | undefined,
  preview: string | null | undefined,
): string {
  if (!FILE_CONTENT_TOOLS.has((name || '').toLowerCase())) return '';
  const body = preview ?? '';
  if (body === '') return '';
  if (looksLikeDiff(body)) return 'diff';
  if (!path) return '';
  const language = LANGUAGE_BY_EXTENSION[extensionOf(path)] ?? '';
  return isHighlightedLanguage(language) ? language : '';
}

/**
 * A subagent's own tool batch, as one collapsible card.
 *
 * `parent` is the call that started the subagent (a `task` call, or the history
 * projection's row that names one); `tools` are the steps the runtime attributed
 * to it.  The batch arrives flat -- every item is just a tool call -- so this is
 * the view model that puts the nesting back.
 */
export interface SubagentToolGroup {
  type: 'subagent';
  parent: ToolItemView;
  subagentName: string;
  subagentGoal: string;
  subagentPrompt: string;
  tools: ToolItemView[];
}

/** One plain tool row of a batch, belonging to the main agent. */
export interface SingleToolItem {
  type: 'single';
  tool: ToolItemView;
}

/** What one item of a tool batch renders as. */
export type ToolRenderNode = SingleToolItem | SubagentToolGroup;

/**
 * Fold a flat tool batch into render nodes.
 *
 * A `task` call -- or, in a history projection, any row that names the subagent it
 * ran -- opens a {@link SubagentToolGroup}; every item the runtime marked as nested
 * (`sub`, or a `parentId` pointing at that call) lands inside it.  Correlation is by
 * identity first (the parent's item id / call id), then by "the group still open",
 * so a projection that dropped the link still nests its steps under the subagent
 * that was running.  Anything else is a plain row, and it closes the open group: the
 * next unattributed item belongs to the main agent again.
 */
export function groupToolsForView(tools: ToolItemView[]): ToolRenderNode[] {
  const nodes: ToolRenderNode[] = [];
  const subagentGroupsByParentId = new Map<string, SubagentToolGroup>();
  let activeSubagent: SubagentToolGroup | null = null;

  for (const t of tools) {
    if (t.name === 'task' || (t.subagentName && !t.sub)) {
      const subagentName = t.subagentName || (typeof t.args?.subagent_type === 'string' ? t.args.subagent_type : 'subagent');
      const subagentGoal = (typeof t.args?.intent === 'string' && t.args.intent)
        || (t.label && t.label !== t.name ? t.label : '')
        || (typeof t.args?.description === 'string' ? t.args.description : '')
        || '子代理任务';
      const subagentPrompt = subagentPromptText(t);

      const groupNode: SubagentToolGroup = {
        type: 'subagent',
        parent: t,
        subagentName,
        subagentGoal,
        subagentPrompt,
        tools: [],
      };
      nodes.push(groupNode);
      if (t.id) subagentGroupsByParentId.set(t.id, groupNode);
      if (t.callId) subagentGroupsByParentId.set(t.callId, groupNode);
      activeSubagent = groupNode;
      continue;
    }

    if (t.sub || t.parentId) {
      let targetGroup: SubagentToolGroup | undefined;
      if (t.parentId) {
        targetGroup = subagentGroupsByParentId.get(t.parentId);
      }
      if (!targetGroup) {
        targetGroup = activeSubagent ?? undefined;
      }
      if (targetGroup) {
        targetGroup.tools.push(t);
        continue;
      }
    }

    nodes.push({ type: 'single', tool: t });
    activeSubagent = null;
  }

  return nodes;
}

/**
 * Tool names that run a program (mirrors the runtime's `run` set in
 * `runtime/timeline.py::tool_category`).
 *
 * Their body is a command's output, not a file, so it is rendered as terminal
 * output -- escapes and all -- instead of being tokenized as source code.
 */
const RUN_TOOLS = new Set(['execute', 'run', 'shell', 'bash']);

/** True when a tool row's body is a program's terminal output. */
export function isTerminalTool(name: string): boolean {
  return RUN_TOOLS.has((name || '').toLowerCase());
}

/** Argument keys carrying a run tool's invocation (mirrors the runtime's `_CMD_KEYS`). */
const COMMAND_KEYS = ['command', 'cmd', 'code', 'script'];

/**
 * The invocation a run tool was called with, for its terminal header.
 *
 * A history row keeps the call's own args, so the command comes straight out of
 * them.  A live row has no args object -- only the bounded repr above -- so the
 * same key is read back out of that.  No command means the block paints its
 * output alone, which is what it did before the prompt line existed.
 */
export function toolCommand(tool: Pick<ToolItemView, 'args' | 'argsPreview'>): string {
  const args = tool.args;
  if (args !== null && args !== undefined && typeof args === 'object' && !Array.isArray(args)) {
    for (const key of COMMAND_KEYS) {
      const value = (args as Record<string, unknown>)[key];
      if (typeof value === 'string' && value.trim() !== '') return value.trim();
    }
    return '';
  }
  return reprField(tool.argsPreview, COMMAND_KEYS);
}
