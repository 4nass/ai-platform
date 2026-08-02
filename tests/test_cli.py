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
        branch="engine/test",
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
        "core.orchestrator.supervisor.run",
        lambda engine_root, target_root, request, dry_run=False, session_id=None, dirty_policy="head", **admission: _report("done"),
    )

    result = runner.invoke(ai_platform.app, ["run", "add a thing"])

    assert result.exit_code == 0


def test_cli_exits_nonzero_when_summary_needs_attention(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "core.orchestrator.supervisor.run",
        lambda engine_root, target_root, request, dry_run=False, session_id=None, dirty_policy="head", **admission: _report("needs attention"),
    )

    result = runner.invoke(ai_platform.app, ["run", "add a thing"])

    assert result.exit_code == 1


def test_cli_dry_run_passes_flag_through_and_ignores_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(engine_root, target_root, request, dry_run=False, session_id=None, dirty_policy="head", **admission):
        captured["dry_run"] = dry_run
        return _report("needs attention")

    monkeypatch.setattr("core.orchestrator.supervisor.run", fake_run)

    result = runner.invoke(ai_platform.app, ["run", "add a thing", "--dry-run"])

    assert result.exit_code == 0
    assert captured["dry_run"] is True


def test_cli_run_passes_the_repo_flag_through_as_target_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    captured: dict = {}

    def fake_run(engine_root, target_root, request, dry_run=False, session_id=None, dirty_policy="head", **admission):
        captured["engine_root"] = engine_root
        captured["target_root"] = target_root
        return _report("done")

    monkeypatch.setattr("core.orchestrator.supervisor.run", fake_run)

    result = runner.invoke(ai_platform.app, ["run", "add a thing", "--repo", str(tmp_path)])

    assert result.exit_code == 0
    assert captured["target_root"] == tmp_path.resolve()
    assert captured["engine_root"] == ai_platform.ENGINE_ROOT
    assert captured["engine_root"] != captured["target_root"]


def test_cli_defaults_the_dirty_policy_to_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """Working on the base commit is what happens when nobody asks for
    anything: the flag exists to *opt out* of the default, not to enable it."""
    captured: dict = {}

    def fake_run(engine_root, target_root, request, dry_run=False, session_id=None, dirty_policy="x", **admission):
        captured["dirty_policy"] = dirty_policy
        return _report("done")

    monkeypatch.setattr("core.orchestrator.supervisor.run", fake_run)

    result = runner.invoke(ai_platform.app, ["run", "add a thing"])

    assert result.exit_code == 0
    assert captured["dirty_policy"] == "head"


def test_cli_passes_the_dirty_policy_through(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(engine_root, target_root, request, dry_run=False, session_id=None, dirty_policy="head", **admission):
        captured["dirty_policy"] = dirty_policy
        return _report("done")

    monkeypatch.setattr("core.orchestrator.supervisor.run", fake_run)

    result = runner.invoke(ai_platform.app, ["run", "add a thing", "--dirty-policy", "reject"])

    assert result.exit_code == 0
    assert captured["dirty_policy"] == "reject"


def test_cli_exits_nonzero_and_prints_clean_error_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_error(engine_root, target_root, request, dry_run=False, session_id=None, dirty_policy="head", **admission):
        raise RuntimeError("boom")

    monkeypatch.setattr("core.orchestrator.supervisor.run", raise_error)

    result = runner.invoke(ai_platform.app, ["run", "add a thing"])

    assert result.exit_code == 1
    assert "boom" in result.stdout
    assert "Traceback" not in result.stdout


class _FakeContextManager:
    """Stands in for the real one so the CLI test needs no index or graph."""

    def __init__(self, repo_root, *, engine_root=None) -> None:
        self.repo_root = repo_root
        self.engine_root = engine_root

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


def test_quota_command_shows_consumption_against_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "core.telemetry.quota.pressure",
        lambda repo_root, budgets=None, window_hours=None: [
            {
                "provider": "codex_cli", "calls": 3, "input_tokens": 1000, "output_tokens": 50,
                "total_tokens": 1050, "success_rate": 1.0, "avg_duration_ms": 6000,
                "window_hours": 5.0, "budget_tokens": 2000, "used_ratio": 0.525,
            }
        ],
    )

    result = runner.invoke(ai_platform.app, ["quota"])

    assert result.exit_code == 0
    assert "codex_cli" in result.stdout
    assert "52.5%" in result.stdout


def test_quota_command_handles_an_undeclared_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider with usage but no declared plan limit reports consumption
    without a percentage rather than a misleading 0%."""
    monkeypatch.setattr(
        "core.telemetry.quota.pressure",
        lambda repo_root, budgets=None, window_hours=None: [
            {
                "provider": "codex_cli", "calls": 1, "input_tokens": 10, "output_tokens": 1,
                "total_tokens": 11, "success_rate": None, "avg_duration_ms": None,
                "window_hours": 5.0, "budget_tokens": None, "used_ratio": None,
            }
        ],
    )

    result = runner.invoke(ai_platform.app, ["quota"])

    assert result.exit_code == 0
    assert "%" not in result.stdout


def test_quota_command_with_nothing_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.telemetry.quota.pressure", lambda repo_root, budgets=None, window_hours=None: [])

    result = runner.invoke(ai_platform.app, ["quota"])

    assert result.exit_code == 0
    assert "No provider usage recorded" in result.stdout


def test_config_command_shows_the_resolved_platform_policy(tmp_path) -> None:
    """Against the real shipped config/platform.yaml + presets, since this
    command's whole point is answering "which preset am I on" -- a fake would
    just prove the table renders, not that it reflects reality."""
    result = runner.invoke(ai_platform.app, ["config"])

    assert result.exit_code == 0
    assert "balanced" in result.stdout  # the default profile
    assert "standard" in result.stdout  # the default workflow mode
    assert "smart" in result.stdout  # the default context mode
    assert "codex_cli" in result.stdout  # a declared quota


# --- durable job lifecycle (issue #24) ---


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Points the CLI's ENGINE_ROOT at a scratch directory so these tests use
    a throwaway job database rather than the developer's real one."""
    monkeypatch.setattr(ai_platform, "ENGINE_ROOT", tmp_path)
    return tmp_path


def test_cli_submit_queues_without_running_anything(
    monkeypatch: pytest.MonkeyPatch, engine, tmp_path
) -> None:
    """The acceptance criterion that defines the command: an id comes back
    before any provider is contacted."""
    from core.jobs import store, worker

    spawned: list[int] = []
    monkeypatch.setattr(worker, "spawn_detached", lambda root, job_id: spawned.append(job_id) or 999)
    monkeypatch.setattr(
        "core.orchestrator.supervisor.run",
        lambda *a, **k: pytest.fail("submit must not run the supervisor"),
    )

    result = runner.invoke(ai_platform.app, ["submit", "add oauth2", "--repo", str(tmp_path)])

    assert result.exit_code == 0
    jobs = store.recent(engine)
    assert len(jobs) == 1
    assert jobs[0].state == "queued"
    assert jobs[0].request == "add oauth2"
    assert spawned == [jobs[0].id]
    assert f"Job {jobs[0].id}" in result.stdout


def test_cli_submit_can_queue_without_starting_a_worker(
    monkeypatch: pytest.MonkeyPatch, engine, tmp_path
) -> None:
    from core.jobs import store, worker

    monkeypatch.setattr(
        worker, "spawn_detached", lambda *a: pytest.fail("--no-detach must not spawn")
    )

    result = runner.invoke(
        ai_platform.app, ["submit", "add oauth2", "--repo", str(tmp_path), "--no-detach"]
    )

    assert result.exit_code == 0
    assert store.recent(engine)[0].state == "queued"


def test_cli_submit_carries_the_envelope(monkeypatch: pytest.MonkeyPatch, engine, tmp_path) -> None:
    from core.jobs import store, worker

    monkeypatch.setattr(worker, "spawn_detached", lambda *a: 1)

    runner.invoke(
        ai_platform.app,
        [
            "submit", "add oauth2", "--repo", str(tmp_path),
            "--session", "s1", "--dirty-policy", "reject", "--no-detach",
        ],
    )

    assert store.recent(engine)[0].envelope == {
        "session_id": "s1",
        "dirty_policy": "reject",
        # submitted by path, so no allowlisted id to re-check at claim time
        "project_id": None,
    }


def test_cli_status_reads_a_job_submitted_elsewhere(engine, tmp_path) -> None:
    """Status is answerable from any process — the submitting terminal is
    long gone by the time anyone asks."""
    from core.jobs import store

    job_id = store.submit(engine, project=str(tmp_path), request="add oauth2")
    store.claim(engine, job_id, worker_pid=4242)
    store.record_progress(engine, job_id, branch="engine/x", base_sha="abc123def456", stage="backend")

    result = runner.invoke(ai_platform.app, ["status", str(job_id)])

    assert result.exit_code == 0
    assert "running" in result.stdout
    assert "engine/x" in result.stdout
    assert "backend" in result.stdout
    assert "queued" in result.stdout  # the lifecycle table shows how it got here


def test_cli_status_on_an_unknown_job_exits_nonzero(engine) -> None:
    result = runner.invoke(ai_platform.app, ["status", "404"])

    assert result.exit_code == 1
    assert "No job 404" in result.stdout


def test_cli_jobs_lists_what_was_submitted(engine, tmp_path) -> None:
    from core.jobs import store

    store.submit(engine, project=str(tmp_path), request="first request")
    store.submit(engine, project=str(tmp_path), request="second request")

    result = runner.invoke(ai_platform.app, ["jobs"])

    assert result.exit_code == 0
    assert "first request" in result.stdout
    assert "second request" in result.stdout


def test_cli_jobs_reports_nothing_when_the_queue_is_empty(engine) -> None:
    result = runner.invoke(ai_platform.app, ["jobs"])

    assert result.exit_code == 0
    assert "No jobs submitted yet" in result.stdout


def test_cli_cancel_stops_a_queued_job(engine, tmp_path) -> None:
    from core.jobs import store

    job_id = store.submit(engine, project=str(tmp_path), request="add oauth2")

    result = runner.invoke(ai_platform.app, ["cancel", str(job_id)])

    assert result.exit_code == 0
    assert store.get(engine, job_id).state == "cancelled"


def test_cli_cancel_refuses_a_running_job(engine, tmp_path) -> None:
    from core.jobs import store

    job_id = store.submit(engine, project=str(tmp_path), request="add oauth2")
    store.claim(engine, job_id, worker_pid=1)

    result = runner.invoke(ai_platform.app, ["cancel", str(job_id)])

    assert result.exit_code == 1
    assert "cannot be stopped mid-run" in result.stdout
    assert store.get(engine, job_id).state == "running"


def test_cli_reconciles_abandoned_jobs_on_a_read(engine, tmp_path) -> None:
    """A restart is exactly when nobody is around to run a repair command, so
    the read paths do it."""
    from datetime import datetime, timedelta, timezone

    from core.jobs import store

    job_id = store.submit(engine, project=str(tmp_path), request="add oauth2")
    store.claim(engine, job_id, worker_pid=1)
    stale = (datetime.now(timezone.utc) - timedelta(seconds=store.STALE_AFTER_SECONDS + 60)).isoformat()
    with store.connect(engine) as con:
        con.execute("UPDATE jobs SET heartbeat_at = ? WHERE id = ?", (stale, job_id))

    result = runner.invoke(ai_platform.app, ["jobs"])

    assert result.exit_code == 0
    assert "marked interrupted" in result.stdout
    assert store.get(engine, job_id).state == "interrupted"


def test_cli_work_runs_a_specific_job(monkeypatch: pytest.MonkeyPatch, engine, tmp_path) -> None:
    from core.jobs import store, worker

    job_id = store.submit(engine, project=str(tmp_path), request="add oauth2")
    monkeypatch.setattr(worker, "run_job", lambda root, jid: "succeeded")

    result = runner.invoke(ai_platform.app, ["work", "--job", str(job_id)])

    assert result.exit_code == 0
    assert "succeeded" in result.stdout


def test_cli_work_on_an_empty_queue_says_so(engine) -> None:
    result = runner.invoke(ai_platform.app, ["work"])

    assert result.exit_code == 0
    assert "Nothing queued" in result.stdout


def _interrupted(engine, project) -> int:
    from core.jobs import store

    job_id = store.submit(engine, project=str(project), request="add oauth2")
    store.claim(engine, job_id, worker_pid=4242)
    store.record_progress(engine, job_id, branch="engine/add-oauth2")
    store.transition(engine, job_id, store.INTERRUPTED, note="worker presumed gone")
    return job_id


def test_cli_resume_requeues_an_interrupted_job(
    monkeypatch: pytest.MonkeyPatch, engine, tmp_path
) -> None:
    from core.jobs import store, worker

    monkeypatch.setattr(worker, "spawn_detached", lambda *a: 4321)
    job_id = _interrupted(engine, tmp_path)

    result = runner.invoke(ai_platform.app, ["resume", str(job_id)])

    assert result.exit_code == 0
    assert store.get(engine, job_id).state == "queued"
    assert "Worker started" in result.stdout


def test_cli_resume_says_when_there_is_nothing_merged_to_keep(
    monkeypatch: pytest.MonkeyPatch, engine, tmp_path
) -> None:
    """A job interrupted before it landed a stage will start the workflow over.
    Saying so first is the difference between an informed resume and a
    surprise bill."""
    from core.jobs import worker

    monkeypatch.setattr(worker, "spawn_detached", lambda *a: 1)

    result = runner.invoke(ai_platform.app, ["resume", str(_interrupted(engine, tmp_path))])

    assert "from the start" in result.stdout


def test_cli_resume_lists_the_stages_it_will_skip(
    monkeypatch: pytest.MonkeyPatch, engine, tmp_path
) -> None:
    from core.jobs import store, worker
    from core.orchestrator import checkpoint

    monkeypatch.setattr(worker, "spawn_detached", lambda *a: 1)
    monkeypatch.setattr(
        checkpoint,
        "load",
        lambda _: checkpoint.Checkpoint(
            base_sha="abc",
            branch="engine/add-oauth2",
            request="add oauth2",
            complexity="complex",
            task_ids=["architecture", "backend"],
            stages=[checkpoint.StageRecord(id="architecture", agent="architect")],
        ),
    )
    job_id = _interrupted(engine, tmp_path)
    store.record_progress(engine, job_id, integration_root="/tmp/engine-run-x")

    result = runner.invoke(ai_platform.app, ["resume", str(job_id)])

    assert "Skipping 1 stage(s) already merged: architecture" in result.stdout


def test_cli_resume_refuses_a_job_that_reached_a_verdict(engine, tmp_path) -> None:
    from core.jobs import store

    job_id = store.submit(engine, project=str(tmp_path), request="add oauth2")
    store.claim(engine, job_id, worker_pid=1)
    store.transition(engine, job_id, store.FAILED)

    result = runner.invoke(ai_platform.app, ["resume", str(job_id)])

    assert result.exit_code == 1
    assert "not interrupted" in result.stdout
    assert store.get(engine, job_id).state == "failed"


def test_cli_status_points_an_interrupted_job_at_resume(engine, tmp_path) -> None:
    result = runner.invoke(ai_platform.app, ["status", str(_interrupted(engine, tmp_path))])

    assert result.exit_code == 0
    assert "ai-platform resume" in result.stdout


# --- admission by project id (issue #25) ---


def _registry(engine, tmp_path, *, actions="[inspect, modify, test]") -> Path:
    """An allowlisted project, in the throwaway engine the `engine` fixture
    points ENGINE_ROOT at."""
    import git

    target = tmp_path / "roots" / "mine"
    target.mkdir(parents=True)
    repo = git.Repo.init(target)
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Test")
        writer.set_value("user", "email", "test@example.com")
    (target / "f.txt").write_text("x", encoding="utf-8")
    repo.index.add(["f.txt"])
    repo.index.commit("initial")

    (engine / "config").mkdir(exist_ok=True)
    (engine / "config" / "projects.yaml").write_text(
        f"roots: [{tmp_path / 'roots'}]\nprojects:\n  mine:\n    path: {target}\n"
        f"    allowed_actions: {actions}\n",
        encoding="utf-8",
    )
    return target


def test_cli_run_resolves_a_project_id_to_its_allowlisted_path(
    monkeypatch: pytest.MonkeyPatch, engine, tmp_path
) -> None:
    target = _registry(engine, tmp_path)
    captured: dict = {}

    def fake_run(engine_root, target_root, request, **kwargs):
        captured["target"] = target_root
        captured["project"] = kwargs.get("project")
        return _report("done")

    monkeypatch.setattr("core.orchestrator.supervisor.run", fake_run)

    result = runner.invoke(ai_platform.app, ["run", "add a thing", "--project", "mine"])

    assert result.exit_code == 0
    assert captured["target"] == target.resolve()
    assert captured["project"].id == "mine"


def test_cli_run_refuses_an_unknown_project_without_calling_the_supervisor(
    monkeypatch: pytest.MonkeyPatch, engine, tmp_path
) -> None:
    """"Fail before context indexing or provider selection" is the criterion —
    so the supervisor must not be reached at all."""
    _registry(engine, tmp_path)
    called: list = []
    monkeypatch.setattr(
        "core.orchestrator.supervisor.run", lambda *a, **k: called.append(1)
    )

    result = runner.invoke(ai_platform.app, ["run", "add a thing", "--project", "theirs"])

    assert result.exit_code == 1
    assert "Refused" in result.stdout
    assert called == []


def test_cli_run_refuses_a_project_that_may_only_be_inspected(
    monkeypatch: pytest.MonkeyPatch, engine, tmp_path
) -> None:
    _registry(engine, tmp_path, actions="[inspect]")
    called: list = []
    monkeypatch.setattr(
        "core.orchestrator.supervisor.run", lambda *a, **k: called.append(1)
    )

    result = runner.invoke(ai_platform.app, ["run", "add a thing", "--project", "mine"])

    assert result.exit_code == 1
    assert "does not permit 'modify'" in result.stdout
    assert called == []


def test_cli_dry_run_only_needs_inspect(
    monkeypatch: pytest.MonkeyPatch, engine, tmp_path
) -> None:
    """A read-only project must still be inspectable — a dry run writes
    nothing, so requiring `modify` for it would make the grant meaningless."""
    _registry(engine, tmp_path, actions="[inspect]")
    monkeypatch.setattr("core.orchestrator.supervisor.run", lambda *a, **k: _report("dry-run"))

    result = runner.invoke(
        ai_platform.app, ["run", "add a thing", "--project", "mine", "--dry-run"]
    )

    assert result.exit_code == 0


def test_cli_refuses_a_project_and_a_repo_together(engine, tmp_path) -> None:
    _registry(engine, tmp_path)

    result = runner.invoke(
        ai_platform.app, ["run", "x", "--project", "mine", "--repo", str(tmp_path)]
    )

    assert result.exit_code == 1
    assert "name the same thing two ways" in result.stdout


def test_cli_submit_records_the_project_id_for_the_worker_to_recheck(
    monkeypatch: pytest.MonkeyPatch, engine, tmp_path
) -> None:
    """The id, not only the resolved path: a queued job can execute hours
    later, and the allowlist has to be checked then too."""
    from core.jobs import store, worker

    target = _registry(engine, tmp_path)
    monkeypatch.setattr(worker, "spawn_detached", lambda *a: 1)

    result = runner.invoke(ai_platform.app, ["submit", "add oauth2", "--project", "mine"])

    assert result.exit_code == 0
    job = store.recent(engine)[0]
    assert job.envelope["project_id"] == "mine"
    assert job.project == str(target.resolve())
