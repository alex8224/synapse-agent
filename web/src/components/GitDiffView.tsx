import React, { useMemo, useState } from 'react';
import {
  ColumnSingle16Regular,
  LayoutColumnTwo16Regular,
  ChevronDown16Regular,
  ChevronRight16Regular,
  Info16Regular,
} from '@fluentui/react-icons';
import type { GitDiffView as GitDiffViewData } from '../runtime-client/git.ts';
import {
  parseUnifiedDiff,
  buildSplitHunkRows,
  type DiffLine,
  type DiffHunk,
} from '../runtime-client/gitDiffParser.ts';

export interface GitDiffViewProps {
  diff: GitDiffViewData | null;
  diffError: string | null;
  selectedPath: string | null;
}

/**
 * Badge for a line whose side of the diff has no trailing newline.
 *
 * git reports it as its own `\ No newline at end of file` line, but that line
 * annotates the line above it -- the parser folds it onto that line as
 * `noNewline`, and this badge makes the difference visible without a floating
 * marker the reader has to map back to a line.
 *
 * The palette is the theme's, not Tailwind's `dark:` variant: the console swaps
 * themes through `data-theme="fluent-dark"` (not a `dark` class), so `dark:*`
 * never fires.  `text-blue-700` / `bg-blue-500/10` resolve through the theme
 * contract and stay legible in both themes.
 */
const NoNewlineBadge: React.FC = () => (
  <span className="ml-2 inline-flex shrink-0 select-none items-center gap-0.5 rounded-control border border-blue-500/30 bg-blue-500/10 px-1.5 text-[10px] font-medium leading-4 text-blue-700">
    <span aria-hidden="true">⏎</span>
    缺少末尾换行符
  </span>
);

function renderCodeContent(line: DiffLine): React.ReactNode {
  if (line.wordParts && line.wordParts.length > 0) {
    return (
      <>
        {line.wordParts.map((part, index) => {
          if (part.type === 'added') {
            return (
              <mark
                key={index}
                className="rounded-control bg-green-500/25 px-0.5 text-green-700"
              >
                {part.text}
              </mark>
            );
          }
          if (part.type === 'removed') {
            return (
              <mark
                key={index}
                className="rounded-control bg-red-500/25 px-0.5 text-red-700"
              >
                {part.text}
              </mark>
            );
          }
          return <span key={index}>{part.text}</span>;
        })}
      </>
    );
  }
  return line.text === '' ? ' ' : line.text;
}

export const GitDiffView: React.FC<GitDiffViewProps> = ({
  diff,
  diffError,
  selectedPath,
}) => {
  const [viewMode, setViewMode] = useState<'unified' | 'split'>('unified');
  const [showHeader, setShowHeader] = useState(false);

  const parsed = useMemo(() => {
    if (diff === null || diff.empty || diff.binary) return null;
    return parseUnifiedDiff(diff.text);
  }, [diff]);

  if (selectedPath === null) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-xs text-gray-400">
        选择一个文件查看 diff。
      </div>
    );
  }

  if (diffError !== null) {
    return (
      <div className="p-4 text-xs leading-relaxed text-amber-700">
        {diffError}
      </div>
    );
  }

  if (diff === null) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-xs text-gray-400">
        加载中…
      </div>
    );
  }

  if (diff.empty) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-xs text-gray-400">
        没有差异（该文件与所选基线一致；可用上方「暂存区」比较另一侧）。
      </div>
    );
  }

  if (diff.binary) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-xs text-gray-500">
        二进制文件，不显示 diff。
      </div>
    );
  }

  if (parsed === null || parsed.hunks.length === 0) {
    return (
      <div className="p-4 font-mono text-xs text-gray-400">
        {diff.text ? (
          <pre className="whitespace-pre">{diff.text}</pre>
        ) : (
          '没有可展示的差异代码块。'
        )}
      </div>
    );
  }

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden bg-canvas">
      {/* Diff Controls & Statistics Bar.  The path is the flexible column: it
          truncates (with the full path in `title`) so a deep workspace path can
          never push the view toggle off the right edge. */}
      <div className="flex shrink-0 select-none items-center gap-2 border-b border-line/70 bg-surface px-3 py-1.5">
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <span
            className="min-w-0 truncate font-mono text-xs font-semibold text-gray-800"
            title={selectedPath}
          >
            {selectedPath}
          </span>
          <div className="flex shrink-0 items-center gap-1 font-mono text-[11px]">
            {parsed.addedCount > 0 && (
              <span className="rounded-control bg-green-500/10 px-1.5 py-0.5 font-medium text-green-700">
                +{parsed.addedCount}
              </span>
            )}
            {parsed.removedCount > 0 && (
              <span className="rounded-control bg-red-500/10 px-1.5 py-0.5 font-medium text-red-700">
                -{parsed.removedCount}
              </span>
            )}
          </div>
        </div>

        <div className="flex shrink-0 items-center gap-2">
          {parsed.headerLines.length > 0 && (
            <button
              type="button"
              onClick={() => setShowHeader((v) => !v)}
              className="flex items-center gap-1 rounded-control px-1.5 py-0.5 text-[11px] text-gray-500 hover:bg-surface-hover"
              title="查看 Git 原始头部信息"
            >
              <Info16Regular className="h-3.5 w-3.5" />
              <span>文件头</span>
              {showHeader ? (
                <ChevronDown16Regular className="h-3 w-3" />
              ) : (
                <ChevronRight16Regular className="h-3 w-3" />
              )}
            </button>
          )}

          {/* View mode toggle: Unified vs Split */}
          <div
            role="group"
            aria-label="Diff 视图模式"
            className="flex items-center rounded-control border border-line/60 bg-sunken/60 p-0.5"
          >
            <button
              type="button"
              onClick={() => setViewMode('unified')}
              aria-pressed={viewMode === 'unified'}
              className={`flex items-center gap-1 rounded-control px-2 py-0.5 text-[11px] transition-colors ${
                viewMode === 'unified'
                  ? 'bg-surface font-medium text-gray-900'
                  : 'text-gray-500 hover:text-gray-800'
              }`}
              title="统一视图 (Unified)"
            >
              <ColumnSingle16Regular className="h-3.5 w-3.5" />
              <span>统一</span>
            </button>
            <button
              type="button"
              onClick={() => setViewMode('split')}
              aria-pressed={viewMode === 'split'}
              className={`flex items-center gap-1 rounded-control px-2 py-0.5 text-[11px] transition-colors ${
                viewMode === 'split'
                  ? 'bg-surface font-medium text-gray-900'
                  : 'text-gray-500 hover:text-gray-800'
              }`}
              title="分栏视图 (Split / Side-by-side)"
            >
              <LayoutColumnTwo16Regular className="h-3.5 w-3.5" />
              <span>分栏</span>
            </button>
          </div>
        </div>
      </div>

      {/* Raw Git Header Box (collapsible) */}
      {showHeader && parsed.headerLines.length > 0 && (
        <div className="shrink-0 border-b border-line/60 bg-sunken/60 p-2 text-[10px] text-gray-600">
          <pre className="font-mono whitespace-pre leading-relaxed">
            {parsed.headerLines.join('\n')}
          </pre>
        </div>
      )}

      {/* Diff Content Scroll Area */}
      <div className="fluent-scrollbar min-h-0 flex-1 overflow-auto">
        <div className={viewMode === 'unified' ? 'min-w-full w-max' : 'w-full'}>
          {parsed.hunks.map((hunk) => (
            <HunkBlock key={hunk.id} hunk={hunk} viewMode={viewMode} />
          ))}
        </div>

        {diff.truncated && (
          <div className="border-t border-amber-500/30 bg-amber-500/10 px-3 py-1.5 text-[11px] text-amber-700">
            diff 超过服务端单文件上限，已截断。
          </div>
        )}
      </div>
    </div>
  );
};

const HunkBlock: React.FC<{ hunk: DiffHunk; viewMode: 'unified' | 'split' }> = ({
  hunk,
  viewMode,
}) => {
  const splitRows = useMemo(() => {
    if (viewMode !== 'split') return [];
    return buildSplitHunkRows(hunk);
  }, [hunk, viewMode]);

  return (
    <div className="border-b border-line/40 last:border-b-0">
      {/* Hunk Header Banner.  An opaque canvas sits under the blue tint so the
          rows scrolling behind the sticky banner cannot bleed through it. */}
      <div className="sticky top-0 z-10 select-none border-y border-line/60 bg-canvas">
        <div className="flex items-center gap-2 bg-blue-500/10 px-3 py-0.5 font-mono text-[11px] text-blue-700">
          <span className="font-semibold">
            @@ -{hunk.oldStart}
            {hunk.oldCount !== 1 ? `,${hunk.oldCount}` : ''} +{hunk.newStart}
            {hunk.newCount !== 1 ? `,${hunk.newCount}` : ''} @@
          </span>
          {hunk.heading && (
            <span className="truncate text-blue-700/80">{hunk.heading}</span>
          )}
        </div>
      </div>

      {viewMode === 'unified' ? (
        /* Unified View */
        <div className="divide-y divide-line/10">
          {hunk.lines.map((line) => {
            const isAdd = line.type === 'addition';
            const isDel = line.type === 'deletion';

            const rowBg = isAdd
              ? 'bg-green-500/10 hover:bg-green-500/15'
              : isDel
                ? 'bg-red-500/10 hover:bg-red-500/15'
                : 'hover:bg-surface-hover';

            const signColor = isAdd
              ? 'text-green-700 font-semibold'
              : isDel
                ? 'text-red-700 font-semibold'
                : 'text-gray-400';

            return (
              <div
                key={line.id}
                className={`flex font-mono text-[11px] leading-5 transition-colors ${rowBg}`}
              >
                {/* Old line number */}
                <span className="w-11 shrink-0 select-none border-r border-line/30 pr-2 text-right text-gray-400/80">
                  {line.oldLineNumber ?? ''}
                </span>
                {/* New line number */}
                <span className="w-11 shrink-0 select-none border-r border-line/30 pr-2 text-right text-gray-400/80">
                  {line.newLineNumber ?? ''}
                </span>
                {/* Sign (+ / - / space) */}
                <span className={`w-5 shrink-0 select-none text-center ${signColor}`}>
                  {isAdd ? '+' : isDel ? '-' : ' '}
                </span>
                {/* Code text */}
                <span className="min-w-0 flex-1 whitespace-pre pl-1 pr-2 text-gray-900">
                  {renderCodeContent(line)}
                  {line.noNewline && <NoNewlineBadge />}
                </span>
              </div>
            );
          })}
        </div>
      ) : (
        /* Fixed half-width panes wrap long lines (including unbroken tokens).
           Both cells share a row, so wrapping preserves old/new alignment
           without painting text across the other pane. */
        <div className="divide-y divide-line/10">
          {splitRows.map((row) => {
            const left = row.left;
            const right = row.right;

            const leftBg = left?.type === 'deletion' ? 'bg-red-500/10' : '';
            const rightBg = right?.type === 'addition' ? 'bg-green-500/10' : '';

            return (
              <div
                key={row.id}
                className="flex font-mono text-[11px] leading-5 hover:bg-surface-hover"
              >
                {/* Left pane: baseline/old */}
                <div
                  className={`flex w-1/2 min-w-0 border-r border-line/60 ${leftBg}`}
                >
                  {left ? (
                    <>
                      <span className="w-11 shrink-0 select-none border-r border-line/30 pr-2 text-right text-gray-400/80">
                        {left.oldLineNumber ?? ''}
                      </span>
                      <span
                        className={`w-5 shrink-0 select-none text-center ${
                          left.type === 'deletion' ? 'font-semibold text-red-700' : 'text-gray-400'
                        }`}
                      >
                        {left.type === 'deletion' ? '-' : ' '}
                      </span>
                      <span className="min-w-0 flex-1 whitespace-pre-wrap [overflow-wrap:anywhere] pl-1 pr-2 text-gray-900">
                        {renderCodeContent(left)}
                        {left.noNewline && <NoNewlineBadge />}
                      </span>
                    </>
                  ) : (
                    <div className="h-full w-full select-none bg-sunken/60" />
                  )}
                </div>

                {/* Right pane: current/new */}
                <div className={`flex w-1/2 min-w-0 ${rightBg}`}>
                  {right ? (
                    <>
                      <span className="w-11 shrink-0 select-none border-r border-line/30 pr-2 text-right text-gray-400/80">
                        {right.newLineNumber ?? ''}
                      </span>
                      <span
                        className={`w-5 shrink-0 select-none text-center ${
                          right.type === 'addition' ? 'font-semibold text-green-700' : 'text-gray-400'
                        }`}
                      >
                        {right.type === 'addition' ? '+' : ' '}
                      </span>
                      <span className="min-w-0 flex-1 whitespace-pre-wrap [overflow-wrap:anywhere] pl-1 pr-2 text-gray-900">
                        {renderCodeContent(right)}
                        {right.noNewline && <NoNewlineBadge />}
                      </span>
                    </>
                  ) : (
                    <div className="h-full w-full select-none bg-sunken/60" />
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};
