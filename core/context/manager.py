"""Context Engineering Layer (v1): vector indexing + git diff + project memory
+ project knowledge graph (AST imports, git co-changes, doc mentions).

Vector search finds chunks that *read* like the request; the graph then
expands that seed set with what's structurally, historically, and
documentarily connected to it (see core/graph/builder.py).

Selecting context is only half the job — it has to reach the model in a form
the provider can use. Providers come in two shapes, and the difference is not
cosmetic: one has disk access and can read whatever it's pointed at, the
other has none and only ever sees what's in its prompt. `render_for()`
resolves which of the two renderings below a given provider gets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import git
import networkx as nx
import yaml

from core.context.chunking import chunk_repo
from core.context.embeddings import embed_query, embed_texts
from core.context.vector_store import VectorStore
from core.errors import ConfigError
from core.graph import builder as graph_builder
from core.memory.loader import load_memory_docs

CONFIG_PATH = Path("config/context.yaml")
VECTOR_STORAGE_PATH = Path("vector/qdrant_db")

POINTERS = "pointers"
FULL = "full"
INJECTION_MODES = (POINTERS, FULL)

VECTOR = "vector"
GRAPH = "graph"


@dataclass
class ContextConfig:
    use_git_diff: bool = True
    use_graph: bool = True
    use_vector_db: bool = True
    use_memory: bool = True
    max_files: int = 20
    injection_mode: str = POINTERS
    """How much of the selected context to put in the prompt of a provider
    that can read the repo itself. `pointers` sends a ranked map and lets it
    fetch what it wants; `full` sends the excerpt text inline. Providers
    without disk access always get `full` — for them there is no choice.

    Which one is cheaper/better is an empirical question, so it's a config
    knob and the choice is recorded per run (see supervisor.run) rather than
    settled by argument."""


@dataclass
class ContextEntry:
    """One selected file, with the reason it was selected and where it ranked.

    Provenance and rank are the selection's actual output, and they used to be
    thrown away: paths from vector search and from the graph were merged into
    one alphabetically-sorted list, which put README.md first because "R" <
    "c". Both orderings mean something — vector order is semantic similarity,
    graph order is PageRank centrality around the seeds — so both are kept.
    """

    path: str
    source: str
    rank: int
    excerpts: list[dict] = field(default_factory=list)
    """The chunks that matched, for a vector hit. Empty for a graph entry:
    the graph relates whole files, it doesn't point at a region."""


@dataclass
class SelectedContext:
    chunks: list[dict] = field(default_factory=list)
    git_diff: str = ""
    memory_docs: dict[str, str] = field(default_factory=dict)
    related_files: list[str] = field(default_factory=list)
    """Files/docs pulled in via the project graph (imports, co-changes, doc
    mentions) around the vector-search hits — not semantically similar text,
    structurally/historically/documentarily connected."""
    injection_mode: str = POINTERS

    def entries(self) -> list[ContextEntry]:
        """The selection as a single ranked list, best match first.

        Vector hits come first — a semantic match on the request itself is a
        stronger signal than being adjacent to one — then the graph expansion
        in PageRank order. A file found by both keeps its (higher) vector
        rank; it isn't listed twice.
        """
        by_path: dict[str, ContextEntry] = {}
        for chunk in self.chunks:
            entry = by_path.get(chunk["path"])
            if entry is None:
                by_path[chunk["path"]] = ContextEntry(
                    path=chunk["path"], source=VECTOR, rank=len(by_path) + 1, excerpts=[chunk]
                )
            else:
                entry.excerpts.append(chunk)

        for path in self.related_files:
            if path not in by_path:
                by_path[path] = ContextEntry(path=path, source=GRAPH, rank=len(by_path) + 1)

        return list(by_path.values())

    def context_paths(self) -> list[str]:
        """The selected paths, most relevant first."""
        return [entry.path for entry in self.entries()]

    def render_for(self, *, reads_files: bool) -> str:
        """The rendering this provider can actually use.

        A provider with no disk access gets the content or it gets nothing —
        `injection_mode` doesn't apply to it.
        """
        if not reads_files:
            return self.render()
        return self.render_pointers() if self.injection_mode == POINTERS else self.render()

    def render(self) -> str:
        """Everything, inline — for a provider that can't read the repo."""
        parts: list[str] = []
        if self.memory_docs:
            parts.append("## Project memory")
            for name, content in self.memory_docs.items():
                parts.append(f"### {name}\n{content}")
        if self.git_diff:
            parts.append(f"## Current git diff\n```diff\n{self.git_diff}\n```")
        if self.chunks:
            parts.append("## Relevant code excerpts")
            for chunk in self.chunks:
                parts.append(
                    f"### {chunk['path']} ({chunk['kind']} {chunk['name']}, "
                    f"lines {chunk['start_line']}-{chunk['end_line']})\n```\n{chunk['text']}\n```"
                )
        if self.related_files:
            parts.append("## Related via project graph")
            parts.append("\n".join(f"- {path}" for path in self.related_files))
        return "\n\n".join(parts)

    def render_pointers(self) -> str:
        """A ranked map — for a provider that reads files itself.

        Sending excerpt text to such a provider duplicates what it can fetch
        at better fidelity (whole file, current on disk, its own line
        numbers). What it cannot reconstruct is *this*: which files matter,
        in what order, and why — so that's what gets sent.

        The git diff is the exception and is always inlined: no role's
        allowed-tools list includes a general Bash (see
        providers.claude_code.adapter.ROLE_ALLOWED_TOOLS), so uncommitted
        state is genuinely unreachable unless we put it in the prompt.
        """
        entries = self.entries()
        parts: list[str] = []

        if self.memory_docs:
            parts.append(
                "## Project memory\n"
                + "\n".join(f"- memory/{name}" for name in self.memory_docs)
            )
        if self.git_diff:
            parts.append(
                "## Current git diff (uncommitted — you cannot obtain this yourself)\n"
                f"```diff\n{self.git_diff}\n```"
            )
        if entries:
            lines = [
                f"## Selected context — {len(entries)} files, most relevant first",
                "Ranked by semantic search over the repo, then by the project dependency",
                "graph (imports, git co-changes, doc mentions). The order is meaningful.",
                "Read what you need; this is a map, not a restriction.",
                "",
            ]
            for entry in entries:
                if entry.source == VECTOR:
                    regions = ", ".join(
                        f"{c['start_line']}-{c['end_line']}" for c in entry.excerpts
                    )
                    why = f"semantic match — lines {regions}"
                else:
                    why = "related via the project graph"
                lines.append(f"{entry.rank:>3}. {entry.path} — {why}")
            parts.append("\n".join(lines))

        return "\n\n".join(parts)


def load_config(repo_root: Path) -> ContextConfig:
    path = repo_root / CONFIG_PATH
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    config = ContextConfig(**{k: v for k, v in data.items() if k in ContextConfig.__dataclass_fields__})
    if config.injection_mode not in INJECTION_MODES:
        raise ConfigError(
            f"Unknown injection_mode {config.injection_mode!r} in {CONFIG_PATH}. "
            f"Valid modes: {', '.join(INJECTION_MODES)}"
        )
    return config


class ContextManager:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.config = load_config(repo_root)
        self._store: VectorStore | None = None
        self._graph: nx.MultiDiGraph | None = None

    def index_repo(self) -> int:
        if self.config.use_graph:
            self._graph = graph_builder.load_or_build(self.repo_root)

        if not self.config.use_vector_db:
            return 0
        chunks = chunk_repo(self.repo_root)
        vectors = embed_texts([chunk.text for chunk in chunks])
        self._store = VectorStore(self.repo_root / VECTOR_STORAGE_PATH)
        self._store.reset()
        self._store.add(chunks, vectors)
        return len(chunks)

    def select_context(self, request: str) -> SelectedContext:
        context = SelectedContext(injection_mode=self.config.injection_mode)

        if self.config.use_vector_db:
            if self._store is None:
                self._store = VectorStore(self.repo_root / VECTOR_STORAGE_PATH)
            query_vector = embed_query(request)
            context.chunks = self._store.search(query_vector, limit=self.config.max_files)

        if self.config.use_git_diff:
            context.git_diff = self._current_git_diff()

        if self.config.use_memory:
            context.memory_docs = load_memory_docs(self.repo_root)

        if self.config.use_graph and self._graph is not None:
            seeds = context.context_paths()
            context.related_files = graph_builder.related_files(self._graph, seeds, limit=self.config.max_files)

        return context

    def _current_git_diff(self) -> str:
        repo = git.Repo(self.repo_root)
        return repo.git.diff("--", ".", ":(exclude)uv.lock")
