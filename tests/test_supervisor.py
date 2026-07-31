"""Tests for core.orchestrator.supervisor."""

from __future__ import annotations

from pathlib import Path

import git
import pytest

from core.orchestrator import scheduler, supervisor, test_runner
from providers.base import AgentTask, ProviderResult

AGENTS_YAML = """architect:
  provider: claude_code
backend:
  provider: claude_code
frontend:
  provider: claude_code
tests:
  provider: claude_code
security:
  provider: claude_code
documentation:
  provider: claude_code
reviewer:
  provider: claude_code
"""

WORKFLOW_YAML = """tasks:
  - id: architecture
    agent: architect
    depends_on: []
  - id: backend
    agent: backend
    depends_on: [architecture]
  - id: frontend
    agent: frontend
    depends_on: [architecture]
  - id: tests
    agent: tests
    depends_on: [backend, frontend]
  - id: security
    agent: security
    depends_on: [tests]
  - id: documentation
    agent: documentation
    depends_on: [security]
"""

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
    (tmp_path / "config" / "workflow.yaml").write_text(WORKFLOW_YAML, encoding="utf-8")
    (tmp_path / "src.py").write_text("def foo():\n    return 1\n", encoding="utf-8")

    repo.index.add(["config/context.yaml", "config/agents.yaml", "config/workflow.yaml", "src.py"])
    repo.index.commit("initial commit")
    return tmp_path


def _patch_provider(monkeypatch: pytest.MonkeyPatch, fake_run) -> None:
    monkeypatch.setitem(scheduler.PROVIDERS, "claude_code", type("FakeProvider", (), {"run": staticmethod(fake_run)}))


def _patch_tests(monkeypatch: pytest.MonkeyPatch, passed: bool, output: str = "") -> None:
    monkeypatch.setattr(
        test_runner, "run_tests", lambda repo_root: test_runner.TestResult(passed=passed, output=output)
    )


def _multi_stage_run(verdict: str = "VERDICT: PASS", fail_agents: frozenset[str] = frozenset()):
    def fake_run(task: AgentTask) -> ProviderResult:
        if task.agent == "reviewer":
            return ProviderResult(success=True, summary=f"Review notes.\n{verdict}")
        if task.agent in fail_agents:
            return ProviderResult(success=False, summary=f"{task.agent} failed")
        Path(task.repo_root, f"{task.agent}.py").write_text(f"# {task.agent}\n", encoding="utf-8")
        return ProviderResult(success=True, summary=f"{task.agent} done")

    return fake_run


def test_run_executes_all_stages_in_dependency_order(monkeypatch: pytest.MonkeyPatch, fake_repo: Path) -> None:
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="6 passed")

    report = supervisor.run(fake_repo, "add oauth2")

    assert [s.id for s in report.stages] == [
        "architecture",
        "backend",
        "frontend",
        "tests",
        "security",
        "documentation",
    ]
    assert all(s.status == "done" for s in report.stages)
    assert report.summary == "done"


def test_run_skips_downstream_tasks_when_a_dependency_fails(monkeypatch: pytest.MonkeyPatch, fake_repo: Path) -> None:
    _patch_provider(monkeypatch, _multi_stage_run(fail_agents=frozenset({"backend"})))
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, "add oauth2")

    by_id = {s.id: s for s in report.stages}
    assert by_id["architecture"].status == "done"
    assert by_id["backend"].status == "failed"
    assert by_id["frontend"].status == "done"  # sibling: doesn't depend on backend
    assert by_id["tests"].status == "skipped"  # depends on backend AND frontend
    assert by_id["security"].status == "skipped"
    assert by_id["documentation"].status == "skipped"
    assert report.summary == "needs attention"


def test_run_commits_each_stage_separately(monkeypatch: pytest.MonkeyPatch, fake_repo: Path) -> None:
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")

    supervisor.run(fake_repo, "add oauth2")

    repo = git.Repo(fake_repo)
    messages = [c.message for c in repo.iter_commits(max_count=10)]
    assert any("architecture:" in m for m in messages)
    assert any("backend:" in m for m in messages)
    assert any("documentation:" in m for m in messages)


def test_run_commits_partial_edits_from_a_failed_stage_so_they_dont_leak_into_the_next_commit(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    def fake_run(task: AgentTask) -> ProviderResult:
        if task.agent == "reviewer":
            return ProviderResult(success=True, summary="VERDICT: PASS")
        if task.agent == "backend":
            Path(task.repo_root, "backend_partial.py").write_text("x = 1\n", encoding="utf-8")
            return ProviderResult(success=False, summary="backend crashed mid-edit")
        Path(task.repo_root, f"{task.agent}.py").write_text(f"# {task.agent}\n", encoding="utf-8")
        return ProviderResult(success=True, summary=f"{task.agent} done")

    _patch_provider(monkeypatch, fake_run)
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, "add oauth2")

    by_id = {s.id: s for s in report.stages}
    assert "backend_partial.py" in by_id["backend"].files_changed
    assert "backend_partial.py" not in by_id["frontend"].files_changed


def test_run_needs_attention_when_tests_fail(monkeypatch: pytest.MonkeyPatch, fake_repo: Path) -> None:
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=False, output="1 failed")

    report = supervisor.run(fake_repo, "add oauth2")

    assert report.summary == "needs attention"
    assert report.tests_passed is False


def test_run_needs_attention_when_review_fails(monkeypatch: pytest.MonkeyPatch, fake_repo: Path) -> None:
    _patch_provider(monkeypatch, _multi_stage_run(verdict="VERDICT: FAIL"))
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, "add oauth2")

    assert report.summary == "needs attention"
    assert report.review_passed is False


def test_run_stops_early_when_the_first_stage_fails_with_no_disk_writes(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    def fake_run(task: AgentTask) -> ProviderResult:
        if task.agent == "reviewer":
            return ProviderResult(success=True, summary="VERDICT: PASS")
        return ProviderResult(success=False, summary="claude CLI: not logged in")

    _patch_provider(monkeypatch, fake_run)
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, "add oauth2")

    by_id = {s.id: s for s in report.stages}
    assert by_id["architecture"].status == "failed"
    assert all(s.status == "skipped" for s in report.stages[1:])
    assert report.files_changed == []
    assert report.summary == "needs attention"
