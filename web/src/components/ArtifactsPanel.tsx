import React, { useCallback, useEffect, useState } from 'react';
import { useConsoleStore } from '../stores/useConsoleStore';
import { CodeBlock } from './CodeBlock.tsx';
import {
  ARTIFACT_CHUNK_BYTES,
  ARTIFACT_LIST_LIMIT,
  ARTIFACT_MAX_LOADED_BYTES,
  MalformedArtifactError,
  artifactLanguage,
  decodeBase64Text,
  formatBytes,
  isTextArtifact,
  parentArtifactPath,
} from '../client/artifacts.ts';
import type { ArtifactEntry } from '../client/artifacts.ts';

interface OpenFile {
  entry: ArtifactEntry;
  text: string;
  eof: boolean;
  nextOffset: number;
  loadedBytes: number;
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

/**
 * Workspace file browser: a paged directory tree plus a bounded text / diff
 * viewer, backed by the read-only `runtime.artifacts.stat/list/read` surface.
 *
 * Bounded by construction:
 * - one page per list call, paging only through an explicit "load more";
 * - one chunk per read call (`ARTIFACT_CHUNK_BYTES`), and a hard cap on the bytes
 *   held for one file (`ARTIFACT_MAX_LOADED_BYTES`) — a huge file is never read
 *   whole, and the truncation is stated instead of silently cut;
 * - every failure is rendered (list error, read error, binary refusal), so the
 *   panel never shows an empty state that looks like "no files".
 *
 * The "diff" toggle highlights added/removed lines of the loaded text; it does
 * not compute a diff between revisions (the wire surface exposes no revision
 * history), and the panel says so.
 */
export const ArtifactsPanel: React.FC<{ onClose: () => void }> = ({ onClose }) => {
  const client = useConsoleStore((s) => s.client);
  const currentSession = useConsoleStore((s) => s.currentSession);
  const paired = useConsoleStore((s) => s.pairingState === 'paired');

  const [dir, setDir] = useState('.');
  const [entries, setEntries] = useState<ArtifactEntry[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);

  const [file, setFile] = useState<OpenFile | null>(null);
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

  const openEntry = async (entry: ArtifactEntry): Promise<void> => {
    if (entry.kind === 'directory') {
      setFile(null);
      setFileError(null);
      await loadDir(entry.path);
      return;
    }
    if (!client) return;
    if (!isTextArtifact(entry.media_type, entry.path)) {
      setFile(null);
      setFileError(`二进制文件（${entry.media_type}）不读取内容`);
      return;
    }
    setFileLoading(true);
    setFileError(null);
    try {
      const chunk = await client.readArtifact(
        session,
        entry.path,
        0,
        ARTIFACT_CHUNK_BYTES,
        entry.revision,
      );
      setFile({
        entry,
        text: decodeBase64Text(chunk.data_base64),
        eof: chunk.eof,
        nextOffset: chunk.nextOffset,
        loadedBytes: chunk.byteLength,
      });
    } catch (err) {
      setFile(null);
      setFileError(describe(err));
    } finally {
      setFileLoading(false);
    }
  };

  const appendChunk = async (): Promise<void> => {
    if (!client || file === null || file.eof || fileLoading) return;
    if (file.loadedBytes >= ARTIFACT_MAX_LOADED_BYTES) return;
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

  const truncated = file !== null && !file.eof;
  const capped = truncated && file.loadedBytes >= ARTIFACT_MAX_LOADED_BYTES;

  return (
    <div
      role="dialog"
      aria-label="工作区文件"
      className="absolute right-0 top-9 z-50 flex h-[32rem] w-[52rem] flex-col rounded-md border border-gray-200 bg-white text-left shadow-xl"
    >
      <div className="flex items-center justify-between border-b border-gray-100 px-3 py-1.5">
        <span className="font-mono text-[11px] font-semibold text-gray-900">工作区文件</span>
        <div className="flex items-center gap-2">
          <span className="font-mono text-[10px] text-gray-400">{dir}</span>
          <button
            onClick={onClose}
            title="关闭 (Esc)"
            className="material-symbols-outlined cursor-pointer text-[16px] text-gray-400 hover:text-gray-700"
          >
            close
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
                void loadDir(parentArtifactPath(dir));
              }}
              className="flex items-center gap-1 disabled:text-gray-300 hover:text-gray-900"
              title="上级目录"
            >
              <span className="material-symbols-outlined text-[14px]">arrow_upward</span>
              ..
            </button>
            <span>{listLoading ? '读取中…' : `${entries.length} 项`}</span>
          </div>

          {listError !== null && (
            <div className="border-b border-red-100 bg-red-50 px-2 py-1 text-[10px] leading-relaxed text-red-700">
              目录读取失败：{listError}
            </div>
          )}

          <div className="min-h-0 flex-1 overflow-y-auto py-1">
            {entries.length === 0 && !listLoading && listError === null && (
              <div className="px-2 py-2 font-mono text-[10px] text-gray-400">空目录</div>
            )}
            {entries.map((entry) => (
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
                <span className="material-symbols-outlined text-[14px] text-gray-400">
                  {KIND_ICON[entry.kind]}
                </span>
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
                  {formatBytes(file.entry.size)} · {file.entry.media_type}
                </span>
              )}
              <button
                type="button"
                onClick={() => setDiffMode((v) => !v)}
                title="差异高亮（仅按 +/- 着色，不计算版本差异）"
                className={`rounded px-1.5 py-0.5 font-mono text-[10px] ${
                  diffMode ? 'bg-purple-50 text-purple-600' : 'text-gray-400 hover:text-gray-700'
                }`}
              >
                diff
              </button>
            </div>
          </div>

          <div className="min-h-0 flex-1 overflow-auto px-2 py-1">
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
                <CodeBlock
                  lang={artifactLanguage(file.entry.path, diffMode)}
                  code={file.text}
                />
                {truncated && (
                  <div className="mt-1 flex items-center gap-2 font-mono text-[10px] text-amber-700">
                    <span>
                      已读取 {formatBytes(file.loadedBytes)} / {formatBytes(file.entry.size)}
                      {capped ? `（达到单文件上限 ${formatBytes(ARTIFACT_MAX_LOADED_BYTES)}，停止读取）` : '（未读完）'}
                    </span>
                    {!capped && (
                      <button
                        type="button"
                        onClick={() => void appendChunk()}
                        className="text-blue-600 hover:underline"
                      >
                        读取下一块
                      </button>
                    )}
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
