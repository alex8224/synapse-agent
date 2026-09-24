import type { Block } from './parse.ts';

/** Keep the same bounded-document fallback as the synchronous renderer. */
export const PARSE_MAX_CHARS = 200_000;

export interface ParseRequest {
  id: number;
  text: string;
}

export interface ParsedBlock {
  /** Exact structural identity, computed off-thread, not a collision-prone hash. */
  key: string;
  block: Block;
}

export interface ParseResponse {
  id: number;
  blocks: ParsedBlock[] | null;
}

export interface ParseWorker {
  postMessage(request: ParseRequest): void;
  onmessage: ((event: MessageEvent<ParseResponse>) => void) | null;
  onerror: ((event: ErrorEvent) => void) | null;
  onmessageerror: ((event: MessageEvent<unknown>) => void) | null;
  terminate(): void;
}

export interface MarkdownSnapshot {
  source: string;
  blocks: Block[] | null;
}

export interface MarkdownDocument {
  update(text: string): void;
  dispose(): void;
}

interface DocumentState {
  text: string;
  blocks: ParsedBlock[];
  receive: (snapshot: MarkdownSnapshot) => void;
}

interface Job {
  id: number;
  document: DocumentState;
  text: string;
}

/**
 * One worker for the mounted documents, with one in-flight parse and at most one
 * pending source per document. A fast stream replaces pending work rather than
 * filling the worker's message queue with obsolete copies of a growing answer.
 * No parser runs on the UI thread, including the worker-failure path.
 */
export class MarkdownParseQueue {
  private worker: ParseWorker | null = null;
  private failed = false;
  private nextId = 0;
  private active: Job | null = null;
  private documents = new Set<DocumentState>();
  private pending = new Map<DocumentState, string>();
  private createWorker: () => ParseWorker;

  constructor(createWorker: () => ParseWorker) {
    this.createWorker = createWorker;
  }

  open(receive: (snapshot: MarkdownSnapshot) => void): MarkdownDocument {
    const document: DocumentState = { text: '', blocks: [], receive };
    this.documents.add(document);
    return {
      update: (text) => {
        if (!this.documents.has(document)) return;
        document.text = text;
        if (this.failed || text.length > PARSE_MAX_CHARS || text === '') {
          this.pending.delete(document);
          document.blocks = [];
          receive({ source: text, blocks: text === '' ? [] : null });
          return;
        }
        this.pending.set(document, text);
        this.pump();
      },
      dispose: () => {
        this.documents.delete(document);
        this.pending.delete(document);
        document.blocks = [];
        // A virtualized transcript must not retain sources for unmounted rows.
        if (this.documents.size === 0) {
          this.worker?.terminate();
          this.worker = null;
          this.active = null;
        }
      },
    };
  }

  private pump(): void {
    if (this.failed || this.active !== null || this.pending.size === 0) return;
    const [document, text] = this.pending.entries().next().value!;
    this.pending.delete(document);
    const job: Job = { id: ++this.nextId, document, text };
    this.active = job;
    try {
      if (this.worker === null) {
        const worker = this.createWorker();
        this.worker = worker;
        worker.onmessage = (event) => {
          if (this.worker === worker) this.complete(event.data);
        };
        worker.onerror = () => {
          if (this.worker === worker) this.fail();
        };
        worker.onmessageerror = () => {
          if (this.worker === worker) this.fail();
        };
      }
      this.worker.postMessage({ id: job.id, text });
    } catch {
      // CSP, an unavailable Worker API, or a missing build chunk: keep the full
      // source readable, never put the expensive parse back on the UI thread.
      this.fail();
    }
  }

  private complete(response: ParseResponse): void {
    const job = this.active;
    if (job === null || job.id !== response.id) return;
    this.active = null;
    const document = job.document;
    if (this.documents.has(document) && document.text.length <= PARSE_MAX_CHARS &&
        document.text.startsWith(job.text)) {
      // A slightly older prefix may paint while the next parse is in flight.
      // Dropping every non-latest result would starve a stream faster than the
      // worker. A replacement, in contrast, must never display the old answer.
      const blocks = response.blocks?.map((entry, index) => {
        const previous = document.blocks[index];
        return previous?.key === entry.key ? previous : entry;
      }) ?? null;
      document.blocks = blocks ?? [];
      document.receive({ source: job.text, blocks: blocks?.map((entry) => entry.block) ?? null });
    }
    this.pump();
  }

  private fail(): void {
    this.failed = true;
    this.worker?.terminate();
    this.worker = null;
    this.active = null;
    this.pending.clear();
    for (const document of this.documents) {
      document.blocks = [];
      document.receive({ source: document.text, blocks: null });
    }
  }
}
