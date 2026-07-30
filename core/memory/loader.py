"""Loads project memory (memory/*.md)."""

from __future__ import annotations

from pathlib import Path

MEMORY_DIR = Path("memory")


def load_memory_docs(repo_root: Path) -> dict[str, str]:
    docs: dict[str, str] = {}
    memory_dir = repo_root / MEMORY_DIR
    if not memory_dir.exists():
        return docs
    for path in sorted(memory_dir.glob("*.md")):
        content = path.read_text(encoding="utf-8").strip()
        if content:
            docs[path.name] = content
    return docs
