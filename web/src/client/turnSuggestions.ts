import {
  CONSOLE_CSRF_HEADER,
  CONSOLE_CSRF_HEADER_VALUE,
  type FetchLike,
} from './bootstrap.ts';
import type { ModelSuggestionInput } from '../stores/turnSuggestions.ts';

/**
 * Post-turn side-channel suggestion client (`POST /api/turn-suggestions`).
 *
 * Strictly whitelist-decodes `{ suggestions: [{ label, prompt, mode }] }` and
 * degrades to an empty array on any HTTP/network/parsing failure so the
 * console's deterministic rule suggestions remain uninterrupted.
 */
export const TURN_SUGGESTIONS_PATH = '/api/turn-suggestions';

export const MAX_SUGGESTION_LABEL_CHARS = 24;
export const MAX_SUGGESTION_PROMPT_CHARS = 240;
export const MAX_SUGGESTION_ITEMS = 3;

export interface TurnSuggestionsRequest {
  user_prompt: string;
  conclusion: string;
  project_id?: string;
  model?: string | null;
}

export function parseTurnSuggestionsPayload(payload: unknown): ModelSuggestionInput[] {
  if (payload === null || typeof payload !== 'object') {
    return [];
  }
  const rawList = (payload as { suggestions?: unknown }).suggestions;
  if (!Array.isArray(rawList)) {
    return [];
  }
  const results: ModelSuggestionInput[] = [];
  for (const entry of rawList) {
    if (entry === null || typeof entry !== 'object') continue;
    const record = entry as Record<string, unknown>;
    const rawLabel = record['label'];
    const rawPrompt = record['prompt'];
    const rawMode = record['mode'];
    if (typeof rawLabel !== 'string' || typeof rawPrompt !== 'string') continue;
    const label = rawLabel.trim().slice(0, MAX_SUGGESTION_LABEL_CHARS);
    const prompt = rawPrompt.trim().slice(0, MAX_SUGGESTION_PROMPT_CHARS);
    if (!label || !prompt) continue;
    results.push({
      label,
      prompt,
      mode: rawMode === 'fill' ? 'fill' : 'submit',
    });
    if (results.length >= MAX_SUGGESTION_ITEMS) break;
  }
  return results;
}

export async function fetchTurnSuggestions(
  request: TurnSuggestionsRequest,
  fetchImpl: FetchLike = fetch,
): Promise<ModelSuggestionInput[]> {
  const conclusion = (request.conclusion ?? '').trim();
  if (!conclusion) return [];
  let response: Response;
  try {
    response = await fetchImpl(TURN_SUGGESTIONS_PATH, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
        [CONSOLE_CSRF_HEADER]: CONSOLE_CSRF_HEADER_VALUE,
      },
      body: JSON.stringify({
        user_prompt: (request.user_prompt ?? '').slice(0, 1000),
        conclusion: conclusion.slice(0, 2500),
        ...(request.project_id ? { project_id: request.project_id } : {}),
        ...(request.model ? { model: request.model } : {}),
      }),
    });
  } catch {
    return [];
  }
  if (!response.ok) {
    return [];
  }
  try {
    const payload: unknown = await response.json();
    return parseTurnSuggestionsPayload(payload);
  } catch {
    return [];
  }
}
