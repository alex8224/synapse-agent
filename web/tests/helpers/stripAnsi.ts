/**
 * Strip ANSI colour codes from captured dev-server output.
 *
 * Written without a literal control character in the pattern so the shared
 * `no-control-regex` lint rule stays clean for the whole project.
 */
const ESCAPE = String.fromCharCode(27)

export function stripAnsi(text: string): string {
  return text
    .split(ESCAPE)
    .map((part, index) => (index === 0 ? part : part.replace(/^\[[0-9;]*m/, '')))
    .join('')
}