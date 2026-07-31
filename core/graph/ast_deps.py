"""Python import extraction, resolved to repo-local files only.

Reuses the tree-sitter-python setup from core.context.chunking. Only
project-local imports (absolute or relative, resolvable to a file that
actually exists in the repo) become graph edges — stdlib/third-party
imports resolve to nothing and are silently skipped: this is a
project-local dependency graph, not a full import resolver.
"""

from __future__ import annotations

from pathlib import Path

import tree_sitter_python as tspython
from tree_sitter import Language, Node, Parser

PY_LANGUAGE = Language(tspython.language())


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8")


def _iter_import_targets(node: Node, source: bytes):
    """Yields (dotted_path, level) pairs. level=0 is absolute; level>=1 is the
    number of leading dots in a relative import."""
    if node.type == "import_statement":
        for child in node.children:
            if child.type == "dotted_name":
                yield _text(child, source), 0
            elif child.type == "aliased_import":
                inner = next((c for c in child.children if c.type == "dotted_name"), None)
                if inner is not None:
                    yield _text(inner, source), 0

    elif node.type == "import_from_statement":
        relative = next((c for c in node.children if c.type == "relative_import"), None)
        if relative is None:
            level = 0
            base_dotted = next((c for c in node.children if c.type == "dotted_name"), None)
        else:
            prefix = next((c for c in relative.children if c.type == "import_prefix"), None)
            level = len(prefix.children) if prefix is not None else 0
            base_dotted = next((c for c in relative.children if c.type == "dotted_name"), None)

        # Names after "import" — for absolute imports these are direct
        # children of the statement; for relative imports base_dotted lives
        # under `relative`, so it's never matched here (nothing to skip).
        imported_names: list[str] = []
        for child in node.children:
            if child is base_dotted:
                continue
            if child.type == "dotted_name":
                imported_names.append(_text(child, source))
            elif child.type == "aliased_import":
                inner = next((c for c in child.children if c.type == "dotted_name"), None)
                if inner is not None:
                    imported_names.append(_text(inner, source))

        if base_dotted is not None:
            base_path = _text(base_dotted, source)
            # "from X import Y" is ambiguous between "Y is an attribute of
            # module X" and "Y is a submodule of package X" — this codebase
            # uses the latter constantly (`from core.orchestrator import
            # git_ops, test_runner`), so try both: the base module itself,
            # and each imported name as a submodule of it.
            yield base_path, level
            for name in imported_names:
                yield f"{base_path}.{name}", level
        else:
            # Bare "from . import x[, y]" — there's no "from" target, so each
            # imported name is itself a sibling/cousin module to resolve.
            for name in imported_names:
                yield name, level


def _resolve(repo_root: Path, importing_file: Path, dotted_path: str, level: int) -> str | None:
    if level > 0:
        base_dir = importing_file.parent
        for _ in range(level - 1):
            base_dir = base_dir.parent
    else:
        base_dir = repo_root

    candidate = base_dir.joinpath(*dotted_path.split(".")) if dotted_path else base_dir

    module_file = candidate.with_suffix(".py")
    if module_file.is_file():
        return module_file.relative_to(repo_root).as_posix()

    package_init = candidate / "__init__.py"
    if package_init.is_file():
        return package_init.relative_to(repo_root).as_posix()

    return None


def _walk(node: Node):
    yield node
    for child in node.children:
        yield from _walk(child)


def extract_imports(repo_root: Path, path: Path) -> list[str]:
    """Finds imports anywhere in the file, including deferred ones nested
    inside function bodies (common in this codebase, e.g. `src/ai_platform`
    importing `core.orchestrator` lazily inside the CLI command)."""
    source = path.read_bytes()
    if not source.strip():
        return []

    parser = Parser(PY_LANGUAGE)
    tree = parser.parse(source)
    own_path = path.relative_to(repo_root).as_posix()

    targets: list[str] = []
    for node in _walk(tree.root_node):
        if node.type not in ("import_statement", "import_from_statement"):
            continue
        for dotted_path, level in _iter_import_targets(node, source):
            resolved = _resolve(repo_root, path, dotted_path, level)
            if resolved is not None and resolved != own_path and resolved not in targets:
                targets.append(resolved)
    return targets
