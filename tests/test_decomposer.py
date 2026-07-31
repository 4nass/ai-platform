"""Tests for core.orchestrator.decomposer."""

from __future__ import annotations

from core.orchestrator.decomposer import build_description, parse_complexity, parse_tasks

KNOWN_IDS = ["architecture", "backend", "frontend", "tests", "security", "documentation"]


def test_build_description_lists_known_ids() -> None:
    description = build_description("add oauth2", KNOWN_IDS)

    assert "COMPLEXITY:" in description
    assert "routine, complex, or critical" in description
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


def test_parse_tasks_ignores_an_inline_example_and_takes_the_real_answer() -> None:
    """prompts/decomposer.md documents the format with an example, and that
    file is in the decomposer's own context — an inline mention must not win
    over the trailing answer, which would silently truncate the workflow."""
    text = (
        'The prompt shows examples like "TASKS: backend, tests".\n'
        "For this request I need the full pipeline:\n"
        "\n"
        "TASKS: architecture, backend, frontend, tests, security, documentation"
    )

    assert parse_tasks(text, KNOWN_IDS) == KNOWN_IDS


def test_parse_tasks_accepts_markdown_emphasis() -> None:
    assert parse_tasks("**TASKS:** backend, tests", KNOWN_IDS) == ["backend", "tests"]


def test_parse_complexity_accepts_each_bounded_value() -> None:
    for value in ("routine", "complex", "critical"):
        assert parse_complexity(f"COMPLEXITY: {value}") == value


def test_parse_complexity_takes_the_last_anchored_value() -> None:
    text = "The format is COMPLEXITY: routine.\nCOMPLEXITY: critical"

    assert parse_complexity(text) == "critical"


def test_parse_complexity_rejects_missing_or_unknown_values() -> None:
    assert parse_complexity("TASKS: backend") is None
    assert parse_complexity("COMPLEXITY: extreme") is None


def test_parse_complexity_accepts_markdown_emphasis() -> None:
    assert parse_complexity("**COMPLEXITY:** complex") == "complex"
