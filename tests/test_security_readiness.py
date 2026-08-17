"""End-to-end evidence tests for the remote exposure gate (#49)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core import attestations, security_readiness
from core.transport import server


def _pass(name: str) -> security_readiness.SecurityCheck:
    return security_readiness.SecurityCheck(name, security_readiness.PASS, "ok")


def _named(report, name: str) -> security_readiness.SecurityCheck:
    return next(check for check in report.checks if check.name == name)


def _registry(root: Path, *, actions: str) -> None:
    """A one-project registry whose path resolves, so the policy check has
    something real to read."""
    target = root / "target"
    target.mkdir(exist_ok=True)
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "projects.yaml").write_text(
        f"roots:\n  - {root}\nprojects:\n  demo:\n    path: {target}\n"
        f"    allowed_actions: {actions}\n",
        encoding="utf-8",
    )


def test_default_report_is_fail_closed(tmp_path: Path) -> None:
    report = security_readiness.evaluate(tmp_path, env={})

    assert report.decision == "NO_GO"
    assert not report.remote_ready
    assert {check.name for check in report.failures} >= {
        "Authenticated credentials",
        "Project registry",
        "Hard budgets",
        "Secrets retention",
    }


def test_credentials_are_validated_without_echoing_secret() -> None:
    secret = "do-not-log-this"
    check = security_readiness._auth_check(
        {
            "AI_PLATFORM_TRANSPORT_CREDENTIALS": json.dumps(
                [{"key_id": "gateway", "secret": secret, "scopes": sorted(security_readiness.REQUIRED_SCOPES)}]
            )
        }
    )
    assert check.status == security_readiness.PASS
    assert secret not in check.detail




# --- preflight: protections are checked before exposure, not after ---


def test_tls_is_checked_while_exposure_is_still_disabled(tmp_path: Path) -> None:
    """Otherwise the only way to test the protections is to turn on the thing
    they protect — and the remediation text told operators to do exactly that."""
    report = security_readiness.evaluate(
        tmp_path, env={"AI_PLATFORM_REMOTE_ENABLED": "false"}
    )

    tls = _named(report, "TLS termination")
    assert tls.status == security_readiness.FAIL
    assert tls.blocking is True


def test_rate_limiting_is_checked_while_exposure_is_still_disabled(tmp_path: Path) -> None:
    report = security_readiness.evaluate(
        tmp_path, env={"AI_PLATFORM_REMOTE_ENABLED": "false"}
    )

    assert _named(report, "Rate limiting").status == security_readiness.FAIL


def test_the_exposure_switch_never_decides_what_else_is_checked(tmp_path: Path) -> None:
    off = security_readiness.evaluate(tmp_path, env={})
    on = security_readiness.evaluate(tmp_path, env={"AI_PLATFORM_REMOTE_ENABLED": "true"})

    assert {c.name for c in off.checks} == {c.name for c in on.checks}
    assert _named(off, "Remote exposure switch").blocking is False


# --- attestation, not declaration ---


def test_an_environment_variable_cannot_satisfy_tls(tmp_path: Path) -> None:
    """The whole defect: `AI_PLATFORM_TLS_TERMINATED=true` was a claim reported
    as a check. Four variables instead of one would have been four claims."""
    report = security_readiness.evaluate(
        tmp_path,
        env={
            "AI_PLATFORM_REMOTE_ENABLED": "true",
            "AI_PLATFORM_BIND_HOST": "0.0.0.0",
            "AI_PLATFORM_TLS_TERMINATED": "true",
            "AI_PLATFORM_RATE_LIMIT": "true",
        },
    )

    assert _named(report, "TLS termination").status == security_readiness.FAIL
    assert _named(report, "Rate limiting").status == security_readiness.FAIL


def test_a_recorded_attestation_satisfies_a_control_as_attested(tmp_path: Path) -> None:
    env = {"AI_PLATFORM_BIND_HOST": "10.0.0.5"}
    fingerprint = attestations.deployment_fingerprint(env)
    attestations.record(
        tmp_path, control=attestations.TLS_TERMINATION, fingerprint=fingerprint,
        statement="nginx terminates TLS on 443, certificate verified",
        attested_by="owner", ttl_days=30,
    )

    check = _named(security_readiness.evaluate(tmp_path, env=env), "TLS termination")

    assert check.status == security_readiness.ATTESTED
    assert check.status != security_readiness.PASS, "attested is not observed"
    assert "owner" in check.detail


def test_an_expired_attestation_stops_counting_and_says_so(tmp_path: Path) -> None:
    env = {"AI_PLATFORM_BIND_HOST": "10.0.0.5"}
    fingerprint = attestations.deployment_fingerprint(env)
    attestations.record(
        tmp_path, control=attestations.TLS_TERMINATION, fingerprint=fingerprint,
        statement="checked", attested_by="owner", ttl_days=1,
    )

    later = datetime.now(timezone.utc) + timedelta(days=2)
    check = _named(security_readiness.evaluate(tmp_path, env=env, now=later), "TLS termination")

    assert check.status == security_readiness.FAIL
    assert "expired" in check.detail


def test_moving_the_deployment_voids_the_attestation_made_about_it(tmp_path: Path) -> None:
    """A statement about one exposure is not evidence about a different one."""
    attested = {"AI_PLATFORM_BIND_HOST": "10.0.0.5"}
    attestations.record(
        tmp_path, control=attestations.TLS_TERMINATION,
        fingerprint=attestations.deployment_fingerprint(attested),
        statement="checked", attested_by="owner",
    )

    assert _named(
        security_readiness.evaluate(tmp_path, env=attested), "TLS termination"
    ).status == security_readiness.ATTESTED
    assert _named(
        security_readiness.evaluate(tmp_path, env={"AI_PLATFORM_BIND_HOST": "0.0.0.0"}),
        "TLS termination",
    ).status == security_readiness.FAIL


def test_an_unrelated_setting_does_not_void_an_attestation(tmp_path: Path) -> None:
    """A fingerprint wide enough to catch every edit gets re-attested by reflex,
    which is the same as not attesting."""
    env = {"AI_PLATFORM_BIND_HOST": "10.0.0.5"}
    attestations.record(
        tmp_path, control=attestations.RATE_LIMIT,
        fingerprint=attestations.deployment_fingerprint(env),
        statement="10 req/s per principal at the proxy", attested_by="owner",
    )

    unrelated = {**env, "AI_PLATFORM_SOMETHING_ELSE": "changed"}
    assert _named(
        security_readiness.evaluate(tmp_path, env=unrelated), "Rate limiting"
    ).status == security_readiness.ATTESTED


def test_an_attestation_must_say_what_was_verified(tmp_path: Path) -> None:
    with pytest.raises(attestations.AttestationError, match="what was verified"):
        attestations.record(
            tmp_path, control=attestations.TLS_TERMINATION, fingerprint="fp",
            statement="   ", attested_by="owner",
        )


def test_a_withdrawn_attestation_stops_counting(tmp_path: Path) -> None:
    env = {"AI_PLATFORM_BIND_HOST": "10.0.0.5"}
    fingerprint = attestations.deployment_fingerprint(env)
    recorded = attestations.record(
        tmp_path, control=attestations.TLS_TERMINATION, fingerprint=fingerprint,
        statement="checked", attested_by="owner",
    )

    assert attestations.revoke(tmp_path, recorded.id, actor="owner") is True

    check = _named(security_readiness.evaluate(tmp_path, env=env), "TLS termination")
    assert check.status == security_readiness.FAIL
    assert "withdrawn" in check.detail


# --- there is no override ---


def test_the_gate_has_no_bypass(tmp_path: Path) -> None:
    """`RISK_ACCEPTED` used to flip `operator_go` true while `remote_ready`
    stayed false, so which field a caller read decided what it was told."""
    assert security_readiness.DECISIONS == ("GO", "NO_GO")
    assert not hasattr(security_readiness, "RiskAcceptance")
    assert not hasattr(security_readiness, "_risk_acceptance")

    report = security_readiness.evaluate(tmp_path, env={})
    assert report.operator_go is report.remote_ready is False


def test_go_requires_every_blocking_check_to_be_passed_or_attested(tmp_path: Path) -> None:
    satisfied = security_readiness.SecurityReport(
        checks=(
            security_readiness.SecurityCheck("observed", security_readiness.PASS, "ok"),
            security_readiness.SecurityCheck("stated", security_readiness.ATTESTED, "ok"),
            security_readiness.SecurityCheck("advisory", security_readiness.WARN, "ok", blocking=False),
        ),
        generated_at="now",
    )
    assert satisfied.decision == "GO"

    blocked = security_readiness.SecurityReport(
        checks=(security_readiness.SecurityCheck("blocking warn", security_readiness.WARN, "ok"),),
        generated_at="now",
    )
    assert blocked.decision == "NO_GO", "a blocking WARN is not a pass"


# --- audited actions: policy and mechanism, not importability ---


def test_no_external_action_configured_warns_rather_than_passes(tmp_path: Path) -> None:
    _registry(tmp_path, actions="[inspect, modify, test]")

    check = security_readiness._action_policy_check(tmp_path)

    assert check.status == security_readiness.WARN
    assert check.blocking is False
    assert "not applicable" in check.detail


def test_an_action_without_a_handler_fails(tmp_path: Path) -> None:
    """`open_pr` is declarable in the registry and has no handler in this
    build — a promise the engine cannot keep, and previously invisible."""
    _registry(tmp_path, actions="[inspect, modify, open_pr]")

    check = security_readiness._action_policy_check(tmp_path)

    assert check.status == security_readiness.FAIL
    assert "open_pr" in check.detail


def test_an_action_with_a_handler_passes(tmp_path: Path) -> None:
    _registry(tmp_path, actions="[inspect, modify, git_push]")

    assert security_readiness._action_policy_check(tmp_path).status == security_readiness.PASS


def test_the_mechanism_check_exercises_approval_and_refuses_a_changed_plan() -> None:
    check = security_readiness._action_mechanism_check()

    assert check.status == security_readiness.PASS
    assert "refused for a changed plan" in check.detail


def test_the_mechanism_check_writes_nothing_to_the_live_queue(tmp_path: Path) -> None:
    """A health check that files execution rows into the production queue is
    its own kind of side effect."""
    from core.jobs import store

    with store.connect(tmp_path):
        pass
    before = (tmp_path / "jobs.sqlite").read_bytes()

    security_readiness._action_mechanism_check()

    assert (tmp_path / "jobs.sqlite").read_bytes() == before


def test_the_mechanism_check_cannot_reach_a_real_handler() -> None:
    """Structural, not a convention: the null handler is passed explicitly, so
    no code path in the check can arrive at GitPushHandler."""
    import inspect

    source = inspect.getsource(security_readiness._action_mechanism_check)
    assert "_NullActionHandler()" in source
    assert "GitPushHandler" not in source


# --- the server re-checks at the moment that matters ---


def test_a_non_loopback_bind_is_refused_while_a_blocking_check_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AI_PLATFORM_REMOTE_ENABLED", "true")
    monkeypatch.setenv("AI_PLATFORM_TLS_TERMINATED", "true")
    monkeypatch.setenv("AI_PLATFORM_RATE_LIMIT", "true")

    with pytest.raises(RuntimeError, match="readiness is NO_GO"):
        server._remote_allowed(tmp_path, "0.0.0.0")


def test_a_loopback_bind_does_not_require_the_gate(tmp_path: Path) -> None:
    server._remote_allowed(tmp_path, "127.0.0.1")


def test_the_disable_switch_still_refuses_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kept as an independent second line: a bug in the readiness logic must not
    be enough on its own to open a socket."""
    monkeypatch.delenv("AI_PLATFORM_REMOTE_ENABLED", raising=False)

    with pytest.raises(RuntimeError, match="remote exposure is disabled"):
        server._remote_allowed(tmp_path, "0.0.0.0")


def test_a_bind_attempt_is_recorded_against_the_configuration_it_was_made_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AI_PLATFORM_REMOTE_ENABLED", "true")

    with pytest.raises(RuntimeError):
        server._remote_allowed(tmp_path, "0.0.0.0")

    recorded = attestations.decisions(tmp_path)
    assert recorded and recorded[0]["decision"] == "NO_GO"
    assert recorded[0]["fingerprint"]
    assert recorded[0]["actor"] == "server:bind"
