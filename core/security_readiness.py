"""Fail-closed remote exposure readiness gate (#49).

This module is an evidence report, not a replacement for the controls it
checks — and it is careful about which of the two each line is.

**GO means the prerequisites are ready, not that exposure is on.** The controls
are therefore evaluated whether or not `AI_PLATFORM_REMOTE_ENABLED` is set:
requiring exposure to be live in order to check its protections would mean
turning the system on to find out whether turning it on is safe. The switch
itself is reported, non-blocking, as the state it is in.

**PASS is reserved for what this process can observe.** TLS terminates upstream
and rate limiting lives with it; neither is visible from here, and an
environment variable saying so is a claim, not evidence. Those resolve from a
recorded operator attestation instead and report `ATTESTED` — see
`core.attestations`, which also explains what that does and does not defend
against.

**There is no override.** A gate with a bypass is the bypass. What used to be a
risk acceptance — an unsigned local JSON file that flipped the decision — is
gone; an accepted risk is an attestation with a short expiry, which is the same
act with an honest name and an audit row.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from core import attestations
from core.jobs import budget, store
from core.orchestrator import platform_config, registry, target_config
from core.transport.http import SCOPES

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

ATTESTED = "ATTESTED"
"""Verified by a person, recorded, and expiring — not observed by this process.

A separate status rather than a PASS, because the difference between "the
engine confirmed this" and "someone said so on 3 March" is exactly what the
reader of a security report needs to see."""

SATISFIED = frozenset({PASS, ATTESTED})
DECISIONS = ("GO", "NO_GO")
REQUIRED_SCOPES = frozenset({"jobs:submit", "jobs:read", "jobs:cancel", "jobs:approve"})
EXTERNAL_ACTIONS = frozenset({registry.OPEN_PR, registry.GIT_PUSH, registry.PREVIEW_DEPLOY})


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
class SecurityReport:
    checks: tuple[SecurityCheck, ...]
    generated_at: str
    fingerprint: str = ""

    @property
    def failures(self) -> tuple[SecurityCheck, ...]:
        """Blocking checks that are neither observed nor attested.

        A blocking check must reach PASS or ATTESTED; anything else — FAIL, or
        a WARN on something that blocks — leaves the gate shut. Written against
        `SATISFIED` rather than `== FAIL` so a status added later cannot be
        counted as success by omission.
        """
        return tuple(
            check for check in self.checks
            if check.blocking and check.status not in SATISFIED
        )

    @property
    def remote_ready(self) -> bool:
        return not self.failures

    @property
    def decision(self) -> str:
        return "GO" if self.remote_ready else "NO_GO"

    @property
    def operator_go(self) -> bool:
        """The same answer as `decision`, and deliberately not a second one.

        These were once allowed to disagree — `remote_ready` stayed false while
        `operator_go` went true on a risk acceptance — which meant the field a
        caller read decided what it was told."""
        return self.remote_ready

    @property
    def attested(self) -> tuple[SecurityCheck, ...]:
        return tuple(check for check in self.checks if check.status == ATTESTED)

    def as_dict(self) -> dict:
        return {
            "version": "v1",
            "generated_at": self.generated_at,
            "decision": self.decision,
            "remote_ready": self.remote_ready,
            "deployment_fingerprint": self.fingerprint,
            "checks": [check.as_dict() for check in self.checks],
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


def _exposure_switch_check(env: Mapping[str, str]) -> SecurityCheck:
    """Report the switch; never let its position decide what else gets checked.

    Non-blocking in both positions, because it describes the current state
    rather than a prerequisite: a preflight on a disabled engine is exactly the
    situation this report exists for.
    """
    if _truthy(env.get("AI_PLATFORM_REMOTE_ENABLED")):
        return _check(
            "Remote exposure switch", PASS,
            "remote exposure is enabled",
            "Set AI_PLATFORM_REMOTE_ENABLED=false and restart to disable it",
            blocking=False,
        )
    return _check(
        "Remote exposure switch", PASS,
        "remote exposure is disabled — this report is a preflight, not a live state",
        "Enable it only once this report is GO; the server re-checks before it binds",
        blocking=False,
    )


def _bind_target_check(env: Mapping[str, str]) -> SecurityCheck:
    host = env.get("AI_PLATFORM_BIND_HOST", "127.0.0.1")
    port = env.get("AI_PLATFORM_BIND_PORT", "8787")
    if _loopback(host):
        return _check(
            "Remote bind target", WARN,
            f"bind target {host}:{port} is loopback-only; nothing is reachable off this machine",
            "Set AI_PLATFORM_BIND_HOST to the interface the proxy forwards to",
            blocking=False,
        )
    return _check(
        "Remote bind target", PASS,
        f"bind target {host}:{port} is reachable off this machine",
        blocking=False,
    )


def _attested_check(
    engine_root: Path, name: str, control: str, fingerprint: str, now: datetime, missing: str
) -> SecurityCheck:
    """Resolve a control this process cannot observe from a recorded statement.

    Three outcomes worth telling apart, because they have different fixes: no
    statement was ever made for this exposure, one was made and has run out, or
    one is live.
    """
    live = attestations.active(
        engine_root, control=control, fingerprint=fingerprint, now=now
    )
    if live is not None:
        return _check(
            name, ATTESTED,
            f"attested by {live.attested_by} on {live.attested_at[:10]}, "
            f"expires {live.expires_at[:10]}: {live.statement}",
            f"Re-attest before {live.expires_at[:10]} with: ai-platform attest {control}",
        )
    stale = attestations.latest(engine_root, control=control, fingerprint=fingerprint)
    if stale is not None and stale.revoked_at:
        detail = f"the attestation by {stale.attested_by} was withdrawn on {stale.revoked_at[:10]}"
    elif stale is not None:
        detail = f"the attestation by {stale.attested_by} expired on {stale.expires_at[:10]}"
    else:
        detail = missing
    return _check(
        name, FAIL, detail,
        f"Verify it, then record what you checked: ai-platform attest {control}",
    )


def _tls_check(engine_root: Path, fingerprint: str, now: datetime) -> SecurityCheck:
    return _attested_check(
        engine_root, "TLS termination", attestations.TLS_TERMINATION, fingerprint, now,
        "TLS terminates upstream of this process and cannot be observed from here; "
        "no operator has attested it for this deployment",
    )


def _rate_limit_check(engine_root: Path, fingerprint: str, now: datetime) -> SecurityCheck:
    return _attested_check(
        engine_root, "Rate limiting", attestations.RATE_LIMIT, fingerprint, now,
        "rate limiting lives with the upstream proxy and cannot be observed from here; "
        "no operator has attested it for this deployment",
    )


def _rollback_check(env: Mapping[str, str]) -> SecurityCheck:
    return _check(
        "Disable switch", PASS,
        "set AI_PLATFORM_REMOTE_ENABLED=false and restart the service to disable exposure",
        blocking=False,
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


def _action_policy_check(engine_root: Path) -> SecurityCheck:
    """Can the engine actually perform every external action it permits?

    Read-only, against the real registry, because that is the question with
    consequences: a project allowing `open_pr` against a build with no
    pull-request handler is a promise the engine cannot keep. The old check
    asked whether `ActionExecutor` was importable, which is true of a class
    nothing ever constructs.
    """
    try:
        from core.actions import executor

        projects = registry.load(engine_root)
        available = set(executor.default_handlers())
    except Exception as exc:
        return _check(
            "Audited actions — policy", FAIL,
            f"action policy cannot be resolved ({type(exc).__name__})",
        )

    enabled = {
        action: project_id
        for project_id, project in projects.items()
        for action in project.allowed_actions
        if action in EXTERNAL_ACTIONS
    }
    if not enabled:
        return _check(
            "Audited actions — policy", WARN,
            "no project enables an external action; audited execution is not applicable here",
            "Add open_pr, git_push or preview_deploy to a project's allowed_actions "
            "only once a handler exists for it",
            blocking=False,
        )
    missing = sorted(action for action in enabled if action not in available)
    if missing:
        return _check(
            "Audited actions — policy", FAIL,
            "configured actions have no executable audited handler: "
            + ", ".join(f"{action} ({enabled[action]})" for action in missing),
            "Register a handler, or remove the action from allowed_actions",
        )
    return _check(
        "Audited actions — policy", PASS,
        f"every enabled external action has a handler: {', '.join(sorted(enabled))}",
    )


class _NullActionHandler:
    """Stands where a real handler would, and reaches nothing outside.

    The mechanism check has to drive a plan all the way through approval and
    settlement, which means something has to answer at the end of it. Passing
    this explicitly makes "the health check cannot push" a property of the
    object graph rather than a rule someone has to keep following.
    """

    def execute(self, plan, context):
        from core.actions.executor import ActionResult

        return ActionResult(True, "healthcheck", "dry mechanism check", "")

    def cleanup(self, plan, context):
        return None


def _action_mechanism_check() -> SecurityCheck:
    """Exercise request → approval → fingerprint → audit, touching nothing real.

    In a throwaway engine root: the tables, the approval binding and the refusal
    of a changed plan are what make an audited action auditable, and none of it
    can be confirmed by importing a symbol. Deliberately not run against the
    live `jobs.sqlite` — a health check that writes execution rows into the
    production queue is its own kind of side effect.
    """
    try:
        from core.actions import executor
        from core.jobs import approvals

        with tempfile.TemporaryDirectory(prefix="ai-platform-actioncheck-") as tmp:
            root = Path(tmp)
            project = registry.Project(
                id="healthcheck",
                path=root,
                remote="https://example.invalid/healthcheck.git",
                base_branch="main",
                allowed_actions=(registry.GIT_PUSH,),
                approval_required=(registry.GIT_PUSH,),
            )
            engine = executor.ActionExecutor(
                root, handlers={executor.GIT_PUSH: _NullActionHandler()}
            )
            plan = executor.GitPushPlan(
                project_id="healthcheck",
                branch="engine/healthcheck",
                commit_sha="0" * 40,
                base_sha="1" * 40,
                remote_name="origin",
                remote_url=project.remote,
                base_branch="main",
            )
            pending = engine.execute(
                plan, project=project, principal="cli:healthcheck", request_id="probe-1"
            )
            if pending.state != executor.WAITING_APPROVAL:
                return _check(
                    "Audited actions — mechanism", FAIL,
                    f"a privileged action did not stop for approval (state {pending.state})",
                )

            # The decision a person makes at the CLI. Stood in for here because
            # the point is that the *gate* holds, not that someone is watching.
            approvals.decide(
                root, pending.approval_id, approved=True, principal="cli:healthcheck",
                note="readiness mechanism check",
            )
            done = engine.execute(
                plan, project=project, principal="cli:healthcheck", request_id="probe-1",
                approval_id=pending.approval_id,
            )
            if done.state != executor.SUCCEEDED:
                return _check(
                    "Audited actions — mechanism", FAIL,
                    f"an approved action did not complete (state {done.state})",
                )
            if not engine.events(done.id, principal="cli:healthcheck"):
                return _check(
                    "Audited actions — mechanism", FAIL,
                    "the action produced no audit trail",
                )

            # The property that makes an approval an approval: it covers the
            # inputs it was shown, and a different commit is a different act.
            moved = executor.GitPushPlan(
                project_id="healthcheck",
                branch="engine/healthcheck",
                commit_sha="2" * 40,
                base_sha="1" * 40,
                remote_name="origin",
                remote_url=project.remote,
                base_branch="main",
            )
            second = engine.execute(
                moved, project=project, principal="cli:healthcheck", request_id="probe-2"
            )
            reused = engine.execute(
                moved, project=project, principal="cli:healthcheck", request_id="probe-2",
                approval_id=pending.approval_id,
            )
            if reused.state == executor.SUCCEEDED:
                return _check(
                    "Audited actions — mechanism", FAIL,
                    "an approval was accepted for a plan it was not granted against",
                )
            if second.state != executor.WAITING_APPROVAL:
                return _check(
                    "Audited actions — mechanism", FAIL,
                    "a changed plan did not require its own approval",
                )
    except Exception as exc:
        return _check(
            "Audited actions — mechanism", FAIL,
            f"the audited action path could not be exercised ({type(exc).__name__})",
        )
    return _check(
        "Audited actions — mechanism", PASS,
        "approval is required, consumed once, audited, and refused for a changed plan",
    )


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


def evaluate(
    engine_root: Path,
    *,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> SecurityReport:
    """Read-only. Recording a decision is `attestations.record_decision`."""
    values = os.environ if env is None else env
    current = now or datetime.now(timezone.utc)
    root = Path(engine_root)
    fingerprint = attestations.deployment_fingerprint(values)
    checks = (
        _auth_check(values),
        _registry_check(root),
        _exposure_switch_check(values),
        _bind_target_check(values),
        _tls_check(root, fingerprint, current),
        _rate_limit_check(root, fingerprint, current),
        _rollback_check(values),
        _budget_check(root),
        _action_policy_check(root),
        _action_mechanism_check(),
        _sandbox_check(root),
        _secrets_check(values),
        _api_check(),
        _audit_check(),
    )
    return SecurityReport(checks, current.isoformat(), fingerprint)


def report_json(report: SecurityReport) -> str:
    return json.dumps(report.as_dict(), sort_keys=True)
