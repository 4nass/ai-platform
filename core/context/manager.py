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

from core import untrusted
from core.context import selection
from core.context.chunking import chunk_repo
from core.context.embeddings import embed_query, embed_texts
from core.context.selection import Decision
from core.context.vector_store import VectorStore
from core.errors import ConfigError
from core.graph import builder as graph_builder
from core.memory.loader import load_memory_docs

CONFIG_PATH = Path("config/context.yaml")
VECTOR_STORAGE_PATH = Path(".ai-platform/vector/qdrant_db")
"""Under the target repo, not the engine install: this indexes the target's
own source, so a second `--repo` target must not share (or collide with) the
first one's index."""

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

    min_similarity: float = 0.20
    min_similarity_ratio: float = 0.5
    min_lift: float = 1.2
    """Relevance floors — see core.context.selection.Thresholds for what each
    one is calibrated against."""

    max_context_chars: int = 20000
    """Ceiling on the rendered context, applied per call because the cost of
    an entry depends on the rendering (see SelectedContext.render_for)."""

    def thresholds(self) -> selection.Thresholds:
        return selection.Thresholds(
            min_similarity=self.min_similarity,
            min_similarity_ratio=self.min_similarity_ratio,
            min_lift=self.min_lift,
            max_files=self.max_files,
        )


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
    score: float = 0.0
    lift: float | None = None
    reason: str = ""
    """Why this file cleared its gate, in words — the answer to "why this
    file?" carried alongside the file itself rather than reconstructed."""


@dataclass(frozen=True)
class Rendered:
    """What one provider actually receives, and what the budget cost it."""

    text: str
    files: int
    dropped: int
    """Entries that cleared every relevance floor but didn't fit the character
    budget. Distinct from a file that failed a gate, and recorded separately —
    the fix for one is a bigger budget, for the other a lower floor."""


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
    decisions: list[Decision] = field(default_factory=list)
    """Every candidate considered, kept or dropped, with the rule that decided
    and why. `chunks` and `related_files` hold only the survivors; this holds
    the reasoning that produced them."""
    max_context_chars: int = 20000

    def entries(self) -> list[ContextEntry]:
        """The selection as a single ranked list, best match first.

        Vector hits come first — a semantic match on the request itself is a
        stronger signal than being adjacent to one — then the graph expansion
        in PageRank order. A file found by both keeps its (higher) vector
        rank; it isn't listed twice.
        """
        scored = {d.path: d for d in self.decisions if d.kept}
        by_path: dict[str, ContextEntry] = {}

        def add(path: str, source: str) -> ContextEntry:
            decision = scored.get(path)
            entry = ContextEntry(
                path=path,
                source=source,
                rank=len(by_path) + 1,
                score=decision.score if decision else 0.0,
                lift=decision.lift if decision else None,
                reason=decision.reason if decision else "",
            )
            by_path[path] = entry
            return entry

        for chunk in self.chunks:
            entry = by_path.get(chunk["path"]) or add(chunk["path"], VECTOR)
            entry.excerpts.append(chunk)

        for path in self.related_files:
            if path not in by_path:
                add(path, GRAPH)

        return list(by_path.values())

    def context_paths(self) -> list[str]:
        """The selected paths, most relevant first."""
        return [entry.path for entry in self.entries()]

    def render_for(self, *, reads_files: bool) -> Rendered:
        """The rendering this provider can actually use, trimmed to budget.

        A provider with no disk access gets the content or it gets nothing —
        `injection_mode` doesn't apply to it.

        The budget is applied here rather than at selection time because what
        an entry costs depends on the rendering, and the rendering depends on
        the provider: the same selection legitimately trims differently for
        two providers in one run.

        It caps the *selected-file portion* and is measured against that
        portion alone. Project memory and the git diff are fixed overhead,
        outside the budget: a provider cannot obtain uncommitted state itself
        (no role's allowed-tools list has a general Bash). Counting them in
        was the first implementation, and measuring it killed it — a 26k diff
        in the working tree consumed the whole budget and cut every one of the
        nine selected files, which is the opposite of selecting well.
        """
        render = self.render_pointers if (reads_files and self.injection_mode == POINTERS) else self.render
        entries = self.entries()
        overhead = len(render([]))

        fitted: list[ContextEntry] = []
        for entry in entries:
            if len(render(fitted + [entry])) - overhead > self.max_context_chars:
                break
            fitted.append(entry)

        return Rendered(text=render(fitted), files=len(fitted), dropped=len(entries) - len(fitted))

    def render(self, entries: list[ContextEntry] | None = None) -> str:
        """Everything, inline — for a provider that can't read the repo.

        This is the rendering that actually embeds repo content in a prompt,
        so it's the one that carries injection risk from files (issue #5):
        a README, a memory doc, or a code excerpt can address the model
        directly. Each is wrapped and its control lines defanged
        (core.untrusted) — see that module on what that does and doesn't buy.
        """
        entries = self.entries() if entries is None else entries
        parts: list[str] = []
        if self.memory_docs:
            parts.append("## Project memory")
            for name, content in self.memory_docs.items():
                parts.append(
                    f"### {name}\n"
                    + untrusted.wrap(content, source=f"memory/{name}", kind="document")
                )
        if self.git_diff:
            parts.append(
                "## Current git diff\n"
                + untrusted.wrap(self.git_diff, source="the working tree", kind="diff")
            )

        excerpted = [e for e in entries if e.excerpts]
        if excerpted:
            parts.append("## Relevant code excerpts")
            for entry in excerpted:
                for chunk in entry.excerpts:
                    parts.append(
                        f"### {chunk['path']} ({chunk['kind']} {chunk['name']}, "
                        f"lines {chunk['start_line']}-{chunk['end_line']})\n"
                        + untrusted.wrap(chunk["text"], source=chunk["path"], kind="excerpt")
                    )

        related = [e.path for e in entries if not e.excerpts]
        if related:
            parts.append("## Related via project graph")
            parts.append("\n".join(f"- {path}" for path in related))
        return "\n\n".join(self._with_provenance_note(parts))

    def render_pointers(self, entries: list[ContextEntry] | None = None) -> str:
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
        entries = self.entries() if entries is None else entries
        parts: list[str] = []

        if self.memory_docs:
            parts.append(
                "## Project memory\n"
                + "\n".join(f"- memory/{name}" for name in self.memory_docs)
            )
        if self.git_diff:
            parts.append(
                "## Current git diff (uncommitted — you cannot obtain this yourself)\n"
                + untrusted.wrap(self.git_diff, source="the working tree", kind="diff")
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

        return "\n\n".join(self._with_provenance_note(parts))

    def _with_provenance_note(self, parts: list[str]) -> list[str]:
        """Appends the data-not-instructions note, unless there's no context
        at all — an empty selection should render as the empty string, not as
        a warning about content that isn't there.

        Keyed on `self.entries()` rather than on `parts` so the note is
        present in `render([])` too, whenever entries exist to be added.
        `render_for` measures fixed overhead as exactly `len(render([]))`, so
        a note that only appeared once the first entry was added would be
        charged to that entry's budget instead — silently costing ~380 chars
        of the selected-file allowance (decisive at a small budget, which is
        how this surfaced).
        """
        if not parts and not self.entries():
            return parts
        return parts + [untrusted.DATA_NOT_INSTRUCTIONS]


def _best_score_per_file(chunks: list[dict]) -> list[tuple[str, float]]:
    """Collapses chunk hits to one score per file, keeping search order.

    A file is as relevant as its strongest matching chunk: a 600-line module
    with one paragraph that answers the request is a hit, and averaging its
    chunks would bury that.
    """
    best: dict[str, float] = {}
    for chunk in chunks:
        path = chunk["path"]
        score = float(chunk.get("score", 0.0))
        best[path] = max(best.get(path, score), score)
    return list(best.items())


def load_config(engine_root: Path) -> ContextConfig:
    path = engine_root / CONFIG_PATH
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    config = ContextConfig(**{k: v for k, v in data.items() if k in ContextConfig.__dataclass_fields__})
    if config.injection_mode not in INJECTION_MODES:
        raise ConfigError(
            f"Unknown injection_mode {config.injection_mode!r} in {CONFIG_PATH}. "
            f"Valid modes: {', '.join(INJECTION_MODES)}"
        )
    return config


class ContextManager:
    def __init__(self, repo_root: Path, *, engine_root: Path | None = None):
        """`repo_root` is the target being indexed and searched. `engine_root`
        is where config/context.yaml's thresholds live — the engine install,
        which defaults to `repo_root` so self-targeting (still the common
        case) and existing callers/tests need no change."""
        self.repo_root = repo_root
        self.engine_root = engine_root if engine_root is not None else repo_root
        self.config = load_config(self.engine_root)
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
        """Retrieves candidates, then decides which of them are worth sending.

        The order matters: the search is gated *before* the graph runs, so the
        graph is seeded by files that matched rather than by whatever came
        back. Both retrievers used to be treated as deciders, which is why a
        request with nothing to find still filled the context.
        """
        thresholds = self.config.thresholds()
        context = SelectedContext(
            injection_mode=self.config.injection_mode,
            max_context_chars=self.config.max_context_chars,
        )

        raw_chunks: list[dict] = []
        if self.config.use_vector_db:
            if self._store is None:
                self._store = VectorStore(self.repo_root / VECTOR_STORAGE_PATH)
            query_vector = embed_query(request)
            raw_chunks = self._store.search(query_vector, limit=self.config.max_files)

        vector_decisions = selection.gate_vector(_best_score_per_file(raw_chunks), thresholds)
        seed_weights = {d.path: d.score for d in vector_decisions if d.kept}

        graph_decisions: list[Decision] = []
        if self.config.use_graph and self._graph is not None and seed_weights:
            related = graph_builder.related_files(
                self._graph, seed_weights, limit=self.config.max_files
            )
            graph_decisions = selection.gate_graph(related, thresholds)

        context.decisions = selection.merge(vector_decisions, graph_decisions, thresholds.max_files)
        kept_paths = {d.path for d in context.decisions if d.kept}
        context.chunks = [c for c in raw_chunks if c["path"] in kept_paths]
        context.related_files = [d.path for d in context.decisions if d.kept and d.source == GRAPH]

        if self.config.use_git_diff:
            context.git_diff = self._current_git_diff()

        if self.config.use_memory:
            context.memory_docs = load_memory_docs(self.repo_root)

        return context

    def _current_git_diff(self) -> str:
        repo = git.Repo(self.repo_root)
        return repo.git.diff("--", ".", ":(exclude)uv.lock")
