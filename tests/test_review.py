"""Tests for core.orchestrator.review."""

from __future__ import annotations

from core.orchestrator.review import build_description, parse_verdict


def test_parse_verdict_pass() -> None:
    assert parse_verdict("Looks good.\nVERDICT: PASS") is True


def test_parse_verdict_fail() -> None:
    assert parse_verdict("Found a bug.\nVERDICT: FAIL") is False


def test_parse_verdict_case_insensitive() -> None:
    assert parse_verdict("verdict: pass") is True


def test_parse_verdict_missing_returns_none() -> None:
    assert parse_verdict("The agent forgot to conclude.") is None


def test_build_description_empty_diff_asks_for_pass() -> None:
    description = build_description("Add a helper", "")

    assert "no file changes" in description
    assert "VERDICT: PASS" in description


def test_build_description_includes_request_and_diff() -> None:
    diff = "+def foo():\n+    return 1\n"

    description = build_description("Add foo()", diff)

    assert "Add foo()" in description
    assert diff in description
    assert "VERDICT" in description
