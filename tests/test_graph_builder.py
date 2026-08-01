"""Tests for core.graph.builder."""

from __future__ import annotations

from pathlib import Path

import git
import pytest

from core.graph import builder


def _paths(related: list[builder.RelatedFile]) -> list[str]:
    return [r.path for r in related]


@pytest.fixture
def repo(tmp_path: Path) -> git.Repo:
    r = git.Repo.init(tmp_path)
    with r.config_writer() as cw:
        cw.set_value("user", "name", "Test")
        cw.set_value("user", "email", "test@example.com")
    return r


def _write(tmp_path: Path, rel: str, content: str) -> None:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _commit(repo: git.Repo, tmp_path: Path, files: dict[str, str], message: str) -> None:
    for rel, content in files.items():
        _write(tmp_path, rel, content)
    repo.index.add(list(files))
    repo.index.commit(message)


def test_build_graph_adds_file_and_doc_nodes(repo: git.Repo, tmp_path: Path) -> None:
    _commit(
        repo,
        tmp_path,
        {
            "pkg/util.py": "x = 1\n",
            "pkg/main.py": "from pkg import util\n",
            "memory/architecture.md": "main.py depends on util.py.",
        },
        "initial",
    )

    graph = builder.build_graph(tmp_path)

    assert graph.nodes["pkg/main.py"]["type"] == "file"
    assert graph.nodes["memory/architecture.md"]["type"] == "doc"


def test_build_graph_adds_import_edges(repo: git.Repo, tmp_path: Path) -> None:
    _commit(repo, tmp_path, {"pkg/util.py": "x = 1\n", "pkg/main.py": "from pkg import util\n"}, "initial")

    graph = builder.build_graph(tmp_path)

    imports = [t for _, t, d in graph.out_edges("pkg/main.py", data=True) if d.get("type") == "imports"]
    assert imports == ["pkg/util.py"]


def test_build_graph_adds_reference_edges_from_docs(repo: git.Repo, tmp_path: Path) -> None:
    _commit(
        repo,
        tmp_path,
        {"pkg/util.py": "x = 1\n", "memory/architecture.md": "util.py holds shared helpers."},
        "initial",
    )

    graph = builder.build_graph(tmp_path)

    refs = [t for _, t, d in graph.out_edges("memory/architecture.md", data=True) if d.get("type") == "references"]
    assert refs == ["pkg/util.py"]


def test_build_graph_skips_co_change_below_threshold(repo: git.Repo, tmp_path: Path) -> None:
    _commit(repo, tmp_path, {"a.py": "1", "b.py": "1"}, "only once together")

    graph = builder.build_graph(tmp_path)

    co_change = [d for _, _, d in graph.out_edges("a.py", data=True) if d.get("type") == "co_changes_with"]
    assert co_change == []


def test_build_graph_includes_co_change_at_threshold(repo: git.Repo, tmp_path: Path) -> None:
    _commit(repo, tmp_path, {"a.py": "1", "b.py": "1"}, "first")
    _commit(repo, tmp_path, {"a.py": "2", "b.py": "2"}, "second")

    graph = builder.build_graph(tmp_path)

    co_change = {t for _, t, d in graph.out_edges("a.py", data=True) if d.get("type") == "co_changes_with"}
    assert "b.py" in co_change


def test_load_or_build_reuses_cache_ignoring_uncommitted_changes(repo: git.Repo, tmp_path: Path) -> None:
    _commit(repo, tmp_path, {"a.py": "1\n"}, "first")
    builder.load_or_build(tmp_path)

    cache_path = tmp_path / builder.CACHE_PATH
    assert cache_path.is_file()

    # a new file appears on disk without a commit — cache should still win
    _write(tmp_path, "new_untracked.py", "1\n")
    graph2 = builder.load_or_build(tmp_path)

    assert "new_untracked.py" not in graph2.nodes


def test_load_or_build_can_cache_outside_the_tree_it_reads(repo: git.Repo, tmp_path: Path) -> None:
    """A run builds the graph from its integration worktree — deleted when
    the run ends — so the cache has to live somewhere that outlives it, or
    every run rebuilds from scratch."""
    storage = tmp_path.parent / f"{tmp_path.name}-storage"
    storage.mkdir()
    _commit(repo, tmp_path, {"a.py": "1\n"}, "first")

    builder.load_or_build(tmp_path, storage_root=storage)

    assert (storage / builder.CACHE_PATH).is_file()
    assert not (tmp_path / builder.CACHE_PATH).exists()

    # and it is read back from there on the next call at the same HEAD
    _write(tmp_path, "new_untracked.py", "1\n")
    assert "new_untracked.py" not in builder.load_or_build(tmp_path, storage_root=storage).nodes


def test_load_or_build_invalidates_on_new_commit(repo: git.Repo, tmp_path: Path) -> None:
    _commit(repo, tmp_path, {"a.py": "1\n"}, "first")
    builder.load_or_build(tmp_path)

    _commit(repo, tmp_path, {"b.py": "1\n"}, "second")
    graph2 = builder.load_or_build(tmp_path)

    assert "b.py" in graph2.nodes


def test_load_or_build_rebuilds_on_corrupt_cache(repo: git.Repo, tmp_path: Path) -> None:
    _commit(repo, tmp_path, {"a.py": "1\n"}, "first")

    cache_path = tmp_path / builder.CACHE_PATH
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(b"not valid json")

    graph = builder.load_or_build(tmp_path)

    assert "a.py" in graph.nodes


def test_load_or_build_rebuilds_rather_than_deserializing_a_malicious_cache(
    repo: git.Repo, tmp_path: Path
) -> None:
    """The cache used to be pickle, and pickle.load() on attacker-controlled
    bytes is arbitrary code execution in the parent process (issue #3). JSON
    can't do that — but json.loads() must still be reached (not crash on a
    non-UTF-8 read) for the "corrupt cache rebuilds" guarantee to hold against
    exactly this input, not just against ASCII garbage."""
    import pickle

    _commit(repo, tmp_path, {"a.py": "1\n"}, "first")

    cache_path = tmp_path / builder.CACHE_PATH
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    class _ExecOnUnpickle:
        def __reduce__(self):
            return (list, ())  # if this ever runs, deserialization executed arbitrary code

    cache_path.write_bytes(pickle.dumps(_ExecOnUnpickle()))

    graph = builder.load_or_build(tmp_path)

    assert "a.py" in graph.nodes
    # the cache is now a real, valid JSON file -- confirms load_or_build
    # rebuilt and rewrote it rather than choking on the binary content
    assert cache_path.read_text(encoding="utf-8").startswith("{")


def test_related_files_ranks_imports_above_co_changes(repo: git.Repo, tmp_path: Path) -> None:
    _commit(
        repo,
        tmp_path,
        {"seed.py": "from pkg import util\n", "pkg/util.py": "x = 1\n", "other.py": "y = 1\n"},
        "first",
    )
    # seed.py keeps its import (content still present on disk when build_graph
    # reads it) while also co-changing with other.py in this second commit.
    _commit(repo, tmp_path, {"seed.py": "from pkg import util\nz = 2\n", "other.py": "y = 2\n"}, "co-change")

    graph = builder.build_graph(tmp_path)
    related = _paths(builder.related_files(graph, {"seed.py": 1.0}, limit=10))

    assert "pkg/util.py" in related
    assert "other.py" in related
    assert related.index("pkg/util.py") < related.index("other.py")


def test_related_files_discovers_two_hop_dependency(repo: git.Repo, tmp_path: Path) -> None:
    _commit(
        repo,
        tmp_path,
        {"seed.py": "import a\n", "a.py": "import b\n", "b.py": "x = 1\n"},
        "chain",
    )

    graph = builder.build_graph(tmp_path)
    related = _paths(builder.related_files(graph, {"seed.py": 1.0}, limit=10))

    assert "b.py" in related


def test_related_files_ranks_closer_hops_higher(repo: git.Repo, tmp_path: Path) -> None:
    _commit(
        repo,
        tmp_path,
        {"seed.py": "import a\n", "a.py": "import b\n", "b.py": "x = 1\n"},
        "chain",
    )

    graph = builder.build_graph(tmp_path)
    related = _paths(builder.related_files(graph, {"seed.py": 1.0}, limit=10))

    assert related.index("a.py") < related.index("b.py")


def test_related_files_leaves_source_graph_unmodified(repo: git.Repo, tmp_path: Path) -> None:
    _commit(repo, tmp_path, {"seed.py": "import a\n", "a.py": "x = 1\n"}, "first")

    graph = builder.build_graph(tmp_path)
    edges_before = list(graph.edges(data=True))

    builder.related_files(graph, {"seed.py": 1.0}, limit=10)

    assert graph.is_directed()
    assert list(graph.edges(data=True)) == edges_before


def test_related_files_returns_empty_for_isolated_seed(repo: git.Repo, tmp_path: Path) -> None:
    _commit(repo, tmp_path, {"seed.py": "x = 1\n", "unrelated.py": "y = 1\n"}, "first")

    graph = builder.build_graph(tmp_path)
    related = builder.related_files(graph, {"seed.py": 1.0}, limit=10)

    assert related == []


def test_context_view_deduplicates_mirrored_co_change_edges(repo: git.Repo, tmp_path: Path) -> None:
    _commit(repo, tmp_path, {"a.py": "1", "b.py": "1"}, "first")
    _commit(repo, tmp_path, {"a.py": "2", "b.py": "2"}, "second")

    graph = builder.build_graph(tmp_path)
    view = builder._context_view(graph)

    single_edge_data = [d for u, v, d in graph.edges(data=True) if {u, v} == {"a.py", "b.py"}][0]
    expected_weight = builder._edge_weight(single_edge_data)
    assert view["a.py"]["b.py"]["weight"] == pytest.approx(expected_weight)


def test_related_files_excludes_seeds_and_respects_limit(repo: git.Repo, tmp_path: Path) -> None:
    _commit(
        repo,
        tmp_path,
        {
            "seed.py": "from pkg import a, b, c\n",
            "pkg/a.py": "1",
            "pkg/b.py": "1",
            "pkg/c.py": "1",
        },
        "first",
    )

    graph = builder.build_graph(tmp_path)
    related = _paths(builder.related_files(graph, {"seed.py": 1.0}, limit=2))

    assert "seed.py" not in related
    assert len(related) == 2


def test_related_files_includes_referencing_docs(repo: git.Repo, tmp_path: Path) -> None:
    _commit(
        repo,
        tmp_path,
        {"seed.py": "x = 1\n", "memory/architecture.md": "seed.py is the entry point."},
        "first",
    )

    graph = builder.build_graph(tmp_path)
    related = _paths(builder.related_files(graph, {"seed.py": 1.0}, limit=10))

    assert "memory/architecture.md" in related


def test_lift_discounts_a_hub_everything_points_at(repo: git.Repo, tmp_path: Path) -> None:
    """The regression this metric exists for: on the real repo the same four
    highest-degree files topped the expansion for nearly every request,
    including a nonsense one, because pagerank mass follows node degree."""
    _commit(
        repo,
        tmp_path,
        {
            "seed.py": "import target\n",
            "target.py": "import hub\n",
            "hub.py": "x = 1\n",
            "a.py": "import hub\n",
            "b.py": "import hub\n",
            "c.py": "import hub\n",
            "d.py": "import hub\n",
        },
        "one hub, many dependants",
    )

    graph = builder.build_graph(tmp_path)
    related = {r.path: r for r in builder.related_files(graph, {"seed.py": 1.0}, limit=10)}

    assert related["target.py"].lift > related["hub.py"].lift


def test_seed_weights_shift_relevance_toward_the_stronger_match(
    repo: git.Repo, tmp_path: Path
) -> None:
    """A file the search matched at 0.69 should push more relevance into the
    graph than one it matched at 0.33 — seeds are evidence, not equals."""
    _commit(
        repo,
        tmp_path,
        {
            "left.py": "import left_dep\n",
            "left_dep.py": "x = 1\n",
            "right.py": "import right_dep\n",
            "right_dep.py": "y = 1\n",
        },
        "two independent branches",
    )
    graph = builder.build_graph(tmp_path)

    def score_of(weights: dict[str, float], path: str) -> float:
        related = {r.path: r for r in builder.related_files(graph, weights, limit=10)}
        return related[path].score

    left_favoured = {"left.py": 0.9, "right.py": 0.1}
    right_favoured = {"left.py": 0.1, "right.py": 0.9}

    assert score_of(left_favoured, "left_dep.py") > score_of(left_favoured, "right_dep.py")
    assert score_of(right_favoured, "right_dep.py") > score_of(right_favoured, "left_dep.py")


def test_all_zero_seed_weights_fall_back_to_equal_seeds(repo: git.Repo, tmp_path: Path) -> None:
    """nx.pagerank divides by the personalization total, so an all-zero vector
    would raise rather than degrade."""
    _commit(repo, tmp_path, {"seed.py": "import a\n", "a.py": "x = 1\n"}, "first")

    graph = builder.build_graph(tmp_path)
    related = builder.related_files(graph, {"seed.py": 0.0}, limit=10)

    assert [r.path for r in related] == ["a.py"]


def test_related_files_ignores_seeds_absent_from_the_graph(repo: git.Repo, tmp_path: Path) -> None:
    _commit(repo, tmp_path, {"seed.py": "import a\n", "a.py": "x = 1\n"}, "first")

    graph = builder.build_graph(tmp_path)
    related = builder.related_files(graph, {"seed.py": 1.0, "deleted.py": 1.0}, limit=10)

    assert [r.path for r in related] == ["a.py"]
