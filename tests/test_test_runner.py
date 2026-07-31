"""Tests for core.orchestrator.test_runner."""

from __future__ import annotations

import subprocess
from pathlib import Path

from core.orchestrator import test_runner
from core.orchestrator.test_runner import TestResult, format_test_summary


def _declare_test_command(tmp_path: Path, command: str = "uv run pytest -q") -> None:
    (tmp_path / test_runner.CONFIG_PATH).write_text(f"test_command: {command!r}\n", encoding="utf-8")


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
    result = TestResult(passed=False, output="  error  \n")
    assert format_test_summary(result) == "[FAIL] error"


def test_format_test_summary_skipped() -> None:
    result = TestResult(passed=True, skipped=True, output="No test_command declared")
    assert format_test_summary(result) == "[SKIPPED] No test_command declared"


def test_run_tests_skips_cleanly_when_no_config_declared(tmp_path: Path) -> None:
    result = test_runner.run_tests(tmp_path)

    assert result.skipped is True
    assert result.passed is True
    assert "test_command" in result.output


def test_run_tests_reports_pass(monkeypatch, tmp_path: Path) -> None:
    _declare_test_command(tmp_path)

    def fake_run(cmd, cwd, capture_output, text, timeout):
        assert cmd == ["uv", "run", "pytest", "-q"]
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="3 passed\n", stderr="")

    monkeypatch.setattr(test_runner.subprocess, "run", fake_run)

    result = test_runner.run_tests(tmp_path)

    assert result.passed is True
    assert result.skipped is False
    assert "3 passed" in result.output


def test_run_tests_reports_failure_with_combined_output(monkeypatch, tmp_path: Path) -> None:
    _declare_test_command(tmp_path)

    def fake_run(cmd, cwd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="1 failed\n", stderr="traceback...")

    monkeypatch.setattr(test_runner.subprocess, "run", fake_run)

    result = test_runner.run_tests(tmp_path)

    assert result.passed is False
    assert "1 failed" in result.output
    assert "traceback..." in result.output


def test_run_tests_handles_timeout(monkeypatch, tmp_path: Path) -> None:
    _declare_test_command(tmp_path)

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=120)

    monkeypatch.setattr(test_runner.subprocess, "run", fake_run)

    result = test_runner.run_tests(tmp_path)

    assert result.passed is False
    assert "Timeout" in result.output


def test_run_tests_reads_a_list_form_command(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / test_runner.CONFIG_PATH).write_text(
        "test_command: [npm, test]\ntest_timeout: 30\n", encoding="utf-8"
    )

    captured = {}

    def fake_run(cmd, cwd, capture_output, text, timeout):
        captured["cmd"] = cmd
        captured["timeout"] = timeout
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(test_runner.subprocess, "run", fake_run)

    result = test_runner.run_tests(tmp_path)

    assert result.passed is True
    assert captured["cmd"] == ["npm", "test"]
    assert captured["timeout"] == 30
