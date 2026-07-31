"""Assembles the project knowledge graph: AST imports + git co-changes +
doc-to-code mentions. Cached to disk, keyed on the current git HEAD commit.
"""

from __future__ import annotations

import pickle
from pathlib import Path

import git
import networkx as nx

from core.context.chunking import iter_source_files
from core.graph.ast_deps import extract_imports
from core.graph.git_deps import co_change_counts
from core.graph.knowledge import mention_edges

CACHE_PATH = Path("vector/graph.pkl")
DOC_FILES = [
    Path("README.md"),
    Path("memory/architecture.md"),
    Path("memory/business_rules.md"),
    Path("memory/coding_rules.md"),
    Path("memory/roadmap.md"),
]
DOC_DIRS = [Path("memory/adr")]
MIN_CO_CHANGE_COUNT = 2

RELATION_TYPE_WEIGHT = {
    "imports": 1.0,  # structural, deterministic fact
    "references": 0.6,  # a doc explicitly names the file
    "co_changes_with": 0.4,  # statistical signal, noisier — see CoChange.strength
}
PAGERANK_ALPHA = 0.85
"""Damping factor for the relevance random walk — the classic PageRank
default. Not yet tuned against real usage of this repo; revisit once there's
feedback from real `ai-platform run` requests."""


def _collect_docs(repo_root: Path) -> dict[str, str]:
    candidates = list(DOC_FILES)
    for doc_dir in DOC_DIRS:
        full_dir = repo_root / doc_dir
        if full_dir.is_dir():
            candidates += [p.relative_to(repo_root) for p in sorted(full_dir.glob("*.md"))]

    docs: dict[str, str] = {}
    for rel in candidates:
        path = repo_root / rel
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8").strip()
        if content:
            docs[rel.as_posix()] = content
    return docs


def build_graph(repo_root: Path) -> nx.MultiDiGraph:
    graph = nx.MultiDiGraph()

    files = [p.relative_to(repo_root).as_posix() for p in iter_source_files(repo_root)]
    for path in files:
        graph.add_node(path, type="file")

    for path in files:
        if not path.endswith(".py"):
            continue
        for target in extract_imports(repo_root, repo_root / path):
            graph.add_edge(path, target, type="imports")

    for (a, b), co_change in co_change_counts(repo_root).items():
        if co_change.count < MIN_CO_CHANGE_COUNT or a not in graph or b not in graph:
            continue
        graph.add_edge(a, b, type="co_changes_with", count=co_change.count, strength=co_change.strength)
        graph.add_edge(b, a, type="co_changes_with", count=co_change.count, strength=co_change.strength)

    docs = _collect_docs(repo_root)
    for doc_path in docs:
        graph.add_node(doc_path, type="doc")
    for doc_path, file_path in mention_edges(files, docs):
        graph.add_edge(doc_path, file_path, type="references")

    return graph


def load_or_build(repo_root: Path) -> nx.MultiDiGraph:
    """Rebuilds only when the current HEAD differs from the cached one.

    Safe to key on HEAD alone (no working-tree hash needed): callers only
    reach this after `git_ops.ensure_clean_worktree()`, so the tree is
    guaranteed clean whenever this runs.
    """
    repo = git.Repo(repo_root)
    head_sha = repo.head.commit.hexsha
    cache_path = repo_root / CACHE_PATH

    if cache_path.is_file():
        try:
            with cache_path.open("rb") as f:
                cached = pickle.load(f)
            if cached.get("head_sha") == head_sha:
                return cached["graph"]
        except (pickle.PickleError, EOFError, KeyError, AttributeError):
            pass  # corrupt cache — fall through and rebuild

    graph = build_graph(repo_root)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("wb") as f:
        pickle.dump({"head_sha": head_sha, "graph": graph}, f)
    return graph


def _relation_strength(data: dict) -> float:
    """How strongly this specific edge instance backs its relation.
    imports/references are binary, fully-trusted facts (1.0); co-changes are
    a statistical signal whose magnitude is the commit-size-diluted
    `strength` computed in git_deps.co_change_counts."""
    if data.get("type") == "co_changes_with":
        return data.get("strength", 0.0)
    return 1.0


def _edge_weight(data: dict) -> float:
    return _relation_strength(data) * RELATION_TYPE_WEIGHT.get(data.get("type"), 0.0)


def _context_view(graph: nx.MultiDiGraph) -> nx.Graph:
    """Undirected projection used only for relevance propagation.

    The directed MultiDiGraph stays the source of truth (kept for future
    direction-sensitive uses, e.g. impact analysis); this view exists only
    to let relevance flow both ways along every relation for context
    selection.

    co_changes_with is stored twice in the source graph (A->B and B->A, same
    values — added that way purely to make it traversable in both
    directions there) and is deduplicated here by unordered pair so it
    isn't double-counted. imports/references are not deduped: A->B and B->A
    can be genuinely independent facts (e.g. a circular import), and both
    should reinforce the combined weight.
    """
    view = nx.Graph()
    view.add_nodes_from(graph.nodes)

    seen_co_change_pairs: set[frozenset[str]] = set()
    for u, v, data in graph.edges(data=True):
        if data.get("type") == "co_changes_with":
            pair = frozenset((u, v))
            if pair in seen_co_change_pairs:
                continue
            seen_co_change_pairs.add(pair)

        weight = _edge_weight(data)
        if view.has_edge(u, v):
            view[u][v]["weight"] += weight
        else:
            view.add_edge(u, v, weight=weight)

    return view


def related_files(graph: nx.MultiDiGraph, seed_files: list[str], limit: int) -> list[str]:
    """Ranks files/docs by relevance to the seeds via personalized PageRank
    over an undirected projection of the graph (see _context_view). Lets
    relevance propagate through multiple hops — decaying with distance via
    `PAGERANK_ALPHA` — rather than only considering direct neighbors."""
    seeds = [s for s in dict.fromkeys(seed_files) if s in graph]
    if not seeds:
        return []

    view = _context_view(graph)

    reachable: set[str] = set()
    for seed in seeds:
        reachable |= nx.node_connected_component(view, seed)
    candidates = reachable - set(seeds)
    if not candidates:
        return []

    personalization = {node: (1.0 if node in seeds else 0.0) for node in view.nodes}
    scores = nx.pagerank(view, alpha=PAGERANK_ALPHA, personalization=personalization, weight="weight")

    ranked = sorted(candidates, key=lambda node: scores[node], reverse=True)
    return ranked[:limit]
