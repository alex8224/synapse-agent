/**
 * Source guards for the console shell (two columns) and the reading column.
 *
 * The window is a row of two columns, not three stacked full-width rows: the
 * navigation is a full-height column on the left, and the header chips,
 * transcript, composer and status strip all belong to the workspace column on the
 * right.  Two consequences are pinned here, because both are easy to undo by
 * accident:
 *
 *  1. Nothing that belongs to the session may span the window again: the header
 *     and the status strip have to stay inside the workspace column, otherwise
 *     they render underneath the sidebar.
 *  2. The transcript and the composer must share one reading geometry
 *     (`.console-gutter` + `.console-column` in `index.css`): the same gutters
 *     around the same capped column, so the input card keeps the chat's left and
 *     right edges.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const read = (relative: string) => readFileSync(join(here, '..', 'src', relative), 'utf8');

const app = read('App.tsx');
const styles = read('index.css');
const sidebar = read('components/SideBar.tsx');
const transcript = read('components/Transcript.tsx');
const composer = read('components/CommandInput.tsx');
const banner = read('components/RuntimeDiagnosticsBanner.tsx');

test('the shell is a sidebar column plus a workspace column', () => {
  // The window root paints the window fill (`material-canvas`), which is the
  // backdrop's translucent layer in a theme that has one.
  const shell = app.slice(app.indexOf('<div className="console-shell'));
  const sidebarAt = shell.indexOf('<SideBar ');
  const columnAt = shell.indexOf('flex min-w-0 flex-1 flex-col');
  assert.ok(sidebarAt >= 0, 'the sidebar must live inside the navigation wrapper');
  assert.ok(columnAt > sidebarAt, 'the workspace column must follow the sidebar');
  for (const child of ['<TopBar ', '<Transcript />', '<CommandInput />', '<BottomBar />']) {
    assert.ok(shell.indexOf(child) > columnAt, `${child} must live inside the workspace column`);
  }
  assert.equal(
    shell.includes('flex-col font-body-md'),
    false,
    'the shell must not stack full-width rows again',
  );
});

test('mobile navigation is an independent modal drawer, not a squeezed column', () => {
  assert.ok(app.includes("matchMedia('(max-width: 767px)')"));
  assert.ok(app.includes('const [tabletCollapsed, setTabletCollapsed] = useState(true)'));
  assert.ok(app.includes('hidden={mobile && !drawerOpen}'));
  assert.ok(app.includes('inert={mobile && drawerOpen}'));
  assert.ok(app.includes("event.key === 'Tab'"));
  assert.ok(app.includes("event.key === 'Escape'"));
  assert.ok(app.includes('previous?.focus()'));
  assert.ok(app.includes('state.currentSession.project_id !== previous.currentSession.project_id'));
  assert.ok(sidebar.includes('inert={isSidebarCollapsed}'));
  assert.ok(sidebar.includes('inert={!isSidebarCollapsed}'));
});

test('small screens have fluid content, safe areas and usable touch targets', () => {
  assert.ok(styles.includes('height: 100dvh'));
  assert.ok(styles.includes('padding-left: max(12px, env(safe-area-inset-left'));
  assert.ok(styles.includes('@media (pointer: coarse)'));
  assert.ok(styles.includes('min-height: 44px'));
  assert.ok(styles.includes('.navigation-drawer[hidden] { display: none; }'));
  // A dialog caps and scrolls its own body: a global rule would also override
  // the caps the individual dialogs already declare.
  const goal = read('components/GoalDialog.tsx');
  assert.ok(goal.includes('max-h-[85vh]') && goal.includes('overflow-y-auto'));
});

test('file explorers have mobile list/detail navigation without changing desktop columns', () => {
  const git = read('components/GitExplorer.tsx');
  const files = read('components/ArtifactsPanel.tsx');
  assert.ok(git.includes('data-detail={mobileDetail}'));
  assert.ok(files.includes('data-mobile-detail={mobileDetail}'));
  assert.ok(git.includes('返回文件列表') && files.includes('返回文件列表'));
  assert.ok(styles.includes('.responsive-file-window { left: 8px !important'));
});

test('the sidebar runs the full height of the window', () => {
  // Both the expanded tree and the collapsed rail are `h-full` columns and the
  // shell is `h-screen`, so they reach from the top edge to the bottom one.
  assert.equal(
    (sidebar.match(/h-full/g) ?? []).length,
    2,
    'both sidebar states must be full-height columns',
  );
  assert.ok(sidebar.includes('border-r'), 'the sidebar must keep its own right edge');
  assert.ok(app.includes('h-screen'), 'the shell must stay viewport-height');
});

test('the sidebar tree scrolls with no visible scrollbar', () => {
  // The tree still scrolls (wheel / touch / keyboard), but the 240px rail keeps a
  // clean edge.  The class is applied to the scroll container and is focusable so
  // the keyboard scrolls it even before a row inside has focus.
  const tree = sidebar.slice(
    sidebar.indexOf('no-scrollbar'),
    sidebar.indexOf('{/* Foot of the sidebar'),
  );
  assert.ok(tree.includes('overflow-y-auto'), 'the tree must still scroll');
  assert.ok(tree.includes('tabIndex={0}'), 'the tree must be keyboard-focusable');
  // The rules live in index.css and cover the three engine families.
  assert.ok(styles.includes('.no-scrollbar'), 'the class lives in index.css');
  assert.ok(/scrollbar-width:\s*none/.test(styles), 'Firefox needs scrollbar-width: none');
  assert.ok(/-ms-overflow-style:\s*none/.test(styles), 'legacy Edge needs -ms-overflow-style');
  assert.ok(
    /\.no-scrollbar::-webkit-scrollbar\s*\{[^}]*display:\s*none/.test(styles),
    'Blink/WebKit need the scoped webkit pseudo-element',
  );
  // Scoped on purpose: a bare `::-webkit-scrollbar` rule would hide every
  // scrollbar in the app, including a nested block's own.
  assert.equal(
    /(^|\n)\s*::-webkit-scrollbar/.test(styles),
    false,
    'the scrollbar rule must stay scoped to the class',
  );
});

test('the chat column keeps the reading width and the composer is narrower', () => {
  assert.ok(styles.includes('.console-column'), 'the shared reading width lives in index.css');
  assert.ok(styles.includes('.console-gutter'), 'the shared gutters live in index.css');
  // Desktop: the gutters are 10% of the workspace each side, so the chat column is
  // 80% of it; below `lg` the gutters are a flat 2rem.
  assert.ok(
    /@media\s*\(min-width:\s*1024px\)\s*\{[^}]*\.console-gutter\s*\{[^}]*padding-left:\s*10%[^}]*padding-right:\s*10%/.test(
      styles,
    ),
    'on desktop the shared gutters must be 10% of the workspace each side',
  );
  assert.ok(
    /\.console-gutter\s*\{[^}]*padding-left:\s*2rem[^}]*padding-right:\s*2rem/.test(styles),
    'below the breakpoint the shared gutters must be a flat 2rem',
  );
  assert.ok(
    /\.console-column\s*\{[^}]*width:\s*100%/.test(styles),
    'the chat column must fill the gutter-inset content box',
  );
  assert.equal(
    /\.console-column\s*\{[^}]*max-width:/.test(styles),
    false,
    'the chat column keeps the full reading width; the cap belongs to the composer',
  );
  // The composer is the only capped surface: narrower than the chat on purpose,
  // still centred on the same axis.
  assert.ok(
    /\.ui-composer\s*\{\s*max-width:\s*var\(--composer-max\)/.test(styles),
    'only the composer carries the width cap',
  );
  assert.ok(
    /--composer-max:\s*\d+(\.\d+)?rem/.test(styles),
    'the cap belongs to the theme contract, not to a component',
  );
  assert.ok(transcript.includes('console-column'), 'the chat column must use it');
  assert.ok(composer.includes('console-column'), 'the composer card must use it');
  assert.ok(composer.includes('ui-composer'), 'the composer must carry its own cap');
  assert.equal(
    /max-w-3xl/.test(composer),
    false,
    'the composer must not carry a Tailwind width next to the shared geometry',
  );
  // The same horizontal gutters on every reading wrapper, otherwise the columns
  // drift apart (and the diagnostics notice stops starting on the chat edge).
  assert.ok(transcript.includes('console-gutter'), 'the transcript wrapper must keep its gutters');
  assert.ok(composer.includes('console-gutter'), 'the composer wrapper must keep the same gutters');
  assert.ok(banner.includes('console-gutter'), 'the diagnostics notice must use the same gutters');
});

test('the composer is the last row of the workspace column, not a floating card', () => {
  // The card floats so the transcript scrolls behind it — that is what makes the
  // card's own acrylic visible at all.  What the in-flow row bought was that the
  // newest streamed line was never hidden behind the input; the scroller reserves
  // the card's *measured* height instead, so the same guarantee holds.
  assert.ok(
    composer.includes('absolute inset-x-0 bottom-0'),
    'the composer must float over the transcript',
  );
  assert.ok(composer.includes('cardRef'), 'the card must be measured, not guessed');
  assert.ok(
    /new ResizeObserver/.test(composer),
    'a card that grows (attachments, a wrapped row) must republish its height',
  );
  assert.ok(
    composer.includes("setProperty('--composer-h'"),
    'the measured height travels to the scroller as a CSS variable',
  );
  assert.ok(transcript.includes('console-pane-inset'), 'the transcript must reserve the card');
  assert.ok(
    /\.console-pane-inset\s*\{[^}]*padding-bottom:\s*calc\(var\(--composer-h\) \+ 1\.5rem\)[^}]*scroll-padding-bottom:\s*calc\(var\(--composer-h\) \+ 1\.5rem\)/.test(
      styles,
    ),
    'the reserved height and the scroll padding must be the same inset',
  );
  assert.ok(
    /--composer-h:\s*[\d.]+rem/.test(styles),
    'the first paint needs a value before the observer runs',
  );
  // The scroller starts *behind* the header, so a `block: 'start'` scroll -- the
  // turn rail's jump to a turn's user message -- needs the bar reserved at the
  // top too, exactly as the composer is reserved at the bottom.
  assert.ok(
    /\.console-pane-inset\s*\{[^}]*scroll-padding-top:\s*calc\(var\(--chrome-h\) \+ 1\.5rem\)/.test(
      styles,
    ),
    'a start-aligned jump must clear the header, not land behind it',
  );
  assert.equal(
    transcript.includes('pb-36'),
    false,
    'a fixed guess at the card height is what hid the newest line',
  );
});

test('the composer is centred without borrowing the chat width', () => {
  // The transcript scrolls with no visible scrollbar, so nothing takes a bite out
  // of the reading column.  The composer no longer shares that column's edges: it
  // is capped narrower (`--composer-max`) and centred on the same axis.
  assert.ok(composer.includes('console-column'), 'the composer stays on the reading axis');
  assert.ok(
    transcript.includes('no-scrollbar'),
    'the transcript must scroll with no visible scrollbar',
  );
  assert.ok(transcript.includes('overflow-y-auto'), 'the transcript must still scroll');
  assert.equal(
    transcript.includes('scrollbar-gutter'),
    false,
    'with no scrollbar there is no gutter to reserve',
  );
  assert.equal(
    composer.includes('scrollbar-gutter'),
    false,
    'the composer must not reserve a gutter the transcript does not need',
  );
});

test('the diagnostics notice is aligned with the reading column', () => {
  assert.ok(banner.includes('console-column'), 'the notice must start on the chat edge');
});

test('the sidebar foot carries the workspace identity and the settings entry', () => {
  // Real data only: the label is the attached workspace path (or an explicit empty
  // state), never a placeholder project or account name.
  assert.ok(sidebar.includes('workspacePath'), 'the identity row must show the real path');
  assert.ok(sidebar.includes('未绑定工作区'), 'an unset path needs an explicit empty state');
  assert.ok(sidebar.includes('<ConsoleActions'), 'the app-level actions stay at the foot');
  assert.ok(sidebar.includes('打开设置'), 'the settings entry stays at the foot');
});

test('the sidebar nav names the two entries and their shortcuts', () => {
  // The rail and the expanded tree expose the same actions; the expanded one
  // spells the shortcuts out instead of relying on the help dialog.
  const nav = sidebar.slice(
    sidebar.indexOf('{/* Nav entry'),
    sidebar.indexOf('{/* Shortcut hint'),
  );
  assert.ok(nav.includes('新建任务'), 'the sidebar nav must name the new-task entry');
  assert.ok(nav.includes('Ctrl+N'), 'and show its shortcut');
  // New task creates in the current project, exactly like the rail's `+`.
  assert.ok(nav.includes('createNewSession()'), 'the nav entry must call the existing action');
  // The search entry carries its shortcut in the tooltip and as a visible badge.
  assert.ok(
    (sidebar.match(/Ctrl\+K/g) ?? []).length >= 2,
    'the search entry must show its shortcut too',
  );
});

test('the sidebar popovers are windows of their own, not boxes in the rail', () => {
  // They were `absolute` boxes inside the nav, and that broke three things:
  //
  //  * the rail's own `backdrop-filter` made the nav a backdrop root, so the panel
  //    could only blur what the nav painted — the transcript behind it stayed sharp
  //    and the panel read as transparent instead of frosted;
  //  * a 512px-tall panel was laid out in the footer's box, so it stretched that
  //    box and pushed the session tree up;
  //  * with the workspace column following the rail in the DOM, the panel also lost
  //    the paint race (its close button landed on the transcript).
  //
  // They are `FloatingPanel`s now: portalled to the body and positioned from the
  // trigger's viewport rect.
  const actions = read('components/ConsoleActions.tsx');
  const artifacts = read('components/ArtifactsPanel.tsx');
  assert.ok(actions.includes('<FloatingPanel'), 'the rail panels must float');
  assert.ok(artifacts.includes('<FloatingPanel'), 'the file browser must float too');
  for (const source of [actions, artifacts]) {
    assert.equal(
      /className="absolute bottom-full/.test(source),
      false,
      'a floating panel must not also be laid out inside its anchor',
    );
  }
  const floating = read('components/FloatingPanel.tsx');
  assert.ok(floating.includes("from './Portal.tsx'"), 'it must portal, or the rail stays its backdrop root');
  assert.ok(
    floating.includes('fixed z-50'),
    'it must position itself against the viewport',
  );
  assert.ok(
    floating.includes('addEventListener(\'resize\''),
    'a resize must re-measure the anchor',
  );
});

test('the transcript scrolls under the chrome, so the acrylic has something to blur', () => {
  // A blur needs content behind it.  While the pane clipped its own children and
  // the header sat above a static window fill, `backdrop-filter` had nothing to
  // act on and the material was invisible at rest.
  assert.ok(
    transcript.includes('console-pane-inset'),
    'the transcript must reach up behind the header',
  );
  assert.ok(
    /\.console-pane-inset\s*\{[^}]*margin-top:\s*calc\(-1 \* var\(--chrome-h\)\)[^}]*padding-top:\s*calc\(var\(--chrome-h\) \+ 1\.5rem\)/.test(
      styles,
    ),
    'the class owns the negative margin and the matching content inset',
  );
  const main = app.slice(app.indexOf('<main className='), app.indexOf('<main className=') + 200);
  assert.equal(
    main.includes('overflow-hidden'),
    false,
    'the pane must not clip the strip the scroller reaches into',
  );
  const topBar = read('components/TopBar.tsx');
  // `z-20` only orders a *positioned* element; a static header lost the race
  // against the pane and the scrolled content covered it instead.
  assert.ok(
    topBar.includes('material-chrome relative z-20'),
    'the header must be positioned for its z-index to apply',
  );
});
