"""Tests for core.orchestrator.decomposer."""

from __future__ import annotations

from core.orchestrator.decomposer import build_description, parse_tasks

KNOWN_IDS = ["architecture", "backend", "frontend", "tests", "security", "documentation"]


def test_build_description_lists_known_ids() -> None:
    description = build_description("add oauth2", KNOWN_IDS)

    assert "add oauth2" in description
    for task_id in KNOWN_IDS:
        assert task_id in description


def test_parse_tasks_extracts_valid_subset() -> None:
    text = "Some reasoning.\nTASKS: backend, tests"

    assert parse_tasks(text, KNOWN_IDS) == ["backend", "tests"]


def test_parse_tasks_filters_out_hallucinated_ids() -> None:
    text = "TASKS: backend, made_up_task, tests"

    assert parse_tasks(text, KNOWN_IDS) == ["backend", "tests"]


def test_parse_tasks_strips_whitespace() -> None:
    text = "TASKS:  backend ,  tests  "

    assert parse_tasks(text, KNOWN_IDS) == ["backend", "tests"]


def test_parse_tasks_returns_none_when_line_missing() -> None:
    assert parse_tasks("I don't know.", KNOWN_IDS) is None


def test_parse_tasks_returns_none_when_nothing_valid_survives() -> None:
    assert parse_tasks("TASKS: made_up_a, made_up_b", KNOWN_IDS) is None
