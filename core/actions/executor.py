"""One audited executor for consequential external actions (issue #46)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import re
import uuid
from urllib.parse import urlsplit
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Protocol

import git

from core.jobs import approvals, store
from core.orchestrator import git_remote, registry

GIT_PUSH = "git_push"
OPEN_PR = registry.OPEN_PR
PREVIEW_DEPLOY = "preview_deploy"
SUPPORTED_ACTIONS = frozenset({GIT_PUSH, OPEN_PR, PREVIEW_DEPLOY})

REQUESTED = "requested"
WAITING_APPROVAL = "waiting_approval"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
DENIED = "denied"
EXPIRED = "expired"
CANCEL_REQUESTED = "cancel_requested"
CANCELLED = "cancelled"

SCHEMA = """
CREATE TABLE IF NOT EXISTS action_executions (
  id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL UNIQUE,
  action TEXT NOT NULL,
  project_id TEXT NOT NULL,
  principal TEXT NOT NULL,
  job_id INTEGER,
  run_id INTEGER,
  target TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  plan TEXT NOT NULL,
  state TEXT NOT NULL,
  approval_id INTEGER,
  provider TEXT NOT NULL DEFAULT '',
  result_code TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  requested_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_action_exec_project ON action_executions(project_id);
CREATE INDEX IF NOT EXISTS idx_action_exec_state ON action_executions(state);
CREATE TABLE IF NOT EXISTS action_events (
  id INTEGER PRIMARY KEY,
  execution_id TEXT NOT NULL,
  event TEXT NOT NULL,
  at TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT '',
  payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_action_events_execution ON action_events(execution_id);
"""

TERMINAL = frozenset({SUCCEEDED, FAILED, DENIED, EXPIRED, CANCELLED})
_ALLOWED_TRANSITIONS = {
    REQUESTED: frozenset({WAITING_APPROVAL, RUNNING, DENIED, CANCELLED}),
    WAITING_APPROVAL: frozenset({RUNNING, DENIED, EXPIRED, CANCELLED}),
    RUNNING: frozenset({SUCCEEDED, FAILED, CANCEL_REQUESTED}),
    CANCEL_REQUESTED: frozenset({SUCCEEDED, FAILED, CANCELLED}),
}


class ActionError(Exception):
    pass


class ActionReplayError(ActionError):
    pass


class ActionPolicyError(ActionError):
    pass


class ActionNotFound(ActionError):
    pass


@dataclass(frozen=True)
class GitPushPlan:
    project_id: str
    branch: str
    commit_sha: str
    base_sha: str
    remote_name: str
    remote_url: str = ""
    base_branch: str = ""
    remote_sha: str = ""
    operation: str = "git push --no-force"

    def __post_init__(self) -> None:
        if self.operation != "git push --no-force":
            raise ActionPolicyError("git push operation is fixed and cannot contain a shell command")

    @property
    def action(self) -> str:
        return GIT_PUSH

    @property
    def target(self) -> str:
        return f"{self.remote_name}:{self.branch}"

    def detail(self) -> dict:
        return {
            "operation": self.operation,
            "target": self.target,
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "base_sha": self.base_sha,
            "remote_name": self.remote_name,
            "base_branch": self.base_branch,
            "remote_sha": self.remote_sha,
        }

    def safe_payload(self) -> dict:
        return self.detail()


@dataclass(frozen=True)
class OpenPRPlan:
    project_id: str
    branch: str
    commit_sha: str
    base_branch: str
    title: str
    body: str = ""

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", self.branch):
            raise ActionPolicyError("pull request branch is invalid")
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", self.base_branch):
            raise ActionPolicyError("pull request base branch is invalid")
        if not self.title.strip() or len(self.title) > 300:
            raise ActionPolicyError("pull request title must contain 1-300 characters")
        if len(self.body) > 100_000:
            raise ActionPolicyError("pull request body is too large")

    @property
    def action(self) -> str:
        return OPEN_PR

    @property
    def target(self) -> str:
        return f"{self.base_branch}<-{self.branch}"

    def detail(self) -> dict:
        return {
            "operation": "open pull request",
            "target": self.target,
            "branch": self.branch,
            "base_branch": self.base_branch,
            "commit_sha": self.commit_sha,
            "title_sha256": hashlib.sha256(self.title.encode()).hexdigest(),
            "body_sha256": hashlib.sha256(self.body.encode()).hexdigest(),
            "body_chars": len(self.body),
        }

    def safe_payload(self) -> dict:
        return self.detail()


@dataclass(frozen=True)
class PreviewDeployPlan:
    project_id: str
    service: str
    environment: str
    commit_sha: str
    ttl_seconds: int
    config_sha256: str = ""
    data_mode: str = "readonly"

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,160}", self.service):
            raise ActionPolicyError("preview service is invalid")
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,160}", self.environment):
            raise ActionPolicyError("preview environment is invalid")
        if not self.service or not self.environment:
            raise ActionPolicyError("preview service and environment are required")
        if self.ttl_seconds < 60 or self.ttl_seconds > 7 * 24 * 3600:
            raise ActionPolicyError("preview TTL must be between 60 seconds and 7 days")
        if self.data_mode not in {"ephemeral", "readonly"}:
            raise ActionPolicyError("preview data mode must be ephemeral or readonly")

    @property
    def action(self) -> str:
        return PREVIEW_DEPLOY

    @property
    def target(self) -> str:
        return f"{self.environment}/{self.service}"

    def detail(self) -> dict:
        return {
            "operation": "deploy preview",
            "target": self.target,
            "service": self.service,
            "environment": self.environment,
            "commit_sha": self.commit_sha,
            "ttl_seconds": self.ttl_seconds,
            "config_sha256": self.config_sha256,
            "data_mode": self.data_mode,
        }

    def safe_payload(self) -> dict:
        return self.detail()


ActionPlan = GitPushPlan | OpenPRPlan | PreviewDeployPlan


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    provider: str
    summary: str
    external_id: str = ""


@dataclass(frozen=True)
class CleanupResult:
    ok: bool
    summary: str


@dataclass(frozen=True)
class ActionExecution:
    id: str
    request_id: str
    action: str
    project_id: str
    principal: str
    job_id: int | None
    run_id: int | None
    target: str
    fingerprint: str
    state: str
    approval_id: int | None
    provider: str
    result_code: str
    summary: str
    requested_at: str
    started_at: str | None
    finished_at: str | None


class ActionHandler(Protocol):
    def execute(self, plan: ActionPlan, context: "ActionContext") -> ActionResult: ...
    def cleanup(self, plan: ActionPlan, context: "ActionContext") -> CleanupResult | None: ...


@dataclass(frozen=True)
class ActionContext:
    engine_root: Path
    project: registry.Project
    principal: str
    credentials: object = None
    cancel_event: threading.Event | None = None
    request_id: str = ""
    job_id: int | None = None
    run_id: int | None = None


class GitPushHandler:
    """Concrete non-force delivery handler using the #33 base guard."""

    def execute(self, plan: ActionPlan, context: ActionContext) -> ActionResult:
        if not isinstance(plan, GitPushPlan):
            raise ActionPolicyError("git push handler received a different action")
        if context.cancel_event is not None and context.cancel_event.is_set():
            raise ActionError("action cancelled before external call")
        repo = git.Repo(context.project.path)
        try:
            actual = repo.commit(plan.branch).hexsha
        except (git.BadName, ValueError) as exc:
            raise ActionError("delivery branch is no longer available") from exc
        if actual != plan.commit_sha:
            raise ActionError("delivery branch changed after approval")
        snapshot = git_remote.BaseSnapshot(
            base_ref=plan.base_sha,
            base_sha=plan.base_sha,
            remote_url=plan.remote_url,
            remote_name=plan.remote_name,
            remote_sha=plan.remote_sha or plan.base_sha,
            base_branch=plan.base_branch,
            sync_policy=registry.SYNC_OFFLINE,
            sync_status="pinned",
        )
        ref = git_remote.push_delivery_branch(repo, snapshot, plan.branch, approved=True)
        return ActionResult(True, "git", f"pushed {ref}", ref)

    def cleanup(self, plan: ActionPlan, context: ActionContext) -> CleanupResult | None:
        return CleanupResult(True, "no cleanup required for git push")


class ActionExecutor:
    def __init__(
        self,
        engine_root: Path,
        handlers: Mapping[str, ActionHandler] | None = None,
        *,
        credential_provider=None,
        clock=None,
    ):
        self.engine_root = Path(engine_root)
        self.handlers = dict(handlers or {})
        self.handlers.setdefault(GIT_PUSH, GitPushHandler())
        self.credential_provider = credential_provider
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        self._cancel_events: dict[str, threading.Event] = {}
        with self._connect() as con:
            con.executescript(SCHEMA)

    def _connect(self):
        """Use the queue's SQLite discipline for the action tables too.

        Actions and jobs share one durable database. Opening a raw connection
        here would silently skip the queue's WAL mode, foreign keys and
        owner-only creation policy.
        """
        return store.connect(self.engine_root)

    def _now(self) -> str:
        value = self.clock()
        return value if isinstance(value, str) else datetime.fromtimestamp(value, timezone.utc).isoformat()

    def execute(
        self,
        plan: ActionPlan,
        *,
        project: registry.Project,
        principal: str,
        request_id: str,
        approval_id: int | None = None,
        job_id: int | None = None,
        run_id: int | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ActionExecution:
        self._validate(plan, project, request_id)
        detail = plan.detail()
        fp = approvals.fingerprint(plan.action, plan.target, detail)
        existing = self._find_request(request_id)
        if existing is not None:
            return self._replay(existing, fp, plan, project, principal, approval_id, cancel_event)

        execution_id = str(uuid.uuid4())
        now = self._now()
        try:
            with self._connect() as con:
                con.execute(
                    "INSERT INTO action_executions(id,request_id,action,project_id,principal,job_id,run_id,target,"
                    "fingerprint,plan,state,requested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (execution_id, request_id, plan.action, project.id, principal, job_id, run_id,
                     plan.target, fp, json.dumps(plan.safe_payload(), sort_keys=True), REQUESTED, now),
                )
                self._event_con(
                    con, execution_id, "requested", principal,
                    {"action": plan.action, "target": plan.target},
                )
        except sqlite3.IntegrityError:
            # The unique index is the durable idempotency arbiter. Two callers
            # may both observe no row; only one inserts, and the loser returns
            # that exact execution rather than surfacing a SQLite error.
            existing = self._find_request(request_id)
            if existing is None:
                raise
            return self._replay(
                existing, fp, plan, project, principal, approval_id, cancel_event
            )
        execution = self.get(execution_id)

        decision = approvals.classify(project, plan.action)
        if decision == approvals.DENIED_BY_POLICY:
            self._transition(execution_id, DENIED, result_code="policy_denied",
                             summary="action denied by project policy")
            self._audit(execution_id, "refused.policy", principal, {"action": plan.action})
            return self.get(execution_id)

        if decision == approvals.REQUIRES_APPROVAL:
            # A supplied approval is consumed against this newly-created
            # execution before a fresh request can be issued. This makes a
            # reused approval fail closed when any input (for example the
            # commit SHA) changed between approval and execution.
            if approval_id is not None:
                return self._consume_and_run(
                    execution, plan, project, principal, approval_id, cancel_event
                )
            approval = approvals.request(
                self.engine_root, action=plan.action, target=plan.target,
                detail=detail, job_id=job_id, run_key=f"run-{run_id}" if run_id else "",
                requested_by=principal,
            )
            self._transition(execution_id, WAITING_APPROVAL, approval_id=approval.id,
                             result_code="approval_required", summary="waiting for approval")
            self._audit(execution_id, "approval.required", principal,
                        {"approval_id": approval.id, "fingerprint": fp})
            return self.get(execution_id)

        return self._run(execution_id, plan, project, principal, cancel_event)

    def _replay(self, execution, fp, plan, project, principal, approval_id, cancel_event):
        """Resolve an existing request after a retry or concurrent submission."""
        self._owner(execution, principal)
        if execution.fingerprint != fp:
            self._audit(
                execution.id, "refused.replay", principal,
                {"reason": "fingerprint mismatch"},
            )
            raise ActionReplayError("request id was already used for different action inputs")
        if approval_id is not None and execution.state == WAITING_APPROVAL:
            return self._consume_and_run(
                execution, plan, project, principal, approval_id, cancel_event
            )
        return execution

    def cancel(self, execution_id: str, *, principal: str) -> ActionExecution:
        execution = self.get(execution_id)
        self._owner(execution, principal)
        event = self._cancel_events.get(execution_id)
        if event is not None:
            event.set()
        if execution.state in TERMINAL:
            return execution
        target_state = CANCEL_REQUESTED if execution.state == RUNNING else CANCELLED
        self._transition(execution_id, target_state, result_code="cancelled",
                         summary="cancellation requested")
        self._audit(execution_id, "cancel.requested", principal, {})
        return self.get(execution_id)

    def events(self, execution_id: str) -> list[dict]:
        self.get(execution_id)
        with self._connect() as con:
            rows = con.execute(
                "SELECT event,at,actor,payload FROM action_events WHERE execution_id=? ORDER BY id",
                (execution_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"] or "{}")
            result.append(item)
        return result

    def get(self, execution_id: str) -> ActionExecution:
        with self._connect() as con:
            row = con.execute("SELECT * FROM action_executions WHERE id=?", (execution_id,)).fetchone()
        if row is None:
            raise ActionNotFound("action execution not found")
        return ActionExecution(**{key: row[key] for key in ActionExecution.__dataclass_fields__})

    def _run(self, execution_id, plan, project, principal, cancel_event, approval_id=None):
        if cancel_event is None:
            cancel_event = threading.Event()
        self._cancel_events[execution_id] = cancel_event
        context = None
        try:
            if cancel_event.is_set():
                self._transition(execution_id, CANCELLED, result_code="cancelled",
                                 summary="cancelled before external call")
                self._audit(execution_id, "cancelled", principal, {})
                return self.get(execution_id)
            if approval_id is None:
                self._transition(execution_id, RUNNING, started_at=self._now(),
                                 summary="external action started")
                self._audit(execution_id, "started", principal, {"action": plan.action})
            credentials = None
            if self.credential_provider is not None:
                credentials = self.credential_provider.get(project.id, plan.action)
            execution = self.get(execution_id)
            context = ActionContext(
                engine_root=self.engine_root,
                project=project,
                principal=principal,
                credentials=credentials,
                cancel_event=cancel_event,
                request_id=execution.request_id,
                job_id=execution.job_id,
                run_id=execution.run_id,
            )
            handler = self.handlers.get(plan.action)
            if handler is None:
                raise ActionError("no handler registered for this action")
            result = handler.execute(plan, context)
            self._transition(
                execution_id,
                SUCCEEDED if result.ok else FAILED,
                provider=result.provider,
                result_code="success" if result.ok else "provider_failed",
                summary=self._summary(result.summary), finished_at=self._now(),
            )
            self._audit(execution_id, "provider.result", principal,
                        {"provider": result.provider, "ok": result.ok,
                         "external_id": result.external_id})
            if not result.ok:
                self._cleanup(execution_id, plan, context, principal)
            return self.get(execution_id)
        except approvals.ApprovalError as exc:
            state = EXPIRED if "expired" in str(exc).lower() else DENIED
            self._transition(execution_id, state, result_code="approval_rejected",
                             summary="approval was not consumable", finished_at=self._now())
            self._audit(execution_id, "approval.refused", principal,
                        {"reason": type(exc).__name__})
            return self.get(execution_id)
        except Exception as exc:
            self._transition(execution_id, FAILED, result_code=type(exc).__name__,
                             summary="external action failed", finished_at=self._now())
            self._audit(execution_id, "provider.failure", principal,
                        {"error": type(exc).__name__})
            self._cleanup(execution_id, plan, context, principal)
            return self.get(execution_id)
        finally:
            self._cancel_events.pop(execution_id, None)

    def _consume_and_run(self, execution, plan, project, principal, approval_id, cancel_event):
        if cancel_event is not None and cancel_event.is_set():
            self._transition(execution.id, CANCELLED, result_code="cancelled",
                             summary="cancelled before approval consumption",
                             finished_at=self._now())
            self._audit(execution.id, "cancelled", principal, {})
            return self.get(execution.id)
        try:
            approval_record = approvals.get(self.engine_root, approval_id)
            if approval_record.requested_by and approval_record.requested_by != principal:
                raise approvals.ApprovalError("approval belongs to a different principal")
            approval = approvals.consume(
                self.engine_root, approval_id, action=plan.action,
                target=plan.target, detail=plan.detail(),
            )
        except approvals.ApprovalError as exc:
            state = EXPIRED if "expired" in str(exc).lower() else DENIED
            self._transition(execution.id, state, result_code="approval_rejected",
                             summary="approval was not consumable", finished_at=self._now())
            self._audit(execution.id, "approval.refused", principal,
                        {"reason": type(exc).__name__})
            return self.get(execution.id)
        self._transition(execution.id, RUNNING, approval_id=approval.id,
                         started_at=self._now(), result_code="approval_consumed",
                         summary="external action started")
        self._audit(execution.id, "approval.consumed", principal,
                    {"approval_id": approval.id})
        return self._run(execution.id, plan, project, principal, cancel_event, approval.id)

    def _cleanup(self, execution_id, plan, context, principal):
        if context is None:
            return
        handler = self.handlers.get(plan.action)
        if handler is None:
            return
        try:
            result = handler.cleanup(plan, context)
            if result is not None:
                self._audit(execution_id, "cleanup.result", principal, {"ok": result.ok})
        except Exception:
            self._audit(execution_id, "cleanup.failure", principal,
                        {"error": "cleanup failed"})

    def _validate(self, plan, project, request_id):
        if plan.action not in SUPPORTED_ACTIONS:
            raise ActionPolicyError("unsupported action")
        if plan.project_id != project.id:
            raise ActionPolicyError("action project does not match resolved project")
        if not request_id or len(request_id) > 200:
            raise ActionPolicyError("request_id is required and must be <= 200 characters")
        if not all(c.isprintable() for c in request_id):
            raise ActionPolicyError("request_id contains control characters")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", plan.commit_sha or ""):
            raise ActionPolicyError("action must pin a 40-character hexadecimal commit SHA")
        if isinstance(plan, GitPushPlan):
            if not project.remote or plan.remote_url != project.remote:
                raise ActionPolicyError("push remote does not match the project registry")
            parsed = urlsplit(plan.remote_url)
            if parsed.username or parsed.password:
                raise ActionPolicyError("push remote must not contain credentials")
            if not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", plan.branch):
                raise ActionPolicyError("delivery branch is invalid")
            if not re.fullmatch(r"[0-9a-fA-F]{40}", plan.base_sha):
                raise ActionPolicyError("push must pin a hexadecimal base SHA")
            if plan.remote_sha and not re.fullmatch(r"[0-9a-fA-F]{40}", plan.remote_sha):
                raise ActionPolicyError("remote base SHA is invalid")
            if project.base_branch and plan.base_branch != project.base_branch:
                raise ActionPolicyError("push base branch does not match the project registry")
        elif isinstance(plan, OpenPRPlan) and project.base_branch and plan.base_branch != project.base_branch:
            raise ActionPolicyError("pull request base branch does not match the project registry")

    def _owner(self, execution, principal):
        if execution.principal != principal:
            raise ActionError("action is not owned by this principal")

    def _find_request(self, request_id):
        with self._connect() as con:
            row = con.execute(
                "SELECT id FROM action_executions WHERE request_id=?", (request_id,)
            ).fetchone()
        return self.get(row["id"]) if row else None

    def _transition(self, execution_id, state, **fields):
        """Advance one execution with the prior state in the SQL predicate.

        Reading a state on one connection and writing it on another lets two
        callers both validate different successors, then overwrite each other.
        The conditional update makes the loser visible to its caller instead
        of producing an audit trail that claims both transitions happened.
        """
        with self._connect() as con:
            row = con.execute(
                "SELECT state FROM action_executions WHERE id=?", (execution_id,)
            ).fetchone()
            if row is None:
                raise ActionNotFound("action execution not found")
            current = row["state"]
            if current == state:
                return False
            if state not in _ALLOWED_TRANSITIONS.get(current, frozenset()):
                raise ActionError(f"invalid action transition {current} -> {state}")
            assignments = ["state=?"]
            values = [state]
            for key in (
                "approval_id", "provider", "result_code", "summary", "started_at",
                "finished_at",
            ):
                if key in fields:
                    assignments.append(f"{key}=?")
                    values.append(fields[key])
            values.extend((execution_id, current))
            cursor = con.execute(
                f"UPDATE action_executions SET {', '.join(assignments)} "
                "WHERE id=? AND state=?",
                values,
            )
            if cursor.rowcount != 1:
                raise ActionError("action transition lost a concurrent race")
        return True

    def _audit(self, execution_id, event, actor, payload):
        with self._connect() as con:
            self._event_con(con, execution_id, event, actor, payload)

    @staticmethod
    def _event_con(con, execution_id, event, actor, payload):
        con.execute(
            "INSERT INTO action_events(execution_id,event,at,actor,payload) VALUES(?,?,?,?,?)",
            (execution_id, event, datetime.now(timezone.utc).isoformat(), actor,
             json.dumps(payload or {}, sort_keys=True)),
        )

    @staticmethod
    def _summary(value):
        return str(value or "")[:1000]
