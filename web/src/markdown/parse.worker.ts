import { parseMarkdown } from './parse.ts';
import type { ParseRequest, ParseResponse } from './parseQueue.ts';

// This module runs only in a Worker. Keep DOM APIs out of the parsing path;
// React still owns all escaping, rendering and markup sanitization on the UI.
const port = globalThis as unknown as {
  onmessage: (event: { data: ParseRequest }) => void;
  postMessage(response: ParseResponse): void;
};

port.onmessage = ({ data }) => {
  try {
    const blocks = parseMarkdown(data.text).map((block) => ({
      key: JSON.stringify(block),
      block,
    }));
    port.postMessage({ id: data.id, blocks });
  } catch {
    // A pathological/malformed document must remain readable without falling
    // back to a synchronous parse or killing the worker for other documents.
    port.postMessage({ id: data.id, blocks: null });
  }
};
