"""Mention-based links between project knowledge docs and the code they discuss.

Deliberately simple: a substring check on the file's basename or dotted
module path, not fuzzy matching. Explainable and dependency-free — and
since memory/*.md starts out empty, these edges are expected to be sparse
until the user actually writes decisions/rules down.
"""

from __future__ import annotations

from pathlib import Path


def _module_path(file_path: str) -> str:
    """"core/orchestrator/scheduler.py" -> "core.orchestrator.scheduler" """
    return file_path.removesuffix(".py").replace("/", ".")


def mention_edges(known_files: list[str], docs: dict[str, str]) -> list[tuple[str, str]]:
    """Returns (doc_path, file_path) pairs for every mention found."""
    edges: list[tuple[str, str]] = []
    for doc_path, text in docs.items():
        for file_path in known_files:
            basename = Path(file_path).name
            module_path = _module_path(file_path)
            if basename in text or module_path in text:
                edges.append((doc_path, file_path))
    return edges
