"""Tests for core.context.chunking."""

from __future__ import annotations

from pathlib import Path

from core.context.chunking import chunk_file, iter_source_files


def test_iter_source_files_ignores_dirs(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    ignored_dir = tmp_path / ".venv"
    ignored_dir.mkdir()
    (ignored_dir / "b.py").write_text("y = 2\n")

    files = iter_source_files(tmp_path)

    assert [f.name for f in files] == ["a.py"]


def test_iter_source_files_ignores_non_indexable_suffix(tmp_path: Path) -> None:
    (tmp_path / "image.png").write_bytes(b"\x89PNG")

    assert iter_source_files(tmp_path) == []


def test_chunk_file_python_splits_by_function_and_class(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    path.write_text(
        "def foo():\n    return 1\n\n\nclass Bar:\n    pass\n",
        encoding="utf-8",
    )

    chunks = chunk_file(tmp_path, path)

    kinds = {(c.kind, c.name) for c in chunks}
    assert ("function", "foo") in kinds
    assert ("class", "Bar") in kinds


def test_chunk_file_python_empty_file_returns_nothing(tmp_path: Path) -> None:
    path = tmp_path / "empty.py"
    path.write_text("", encoding="utf-8")

    assert chunk_file(tmp_path, path) == []


def test_chunk_file_python_falls_back_to_whole_file(tmp_path: Path) -> None:
    path = tmp_path / "consts.py"
    path.write_text("A = 1\nB = 2\n", encoding="utf-8")

    chunks = chunk_file(tmp_path, path)

    assert len(chunks) == 1
    assert chunks[0].kind == "file"


def test_chunk_file_markdown_splits_by_section(tmp_path: Path) -> None:
    path = tmp_path / "doc.md"
    path.write_text("# Title\nintro\n\n## Section A\nfoo\n\n## Section B\nbar\n", encoding="utf-8")

    chunks = chunk_file(tmp_path, path)

    names = [c.name for c in chunks]
    assert "Section A" in names
    assert "Section B" in names


def test_chunk_file_markdown_single_header_stays_whole_file(tmp_path: Path) -> None:
    path = tmp_path / "doc.md"
    path.write_text("# Title\njust one section\n", encoding="utf-8")

    chunks = chunk_file(tmp_path, path)

    assert len(chunks) == 1
    assert chunks[0].kind == "file"


def test_chunk_file_other_suffix_is_whole_file(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("key: value\n", encoding="utf-8")

    chunks = chunk_file(tmp_path, path)

    assert len(chunks) == 1
    assert chunks[0].kind == "file"
    assert chunks[0].path == "config.yaml"
