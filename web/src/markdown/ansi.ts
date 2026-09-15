/**
 * The colour a command printed, read back from its own SGR sequences.
 *
 * A tool's output is whatever the program wrote to a terminal, so it arrives with
 * the escape sequences that painted it (a coloured `Get-Process` table is the
 * common case here).  Printing those bytes verbatim is what put `[32;1mName[0m`
 * into the transcript, so this module turns them into styled spans instead.
 *
 * Only SGR (`ESC [ ... m`) is interpreted.  Every other control sequence is
 * consumed and dropped: it moves a cursor or clears a screen, and none of it is
 * text the reader asked to see.  Everything outside an escape is kept verbatim,
 * so the output can never be altered by being rendered.
 */

/** One styled run of terminal text. */
export interface AnsiSpan {
  text: string;
  /** Attribute classes (weight, slant, decoration, colour) for the run. */
  className: string;
  /** Explicit colour, when the sequence named a 256-colour or true-colour value. */
  color?: string;
  backgroundColor?: string;
}

/** The 8 ANSI colours and their bright variants, as Tailwind text classes. */
const FOREGROUND: Record<number, string> = {
  30: 'text-gray-900',
  31: 'text-red-600',
  32: 'text-emerald-600',
  33: 'text-amber-600',
  34: 'text-blue-600',
  35: 'text-fuchsia-600',
  36: 'text-cyan-600',
  37: 'text-gray-500',
  90: 'text-gray-400',
  91: 'text-red-400',
  92: 'text-emerald-500',
  93: 'text-amber-500',
  94: 'text-blue-500',
  95: 'text-fuchsia-500',
  96: 'text-cyan-500',
  97: 'text-gray-300',
};

/** The 8 ANSI background colours and their bright variants. */
const BACKGROUND: Record<number, string> = {
  40: 'bg-gray-900',
  41: 'bg-red-600',
  42: 'bg-emerald-600',
  43: 'bg-amber-600',
  44: 'bg-blue-600',
  45: 'bg-fuchsia-600',
  46: 'bg-cyan-600',
  47: 'bg-gray-200',
  100: 'bg-gray-800',
  101: 'bg-red-500',
  102: 'bg-emerald-500',
  103: 'bg-amber-500',
  104: 'bg-blue-500',
  105: 'bg-fuchsia-500',
  106: 'bg-cyan-500',
  107: 'bg-gray-300',
};

/**
 * SGR (`ESC [ ... m`), any other CSI sequence (private markers such as the `?`
 * of `ESC [ ? 25 l` included), and an OSC title.
 */
const ESCAPE =
  /\u001b\[([0-9;?<>!]*)([A-Za-z])|\u001b\][^\u0007\u001b]*(?:\u0007|\u001b\\)/g;

/** True when the text carries terminal control sequences at all. */
export function hasAnsi(text: string): boolean {
  return typeof text === 'string' && text.includes('\u001b');
}

/** The 16 base colours, for the 256-colour cube and true-colour forms. */
const BASE_RGB = [
  '#000000', '#cd0000', '#00cd00', '#cdcd00', '#0000ee', '#cd00cd', '#00cdcd', '#e5e5e5',
  '#7f7f7f', '#ff0000', '#00ff00', '#ffff00', '#5c5cff', '#ff00ff', '#00ffff', '#ffffff',
];

/** `#rrggbb` for one index of the 256-colour palette (16 base, cube, then greys). */
function rgbOf(index: number): string {
  if (index < 16) return BASE_RGB[index];
  const hex = (value: number): string => value.toString(16).padStart(2, '0');
  if (index < 232) {
    const n = index - 16;
    const level = (value: number): number => (value === 0 ? 0 : 55 + value * 40);
    return `#${hex(level(Math.floor(n / 36)))}${hex(level(Math.floor((n % 36) / 6)))}${hex(level(n % 6))}`;
  }
  const grey = 8 + (index - 232) * 10;
  return `#${hex(grey)}${hex(grey)}${hex(grey)}`;
}

interface AnsiState {
  attributes: string[];
  foreground: string | null;
  background: string | null;
}

function reset(state: AnsiState): void {
  state.attributes = [];
  state.foreground = null;
  state.background = null;
}

/** Apply one SGR parameter list; unknown parameters are ignored. */
function applySgr(state: AnsiState, params: number[]): void {
  for (let i = 0; i < params.length; i += 1) {
    const code = params[i];
    if (code === 0) reset(state);
    else if (code === 1) state.attributes.push('font-semibold');
    else if (code === 2) state.attributes.push('opacity-70');
    else if (code === 3) state.attributes.push('italic');
    else if (code === 4) state.attributes.push('underline');
    else if (code === 22) state.attributes = state.attributes.filter((a) => a !== 'font-semibold');
    else if (code === 23) state.attributes = state.attributes.filter((a) => a !== 'italic');
    else if (code === 24) state.attributes = state.attributes.filter((a) => a !== 'underline');
    else if (code === 7) state.attributes.push('bg-gray-900', 'text-gray-100');
    else if (code === 27) state.attributes = state.attributes.filter((a) => a !== 'bg-gray-900' && a !== 'text-gray-100');
    else if (code === 39) state.foreground = null;
    else if (code === 49) state.background = null;
    else if (FOREGROUND[code] !== undefined) state.foreground = FOREGROUND[code];
    else if (BACKGROUND[code] !== undefined) state.background = BACKGROUND[code];
    else if (code === 38 || code === 48) {
      const extended = params[i + 1];
      let value: string | null = null;
      if (extended === 5 && params[i + 2] !== undefined) {
        value = rgbOf(params[i + 2]);
        i += 2;
      } else if (extended === 2 && params[i + 4] !== undefined) {
        value = `#${[params[i + 2], params[i + 3], params[i + 4]]
          .map((channel) => Math.max(0, Math.min(255, channel)).toString(16).padStart(2, '0'))
          .join('')}`;
        i += 4;
      }
      if (value !== null) {
        if (code === 38) state.foreground = value;
        else state.background = value;
      }
    }
  }
}

function spanOf(text: string, state: AnsiState): AnsiSpan {
  const classes = [...state.attributes];
  const colour = (value: string | null): string | undefined =>
    value !== null && !value.startsWith('#') ? value : undefined;
  const explicit = (value: string | null): string | undefined =>
    value !== null && value.startsWith('#') ? value : undefined;
  if (colour(state.foreground)) classes.push(colour(state.foreground) as string);
  if (colour(state.background)) classes.push(colour(state.background) as string);
  const span: AnsiSpan = { text, className: classes.join(' ') };
  const color = explicit(state.foreground);
  const backgroundColor = explicit(state.background);
  if (color !== undefined) span.color = color;
  if (backgroundColor !== undefined) span.backgroundColor = backgroundColor;
  return span;
}

/**
 * Split terminal output into styled runs.
 *
 * A run is only ever created from the text between escapes, so joining every
 * span reproduces the visible output exactly.
 */
export function parseAnsi(text: string): AnsiSpan[] {
  const source = typeof text === 'string' ? text : '';
  const spans: AnsiSpan[] = [];
  const state: AnsiState = { attributes: [], foreground: null, background: null };
  let cursor = 0;
  ESCAPE.lastIndex = 0;
  for (let match = ESCAPE.exec(source); match !== null; match = ESCAPE.exec(source)) {
    if (match.index > cursor) spans.push(spanOf(source.slice(cursor, match.index), state));
    cursor = match.index + match[0].length;
    if (match[2] === 'm') {
      const params = match[1] === '' ? [0] : match[1].split(';').map((part) => Number(part) || 0);
      applySgr(state, params);
    }
  }
  if (cursor < source.length) spans.push(spanOf(source.slice(cursor), state));
  return spans.filter((span) => span.text !== '');
}