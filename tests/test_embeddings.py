"""Tests for core.context.embeddings."""

from __future__ import annotations

from core.context.embeddings import VECTOR_SIZE, embed_query, embed_texts


def test_embed_texts_empty_list_returns_empty() -> None:
    assert embed_texts([]) == []


def test_embed_texts_returns_vectors_of_the_expected_dimension() -> None:
    vectors = embed_texts(["hello world"])

    assert len(vectors) == 1
    assert len(vectors[0]) == VECTOR_SIZE


def test_embed_texts_multiple_inputs() -> None:
    vectors = embed_texts(["a", "b", "c"])

    assert len(vectors) == 3


def test_embed_query_returns_a_single_vector() -> None:
    vector = embed_query("where is authentication handled?")

    assert len(vector) == VECTOR_SIZE
