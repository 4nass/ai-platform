"""Fail-closed remote exposure readiness gate (#49).

This module is an evidence report, not a replacement for the controls it
checks. A report is GO only when every blocking dependency is demonstrably
ready. A documented, time-bounded risk acceptance changes the decision to
RISK_ACCEPTED but remains visible in JSON and operator output.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from core.jobs import budget, store
from core.orchestrator import platform_config, registry, target_config
from core.transport.http import SCOPES

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
DECISIONS = ("GO", "RISK_ACCEPTED", "NO_GO")
REQUIRED_SCOPES = frozenset({"jobs:submit", "jobs:read", "jobs:cancel", "jobs:approve"})


@dataclass(frozen=True)
class SecurityCheck:
    name: str
    status: str
    detail: str
    remediation: str = ""
    blocking: bool = True

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "remediation": self.remediation,
            "blocking": self.blocking,
        }


@dataclass(frozen=True)
class RiskAcceptance:
    identifier: str
    owner: str
    scope: str
    expires_at: str
    rationale: str

    def as_dict(self) -> dict:
        return {
            "id": self.identifier,
            "owner": self.owner,
            "scope": self.scope,
            "expires_at": self.expires_at,
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class SecurityReport:
    checks: tuple[SecurityCheck, ...]
    generated_at: str
    risk_acceptance: RiskAcceptance | None = None

    @property
    def failures(self) -> tuple[SecurityCheck, ...]:
        return tuple(check for check in self.checks if check.blocking and check.status == FAIL)

    @property
    def remote_ready(self) -> bool:
        return not self.failures

    @property
    def decision(self) -> str:
        if self.remote_ready:
            return "GO"
        return "RISK_ACCEPTED" if self.risk_acceptance else "NO_GO"

    @property
    def operator_go(self) -> bool:
        return self.decision in {"GO", "RISK_ACCEPTED"}

    def as_dict(self) -> dict:
        return {
            "version": "v1",
            "generated_at": self.generated_at,
            "decision": self.decision,
            "remote_ready": self.remote_ready,
            "checks": [check.as_dict() for check in self.checks],
            "risk_acceptance": self.risk_acceptance.as_dict() if self.risk_acceptance else None,
        }


def _check(name, status, detail, remediation="", blocking=True) -> SecurityCheck:
    return SecurityCheck(name, status, detail, remediation, blocking)


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _loopback(host: str) -> bool:
    return host in {"127.0.0.1", "::1", "localhost"}


def _auth_check(env: Mapping[str, str]) -> SecurityCheck:
    raw = env.get("AI_PLATFORM_TRANSPORT_CREDENTIALS", "")
    if not raw:
        return _check(
            "Authenticated credentials", FAIL,
            "no transport credentials are configured",
            "Inject AI_PLATFORM_TRANSPORT_CREDENTIALS from a secret manager",
        )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _check("Authenticated credentials", FAIL, "credential payload is invalid JSON")
    items = list(data.values()) if isinstance(data, dict) else data
    if not isinstance(items, list) or not items:
        return _check("Authenticated credentials", FAIL, "credential payload is empty")
    for item in items:
        if not isinstance(item, dict) or not item.get("key_id") or not item.get("secret"):
            return _check("Authenticated credentials", FAIL, "a credential is missing key_id or secret")
        scopes = set(item.get("scopes") or [])
        if not REQUIRED_SCOPES.issubset(scopes):
            return _check(
                "Authenticated credentials", FAIL,
                "a credential lacks one or more required job scopes",
                "Grant only jobs:submit/read/cancel/approve to the gateway credential",
            )
    return _check("Authenticated credentials", PASS, f"{len(items)} credential(s) configured; secrets withheld")


def _exposure_check(env: Mapping[str, str]) -> SecurityCheck:
    if not _truthy(env.get("AI_PLATFORM_REMOTE_ENABLED")):
        return _check(
            "Network exposure policy", PASS,
            "remote exposure is disabled; localhost-only mode is safe and ready for an explicit deployment decision",
            "Set AI_PLATFORM_REMOTE_ENABLED=true only after this report is GO",
            blocking=False,
        )
    host = env.get("AI_PLATFORM_BIND_HOST", "127.0.0.1")
    if _loopback(host):
        return _check("Network exposure policy", FAIL, f"remote exposure binds loopback host {host}")
    if not _truthy(env.get("AI_PLATFORM_TLS_TERMINATED")):
        return _check("TLS termination", FAIL, "remote exposure has no explicit TLS termination")
    if not _truthy(env.get("AI_PLATFORM_RATE_LIMIT")):
        return _check("Rate limiting", FAIL, "remote exposure has no explicit rate limiting")
    return _check("Network exposure policy", PASS, f"remote host {host} requires TLS and rate limiting")


def _rollback_check(env: Mapping[str, str]) -> SecurityCheck:
    if not _truthy(env.get("AI_PLATFORM_REMOTE_ENABLED")):
        return _check("Disable switch", PASS, "remote exposure is disabled by default")
    return _check(
        "Disable switch", PASS,
        "set AI_PLATFORM_REMOTE_ENABLED=false and restart the service to disable exposure",
    )


def _registry_check(engine_root: Path) -> SecurityCheck:
    try:
        projects = registry.load(engine_root)
    except Exception as exc:
        return _check("Project registry", FAIL, f"registry cannot be loaded ({type(exc).__name__})")
    if not projects:
        return _check("Project registry", FAIL, "no allowlisted projects are configured")
    for project in projects.values():
        if not project.path.is_dir():
            return _check("Project registry", FAIL, f"project {project.id!r} path is unavailable")
        if not set(project.allowed_actions).issubset(set(registry.ACTIONS)):
            return _check("Project registry", FAIL, f"project {project.id!r} contains an unknown action")
    return _check("Project registry", PASS, f"{len(projects)} allowlisted project(s) with canonical roots")


def _budget_check(engine_root: Path) -> SecurityCheck:
    try:
        config = platform_config.load(engine_root)
    except Exception as exc:
        return _check("Hard budgets", FAIL, f"platform budget policy cannot be loaded ({type(exc).__name__})")
    if config.budget_mode != budget.STRICT:
        return _check(
            "Hard budgets", FAIL,
            f"budget mode is {config.budget_mode!r}, not strict",
            "Set budgets.mode=strict before remote exposure",
        )
    if not config.budget_classes or any(not limits.declared for limits in config.budget_classes.values()):
        return _check("Hard budgets", FAIL, "one or more project budget classes are unlimited")
    return _check(
        "Hard budgets", FAIL,
        "time and currency ceilings are not enforced by the current budget gate",
        "Complete issue #45 before enabling the remote MVP",
    )


def _action_check() -> SecurityCheck:
    try:
        from core.actions import executor
        from core.jobs import approvals
        if not callable(executor.ActionExecutor) or not callable(approvals.request):
            raise TypeError("audited action primitives are unavailable")
    except Exception as exc:
        return _check("Audited actions", FAIL, f"approval/action executor unavailable ({type(exc).__name__})")
    return _check("Audited actions", PASS, "scoped approvals and audited executor are importable")


def _sandbox_check(engine_root: Path) -> SecurityCheck:
    if shutil.which("bwrap") is None:
        return _check(
            "Fail-closed sandbox", FAIL,
            "Bubblewrap is not installed; unattended validation would run unsandboxed",
            "Install bwrap on the service host",
        )
    try:
        projects = registry.load(engine_root)
        for project in projects.values():
            import git
            repo = git.Repo(project.path)
            config = target_config.load_at_commit(repo, repo.head.commit.hexsha)
            if not config.test_sandbox:
                return _check(
                    "Fail-closed sandbox", FAIL,
                    f"project {project.id!r} disables test_sandbox",
                    "Set test_sandbox: true in the committed target policy",
                )
    except Exception as exc:
        return _check("Fail-closed sandbox", FAIL, f"sandbox policy cannot be verified ({type(exc).__name__})")
    return _check("Fail-closed sandbox", PASS, "Bubblewrap and committed sandbox policies are enabled")


def _secrets_check(env: Mapping[str, str]) -> SecurityCheck:
    try:
        from core import notifications, untrusted
        if not callable(notifications._safe) or not callable(untrusted.neutralize):
            raise TypeError("redaction primitives are unavailable")
    except Exception as exc:
        return _check("Secret redaction", FAIL, f"redaction primitives unavailable ({type(exc).__name__})")
    retention = env.get("AI_PLATFORM_SECRET_RETENTION_DAYS", "")
    policy = env.get("AI_PLATFORM_SECRET_POLICY", "")
    if not retention or not retention.isdigit() or int(retention) < 1 or not policy:
        return _check(
            "Secrets retention", FAIL,
            "no explicit secret retention policy is configured",
            "Complete #35 with a retention duration and policy reference",
        )
    return _check("Secrets redaction", PASS, "credential-like values are redacted before mobile rendering", blocking=False)


def _api_check() -> SecurityCheck:
    try:
        from core.transport.http import RemoteAPI
        required_paths = {
            ("POST", "/v1/jobs"), ("GET", "job"), ("GET", "events"),
            ("POST", "cancel"), ("POST", "approval"), ("GET", "artifacts"),
        }
        if not required_paths.issubset(set(__import__("core.transport.http", fromlist=["SCOPES"]).SCOPES)):
            raise TypeError("one or more authenticated API scopes are missing")
        if not callable(RemoteAPI):
            raise TypeError("REST API is unavailable")
    except Exception as exc:
        return _check("Authenticated REST/SSE API", FAIL, f"API contract unavailable ({type(exc).__name__})")
    return _check("Authenticated REST/SSE API", PASS, "typed endpoints use principal-bound authentication")


def _audit_check() -> SecurityCheck:
    try:
        if not callable(store.events_page):
            raise TypeError("durable job events unavailable")
        from core.telemetry import store as telemetry
        if not callable(telemetry.run_totals):
            raise TypeError("telemetry unavailable")
    except Exception as exc:
        return _check("Audit trail", FAIL, f"audit stores unavailable ({type(exc).__name__})")
    return _check("Audit trail", PASS, "jobs, events and telemetry are durable")


def _risk_acceptance(engine_root: Path, env: Mapping[str, str], now: datetime) -> RiskAcceptance | None:
    path = Path(env.get("AI_PLATFORM_RISK_ACCEPTANCE_FILE", str(engine_root / "config/security-risk-acceptance.json")))
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        expires = datetime.fromisoformat(str(data["expires_at"]))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= now or data.get("scope") != "remote-mvp":
            return None
        values = {key: str(data[key]).strip() for key in ("id", "owner", "scope", "expires_at", "rationale")}
        if not all(values.values()):
            return None
        return RiskAcceptance(values["id"], values["owner"], values["scope"], values["expires_at"], values["rationale"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def evaluate(engine_root: Path, *, env: Mapping[str, str] | None = None, now: datetime | None = None) -> SecurityReport:
    values = os.environ if env is None else env
    current = now or datetime.now(timezone.utc)
    checks = (
        _auth_check(values),
        _registry_check(Path(engine_root)),
        _exposure_check(values),
        _rollback_check(values),
        _budget_check(Path(engine_root)),
        _action_check(),
        _sandbox_check(Path(engine_root)),
        _secrets_check(values),
        _api_check(),
        _audit_check(),
    )
    return SecurityReport(checks, current.isoformat(), _risk_acceptance(Path(engine_root), values, current))


def report_json(report: SecurityReport) -> str:
    return json.dumps(report.as_dict(), sort_keys=True)
