"""Tests for core.orchestrator.scheduler."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.errors import ConfigError
from core.orchestrator import scheduler
from core.orchestrator.planner import Task
from core.orchestrator.scheduler import StageResult
from providers.base import AgentTask, ProviderResult

AGENTS_YAML = """backend:
  provider: claude_code
no_provider: {}
"""


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "agents.yaml").write_text(AGENTS_YAML, encoding="utf-8")
    return tmp_path


def test_resolve_provider_known_agent(repo_root: Path) -> None:
    assert scheduler.resolve_provider(repo_root, "backend") == "claude_code"


def test_resolve_provider_unknown_agent_raises(repo_root: Path) -> None:
    with pytest.raises(ConfigError, match="Unknown agent role"):
        scheduler.resolve_provider(repo_root, "does_not_exist")


def test_resolve_provider_missing_provider_key_raises(repo_root: Path) -> None:
    with pytest.raises(ConfigError, match="no 'provider' set"):
        scheduler.resolve_provider(repo_root, "no_provider")


def test_resolve_provider_unknown_provider_name_raises(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "agents.yaml").write_text(
        "backend:\n  provider: not_a_real_provider\n", encoding="utf-8"
    )

    with pytest.raises(ConfigError, match="Unknown provider"):
        scheduler.resolve_provider(tmp_path, "backend")


def _fake_provider(captured: dict):
    def fake_run(task: AgentTask) -> ProviderResult:
        captured["task"] = task
        return ProviderResult(success=True, summary=task.description)

    return type("FakeProvider", (), {"run": staticmethod(fake_run)})


def test_run_task_dispatches_to_the_resolved_provider(monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
    captured: dict = {}
    monkeypatch.setitem(scheduler.PROVIDERS, "claude_code", _fake_provider(captured))

    result = scheduler.run_task(repo_root, "backend", "do the thing", context_paths=["a.py"])

    assert result.success is True
    assert captured["task"].description == "do the thing"
    assert captured["task"].context_paths == ["a.py"]


def test_build_stage_description_with_no_upstream_returns_the_request() -> None:
    assert scheduler.build_stage_description("add oauth2", []) == "add oauth2"


def test_build_stage_description_ignores_non_done_stages() -> None:
    architecture = Task(id="architecture", agent="architect", depends_on=[])
    upstream = [StageResult(task=architecture, status="skipped")]

    assert scheduler.build_stage_description("add oauth2", upstream) == "add oauth2"


def test_build_stage_description_includes_summary_and_files_of_done_stages() -> None:
    architecture = Task(id="architecture", agent="architect", depends_on=[])
    upstream = [
        StageResult(
            task=architecture,
            status="done",
            result=ProviderResult(success=True, summary="Wrote the ADR."),
            files_changed=["memory/adr/ADR-001-oauth.md"],
        )
    ]

    description = scheduler.build_stage_description("add oauth2", upstream)

    assert "add oauth2" in description
    assert "architecture (architect): Wrote the ADR." in description
    assert "memory/adr/ADR-001-oauth.md" in description


def test_build_stage_description_reports_no_files_changed_for_report_only_stages() -> None:
    security = Task(id="security", agent="security", depends_on=[])
    upstream = [
        StageResult(
            task=security,
            status="done",
            result=ProviderResult(success=True, summary="No issues found."),
            files_changed=[],
        )
    ]

    description = scheduler.build_stage_description("add oauth2", upstream)

    assert "no files changed" in description
