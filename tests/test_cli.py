"""Tests for the ai_platform CLI entry point."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

import ai_platform
from core.context.selection import Decision
from core.orchestrator.supervisor import RunReport

runner = CliRunner()


def _report(summary: str) -> RunReport:
    ok = summary == "done"
    return RunReport(
        branch="hermes/test",
        stages=[],
        files_changed=[],
        tests_passed=ok,
        tests_output="",
        review_passed=ok,
        review_summary="",
        summary=summary,
    )


def test_cli_exits_zero_when_summary_is_done(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "core.orchestrator.supervisor.run", lambda repo_root, request, dry_run=False, session_id=None: _report("done")
    )

    result = runner.invoke(ai_platform.app, ["run", "add a thing"])

    assert result.exit_code == 0


def test_cli_exits_nonzero_when_summary_needs_attention(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "core.orchestrator.supervisor.run", lambda repo_root, request, dry_run=False, session_id=None: _report("needs attention")
    )

    result = runner.invoke(ai_platform.app, ["run", "add a thing"])

    assert result.exit_code == 1


def test_cli_dry_run_passes_flag_through_and_ignores_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(repo_root, request, dry_run=False, session_id=None):
        captured["dry_run"] = dry_run
        return _report("needs attention")

    monkeypatch.setattr("core.orchestrator.supervisor.run", fake_run)

    result = runner.invoke(ai_platform.app, ["run", "add a thing", "--dry-run"])

    assert result.exit_code == 0
    assert captured["dry_run"] is True


def test_cli_exits_nonzero_and_prints_clean_error_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_error(repo_root, request, dry_run=False, session_id=None):
        raise RuntimeError("boom")

    monkeypatch.setattr("core.orchestrator.supervisor.run", raise_error)

    result = runner.invoke(ai_platform.app, ["run", "add a thing"])

    assert result.exit_code == 1
    assert "boom" in result.stdout
    assert "Traceback" not in result.stdout


class _FakeContextManager:
    """Stands in for the real one so the CLI test needs no index or graph."""

    def __init__(self, repo_root) -> None:
        self.repo_root = repo_root

    def index_repo(self) -> int:
        return 0

    def select_context(self, request: str):
        from core.context import selection
        from core.context.manager import SelectedContext

        return SelectedContext(
            chunks=[
                {"path": "kept.py", "kind": "function", "name": "foo",
                 "start_line": 1, "end_line": 2, "text": "body"}
            ],
            decisions=[
                Decision("kept.py", "vector", 0.65, None, True, selection.KEPT,
                         "matched the request at 0.650"),
                Decision("noise.py", "vector", 0.05, None, False,
                         selection.BELOW_MIN_SIMILARITY, "similarity 0.050 is below the 0.20 floor"),
            ],
        )


def test_context_command_shows_kept_and_dropped_with_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.context.manager.ContextManager", _FakeContextManager)

    result = runner.invoke(ai_platform.app, ["context", "add oauth2"])

    assert result.exit_code == 0
    assert "kept.py" in result.stdout
    assert "noise.py" in result.stdout
    assert "floor" in result.stdout


def test_context_command_can_hide_the_rejected_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.context.manager.ContextManager", _FakeContextManager)

    result = runner.invoke(ai_platform.app, ["context", "add oauth2", "--no-dropped"])

    assert result.exit_code == 0
    assert "kept.py" in result.stdout
    assert "noise.py" not in result.stdout


def test_context_command_reports_the_cost_of_each_injection_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("core.context.manager.ContextManager", _FakeContextManager)

    result = runner.invoke(ai_platform.app, ["context", "add oauth2"])

    assert "pointers:" in result.stdout
    assert "full:" in result.stdout
