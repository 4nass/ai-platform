"""End-to-end evidence tests for the remote exposure gate (#49)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core import security_readiness
from core.transport import server


def _pass(name: str) -> security_readiness.SecurityCheck:
    return security_readiness.SecurityCheck(name, security_readiness.PASS, "ok")


def test_default_report_is_fail_closed(tmp_path: Path) -> None:
    report = security_readiness.evaluate(tmp_path, env={})

    assert report.decision == "NO_GO"
    assert not report.remote_ready
    assert {check.name for check in report.failures} >= {
        "Authenticated credentials",
        "Project registry",
        "Network exposure policy",
        "Hard budgets",
        "Secrets retention",
    }


def test_exposure_requires_explicit_tls_and_rate_limit() -> None:
    env = {
        "AI_PLATFORM_REMOTE_ENABLED": "true",
        "AI_PLATFORM_BIND_HOST": "0.0.0.0",
    }
    check = security_readiness._exposure_check(env)
    assert check.status == security_readiness.FAIL
    assert "TLS" in check.detail

    env.update({"AI_PLATFORM_TLS_TERMINATED": "true", "AI_PLATFORM_RATE_LIMIT": "true"})
    check = security_readiness._exposure_check(env)
    assert check.status == security_readiness.PASS


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


def test_risk_acceptance_is_explicit_and_time_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    acceptance = tmp_path / "risk.json"
    acceptance.write_text(
        json.dumps(
            {
                "id": "RA-49-001",
                "owner": "security-owner",
                "scope": "remote-mvp",
                "expires_at": (now + timedelta(days=7)).isoformat(),
                "rationale": "Temporary, reviewed exception while dependencies ship.",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_PLATFORM_RISK_ACCEPTANCE_FILE", str(acceptance))
    checks = ("_auth_check", "_registry_check", "_exposure_check", "_rollback_check", "_budget_check",
              "_action_check", "_sandbox_check", "_secrets_check", "_api_check", "_audit_check")
    for name in checks:
        monkeypatch.setattr(
            security_readiness,
            name,
            (lambda *args, _name=name, **kwargs: _pass(_name)),
        )

    report = security_readiness.evaluate(tmp_path, now=now)
    assert report.remote_ready
    assert report.decision == "GO"

    # A failing check remains visible and only a valid acceptance changes the
    # operator decision; it does not falsely claim that the system is ready.
    monkeypatch.setattr(security_readiness, "_budget_check", lambda *args: security_readiness.SecurityCheck("Hard budgets", security_readiness.FAIL, "not ready"))
    report = security_readiness.evaluate(tmp_path, now=now)
    assert report.decision == "RISK_ACCEPTED"
    assert report.operator_go
    assert not report.remote_ready


def test_remote_server_rejects_unapproved_network_exposure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AI_PLATFORM_REMOTE_ENABLED", raising=False)
    monkeypatch.delenv("AI_PLATFORM_TLS_TERMINATED", raising=False)
    monkeypatch.delenv("AI_PLATFORM_RATE_LIMIT", raising=False)
    with pytest.raises(RuntimeError, match="remote exposure is disabled"):
        server._remote_allowed("0.0.0.0")

    monkeypatch.setenv("AI_PLATFORM_REMOTE_ENABLED", "true")
    monkeypatch.setenv("AI_PLATFORM_TLS_TERMINATED", "true")
    monkeypatch.setenv("AI_PLATFORM_RATE_LIMIT", "true")
    server._remote_allowed("0.0.0.0")
