/**
 * Conservative, dependency-free syntax highlighting for fenced code blocks.
 *
 * Only a few token classes are recognized (comments, strings, numbers,
 * keywords) and only for languages the agent actually emits; an unknown
 * language returns one plain token, so a mis-detected language can never
 * mangle the code.  The concatenation of all token texts always equals the
 * input exactly (asserted by the tests).
 */

export type HighlightKind =
  | 'plain'
  | 'comment'
  | 'string'
  | 'number'
  | 'keyword'
  | 'added'
  | 'removed'
  | 'meta';

export interface HighlightToken {
  text: string;
  kind: HighlightKind;
}

interface LangSpec {
  line: string[];
  block: [string, string] | null;
  quotes: string[];
  keywords: Set<string>;
}

const PY = [
  'and','as','assert','async','await','break','class','continue','def','del','elif','else',
  'except','finally','for','from','global','if','import','in','is','lambda','None','nonlocal',
  'not','or','pass','raise','return','True','False','try','while','with','yield','self',
];
const JS = [
  'as','async','await','break','case','catch','class','const','continue','default','delete','do',
  'else','export','extends','finally','for','from','function','if','import','in','instanceof',
  'let','new','of','return','static','super','switch','this','throw','try','typeof','var','void',
  'while','with','yield','true','false','null','undefined','interface','type','enum','implements',
  'readonly','public','private','protected','satisfies','declare',
];
const SH = [
  'case','do','done','elif','else','esac','fi','for','function','if','in','select','then',
  'until','while','export','local','readonly','return','set','source','echo','cd','exit',
];
const SQL = [
  'select','from','where','insert','into','values','update','set','delete','create','table',
  'alter','drop','index','join','left','right','inner','outer','on','group','by','order','having',
  'limit','offset','union','all','distinct','as','and','or','not','null','primary','key','foreign',
  'references','default','int','integer','text','varchar','boolean','date','timestamp','begin',
  'commit','rollback',
];
const CFG = ['true', 'false', 'null', 'yes', 'no', 'on', 'off'];

function spec(
  line: string[],
  block: [string, string] | null,
  quotes: string[],
  keywords: string[],
): LangSpec {
  return { line, block, quotes, keywords: new Set(keywords) };
}

const SLASH = ['//'] as string[];
const C_BLOCK: [string, string] = ['/*', '*/'];
const DQ = ['"'] as string[];
const DQSQ = ['"', "'"] as string[];
const DQSQBT = ['"', "'", '`'] as string[];

const SPECS: Record<string, LangSpec> = {
  python: spec(['#'], null, DQSQ, PY),
  py: spec(['#'], null, DQSQ, PY),
  javascript: spec(SLASH, C_BLOCK, DQSQBT, JS),
  js: spec(SLASH, C_BLOCK, DQSQBT, JS),
  jsx: spec(SLASH, C_BLOCK, DQSQBT, JS),
  typescript: spec(SLASH, C_BLOCK, DQSQBT, JS),
  ts: spec(SLASH, C_BLOCK, DQSQBT, JS),
  tsx: spec(SLASH, C_BLOCK, DQSQBT, JS),
  json: spec([], null, DQ, ['true', 'false', 'null']),
  bash: spec(['#'], null, DQSQ, SH),
  sh: spec(['#'], null, DQSQ, SH),
  shell: spec(['#'], null, DQSQ, SH),
  zsh: spec(['#'], null, DQSQ, SH),
  powershell: spec(['#'], ['<#', '#>'], DQSQ, SH),
  ps1: spec(['#'], ['<#', '#>'], DQSQ, SH),
  yaml: spec(['#'], null, DQSQ, CFG),
  yml: spec(['#'], null, DQSQ, CFG),
  toml: spec(['#'], null, DQSQ, CFG),
  ini: spec(['#', ';'], null, DQSQ, CFG),
  sql: spec(['--'], C_BLOCK, DQ, SQL),
  rust: spec(SLASH, C_BLOCK, DQ, [
    'fn','let','mut','pub','use','struct','enum','impl','trait','match','if','else','for','while',
    'loop','return','self','Self','const','static','async','await','move','ref','where','as','in',
    'crate','mod','dyn','true','false',
  ]),
  go: spec(SLASH, C_BLOCK, ['"', '`'], [
    'func','package','import','var','const','type','struct','interface','map','chan','go','defer',
    'return','if','else','for','range','switch','case','default','select','nil','true','false',
    'break','continue',
  ]),
  java: spec(SLASH, C_BLOCK, DQSQ, [
    'public','private','protected','class','interface','enum','extends','implements','static',
    'final','void','int','long','double','boolean','char','String','new','return','if','else',
    'for','while','try','catch','finally','throw','throws','import','package','null','true','false',
  ]),
  c: spec(SLASH, C_BLOCK, DQSQ, [
    'int','char','float','double','void','struct','enum','union','typedef','static','const','return',
    'if','else','for','while','switch','case','break','continue','sizeof','NULL','include','define',
  ]),
  cpp: spec(SLASH, C_BLOCK, DQSQ, [
    'int','char','float','double','void','struct','class','enum','namespace','using','template',
    'typename','static','const','return','if','else','for','while','switch','case','break',
    'continue','nullptr','true','false','auto','public','private','protected','include','define',
  ]),
  css: spec([], C_BLOCK, DQSQ, []),
  html: spec([], ['<!--', '-->'], DQSQ, []),
  xml: spec([], ['<!--', '-->'], DQSQ, []),
};

const DIFF_LANGS = new Set(['diff', 'patch']);

/** Languages this module can highlight; anything else stays plain. */
export function isHighlightedLanguage(lang: string): boolean {
  return lang in SPECS || DIFF_LANGS.has(lang);
}

function highlightDiff(code: string): HighlightToken[] {
  const tokens: HighlightToken[] = [];
  const lines = code.split('\n');
  lines.forEach((line, index) => {
    let kind: HighlightKind = 'plain';
    // `--- a/f` and `+++ b/f` are file headers, not removed/added content.
    if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('@@')) kind = 'meta';
    else if (line.startsWith('+')) kind = 'added';
    else if (line.startsWith('-')) kind = 'removed';
    tokens.push({ text: index === lines.length - 1 ? line : `${line}\n`, kind });
  });
  return tokens;
}

/** Tokenize `code`; unknown languages produce a single plain token. */
export function highlight(code: string, lang: string): HighlightToken[] {
  const language = (lang || '').toLowerCase();
  if (DIFF_LANGS.has(language)) return highlightDiff(code);
  const spec0 = SPECS[language];
  if (!spec0) return [{ text: code, kind: 'plain' }];

  const tokens: HighlightToken[] = [];
  let plain = '';
  let i = 0;

  const flushPlain = (): void => {
    if (plain !== '') {
      tokens.push({ text: plain, kind: 'plain' });
      plain = '';
    }
  };
  const push = (text: string, kind: HighlightKind): void => {
    flushPlain();
    tokens.push({ text, kind });
  };

  while (i < code.length) {
    const lineComment = spec0.line.find((prefix) => code.startsWith(prefix, i));
    if (lineComment !== undefined) {
      const newline = code.indexOf('\n', i);
      const stop = newline === -1 ? code.length : newline;
      push(code.slice(i, stop), 'comment');
      i = stop;
      continue;
    }

    const block = spec0.block;
    if (block !== null && code.startsWith(block[0], i)) {
      const end = code.indexOf(block[1], i + block[0].length);
      const stop = end === -1 ? code.length : end + block[1].length;
      push(code.slice(i, stop), 'comment');
      i = stop;
      continue;
    }

    const ch = code[i];

    if (spec0.quotes.includes(ch)) {
      let j = i + 1;
      while (j < code.length) {
        if (code[j] === '\\') {
          j += 2;
          continue;
        }
        if (code[j] === ch) {
          j += 1;
          break;
        }
        if (code[j] === '\n') break;
        j += 1;
      }
      const stop = Math.min(j, code.length);
      push(code.slice(i, stop), 'string');
      i = stop;
      continue;
    }

    if (/[0-9]/.test(ch) && !/[A-Za-z0-9_]/.test(code[i - 1] ?? '')) {
      const match = /^[0-9][0-9_]*(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/.exec(code.slice(i));
      if (match) {
        push(match[0], 'number');
        i += match[0].length;
        continue;
      }
    }

    if (/[A-Za-z_]/.test(ch)) {
      const match = /^[A-Za-z_][A-Za-z0-9_]*/.exec(code.slice(i));
      const word = match ? match[0] : ch;
      if (spec0.keywords.has(word)) push(word, 'keyword');
      else plain += word;
      i += word.length;
      continue;
    }

    plain += ch;
    i += 1;
  }

  flushPlain();
  return tokens;
}
