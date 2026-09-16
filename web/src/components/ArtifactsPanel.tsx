import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Dismiss16Regular,
  ArrowUp16Regular,
  Folder16Regular,
  Document16Regular,
  Image20Regular,
  ImageOff20Regular,
  PanelLeftContract16Regular,
  PanelLeftExpand16Regular,
} from '@fluentui/react-icons';
import { useConsoleStore } from '../stores/useConsoleStore';
import { CodeBlock } from './CodeBlock.tsx';
import { Markdown } from './Markdown.tsx';
import { FloatingPanel } from './FloatingPanel.tsx';
import { FloatingWindow } from './FloatingWindow.tsx';
import { OpenWithMenu } from './OpenWithMenu.tsx';
import {
  ARTIFACT_CHUNK_BYTES,
  ARTIFACT_HARD_MAX_BYTES,
  ARTIFACT_IMAGE_MAX_BYTES,
  ARTIFACT_LIST_LIMIT,
  ARTIFACT_MAX_LOADED_BYTES,
  MalformedArtifactError,
  artifactErrorMessage,
  artifactLanguage,
  artifactPreviewKind,
  createArtifactImageLoader,
  decodeBase64Text,
  filterArtifactEntries,
  formatBytes,
  parentArtifactPath,
} from '../client/artifacts.ts';
import { diffLines } from '../client/artifactsDiff.ts';
import type {
  ArtifactEntry,
  ArtifactImageLoader,
  ArtifactImageResource,
} from '../client/artifacts.ts';
import { locateArtifact } from '../runtime-client/artifactLocate.ts';

interface OpenFile {
  entry: ArtifactEntry;
  text: string;
  eof: boolean;
  nextOffset: number;
  loadedBytes: number;
  /** Revision of the chunk that produced `text`, for the diff header. */
  revision: string | null;
}

/** An image artifact plus the state of its bounded, blob-URL preview. */
interface OpenImage {
  entry: ArtifactEntry;
  resource: ArtifactImageResource;
}

/** The content captured when the file was first opened (or re-baselined). */
interface Baseline {
  text: string;
  revision: string | null;
}

function describe(err: unknown): string {
  if (err instanceof MalformedArtifactError) return err.message;
  if (err instanceof Error && err.message) {
    // The wire message is generic; the reason lives in `service_code`.
    const code = (err as { service_code?: string }).service_code ?? null;
    return artifactErrorMessage(code, err.message);
  }
  return String(err);
}

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
 * Viewer: the row the reader picks is selected *synchronously* (accent bar plus
 * selection tint, a real `<button>` so the keyboard reaches it), so it is already
 * marked while the content loads and stays marked when the read fails.  One
 * artifact is shown at a time, by kind (`artifactPreviewKind`):
 *
 * - text is highlighted through the shared `CodeBlock` (unchanged chunked
 *   reading, "continue reading" and the real diff);
 * - Markdown opens as a *rendered* preview (the shared `Markdown` renderer, so
 *   nothing is ever injected as markup) with a preview/source toggle;
 * - a raster image (png / jpeg / gif / webp / bmp, at most
 *   `ARTIFACT_IMAGE_MAX_BYTES`) is previewed from a blob URL resolved by the
 *   bounded, generation-guarded loader in `runtime-client/artifacts.ts`; the URL
 *   is revoked when the panel unmounts or the session changes;
 * - anything else is refused as binary and is never decoded as text.
 *
 * One token per selection (`requestRef`) fences every read: a chunk that lands
 * after the reader already picked another file is dropped instead of replacing
 * the newer selection's content.
 *
 * Diff: the wire surface exposes no revision history (`revision` is a stat
 * fingerprint), so the diff compares two versions this panel actually holds —
 * the snapshot taken when the file was opened (or last re-baselined) and the
 * current content (re-read with "重新读取" when the file changed on disk). It is
 * a real line diff, never "looks like a diff" colouring, and when the middle
 * block is too large for an exact LCS the panel says the diff is coarse.
 */
export const ArtifactsPanel: React.FC<{
  /** Anchored popover mode: the trigger box the panel hangs from. */
  anchor?: HTMLElement | null;
  /** Centered modal mode: opened from a file path the model wrote. */
  centered?: boolean;
  /** Workspace-relative POSIX path to open on mount (a bare name is located). */
  initialPath?: string | null;
  onClose: () => void;
}> = ({ anchor = null, centered = false, initialPath = null, onClose }) => {
  const client = useConsoleStore((s) => s.client);
  const currentSession = useConsoleStore((s) => s.currentSession);
  const paired = useConsoleStore((s) => s.pairingState === 'paired');
  const openExternalError = useConsoleStore((s) => s.openExternalError);
  const dismissOpenExternalError = useConsoleStore((s) => s.dismissOpenExternalError);

  const [dir, setDir] = useState('.');
  const [entries, setEntries] = useState<ArtifactEntry[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [filter, setFilter] = useState('');

  /** The row the reader picked; set before anything is read. */
  const [selected, setSelected] = useState<ArtifactEntry | null>(null);
  const [file, setFile] = useState<OpenFile | null>(null);
  const [image, setImage] = useState<OpenImage | null>(null);
  const [baseline, setBaseline] = useState<Baseline | null>(null);
  const [fileLoading, setFileLoading] = useState(false);
  const [fileError, setFileError] = useState<string | null>(null);
  const [diffMode, setDiffMode] = useState(false);
  /** Markdown opens rendered (the readable default) and toggles to source. */
  const [preview, setPreview] = useState<'rendered' | 'source'>('rendered');
  const [treeVisible, setTreeVisible] = useState(true);
  const [mobileDetail, setMobileDetail] = useState(false);

  // One token per selection: a read that started for an older file (or before
  // the panel unmounted) may not publish its result into the current view.
  const requestRef = useRef(0);
  const imageLoaderRef = useRef<ArtifactImageLoader | null>(null);

  const session = { project_id: currentSession.project_id, thread_id: currentSession.thread_id };
  const sessionKey = `${session.project_id}/${session.thread_id}`;

  const beginRequest = (): number => {
    requestRef.current += 1;
    return requestRef.current;
  };
  const isCurrent = (token: number): boolean => requestRef.current === token;

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

  // Declared before the "open the initial path" effect below: an image path
  // opened on mount needs the loader to exist by the time that effect runs.
  useEffect(() => {
    if (!client) return;
    const loader = createArtifactImageLoader({
      read: client,
      urls: {
        create: (bytes, mime) => URL.createObjectURL(new Blob([bytes.slice()], { type: mime })),
        revoke: (url) => URL.revokeObjectURL(url),
      },
    });
    imageLoaderRef.current = loader;
    // Disposing revokes every URL this loader created, so closing the window (or
    // a session change) releases the blob instead of leaking it.
    return () => {
      imageLoaderRef.current = null;
      loader.dispose();
    };
  }, [client]);

  // A result that lands after unmount must not be published either: the fence
  // outlives the component, the state update does not.
  useEffect(
    () => () => {
      requestRef.current += 1;
    },
    [],
  );

  useEffect(() => {
    if (!client || !paired) return;
    if (initialPath === null || initialPath === '') {
      void loadDir('.');
      return;
    }
    // A transcript click asked for this file: open it (locating a bare name
    // first) and show its parent directory in the tree.
    void (async () => {
      let path = initialPath;
      if (!path.includes('/')) {
        let found: string | null = null;
        try {
          found = await locateArtifact(client, session, path);
        } catch {
          found = null;
        }
        if (found === null) {
          await loadDir('.');
          setFilter(path);
          return;
        }
        path = found;
      }
      await loadDir(parentArtifactPath(path));
      try {
        const entry = await client.statArtifact(session, path);
        await openEntry(entry);
      } catch (err) {
        setFileError(describe(err));
      }
    })();
    // The session identity, not the object, is what a reload depends on.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialPath, loadDir, client, paired, sessionKey]);

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

  /**
   * Read the whole visible text from offset 0 again (a fresh revision).
   *
   * The token taken here is the fence: if the reader picks another file while
   * this read is in flight, the late chunk is dropped rather than shown under
   * the new path.
   */
  const readFromStart = async (entry: ArtifactEntry, resetBaseline: boolean): Promise<void> => {
    if (!client) return;
    const token = beginRequest();
    setFileLoading(true);
    setFileError(null);
    try {
      const chunk = await client.readArtifact(session, entry.path, 0, ARTIFACT_CHUNK_BYTES, null);
      if (!isCurrent(token)) return;
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
      if (!isCurrent(token)) return;
      setFileError(describe(err));
    } finally {
      if (isCurrent(token)) setFileLoading(false);
    }
  };

  /** Resolve one image preview through the bounded, generation-guarded loader. */
  const loadImage = async (entry: ArtifactEntry, token: number): Promise<void> => {
    const loader = imageLoaderRef.current;
    if (loader === null) {
      setImage({ entry, resource: { status: 'error', error: new Error('运行时客户端尚未就绪') } });
      return;
    }
    const resource = await loader.load(entry, session);
    if (!isCurrent(token)) return;
    setImage({ entry, resource });
  };

  /**
   * Open one tree row.
   *
   * The row is selected *before* anything is read, so the accent marker follows
   * the click (or the keyboard) immediately, while the content is still loading —
   * and stays there if the read fails.
   */
  const openEntry = async (entry: ArtifactEntry): Promise<void> => {
    setSelected(entry);
    if (entry.kind === 'directory') {
      setFile(null);
      setImage(null);
      setBaseline(null);
      setFileError(null);
      await loadDir(entry.path);
      return;
    }
    if (!client) return;
    // A directory stays in the list (drilling in re-lists it); only a file
    // swaps the phone band to the preview pane.
    setMobileDetail(true);
    const kind = artifactPreviewKind(entry.media_type, entry.path);
    setDiffMode(false);
    setPreview('rendered');
    setBaseline(null);
    setFileError(null);
    if (kind === 'image') {
      setFile(null);
      const token = beginRequest();
      // The stage shows its own loading state, so the resource is published now.
      setImage({ entry, resource: { status: 'loading' } });
      await loadImage(entry, token);
      return;
    }
    setImage(null);
    if (kind === 'binary') {
      setFile(null);
      setFileError(`二进制文件（${entry.media_type}）不读取内容`);
      return;
    }
    // Clear the previous file first: a new path is never shown over the previous
    // file's content while the first chunk is still in flight.
    setFile(null);
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
    const token = requestRef.current;
    const path = file.entry.path;
    const offset = file.nextOffset;
    const revision = file.entry.revision;
    setFileLoading(true);
    setFileError(null);
    try {
      const chunk = await client.readArtifact(
        session,
        path,
        offset,
        ARTIFACT_CHUNK_BYTES,
        revision,
      );
      if (!isCurrent(token)) return;
      setFile((prev) =>
        prev === null || prev.entry.path !== path
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
      if (!isCurrent(token)) return;
      setFileError(describe(err));
    } finally {
      if (isCurrent(token)) setFileLoading(false);
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

  /**
   * What the viewer is showing: the loaded file, else the loading/refused image,
   * else the row the reader just picked (so the header names the file it is
   * about to read instead of falling back to "no file selected").
   */
  const shownEntry =
    file?.entry ?? image?.entry ?? (selected !== null && selected.kind === 'file' ? selected : null);
  const shownKind = shownEntry === null ? null : artifactPreviewKind(shownEntry.media_type, shownEntry.path);
  const imageResource = image === null ? null : image.resource;
  const selectedPath = selected === null ? null : selected.path;

  const panel = (
    <>
      {/* The centered window supplies its own header; only the anchored popover
          draws this one. */}
      {!centered && (
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line bg-canvas px-3 py-2">
          <span className="text-[14px] font-semibold text-gray-900">工作区文件</span>
          <div className="flex min-w-0 items-center gap-2">
            <span className="truncate font-mono text-[12px] text-gray-500">{dir}</span>
            <OpenWithMenu
              path={shownEntry !== null && shownEntry.kind === 'file' ? shownEntry.path : null}
              disabledReason="先选择一个文件"
            />
            <button
              type="button"
              onClick={onClose}
              title="关闭 (Esc)"
              aria-label="关闭"
              className="ui-icon-button ui-compact text-gray-400 hover:text-gray-700"
            >
              <Dismiss16Regular aria-hidden="true" />
            </button>
          </div>
        </div>
      )}

      {openExternalError !== null && (
        <div className="flex items-center gap-2 border-b border-amber-100 bg-amber-50 px-3 py-1.5 text-[12px] leading-relaxed text-amber-800">
          <span className="min-w-0 flex-1">{openExternalError}</span>
          <button
            type="button"
            onClick={dismissOpenExternalError}
            className="ui-button ui-compact shrink-0 text-[12px]"
          >
            知道了
          </button>
        </div>
      )}

      {/* A solid content surface: the reading area is opaque, only the window
          frame and its title bar carry the theme's material. */}
      <div className="artifact-responsive-body flex min-h-0 flex-1 bg-surface" data-mobile-detail={mobileDetail}>
        {/* Directory listing */}
        <div
          className={`artifact-responsive-tree flex w-72 min-w-0 shrink-0 flex-col border-r border-line bg-canvas ${
            treeVisible ? '' : 'hidden'
          }`}
        >
          <div className="flex items-center justify-between gap-2 border-b border-line px-2 py-1.5">
            <button
              type="button"
              disabled={dir === '.'}
              onClick={() => {
                setFile(null);
                setImage(null);
                setBaseline(null);
                void loadDir(parentArtifactPath(dir));
              }}
              className="ui-button ui-compact text-[13px]"
              title="上级目录"
            >
              <ArrowUp16Regular aria-hidden="true" className="shrink-0" />
              ..
            </button>
            <span className="text-[12px] text-gray-500">
              {listLoading ? '读取中…' : `${filtered.length}/${entries.length} 项`}
            </span>
          </div>

          <div className="border-b border-line px-2 py-1.5">
            <input
              id="artifact-path-filter"
              name="artifact-path-filter"
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
              placeholder="按路径过滤…"
              title="只过滤已加载的条目；更深的目录请先「加载更多」"
              className="ui-field w-full text-[13px]"
            />
          </div>

          {listError !== null && (
            <div className="border-b border-red-100 bg-red-50 px-2 py-1.5 text-[12px] leading-relaxed text-red-700">
              目录读取失败：{listError}
            </div>
          )}

          <div className="fluent-scrollbar min-h-0 flex-1 overflow-y-auto px-1 py-1">
            {entries.length === 0 && !listLoading && listError === null && (
              <div className="px-2 py-2 text-[12px] text-gray-500">空目录</div>
            )}
            {entries.length > 0 && filtered.length === 0 && (
              <div className="px-2 py-2 text-[12px] leading-relaxed text-gray-500">
                已加载的 {entries.length} 项中没有匹配「{filter.trim()}」的条目
                {nextCursor !== null ? '（还有未加载的条目）' : ''}
              </div>
            )}
            {filtered.map((entry) => {
              const isSelected = selectedPath === entry.path;
              return (
                // A real button, not a clickable row: the keyboard reaches the
                // selection, and `ui-nav-row` draws the accent bar plus the
                // selection tint the moment the row is picked.
                <button
                  key={entry.path}
                  type="button"
                  onClick={() => {
                    void openEntry(entry);
                  }}
                  title={entry.path}
                  aria-current={isSelected ? 'true' : undefined}
                  data-selected={isSelected}
                  className={`ui-nav-row flex w-full cursor-pointer items-center gap-2 px-2 py-1 text-left ${
                    isSelected ? 'text-gray-900' : 'text-gray-700'
                  }`}
                >
                  {entry.kind === 'directory' ? (
                    <Folder16Regular
                      aria-hidden="true"
                      className={`shrink-0 ${isSelected ? 'text-blue-600' : 'text-gray-500'}`}
                    />
                  ) : (
                    <Document16Regular
                      aria-hidden="true"
                      className={`shrink-0 ${isSelected ? 'text-blue-600' : 'text-gray-500'}`}
                    />
                  )}
                  <span className="min-w-0 flex-1 truncate font-mono text-[13px]">
                    {entry.path.slice(entry.path.lastIndexOf('/') + 1)}
                  </span>
                  <span className="shrink-0 text-[12px] tabular-nums text-gray-500">
                    {entry.kind === 'directory' ? 'dir' : formatBytes(entry.size)}
                  </span>
                </button>
              );
            })}
            {nextCursor !== null && (
              <button
                type="button"
                onClick={() => void loadMoreEntries()}
                className="ui-button ui-compact mt-1 w-full text-[13px]"
              >
                加载更多（已加载 {entries.length}）
              </button>
            )}
          </div>
        </div>

        {/* Viewer */}
        <div className="artifact-responsive-preview flex min-h-0 min-w-0 flex-1 flex-col">
          <button className="list-detail-back ui-button" onClick={() => { setMobileDetail(false); setTreeVisible(true); }}>
            返回文件列表
          </button>
          {/* The toolbar wraps instead of squeezing: a narrow window keeps every
              control reachable on its own line rather than truncating labels. */}
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-line px-3 py-2">
            <span
              className="min-w-0 flex-1 truncate text-[13px] font-medium text-gray-800"
              title={shownEntry === null ? undefined : shownEntry.path}
            >
              {shownEntry === null ? '未选择文件' : shownEntry.path}
            </span>
            {shownEntry !== null && (
              <span className="shrink-0 font-mono text-[12px] text-gray-500">
                {formatBytes(shownEntry.size)} · {shownEntry.media_type} · rev{' '}
                {shownEntry.revision === null ? '-' : shownEntry.revision.slice(0, 8)}
              </span>
            )}
            {file !== null && (
              <div className="flex flex-wrap items-center gap-1">
                {shownKind === 'markdown' && !diffMode && (
                  <div
                    role="group"
                    aria-label="Markdown 显示方式"
                    className="flex items-center gap-1"
                  >
                    <button
                      type="button"
                      aria-pressed={preview === 'rendered'}
                      onClick={() => setPreview('rendered')}
                      title="渲染后的预览（默认）"
                      className="ui-button ui-compact ui-toggle text-[13px]"
                    >
                      预览
                    </button>
                    <button
                      type="button"
                      aria-pressed={preview === 'source'}
                      onClick={() => setPreview('source')}
                      title="Markdown 源码（沿用已有的语言高亮）"
                      className="ui-button ui-compact ui-toggle text-[13px]"
                    >
                      源码
                    </button>
                  </div>
                )}
                <button
                  type="button"
                  disabled={fileLoading}
                  onClick={() => void readFromStart(file.entry, false)}
                  title="从偏移 0 重新读取（磁盘上的版本可能已变化）；不改变差异基准"
                  className="ui-button ui-compact text-[13px]"
                >
                  重新读取
                </button>
                <button
                  type="button"
                  disabled={fileLoading}
                  onClick={() => setBaseline({ text: file.text, revision: file.revision })}
                  title="把当前内容设为差异基准（之后重新读取即可看到真实差异）"
                  className="ui-button ui-compact text-[13px]"
                >
                  重设基准
                </button>
                <button
                  type="button"
                  aria-pressed={diffMode}
                  onClick={() => setDiffMode((v) => !v)}
                  title="与打开时的版本做真实行级差异（基线 vs 当前内容）"
                  className="ui-button ui-compact ui-toggle text-[13px]"
                >
                  真实差异
                </button>
              </div>
            )}
          </div>

          <div className="fluent-scrollbar flex min-h-0 flex-1 flex-col overflow-auto px-2 py-1">
            {fileLoading && file === null && image === null && (
              <div className="text-[13px] text-gray-500">读取中…</div>
            )}
            {fileError !== null && (
              <div className="rounded-control border border-red-100 bg-red-50 px-2.5 py-1.5 text-[13px] leading-relaxed text-red-700">
                {fileError}
              </div>
            )}
            {file === null && image === null && fileError === null && !fileLoading && (
              <div className="text-[13px] leading-relaxed text-gray-500">
                从左侧选择目录或文件；文本按 {formatBytes(ARTIFACT_CHUNK_BYTES)} 一块读取，
                Markdown 默认渲染预览（可切换源码），{formatBytes(ARTIFACT_IMAGE_MAX_BYTES)}{' '}
                以内的 png / jpeg / gif / webp / bmp 直接预览。
              </div>
            )}
            {file !== null && (
              <>
                {diffMode ? (
                  <div className="flex min-h-0 flex-1 flex-col">
                    {baseline === null ? (
                      <div className="text-[13px] text-amber-700">
                        没有可比对的基准版本，请先「重设基准」。
                      </div>
                    ) : diff === null ? null : diff.identical ? (
                      <div className="text-[13px] leading-relaxed text-gray-600">
                        与基准版本一致（基线 rev{' '}
                        {baseline.revision === null ? '-' : baseline.revision.slice(0, 8)}，当前 rev{' '}
                        {file.revision === null ? '-' : file.revision.slice(0, 8)}），无差异。
                      </div>
                    ) : (
                      <>
                        <div className="mb-1 shrink-0 text-[12px] leading-relaxed text-gray-600">
                          真实行级差异：+{diff.added} / -{diff.removed}（基线 rev{' '}
                          {baseline.revision === null ? '-' : baseline.revision.slice(0, 8)} → 当前 rev{' '}
                          {file.revision === null ? '-' : file.revision.slice(0, 8)}）
                          {diff.coarse && ' · 中段过大，已按整块替换报告（非最小差异）'}
                          {diff.truncated && ' · 已达渲染上限，差异被截断'}
                        </div>
                        <pre className="fluent-scrollbar min-h-0 flex-1 overflow-auto font-mono text-[11px] leading-4">
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
                ) : shownKind === 'markdown' && preview === 'rendered' ? (
                  // Rendered through the shared Markdown renderer: typed nodes
                  // only, so a document can never inject markup into the window.
                  <div className="fluent-scrollbar min-h-0 flex-1 overflow-auto rounded-control border border-line bg-canvas px-3 py-2">
                    <div className="text-sm leading-relaxed text-gray-800">
                      <Markdown text={file.text} />
                    </div>
                  </div>
                ) : (
                  <CodeBlock lang={artifactLanguage(file.entry.path, false)} code={file.text} fill />
                )}
                {truncated && (
                  <div className="mt-1 flex shrink-0 flex-wrap items-center gap-2 text-[12px] leading-relaxed text-amber-700">
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
                        className="ui-button ui-compact text-[12px]"
                      >
                        继续读取（+{formatBytes(ARTIFACT_CHUNK_BYTES)}）
                      </button>
                    )}
                  </div>
                )}
                {!truncated && (
                  <div className="mt-1 shrink-0 text-[12px] text-gray-500">
                    已读到 EOF（{formatBytes(file.loadedBytes)}，范围 0–{file.nextOffset} 字节）
                  </div>
                )}
              </>
            )}
            {image !== null && imageResource !== null && (
              // The image stage: bounded, object-contain (a screenshot is never
              // cropped), on the sunken layer so the picture reads as content.
              <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-2 rounded-control border border-line bg-sunken p-3">
                {imageResource.status === 'ready' ? (
                  <img
                    src={imageResource.url}
                    alt={image.entry.path}
                    className="max-h-full max-w-full object-contain"
                  />
                ) : imageResource.status === 'loading' ? (
                  <>
                    <Image20Regular
                      aria-hidden="true"
                      className="text-gray-500"
                      style={{ fontSize: '28px' }}
                    />
                    <span className="text-[13px] text-gray-500">读取图片…</span>
                  </>
                ) : (
                  <>
                    <ImageOff20Regular
                      aria-hidden="true"
                      className="text-amber-600"
                      style={{ fontSize: '28px' }}
                    />
                    <span
                      className={`text-[13px] leading-relaxed ${
                        imageResource.status === 'error' ? 'text-red-700' : 'text-amber-700'
                      }`}
                    >
                      {imageResource.status === 'error'
                        ? describe(imageResource.error)
                        : imageResource.reason}
                    </span>
                  </>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </>
  );

  if (centered) {
    // Opened from a file path in the transcript: a movable, resizable, centered
    // window, so the file is read in place instead of docked to the sidebar.
    // It takes the *soft* scrim: this is a document window, not a picture
    // viewer, so the console behind it stays legible instead of being dimmed by
    // the image-lightbox veil (which reads far too heavy in the light theme).
    return (
      <FloatingWindow
        label="工作区文件"
        initialWidth={896}
        initialHeight={576}
        scrim="soft"
        onClose={onClose}
        title={
          <>
            <span className="text-[14px] font-semibold text-gray-900">工作区文件</span>
            <span className="truncate font-mono text-[12px] text-gray-500">{dir}</span>
          </>
        }
        actions={
          <>
            {/* The file the window is showing, opened in the reader's own program.
                A directory is not offered: locating one is the shell's job. */}
            <OpenWithMenu
              path={shownEntry !== null && shownEntry.kind === 'file' ? shownEntry.path : null}
              disabledReason={
                shownEntry !== null && shownEntry.kind === 'directory'
                  ? '目录不能用外部程序打开'
                  : '先选择一个文件'
              }
            />
            <button
              type="button"
              onClick={() => { setTreeVisible((visible) => !visible); setMobileDetail(false); }}
              title={treeVisible ? '收起文件树' : '展开文件树'}
              aria-label={treeVisible ? '收起文件树' : '展开文件树'}
              className="ui-icon-button ui-compact text-gray-400 hover:text-gray-700"
            >
              {treeVisible ? (
                <PanelLeftContract16Regular aria-hidden="true" />
              ) : (
                <PanelLeftExpand16Regular aria-hidden="true" />
              )}
            </button>
          </>
        }
      >
        {panel}
      </FloatingWindow>
    );
  }

  // Opened from the sidebar's settings row, so it expands upward from there (the
  // triggers sit at the bottom of the window).  A `FloatingPanel`, not a box in
  // the rail: see that component for why.
  return (
    <FloatingPanel
      anchor={anchor}
      label="工作区文件"
      className="flex h-[32rem] w-[56rem] max-w-[calc(100vw-2rem)] flex-col rounded-card border border-line/80 material-flyout flyout-in text-left shadow-flyout"
    >
      {panel}
    </FloatingPanel>
  );
};
