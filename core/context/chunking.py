"""Splits the repo into indexable chunks: per function/class for Python
(via tree-sitter), per section for Markdown, whole file otherwise."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Language, Node, Parser
import tree_sitter_python as tspython

PY_LANGUAGE = Language(tspython.language())

IGNORED_DIRS = {".git", ".venv", "vector", "__pycache__", "node_modules", ".pytest_cache"}
IGNORED_FILES = {"uv.lock"}
INDEXABLE_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".toml", ".txt"}

_TOP_LEVEL_NODE_TYPES = {"function_definition", "class_definition"}


@dataclass
class Chunk:
    path: str
    kind: str
    name: str
    start_line: int
    end_line: int
    text: str


def iter_source_files(repo_root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(repo_root.rglob("*")):
        if not path.is_file():
            continue
        if path.name in IGNORED_FILES:
            continue
        if any(part in IGNORED_DIRS for part in path.relative_to(repo_root).parts):
            continue
        if path.suffix not in INDEXABLE_SUFFIXES:
            continue
        files.append(path)
    return files


def chunk_file(repo_root: Path, path: Path) -> list[Chunk]:
    rel_path = str(path.relative_to(repo_root))
    text = path.read_text(encoding="utf-8", errors="ignore")
    if not text.strip():
        return []
    if path.suffix == ".py":
        return _chunk_python(rel_path, text)
    if path.suffix == ".md":
        return _chunk_markdown(rel_path, text)
    return [Chunk(path=rel_path, kind="file", name=rel_path, start_line=1, end_line=text.count("\n") + 1, text=text)]


def _chunk_python(rel_path: str, text: str) -> list[Chunk]:
    parser = Parser(PY_LANGUAGE)
    tree = parser.parse(text.encode("utf-8"))
    lines = text.splitlines()

    def node_text(node: Node) -> str:
        return "\n".join(lines[node.start_point[0] : node.end_point[0] + 1])

    def node_name(node: Node) -> str:
        name_node = node.child_by_field_name("name")
        return name_node.text.decode("utf-8") if name_node is not None else "<anonymous>"

    chunks: list[Chunk] = []
    for node in tree.root_node.children:
        if node.type in _TOP_LEVEL_NODE_TYPES:
            chunks.append(
                Chunk(
                    path=rel_path,
                    kind="function" if node.type == "function_definition" else "class",
                    name=node_name(node),
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    text=node_text(node),
                )
            )

    if not chunks:
        chunks.append(Chunk(path=rel_path, kind="file", name=rel_path, start_line=1, end_line=len(lines), text=text))
    return chunks


_MD_HEADER_RE = re.compile(r"^#{1,2}\s+.*$", re.MULTILINE)


def _chunk_markdown(rel_path: str, text: str) -> list[Chunk]:
    headers = list(_MD_HEADER_RE.finditer(text))
    if len(headers) < 2:
        return [Chunk(path=rel_path, kind="file", name=rel_path, start_line=1, end_line=text.count("\n") + 1, text=text)]

    chunks: list[Chunk] = []
    for i, match in enumerate(headers):
        start = match.start()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        section_text = text[start:end].strip("\n")
        if not section_text:
            continue
        start_line = text.count("\n", 0, start) + 1
        end_line = start_line + section_text.count("\n")
        chunks.append(
            Chunk(
                path=rel_path,
                kind="section",
                name=match.group().lstrip("# ").strip(),
                start_line=start_line,
                end_line=end_line,
                text=section_text,
            )
        )
    return chunks


def chunk_repo(repo_root: Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in iter_source_files(repo_root):
        chunks.extend(chunk_file(repo_root, path))
    return chunks
