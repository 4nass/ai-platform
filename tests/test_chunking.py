"""Tests for core.context.chunking."""

from __future__ import annotations

from pathlib import Path

import git

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


def test_iter_source_files_skips_gitignored_files(tmp_path: Path) -> None:
    """Anything indexed here is embedded and shipped to the model, so a
    gitignored secrets file must never make it into the index."""
    git.Repo.init(tmp_path)
    (tmp_path / ".gitignore").write_text("secrets.yaml\nlocal.toml\n", encoding="utf-8")
    (tmp_path / "secrets.yaml").write_text("api_key: sk-ant-EXAMPLE\n", encoding="utf-8")
    (tmp_path / "local.toml").write_text('password = "hunter2"\n', encoding="utf-8")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    names = {f.name for f in iter_source_files(tmp_path)}

    assert "app.py" in names
    assert "secrets.yaml" not in names
    assert "local.toml" not in names


def test_iter_source_files_skips_gitignored_directory_contents(tmp_path: Path) -> None:
    git.Repo.init(tmp_path)
    (tmp_path / ".gitignore").write_text("build/\n", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "generated.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("y = 2\n", encoding="utf-8")

    names = {f.name for f in iter_source_files(tmp_path)}

    assert "app.py" in names
    assert "generated.py" not in names


def test_iter_source_files_works_outside_a_git_repo(tmp_path: Path) -> None:
    """Indexing a plain directory must keep working — the gitignore filter
    degrades to a no-op rather than failing."""
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    assert [f.name for f in iter_source_files(tmp_path)] == ["app.py"]


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
