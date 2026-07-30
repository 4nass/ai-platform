"""Tests for core.orchestrator.supervisor."""

from __future__ import annotations

from pathlib import Path

import git
import pytest

from core.orchestrator import scheduler, supervisor, test_runner
from providers.base import AgentTask, ProviderResult

AGENTS_YAML = "backend:\n  provider: claude_code\nreviewer:\n  provider: claude_code\n"
CONTEXT_YAML = "use_git_diff: true\nuse_graph: false\nuse_vector_db: true\nuse_memory: true\nmax_files: 5\n"


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    repo = git.Repo.init(tmp_path)
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "Test")
        cw.set_value("user", "email", "test@example.com")

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "context.yaml").write_text(CONTEXT_YAML, encoding="utf-8")
    (tmp_path / "config" / "agents.yaml").write_text(AGENTS_YAML, encoding="utf-8")
    (tmp_path / "src.py").write_text("def foo():\n    return 1\n", encoding="utf-8")

    repo.index.add(["config/context.yaml", "config/agents.yaml", "src.py"])
    repo.index.commit("initial commit")
    return tmp_path


def _patch_provider(monkeypatch: pytest.MonkeyPatch, fake_run) -> None:
    monkeypatch.setitem(scheduler.PROVIDERS, "claude_code", type("FakeProvider", (), {"run": staticmethod(fake_run)}))


def _patch_tests(monkeypatch: pytest.MonkeyPatch, passed: bool, output: str = "") -> None:
    monkeypatch.setattr(
        test_runner, "run_tests", lambda repo_root: test_runner.TestResult(passed=passed, output=output)
    )


def _changing_run(verdict: str):
    def fake_run(task: AgentTask) -> ProviderResult:
        if task.agent == "reviewer":
            return ProviderResult(success=True, summary=f"Review notes.\n{verdict}")
        Path(task.repo_root, "new.py").write_text("x = 1\n", encoding="utf-8")
        return ProviderResult(success=True, summary="added new.py")

    return fake_run


def test_run_reports_done_when_everything_passes(monkeypatch: pytest.MonkeyPatch, fake_repo: Path) -> None:
    _patch_provider(monkeypatch, _changing_run("VERDICT: PASS"))
    _patch_tests(monkeypatch, passed=True, output="3 passed")

    report = supervisor.run(fake_repo, "add something", "backend")

    assert report.summary == "done"
    assert report.provider_success is True
    assert report.tests_passed is True
    assert report.review_passed is True
    assert "new.py" in report.files_changed


def test_run_needs_attention_when_tests_fail(monkeypatch: pytest.MonkeyPatch, fake_repo: Path) -> None:
    _patch_provider(monkeypatch, _changing_run("VERDICT: PASS"))
    _patch_tests(monkeypatch, passed=False, output="1 failed")

    report = supervisor.run(fake_repo, "add something", "backend")

    assert report.summary == "needs attention"
    assert report.tests_passed is False


def test_run_needs_attention_when_review_fails(monkeypatch: pytest.MonkeyPatch, fake_repo: Path) -> None:
    _patch_provider(monkeypatch, _changing_run("VERDICT: FAIL"))
    _patch_tests(monkeypatch, passed=True, output="3 passed")

    report = supervisor.run(fake_repo, "add something", "backend")

    assert report.summary == "needs attention"
    assert report.review_passed is False


def test_run_stops_early_when_provider_fails(monkeypatch: pytest.MonkeyPatch, fake_repo: Path) -> None:
    def fake_run(task: AgentTask) -> ProviderResult:
        return ProviderResult(success=False, summary="claude CLI: not logged in")

    _patch_provider(monkeypatch, fake_run)

    report = supervisor.run(fake_repo, "add something", "backend")

    assert report.summary == "needs attention"
    assert report.provider_success is False
    assert report.files_changed == []
    repo = git.Repo(fake_repo)
    assert repo.head.commit.message == "initial commit"
