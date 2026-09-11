/**
 * Source guard for the bottom bar popover dismissal contract.
 *
 * The model / reasoning / MCP popovers (and the F1 help overlay) must close as
 * soon as they lose focus — a click anywhere outside their own trigger+panel,
 * or Escape. Each popover therefore has to live inside its own ref'd wrapper,
 * which is what the outside-click handler tests against.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, '..', 'src', 'components', 'BottomBar.tsx'), 'utf8');

test('every bottom bar popover sits in a ref-wrapped trigger container', () => {
  for (const ref of ['modelRef', 'thinkingRef', 'mcpRef']) {
    assert.ok(
      source.includes(`ref={${ref}}`),
      `the ${ref} wrapper must exist so the outside-click handler can test it`,
    );
  }
  assert.equal(
    source.split('className="relative" ref={').length - 1,
    3,
    'exactly the three popovers may be ref-wrapped',
  );
});

test('a click outside the open popover closes it', () => {
  assert.ok(
    source.includes("document.addEventListener('mousedown'"),
    'the bar must listen for outside clicks while a popover is open',
  );
  assert.ok(
    source.includes('ref.current?.contains(target)'),
    'the handler must keep clicks inside the popover open',
  );
  assert.ok(
    source.includes('if (!inside) closeOthers()'),
    'a click outside must close the open popover',
  );
});

test('Escape closes the popovers and the help overlay', () => {
  const escape = source.indexOf("e.key === 'Escape'");
  assert.ok(escape >= 0, 'Escape must be handled by the bar');
  const tail = source.slice(escape, escape + 400);
  assert.ok(tail.includes('closeOthers()'), 'Escape must close the popovers');
  assert.ok(tail.includes('setShowHelp(false)'), 'Escape must close the help overlay');
});
