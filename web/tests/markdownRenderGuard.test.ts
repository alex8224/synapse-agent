/**
 * Static guards for the two renderers whose output is markup.
 *
 * Markdown is normally parsed into typed nodes and rendered through React, so
 * the transcript has no HTML-injection path at all.  KaTeX formulas and mermaid
 * diagrams are the deliberate exceptions, and these assertions pin the
 * boundaries that make the exceptions acceptable:
 *
 * - exactly one component in `src/` injects markup, and it is the one whose
 *   callers are documented as generators of trusted markup;
 * - mermaid is loaded lazily (never into the initial bundle), runs with
 *   `securityLevel: 'strict'`, refuses the directive channel and passes its SVG
 *   through DOMPurify before injection;
 * - the dependencies are declared, so the build cannot depend on a hoisted
 *   transitive package.
 *
 * Comments are ignored: the rules run over the TypeScript token stream, so a
 * doc comment that *names* `dangerouslySetInnerHTML` does not count as a use.
 */
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import ts from 'typescript';

const here = dirname(fileURLToPath(import.meta.url));
const webRoot = join(here, '..');
const srcRoot = join(webRoot, 'src');

function sourceFiles(dir: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      found.push(...sourceFiles(full));
    } else if (entry.name.endsWith('.ts') || entry.name.endsWith('.tsx')) {
      found.push(full);
    }
  }
  return found;
}

/** Token texts of a source file, with comments and other trivia removed. */
function tokenTexts(fileName: string, text: string): string[] {
  const scriptKind = fileName.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const source = ts.createSourceFile(fileName, text, ts.ScriptTarget.Latest, false, scriptKind);
  const tokens: string[] = [];
  const visit = (node: ts.Node): void => {
    if (ts.isJSDoc(node)) return;
    const children = node.getChildren(source);
    if (children.length === 0) {
      tokens.push(node.getText(source));
      return;
    }
    for (const child of children) visit(child);
  };
  visit(source);
  return tokens;
}

const INJECTION_BOUNDARY = join(srcRoot, 'components', 'GeneratedHtml.tsx');

function read(fileName: string): string {
  return readFileSync(fileName, 'utf8');
}

test('markup injection happens in exactly one component', () => {
  const offenders = sourceFiles(srcRoot).filter((file) =>
    tokenTexts(file, read(file)).includes('dangerouslySetInnerHTML'),
  );
  assert.deepEqual(
    offenders.map((file) => file.slice(webRoot.length + 1).replaceAll('\\', '/')),
    ['src/components/GeneratedHtml.tsx'],
  );
});

test('Markdown routes mermaid fences and math to the renderers', () => {
  const tokens = tokenTexts('Markdown.tsx', read(join(srcRoot, 'components', 'Markdown.tsx')));
  assert.ok(tokens.includes('MermaidBlock'));
  assert.ok(tokens.includes('DisplayMath'));
  assert.ok(tokens.includes('InlineMath'));
  assert.ok(tokens.includes("'mermaid'"));
  // The old "no renderer ships with the console" caveat must be gone.
  assert.equal(read(join(srcRoot, 'components', 'Markdown.tsx')).includes('终端图形渲染未实现'), false);
});

test('mermaid is loaded lazily with strict security and sanitized output', () => {
  const file = join(srcRoot, 'components', 'MermaidBlock.tsx');
  const text = read(file);
  assert.ok(text.includes("import('mermaid')"), 'mermaid must be a dynamic import');
  assert.equal(/from\s+'mermaid'/.test(text), false, 'mermaid must not be statically imported');
  assert.ok(text.includes("securityLevel: 'strict'"));
  assert.ok(text.includes('htmlLabels: false'));
  assert.ok(text.includes('DOMPurify.sanitize'));
  // The render target is the console's own off-screen node, not document.body.
  assert.equal(/document\s*\.\s*body/.test(text.replace(/\/\*[\s\S]*?\*\//g, '')), false);
  // Remote fetches are stripped from the theme CSS mermaid embeds in the SVG.
  assert.ok(text.includes('@import'));
  assert.ok(text.includes('url\\s*\\('));
  // The refusal rules live in a pure module so they stay unit-testable.
  assert.ok(text.includes("from '../markdown/mermaid.ts'"));
  assert.ok(read(join(srcRoot, 'markdown', 'mermaid.ts')).includes('%%\\s*\\{'));
  assert.ok(read(join(srcRoot, 'markdown', 'mermaid.ts')).includes('FRONTMATTER_RE'));
});

test('math is typeset with trust disabled and no direct injection', () => {
  const math = read(join(srcRoot, 'markdown', 'tex.ts'));
  assert.ok(math.includes('trust: false'));
  assert.ok(math.includes('throwOnError: false'));
  assert.ok(math.includes('maxExpand'));
  const component = read(join(srcRoot, 'components', 'MathTex.tsx'));
  assert.ok(component.includes('GeneratedHtml'));
  assert.equal(component.includes('dangerouslySetInnerHTML'), false);
});

test('the renderer dependencies are declared and the stylesheet is loaded', () => {
  const pkg = JSON.parse(read(join(webRoot, 'package.json'))) as {
    dependencies: Record<string, string>;
  };
  for (const name of ['mermaid', 'katex', 'dompurify']) {
    assert.ok(pkg.dependencies[name], `${name} must be a declared dependency`);
  }
  assert.ok(read(join(srcRoot, 'main.tsx')).includes('katex/dist/katex.min.css'));
});

test('the injection boundary is documented as the only one', () => {
  assert.ok(read(INJECTION_BOUNDARY).includes('dangerouslySetInnerHTML'));
});
