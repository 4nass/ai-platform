"""Tests for core.memory.loader."""

from __future__ import annotations

from pathlib import Path

from core.memory.loader import load_memory_docs


def test_load_memory_docs_missing_dir(tmp_path: Path) -> None:
    assert load_memory_docs(tmp_path) == {}


def test_load_memory_docs_empty_dir(tmp_path: Path) -> None:
    (tmp_path / "memory").mkdir()

    assert load_memory_docs(tmp_path) == {}


def test_load_memory_docs_skips_empty_files(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "empty.md").write_text("   \n", encoding="utf-8")
    (memory_dir / "rules.md").write_text("Always write tests.", encoding="utf-8")

    docs = load_memory_docs(tmp_path)

    assert docs == {"rules.md": "Always write tests."}


def test_load_memory_docs_ignores_non_markdown(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "notes.txt").write_text("not markdown", encoding="utf-8")

    assert load_memory_docs(tmp_path) == {}
