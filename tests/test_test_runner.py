"""Tests pour les utilitaires de core.orchestrator.test_runner."""

from __future__ import annotations

from core.orchestrator.test_runner import TestResult, format_test_summary


def test_format_test_summary_passed_with_output() -> None:
    result = TestResult(passed=True, output="3 passed in 0.01s")
    assert format_test_summary(result) == "[PASS] 3 passed in 0.01s"


def test_format_test_summary_failed_with_output() -> None:
    result = TestResult(passed=False, output="1 failed, 2 passed")
    assert format_test_summary(result) == "[FAIL] 1 failed, 2 passed"


def test_format_test_summary_without_output() -> None:
    result = TestResult(passed=True, output="")
    assert format_test_summary(result) == "[PASS]"


def test_format_test_summary_strips_whitespace() -> None:
    result = TestResult(passed=False, output="  erreur  \n")
    assert format_test_summary(result) == "[FAIL] erreur"
