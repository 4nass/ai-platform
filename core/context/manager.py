"""Context Engineering Layer (v1) : indexation vectorielle + git diff + mémoire projet.

`use_graph` (config/context.yaml) n'est pas implémenté dans ce prototype : pas de
code graph networkx. On log un avertissement plutôt que de faire semblant.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import git
import yaml

from core.context.chunking import chunk_repo
from core.context.embeddings import embed_query, embed_texts
from core.context.vector_store import VectorStore
from core.memory.loader import load_memory_docs

logger = logging.getLogger(__name__)

CONFIG_PATH = Path("config/context.yaml")
VECTOR_STORAGE_PATH = Path("vector/qdrant_db")


@dataclass
class ContextConfig:
    use_git_diff: bool = True
    use_graph: bool = True
    use_vector_db: bool = True
    use_memory: bool = True
    max_files: int = 20


@dataclass
class SelectedContext:
    chunks: list[dict] = field(default_factory=list)
    git_diff: str = ""
    memory_docs: dict[str, str] = field(default_factory=dict)

    def render(self) -> str:
        parts: list[str] = []
        if self.memory_docs:
            parts.append("## Mémoire projet")
            for name, content in self.memory_docs.items():
                parts.append(f"### {name}\n{content}")
        if self.git_diff:
            parts.append(f"## Git diff en cours\n```diff\n{self.git_diff}\n```")
        if self.chunks:
            parts.append("## Extraits de code pertinents")
            for chunk in self.chunks:
                parts.append(
                    f"### {chunk['path']} ({chunk['kind']} {chunk['name']}, "
                    f"lignes {chunk['start_line']}-{chunk['end_line']})\n```\n{chunk['text']}\n```"
                )
        return "\n\n".join(parts)

    def context_paths(self) -> list[str]:
        """Chemins uniques triés des chunks trouvés — pour les providers CLI
        qui lisent les fichiers eux-mêmes (pas besoin du contenu complet)."""
        return sorted({chunk["path"] for chunk in self.chunks})


def load_config(repo_root: Path) -> ContextConfig:
    path = repo_root / CONFIG_PATH
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return ContextConfig(**{k: v for k, v in data.items() if k in ContextConfig.__dataclass_fields__})


class ContextManager:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.config = load_config(repo_root)
        self._store: VectorStore | None = None
        if self.config.use_graph:
            logger.warning("use_graph=true dans config/context.yaml mais le code graph n'est pas implémenté en v1 : ignoré.")

    def index_repo(self) -> int:
        if not self.config.use_vector_db:
            return 0
        chunks = chunk_repo(self.repo_root)
        vectors = embed_texts([chunk.text for chunk in chunks])
        self._store = VectorStore(self.repo_root / VECTOR_STORAGE_PATH)
        self._store.reset()
        self._store.add(chunks, vectors)
        return len(chunks)

    def select_context(self, request: str) -> SelectedContext:
        context = SelectedContext()

        if self.config.use_vector_db:
            if self._store is None:
                self._store = VectorStore(self.repo_root / VECTOR_STORAGE_PATH)
            query_vector = embed_query(request)
            context.chunks = self._store.search(query_vector, limit=self.config.max_files)

        if self.config.use_git_diff:
            context.git_diff = self._current_git_diff()

        if self.config.use_memory:
            context.memory_docs = load_memory_docs(self.repo_root)

        return context

    def _current_git_diff(self) -> str:
        repo = git.Repo(self.repo_root)
        return repo.git.diff("--", ".", ":(exclude)uv.lock")
