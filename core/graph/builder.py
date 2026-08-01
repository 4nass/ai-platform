"""Assembles the project knowledge graph: AST imports + git co-changes +
doc-to-code mentions. Cached to disk, keyed on the current git HEAD commit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import git
import networkx as nx

from core.context.chunking import iter_source_files
from core.graph.ast_deps import extract_imports
from core.graph.git_deps import co_change_counts
from core.graph.knowledge import mention_edges

CACHE_PATH = Path(".ai-platform/graph.json")
# JSON, not pickle: pickle.load() on this file would be arbitrary code
# execution in the parent process, outside any agent sandbox, on a path
# that's gitignored and therefore invisible to commit_all/contracts/the
# reviewer's diff (see #2) if something ever wrote to it unexpectedly.
# Every value on the graph (node/edge attributes below) is already a plain
# str/int/float, so there's nothing pickle buys here that JSON can't hold.
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


def _graph_to_dict(graph: nx.MultiDiGraph) -> dict:
    """Plain-data projection of the graph — the whole reason this can be
    JSON instead of pickle. Every node/edge attribute set anywhere in this
    module is a str, int, or float (see build_graph, co_change_counts),
    so nothing here needs an object serializer."""
    return {
        "nodes": [{"id": node, **data} for node, data in graph.nodes(data=True)],
        "edges": [{"u": u, "v": v, **data} for u, v, data in graph.edges(data=True)],
    }


def _graph_from_dict(data: dict) -> nx.MultiDiGraph:
    graph = nx.MultiDiGraph()
    for node in data["nodes"]:
        node = dict(node)
        graph.add_node(node.pop("id"), **node)
    for edge in data["edges"]:
        edge = dict(edge)
        u, v = edge.pop("u"), edge.pop("v")
        graph.add_edge(u, v, **edge)
    return graph


def load_or_build(repo_root: Path, storage_root: Path | None = None) -> nx.MultiDiGraph:
    """Rebuilds only when the current HEAD differs from the cached one.

    The cache is keyed on HEAD, but `build_graph` reads the *working tree* —
    so those two only agree when the tree is clean. That used to be
    guaranteed: runs refused to start otherwise. Since the run branch moved
    into its own integration worktree, a dirty target tree is allowed, and a
    graph built from uncommitted state must not be written to a cache the
    next clean run at the same HEAD would then trust. So a dirty tree still
    *reads* the cache (it's the right graph for HEAD) but never *writes* one.

    `storage_root` separates *what is read* from *where the cache lives*, and
    defaults to `repo_root` so single-root callers are unaffected. A run
    builds the graph from its integration worktree — a throwaway checkout of
    `base_sha` — while keeping the cache in the target repo, which outlives
    it. Without the split the cache would be written into a directory deleted
    minutes later and every run would rebuild from scratch; with it, the
    cache is now always built from committed state, which is exactly what its
    HEAD key claims.
    """
    storage_root = repo_root if storage_root is None else storage_root
    repo = git.Repo(repo_root)
    head_sha = repo.head.commit.hexsha
    cache_path = storage_root / CACHE_PATH

    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_bytes())
            if cached.get("head_sha") == head_sha:
                return _graph_from_dict(cached)
        except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError, AttributeError):
            pass  # corrupt or unrecognized cache — fall through and rebuild

    graph = build_graph(repo_root)
    if not _source_tree_is_dirty(repo):
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"head_sha": head_sha, **_graph_to_dict(graph)}), encoding="utf-8"
        )
    return graph


def _source_tree_is_dirty(repo: git.Repo) -> bool:
    """Whether anything the graph is built *from* differs from HEAD.

    Deliberately not `repo.is_dirty(untracked_files=True)`: that counts this
    module's own cache file, so in a target repo that doesn't gitignore
    `.ai-platform/` the first write would make the tree permanently "dirty"
    and no cache would ever be written again. Caught by the malicious-cache
    test, whose fixture has no .gitignore.

    Excluding the whole `.ai-platform/` directory is the right rule, not a
    workaround: it holds engine-generated artifacts (this cache, the vector
    index), and `chunking.iter_source_files` — the only thing feeding
    `build_graph` — already skips gitignored files, so nothing in there can
    change the graph regardless.
    """
    status = repo.git.status("--porcelain", "--untracked-files=all", "--", ".", f":(exclude){CACHE_PATH.parts[0]}")
    return bool(status.strip())


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


@dataclass(frozen=True)
class RelatedFile:
    path: str
    score: float
    """Personalized PageRank mass — how much relevance reached this file from
    the seeds. The ranking signal."""
    lift: float
    """score divided by the file's background PageRank: how much more relevant
    it is to *this* request than it is generically. The gate signal."""


def related_files(
    graph: nx.MultiDiGraph, seed_weights: dict[str, float], limit: int
) -> list[RelatedFile]:
    """Ranks files/docs by relevance to the seeds via personalized PageRank
    over an undirected projection of the graph (see _context_view). Lets
    relevance propagate through multiple hops — decaying with distance via
    `PAGERANK_ALPHA` — rather than only considering direct neighbors.

    Seeds are weighted by how well each matched the request, not treated as
    equals: a file the search matched at 0.69 should push more relevance into
    the graph than one it matched at 0.33.

    Two numbers come back per file because raw PageRank mass answers the wrong
    question on its own. Measured on this repo, the same four highest-degree
    files topped the expansion for nearly every request — including a
    deliberately nonsensical one — because mass follows node degree. `lift`
    divides that out: 1.0 means "no more relevant here than anywhere else",
    which means the same thing for every request, whereas a raw score does
    not. Rank by mass, gate by lift — lift alone would promote barely-connected
    files, whose tiny background makes any attention look like a spike.
    """
    seeds = {path: max(weight, 0.0) for path, weight in seed_weights.items() if path in graph}
    if not seeds:
        return []

    view = _context_view(graph)

    reachable: set[str] = set()
    for seed in seeds:
        reachable |= nx.node_connected_component(view, seed)
    candidates = reachable - set(seeds)
    if not candidates:
        return []

    # nx.pagerank divides by the personalization total, so an all-zero vector
    # (every seed matched at or below 0) would raise. Fall back to treating
    # the seeds as equals, which is what it did before weighting.
    if sum(seeds.values()) <= 0:
        seeds = dict.fromkeys(seeds, 1.0)

    personalization = {node: seeds.get(node, 0.0) for node in view.nodes}
    scores = nx.pagerank(view, alpha=PAGERANK_ALPHA, personalization=personalization, weight="weight")
    background = nx.pagerank(view, alpha=PAGERANK_ALPHA, weight="weight")

    ranked = sorted(candidates, key=lambda node: scores[node], reverse=True)
    return [
        RelatedFile(
            path=node,
            score=scores[node],
            lift=scores[node] / background[node] if background[node] > 0 else 0.0,
        )
        for node in ranked[:limit]
    ]
