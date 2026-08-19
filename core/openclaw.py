"""Typed asynchronous OpenClaw tools (#30).

This is an adapter boundary, not a second workflow engine. A caller must pass
an AuthenticatedRequest produced by core.transport.auth; all mutations then
reuse the registry, durable job store, approvals and event cursors.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Type

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.jobs import approvals, budget, store, worker
from core.orchestrator import platform_config, registry
from core.transport.auth import AuthenticatedRequest, AuthenticationError, AuthorizationError
from core.transport import service as transport_service

TOOL_VERSION = "v1"
MAX_EVENT_PAGE = 100
MAX_SUMMARY_CHARS = 1000


class ToolError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubmitInput(ToolInput):
    project: str = Field(min_length=1, max_length=128)
    request: str = Field(min_length=1, max_length=100_000)
    mode: Literal["modify"] = "modify"


class RunInput(ToolInput):
    run_id: int = Field(ge=1)


class EventsInput(RunInput):
    cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=MAX_EVENT_PAGE)


class ApprovalInput(RunInput):
    decision: Literal["approve", "deny"]
    approval_id: int | None = Field(default=None, ge=1)
    note: str = Field(default="", max_length=1000)


TOOL_MODELS: Mapping[str, Type[ToolInput]] = {
    "engineering_submit": SubmitInput,
    "engineering_status": RunInput,
    "engineering_cancel": RunInput,
    "engineering_approve": ApprovalInput,
    "engineering_diff": RunInput,
    "engineering_events": EventsInput,
}
TOOL_SCOPES = {
    "engineering_submit": "jobs:submit",
    "engineering_status": "jobs:read",
    "engineering_cancel": "jobs:cancel",
    "engineering_approve": "jobs:approve",
    "engineering_diff": "jobs:read",
    "engineering_events": "jobs:read",
}
TOOL_DESCRIPTIONS = {
    "engineering_submit": "Queue an allowlisted engineering request and return immediately.",
    "engineering_status": "Read compact durable status for one run.",
    "engineering_cancel": "Request cooperative cancellation for one run.",
    "engineering_approve": "Resolve one exact, scoped approval.",
    "engineering_diff": "Return compact authenticated artifact references for a run.",
    "engineering_events": "Replay durable lifecycle events from a cursor.",
}


def tool_schemas() -> dict:
    """Return the versioned schema payload consumed by an OpenClaw tool registry."""
    return {
        "version": TOOL_VERSION,
        "tools": [
            {
                "name": name,
                "version": TOOL_VERSION,
                "description": TOOL_DESCRIPTIONS[name],
                "input_schema": model.model_json_schema(),
            }
            for name, model in TOOL_MODELS.items()
        ],
    }


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    signed_body: bytes


def _body_matches(body: bytes, authenticated: AuthenticatedRequest, expected: dict) -> None:
    if hashlib.sha256(body).hexdigest() != authenticated.body_hash:
        raise AuthenticationError("signed tool body does not match authenticated request")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AuthenticationError("signed tool body is not valid JSON") from None
    if not isinstance(payload, dict):
        raise AuthenticationError("signed tool body must be an object")
    if payload != expected:
        raise AuthenticationError("signed tool body does not match tool arguments")


class OpenClawTools:
    """In-process OpenClaw tool adapter.

    A network adapter should authenticate first, construct AuthenticatedRequest,
    then call this class. It can be replaced by a remote client without
    changing engine policy or job semantics.
    """

    def __init__(self, engine_root: Path, *, spawn: Callable[[Path, int], int] = worker.spawn_detached):
        self.engine_root = Path(engine_root)
        self.spawn = spawn

    def call(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        authenticated: AuthenticatedRequest,
        signed_body: bytes,
    ) -> dict:
        model = TOOL_MODELS.get(name)
        if model is None:
            raise ToolError("unknown_tool", "unknown engineering tool")
        try:
            parsed = model.model_validate(dict(arguments))
        except ValidationError as exc:
            raise ToolError("invalid_arguments", "tool arguments are invalid") from exc
        try:
            authenticated.require(TOOL_SCOPES[name])
        except AuthorizationError as exc:
            raise ToolError("forbidden", "tool scope is not granted") from exc
        expected = (
            {"project_id": parsed.project, "request": parsed.request}
            if name == "engineering_submit"
            else dict(arguments)
        )
        _body_matches(signed_body, authenticated, expected)
        try:
            if name == "engineering_submit":
                return self._submit(parsed, authenticated, signed_body)
            if name == "engineering_status":
                return self._status(parsed.run_id, authenticated)
            if name == "engineering_cancel":
                return self._cancel(parsed.run_id, authenticated)
            if name == "engineering_approve":
                return self._approve(parsed, authenticated)
            if name == "engineering_diff":
                return self._diff(parsed.run_id, authenticated)
            return self._events(parsed, authenticated)
        except AuthorizationError as exc:
            raise ToolError("forbidden", "tool call is not authorized") from exc
        except ToolError:
            raise
        except store.ReplayConflict as exc:
            raise ToolError("idempotency_conflict", str(exc)) from exc
        except (store.JobError, approvals.ApprovalError) as exc:
            raise ToolError("invalid_run", str(exc)) from exc
        except registry.RegistryError as exc:
            raise ToolError("project_not_allowed", str(exc)) from exc

    def _submit(self, parsed: SubmitInput, authenticated: AuthenticatedRequest, body: bytes) -> dict:
        project_id = parsed.project
        if authenticated.envelope.project_id != project_id:
            raise ToolError("project_not_allowed", "signed project does not match the tool project")
        project = registry.resolve(self.engine_root, project_id, action=registry.MODIFY)
        try:
            submission = transport_service.submit_verified(
                self.engine_root,
                project=str(project.path),
                project_id=project_id,
                request=parsed.request,
                body=body,
                authenticated=authenticated,
            )
        except store.ReplayConflict:
            raise
        if submission.created:
            try:
                self.spawn(self.engine_root, submission.id)
            except Exception:
                pass
        return {
            "version": TOOL_VERSION,
            "run_id": submission.id,
            "created": submission.created,
            "state": store.QUEUED,
            "links": {
                "status": f"/v1/jobs/{submission.id}",
                "events": f"/v1/jobs/{submission.id}/events",
                "artifacts": f"/v1/jobs/{submission.id}/artifacts",
            },
        }

    def _job_for(self, run_id: int, authenticated: AuthenticatedRequest) -> store.Job:
        try:
            return transport_service.job_for_principal(
                self.engine_root, run_id, authenticated.principal
            )
        except transport_service.OwnedResourceNotFound:
            raise ToolError("run_not_found", "run not found") from None

    def _status(self, run_id: int, authenticated: AuthenticatedRequest) -> dict:
        job = self._job_for(run_id, authenticated)
        budget_status = None
        if job.run_id and job.envelope.get("project_id"):
            try:
                project = registry.resolve(
                    self.engine_root, job.envelope["project_id"], action=registry.INSPECT
                )
                config = platform_config.load(self.engine_root)
                report = budget.report(
                    self.engine_root, config.limits_for(project.budget_class),
                    run_key=f"run-{job.run_id}", mode=config.budget_mode,
                )
                budget_status = {
                    "reserved_tokens": report.reserved,
                    "consumed_tokens": report.consumed,
                    "calls": report.calls,
                    "limit_tokens": report.limit or None,
                    "remaining_tokens": report.remaining if report.limit else None,
                }
            except Exception:
                budget_status = None
        return {
            "version": TOOL_VERSION,
            "run_id": job.id,
            "project": job.envelope.get("project_id") or job.project,
            "state": job.state,
            "stage": job.stage or None,
            "summary": (job.summary or job.detail or "")[:MAX_SUMMARY_CHARS] or None,
            "branch": job.branch or None,
            "budget": budget_status,
            "submitted_at": job.submitted_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "links": {
                "events": f"/v1/jobs/{job.id}/events",
                "artifacts": f"/v1/jobs/{job.id}/artifacts",
            },
        }

    def _cancel(self, run_id: int, authenticated: AuthenticatedRequest) -> dict:
        self._job_for(run_id, authenticated)
        changed = store.cancel(self.engine_root, run_id)
        return {
            "version": TOOL_VERSION,
            "run_id": run_id,
            "cancelled": changed,
            "state": store.get(self.engine_root, run_id).state,
        }

    def _approve(self, parsed: ApprovalInput, authenticated: AuthenticatedRequest) -> dict:
        job = self._job_for(parsed.run_id, authenticated)
        candidates = [item for item in approvals.pending(self.engine_root, limit=100)
                      if item.job_id == parsed.run_id]
        if parsed.approval_id is None:
            if len(candidates) != 1:
                raise ToolError("approval_required", "approval_id is required when decisions are ambiguous")
            approval = candidates[0]
        else:
            approval = approvals.get(self.engine_root, parsed.approval_id)
            if approval.job_id != parsed.run_id:
                raise ToolError("run_not_found", "run not found")
        if approval.requested_by and approval.requested_by != str(authenticated.principal):
            raise ToolError("run_not_found", "run not found")
        decided = approvals.decide(
            self.engine_root, approval.id, approved=parsed.decision == "approve",
            principal=str(authenticated.principal), note=parsed.note,
        )
        if job.state == store.WAITING_APPROVAL:
            approved = parsed.decision == "approve"
            target = store.QUEUED if approved else store.FAILED
            store.transition(
                self.engine_root, parsed.run_id, target,
                note="approval granted" if approved else "approval denied",
            )
            if approved:
                try:
                    self.spawn(self.engine_root, parsed.run_id)
                except Exception:
                    pass
        return {
            "version": TOOL_VERSION,
            "run_id": parsed.run_id,
            "approval_id": approval.id,
            "decision": parsed.decision,
            "state": decided.state,
            "job_state": store.get(self.engine_root, parsed.run_id).state,
        }

    def _diff(self, run_id: int, authenticated: AuthenticatedRequest) -> dict:
        job = self._job_for(run_id, authenticated)
        preview = None
        try:
            from core.previews import manager as previews
            preview = previews.get_for_job(self.engine_root, run_id)
        except Exception:
            pass
        return {
            "version": TOOL_VERSION,
            "run_id": run_id,
            "branch": job.branch or None,
            "artifacts": [
                {"kind": "diff", "ref": f"/v1/jobs/{run_id}/artifacts/diff", "available": bool(job.branch)},
                {"kind": "log", "ref": f"/v1/jobs/{run_id}/artifacts/log", "available": bool(job.summary or job.detail)},
                {"kind": "preview", "ref": preview.url if preview else None, "available": bool(preview)},
            ],
            "pagination": {"supported": True, "message": "Use the authenticated artifact view for paginated diff content."},
        }

    def _events(self, parsed: EventsInput, authenticated: AuthenticatedRequest) -> dict:
        self._job_for(parsed.run_id, authenticated)
        page = store.events_page(self.engine_root, parsed.run_id, after=parsed.cursor, limit=parsed.limit)
        return {
            "version": TOOL_VERSION,
            "run_id": parsed.run_id,
            "events": page["events"],
            "next_cursor": page["next_cursor"],
            "has_more": page["has_more"],
        }


def invoke(
    tools: OpenClawTools, call: ToolCall, *, authenticated: AuthenticatedRequest
) -> dict:
    """Convenience entry point for a fake or real OpenClaw tool client."""
    return tools.call(
        call.name, call.arguments, authenticated=authenticated, signed_body=call.signed_body
    )
