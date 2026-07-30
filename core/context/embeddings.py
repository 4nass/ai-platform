"""Local embeddings (sentence-transformers) for indexing and search."""

from __future__ import annotations

from functools import lru_cache

MODEL_NAME = "all-MiniLM-L6-v2"
VECTOR_SIZE = 384


@lru_cache(maxsize=1)
def _model():
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(MODEL_NAME)


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    return _model().encode(texts, convert_to_numpy=True, show_progress_bar=False).tolist()


def embed_query(text: str) -> list[float]:
    return embed_texts([text])[0]
