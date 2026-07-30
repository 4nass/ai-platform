"""Tests for the ai_platform CLI entry point."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

import ai_platform
from core.orchestrator.supervisor import RunReport

runner = CliRunner()


def _report(summary: str) -> RunReport:
    ok = summary == "done"
    return RunReport(
        branch="hermes/test",
        provider_success=True,
        files_changed=[],
        tests_passed=ok,
        tests_output="",
        review_passed=ok,
        review_summary="",
        summary=summary,
    )


def test_cli_exits_zero_when_summary_is_done(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.orchestrator.supervisor.run", lambda repo_root, request, agent: _report("done"))

    result = runner.invoke(ai_platform.app, ["run", "add a thing"])

    assert result.exit_code == 0


def test_cli_exits_nonzero_when_summary_needs_attention(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "core.orchestrator.supervisor.run", lambda repo_root, request, agent: _report("needs attention")
    )

    result = runner.invoke(ai_platform.app, ["run", "add a thing"])

    assert result.exit_code == 1


def test_cli_exits_nonzero_and_prints_clean_error_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_error(repo_root, request, agent):
        raise RuntimeError("boom")

    monkeypatch.setattr("core.orchestrator.supervisor.run", raise_error)

    result = runner.invoke(ai_platform.app, ["run", "add a thing"])

    assert result.exit_code == 1
    assert "boom" in result.stdout
    assert "Traceback" not in result.stdout
