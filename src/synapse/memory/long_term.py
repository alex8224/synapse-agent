"""Long-term memory backed by SQLite + embedding vectors.

Design (inspired by x-agent's LongTermMemory):

    - Entries are stored as (id, text, metadata_json, embedding_blob, created_at).
    - Retrieval uses cosine similarity between the query embedding and stored
      vectors.
    - The embedder is injected via the ``EmbeddingProvider`` protocol so the
      caller can choose LocalEmbedder / SimpleEmbedder / a remote API.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from synapse.memory.embedder import EmbeddingProvider, _build_default_embedder

# SQLite BLOB packing: 32-bit little-endian IEEE 754 floats.
_HEADER = b"LTMF"  # magic
_VERSION = 1


def _pack_floats(values: list[float]) -> bytes:
    """Pack list[float] → compact BLOB (4 bytes per element)."""
    import struct

    return struct.pack(f"<{len(values)}f", *values)


def _unpack_floats(blob: bytes) -> list[float]:
    import struct

    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob))


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0


# ---------------------------------------------------------------------------
# LongTermMemory
# ---------------------------------------------------------------------------


class LongTermMemory:
    """Persistent, searchable memory store.

    Usage::

        ltm = LongTermMemory("mem.db")
        await ltm.remember("The auth module uses JWT tokens with 15 min expiry.")
        results = await ltm.recall("how does auth work?", top_k=3)
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        embedder: EmbeddingProvider | None = None,
        auto_remember_prompts: bool = False,
    ) -> None:
        self._path = Path(db_path)
        self._embedder = embedder or _build_default_embedder()
        self._auto_remember_prompts = auto_remember_prompts
        self._dim: int | None = None
        self._init_db()

    # -- public API -----------------------------------------------------------

    async def remember(
        self,
        text: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Store a memory entry. Returns the entry id."""
        import uuid

        entry_id = uuid.uuid4().hex[:16]
        meta = dict(metadata or {})
        created_at = time.time()

        [embedding] = self._embedder.embed([text])
        self._ensure_dim(len(embedding))

        blob = _pack_floats(embedding)

        def _write() -> None:
            with sqlite3.connect(str(self._path)) as conn:
                conn.execute(
                    """INSERT INTO memories (id, text, metadata, embedding, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (entry_id, text, json.dumps(meta, ensure_ascii=True), blob, created_at),
                )
                conn.commit()

        import asyncio

        await asyncio.to_thread(_write)
        return entry_id

    async def recall(
        self,
        query: str,
        *,
        top_k: int = 5,
        min_similarity: float = 0.0,
    ) -> list[MemoryEntry]:
        """Search for memories similar to ``query``."""

        [query_vec] = self._embedder.embed([query])
        self._ensure_dim(len(query_vec))

        def _search() -> list[MemoryEntry]:
            results: list[tuple[float, str, str, str, float]] = []
            with sqlite3.connect(str(self._path)) as conn:
                rows = conn.execute(
                    "SELECT id, text, metadata, embedding, created_at FROM memories"
                ).fetchall()
            for row_id, text, meta_json, blob, created_at in rows:
                stored = _unpack_floats(blob)
                sim = self._cosine_similarity(query_vec, stored)
                if sim >= min_similarity:
                    results.append((sim, row_id, text, meta_json, created_at))
            results.sort(key=lambda x: x[0], reverse=True)
            top = results[:top_k]
            return [
                MemoryEntry(
                    id=rid,
                    text=text,
                    metadata=json.loads(meta_json) if meta_json else {},
                    created_at=created_at,
                )
                for _, rid, text, meta_json, created_at in top
            ]

        import asyncio

        return await asyncio.to_thread(_search)

    async def forget(self, entry_id: str) -> bool:
        """Delete a memory. Returns True if something was deleted."""

        def _delete() -> bool:
            with sqlite3.connect(str(self._path)) as conn:
                cur = conn.execute("DELETE FROM memories WHERE id = ?", (entry_id,))
                conn.commit()
                return cur.rowcount > 0

        import asyncio

        return await asyncio.to_thread(_delete)

    async def stats(self) -> dict[str, int]:
        def _stats():
            with sqlite3.connect(str(self._path)) as conn:
                count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            return {"total_entries": count}

        import asyncio

        return await asyncio.to_thread(_stats)

    # -- helpers --------------------------------------------------------------

    def _init_db(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self._path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS memories (
                       id          TEXT PRIMARY KEY,
                       text        TEXT    NOT NULL,
                       metadata    TEXT    NOT NULL DEFAULT '{}',
                       embedding   BLOB    NOT NULL,
                       created_at  REAL    NOT NULL
                   )"""
            )
            conn.commit()

    def _ensure_dim(self, dim: int) -> None:
        if self._dim is None:
            self._dim = dim
        elif self._dim != dim:
            raise ValueError(
                f"Embedding dimension changed ({self._dim} → {dim}). "
                "Re-create the store or use a consistent embedder."
            )

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b, strict=False))
        norm_a = math.sqrt(sum(x * x for x in a)) or 1.0
        norm_b = math.sqrt(sum(y * y for y in b)) or 1.0
        return dot / (norm_a * norm_b)


__all__ = ["LongTermMemory", "MemoryEntry"]
