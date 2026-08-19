from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest
import git

from core.actions.executor import (
    ActionError,
    ActionNotFound,
    ActionPolicyError,
    GitPushPlan,
    ActionExecutor,
    ActionReplayError,
    ActionResult,
    CleanupResult,
    FAILED,
    GIT_PUSH,
    OPEN_PR,
    PreviewDeployPlan,
    OpenPRPlan,
    SUCCEEDED,
    WAITING_APPROVAL,
    RUNNING,
    CANCELLED,
    DENIED,
)
from core.jobs import approvals
from core.orchestrator.registry import Project, INSPECT, MODIFY


def project(*, allowed=(OPEN_PR,), approval_required=()):
    return Project(
        id="demo", path=Path("/tmp/demo"),
        allowed_actions=tuple(allowed),
        approval_required=tuple(approval_required),
    )


class Handler:
    def __init__(self, *, ok=True, raises=False):
        self.ok, self.raises = ok, raises
        self.calls = 0
        self.cleanups = 0
        self.seen_credentials = None

    def execute(self, plan, context):
        self.calls += 1
        self.seen_credentials = context.credentials
        if self.raises:
            raise RuntimeError("provider output contains secret=do-not-persist")
        return ActionResult(self.ok, "fake", "provider completed", "external-1")

    def cleanup(self, plan, context):
        self.cleanups += 1
        return CleanupResult(True, "cleaned")


def plan(*, commit="a" * 40, body="safe"):
    return OpenPRPlan(
        project_id="demo", branch="engine/demo", commit_sha=commit,
        base_branch="main", title="Demo PR", body=body,
    )


def test_automatic_execution_is_replayed_without_second_provider_call(tmp_path: Path):
    handler = Handler()
    executor = ActionExecutor(
        tmp_path, {OPEN_PR: handler},
        credential_provider=type("Credentials", (), {"get": lambda self, *_: "secret-value"})(),
    )
    first = executor.execute(plan(), project=project(), principal="openclaw:owner", request_id="req-1")
    second = executor.execute(plan(), project=project(), principal="openclaw:owner", request_id="req-1")
    assert first.state == SUCCEEDED
    assert second.id == first.id
    assert handler.calls == 1
    with sqlite3.connect(tmp_path / "jobs.sqlite") as con:
        raw = con.execute("SELECT plan, summary FROM action_executions").fetchone()
    assert "secret-value" not in repr(raw)


def test_policy_denial_is_audited_and_never_calls_provider(tmp_path: Path):
    handler = Handler()
    executor = ActionExecutor(tmp_path, {OPEN_PR: handler})
    result = executor.execute(
        plan(), project=project(allowed=(INSPECT,)), principal="owner", request_id="deny-1"
    )
    assert result.state == DENIED
    assert handler.calls == 0
    assert any(e["event"] == "refused.policy" for e in executor.events(result.id, principal="owner"))


def test_approval_is_consumed_only_for_the_exact_plan(tmp_path: Path):
    handler = Handler()
    executor = ActionExecutor(tmp_path, {OPEN_PR: handler})
    waiting = executor.execute(
        plan(), project=project(approval_required=(OPEN_PR,)),
        principal="owner", request_id="approval-1",
    )
    assert waiting.state == WAITING_APPROVAL
    approval = approvals.get(tmp_path, waiting.approval_id)
    approvals.decide(tmp_path, approval.id, approved=True, principal="owner")
    changed = plan(commit="b" * 40)
    with pytest.raises(ActionReplayError):
        executor.execute(
            changed, project=project(approval_required=(OPEN_PR,)),
            principal="owner", request_id="approval-1", approval_id=approval.id,
        )
    done = executor.execute(
        plan(), project=project(approval_required=(OPEN_PR,)),
        principal="owner", request_id="approval-1", approval_id=approval.id,
    )
    assert done.state == SUCCEEDED
    assert handler.calls == 1
    assert approvals.get(tmp_path, approval.id).state == approvals.CONSUMED


def test_reused_approval_with_new_request_is_rejected_by_fingerprint(tmp_path: Path):
    handler = Handler()
    executor = ActionExecutor(tmp_path, {OPEN_PR: handler})
    waiting = executor.execute(
        plan(), project=project(approval_required=(OPEN_PR,)),
        principal="owner", request_id="approval-original",
    )
    approval = approvals.get(tmp_path, waiting.approval_id)
    approvals.decide(tmp_path, approval.id, approved=True, principal="owner")

    changed = executor.execute(
        plan(commit="b" * 40), project=project(approval_required=(OPEN_PR,)),
        principal="owner", request_id="approval-changed", approval_id=approval.id,
    )
    assert changed.state == DENIED
    assert handler.calls == 0
    assert approvals.get(tmp_path, approval.id).state == approvals.APPROVED
    assert any(e["event"] == "approval.refused" for e in executor.events(changed.id, principal="owner"))


def test_expired_approval_is_terminal_and_never_calls_provider(tmp_path: Path):
    handler = Handler()
    executor = ActionExecutor(tmp_path, {OPEN_PR: handler})
    waiting = executor.execute(
        plan(), project=project(approval_required=(OPEN_PR,)),
        principal="owner", request_id="expiry-1",
    )
    approval = approvals.get(tmp_path, waiting.approval_id)
    approvals.decide(tmp_path, approval.id, approved=True, principal="owner")
    with sqlite3.connect(tmp_path / "jobs.sqlite") as con:
        con.execute("UPDATE approvals SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                    (approval.id,))
    result = executor.execute(
        plan(), project=project(approval_required=(OPEN_PR,)),
        principal="owner", request_id="expiry-1", approval_id=approval.id,
    )
    assert result.state == "expired"
    assert handler.calls == 0


def test_provider_failure_is_audited_and_cleanup_is_attempted(tmp_path: Path):
    handler = Handler(raises=True)
    executor = ActionExecutor(tmp_path, {OPEN_PR: handler})
    result = executor.execute(plan(), project=project(), principal="owner", request_id="failure-1")
    assert result.state == FAILED
    assert handler.cleanups == 1
    events = [e["event"] for e in executor.events(result.id, principal="owner")]
    assert "provider.failure" in events
    assert "cleanup.result" in events
    assert "do-not-persist" not in json.dumps(events)


def test_cancelled_approval_does_not_consume_or_execute(tmp_path: Path):
    handler = Handler()
    executor = ActionExecutor(tmp_path, {OPEN_PR: handler})
    waiting = executor.execute(
        plan(), project=project(approval_required=(OPEN_PR,)),
        principal="owner", request_id="cancel-1",
    )
    cancelled = executor.cancel(waiting.id, principal="owner")
    assert cancelled.state == CANCELLED
    approval = approvals.get(tmp_path, waiting.approval_id)
    approvals.decide(tmp_path, approval.id, approved=True, principal="owner")
    result = executor.execute(
        plan(), project=project(approval_required=(OPEN_PR,)),
        principal="owner", request_id="cancel-1", approval_id=approval.id,
    )
    assert result.state == CANCELLED
    assert handler.calls == 0

def test_typed_git_push_rejects_commands_and_unregistered_remotes(tmp_path: Path):
    with pytest.raises(ActionPolicyError, match="fixed"):
        GitPushPlan(
            project_id="demo", branch="engine/demo", commit_sha="a" * 40,
            base_sha="b" * 40, remote_name="origin", operation="git push; rm -rf /",
        )
    executor = ActionExecutor(tmp_path)
    with pytest.raises(ActionPolicyError, match="remote"):
        executor.execute(
            GitPushPlan(
                project_id="demo", branch="engine/demo", commit_sha="a" * 40,
                base_sha="b" * 40, remote_name="origin",
                remote_url="https://evil.example/repo.git",
            ),
            project=Project(
                id="demo", path=Path("/tmp/demo"), remote="https://safe.example/repo.git",
                allowed_actions=("git_push",),
            ),
            principal="owner", request_id="remote-mismatch",
        )

def test_git_push_handler_revalidates_pinned_remote_base(tmp_path: Path):
    remote = git.Repo.init(tmp_path / "remote.git", bare=True)
    repo = git.Repo.init(tmp_path / "repo")
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "test")
        writer.set_value("user", "email", "test@example.invalid")
    (tmp_path / "repo" / "file.txt").write_text("one", encoding="utf-8")
    repo.index.add(["file.txt"])
    base_commit = repo.index.commit("base")
    repo.create_remote("origin", str(tmp_path / "remote.git"))
    repo.git.push("origin", "HEAD:refs/heads/main")
    delivery = repo.create_head("engine/demo", base_commit)
    (tmp_path / "repo" / "file.txt").write_text("two", encoding="utf-8")
    repo.index.add(["file.txt"])
    delivery.commit = repo.index.commit("delivery")
    project = Project(
        id="demo", path=tmp_path / "repo", remote=str(tmp_path / "remote.git"),
        base_branch="main", allowed_actions=(GIT_PUSH,),
    )
    executor = ActionExecutor(tmp_path)
    result = executor.execute(
        GitPushPlan(
            project_id="demo", branch="engine/demo",
            commit_sha=delivery.commit.hexsha, base_sha=base_commit.hexsha,
            remote_name="origin", remote_url=str(tmp_path / "remote.git"),
            base_branch="main", remote_sha=base_commit.hexsha,
        ),
        project=project, principal="owner", request_id="git-push-1",
    )
    assert result.state == SUCCEEDED
    assert remote.commit("refs/heads/engine/demo").hexsha == delivery.commit.hexsha

def test_concurrent_request_id_submissions_share_one_execution(tmp_path: Path, monkeypatch):
    handler = Handler()
    executor = ActionExecutor(tmp_path, {OPEN_PR: handler})
    original_find = executor._find_request
    barrier = threading.Barrier(2)
    calls = 0
    calls_lock = threading.Lock()

    def synchronized_find(request_id):
        nonlocal calls
        with calls_lock:
            calls += 1
            number = calls
        if number <= 2:
            barrier.wait(timeout=5)
        return original_find(request_id)

    monkeypatch.setattr(executor, "_find_request", synchronized_find)
    results, failures = [], []

    def submit():
        try:
            results.append(
                executor.execute(
                    plan(), project=project(), principal="owner", request_id="racing-request"
                )
            )
        except Exception as exc:
            failures.append(exc)

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not failures
    assert len(results) == 2
    assert len({result.id for result in results}) == 1
    assert handler.calls == 1


def test_concurrent_transitions_cannot_both_leave_waiting_approval(tmp_path: Path):
    executor = ActionExecutor(tmp_path, {OPEN_PR: Handler()})
    waiting = executor.execute(
        plan(), project=project(approval_required=(OPEN_PR,)),
        principal="owner", request_id="transition-race",
    )
    barrier = threading.Barrier(2)
    outcomes = []

    def transition(target):
        barrier.wait(timeout=5)
        try:
            executor._transition(waiting.id, target)
            outcomes.append("won")
        except ActionError:
            outcomes.append("lost")

    threads = [
        threading.Thread(target=transition, args=(RUNNING,)),
        threading.Thread(target=transition, args=(DENIED,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(outcomes) == ["lost", "won"]
    assert executor.get(waiting.id, principal="owner").state in {RUNNING, DENIED}

def test_action_reads_do_not_disclose_another_principal(tmp_path: Path):
    executor = ActionExecutor(tmp_path, {OPEN_PR: Handler()})
    result = executor.execute(
        plan(), project=project(), principal="owner", request_id="owner-only-read"
    )

    with pytest.raises(ActionNotFound, match="action execution not found"):
        executor.get(result.id, principal="other")
    with pytest.raises(ActionNotFound, match="action execution not found"):
        executor.events(result.id, principal="other")
