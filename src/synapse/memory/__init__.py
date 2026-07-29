"""Memory module: long-term knowledge retention across sessions.

Exports:
    AutoRecorder        – importance scoring + automatic lesson extraction
    EmbeddingProvider   – protocol for embedding backends
    LocalEmbedder       – sentence-transformers based embedder (optional)
    SimpleEmbedder      – lightweight TF-IDF fallback (no extra deps)
    LongTermMemory      – persistent vector-backed memory store
"""

from __future__ import annotations

from synapse.memory.auto_recorder import AutoRecorder
from synapse.memory.embedder import (
    EmbeddingProvider,
    LocalEmbedder,
    SimpleEmbedder,
    _build_default_embedder,
)
from synapse.memory.long_term import LongTermMemory, MemoryEntry

__all__ = [
    "AutoRecorder",
    "EmbeddingProvider",
    "LocalEmbedder",
    "LongTermMemory",
    "MemoryEntry",
    "SimpleEmbedder",
    "_build_default_embedder",
]
