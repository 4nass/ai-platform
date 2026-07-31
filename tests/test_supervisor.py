"""Tests for core.orchestrator.supervisor."""

from __future__ import annotations

import json
import time
from pathlib import Path

import git
import pytest

from core.orchestrator import scheduler, supervisor, test_runner
from core.telemetry import store as telemetry
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
decomposer:
  provider: claude_code
"""

# decompose: false here -- these fixtures exercise DAG execution mechanics,
# not decomposition, and none of the fake providers below know how to answer
# a decomposer call. Decomposition itself is tested separately below with
# its own workflow.yaml (decompose defaults to true when the key is absent).
WORKFLOW_YAML = """max_parallel: 2
decompose: false
tasks:
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
    # mirrors the real repo's .gitignore: the embedded vector DB is generated,
    # not something a stage's commit should ever sweep up
    (tmp_path / ".gitignore").write_text("vector/*\n", encoding="utf-8")

    repo.index.add([".gitignore", "config/context.yaml", "config/agents.yaml", "config/workflow.yaml", "src.py"])
    repo.index.commit("initial commit")
    return tmp_path


def _patch_provider(monkeypatch: pytest.MonkeyPatch, fake_run) -> None:
    monkeypatch.setitem(scheduler.PROVIDERS, "claude_code", type("FakeProvider", (), {"run": staticmethod(fake_run)}))


def _patch_tests(monkeypatch: pytest.MonkeyPatch, passed: bool, output: str = "") -> None:
    monkeypatch.setattr(
        test_runner, "run_tests", lambda repo_root: test_runner.TestResult(passed=passed, output=output)
    )


def _write_compliant_artifact(task: AgentTask) -> None:
    """Writes a file inside the agent's declared contract (see
    core.orchestrator.contracts) so these fakes don't spuriously trip the
    Phase 2 contract check."""
    if task.agent == "architect":
        path = Path(task.repo_root, "memory/architecture.md")
    elif task.agent == "documentation":
        path = Path(task.repo_root, "README.md")
    elif task.agent == "security":
        return  # never writes any file
    else:
        path = Path(task.repo_root, f"{task.agent}.py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# produced by {task.agent}\n", encoding="utf-8")


def _multi_stage_run(verdict: str = "VERDICT: PASS", fail_agents: frozenset[str] = frozenset()):
    def fake_run(task: AgentTask) -> ProviderResult:
        if task.agent == "reviewer":
            return ProviderResult(success=True, summary=f"Review notes.\n{verdict}")
        if task.agent in fail_agents:
            return ProviderResult(success=False, summary=f"{task.agent} failed")
        _write_compliant_artifact(task)
        return ProviderResult(success=True, summary=f"{task.agent} done")

    return fake_run


def test_run_executes_all_stages_respecting_dependency_order(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="6 passed")

    report = supervisor.run(fake_repo, "add oauth2")

    ids = [s.id for s in report.stages]
    assert ids[0] == "architecture"
    # backend/frontend run concurrently (both only depend on architecture) --
    # which finishes first isn't deterministic, only that both land before tests
    assert set(ids[1:3]) == {"backend", "frontend"}
    assert ids[3:] == ["tests", "security", "documentation"]
    assert all(s.status == "done" for s in report.stages)
    assert report.summary == "done"


def test_run_executes_independent_stages_concurrently(monkeypatch: pytest.MonkeyPatch, fake_repo: Path) -> None:
    """Proof of real concurrency has to isolate the two stages' own sleep
    windows, not total run() wall-clock time -- the latter is dominated by
    unrelated, highly variable cost (the embeddings model load inside
    ContextManager.index_repo(), seconds on a cold start vs. near-zero once
    warm from an earlier test), which would make a total-time assertion
    flaky regardless of whether the stages actually overlap."""
    sleep_seconds = 0.3
    intervals: dict[str, tuple[float, float]] = {}

    def fake_run(task: AgentTask) -> ProviderResult:
        if task.agent == "reviewer":
            return ProviderResult(success=True, summary="VERDICT: PASS")
        if task.agent in ("backend", "frontend"):
            started = time.monotonic()
            time.sleep(sleep_seconds)
            intervals[task.agent] = (started, time.monotonic())
        _write_compliant_artifact(task)
        return ProviderResult(success=True, summary=f"{task.agent} done")

    _patch_provider(monkeypatch, fake_run)
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, "add oauth2")

    assert report.summary == "done"
    backend_start, backend_end = intervals["backend"]
    frontend_start, frontend_end = intervals["frontend"]
    # real concurrency: each stage's sleep window starts before the other's ends
    assert backend_start < frontend_end
    assert frontend_start < backend_end


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
    messages = [c.message for c in repo.iter_commits(max_count=20)]
    assert any("architecture:" in m for m in messages)
    assert any("backend:" in m for m in messages)
    assert any("documentation:" in m for m in messages)


def test_run_isolates_a_failed_stages_partial_edits_to_its_own_worktree(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """Each stage runs in its own git worktree (core.orchestrator.git_ops) --
    a failed stage's partial edits are committed there and never merged, so
    they can't leak into a concurrently-running sibling's result."""

    def fake_run(task: AgentTask) -> ProviderResult:
        if task.agent == "reviewer":
            return ProviderResult(success=True, summary="VERDICT: PASS")
        if task.agent == "backend":
            Path(task.repo_root, "backend_partial.py").write_text("x = 1\n", encoding="utf-8")
            return ProviderResult(success=False, summary="backend crashed mid-edit")
        _write_compliant_artifact(task)
        return ProviderResult(success=True, summary=f"{task.agent} done")

    _patch_provider(monkeypatch, fake_run)
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, "add oauth2")

    by_id = {s.id: s for s in report.stages}
    assert "backend_partial.py" in by_id["backend"].files_changed
    assert "backend_partial.py" not in by_id["frontend"].files_changed
    # never merged into the run branch -- backend's failure shouldn't taint it
    assert "backend_partial.py" not in report.files_changed


def test_run_marks_conflicting_sibling_as_conflict_and_keeps_its_worktree(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    def fake_run(task: AgentTask) -> ProviderResult:
        if task.agent == "reviewer":
            return ProviderResult(success=True, summary="VERDICT: PASS")
        if task.agent == "backend":
            Path(task.repo_root, "shared.py").write_text("backend version\n", encoding="utf-8")
            return ProviderResult(success=True, summary="backend done")
        if task.agent == "frontend":
            Path(task.repo_root, "shared.py").write_text("frontend version\n", encoding="utf-8")
            return ProviderResult(success=True, summary="frontend done")
        _write_compliant_artifact(task)
        return ProviderResult(success=True, summary=f"{task.agent} done")

    _patch_provider(monkeypatch, fake_run)
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, "add oauth2")

    by_id = {s.id: s for s in report.stages}
    # both add the same file with different content -- whichever merges
    # first wins cleanly, the other conflicts. Which one wins the race isn't
    # deterministic, only that exactly one of each outcome happens.
    assert {by_id["backend"].status, by_id["frontend"].status} == {"done", "conflict"}
    assert by_id["tests"].status == "skipped"  # depends on both backend and frontend
    assert report.summary == "needs attention"


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


def test_run_marks_a_stage_violated_when_it_writes_outside_its_contract(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    def fake_run(task: AgentTask) -> ProviderResult:
        if task.agent == "reviewer":
            return ProviderResult(success=True, summary="VERDICT: PASS")
        if task.agent == "architect":
            # succeeds, but writes application code -- outside its contract
            Path(task.repo_root, "core/auth/oauth.py").parent.mkdir(parents=True, exist_ok=True)
            Path(task.repo_root, "core/auth/oauth.py").write_text("x = 1\n", encoding="utf-8")
            return ProviderResult(success=True, summary="architect done")
        Path(task.repo_root, f"{task.agent}.py").write_text(f"# {task.agent}\n", encoding="utf-8")
        return ProviderResult(success=True, summary=f"{task.agent} done")

    _patch_provider(monkeypatch, fake_run)
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, "add oauth2")

    by_id = {s.id: s for s in report.stages}
    assert by_id["architecture"].status == "violated"
    assert by_id["backend"].status == "skipped"
    assert by_id["frontend"].status == "skipped"
    assert report.summary == "needs attention"


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


def _enable_decompose(repo_root: Path) -> None:
    workflow_yaml = WORKFLOW_YAML.replace("decompose: false", "decompose: true")
    (repo_root / "config" / "workflow.yaml").write_text(workflow_yaml, encoding="utf-8")
    repo = git.Repo(repo_root)
    repo.index.add(["config/workflow.yaml"])
    repo.index.commit("enable decomposition")


def test_format_totals_counts_cached_input_not_just_the_uncached_remainder() -> None:
    """`input_tokens` is only what wasn't served from cache. Reporting it
    alone showed "28 in" for a real run that processed ~600k tokens, because
    prompt caching moves nearly everything into cache_read/cache_creation.
    Figures below are from that run.
    """
    line = supervisor.format_totals(
        {
            "calls": 3,
            "priced_calls": 3,
            "cost_usd": 0.7410,
            "input_tokens": 28,
            "cache_read_tokens": 514064,
            "cache_creation_tokens": 87578,
            "output_tokens": 3957,
        }
    )

    assert "601,670 in" in line
    assert "514,064 cached" in line
    assert "28 in" not in line


def test_format_totals_flags_unpriced_calls() -> None:
    line = supervisor.format_totals(
        {"calls": 8, "priced_calls": 3, "cost_usd": 0.42, "input_tokens": 100, "output_tokens": 10}
    )

    assert "(3/8 priced)" in line


def test_run_records_telemetry_for_every_provider_call(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, "add oauth2")

    with telemetry.connect(fake_repo) as con:
        run = con.execute("SELECT * FROM runs").fetchone()
        agents = [r["agent"] for r in con.execute("SELECT agent FROM calls ORDER BY id")]

    assert run["request"] == "add oauth2"
    assert run["summary"] == "done"
    assert run["engine_commit"]  # the engine version that produced these numbers
    metadata = json.loads(run["metadata"])
    assert metadata["use_graph"] is False  # config snapshot from the fixture
    assert metadata["injection_mode"] == "pointers"  # what makes the A/B queryable later

    # 6 DAG stages + the reviewer. No decomposer: the fixture sets decompose: false.
    assert agents.count("reviewer") == 1
    assert len(agents) == 7
    assert report.totals["calls"] == 7


def test_run_records_the_decomposer_call_too(monkeypatch: pytest.MonkeyPatch, fake_repo: Path) -> None:
    """The decomposer is a billable provider call — leaving it out would
    understate every decomposed run."""
    _enable_decompose(fake_repo)

    def fake_run(task: AgentTask) -> ProviderResult:
        if task.agent == "decomposer":
            return ProviderResult(success=True, summary="TASKS: architecture")
        if task.agent == "reviewer":
            return ProviderResult(success=True, summary="VERDICT: PASS")
        _write_compliant_artifact(task)
        return ProviderResult(success=True, summary=f"{task.agent} done")

    _patch_provider(monkeypatch, fake_run)
    _patch_tests(monkeypatch, passed=True, output="ok")

    supervisor.run(fake_repo, "add oauth2")

    with telemetry.connect(fake_repo) as con:
        agents = [r["agent"] for r in con.execute("SELECT agent FROM calls ORDER BY id")]

    assert agents[0] == "decomposer"
    assert agents == ["decomposer", "architect", "reviewer"]


def test_dry_run_records_nothing(monkeypatch: pytest.MonkeyPatch, fake_repo: Path) -> None:
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, "add oauth2", dry_run=True)

    assert report.totals == {}
    assert not (fake_repo / "telemetry.sqlite").exists()


def test_run_stores_the_session_id(monkeypatch: pytest.MonkeyPatch, fake_repo: Path) -> None:
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")

    supervisor.run(fake_repo, "add oauth2", session_id="whatsapp-42")

    with telemetry.connect(fake_repo) as con:
        assert con.execute("SELECT session_id FROM runs").fetchone()[0] == "whatsapp-42"


def test_run_prunes_the_plan_when_decomposer_selects_a_subset(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    _enable_decompose(fake_repo)

    def fake_run(task: AgentTask) -> ProviderResult:
        if task.agent == "decomposer":
            return ProviderResult(success=True, summary="Reasoning...\nTASKS: architecture, backend")
        if task.agent == "reviewer":
            return ProviderResult(success=True, summary="VERDICT: PASS")
        _write_compliant_artifact(task)
        return ProviderResult(success=True, summary=f"{task.agent} done")

    _patch_provider(monkeypatch, fake_run)
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, "add oauth2")

    ids = {s.id for s in report.stages}
    assert ids == {"architecture", "backend"}  # frontend/tests/security/documentation never even appear
    assert report.summary == "done"


def test_run_dry_run_invokes_only_the_decomposer_and_skips_the_rest(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    _enable_decompose(fake_repo)
    invoked_agents: list[str] = []

    def fake_run(task: AgentTask) -> ProviderResult:
        invoked_agents.append(task.agent)
        if task.agent == "decomposer":
            return ProviderResult(success=True, summary="Reasoning...\nTASKS: architecture, backend")
        raise AssertionError(f"dry run should not invoke {task.agent}")

    _patch_provider(monkeypatch, fake_run)

    repo = git.Repo(fake_repo)
    branch_before = repo.active_branch.name

    report = supervisor.run(fake_repo, "add oauth2", dry_run=True)

    assert invoked_agents == ["decomposer"]  # no work agent, no reviewer
    assert report.summary == "dry-run"
    assert report.stages == []
    assert report.files_changed == []
    assert repo.active_branch.name == branch_before  # no hermes/<slug> branch created


def test_run_dry_run_without_decomposition_invokes_no_agent_at_all(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    def fake_run(task: AgentTask) -> ProviderResult:
        raise AssertionError(f"dry run should not invoke {task.agent}")

    _patch_provider(monkeypatch, fake_run)

    repo = git.Repo(fake_repo)
    branch_before = repo.active_branch.name

    report = supervisor.run(fake_repo, "add oauth2", dry_run=True)

    assert report.summary == "dry-run"
    assert report.stages == []
    assert report.files_changed == []
    assert repo.active_branch.name == branch_before  # no hermes/<slug> branch created


def test_run_dry_run_prints_the_full_planned_workflow(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole point of --dry-run is what it prints (see
    core.orchestrator.supervisor.run's dry_run branch) -- the other dry-run
    tests only check the returned RunReport and which agents got invoked, so
    this one is the one actually asserting on that printed plan."""

    def fake_run(task: AgentTask) -> ProviderResult:
        raise AssertionError(f"dry run should not invoke {task.agent}")

    _patch_provider(monkeypatch, fake_run)

    supervisor.run(fake_repo, "add oauth2", dry_run=True)

    out = capsys.readouterr().out
    assert "Dry run" in out
    assert "Planned workflow:" in out
    assert "architecture (architect) depends_on: none" in out
    assert "backend (backend) depends_on: architecture" in out
    assert "documentation (documentation) depends_on: security" in out


def test_run_dry_run_prints_the_decomposers_pruned_selection(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _enable_decompose(fake_repo)

    def fake_run(task: AgentTask) -> ProviderResult:
        if task.agent == "decomposer":
            return ProviderResult(success=True, summary="Reasoning...\nTASKS: architecture, backend")
        raise AssertionError(f"dry run should not invoke {task.agent}")

    _patch_provider(monkeypatch, fake_run)

    report = supervisor.run(fake_repo, "add oauth2", dry_run=True)

    out = capsys.readouterr().out
    assert "Decomposed to:" in out
    assert "architecture, backend" in out
    assert "frontend" in out and "not needed" in out  # dropped tasks are called out too
    assert "Planned workflow:" in out
    assert "architecture (architect)" in out
    assert "backend (backend)" in out
    # the printed plan reflects the pruned selection, not the full workflow
    assert "frontend (frontend)" not in out
    assert "tests (tests)" not in out
    assert report.summary == "dry-run"


def test_run_falls_back_to_the_full_plan_when_decomposition_is_unparseable(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    _enable_decompose(fake_repo)

    def fake_run(task: AgentTask) -> ProviderResult:
        if task.agent == "decomposer":
            return ProviderResult(success=True, summary="I'm not sure what's needed here.")
        if task.agent == "reviewer":
            return ProviderResult(success=True, summary="VERDICT: PASS")
        _write_compliant_artifact(task)
        return ProviderResult(success=True, summary=f"{task.agent} done")

    _patch_provider(monkeypatch, fake_run)
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, "add oauth2")

    ids = {s.id for s in report.stages}
    assert ids == {"architecture", "backend", "frontend", "tests", "security", "documentation"}
    assert report.summary == "done"
