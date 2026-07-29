"""Embedding provider protocol and concrete backends.

Design:
    - ``EmbeddingProvider`` is a simple Protocol – swap backends without touching
      the rest of the memory / rag code.
    - ``LocalEmbedder`` uses ``sentence-transformers`` (pip install optional).
    - ``SimpleEmbedder`` is a pure-Python TF-IDF / token-overlap fallback that
      requires zero extra dependencies.  It is accurate enough for small
      project-scale retrieval.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Protocol


class EmbeddingProvider(Protocol):
    """Produce a fixed-size vector for each input text."""

    def embed(self, texts: list[str]) -> list[list[float]]: ...


# ---------------------------------------------------------------------------
# LocalEmbedder – sentence-transformers  (optional dependency)
# ---------------------------------------------------------------------------

_LOCAL_MODEL_NAME = "all-MiniLM-L6-v2"
_LOCAL_DIM = 384


class LocalEmbedder:
    """Small sentence-transformers model (local, no API key needed)."""

    def __init__(
        self,
        model_name: str = _LOCAL_MODEL_NAME,
        *,
        normalize: bool = True,
    ) -> None:
        self._model_name = model_name
        self._normalize = normalize
        self._model: object | None = None

    @property
    def dim(self) -> int:
        if self._model is not None:
            return getattr(self._model, "get_sentence_embedding_dimension", lambda: _LOCAL_DIM)()
        return _LOCAL_DIM

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ImportError(
                "LocalEmbedder requires 'sentence-transformers'. "
                "Install with: pip install sentence-transformers"
            ) from exc
        self._model = SentenceTransformer(self._model_name)

    def embed(self, texts: list[str]) -> list[list[float]]:
        self._load()
        assert self._model is not None
        vectors = self._model.encode(  # type: ignore[union-attr]
            texts,
            normalize_embeddings=self._normalize,
            show_progress_bar=False,
        )
        return [v.tolist() for v in vectors]


# ---------------------------------------------------------------------------
# SimpleEmbedder – zero-dependency TF-IDF fallback
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _idf_weight(df: int, total: int) -> float:
    return math.log((total + 1) / (df + 1)) + 1.0


class SimpleEmbedder:
    """Deterministic TF-IDF embedder.  Good enough for small document sets.

    All vectors share the same vocabulary (built from the corpus), so
    cosine-similarity comparisons are meaningful.
    """

    def __init__(self, *, dim: int = 256) -> None:
        self._dim = dim
        self._vocab: list[str] = []        # ordered term list
        self._term_to_idx: dict[str, int] = {}
        self._df: Counter[str] = Counter()  # document frequency
        self._doc_count: int = 0
        self._fitted: bool = False

    @property
    def dim(self) -> int:
        return self._dim

    # -- public API -----------------------------------------------------------

    def fit(self, corpus: list[str]) -> None:
        """Build vocabulary from a representative corpus."""
        docs = [_tokenize(doc) for doc in corpus]
        # Collect term frequencies
        tf_global: Counter[str] = Counter()
        for tokens in docs:
            unique = set(tokens)
            tf_global.update(tokens)
            for term in unique:
                self._df[term] += 1
            self._doc_count += 1
        # Select top terms by global TF
        top = tf_global.most_common(self._dim)
        self._vocab = [term for term, _ in top]
        self._term_to_idx = {term: idx for idx, term in enumerate(self._vocab)}
        self._fitted = True

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self._fitted:
            # Auto-fit on the input texts so embed() works standalone.
            self.fit(texts)
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        tokens = _tokenize(text)
        tf = Counter(tokens)
        vec = [0.0] * self._dim
        norm_sq = 0.0
        for term, count in tf.items():
            idx = self._term_to_idx.get(term)
            if idx is None:
                continue
            weight = count * _idf_weight(self._df.get(term, 0), self._doc_count or 1)
            vec[idx] = weight
            norm_sq += weight * weight
        norm = math.sqrt(norm_sq) or 1.0
        return [v / norm for v in vec]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def _build_default_embedder() -> EmbeddingProvider:
    """Try LocalEmbedder first, fall back to SimpleEmbedder."""
    try:
        return LocalEmbedder()  # type: ignore[return-value]
    except ImportError:
        return SimpleEmbedder()  # type: ignore[return-value]


__all__ = [
    "EmbeddingProvider",
    "LocalEmbedder",
    "SimpleEmbedder",
    "_build_default_embedder",
]
