import React, { useEffect, useRef, useState } from 'react';
import { Portal } from '../Portal.tsx';
import { MENTION_GROUP_TITLE, MENTION_KIND_LABEL, type MentionEntry } from './mentionCatalog.ts';
import { mentionOptionId } from './mentionOption.ts';

export interface MentionFlyoutProps {
  /** The rectangle of the typed `@`, in viewport coordinates. */
  anchorRect: DOMRect;
  entries: readonly MentionEntry[];
  activeIndex: number;
  /** Whether the file source is still being read (a loading line, not "none"). */
  loading: boolean;
  /** A read failure for the file source, shown as a plain line. */
  error: string | null;
  onPick: (entry: MentionEntry) => void;
  onHoverIndex: (index: number) => void;
}

/**
 * The `@` suggestion list.
 *
 * It is portalled and anchored to the `@` the reader typed, not to the editor:
 * the composer is a short card floating over the transcript, so a list laid out
 * inside it would be clipped by the editor's own scroll box.  Placement is 8px
 * above the `@` with a flip below when the viewport has no room, which is what
 * keeps it readable whether the composer sits at the bottom of a tall window or
 * under a short one.
 *
 * Rows are grouped by kind with a caption, exactly like the console's other
 * Fluent lists, and the active row is marked the way the navigation rows are
 * (`ui-nav-row`'s accent capsule).  Pointer *down* is what picks a row: taking
 * focus on mouse down would collapse the editor's selection and lose the `@`
 * range the pick has to replace.
 */
export const MentionFlyout: React.FC<MentionFlyoutProps> = ({
  anchorRect,
  entries,
  activeIndex,
  loading,
  error,
  onPick,
  onHoverIndex,
}) => {
  const boxRef = useRef<HTMLDivElement | null>(null);
  const [position, setPosition] = useState<{ top: number; left: number } | null>(null);

  useEffect(() => {
    const box = boxRef.current;
    const width = box?.offsetWidth ?? 340;
    const height = box?.offsetHeight ?? 240;
    const gap = 8;
    const margin = 12;

    let top = anchorRect.top - height - gap;
    if (top < margin) top = anchorRect.bottom + gap;
    let left = anchorRect.left;
    if (left + width > window.innerWidth - margin) left = window.innerWidth - width - margin;
    if (left < margin) left = margin;
    setPosition({ top: Math.round(top), left: Math.round(left) });
  }, [anchorRect, entries.length, loading, error]);

  // Keep the active row inside the list while the arrows walk past the fold.
  useEffect(() => {
    const box = boxRef.current;
    if (box === null) return;
    const active = box.querySelector<HTMLElement>('[data-active="true"]');
    active?.scrollIntoView({ block: 'nearest' });
  }, [activeIndex, entries]);

  let index = -1;

  return (
    <Portal>
      <div
        ref={boxRef}
        id="composer-mention-list"
        role="listbox"
        aria-label="引用插入"
        style={position === null ? { top: -9999, left: -9999 } : { top: position.top, left: position.left }}
        className="fixed z-50 flex w-80 max-w-[calc(100vw-2rem)] flex-col rounded-card border border-line/80 material-flyout flyout-in p-1 shadow-flyout"
      >
        <div className="fluent-scrollbar max-h-72 overflow-y-auto">
          {entries.length === 0 && !loading && (
            <p className="px-3 py-3 text-[12px] text-gray-500">
              {error ?? '没有匹配的引用'}
            </p>
          )}
          {entries.length === 0 && loading && (
            <p className="px-3 py-3 text-[12px] text-gray-500">正在读取工作区文件…</p>
          )}
          {(['file', 'skill', 'context'] as const).map((kind) => {
            const rows = entries.filter((entry) => entry.kind === kind);
            if (rows.length === 0) return null;
            return (
              <div key={kind} role="group" aria-label={MENTION_GROUP_TITLE[kind]}>
                <p className="ui-section-label px-2 pb-1 pt-2 text-gray-500">
                  {MENTION_GROUP_TITLE[kind]}
                </p>
                {rows.map((entry) => {
                  index += 1;
                  const active = index === activeIndex;
                  const own = index;
                  return (
                    <button
                      key={entry.id}
                      id={mentionOptionId(entry)}
                      type="button"
                      role="option"
                      aria-selected={active}
                      data-active={active ? 'true' : 'false'}
                      onMouseDown={(event) => {
                        // The pick replaces the `@` range, so the selection has to
                        // survive the click: never let the button take focus.
                        event.preventDefault();
                        onPick(entry);
                      }}
                      onMouseEnter={() => onHoverIndex(own)}
                      className={`ui-nav-row relative flex w-full items-center gap-2 px-2 py-1.5 text-left ${
                        active ? 'bg-blue-50/80' : 'hover:bg-gray-100/70'
                      }`}
                    >
                      <span className="flex min-w-0 flex-1 flex-col">
                        <span className="truncate font-mono text-[12px] text-gray-900">
                          {entry.label}
                        </span>
                        <span className="truncate text-[11px] text-gray-500">{entry.detail}</span>
                      </span>
                      <span className="shrink-0 font-mono text-[10px] text-gray-400">
                        {MENTION_KIND_LABEL[entry.kind]}
                      </span>
                    </button>
                  );
                })}
              </div>
            );
          })}
        </div>
        <div className="mt-1 flex items-center justify-between border-t border-line/60 px-2 pt-1.5 text-[10px] text-gray-500">
          <span>支持直接键入筛选</span>
          <span className="flex items-center gap-1">
            <span className="ui-kbd">↑</span>
            <span className="ui-kbd">↓</span>
            <span>选择</span>
            <span className="ui-kbd">Enter</span>
            <span>插入</span>
            <span className="ui-kbd">Esc</span>
          </span>
        </div>
      </div>
    </Portal>
  );
};
