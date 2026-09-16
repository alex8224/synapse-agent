/**
 * The console's keyboard shortcuts, and the help rows they render.
 *
 * One table owns the *copy* of the shortcut list so the F1 help dialog, the
 * status strip's triggers and the strip's own key handling cannot drift apart:
 *
 *  - `chord` / `label` are exactly what the help dialog prints (F1),
 *  - `key` is the `KeyboardEvent.key` the *status strip* answers to, so the bar
 *    resolves a keypress to the entry that declares the same `shortcutKey`,
 *  - `title` is the trigger tooltip's text; `withChord` appends the chord, so a
 *    tooltip can never advertise a key the help list does not mention.
 *
 * The other chords (Enter, Ctrl+C, Ctrl+B, Ctrl+N, Ctrl+K, F2) are implemented
 * by the composer, the shell and the model picker; they are listed here as copy
 * only, which is why `key` is optional.  Only the keys a bottom-bar entry claims
 * are answered by the bar itself.
 *
 * Deliberately dependency-free (no React, no store) so the Node test runner can
 * exercise the table directly, like `usageView` / `goalView`.
 */

export interface ConsoleShortcut {
  /** `KeyboardEvent.key` the status strip answers to; absent = copy only. */
  key?: string;
  /** The chord the help list prints. */
  chord: string;
  /** What the shortcut does, as the help list says it. */
  label: string;
  /** Trigger tooltip without the chord; defaults to `label`. */
  title?: string;
}

/** F1 opens the help list itself. */
export const HELP_SHORTCUT_KEY = 'F1';

/**
 * The help list, in the order it renders.
 *
 * The F1 / F5 / F6 rows are also the strip's own bindings: the bar resolves a
 * keypress through this table and then finds the entry that declares the key.
 */
export const CONSOLE_SHORTCUTS: readonly ConsoleShortcut[] = [
  { chord: 'Enter', label: '发送指令 / 运行态下排队插话' },
  { chord: 'Ctrl + C', label: '中止当前运行中的轮次' },
  { chord: 'Ctrl + B', label: '展开 / 收起侧边栏' },
  { chord: 'Ctrl + N', label: '新建会话' },
  { chord: 'Ctrl + K', label: '搜索会话' },
  { key: HELP_SHORTCUT_KEY, chord: 'F1', label: '打开快捷键帮助' },
  { chord: 'F2', label: '切换大语言模型' },
  { key: 'F5', chord: 'F5', label: 'MCP 服务器', title: '管理 MCP 服务器' },
  { key: 'F6', chord: 'F6', label: '目标管理 (Goal)', title: '设置目标' },
];

/** The row registered for a `KeyboardEvent.key`, or `undefined`. */
export function shortcutByKey(key: string): ConsoleShortcut | undefined {
  return CONSOLE_SHORTCUTS.find((shortcut) => shortcut.key === key);
}

/**
 * `管理 MCP 服务器 (F5)`: the tooltip a trigger advertises.
 *
 * Falls back to the plain text when the key is not registered, so a typo loses
 * the chord instead of printing `undefined`.
 */
export function withChord(text: string, key: string): string {
  const shortcut = shortcutByKey(key);
  return shortcut === undefined ? text : `${text} (${shortcut.chord})`;
}

/** The help dialog's rows, straight from the table (one source of copy). */
export function helpRows(): Array<{ keys: string; label: string }> {
  return CONSOLE_SHORTCUTS.map((shortcut) => ({ keys: shortcut.chord, label: shortcut.label }));
}
