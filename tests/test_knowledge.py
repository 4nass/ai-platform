"""Tests for core.graph.knowledge."""

from __future__ import annotations

from core.graph.knowledge import mention_edges


def test_mention_by_basename() -> None:
    docs = {"memory/architecture.md": "We route requests through scheduler.py."}
    files = ["core/orchestrator/scheduler.py", "core/orchestrator/planner.py"]

    assert mention_edges(files, docs) == [("memory/architecture.md", "core/orchestrator/scheduler.py")]


def test_mention_by_dotted_module_path() -> None:
    docs = {"memory/architecture.md": "See core.orchestrator.planner for the task breakdown."}
    files = ["core/orchestrator/planner.py"]

    assert mention_edges(files, docs) == [("memory/architecture.md", "core/orchestrator/planner.py")]


def test_no_mention_produces_no_edge() -> None:
    docs = {"memory/architecture.md": "Nothing relevant here."}
    files = ["core/orchestrator/planner.py"]

    assert mention_edges(files, docs) == []


def test_multiple_docs_and_files_only_pair_actual_mentions() -> None:
    docs = {
        "memory/architecture.md": "scheduler.py routes tasks.",
        "memory/business_rules.md": "No mention of code here.",
    }
    files = ["core/orchestrator/scheduler.py", "core/orchestrator/planner.py"]

    edges = mention_edges(files, docs)

    assert edges == [("memory/architecture.md", "core/orchestrator/scheduler.py")]
