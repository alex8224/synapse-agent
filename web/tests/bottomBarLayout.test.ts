/**
 * Source guard for the bottom bar layout decision.
 *
 * The bar is a symmetric three-track grid (`1fr auto 1fr`) so the current-turn
 * telemetry sits in the exact horizontal centre; the left column carries
 * activity + MCP / goal (the model and reasoning pickers moved into the composer)
 * and the right track stays an empty spacer. This test pins that decision against
 * a future edit that would slide the telemetry to the right edge.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, '..', 'src', 'components', 'BottomBar.tsx'), 'utf8');

function classNamesOf(anchor: string): string {
  const index = source.indexOf(anchor);
  assert.ok(index >= 0, `BottomBar must contain ${anchor}`);
  const match = /className="([^"]*)"/.exec(source.slice(index));
  assert.ok(match, `no className found after ${anchor}`);
  return match[1];
}

test('the bottom bar keeps the symmetric three-track grid', () => {
  const footer = classNamesOf('<footer');
  assert.ok(
    footer.includes('grid-cols-[1fr_auto_1fr]'),
    'the footer must keep 1fr auto 1fr so the centre track stays centred',
  );
  assert.equal(footer.includes('justify-end'), false, 'the bar must not be right-aligned');
});

test('the telemetry block is centred, not pushed to the right edge', () => {
  const centre = classNamesOf('{/* Centre: all turn telemetry');
  assert.ok(centre.includes('justify-self-center'), 'the telemetry block must self-centre');
  assert.equal(centre.includes('ml-auto'), false);
  assert.equal(centre.includes('justify-end'), false);
});
