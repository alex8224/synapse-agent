/**
 * Source guard: a relative import carries its `.ts`/`.tsx` extension.
 *
 * `web/AGENTS.md` requires it ("`src/**` uses semicolons and relative imports
 * carry the `.ts`/`.tsx` extension"), but both spellings resolve through Vite and
 * `tsc`, so only a static assertion catches a regression.  The recovery strip's
 * import in `App.tsx` is the one this console change introduced, so it is pinned
 * here; a large pre-existing body of extensionless imports predates the rule and
 * is out of scope.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));

test('App.tsx imports the recovery strip with its .tsx extension', () => {
  const app = readFileSync(join(here, '..', 'src', 'App.tsx'), 'utf8');
  assert.ok(
    app.includes("from './components/RecoveryNotice.tsx'"),
    'the RecoveryNotice import must carry the extension web/AGENTS.md requires',
  );
  assert.equal(
    app.includes("from './components/RecoveryNotice'"),
    false,
    'the extensionless spelling must not come back',
  );
});
