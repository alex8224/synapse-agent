"""Project document indexer + retriever.

Design:
    - ``_discover_docs()`` walks the project root and collects markdown, Python,
      reStructuredText and plain-text files (skipping venv / node_modules / .git
      / __pycache__ etc.).
    - ``_chunk()`` splits long texts at paragraph / sentence boundaries so each
      chunk stays under *max_chars* (default 1 500).
    - Vectors are produced by the same ``EmbeddingProvider`` used by the memory
      module – shared infrastructure.
"""

from __future__ import annotations

import math
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from synapse.memory.embedder import EmbeddingProvider, _build_default_embedder
from synapse.memory.long_term import _pack_floats, _unpack_floats

# Directories / file-name patterns to skip during discovery.
_EXCLUDE_DIRS: set[str] = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
    ".tox",
    ".eggs",
}

_EXCLUDE_FILE_SUFFIXES: tuple[str, ...] = (
    ".pyc",
    ".pyo",
    ".so",
    ".dll",
    ".pyd",
    ".exe",
    ".bin",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".lock",
)

_INCLUDE_SUFFIXES: tuple[str, ...] = (".md", ".py", ".rst", ".txt", ".toml", ".yaml", ".yml")

# -- data model ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KnowledgeChunk:
    source: str  # relative path from project root
    text: str
    index: int  # chunk number within source
    total: int  # total chunks for source


# -- chunker ------------------------------------------------------------------


def _chunk(text: str, *, max_chars: int = 1500) -> list[str]:
    """Split *text* into chunks of at most *max_chars* characters,
    trying to break at paragraph boundaries.
    """
    if len(text) <= max_chars:
        return [text]

    # Split on double-newline (paragraphs)
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    def _flush() -> None:
        nonlocal current, current_len
        if current:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0

    for para in paragraphs:
        para_len = len(para)
        if current_len + para_len > max_chars and current:
            _flush()
        current.append(para)
        current_len += para_len

    _flush()

    # If any chunk is still too large, split at sentence boundaries.
    final: list[str] = []
    for chunk_text in chunks:
        if len(chunk_text) <= max_chars:
            final.append(chunk_text)
            continue
        sentences = chunk_text.replace("\n", " ").split(". ")
        buf = ""
        for sent in sentences:
            candidate = (buf + ". " + sent).strip() if buf else sent
            if len(candidate) > max_chars and buf:
                final.append(buf.strip())
                buf = sent
            else:
                buf = candidate
        if buf:
            final.append(buf.strip())

    return final or [text]


# -- knowledge base -----------------------------------------------------------


class ProjectKnowledgeBase:
    """Index + search over project documentation.

    Usage::

        kb = ProjectKnowledgeBase(project_root=".")
        await kb.index()
        results = await kb.search("how does auth work?", top_k=3)
    """

    def __init__(
        self,
        project_root: str | Path,
        *,
        embedder: EmbeddingProvider | None = None,
        max_files: int = 300,
        max_chars_per_chunk: int = 1500,
        db_path: str | Path | None = None,
    ) -> None:
        self._root = Path(project_root).resolve()
        self._embedder = embedder or _build_default_embedder()
        self._max_files = max_files
        self._max_chars = max_chars_per_chunk
        self._db_path = Path(db_path) if db_path else (self._root / ".synapse" / "knowledge.sqlite")
        self._indexed_at: float | None = None
        self._init_db()

    # -- public API -----------------------------------------------------------

    async def index(self, *, force: bool = False) -> int:
        """(Re-)index the project. Returns number of chunks indexed."""
        if not force and self._indexed_at is not None:
            # Incremental: skip files that haven't changed since last index.
            return self._count_chunks()

        docs = self._discover_docs()
        chunk_count = 0

        for doc_path in docs[: self._max_files]:
            try:
                text = doc_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if not text.strip():
                continue

            rel = str(doc_path.relative_to(self._root))
            chunks = _chunk(text, max_chars=self._max_chars)
            embeddings = self._embedder.embed(chunks)

            for idx, (chunk_text, vec) in enumerate(zip(chunks, embeddings)):
                await self._upsert_chunk(
                    source=rel,
                    text=chunk_text,
                    index=idx,
                    total=len(chunks),
                    embedding=vec,
                )
                chunk_count += 1

        self._indexed_at = time.time()
        return chunk_count

    async def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        min_similarity: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Search for relevant document chunks."""
        [query_vec] = self._embedder.embed([query])

        def _search():
            results: list[tuple[float, str, str, int, int]] = []
            with sqlite3.connect(str(self._db_path)) as conn:
                rows = conn.execute(
                    "SELECT source, text, chunk_index, chunk_total, embedding FROM knowledge"
                ).fetchall()
            for source, text, idx, total_count, blob in rows:
                stored = _unpack_floats(blob)
                sim = self._cosine_similarity(query_vec, stored)
                if sim >= min_similarity:
                    results.append((sim, source, text, idx, total_count))
            results.sort(key=lambda x: x[0], reverse=True)
            return results[:top_k]

        import asyncio

        rows = await asyncio.to_thread(_search)
        return [
            {
                "source": src,
                "text": txt,
                "chunk_index": idx,
                "chunk_total": total,
                "similarity": round(sim, 4),
            }
            for sim, src, txt, idx, total in rows
        ]

    async def stats(self) -> dict[str, Any]:
        def _stats():
            with sqlite3.connect(str(self._db_path)) as conn:
                chunks = conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
                sources = conn.execute(
                    "SELECT COUNT(DISTINCT source) FROM knowledge"
                ).fetchone()[0]
            return {"total_chunks": chunks, "total_sources": sources}

        import asyncio

        return await asyncio.to_thread(_stats)

    # -- internals ------------------------------------------------------------

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS knowledge (
                       source       TEXT    NOT NULL,
                       text         TEXT    NOT NULL,
                       chunk_index  INTEGER NOT NULL,
                       chunk_total  INTEGER NOT NULL,
                       embedding    BLOB    NOT NULL,
                       updated_at   REAL    NOT NULL DEFAULT 0,
                       PRIMARY KEY (source, chunk_index)
                   )"""
            )
            conn.commit()

    async def _upsert_chunk(
        self,
        source: str,
        text: str,
        index: int,
        total: int,
        embedding: list[float],
    ) -> None:
        blob = _pack_floats(embedding)
        now = time.time()

        def _write():
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO knowledge
                       (source, text, chunk_index, chunk_total, embedding, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (source, text, index, total, blob, now),
                )
                conn.commit()

        import asyncio

        await asyncio.to_thread(_write)

    def _count_chunks(self) -> int:
        with sqlite3.connect(str(self._db_path)) as conn:
            return conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]

    def _discover_docs(self) -> list[Path]:
        """Find indexable files in the project root."""
        result: list[Path] = []
        for suffix in _INCLUDE_SUFFIXES:
            for path in self._root.rglob(f"*{suffix}"):
                # Skip excluded directories
                parts = set(path.relative_to(self._root).parts)
                if parts & _EXCLUDE_DIRS:
                    continue
                if path.name.endswith(_EXCLUDE_FILE_SUFFIXES):
                    continue
                # Skip files inside hidden dirs (but not the root .synapse)
                if any(p.startswith(".") and p not in (".synapse",) for p in path.relative_to(self._root).parts):
                    continue
                result.append(path)
        # Deduplicate (a file might match multiple suffixes)
        seen: set[str] = set()
        unique: list[Path] = []
        for p in result:
            key = str(p.resolve())
            if key not in seen:
                seen.add(key)
                unique.append(p)
        return unique

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b, strict=False))
        norm_a = math.sqrt(sum(x * x for x in a)) or 1.0
        norm_b = math.sqrt(sum(y * y for y in b)) or 1.0
        return dot / (norm_a * norm_b)


__all__ = ["KnowledgeChunk", "ProjectKnowledgeBase"]
