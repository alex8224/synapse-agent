/**
 * Source guards for the installed console's caption strip (Window Controls
 * Overlay).
 *
 * Declaring `window-controls-overlay` hands the caption to the app: the browser
 * keeps drawing the window buttons, but it draws them *over* the page.  Three
 * things then have to stay true, and none of them is visible until somebody
 * installs the app and drags the window:
 *
 *  1. The header becomes the window's drag handle, and everything clickable
 *     inside it opts out again -- a caption that swallows clicks on the sidebar
 *     toggle or the branch chip is worse than no caption at all.
 *  2. The reserve for the window buttons is derived from `env(titlebar-area-*)`
 *     and never hard-coded, so it also collapses to nothing when the user
 *     re-shows the native title bar from the window's ⋮ menu.
 *  3. Nothing leaks outside the display mode: a browser without the overlay (and
 *     every non-installed tab) must keep the exact layout it has today.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const read = (relative: string) => readFileSync(join(here, '..', 'src', relative), 'utf8');

const styles = read('index.css');
const topBar = read('components/TopBar.tsx');

const OVERLAY_MEDIA = '@media (display-mode: window-controls-overlay)';

test('the caption rules are scoped to the overlay display mode', () => {
  const at = styles.indexOf(OVERLAY_MEDIA);
  assert.ok(at > 0, `index.css must scope the caption rules to \`${OVERLAY_MEDIA}\``);
  assert.equal(
    /app-region/.test(styles.slice(0, at)),
    false,
    'no drag region may exist outside the overlay mode',
  );
});

test('the header drags and the chip track opts back out', () => {
  const block = styles.slice(styles.indexOf(OVERLAY_MEDIA));
  assert.ok(
    /\.wco-caption\s*\{[^}]*-webkit-app-region:\s*drag/.test(block),
    'the header must be the window drag handle',
  );
  assert.ok(
    /\.wco-caption-controls\s*\{[^}]*-webkit-app-region:\s*no-drag/.test(block),
    'interactive children must opt out of the drag region',
  );
});

test('the reserve for the window buttons comes from the titlebar area', () => {
  const block = styles.slice(styles.indexOf(OVERLAY_MEDIA));
  assert.ok(
    /\.wco-caption-reserve\s*\{[^}]*min-width:\s*calc\(100vw - env\(titlebar-area-x,\s*0px\) - env\(titlebar-area-width,\s*100vw\)\)/.test(
      block,
    ),
    'the reserve must be derived from env(titlebar-area-*) with a zero fallback',
  );
});

test('the header is the caption and keeps its three tracks', () => {
  assert.match(
    topBar,
    /<header className="[^"]*\bwco-caption\b[^"]*"/,
    'the header must carry the drag utility',
  );
  assert.ok(
    topBar.includes('grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)]'),
    'the caption must stay the three-track grid, so the title stays centred',
  );
  assert.ok(
    /<div className="wco-caption-reserve" \/>/.test(topBar),
    'the empty right track is what reserves the window buttons',
  );
});

test('the chips and the session title stay clickable inside the caption', () => {
  const controlsOpen = /<div className="[^"]*wco-caption-controls[^"]*">/.exec(topBar);
  assert.ok(controlsOpen, 'the track holding the chips must opt out of the drag region');
  const track = topBar.slice(topBar.indexOf(controlsOpen[0]));
  assert.ok(track.indexOf('<button') > 0, 'that track must be the one holding the chips');

  const titleAt = topBar.indexOf('ui-session-title');
  assert.ok(titleAt > 0, 'the centre track still carries the session title');
  const titleTag = topBar.slice(titleAt, topBar.indexOf('>', titleAt));
  // The title opens the session info, so it is a control like the chips: without the
  // opt-out the caption would swallow the click in an installed window.  The rest of
  // the strip stays draggable.
  assert.ok(
    titleTag.includes('wco-caption-controls'),
    'the title opens a panel, so the caption must not swallow its click',
  );
});
