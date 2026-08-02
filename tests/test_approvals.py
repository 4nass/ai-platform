"""Tests for core.jobs.approvals — scoped authorization (issue #28).

Every test here is a way an authorization could be stretched beyond what was
actually read and agreed to: replayed, reused for a different diff, decided by
someone else, or applied after the state it was shown against had moved.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.jobs import approvals
from core.jobs.approvals import ApprovalError
from core.orchestrator.registry import Project


@pytest.fixture
def engine(tmp_path: Path) -> Path:
    return tmp_path


def _request(engine: Path, **overrides) -> approvals.Approval:
    base = {
        "action": "open_pr",
        "target": "engine/add-oauth2",
        "detail": {"diff_sha": "abc123"},
        "requested_by": "cli:anass",
    }
    return approvals.request(engine, **{**base, **overrides})


# --- classification ---


def test_an_action_the_project_permits_outright_is_automatic() -> None:
    project = Project(id="p", path=Path("/x"), allowed_actions=("inspect", "modify"))

    assert approvals.classify(project, "modify") == approvals.AUTOMATIC


def test_an_action_the_project_withholds_is_denied_not_askable() -> None:
    """A denial is a decision already made. Offering to ask about it would
    invite someone to approve exactly what the policy exists to refuse."""
    project = Project(id="p", path=Path("/x"), allowed_actions=("inspect",))

    assert approvals.classify(project, "modify") == approvals.DENIED_BY_POLICY


def test_an_action_listed_as_approval_required_asks() -> None:
    project = Project(
        id="p",
        path=Path("/x"),
        allowed_actions=("inspect", "modify", "open_pr"),
        approval_required=("open_pr",),
    )

    assert approvals.classify(project, "open_pr") == approvals.REQUIRES_APPROVAL


def test_no_project_policy_means_automatic() -> None:
    """`--repo` is the local-owner path and has no registry entry. Inventing a
    policy for it would claim one nobody declared."""
    assert approvals.classify(None, "open_pr") == approvals.AUTOMATIC


# --- the request ---


def test_a_request_records_what_is_being_asked(engine: Path) -> None:
    approval = _request(engine)

    assert approval.state == approvals.PENDING
    assert approval.action == "open_pr"
    assert approval.target == "engine/add-oauth2"
    assert approval.detail == {"diff_sha": "abc123"}
    assert approval.token


def test_the_description_is_built_from_what_was_stored(engine: Path) -> None:
    """The thing displayed and the thing bound to the fingerprint have to be
    the same thing, or the display is theatre."""
    assert "open_pr on engine/add-oauth2" in _request(engine).describe()
    assert "diff_sha=abc123" in _request(engine).describe()


def test_tokens_are_not_guessable(engine: Path) -> None:
    tokens = {_request(engine).token for _ in range(5)}

    assert len(tokens) == 5
    assert all(len(token) > 20 for token in tokens)


def test_a_request_is_audited(engine: Path) -> None:
    approval = _request(engine)

    assert [e["event"] for e in approvals.events(engine, approval.id)] == ["requested"]


def test_pending_lists_what_is_waiting(engine: Path) -> None:
    _request(engine)
    _request(engine, action="deploy", target="staging")

    assert len(approvals.pending(engine)) == 2


# --- deciding ---


def test_approving_records_who_decided(engine: Path) -> None:
    approval = _request(engine)

    decided = approvals.decide(engine, approval.id, approved=True, principal="cli:anass")

    assert decided.state == approvals.APPROVED
    assert decided.decided_by == "cli:anass"
    assert decided.decided_at


def test_denying_is_terminal(engine: Path) -> None:
    approval = _request(engine)
    approvals.decide(engine, approval.id, approved=False, principal="cli:anass")

    with pytest.raises(ApprovalError, match="already denied"):
        approvals.decide(engine, approval.id, approved=True, principal="cli:anass")


def test_a_decision_is_made_once(engine: Path) -> None:
    approval = _request(engine)
    approvals.decide(engine, approval.id, approved=True, principal="cli:anass")

    with pytest.raises(ApprovalError, match="decisions are made once"):
        approvals.decide(engine, approval.id, approved=True, principal="cli:anass")


def test_only_the_principal_who_asked_may_decide(engine: Path) -> None:
    """Close to a formality with one owner — written now because retrofitting
    an identity check onto a flow that never had one is how it ends up missing
    when a second channel appears."""
    approval = _request(engine, requested_by="whatsapp:+33600000000")

    with pytest.raises(ApprovalError, match="can only be decided by them"):
        approvals.decide(engine, approval.id, approved=True, principal="whatsapp:+33699999999")

    assert approvals.get(engine, approval.id).state == approvals.PENDING


def test_a_refused_decision_attempt_is_audited(engine: Path) -> None:
    approval = _request(engine, requested_by="whatsapp:+33600000000")

    with pytest.raises(ApprovalError):
        approvals.decide(engine, approval.id, approved=True, principal="someone:else")

    assert any(e["event"] == "refused" for e in approvals.events(engine, approval.id))


def test_deciding_after_expiry_is_refused(engine: Path) -> None:
    approval = _request(engine, ttl_seconds=-1)

    with pytest.raises(ApprovalError, match="expired"):
        approvals.decide(engine, approval.id, approved=True, principal="cli:anass")

    assert approvals.get(engine, approval.id).state == approvals.EXPIRED


# --- consuming: single-use, and bound to the inputs shown ---


def _approved(engine: Path, **overrides) -> approvals.Approval:
    approval = _request(engine, **overrides)
    return approvals.decide(engine, approval.id, approved=True, principal="cli:anass")


def test_an_approved_action_can_be_performed_once(engine: Path) -> None:
    approval = _approved(engine)

    consumed = approvals.consume(
        engine, approval.id, action="open_pr", target="engine/add-oauth2",
        detail={"diff_sha": "abc123"},
    )

    assert consumed.state == approvals.CONSUMED


def test_an_approval_cannot_be_used_twice(engine: Path) -> None:
    """Single-use is what stops one decision authorizing two actions."""
    approval = _approved(engine)
    kwargs = {"action": "open_pr", "target": "engine/add-oauth2", "detail": {"diff_sha": "abc123"}}
    approvals.consume(engine, approval.id, **kwargs)

    with pytest.raises(ApprovalError, match="already been used"):
        approvals.consume(engine, approval.id, **kwargs)


def test_a_changed_diff_invalidates_the_approval(engine: Path) -> None:
    """Approving "push" is approving *that* push. A new commit is a different
    action wearing an approval granted for another."""
    approval = _approved(engine)

    with pytest.raises(ApprovalError, match="new decision"):
        approvals.consume(
            engine, approval.id, action="open_pr", target="engine/add-oauth2",
            detail={"diff_sha": "def456"},
        )


def test_a_changed_target_invalidates_the_approval(engine: Path) -> None:
    approval = _approved(engine)

    with pytest.raises(ApprovalError, match="new decision"):
        approvals.consume(
            engine, approval.id, action="open_pr", target="production",
            detail={"diff_sha": "abc123"},
        )


def test_an_approval_cannot_authorize_a_broader_action(engine: Path) -> None:
    approval = _approved(engine, action="open_pr")

    with pytest.raises(ApprovalError, match="new decision"):
        approvals.consume(engine, approval.id, action="deploy", target="engine/add-oauth2",
                          detail={"diff_sha": "abc123"})


def test_an_unapproved_request_cannot_be_consumed(engine: Path) -> None:
    approval = _request(engine)

    with pytest.raises(ApprovalError, match="is pending, not approved"):
        approvals.consume(engine, approval.id, action="open_pr", target="engine/add-oauth2",
                          detail={"diff_sha": "abc123"})


def test_a_denied_request_cannot_be_consumed(engine: Path) -> None:
    approval = _request(engine)
    approvals.decide(engine, approval.id, approved=False, principal="cli:anass")

    with pytest.raises(ApprovalError, match="is denied, not approved"):
        approvals.consume(engine, approval.id, action="open_pr", target="engine/add-oauth2",
                          detail={"diff_sha": "abc123"})


def test_an_approval_that_expired_before_use_is_refused(engine: Path) -> None:
    approval = _request(engine, ttl_seconds=2)
    approvals.decide(engine, approval.id, approved=True, principal="cli:anass")
    _age(engine, approval.id)

    with pytest.raises(ApprovalError, match="expired before it was used"):
        approvals.consume(engine, approval.id, action="open_pr", target="engine/add-oauth2",
                          detail={"diff_sha": "abc123"})


def _age(engine: Path, approval_id: int) -> None:
    from core.jobs.store import connect

    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with connect(engine) as con:
        con.execute("UPDATE approvals SET expires_at = ? WHERE id = ?", (past, approval_id))


def test_key_order_does_not_change_the_fingerprint() -> None:
    """A re-request nobody changed must not read as a different action and ask
    again."""
    a = approvals.fingerprint("deploy", "prod", {"x": 1, "y": 2})
    b = approvals.fingerprint("deploy", "prod", {"y": 2, "x": 1})

    assert a == b


# --- expiry is deterministic, not lazy ---


def test_stale_requests_are_moved_to_expired(engine: Path) -> None:
    """A request that quietly stays `pending` forever reads as "still waiting
    for you", which is the one thing it is not."""
    approval = _request(engine, ttl_seconds=-1)

    assert approvals.expire_stale(engine) == 1

    assert approvals.get(engine, approval.id).state == approvals.EXPIRED
    assert not approvals.pending(engine)


def test_expiring_is_audited(engine: Path) -> None:
    approval = _request(engine, ttl_seconds=-1)
    approvals.expire_stale(engine)

    assert any(e["event"] == approvals.EXPIRED for e in approvals.events(engine, approval.id))


def test_a_live_request_is_left_pending(engine: Path) -> None:
    _request(engine)

    assert approvals.expire_stale(engine) == 0
    assert len(approvals.pending(engine)) == 1


# --- the full trail survives the row being consumed ---


def test_the_audit_trail_records_every_step(engine: Path) -> None:
    approval = _approved(engine)
    approvals.consume(engine, approval.id, action="open_pr", target="engine/add-oauth2",
                      detail={"diff_sha": "abc123"})

    assert [e["event"] for e in approvals.events(engine, approval.id)] == [
        "requested",
        approvals.APPROVED,
        approvals.CONSUMED,
    ]


# --- budget extensions (issue #27's strict pause) ---


def test_an_approved_budget_extension_only_counts_once_consumed(engine: Path) -> None:
    """An approved-but-unused grant is not spendable capacity — it becomes one
    when consumed, which keeps single-use meaningful for budgets too."""
    approval = _approved(
        engine, action="budget", target="run-1", detail={"extra_tokens": 5000}
    )
    approval = approvals.get(engine, approval.id)

    with_run = dict(run_key="run-1")
    _request(engine, action="budget", target="run-1", detail={"extra_tokens": 5000}, **with_run)
    assert approvals.granted_extension(engine, "run-1") == 0


def test_a_consumed_budget_extension_raises_the_run_ceiling(engine: Path) -> None:
    approval = _request(
        engine, action="budget", target="run-1", detail={"extra_tokens": 5000}, run_key="run-1"
    )
    approvals.decide(engine, approval.id, approved=True, principal="cli:anass")
    approvals.consume(engine, approval.id, action="budget", target="run-1",
                      detail={"extra_tokens": 5000})

    assert approvals.granted_extension(engine, "run-1") == 5000


def test_extensions_from_another_run_do_not_apply(engine: Path) -> None:
    approval = _request(
        engine, action="budget", target="run-1", detail={"extra_tokens": 5000}, run_key="run-1"
    )
    approvals.decide(engine, approval.id, approved=True, principal="cli:anass")
    approvals.consume(engine, approval.id, action="budget", target="run-1",
                      detail={"extra_tokens": 5000})

    assert approvals.granted_extension(engine, "run-2") == 0
