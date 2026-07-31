"""Tests for core.context.vector_store."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.context.chunking import Chunk
from core.context.embeddings import VECTOR_SIZE
from core.context.vector_store import VectorStore


def _chunk(path: str = "a.py", name: str = "foo") -> Chunk:
    return Chunk(path=path, kind="function", name=name, start_line=1, end_line=2, text="def foo(): pass")


def _vector(seed: float = 0.1) -> list[float]:
    return [seed] * VECTOR_SIZE


def test_search_before_any_indexing_returns_empty(tmp_path: Path) -> None:
    store = VectorStore(tmp_path / "qdrant_db")

    assert store.search(_vector(), limit=5) == []


def test_reset_add_search_round_trip(tmp_path: Path) -> None:
    store = VectorStore(tmp_path / "qdrant_db")
    store.reset()
    store.add([_chunk()], [_vector()])

    hits = store.search(_vector(), limit=5)

    assert len(hits) == 1
    assert hits[0]["path"] == "a.py"
    assert hits[0]["name"] == "foo"


def test_add_with_no_chunks_is_a_no_op(tmp_path: Path) -> None:
    store = VectorStore(tmp_path / "qdrant_db")
    store.reset()
    store.add([], [])

    assert store.search(_vector(), limit=5) == []


def test_reset_clears_previous_data(tmp_path: Path) -> None:
    store = VectorStore(tmp_path / "qdrant_db")
    store.reset()
    store.add([_chunk()], [_vector()])
    store.reset()

    assert store.search(_vector(), limit=5) == []


def test_search_returns_the_similarity_score(tmp_path: Path) -> None:
    """The score used to be dropped here, which left callers with an order but
    no way to tell a strong match from a weak one — and so no way to set a
    relevance floor."""
    store = VectorStore(tmp_path / "qdrant_db")
    store.reset()
    store.add([_chunk()], [_vector()])

    (hit,) = store.search(_vector(), limit=5)

    assert hit["score"] == pytest.approx(1.0)  # cosine against itself


def test_search_scores_a_closer_vector_higher(tmp_path: Path) -> None:
    store = VectorStore(tmp_path / "qdrant_db")
    store.reset()
    near = [1.0] + [0.0] * (VECTOR_SIZE - 1)
    far = [0.0] * (VECTOR_SIZE - 1) + [1.0]
    store.add([_chunk("near.py"), _chunk("far.py")], [near, far])

    hits = store.search(near, limit=5)

    assert hits[0]["path"] == "near.py"
    assert hits[0]["score"] > hits[1]["score"]
