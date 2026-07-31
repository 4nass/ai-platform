"""Tests for core.graph.ast_deps."""

from __future__ import annotations

from pathlib import Path

from core.graph.ast_deps import extract_imports


def _write(tmp_path: Path, rel: str, content: str) -> None:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_absolute_import_resolves_to_module_file(tmp_path: Path) -> None:
    _write(tmp_path, "pkg/util.py", "x = 1\n")
    _write(tmp_path, "pkg/main.py", "import pkg.util\n")

    assert extract_imports(tmp_path, tmp_path / "pkg/main.py") == ["pkg/util.py"]


def test_from_import_of_submodule_resolves(tmp_path: Path) -> None:
    _write(tmp_path, "pkg/util.py", "x = 1\n")
    _write(tmp_path, "pkg/main.py", "from pkg import util\n")

    assert extract_imports(tmp_path, tmp_path / "pkg/main.py") == ["pkg/util.py"]


def test_from_import_of_symbol_resolves_to_its_module(tmp_path: Path) -> None:
    _write(tmp_path, "pkg/util.py", "def helper(): pass\n")
    _write(tmp_path, "pkg/main.py", "from pkg.util import helper\n")

    assert extract_imports(tmp_path, tmp_path / "pkg/main.py") == ["pkg/util.py"]


def test_relative_import_single_dot(tmp_path: Path) -> None:
    _write(tmp_path, "pkg/util.py", "x = 1\n")
    _write(tmp_path, "pkg/main.py", "from . import util\n")

    assert extract_imports(tmp_path, tmp_path / "pkg/main.py") == ["pkg/util.py"]


def test_relative_import_double_dot(tmp_path: Path) -> None:
    _write(tmp_path, "pkg/shared.py", "x = 1\n")
    _write(tmp_path, "pkg/sub/main.py", "from .. import shared\n")

    assert extract_imports(tmp_path, tmp_path / "pkg/sub/main.py") == ["pkg/shared.py"]


def test_relative_import_with_submodule_path(tmp_path: Path) -> None:
    _write(tmp_path, "pkg/core/chunking.py", "x = 1\n")
    _write(tmp_path, "pkg/context/manager.py", "from ..core import chunking\n")

    assert extract_imports(tmp_path, tmp_path / "pkg/context/manager.py") == ["pkg/core/chunking.py"]


def test_aliased_import_resolves(tmp_path: Path) -> None:
    _write(tmp_path, "pkg/util.py", "x = 1\n")
    _write(tmp_path, "pkg/main.py", "import pkg.util as u\n")

    assert extract_imports(tmp_path, tmp_path / "pkg/main.py") == ["pkg/util.py"]


def test_external_imports_are_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "pkg/main.py", "import os\nimport sys\nfrom pathlib import Path\n")

    assert extract_imports(tmp_path, tmp_path / "pkg/main.py") == []


def test_deferred_import_inside_a_function_is_found(tmp_path: Path) -> None:
    _write(tmp_path, "pkg/util.py", "x = 1\n")
    _write(tmp_path, "pkg/main.py", "def run():\n    from pkg import util\n    return util\n")

    assert extract_imports(tmp_path, tmp_path / "pkg/main.py") == ["pkg/util.py"]


def test_empty_file_returns_no_imports(tmp_path: Path) -> None:
    _write(tmp_path, "empty.py", "")

    assert extract_imports(tmp_path, tmp_path / "empty.py") == []


def test_self_import_is_excluded(tmp_path: Path) -> None:
    _write(tmp_path, "pkg/main.py", "import pkg.main\n")

    assert extract_imports(tmp_path, tmp_path / "pkg/main.py") == []
