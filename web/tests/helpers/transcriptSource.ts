/**
 * The transcript's row sources, discovered rather than listed.
 *
 * A row kind is one module under `components/transcriptRows/`, so the guards that pin
 * what a row paints read *that directory* plus the dispatcher -- a new kind is covered
 * the moment its module exists, and no guard has to name it or depend on the order the
 * kinds happen to be written in.
 */
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const components = join(here, '..', '..', 'src', 'components');
const rowsDir = join(components, 'transcriptRows');

/** The dispatcher: the one file that is not a row kind. */
export const DISPATCHER = 'Transcript.tsx';

export interface RowModule {
  /** The message kind the registry maps to this module. */
  kind: string;
  /** Module name without its extension, e.g. `ToolGroupRow`. */
  name: string;
  text: string;
}

/** The object literal of a table, from `= {` to its closing `};`. */
export function tableBody(source: string): string {
  return source.slice(source.indexOf('= {') + 3, source.indexOf('};'));
}

/** The kinds a table lists, in source order. */
export function tableKinds(source: string): string[] {
  return [...tableBody(source).matchAll(/(?:^|\n) {2}(\w+):/g)].map((match) => match[1]);
}

/**
 * The row-kind modules, discovered from the registry.
 *
 * The table is the source of truth for which module paints which kind, so a guard that
 * reads the modules reads exactly the registered ones -- a new kind is covered as soon
 * as it is registered, and a module that nothing maps is not mistaken for a row.
 */
export function rowModules(): RowModule[] {
  const registry = readFileSync(join(rowsDir, 'registry.tsx'), 'utf8');
  return [...tableBody(registry).matchAll(/(\w+):\s*(\w+),/g)].map(([, kind, name]) => ({
    kind,
    name,
    text: readFileSync(join(rowsDir, `${name}.tsx`), 'utf8'),
  }));
}

/** One row kind's module, by name (`ToolGroupRow`). */
export function rowSource(name: string): string {
  return readFileSync(join(rowsDir, `${name}.tsx`), 'utf8');
}

/** The dispatcher's source. */
export function dispatcherSource(): string {
  return readFileSync(join(components, DISPATCHER), 'utf8');
}

/** The dispatcher and every row kind, as one blob (for cross-row counts). */
export function allRowSources(): string {
  return [dispatcherSource(), ...rowModules().map((row) => row.text)].join('\n');
}

/** Every `*.tsx` in the row directory, mapped or not. */
export function rowDirectoryFiles(): string[] {
  return readdirSync(rowsDir).filter((entry) => entry.endsWith('.tsx')).sort();
}

/** How many times `needle` appears in `text`. */
export function count(text: string, needle: string): number {
  return text.split(needle).length - 1;
}
