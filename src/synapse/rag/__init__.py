"""RAG module: project knowledge base indexing and retrieval.

Inspired by x-agent's RAG Pipeline, simplified for Synapse:
    - Index project documents (.md / .py / .rst / .txt)
    - Chunk large files into ~1 500-character segments
    - Embed + store in memory (or SQLite-backed)
    - Retrieve top-k relevant chunks at query time
"""

from synapse.rag.knowledge_base import KnowledgeChunk, ProjectKnowledgeBase

__all__ = ["KnowledgeChunk", "ProjectKnowledgeBase"]
