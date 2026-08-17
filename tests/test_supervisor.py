"""Tests for core.orchestrator.supervisor."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import git
import pytest

from core.errors import ConfigError
from core.jobs import budget, store
from core.context.manager import SelectedContext
from core.orchestrator import (
    checkpoint, git_ops, planner, platform_config, registry, scheduler, supervisor,
    target_config, test_runner,
)
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
corrector:
  provider: claude_code
"""

# The DAG shape only -- the preset file no longer carries max_parallel/
# decompose/max_correction_attempts, those are config/platform.yaml's job now
# (see PLATFORM_YAML below).
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

# decompose: false here -- these fixtures exercise DAG execution mechanics,
# not decomposition, and none of the fake providers below know how to answer
# a decomposer call. Decomposition itself is tested separately below via
# _enable_decompose, which overrides this on its own copy of platform.yaml.
#
# max_correction_attempts: 0 -- these fixtures exercise the DAG/test/review
# gate itself; the correction loop that can follow a test/review failure is
# tested separately below (test_run_correction_loop_*), which overrides this
# to a positive value via _enable_correction.
PLATFORM_YAML = """profile: balanced
workflow:
  mode: standard
  max_parallel: 2
  decompose: false
  max_correction_attempts: 0
context:
  mode: smart
"""

CONTEXT_YAML = "use_git_diff: true\nuse_graph: false\nuse_vector_db: true\nuse_memory: true\nmax_files: 5\n"


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    repo = git.Repo.init(tmp_path)
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "Test")
        cw.set_value("user", "email", "test@example.com")

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "platform.yaml").write_text(PLATFORM_YAML, encoding="utf-8")
    (tmp_path / "config/presets/profiles").mkdir(parents=True)
    (tmp_path / "config/presets/profiles/balanced.yaml").write_text(AGENTS_YAML, encoding="utf-8")
    (tmp_path / "config/presets/workflow").mkdir(parents=True)
    (tmp_path / "config/presets/workflow/standard.yaml").write_text(WORKFLOW_YAML, encoding="utf-8")
    (tmp_path / "config/presets/context").mkdir(parents=True)
    (tmp_path / "config/presets/context/smart.yaml").write_text(CONTEXT_YAML, encoding="utf-8")
    (tmp_path / "src.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    # mirrors the real repo's .gitignore: the embedded vector store/graph
    # cache under .ai-platform/ is generated, not something a stage's commit
    # should ever sweep up (see core.context.manager.VECTOR_STORAGE_PATH)
    (tmp_path / ".gitignore").write_text(".ai-platform/\n", encoding="utf-8")

    repo.index.add([
        ".gitignore",
        "config/platform.yaml",
        "config/presets/profiles/balanced.yaml",
        "config/presets/workflow/standard.yaml",
        "config/presets/context/smart.yaml",
        "src.py",
    ])
    repo.index.commit("initial commit")
    return tmp_path


def _patch_provider(monkeypatch: pytest.MonkeyPatch, fake_run) -> None:
    monkeypatch.setitem(scheduler.PROVIDERS, "claude_code", type("FakeProvider", (), {"run": staticmethod(fake_run)}))


def _patch_tests(monkeypatch: pytest.MonkeyPatch, passed: bool, output: str = "") -> None:
    monkeypatch.setattr(
        test_runner,
        "run_tests",
        lambda repo_root, config: test_runner.TestResult(passed=passed, output=output),
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

    report = supervisor.run(fake_repo, fake_repo, "add oauth2")

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

    report = supervisor.run(fake_repo, fake_repo, "add oauth2")

    assert report.summary == "done"
    backend_start, backend_end = intervals["backend"]
    frontend_start, frontend_end = intervals["frontend"]
    # real concurrency: each stage's sleep window starts before the other's ends
    assert backend_start < frontend_end
    assert frontend_start < backend_end


def test_run_skips_downstream_tasks_when_a_dependency_fails(monkeypatch: pytest.MonkeyPatch, fake_repo: Path) -> None:
    _patch_provider(monkeypatch, _multi_stage_run(fail_agents=frozenset({"backend"})))
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, fake_repo, "add oauth2")

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

    report = supervisor.run(fake_repo, fake_repo, "add oauth2")

    repo = git.Repo(fake_repo)
    # walk the run branch, not HEAD: the commits land in the run's own
    # integration worktree, and the caller's checkout never moves
    messages = [c.message for c in repo.iter_commits(report.branch, max_count=20)]
    assert any("architecture:" in m for m in messages)
    assert any("backend:" in m for m in messages)
    assert any("documentation:" in m for m in messages)


def test_run_never_moves_the_targets_own_checkout(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """The point of the integration worktree: a run used to switch the user's
    HEAD to engine/<slug> and leave it there."""
    repo = git.Repo(fake_repo)
    branch_before = repo.active_branch.name
    head_before = repo.head.commit.hexsha

    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, fake_repo, "add oauth2")

    assert repo.active_branch.name == branch_before
    assert repo.head.commit.hexsha == head_before
    assert report.branch != branch_before
    # the work is on the run branch, ahead of where the caller still sits
    assert repo.commit(report.branch).hexsha != head_before


# --- a run sees exactly its base commit, and nothing else ---
#
# A run is checked out from `base_sha` and every prompt is built from that
# checkout. Excluding the git diff was not enough on its own: file content,
# chunk excerpts and project memory were all read off the user's own working
# tree, so an uncommitted edit could describe code that did not exist in what
# the agent was editing. These tests pin both halves -- what reaches the
# prompt and what reaches the worktree -- against the same base commit.

COMMITTED_MARKER = "COMMITTED_ONLY_9f3a"
LOCAL_MARKER = "LOCAL_ONLY_4b7e"


def _prepare_dirty_target(repo_root: Path) -> str:
    """Commits content carrying COMMITTED_MARKER, then dirties the checkout
    three ways: a tracked source edit, a tracked *memory* edit, and an
    untracked memory doc -- each carrying LOCAL_MARKER. Returns the base
    commit.

    Two deliberate choices make the absence assertions mean something rather
    than pass by accident:

    - `injection_mode: full`, because pointers mode sends a ranked list of
      paths and no file text at all;
    - project memory as the carrier, because `memory/*.md` is loaded
      unconditionally (it is not subject to the relevance floors), so whether
      it reaches the prompt depends only on which tree was read -- not on
      what an embedding model happened to score.
    """
    (repo_root / "config/presets/context/smart.yaml").write_text(
        CONTEXT_YAML + "injection_mode: full\n", encoding="utf-8"
    )
    (repo_root / "memory").mkdir(exist_ok=True)
    (repo_root / "memory" / "business_rules.md").write_text(
        f"Rule: {COMMITTED_MARKER}\n", encoding="utf-8"
    )
    (repo_root / "src.py").write_text(
        f"def foo():\n    return '{COMMITTED_MARKER}'\n", encoding="utf-8"
    )
    repo = git.Repo(repo_root)
    repo.index.add(["config/presets/context/smart.yaml", "memory/business_rules.md", "src.py"])
    base_sha = repo.index.commit("committed state").hexsha

    (repo_root / "memory" / "business_rules.md").write_text(
        f"Rule: {LOCAL_MARKER}\n", encoding="utf-8"
    )
    (repo_root / "src.py").write_text(
        f"def foo():\n    return '{LOCAL_MARKER}'\n", encoding="utf-8"
    )
    (repo_root / "memory" / "local_notes.md").write_text(
        f"Draft: {LOCAL_MARKER}\n", encoding="utf-8"
    )
    return base_sha


WATCHED_PATHS = ("src.py", "memory/business_rules.md", "memory/local_notes.md")


def _capturing_run(prompts: list[str], worktrees: list[dict[str, str]]):
    """A provider fake that records what each call was told and what it could
    see on disk, before doing its stage's normal work."""

    def fake_run(task: AgentTask) -> ProviderResult:
        prompts.append(task.context_render)
        worktrees.append(
            {
                rel: (
                    Path(task.repo_root, rel).read_text(encoding="utf-8")
                    if Path(task.repo_root, rel).is_file()
                    else ""
                )
                for rel in WATCHED_PATHS
            }
        )
        if task.agent == "reviewer":
            return ProviderResult(success=True, summary="Review notes.\nVERDICT: PASS")
        _write_compliant_artifact(task)
        return ProviderResult(success=True, summary=f"{task.agent} done")

    return fake_run


def test_a_local_modification_reaches_neither_the_prompt_nor_the_worktree(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    _prepare_dirty_target(fake_repo)
    prompts: list[str] = []
    worktrees: list[dict[str, str]] = []
    _patch_provider(monkeypatch, _capturing_run(prompts, worktrees))
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, fake_repo, "add oauth2")

    assert report.summary == "done"

    # Positive control. Project memory is ungated -- it is not subject to the
    # relevance floors -- and `full` inlines it, so the committed text really
    # is in every prompt that carried context at all. Without this, "the local
    # marker is absent" would also pass on an empty string.
    carried = [p for p in prompts if p]
    assert carried and all(COMMITTED_MARKER in p for p in carried)
    assert not any(LOCAL_MARKER in p for p in prompts)

    assert worktrees
    assert all(COMMITTED_MARKER in seen["src.py"] for seen in worktrees)
    assert all(LOCAL_MARKER not in seen["src.py"] for seen in worktrees)
    assert all(LOCAL_MARKER not in seen["memory/business_rules.md"] for seen in worktrees)


def test_an_untracked_file_is_neither_injected_nor_checked_out(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """An untracked file is invisible to `git worktree add`, so injecting it
    would describe a file the agent cannot open.

    The untracked file here is a *memory doc*, which is loaded ungated: read
    from the user's checkout it would land in every prompt by name and by
    content, so its absence is a statement about which tree was read.
    """
    _prepare_dirty_target(fake_repo)
    prompts: list[str] = []
    worktrees: list[dict[str, str]] = []
    _patch_provider(monkeypatch, _capturing_run(prompts, worktrees))
    _patch_tests(monkeypatch, passed=True, output="ok")

    supervisor.run(fake_repo, fake_repo, "add oauth2")

    assert not any("local_notes" in p for p in prompts)
    assert worktrees and all(seen["memory/local_notes.md"] == "" for seen in worktrees)


def test_what_the_agents_see_is_byte_for_byte_the_base_commit(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """Both halves against the same commit: the text put in the prompt, and
    the bytes on disk in the tree that text describes."""
    _prepare_dirty_target(fake_repo)
    repo = git.Repo(fake_repo)
    base_sha = repo.head.commit.hexsha
    prompts: list[str] = []
    worktrees: list[dict[str, str]] = []
    _patch_provider(monkeypatch, _capturing_run(prompts, worktrees))
    _patch_tests(monkeypatch, passed=True, output="ok")

    supervisor.run(fake_repo, fake_repo, "add oauth2")

    for rel in ("src.py", "memory/business_rules.md"):
        committed = repo.git.show(f"{base_sha}:{rel}")
        assert worktrees
        assert all(seen[rel].rstrip("\n") == committed.rstrip("\n") for seen in worktrees)

    # The injected side, verbatim -- memory is rendered inline in `full` mode.
    # The negative half is what makes this discriminating: a diff of the
    # user's tree quotes the committed line too (as a `-` line), so
    # "the committed text is present" alone would hold even when the prompt
    # was built from the dirty checkout.
    injected_memory = repo.git.show(f"{base_sha}:memory/business_rules.md").strip()
    carried = [p for p in prompts if p]
    assert carried and all(injected_memory in p for p in carried)
    assert not any(LOCAL_MARKER in p for p in prompts)


def _user_visible_status(repo: git.Repo) -> list[str]:
    """The target's own status, minus the engine's artifacts.

    `.ai-platform/` (the vector index and graph cache) and `telemetry.sqlite`
    belong to the engine, and here engine_root and target_root happen to be
    the same directory -- so they show up in this repo's status without being
    anything the run did to the *target*.
    """
    return sorted(
        line
        for line in repo.git.status("--porcelain", "--untracked-files=all").splitlines()
        if not line[3:].startswith((".ai-platform", "telemetry.sqlite"))
    )


def test_the_user_checkout_is_left_exactly_as_it_was(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """Not switched, not stashed, not committed, not cleaned. A run against a
    repo someone is working in has to be invisible to them."""
    _prepare_dirty_target(fake_repo)
    repo = git.Repo(fake_repo)
    before = (
        repo.head.commit.hexsha,
        repo.active_branch.name,
        _user_visible_status(repo),
        Path(fake_repo, "src.py").read_text(encoding="utf-8"),
        Path(fake_repo, "memory/local_notes.md").read_text(encoding="utf-8"),
    )
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")

    supervisor.run(fake_repo, fake_repo, "add oauth2")

    assert (
        repo.head.commit.hexsha,
        repo.active_branch.name,
        _user_visible_status(repo),
        Path(fake_repo, "src.py").read_text(encoding="utf-8"),
        Path(fake_repo, "memory/local_notes.md").read_text(encoding="utf-8"),
    ) == before


def test_run_reports_how_many_local_modifications_it_left_out(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The count, not a "your tree is dirty" warning: the reader already knows
    it's dirty, what they can't see is that four specific edits are outside
    what the agents were given."""
    _prepare_dirty_target(fake_repo)  # 2 tracked edits + 1 untracked file
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")

    supervisor.run(fake_repo, fake_repo, "add oauth2")

    out = capsys.readouterr().out
    assert "3 local modification(s) excluded from the run" in out


def test_run_says_nothing_about_exclusions_when_the_target_is_clean(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")

    supervisor.run(fake_repo, fake_repo, "add oauth2")

    assert "excluded from the run" not in capsys.readouterr().out


def test_run_records_the_dirty_policy_and_whether_the_source_was_dirty(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """A run whose source was dirty is not wrong, but it isn't reproducible
    from `base_sha` alone either -- the excluded edits explain a result the
    commit doesn't."""
    _prepare_dirty_target(fake_repo)
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")

    supervisor.run(fake_repo, fake_repo, "add oauth2")

    with telemetry.connect(fake_repo) as con:
        metadata = json.loads(con.execute("SELECT metadata FROM runs").fetchone()["metadata"])

    assert metadata["dirty_policy"] == supervisor.DIRTY_HEAD
    assert metadata["source_dirty"] is True
    assert metadata["excluded_local_modifications"] == 3


def test_run_records_a_clean_source_as_such(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")

    supervisor.run(fake_repo, fake_repo, "add oauth2")

    with telemetry.connect(fake_repo) as con:
        metadata = json.loads(con.execute("SELECT metadata FROM runs").fetchone()["metadata"])

    assert metadata["source_dirty"] is False
    assert metadata["excluded_local_modifications"] == 0


# --- the other dirty policies ---


def test_dirty_policy_reject_refuses_and_creates_nothing(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    _prepare_dirty_target(fake_repo)
    repo = git.Repo(fake_repo)
    branches_before = {h.name for h in repo.heads}
    _patch_provider(monkeypatch, _multi_stage_run())

    with pytest.raises(ConfigError, match="dirty-policy=reject"):
        supervisor.run(fake_repo, fake_repo, "add oauth2", dirty_policy=supervisor.DIRTY_REJECT)

    assert {h.name for h in repo.heads} == branches_before


def test_dirty_policy_reject_runs_normally_on_a_clean_target(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(
        fake_repo, fake_repo, "add oauth2", dirty_policy=supervisor.DIRTY_REJECT
    )

    assert report.summary == "done"


def test_dirty_policy_snapshot_is_refused_even_on_a_clean_target(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """Falling back to `head` because the tree happens to be clean would put
    `dirty_policy=snapshot` in the telemetry for a run that did no such
    thing."""
    _patch_provider(monkeypatch, _multi_stage_run())

    with pytest.raises(ConfigError, match="not implemented"):
        supervisor.run(fake_repo, fake_repo, "add oauth2", dirty_policy=supervisor.DIRTY_SNAPSHOT)


def test_an_unknown_dirty_policy_is_refused(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    _patch_provider(monkeypatch, _multi_stage_run())

    with pytest.raises(ConfigError, match="Unknown --dirty-policy"):
        supervisor.run(fake_repo, fake_repo, "add oauth2", dirty_policy="yolo")


# --- the run lock comes before the first mutation ---


def test_a_refused_run_creates_no_branch_and_no_worktree(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """The lock used to be taken *after* the integration worktree existed, so
    the run that got refused had already pruned, branched and checked out a
    worktree before being told it couldn't start."""
    repo = git.Repo(fake_repo)
    branches_before = {h.name for h in repo.heads}
    worktrees_before = repo.git.worktree("list")
    _patch_provider(monkeypatch, _multi_stage_run())

    with git_ops.exclusive_run_lock(repo):
        with pytest.raises(RuntimeError, match="already modifying"):
            supervisor.run(fake_repo, fake_repo, "add oauth2")

    assert {h.name for h in repo.heads} == branches_before
    assert repo.git.worktree("list") == worktrees_before


def test_a_dry_run_leaves_no_branch_or_worktree_behind(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """A dry run now creates an integration worktree, because that is what it
    indexes from -- so it has to take it back down, or every inspection would
    leave an `engine/<slug>-N` branch behind."""
    repo = git.Repo(fake_repo)
    branches_before = {h.name for h in repo.heads}
    worktrees_before = repo.git.worktree("list")
    _patch_provider(monkeypatch, _multi_stage_run())

    report = supervisor.run(fake_repo, fake_repo, "add oauth2", dry_run=True)

    assert report.summary == "dry-run"
    assert {h.name for h in repo.heads} == branches_before
    assert repo.git.worktree("list") == worktrees_before


def test_a_successful_run_removes_its_integration_worktree_but_keeps_the_branch(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, fake_repo, "add oauth2")

    assert report.summary == "done"
    repo = git.Repo(fake_repo)
    assert "engine-run-" not in repo.git.worktree("list")
    assert report.branch in {h.name for h in repo.heads}  # the deliverable survives


def test_a_failed_run_keeps_its_integration_worktree_for_inspection(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """On failure the on-disk state answers questions the branch alone can't."""
    _patch_provider(monkeypatch, _multi_stage_run(verdict="VERDICT: FAIL"))
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, fake_repo, "add oauth2")

    assert report.summary == "needs attention"
    assert "engine-run-" in git.Repo(fake_repo).git.worktree("list")
    assert "Left for inspection" in capsys.readouterr().out


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

    report = supervisor.run(fake_repo, fake_repo, "add oauth2")

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

    report = supervisor.run(fake_repo, fake_repo, "add oauth2")

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

    report = supervisor.run(fake_repo, fake_repo, "add oauth2")

    assert report.summary == "needs attention"
    assert report.tests_passed is False
    # max_correction_attempts: 0 in this fixture -- no correction attempted
    assert report.correction_attempts == 0


def test_run_needs_attention_when_review_fails(monkeypatch: pytest.MonkeyPatch, fake_repo: Path) -> None:
    _patch_provider(monkeypatch, _multi_stage_run(verdict="VERDICT: FAIL"))
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, fake_repo, "add oauth2")

    assert report.summary == "needs attention"
    assert report.review_passed is False
    assert report.correction_attempts == 0


def test_run_marks_a_stage_violated_when_it_writes_to_a_gitignored_path(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """Reproduces issue #2's own repro almost exactly: a legitimate tracked
    change plus a hidden write to a gitignored path. Before the fix,
    commit_all/contracts.violations() only see the tracked file and report
    the stage compliant -- exactly the blind spot the issue is about, for a
    role (backend) that has no declared artifact contract at all."""
    (fake_repo / ".gitignore").write_text(".ai-platform/\n*.log\n", encoding="utf-8")
    git.Repo(fake_repo).index.add([".gitignore"])
    git.Repo(fake_repo).index.commit("add gitignore")

    def fake_run(task: AgentTask) -> ProviderResult:
        if task.agent == "reviewer":
            return ProviderResult(success=True, summary="VERDICT: PASS")
        if task.agent == "backend":
            Path(task.repo_root, "backend.py").write_text("x = 1\n", encoding="utf-8")
            Path(task.repo_root, "exfil.log").write_text("secret\n", encoding="utf-8")
            return ProviderResult(success=True, summary="backend done")
        _write_compliant_artifact(task)
        return ProviderResult(success=True, summary=f"{task.agent} done")

    _patch_provider(monkeypatch, fake_run)
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, fake_repo, "add oauth2")

    by_id = {s.id: s for s in report.stages}
    assert by_id["backend"].status == "violated"
    # the tainted worktree was never merged: the ignored file never reaches
    # target_root, and neither does backend.py, since the whole stage is
    # rejected rather than partially accepted
    assert "backend.py" not in report.files_changed
    assert not (fake_repo / "exfil.log").exists()
    assert report.summary == "needs attention"


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

    report = supervisor.run(fake_repo, fake_repo, "add oauth2")

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

    report = supervisor.run(fake_repo, fake_repo, "add oauth2")

    by_id = {s.id: s for s in report.stages}
    assert by_id["architecture"].status == "failed"
    assert all(s.status == "skipped" for s in report.stages[1:])
    assert report.files_changed == []
    assert report.summary == "needs attention"


def _enable_decompose(repo_root: Path) -> None:
    platform_yaml = PLATFORM_YAML.replace("decompose: false", "decompose: true")
    (repo_root / "config" / "platform.yaml").write_text(platform_yaml, encoding="utf-8")
    repo = git.Repo(repo_root)
    repo.index.add(["config/platform.yaml"])
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


def test_format_totals_scopes_a_partially_priced_run() -> None:
    """A subscription provider reports no price, so a dollar figure that
    covers only some calls must say which — otherwise it reads as the whole
    run's cost."""
    line = supervisor.format_totals(
        {"calls": 8, "priced_calls": 3, "cost_usd": 0.42, "input_tokens": 100, "output_tokens": 10}
    )

    assert "$0.4200 for 3/8" in line


def test_format_totals_leads_with_tokens_not_dollars() -> None:
    """Both providers are flat-rate subscriptions: tokens consume quota, a
    per-call price measures nothing the subscriber can act on."""
    line = supervisor.format_totals(
        {"calls": 3, "priced_calls": 3, "cost_usd": 0.42, "input_tokens": 100, "output_tokens": 10}
    )

    assert line.index("100 in") < line.index("$0.4200")


def test_format_totals_omits_cost_entirely_when_no_provider_reported_one() -> None:
    """`$0.0000` would read as free rather than as unpriced."""
    line = supervisor.format_totals(
        {"calls": 2, "priced_calls": 0, "cost_usd": 0, "input_tokens": 100, "output_tokens": 10}
    )

    assert "$" not in line


def test_run_records_telemetry_for_every_provider_call(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, fake_repo, "add oauth2")

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

    supervisor.run(fake_repo, fake_repo, "add oauth2")

    with telemetry.connect(fake_repo) as con:
        agents = [r["agent"] for r in con.execute("SELECT agent FROM calls ORDER BY id")]

    assert agents[0] == "decomposer"
    assert agents == ["decomposer", "architect", "reviewer"]


def test_dry_run_records_nothing(monkeypatch: pytest.MonkeyPatch, fake_repo: Path) -> None:
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, fake_repo, "add oauth2", dry_run=True)

    assert report.totals == {}
    assert not (fake_repo / "telemetry.sqlite").exists()


def test_run_stores_the_session_id(monkeypatch: pytest.MonkeyPatch, fake_repo: Path) -> None:
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")

    supervisor.run(fake_repo, fake_repo, "add oauth2", session_id="whatsapp-42")

    with telemetry.connect(fake_repo) as con:
        assert con.execute("SELECT session_id FROM runs").fetchone()[0] == "whatsapp-42"


def test_run_prunes_the_plan_when_decomposer_selects_a_subset(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    _enable_decompose(fake_repo)
    seen_complexities: dict[str, str] = {}

    def fake_run(task: AgentTask) -> ProviderResult:
        seen_complexities[task.agent] = task.complexity
        if task.agent == "decomposer":
            return ProviderResult(success=True, summary="Reasoning...\nCOMPLEXITY: critical\nTASKS: architecture, backend")
        if task.agent == "reviewer":
            return ProviderResult(success=True, summary="VERDICT: PASS")
        _write_compliant_artifact(task)
        return ProviderResult(success=True, summary=f"{task.agent} done")

    _patch_provider(monkeypatch, fake_run)
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, fake_repo, "add oauth2")

    ids = {s.id for s in report.stages}
    assert ids == {"architecture", "backend"}  # frontend/tests/security/documentation never even appear
    assert report.summary == "done"

    assert seen_complexities["decomposer"] == "routine"
    assert {seen_complexities[name] for name in ("architect", "backend", "reviewer")} == {"critical"}

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

    report = supervisor.run(fake_repo, fake_repo, "add oauth2", dry_run=True)

    assert invoked_agents == ["decomposer"]  # no work agent, no reviewer
    assert report.summary == "dry-run"
    assert report.stages == []
    assert report.files_changed == []
    assert repo.active_branch.name == branch_before  # no engine/<slug> branch created


def test_run_dry_run_without_decomposition_invokes_no_agent_at_all(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    def fake_run(task: AgentTask) -> ProviderResult:
        raise AssertionError(f"dry run should not invoke {task.agent}")

    _patch_provider(monkeypatch, fake_run)

    repo = git.Repo(fake_repo)
    branch_before = repo.active_branch.name

    report = supervisor.run(fake_repo, fake_repo, "add oauth2", dry_run=True)

    assert report.summary == "dry-run"
    assert report.stages == []
    assert report.files_changed == []
    assert repo.active_branch.name == branch_before  # no engine/<slug> branch created


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

    supervisor.run(fake_repo, fake_repo, "add oauth2", dry_run=True)

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

    report = supervisor.run(fake_repo, fake_repo, "add oauth2", dry_run=True)

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

    report = supervisor.run(fake_repo, fake_repo, "add oauth2")

    ids = {s.id for s in report.stages}
    assert ids == {"architecture", "backend", "frontend", "tests", "security", "documentation"}
    assert report.summary == "done"


def _enable_correction(repo_root: Path, max_attempts: int = 1) -> None:
    platform_yaml = PLATFORM_YAML.replace(
        "max_correction_attempts: 0", f"max_correction_attempts: {max_attempts}"
    )
    (repo_root / "config" / "platform.yaml").write_text(platform_yaml, encoding="utf-8")
    repo = git.Repo(repo_root)
    repo.index.add(["config/platform.yaml"])
    repo.index.commit("enable correction")


def test_run_correction_loop_fixes_a_failing_test_and_succeeds(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """The corrector role is only invoked once tests/review actually failed --
    and once it "fixes" the problem, run() stops retrying instead of burning
    its remaining budget."""
    _enable_correction(fake_repo, max_attempts=2)
    corrector_calls: list[AgentTask] = []

    def fake_run(task: AgentTask) -> ProviderResult:
        if task.agent == "reviewer":
            return ProviderResult(success=True, summary="VERDICT: PASS")
        if task.agent == "corrector":
            corrector_calls.append(task)
            Path(task.repo_root, "fix.py").write_text("x = 1\n", encoding="utf-8")
            return ProviderResult(success=True, summary="fixed the failing assertion")
        _write_compliant_artifact(task)
        return ProviderResult(success=True, summary=f"{task.agent} done")

    _patch_provider(monkeypatch, fake_run)

    test_calls = {"n": 0}

    def fake_run_tests(repo_root: Path, config) -> test_runner.TestResult:
        test_calls["n"] += 1
        if test_calls["n"] == 1:
            return test_runner.TestResult(passed=False, output="1 failed")
        return test_runner.TestResult(passed=True, output="all passed")

    monkeypatch.setattr(test_runner, "run_tests", fake_run_tests)

    report = supervisor.run(fake_repo, fake_repo, "add oauth2")

    assert len(corrector_calls) == 1  # stopped after the first attempt fixed it
    assert report.correction_attempts == 1
    assert report.tests_passed is True
    assert report.summary == "done"
    assert "fix.py" in report.files_changed


def test_run_correction_loop_exhausts_attempts_and_still_needs_attention(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    _enable_correction(fake_repo, max_attempts=2)
    corrector_calls: list[AgentTask] = []

    def fake_run(task: AgentTask) -> ProviderResult:
        if task.agent == "reviewer":
            return ProviderResult(success=True, summary="VERDICT: PASS")
        if task.agent == "corrector":
            corrector_calls.append(task)
            return ProviderResult(success=True, summary="tried, but couldn't reproduce the failure")
        _write_compliant_artifact(task)
        return ProviderResult(success=True, summary=f"{task.agent} done")

    _patch_provider(monkeypatch, fake_run)
    _patch_tests(monkeypatch, passed=False, output="still failing")

    report = supervisor.run(fake_repo, fake_repo, "add oauth2")

    assert len(corrector_calls) == 2  # both attempts used, neither fixed it
    assert report.correction_attempts == 2
    assert report.tests_passed is False
    assert report.summary == "needs attention"


def test_run_correction_loop_stops_when_the_corrector_writes_to_a_gitignored_path(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """Unlike a DAG stage's worktree, the corrector runs directly on
    target_root -- an ignored write here would persist past this run rather
    than dying with a discarded worktree, so it stops the loop outright
    instead of continuing to iterate."""
    (fake_repo / ".gitignore").write_text(".ai-platform/\n*.log\n", encoding="utf-8")
    git.Repo(fake_repo).index.add([".gitignore"])
    git.Repo(fake_repo).index.commit("add gitignore")

    _enable_correction(fake_repo, max_attempts=2)
    corrector_calls: list[AgentTask] = []

    def fake_run(task: AgentTask) -> ProviderResult:
        if task.agent == "reviewer":
            return ProviderResult(success=True, summary="VERDICT: PASS")
        if task.agent == "corrector":
            corrector_calls.append(task)
            Path(task.repo_root, "exfil.log").write_text("secret\n", encoding="utf-8")
            return ProviderResult(success=True, summary="corrector done")
        _write_compliant_artifact(task)
        return ProviderResult(success=True, summary=f"{task.agent} done")

    _patch_provider(monkeypatch, fake_run)
    _patch_tests(monkeypatch, passed=False, output="still failing")

    report = supervisor.run(fake_repo, fake_repo, "add oauth2")

    assert len(corrector_calls) == 1  # stopped after the first attempt's anomaly
    assert report.correction_attempts == 1
    assert report.summary == "needs attention"


def test_run_correction_loop_does_not_trigger_on_a_dag_stage_failure(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """A stage that itself failed/was skipped isn't something a corrector
    pass can retroactively complete -- correction is scoped to test/review
    failure on an otherwise-complete DAG (see supervisor.run's `can_correct`)."""
    _enable_correction(fake_repo, max_attempts=2)
    corrector_calls: list[AgentTask] = []

    def fake_run(task: AgentTask) -> ProviderResult:
        if task.agent == "reviewer":
            return ProviderResult(success=True, summary="VERDICT: PASS")
        if task.agent == "corrector":
            corrector_calls.append(task)
            return ProviderResult(success=True, summary="corrector done")
        if task.agent == "backend":
            return ProviderResult(success=False, summary="backend failed")
        _write_compliant_artifact(task)
        return ProviderResult(success=True, summary=f"{task.agent} done")

    _patch_provider(monkeypatch, fake_run)
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, fake_repo, "add oauth2")

    assert corrector_calls == []
    assert report.correction_attempts == 0
    assert report.summary == "needs attention"


# --- worker crash containment (issue #1) ---


def test_an_unknown_agent_fails_one_stage_instead_of_crashing_the_run(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """A workflow naming a role that agents.yaml doesn't define used to raise
    ConfigError out of the worker, through future.result(), killing the whole
    run and stranding that stage's worktree."""
    workflow = WORKFLOW_YAML.replace("agent: backend", "agent: not_a_configured_role")
    (fake_repo / "config/presets/workflow/standard.yaml").write_text(workflow, encoding="utf-8")
    repo = git.Repo(fake_repo)
    repo.index.add(["config/presets/workflow/standard.yaml"])
    repo.index.commit("point a task at an undefined role")

    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, fake_repo, "add oauth2")

    by_id = {s.id: s for s in report.stages}
    assert by_id["backend"].status == "failed"
    assert "Unknown agent role" in by_id["backend"].summary
    # the sibling that shares no dependency with it still ran to completion
    assert by_id["frontend"].status == "done"
    assert report.summary == "needs attention"


def test_a_provider_raising_fails_one_stage_instead_of_crashing_the_run(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """Providers are expected to return a failed ProviderResult, but nothing
    forces them to — an adapter bug or an unimplemented one raises instead."""

    def fake_run(task: AgentTask) -> ProviderResult:
        if task.agent == "reviewer":
            return ProviderResult(success=True, summary="VERDICT: PASS")
        if task.agent == "backend":
            raise NotImplementedError("this provider is a stub")
        _write_compliant_artifact(task)
        return ProviderResult(success=True, summary=f"{task.agent} done")

    _patch_provider(monkeypatch, fake_run)
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, fake_repo, "add oauth2")

    by_id = {s.id: s for s in report.stages}
    assert by_id["backend"].status == "failed"
    assert "NotImplementedError" in by_id["backend"].summary
    assert by_id["frontend"].status == "done"
    assert report.summary == "needs attention"


def test_a_crashed_stage_leaves_no_worktree_behind(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """The leak half of issue #1: the worktree is created before anything
    that can raise, so an escaping exception stranded the directory with
    nothing left holding a reference to it."""

    def fake_run(task: AgentTask) -> ProviderResult:
        if task.agent == "reviewer":
            return ProviderResult(success=True, summary="VERDICT: PASS")
        if task.agent == "backend":
            raise RuntimeError("boom")
        _write_compliant_artifact(task)
        return ProviderResult(success=True, summary=f"{task.agent} done")

    _patch_provider(monkeypatch, fake_run)
    _patch_tests(monkeypatch, passed=True, output="ok")

    supervisor.run(fake_repo, fake_repo, "add oauth2")

    listed = git.Repo(fake_repo).git.worktree("list")
    assert "engine-task/" not in listed  # no task worktree still registered
    assert "engine-backend-" not in listed


def test_a_worker_that_breaks_its_never_raise_contract_is_still_contained(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """_run_stage_in_worktree is written never to raise; this covers the
    backstop for a bug in that guarantee itself, which is the failure mode
    the issue is actually about."""

    def exploding_stage(*args, **kwargs):
        raise RuntimeError("the worker's own error handling failed")

    monkeypatch.setattr(supervisor, "_run_stage_in_worktree", exploding_stage)
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, fake_repo, "add oauth2")

    assert report.summary == "needs attention"
    assert all(s.status in {"failed", "skipped"} for s in report.stages)


# --- run-scoped policy and ephemeral writes ---


def _write_target_policy(fake_repo: Path, body: str) -> None:
    (fake_repo / ".ai-platform.yml").write_text(body, encoding="utf-8")
    repo = git.Repo(fake_repo)
    repo.index.add([".ai-platform.yml"])
    repo.index.commit("declare target policy")


def test_a_stage_cannot_grant_the_run_new_permissions(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """The escalation this closes, demonstrated end to end before the fix:
    a role with no artifact contract rewrote .ai-platform.yml, the final
    test run re-read it, and `test_sandbox: false` plus an arbitrary
    `test_command` were honoured -- while the run still reported `done`."""
    _write_target_policy(fake_repo, 'test_command: ["python3", "-c", "print(1)"]\ntest_sandbox: true\n')

    seen: list = []

    def fake_run(task: AgentTask) -> ProviderResult:
        if task.agent == "reviewer":
            return ProviderResult(success=True, summary="VERDICT: PASS")
        Path(task.repo_root, ".ai-platform.yml").write_text(
            'test_command: ["python3", "-c", "print(2)"]\ntest_sandbox: false\n', encoding="utf-8"
        )
        return ProviderResult(success=True, summary=f"{task.agent} done")

    _patch_provider(monkeypatch, fake_run)
    monkeypatch.setattr(
        test_runner,
        "run_tests",
        lambda repo_root, config: seen.append(config) or test_runner.TestResult(passed=True, output="ok"),
    )

    supervisor.run(fake_repo, fake_repo, "add oauth2")

    assert seen, "the test runner was never reached"
    for config in seen:
        # the policy as committed before any agent ran, every time
        assert config.test_sandbox is True
        assert config.test_command == ("python3", "-c", "print(1)")


def test_a_declared_ephemeral_write_does_not_fail_a_stage(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """Found by a real run: a backend stage ran pytest, pytest created
    .pytest_cache/ (which self-ignores), and the stage was rejected with its
    work discarded."""
    (fake_repo / ".gitignore").write_text(".ai-platform/\n.pytest_cache/\n", encoding="utf-8")
    (fake_repo / ".ai-platform.yml").write_text(
        'allowed_ephemeral_writes:\n  - ".pytest_cache/**"\n', encoding="utf-8"
    )
    repo = git.Repo(fake_repo)
    repo.index.add([".gitignore", ".ai-platform.yml"])
    repo.index.commit("declare expected caches")

    def fake_run(task: AgentTask) -> ProviderResult:
        if task.agent == "reviewer":
            return ProviderResult(success=True, summary="VERDICT: PASS")
        if task.agent == "backend":
            cache = Path(task.repo_root, ".pytest_cache")
            cache.mkdir(exist_ok=True)
            (cache / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172\n", encoding="utf-8")
        _write_compliant_artifact(task)
        return ProviderResult(success=True, summary=f"{task.agent} done")

    _patch_provider(monkeypatch, fake_run)
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, fake_repo, "add oauth2")

    by_id = {s.id: s for s in report.stages}
    assert by_id["backend"].status == "done"
    assert report.summary == "done"


def test_an_undeclared_ignored_write_still_fails_the_stage(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """Declaring caches must not reopen issue #2: anything the project
    didn't declare is still invisible to the reviewer and still blocks."""
    (fake_repo / ".gitignore").write_text(".ai-platform/\n.pytest_cache/\n*.log\n", encoding="utf-8")
    (fake_repo / ".ai-platform.yml").write_text(
        'allowed_ephemeral_writes:\n  - ".pytest_cache/**"\n', encoding="utf-8"
    )
    repo = git.Repo(fake_repo)
    repo.index.add([".gitignore", ".ai-platform.yml"])
    repo.index.commit("declare expected caches")

    def fake_run(task: AgentTask) -> ProviderResult:
        if task.agent == "reviewer":
            return ProviderResult(success=True, summary="VERDICT: PASS")
        if task.agent == "backend":
            Path(task.repo_root, "exfil.log").write_text("secret\n", encoding="utf-8")
        _write_compliant_artifact(task)
        return ProviderResult(success=True, summary=f"{task.agent} done")

    _patch_provider(monkeypatch, fake_run)
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, fake_repo, "add oauth2")

    by_id = {s.id: s for s in report.stages}
    assert by_id["backend"].status == "violated"
    assert not (fake_repo / "exfil.log").exists()


def test_verification_runs_in_a_disposable_worktree(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """The test command is the one actor guaranteed to litter. Running it
    somewhere thrown away afterwards keeps .pytest_cache/.coverage out of
    the branch under review, and stops them being attributed to whichever
    actor happens to run next."""
    _write_target_policy(fake_repo, 'test_command: ["python3", "-c", "open(\'.coverage\',\'w\').write(\'x\')"]\n')
    seen_roots: list[Path] = []

    real_run_tests = test_runner.run_tests

    def spy(repo_root: Path, config):
        seen_roots.append(Path(repo_root))
        return real_run_tests(repo_root, config)

    monkeypatch.setattr(test_runner, "run_tests", spy)
    _patch_provider(monkeypatch, _multi_stage_run())

    report = supervisor.run(fake_repo, fake_repo, "add oauth2")

    assert seen_roots, "the test runner was never reached"
    verify_root = seen_roots[0]
    assert "engine-verify-" in verify_root.name  # a throwaway, not the integration worktree
    assert not verify_root.exists()  # and it's gone afterwards
    assert report.summary == "done"


# --- crash recovery: checkpointing and resume (issue #24's remaining half) ---


class _Progress:
    """Captures the progress fields a job store would persist, so a test can
    resume from exactly the information a crashed worker would have left."""

    def __init__(self) -> None:
        self.fields: dict = {}
        self.roots: list[str] = []

    def __call__(self, **fields) -> None:
        self.fields.update(fields)
        if "integration_root" in fields:
            # Kept as a history, not just the latest: a successful run clears
            # this field on purpose (the branch is the deliverable, the
            # directory is gone), and a test still needs to know where it was.
            self.roots.append(fields["integration_root"])

    @property
    def integration_root(self) -> Path:
        return Path(self.roots[0])

    @property
    def branch(self) -> str:
        return self.fields["branch"]


def _interrupted_run(monkeypatch: pytest.MonkeyPatch, fake_repo: Path) -> _Progress:
    """A run that lands three stages and then stops, leaving its worktree.

    `tests` failing is how a run is made to stop mid-DAG in-process — a real
    interruption is a killed process, which pytest cannot do to itself. What
    matters for resume is identical either way: three stages merged onto the
    branch, a checkpoint naming them, and a worktree still on disk.
    """
    _patch_provider(monkeypatch, _multi_stage_run(fail_agents=frozenset({"tests"})))
    _patch_tests(monkeypatch, passed=True, output="ok")
    progress = _Progress()

    report = supervisor.run(fake_repo, fake_repo, "add oauth2", progress=progress)

    assert report.summary == "needs attention"
    assert progress.integration_root.exists()
    return progress


def test_a_run_checkpoints_each_stage_as_it_merges(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """The record has to be on disk *while* the run is in flight — that is the
    whole point. Asserting it only at the end would pass even if it were
    written once, at the end, which is exactly when a crash has already made
    it useless."""
    progress = _Progress()
    seen: dict[str, set[str]] = {}

    def fake_run(task: AgentTask) -> ProviderResult:
        if task.agent == "reviewer":
            return ProviderResult(success=True, summary="VERDICT: PASS")
        state = checkpoint.load(progress.integration_root)
        seen[task.agent] = state.completed_ids if state else set()
        _write_compliant_artifact(task)
        return ProviderResult(success=True, summary=f"{task.agent} done")

    _patch_provider(monkeypatch, fake_run)
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, fake_repo, "add oauth2", progress=progress)

    assert report.summary == "done"
    # architecture saw an empty checkpoint; everything after it saw architecture
    assert seen["architect"] == set()
    assert "architecture" in seen["tests"]
    assert {"architecture", "backend", "frontend"} <= seen["security"]


def test_resuming_does_not_re_run_stages_already_merged(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    progress = _interrupted_run(monkeypatch, fake_repo)
    called: list[str] = []

    def fake_run(task: AgentTask) -> ProviderResult:
        called.append(task.agent)
        if task.agent == "reviewer":
            return ProviderResult(success=True, summary="VERDICT: PASS")
        _write_compliant_artifact(task)
        return ProviderResult(success=True, summary=f"{task.agent} done")

    _patch_provider(monkeypatch, fake_run)
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(
        fake_repo,
        fake_repo,
        "add oauth2",
        resume=supervisor.Resume(
            branch=progress.branch, integration_root=progress.integration_root
        ),
    )

    assert report.summary == "done"
    # the three stages already on the branch cost nothing a second time
    assert "architect" not in called
    assert "backend" not in called
    assert "frontend" not in called
    # and the ones that never completed do run
    assert {"tests", "security", "documentation"} <= set(called)


def test_a_resumed_run_still_reports_the_stages_it_skipped(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """A report listing only the stages this attempt ran would describe half a
    run — and `files_changed` is what the caller is handed as the deliverable."""
    progress = _interrupted_run(monkeypatch, fake_repo)
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(
        fake_repo,
        fake_repo,
        "add oauth2",
        resume=supervisor.Resume(
            branch=progress.branch, integration_root=progress.integration_root
        ),
    )

    by_id = {s.id: s for s in report.stages}
    assert set(by_id) == {"architecture", "backend", "frontend", "tests", "security", "documentation"}
    assert all(s.status == "done" for s in report.stages)
    assert by_id["backend"].files_changed == ["backend.py"]
    assert "backend.py" in report.files_changed


def test_resuming_continues_the_same_branch_rather_than_starting_another(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    progress = _interrupted_run(monkeypatch, fake_repo)
    branches_before = {head.name for head in git.Repo(fake_repo).heads}

    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(
        fake_repo,
        fake_repo,
        "add oauth2",
        resume=supervisor.Resume(
            branch=progress.branch, integration_root=progress.integration_root
        ),
    )

    assert report.branch == progress.branch
    new_engine_branches = {
        name
        for name in {head.name for head in git.Repo(fake_repo).heads} - branches_before
        if name.startswith("engine/")
    }
    assert not new_engine_branches


def test_resuming_reviews_against_the_original_base_commit(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """The target's own HEAD can move between the crash and the resume. Taking
    `base_sha` from there would diff the review against a commit this run never
    branched from, describing changes it never made."""
    progress = _interrupted_run(monkeypatch, fake_repo)
    original_base = progress.fields["base_sha"]

    target = git.Repo(fake_repo)
    (fake_repo / "unrelated.py").write_text("z = 3\n", encoding="utf-8")
    target.index.add(["unrelated.py"])
    target.index.commit("someone else's work, after the crash")
    assert target.head.commit.hexsha != original_base

    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")
    resumed = _Progress()

    supervisor.run(
        fake_repo,
        fake_repo,
        "add oauth2",
        progress=resumed,
        resume=supervisor.Resume(
            branch=progress.branch, integration_root=progress.integration_root
        ),
    )

    assert resumed.fields["base_sha"] == original_base


def test_resuming_a_worktree_with_no_checkpoint_is_refused(fake_repo: Path) -> None:
    with pytest.raises(ConfigError, match="Nothing to resume"):
        supervisor.run(
            fake_repo,
            fake_repo,
            "add oauth2",
            resume=supervisor.Resume(
                branch="engine/add-oauth2", integration_root=fake_repo / "nowhere"
            ),
        )


def test_resuming_the_wrong_branch_is_refused(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """Continuing one run's work on another's branch would merge new stages
    onto a history they were never written against."""
    progress = _interrupted_run(monkeypatch, fake_repo)

    with pytest.raises(ConfigError, match="refusing to continue"):
        supervisor.run(
            fake_repo,
            fake_repo,
            "add oauth2",
            resume=supervisor.Resume(
                branch="engine/something-else", integration_root=progress.integration_root
            ),
        )


def test_a_dry_run_cannot_resume(monkeypatch: pytest.MonkeyPatch, fake_repo: Path) -> None:
    """A dry run discards its worktree at the end — which on a resume is the
    worktree holding the interrupted run's work."""
    progress = _interrupted_run(monkeypatch, fake_repo)

    with pytest.raises(ConfigError, match="dry run cannot resume"):
        supervisor.run(
            fake_repo,
            fake_repo,
            "add oauth2",
            dry_run=True,
            resume=supervisor.Resume(
                branch=progress.branch, integration_root=progress.integration_root
            ),
        )

    assert progress.integration_root.exists()


def test_a_successful_run_leaves_nothing_to_resume(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")
    progress = _Progress()

    report = supervisor.run(fake_repo, fake_repo, "add oauth2", progress=progress)

    assert report.summary == "done"
    # the worktree is removed on success, and the checkpoint lives inside it,
    # so nothing can offer to resume a run that already finished
    assert not progress.integration_root.exists()
    assert checkpoint.load(progress.integration_root) is None


def test_a_resumed_stage_is_still_told_what_its_upstreams_produced(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """Stages only communicate through the description built from their
    upstreams (scheduler.build_stage_description). Skipping a stage without
    being able to describe it would hand the next agent a prompt missing
    exactly the context that stage exists to provide — which is why the
    checkpoint stores summaries and file lists, not just ids."""
    progress = _interrupted_run(monkeypatch, fake_repo)
    descriptions: dict[str, str] = {}

    def fake_run(task: AgentTask) -> ProviderResult:
        if task.agent == "reviewer":
            return ProviderResult(success=True, summary="VERDICT: PASS")
        descriptions[task.agent] = task.description
        _write_compliant_artifact(task)
        return ProviderResult(success=True, summary=f"{task.agent} done")

    _patch_provider(monkeypatch, fake_run)
    _patch_tests(monkeypatch, passed=True, output="ok")

    supervisor.run(
        fake_repo,
        fake_repo,
        "add oauth2",
        resume=supervisor.Resume(
            branch=progress.branch, integration_root=progress.integration_root
        ),
    )

    tests_prompt = descriptions["tests"]
    assert "backend done" in tests_prompt  # the skipped stage's own summary
    assert "backend.py" in tests_prompt  # and what it changed


# --- per-project action policy (issue #25) ---


def _project(actions: tuple[str, ...]) -> registry.Project:
    return registry.Project(id="mine", path=Path("/unused"), allowed_actions=actions)


def test_a_project_that_does_not_permit_tests_does_not_run_them(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """Executing a target's own declared command is arbitrary code execution on
    this machine, so it is a separate grant from "may be modified"."""
    _patch_provider(monkeypatch, _multi_stage_run())
    ran: list = []
    monkeypatch.setattr(
        test_runner,
        "run_tests",
        lambda root, config: ran.append(1) or test_runner.TestResult(passed=True, output="ok"),
    )

    report = supervisor.run(
        fake_repo, fake_repo, "add oauth2", project=_project(("inspect", "modify"))
    )

    assert ran == []
    assert "does not permit" in report.tests_output
    assert report.summary == "done"  # withheld, not failed


def test_a_project_that_permits_tests_still_runs_them(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    _patch_provider(monkeypatch, _multi_stage_run())
    ran: list = []
    monkeypatch.setattr(
        test_runner,
        "run_tests",
        lambda root, config: ran.append(1) or test_runner.TestResult(passed=True, output="ok"),
    )

    supervisor.run(
        fake_repo, fake_repo, "add oauth2", project=_project(("inspect", "modify", "test"))
    )

    assert ran


def test_the_project_policy_is_recorded_on_the_run(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """config/projects.yaml can be edited afterwards, so what a run was allowed
    to do has to be recorded with the run, not looked up later."""
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")

    supervisor.run(
        fake_repo, fake_repo, "add oauth2", project=_project(("inspect", "modify"))
    )

    with telemetry.connect(fake_repo) as con:
        metadata = con.execute("SELECT metadata FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0]
    recorded = json.loads(metadata)
    assert recorded["project_id"] == "mine"
    assert recorded["project_allowed_actions"] == "inspect,modify"


def test_a_run_with_no_project_records_no_project_policy(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """`--repo` is a different trust context and has no registry entry. An
    invented one would claim a policy nobody declared."""
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")

    supervisor.run(fake_repo, fake_repo, "add oauth2")

    with telemetry.connect(fake_repo) as con:
        metadata = con.execute("SELECT metadata FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert "project_id" not in json.loads(metadata)


# --- hard budgets (issue #27) ---


def _budgeted(fake_repo: Path, body: str) -> None:
    (fake_repo / "config" / "platform.yaml").write_text(
        PLATFORM_YAML + body, encoding="utf-8"
    )


def test_a_soft_budget_never_blocks_a_run(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """The interactive default. Refusing to run is worse than running
    expensively for someone sitting at a terminal."""
    _budgeted(fake_repo, "budgets:\n  mode: soft\n  classes:\n    standard: {max_run_tokens: 1}\n")
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, fake_repo, "add oauth2", project=_project(("modify", "test")))

    assert report.summary == "done"


def test_a_strict_budget_stops_the_run_before_the_call(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """The point of a hard limit: the run stops rather than overrunning, and
    the refusal names which limit and by how much."""
    _budgeted(fake_repo, "budgets:\n  mode: strict\n  classes:\n    standard: {max_run_tokens: 1}\n")
    called: list = []
    _patch_provider(monkeypatch, lambda task: called.append(task.agent) or _multi_stage_run()(task))
    _patch_tests(monkeypatch, passed=True, output="ok")

    with pytest.raises(budget.BudgetExceeded) as caught:
        supervisor.run(fake_repo, fake_repo, "add oauth2", project=_project(("modify", "test")))

    assert called == []  # refused before any provider was reached
    assert caught.value.decision.limit == "max_run_tokens"
    assert caught.value.decision.ceiling == 1


def test_a_run_with_no_declared_budget_is_ungated(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, fake_repo, "add oauth2")

    assert report.summary == "done"
    assert report.budget.limit == 0


def test_a_run_reports_reserved_consumed_and_remaining(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    _budgeted(
        fake_repo,
        "budgets:\n  mode: soft\n  classes:\n    standard: {max_run_tokens: 100000000}\n",
    )
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, fake_repo, "add oauth2", project=_project(("modify", "test")))

    assert report.budget.calls > 0
    assert report.budget.reserved > 0
    assert report.budget.limit == 100000000
    assert report.budget.remaining > 0


def test_every_provider_call_in_a_run_is_reserved(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """"No adapter can bypass the common budget gate" — checked by counting
    reservations against calls actually made, not by reading the code."""
    _budgeted(
        fake_repo,
        "budgets:\n  mode: soft\n  classes:\n    standard: {max_run_tokens: 100000000}\n",
    )
    calls: list = []
    _patch_provider(monkeypatch, lambda task: calls.append(task.agent) or _multi_stage_run()(task))
    _patch_tests(monkeypatch, passed=True, output="ok")

    report = supervisor.run(fake_repo, fake_repo, "add oauth2", project=_project(("modify", "test")))

    assert report.budget.calls == len(calls)


def test_a_project_selects_its_own_budget_class(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """The allowlist says which budget a repository belongs to; the amounts
    live with the rest of the tuning policy."""
    _budgeted(
        fake_repo,
        "budgets:\n  mode: soft\n  classes:\n"
        "    standard: {max_run_tokens: 111}\n    generous: {max_run_tokens: 999999999}\n",
    )
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")
    project = registry.Project(
        id="mine", path=Path("/unused"), allowed_actions=("modify", "test"), budget_class="generous"
    )

    report = supervisor.run(fake_repo, fake_repo, "add oauth2", project=project)

    assert report.budget.limit == 999999999


def test_cancelling_mid_stage_unwinds_the_run_instead_of_failing_the_stage(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """A cancellation is not a stage that went wrong.

    `CancellationRequested` was an `Exception`, so the broad handlers that turn
    a stage's problems into a failed `StageResult` swallowed it: the request
    was recorded as an error in the very work it was cancelling, and the run
    carried on to the next stage.
    """
    cancel_event = threading.Event()

    def fake_run(task: AgentTask) -> ProviderResult:
        cancel_event.set()  # the watcher would set this from the job row
        _write_compliant_artifact(task)
        return ProviderResult(success=True, summary="partial work")

    _patch_provider(monkeypatch, fake_run)
    repo = git.Repo(fake_repo)
    integration_root, branch = git_ops.create_integration_worktree(repo, "add oauth2")

    # The stage function itself, because that is where the swallowing was: run()
    # would raise anyway from its next `check_cancel`, which hid the fact that
    # the stage had already been written down as a failure on the way there.
    with pytest.raises(store.CancellationRequested):
        supervisor._run_stage_in_worktree(
            integration_root,
            fake_repo,
            branch,
            planner.Task(id="backend", agent="backend", depends_on=[]),
            "add oauth2",
            SelectedContext(chunks=[]),
            [],
            target_config.TargetConfig(),
            platform_config.load(fake_repo),
            budget.Limits(),
            run_key="run-1",
            cancel_event=cancel_event,
        )


def test_a_cancelled_run_leaves_no_worktree_behind(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """Cancellation can unwind from half a dozen points; cleanup written at one
    of them covered one of them. What was left was the integration worktree,
    its task worktrees and the branch, under a job reporting itself stopped."""
    cancel_event = threading.Event()

    def fake_run(task: AgentTask) -> ProviderResult:
        cancel_event.set()
        _write_compliant_artifact(task)
        return ProviderResult(success=True, summary="partial work")

    _patch_provider(monkeypatch, fake_run)
    _patch_tests(monkeypatch, passed=True)

    with pytest.raises(store.CancellationRequested):
        supervisor.run(fake_repo, fake_repo, "add oauth2", cancel_event=cancel_event)

    listed = git.Repo(fake_repo).git.worktree("list", "--porcelain")
    extra = [
        line.split(" ", 1)[1]
        for line in listed.splitlines()
        if line.startswith("worktree ") and Path(line.split(" ", 1)[1]) != fake_repo
    ]
    assert extra == [], f"worktrees survived cancellation: {extra}"
