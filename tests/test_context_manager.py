"""Tests for core.context.manager."""

from __future__ import annotations

from pathlib import Path

import git
import pytest

from core.context import manager as manager_module
from core.context.manager import FULL, POINTERS, ContextManager, SelectedContext, load_config
from core.errors import ConfigError


def test_load_config_overrides_and_defaults(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "context.yaml").write_text("use_git_diff: false\nmax_files: 3\n", encoding="utf-8")

    config = load_config(tmp_path)

    assert config.use_git_diff is False
    assert config.max_files == 3
    assert config.use_vector_db is True  # not overridden, keeps its default


def test_selected_context_render_combines_all_sections() -> None:
    context = SelectedContext(
        chunks=[
            {
                "path": "a.py",
                "kind": "function",
                "name": "foo",
                "start_line": 1,
                "end_line": 2,
                "text": "def foo(): pass",
            }
        ],
        git_diff="diff --git a/a.py b/a.py",
        memory_docs={"rules.md": "Always write tests."},
    )

    rendered = context.render()

    assert "## Project memory" in rendered
    assert "rules.md" in rendered
    assert "## Current git diff" in rendered
    assert "## Relevant code excerpts" in rendered
    assert "a.py" in rendered


def test_selected_context_render_empty_context_is_empty_string() -> None:
    assert SelectedContext().render() == ""


def test_selected_context_render_includes_related_files() -> None:
    context = SelectedContext(related_files=["core/orchestrator/scheduler.py"])

    rendered = context.render()

    assert "## Related via project graph" in rendered
    assert "core/orchestrator/scheduler.py" in rendered


# --- render_pointers: what a provider that reads files itself gets ---


def test_render_pointers_ranks_files_and_says_why_each_is_there() -> None:
    context = SelectedContext(
        chunks=[
            {
                "path": "core/telemetry/store.py",
                "kind": "function",
                "name": "run_totals",
                "start_line": 180,
                "end_line": 199,
                "text": "def run_totals(): pass",
            }
        ],
        related_files=["providers/base.py"],
    )

    rendered = context.render_pointers()

    assert "  1. core/telemetry/store.py — semantic match — lines 180-199" in rendered
    assert "  2. providers/base.py — related via the project graph" in rendered


def test_render_pointers_omits_excerpt_text() -> None:
    """The point of pointers mode: a provider that can open the file gets a
    better copy than our excerpt, so sending the text is pure duplication."""
    context = SelectedContext(
        chunks=[
            {
                "path": "a.py",
                "kind": "function",
                "name": "foo",
                "start_line": 1,
                "end_line": 2,
                "text": "SECRET_EXCERPT_BODY",
            }
        ]
    )

    assert "SECRET_EXCERPT_BODY" not in context.render_pointers()
    assert "SECRET_EXCERPT_BODY" in context.render()


def test_render_pointers_still_inlines_the_git_diff() -> None:
    """No role's allowed-tools list includes a general Bash, so uncommitted
    state is unreachable unless it's in the prompt."""
    context = SelectedContext(git_diff="diff --git a/a.py b/a.py")

    rendered = context.render_pointers()

    assert "diff --git a/a.py b/a.py" in rendered


def test_render_pointers_points_at_memory_docs_rather_than_inlining_them() -> None:
    context = SelectedContext(memory_docs={"coding_rules.md": "Always write tests."})

    rendered = context.render_pointers()

    assert "memory/coding_rules.md" in rendered
    assert "Always write tests." not in rendered


def test_render_pointers_on_empty_context_is_empty_string() -> None:
    assert SelectedContext().render_pointers() == ""


# --- render_for: the provider's shape decides ---


def test_render_for_a_provider_without_disk_access_always_sends_content() -> None:
    """It only ever sees its prompt — a list of paths it cannot open is
    useless to it, whatever injection_mode says."""
    context = SelectedContext(
        chunks=[
            {
                "path": "a.py",
                "kind": "function",
                "name": "foo",
                "start_line": 1,
                "end_line": 2,
                "text": "BODY",
            }
        ],
        injection_mode=POINTERS,
    )

    assert "BODY" in context.render_for(reads_files=False)


def test_render_for_a_provider_that_reads_files_honors_the_injection_mode() -> None:
    chunks = [
        {"path": "a.py", "kind": "function", "name": "foo", "start_line": 1, "end_line": 2, "text": "BODY"}
    ]

    pointers = SelectedContext(chunks=chunks, injection_mode=POINTERS).render_for(reads_files=True)
    full = SelectedContext(chunks=chunks, injection_mode=FULL).render_for(reads_files=True)

    assert "BODY" not in pointers
    assert "BODY" in full


# --- injection_mode config ---


def test_load_config_defaults_to_pointers(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "context.yaml").write_text("max_files: 3\n", encoding="utf-8")

    assert load_config(tmp_path).injection_mode == POINTERS


def test_load_config_rejects_an_unknown_injection_mode(tmp_path: Path) -> None:
    """Silently falling back would make a run's recorded injection_mode a lie,
    which is worse than failing at startup."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "context.yaml").write_text("injection_mode: everything\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="Unknown injection_mode"):
        load_config(tmp_path)


def _chunk(path: str, name: str = "foo", start: int = 1, end: int = 2, text: str = "") -> dict:
    return {"path": path, "kind": "function", "name": name, "start_line": start, "end_line": end, "text": text}


def test_selected_context_paths_deduplicates_and_keeps_search_order() -> None:
    """Order is the search result's, not the alphabet's. Sorting here used to
    discard both the vector similarity ranking and the graph's PageRank."""
    context = SelectedContext(
        chunks=[_chunk("z.py"), _chunk("a.py", name="foo"), _chunk("a.py", name="bar", start=9, end=10)]
    )

    assert context.context_paths() == ["z.py", "a.py"]


def test_selected_context_paths_puts_vector_hits_before_graph_files() -> None:
    """A semantic match on the request outranks being adjacent to one."""
    context = SelectedContext(chunks=[_chunk("z.py")], related_files=["a.py"])

    assert context.context_paths() == ["z.py", "a.py"]


def test_entries_carry_provenance_and_a_dense_ranking() -> None:
    context = SelectedContext(chunks=[_chunk("z.py")], related_files=["a.py", "b.py"])

    entries = context.entries()

    assert [(e.rank, e.path, e.source) for e in entries] == [
        (1, "z.py", "vector"),
        (2, "a.py", "graph"),
        (3, "b.py", "graph"),
    ]


def test_a_file_found_by_both_sources_keeps_its_vector_rank_and_is_listed_once() -> None:
    context = SelectedContext(chunks=[_chunk("shared.py")], related_files=["shared.py", "other.py"])

    entries = context.entries()

    assert [e.path for e in entries] == ["shared.py", "other.py"]
    assert entries[0].source == "vector"


def test_entries_group_every_matching_chunk_under_its_file() -> None:
    context = SelectedContext(
        chunks=[_chunk("a.py", name="foo", start=1, end=5), _chunk("a.py", name="bar", start=20, end=30)]
    )

    (entry,) = context.entries()

    assert [(c["start_line"], c["end_line"]) for c in entry.excerpts] == [(1, 5), (20, 30)]


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    repo = git.Repo.init(tmp_path)
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "Test")
        cw.set_value("user", "email", "test@example.com")

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "context.yaml").write_text(
        "use_git_diff: true\nuse_graph: false\nuse_vector_db: true\nuse_memory: true\nmax_files: 5\n",
        encoding="utf-8",
    )
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "architecture.md").write_text("We use a layered architecture.", encoding="utf-8")
    (tmp_path / "auth.py").write_text("def authenticate(user):\n    return True\n", encoding="utf-8")

    repo.index.add(["config/context.yaml", "memory/architecture.md", "auth.py"])
    repo.index.commit("initial commit")
    return tmp_path


def test_context_manager_index_and_select(fake_repo: Path) -> None:
    manager = ContextManager(fake_repo)

    n_chunks = manager.index_repo()
    assert n_chunks > 0

    context = manager.select_context("where is authentication handled?")

    assert context.memory_docs == {"architecture.md": "We use a layered architecture."}
    assert any(c["path"] == "auth.py" for c in context.chunks)


def test_select_context_expands_with_the_graph_when_enabled(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    (fake_repo / "config" / "context.yaml").write_text(
        "use_git_diff: true\nuse_graph: true\nuse_vector_db: true\nuse_memory: true\nmax_files: 5\n",
        encoding="utf-8",
    )
    repo = git.Repo(fake_repo)
    repo.index.add(["config/context.yaml"])
    repo.index.commit("enable graph")

    monkeypatch.setattr(
        manager_module.graph_builder,
        "related_files",
        lambda graph, seeds, limit: ["extra_related.py"],
    )

    manager = ContextManager(fake_repo)
    manager.index_repo()
    context = manager.select_context("where is authentication handled?")

    assert context.related_files == ["extra_related.py"]
    assert "extra_related.py" in context.context_paths()
    assert "## Related via project graph" in context.render()


def test_select_context_skips_graph_expansion_when_disabled(fake_repo: Path) -> None:
    manager = ContextManager(fake_repo)  # fake_repo's config has use_graph: false
    manager.index_repo()

    context = manager.select_context("where is authentication handled?")

    assert context.related_files == []
