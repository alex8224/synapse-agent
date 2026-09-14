/**
 * Source guards for the workspace file window's viewer contract.
 *
 * The behaviour of the helpers is pinned in `artifactImages.test.ts`; what is
 * left is the *wiring*, which is a JSX fact and cannot be exercised without a
 * DOM:
 *
 * - the picked row is a real button, and it is selected *before* anything is
 *   read, so the accent marker appears while the content is still loading (and
 *   stays when the read fails);
 * - the viewer routes by artifact kind, renders Markdown as a *preview* by
 *   default with a source toggle that keeps the highlighted code block, and
 *   never injects markup;
 * - an image is previewed from the bounded loader (never decoded as text), and
 *   a stale read cannot replace the newer selection;
 * - the window is a readable Fluent surface: shared `ui-field` / `ui-button` /
 *   `ui-nav-row` controls, a solid content layer, a wrapping toolbar and the
 *   soft scrim, with no 10px type left in it.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const webRoot = join(here, '..');
const read = (name: string) => readFileSync(join(webRoot, 'src', 'components', name), 'utf8');

const panel = read('ArtifactsPanel.tsx');
const floatingWindow = read('FloatingWindow.tsx');
const styles = readFileSync(join(webRoot, 'src', 'index.css'), 'utf8');

/** The body of one `const name = …` declaration, up to the next top-level const. */
function declaration(name: string, next: string): string {
  const start = panel.indexOf(`const ${name} =`);
  assert.ok(start >= 0, `ArtifactsPanel must declare ${name}`);
  const end = panel.indexOf(`const ${next} =`, start);
  assert.ok(end > start, `expected ${next} to follow ${name}`);
  return panel.slice(start, end);
}

test('a picked row is a keyboard button, selected before it is read', () => {
  const openEntry = declaration('openEntry', 'appendChunk');
  assert.ok(
    openEntry.indexOf('setSelected(entry)') < openEntry.indexOf('readFromStart(entry, true)'),
    'the selection must be published before the file read starts',
  );
  assert.ok(
    openEntry.indexOf('setSelected(entry)') < openEntry.indexOf('await loadDir'),
    'the selection must be published before a directory read starts too',
  );
  // A clickable div is not reachable from the keyboard.
  assert.ok(/<button\s+key=\{entry\.path\}\s+type="button"/.test(panel));
  assert.ok(panel.includes('aria-current={isSelected'), 'the selected row must announce itself');
  assert.ok(panel.includes('data-selected={isSelected}'));
  assert.ok(panel.includes('ui-nav-row'), 'the row takes the shared navigation tokens');
  assert.ok(
    styles.includes(".ui-nav-row[data-selected='true']::before"),
    'the accent marker is the theme\'s own navigation marker',
  );
});

test('the viewer routes by artifact kind and never decodes binary as text', () => {
  assert.ok(panel.includes('artifactPreviewKind(entry.media_type, entry.path)'));
  assert.ok(panel.includes("kind === 'image'"));
  assert.ok(panel.includes("kind === 'binary'"));
  assert.ok(panel.includes('不读取内容'), 'a non-previewable file must say why');
  // Images go through the bounded loader (helper behaviour is tested elsewhere);
  // they are never handed to the text decoder.
  assert.ok(panel.includes('createArtifactImageLoader'));
  assert.ok(panel.includes('loader.load(entry, session)'));
  assert.ok(panel.includes('object-contain'), 'an image keeps its aspect ratio');
  assert.equal(panel.includes('object-cover'), false, 'an image is never cropped');
});

test('Markdown opens as a rendered preview with a source toggle', () => {
  assert.ok(panel.includes("useState<'rendered' | 'source'>('rendered')"), 'preview is the default');
  assert.ok(panel.includes("shownKind === 'markdown' && preview === 'rendered'"));
  assert.ok(panel.includes('<Markdown text={file.text} />'), 'the shared renderer draws the preview');
  assert.ok(panel.includes("aria-pressed={preview === 'rendered'}"));
  assert.ok(panel.includes("aria-pressed={preview === 'source'}"));
  assert.ok(
    panel.includes('<CodeBlock lang={artifactLanguage(file.entry.path, false)} code={file.text} fill />'),
    'the source view keeps the highlighted code block',
  );
  assert.equal(panel.includes('dangerouslySetInnerHTML'), false, 'the window injects no markup');
});

test('a stale read cannot replace the newer selection', () => {
  assert.ok(panel.includes('const requestRef = useRef(0)'));
  assert.ok(panel.includes('beginRequest()'));
  assert.ok(panel.includes('if (!isCurrent(token)) return;'));
  assert.ok(panel.includes('const imageLoaderRef = useRef<ArtifactImageLoader | null>(null)'));
  assert.ok(panel.includes('loader.dispose()'), 'the blob URL is released on unmount');
  assert.ok(
    panel.includes('prev.entry.path !== path'),
    'a continuation may only extend the file it started from',
  );
});

test('the file window is a readable Fluent surface, not a 10px monospace grid', () => {
  assert.equal(panel.includes('text-[10px]'), false, 'no 10px type is left in the file window');
  assert.ok(panel.includes('ui-field'), 'the path filter is the shared field');
  assert.ok(panel.includes('ui-button ui-compact'), 'the toolbar uses the shared controls');
  assert.ok(panel.includes('bg-surface'), 'the reading surface is a solid layer fill');
  assert.ok(panel.includes('flex-wrap'), 'the toolbar wraps at narrow widths');
  assert.ok(panel.includes('scrim="soft"'), 'a document window opts into the soft scrim');
  assert.ok(floatingWindow.includes("scrim?: 'dim' | 'soft'"), 'the scrim is an explicit prop');
  assert.ok(
    floatingWindow.includes("scrim === 'dim' ? 'bg-black/60 backdrop-blur-md' : 'bg-black/25 backdrop-blur-sm'"),
    'the default keeps the lightbox dim, the file window takes the dialog scrim',
  );
  assert.ok(panel.includes('ui-toggle'), 'a pressed toolbar toggle is styled by the theme');
  assert.ok(styles.includes(".ui-toggle[aria-pressed='true']"));
});
