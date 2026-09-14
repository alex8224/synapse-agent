import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Dismiss16Regular, ArrowUp16Regular, Folder16Regular, Document16Regular } from '@fluentui/react-icons';
import { useConsoleStore } from '../stores/useConsoleStore';
import { CodeBlock } from './CodeBlock.tsx';
import { FloatingPanel } from './FloatingPanel.tsx';
import {
  ARTIFACT_CHUNK_BYTES,
  ARTIFACT_HARD_MAX_BYTES,
  ARTIFACT_LIST_LIMIT,
  ARTIFACT_MAX_LOADED_BYTES,
  MalformedArtifactError,
  artifactLanguage,
  decodeBase64Text,
  filterArtifactEntries,
  formatBytes,
  isTextArtifact,
  parentArtifactPath,
} from '../client/artifacts.ts';
import { diffLines } from '../client/artifactsDiff.ts';
import type { ArtifactEntry } from '../client/artifacts.ts';

interface OpenFile {
  entry: ArtifactEntry;
  text: string;
  eof: boolean;
  nextOffset: number;
  loadedBytes: number;
  /** Revision of the chunk that produced `text`, for the diff header. */
  revision: string | null;
}

/** The content captured when the file was first opened (or re-baselined). */
interface Baseline {
  text: string;
  revision: string | null;
}

function describe(err: unknown): string {
  if (err instanceof MalformedArtifactError) return err.message;
  if (err instanceof Error && err.message) return err.message;
  return String(err);
}

const KIND_ICON: Record<ArtifactEntry['kind'], string> = {
  directory: 'folder',
  file: 'description',
};

const DIFF_CLASS: Record<'context' | 'add' | 'remove', string> = {
  context: 'text-gray-700',
  add: 'bg-green-50 text-green-800',
  remove: 'bg-red-50 text-red-800',
};

const DIFF_SIGN: Record<'context' | 'add' | 'remove', string> = {
  context: ' ',
  add: '+',
  remove: '-',
};

/**
 * Workspace file browser: a paged, filterable directory tree plus a bounded text
 * viewer and a *real* line diff, backed by the read-only
 * `runtime.artifacts.stat/list/read` surface.
 *
 * Bounded by construction:
 * - one page per list call, paging only through an explicit "load more";
 * - one chunk per read call (`ARTIFACT_CHUNK_BYTES`), automatic continuation stops
 *   at `ARTIFACT_MAX_LOADED_BYTES`, and anything beyond that needs an explicit
 *   "continue reading" click, up to `ARTIFACT_HARD_MAX_BYTES` — the panel always
 *   states the loaded range and whether the file is fully read;
 * - the path filter only narrows the entries already loaded and says so;
 * - every failure is rendered (list error, read error, binary refusal), so the
 *   panel never shows an empty state that looks like "no files".
 *
 * Diff: the wire surface exposes no revision history (`revision` is a stat
 * fingerprint), so the diff compares two versions this panel actually holds —
 * the snapshot taken when the file was opened (or last re-baselined) and the
 * current content (re-read with "重新读取" when the file changed on disk). It is
 * a real line diff, never "looks like a diff" colouring, and when the middle
 * block is too large for an exact LCS the panel says the diff is coarse.
 */
export const ArtifactsPanel: React.FC<{ anchor: HTMLElement | null; onClose: () => void }> = ({
  anchor,
  onClose,
}) => {
  const client = useConsoleStore((s) => s.client);
  const currentSession = useConsoleStore((s) => s.currentSession);
  const paired = useConsoleStore((s) => s.pairingState === 'paired');

  const [dir, setDir] = useState('.');
  const [entries, setEntries] = useState<ArtifactEntry[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [filter, setFilter] = useState('');

  const [file, setFile] = useState<OpenFile | null>(null);
  const [baseline, setBaseline] = useState<Baseline | null>(null);
  const [fileLoading, setFileLoading] = useState(false);
  const [fileError, setFileError] = useState<string | null>(null);
  const [diffMode, setDiffMode] = useState(false);

  const session = { project_id: currentSession.project_id, thread_id: currentSession.thread_id };
  const sessionKey = `${session.project_id}/${session.thread_id}`;

  const loadDir = useCallback(
    async (path: string) => {
      if (!client || !paired) return;
      setListLoading(true);
      setListError(null);
      try {
        const page = await client.listArtifacts(session, path, null, ARTIFACT_LIST_LIMIT);
        setEntries(page.entries);
        setNextCursor(page.nextCursor);
        setDir(page.path);
      } catch (err) {
        setEntries([]);
        setNextCursor(null);
        setListError(describe(err));
      } finally {
        setListLoading(false);
      }
    },
    // The session identity, not the object, is what a reload depends on.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [client, paired, sessionKey],
  );

  useEffect(() => {
    void loadDir('.');
  }, [loadDir]);

  const loadMoreEntries = async (): Promise<void> => {
    if (!client || nextCursor === null || listLoading) return;
    setListLoading(true);
    setListError(null);
    try {
      const page = await client.listArtifacts(session, dir, nextCursor, ARTIFACT_LIST_LIMIT);
      setEntries((prev) => [...prev, ...page.entries]);
      setNextCursor(page.nextCursor);
    } catch (err) {
      setListError(describe(err));
    } finally {
      setListLoading(false);
    }
  };

  /** Read the whole visible text from offset 0 again (a fresh revision). */
  const readFromStart = async (entry: ArtifactEntry, resetBaseline: boolean): Promise<void> => {
    if (!client) return;
    setFileLoading(true);
    setFileError(null);
    try {
      const chunk = await client.readArtifact(
        session,
        entry.path,
        0,
        ARTIFACT_CHUNK_BYTES,
        null,
      );
      const text = decodeBase64Text(chunk.data_base64);
      setFile({
        entry: chunk.metadata,
        text,
        eof: chunk.eof,
        nextOffset: chunk.nextOffset,
        loadedBytes: chunk.byteLength,
        revision: chunk.metadata.revision,
      });
      if (resetBaseline) {
        setBaseline({ text, revision: chunk.metadata.revision });
      }
    } catch (err) {
      setFileError(describe(err));
    } finally {
      setFileLoading(false);
    }
  };

  const openEntry = async (entry: ArtifactEntry): Promise<void> => {
    if (entry.kind === 'directory') {
      setFile(null);
      setBaseline(null);
      setFileError(null);
      await loadDir(entry.path);
      return;
    }
    if (!client) return;
    if (!isTextArtifact(entry.media_type, entry.path)) {
      setFile(null);
      setBaseline(null);
      setFileError(`二进制文件（${entry.media_type}）不读取内容`);
      return;
    }
    setDiffMode(false);
    await readFromStart(entry, true);
  };

  const appendChunk = async (): Promise<void> => {
    if (!client || file === null || file.eof || fileLoading) return;
    if (file.loadedBytes >= ARTIFACT_HARD_MAX_BYTES) {
      setFileError(
        `已达到单文件硬上限 ${formatBytes(ARTIFACT_HARD_MAX_BYTES)}，停止读取（可用「重新读取」回到开头）`,
      );
      return;
    }
    setFileLoading(true);
    setFileError(null);
    try {
      const chunk = await client.readArtifact(
        session,
        file.entry.path,
        file.nextOffset,
        ARTIFACT_CHUNK_BYTES,
        file.entry.revision,
      );
      setFile((prev) =>
        prev === null
          ? prev
          : {
              ...prev,
              text: prev.text + decodeBase64Text(chunk.data_base64),
              eof: chunk.eof,
              nextOffset: chunk.nextOffset,
              loadedBytes: prev.loadedBytes + chunk.byteLength,
            },
      );
    } catch (err) {
      setFileError(describe(err));
    } finally {
      setFileLoading(false);
    }
  };

  const filtered = useMemo(() => filterArtifactEntries(entries, filter), [entries, filter]);

  const diff = useMemo(() => {
    if (!diffMode || file === null || baseline === null) return null;
    return diffLines(baseline.text, file.text);
  }, [diffMode, file, baseline]);

  const truncated = file !== null && !file.eof;
  const softCapped = truncated && file.loadedBytes >= ARTIFACT_MAX_LOADED_BYTES;
  const hardCapped = truncated && file.loadedBytes >= ARTIFACT_HARD_MAX_BYTES;

  return (
    // Opened from the sidebar's settings row, so it expands upward from there (the
    // triggers sit at the bottom of the window).  A `FloatingPanel`, not a box in
    // the rail: see that component for why.
    <FloatingPanel
      anchor={anchor}
      label="工作区文件"
      className="flex h-[32rem] w-[56rem] max-w-[calc(100vw-2rem)] flex-col rounded-card border border-line/80 material-flyout flyout-in text-left shadow-flyout"
    >
      <div className="flex items-center justify-between border-b border-gray-100 px-3 py-1.5">
        <span className="font-mono text-[11px] font-semibold text-gray-900">工作区文件</span>
        <div className="flex items-center gap-2">
          <span className="font-mono text-[10px] text-gray-400">{dir}</span>
          <button
            onClick={onClose}
            title="关闭 (Esc)"
            className="ui-icon-button ui-compact text-gray-400 hover:text-gray-700"
          >
            <Dismiss16Regular aria-hidden="true" />
          </button>
        </div>
      </div>

      <div className="flex min-h-0 flex-1">
        {/* Directory listing */}
        <div className="flex w-72 min-w-0 shrink-0 flex-col border-r border-gray-100">
          <div className="flex items-center justify-between border-b border-gray-50 px-2 py-1 font-mono text-[10px] text-gray-500">
            <button
              type="button"
              disabled={dir === '.'}
              onClick={() => {
                setFile(null);
                setBaseline(null);
                void loadDir(parentArtifactPath(dir));
              }}
              className="flex items-center gap-1 disabled:text-gray-300 hover:text-gray-900"
              title="上级目录"
            >
              <ArrowUp16Regular aria-hidden="true" className="shrink-0" style={{ fontSize: '13px' }} />
              ..
            </button>
            <span>{listLoading ? '读取中…' : `${filtered.length}/${entries.length} 项`}</span>
          </div>

          <input
            id="artifact-path-filter"
            name="artifact-path-filter"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
            placeholder="按路径过滤…"
            title="只过滤已加载的条目；更深的目录请先「加载更多」"
            className="border-b border-gray-50 px-2 py-1 font-mono text-[10px] text-gray-700 placeholder:text-gray-300 focus:outline-none"
          />

          {listError !== null && (
            <div className="border-b border-red-100 bg-red-50 px-2 py-1 text-[10px] leading-relaxed text-red-700">
              目录读取失败：{listError}
            </div>
          )}

          <div className="fluent-scrollbar min-h-0 flex-1 overflow-y-auto py-1">
            {entries.length === 0 && !listLoading && listError === null && (
              <div className="px-2 py-2 font-mono text-[10px] text-gray-400">空目录</div>
            )}
            {entries.length > 0 && filtered.length === 0 && (
              <div className="px-2 py-2 font-mono text-[10px] text-gray-400">
                已加载的 {entries.length} 项中没有匹配「{filter.trim()}」的条目
                {nextCursor !== null ? '（还有未加载的条目）' : ''}
              </div>
            )}
            {filtered.map((entry) => (
              <div
                key={entry.path}
                onClick={() => {
                  void openEntry(entry);
                }}
                title={entry.path}
                className={`flex cursor-pointer items-center gap-1.5 px-2 py-0.5 font-mono text-[11px] hover:bg-gray-50 ${
                  file?.entry.path === entry.path ? 'bg-blue-50 text-blue-700' : 'text-gray-700'
                }`}
              >
                {entry.kind === 'directory' ? (
                  <Folder16Regular aria-hidden="true" className="shrink-0 text-gray-400" />
                ) : (
                  <Document16Regular aria-hidden="true" className="shrink-0 text-gray-400" />
                )}
                <span className="min-w-0 flex-1 truncate">
                  {entry.path.slice(entry.path.lastIndexOf('/') + 1)}
                </span>
                <span className="shrink-0 text-[10px] text-gray-400">
                  {entry.kind === 'directory' ? 'dir' : formatBytes(entry.size)}
                </span>
              </div>
            ))}
            {nextCursor !== null && (
              <button
                type="button"
                onClick={() => void loadMoreEntries()}
                className="mt-1 w-full px-2 py-1 font-mono text-[10px] text-blue-600 hover:bg-blue-50"
              >
                加载更多（已加载 {entries.length}）
              </button>
            )}
          </div>
        </div>

        {/* Viewer */}
        <div className="flex min-w-0 flex-1 flex-col">
          <div className="flex items-center justify-between border-b border-gray-50 px-3 py-1">
            <span className="truncate font-mono text-[10px] text-gray-500">
              {file === null ? '未选择文件' : file.entry.path}
            </span>
            <div className="flex items-center gap-2">
              {file !== null && (
                <span className="font-mono text-[10px] text-gray-400">
                  {formatBytes(file.entry.size)} · {file.entry.media_type} · rev{' '}
                  {file.revision === null ? '-' : file.revision.slice(0, 8)}
                </span>
              )}
              <button
                type="button"
                disabled={file === null || fileLoading}
                onClick={() => void readFromStart(file!.entry, false)}
                title="从偏移 0 重新读取（磁盘上的版本可能已变化）；不改变差异基准"
                className="rounded px-1.5 py-0.5 font-mono text-[10px] text-gray-400 hover:text-gray-700 disabled:text-gray-300"
              >
                重新读取
              </button>
              <button
                type="button"
                disabled={file === null || fileLoading}
                onClick={() => {
                  if (file !== null) setBaseline({ text: file.text, revision: file.revision });
                }}
                title="把当前内容设为差异基准（之后重新读取即可看到真实差异）"
                className="rounded px-1.5 py-0.5 font-mono text-[10px] text-gray-400 hover:text-gray-700 disabled:text-gray-300"
              >
                重设基准
              </button>
              <button
                type="button"
                onClick={() => setDiffMode((v) => !v)}
                title="与打开时的版本做真实行级差异（基线 vs 当前内容）"
                className={`rounded px-1.5 py-0.5 font-mono text-[10px] ${
                  diffMode ? 'bg-purple-50 text-purple-600' : 'text-gray-400 hover:text-gray-700'
                }`}
              >
                真实差异
              </button>
            </div>
          </div>

          <div className="fluent-scrollbar min-h-0 flex-1 overflow-auto px-2 py-1">
            {fileLoading && <div className="font-mono text-[10px] text-gray-400">读取中…</div>}
            {fileError !== null && (
              <div className="rounded border border-red-100 bg-red-50 px-2 py-1 font-mono text-[10px] leading-relaxed text-red-700">
                {fileError}
              </div>
            )}
            {file === null && fileError === null && !fileLoading && (
              <div className="font-mono text-[10px] text-gray-400">
                从左侧选择目录或文件；内容按 {formatBytes(ARTIFACT_CHUNK_BYTES)} 一块读取。
              </div>
            )}
            {file !== null && (
              <>
                {diffMode ? (
                  <div>
                    {baseline === null ? (
                      <div className="font-mono text-[10px] text-amber-700">
                        没有可比对的基准版本，请先「重设基准」。
                      </div>
                    ) : diff === null ? null : diff.identical ? (
                      <div className="font-mono text-[10px] text-gray-500">
                        与基准版本一致（基线 rev{' '}
                        {baseline.revision === null ? '-' : baseline.revision.slice(0, 8)}，当前 rev{' '}
                        {file.revision === null ? '-' : file.revision.slice(0, 8)}），无差异。
                      </div>
                    ) : (
                      <>
                        <div className="mb-1 font-mono text-[10px] text-gray-500">
                          真实行级差异：+{diff.added} / -{diff.removed}（基线 rev{' '}
                          {baseline.revision === null ? '-' : baseline.revision.slice(0, 8)} → 当前 rev{' '}
                          {file.revision === null ? '-' : file.revision.slice(0, 8)}）
                          {diff.coarse && ' · 中段过大，已按整块替换报告（非最小差异）'}
                          {diff.truncated && ' · 已达渲染上限，差异被截断'}
                        </div>
                        <pre className="fluent-scrollbar overflow-auto font-mono text-[11px] leading-4">
                          {diff.lines.map((line, index) => (
                            <div
                              key={`${index}.${line.kind}`}
                              className={`whitespace-pre ${DIFF_CLASS[line.kind]}`}
                            >
                              {`${line.baselineLine === null ? '    ' : String(line.baselineLine).padStart(4)} ${line.currentLine === null ? '    ' : String(line.currentLine).padStart(4)} ${DIFF_SIGN[line.kind]} `}
                              {line.text}
                            </div>
                          ))}
                        </pre>
                      </>
                    )}
                  </div>
                ) : (
                  <CodeBlock lang={artifactLanguage(file.entry.path, false)} code={file.text} />
                )}
                {truncated && (
                  <div className="mt-1 flex flex-wrap items-center gap-2 font-mono text-[10px] text-amber-700">
                    <span>
                      已读取 {formatBytes(file.loadedBytes)} / {formatBytes(file.entry.size)}（范围 0–
                      {file.nextOffset} 字节，未到 EOF）
                      {softCapped && `；超过自动上限 ${formatBytes(ARTIFACT_MAX_LOADED_BYTES)}，需手动继续`}
                      {hardCapped && `；已达硬上限 ${formatBytes(ARTIFACT_HARD_MAX_BYTES)}`}
                    </span>
                    {!hardCapped && (
                      <button
                        type="button"
                        onClick={() => void appendChunk()}
                        className="text-blue-600 hover:underline"
                      >
                        继续读取（+{formatBytes(ARTIFACT_CHUNK_BYTES)}）
                      </button>
                    )}
                  </div>
                )}
                {!truncated && (
                  <div className="mt-1 font-mono text-[10px] text-gray-400">
                    已读到 EOF（{formatBytes(file.loadedBytes)}，范围 0–{file.nextOffset} 字节）
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      </div>
    </FloatingPanel>
  );
};
