"""Minimal authenticated REST + SSE boundary for remote clients (issue #47).

The application is WSGI-compatible and deliberately has no orchestration logic:
authentication, project admission and durable job primitives remain the only
ways to reach the engine. Deploy it behind a TLS-terminating reverse proxy;
the built-in server helper is for local development and smoke tests.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs

from core.jobs import approvals, budget, store, worker
from core.previews import manager as preview_manager
from core.jobs.envelope import Envelope
from core.orchestrator import platform_config, registry
from core.transport.auth import (
    Authenticator,
    AuthorizationError,
    AuthenticationError,
    ReplayError,
    TransportAuthError,
)
from core.transport import service as transport_service

MAX_BODY_BYTES = 1_048_576
JOB_ID = re.compile(r"^[1-9][0-9]{0,18}$")
ACCESS_LOG = logging.getLogger("ai_platform.transport.access")
SCOPES = {
    ("POST", "/v1/jobs"): "jobs:submit",
    ("GET", "job"): "jobs:read",
    ("GET", "events"): "jobs:read",
    ("POST", "cancel"): "jobs:cancel",
    ("POST", "approval"): "jobs:approve",
    ("GET", "artifacts"): "jobs:read",
    ("GET", "preview"): "jobs:read",
}


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class RemoteAPI:
    def __init__(
        self,
        engine_root: Path,
        authenticator: Authenticator,
        *,
        max_body_bytes: int = MAX_BODY_BYTES,
        spawn: Callable[[Path, int], int] = worker.spawn_detached,
        clock: Callable[[], float] = time.time,
    ):
        self.engine_root = Path(engine_root)
        self.authenticator = authenticator
        self.max_body_bytes = max_body_bytes
        self.spawn = spawn
        self.clock = clock

    def application(self, environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "")
        status = 500
        outcome = "internal_error"
        try:
            if not path.startswith("/v1/"):
                raise APIError(404, "not_found", "resource not found")
            body = self._body(environ)
            if body:
                self._require_json_content_type(environ)
            payload = self._json(body) if body else {}
            auth = self._authenticate(environ, method, path, body, payload)
            result = self._dispatch(method, path, payload, auth, environ, body)
            status, outcome = 200, "ok"
            if isinstance(result, _SSE):
                headers = [
                    ("Content-Type", "text/event-stream; charset=utf-8"),
                    ("Cache-Control", "no-cache, no-store"),
                    ("Connection", "keep-alive"),
                    ("X-Accel-Buffering", "no"),
                ]
                start_response("200 OK", headers)
                return result.iter_bytes()
            return self._json_response(start_response, status, result)
        except APIError as exc:
            status, outcome = exc.status, exc.code
            return self._error(start_response, status, exc.code, exc.message)
        except AuthorizationError:
            status, outcome = 403, "authorization_denied"
            return self._error(start_response, status, "forbidden", "operation not authorized")
        except (AuthenticationError, ReplayError, TransportAuthError):
            status, outcome = 401, "authentication_failed"
            return self._error(start_response, status, "unauthorized", "request not authorized")
        except (registry.RegistryError, store.JobError, approvals.ApprovalError):
            status, outcome = 404, "not_found"
            return self._error(start_response, status, "not_found", "resource not found")
        except Exception:
            return self._error(start_response, status, outcome, "internal server error")
        finally:
            self._log_access(environ, method=method, path=path, status=status, outcome=outcome)

    @staticmethod
    def _log_path(path: str) -> str:
        """Return a route template rather than logging a caller-controlled path."""
        if path == "/v1/jobs":
            return path
        parts = path.rstrip("/").split("/")
        if len(parts) == 4 and JOB_ID.fullmatch(parts[3]):
            return "/v1/jobs/{id}"
        if len(parts) == 5 and JOB_ID.fullmatch(parts[3]) and parts[4] in {
            "events", "cancel", "approval", "artifacts", "preview"
        }:
            return f"/v1/jobs/{{id}}/{parts[4]}"
        return "invalid"

    @staticmethod
    def _log_access(environ, *, method: str, path: str, status: int, outcome: str) -> None:
        """Log bounded, non-secret request evidence for incident response.

        Do not log bodies, query strings, credential ids, signatures, nonces,
        envelope ids or an arbitrary PATH_INFO value. Those are all supplied by
        the caller and may contain sensitive data.
        """
        level = logging.INFO if status < 400 else logging.WARNING
        ACCESS_LOG.log(
            level,
            "transport_request method=%s route=%s status=%s outcome=%s client=%s",
            method,
            RemoteAPI._log_path(path),
            status,
            outcome,
            environ.get("REMOTE_ADDR", "-"),
        )

    @staticmethod
    def _json_response(start_response, status, value):
        data = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        reasons = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
                   413: "Payload Too Large", 403: "Forbidden",
                   415: "Unsupported Media Type", 500: "Internal Server Error"}
        start_response(f"{status} {reasons.get(status, 'Error')}", [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(data))),
            ("Cache-Control", "no-store"),
        ])
        return [data]

    def _error(self, start_response, status, code, message):
        return self._json_response(start_response, status, {"error": {"code": code, "message": message}})

    def _body(self, environ) -> bytes:
        raw_length = environ.get("CONTENT_LENGTH", "")
        try:
            length = int(raw_length or 0)
        except ValueError:
            raise APIError(400, "invalid_request", "invalid content length")
        if length < 0 or length > self.max_body_bytes:
            raise APIError(413, "request_too_large", "request body is too large")
        stream = environ.get("wsgi.input")
        body = stream.read(length) if stream is not None else b""
        if len(body) != length:
            raise APIError(400, "invalid_request", "incomplete request body")
        return body

    @staticmethod
    def _require_json_content_type(environ) -> None:
        content_type = environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise APIError(
                415,
                "unsupported_media_type",
                "request body must use Content-Type application/json",
            )

    @staticmethod
    def _json(body: bytes) -> dict:
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise APIError(400, "invalid_json", "request body must be valid JSON")
        if not isinstance(value, dict):
            raise APIError(400, "invalid_json", "request body must be a JSON object")
        return value

    def _authenticate(self, environ, method, path, body, payload):
        key_id = environ.get("HTTP_X_API_KEY", "")
        signature = environ.get("HTTP_X_SIGNATURE", "")
        nonce = environ.get("HTTP_X_NONCE", "")
        timestamp = environ.get("HTTP_X_TIMESTAMP", "")
        if not all((key_id, signature, nonce, timestamp)):
            raise AuthenticationError("missing authentication headers")
        envelope_data = payload.get("envelope") if method == "POST" else None
        envelope_data = envelope_data if isinstance(envelope_data, dict) else {}
        envelope = Envelope(
            channel=str(envelope_data.get("channel") or environ.get("HTTP_X_CHANNEL", "")),
            sender_id=str(envelope_data.get("sender_id") or environ.get("HTTP_X_SENDER_ID", "")),
            chat_id=str(envelope_data.get("chat_id") or environ.get("HTTP_X_CHAT_ID", "")),
            message_id=str(envelope_data.get("message_id") or environ.get("HTTP_X_MESSAGE_ID", "")),
            sent_at=str(envelope_data.get("sent_at") or ""),
            project_id=str(payload.get("project_id") or envelope_data.get("project_id") or "") or None,
            session_id=payload.get("session_id"),
            dirty_policy=str(payload.get("dirty_policy") or "head"),
        )
        scope = self._scope(method, path)
        query = environ.get("QUERY_STRING", "")
        signed_path = f"{path}?{query}" if query else path
        return self.authenticator.verify(
            method=method,
            path=signed_path,
            body=body,
            key_id=key_id,
            timestamp=timestamp,
            nonce=nonce,
            signature=signature,
            envelope=envelope,
            scope=scope,
            require_delivery_identity=(method == "POST" and path == "/v1/jobs"),
        )

    @staticmethod
    def _scope(method, path):
        if path == "/v1/jobs":
            return SCOPES[(method, path)]
        parts = path.rstrip("/").split("/")
        if len(parts) == 4 and JOB_ID.fullmatch(parts[3]):
            return SCOPES.get((method, "job"))
        if len(parts) == 5 and JOB_ID.fullmatch(parts[3]):
            return SCOPES.get((method, parts[4]))
        raise APIError(404, "not_found", "resource not found")

    def _dispatch(self, method, path, payload, auth, environ, body):
        if method == "POST" and path == "/v1/jobs":
            return self._submit(payload, auth, body)
        parts = path.rstrip("/").split("/")
        if len(parts) < 4 or not JOB_ID.fullmatch(parts[3]):
            raise APIError(404, "not_found", "resource not found")
        job_id = int(parts[3])
        if method == "GET" and len(parts) == 4:
            return self._status(job_id, auth)
        if method == "GET" and len(parts) == 5 and parts[4] == "events":
            return self._events(job_id, auth, environ)
        if method == "POST" and len(parts) == 5 and parts[4] == "cancel":
            return self._cancel(job_id, auth)
        if method == "POST" and len(parts) == 5 and parts[4] == "approval":
            return self._approval(job_id, payload, auth)
        if method == "GET" and len(parts) == 5 and parts[4] == "artifacts":
            return self._artifacts(job_id, auth)
        if method == "GET" and len(parts) == 5 and parts[4] == "preview":
            return self._preview(job_id, auth)
        raise APIError(404, "not_found", "resource not found")

    def _job_for(self, job_id, auth):
        try:
            return transport_service.job_for_principal(
                self.engine_root, job_id, auth.principal
            )
        except transport_service.OwnedResourceNotFound:
            raise APIError(404, "not_found", "resource not found") from None

    def _submit(self, payload, auth, body):
        project_id = payload.get("project_id")
        request = payload.get("request")
        if not isinstance(project_id, str) or not project_id or not isinstance(request, str) or not request.strip():
            raise APIError(400, "invalid_request", "project_id and request are required")
        if len(request) > 100_000:
            raise APIError(413, "request_too_large", "request is too large")
        try:
            project = registry.resolve(self.engine_root, project_id, action=registry.MODIFY)
        except registry.RegistryError:
            raise APIError(404, "not_found", "resource not found")
        submission = transport_service.submit_verified(
            self.engine_root,
            project=str(project.path),
            project_id=project_id,
            request=request,
            body=body,
            authenticated=auth,
        )
        if submission.created:
            try:
                self.spawn(self.engine_root, submission.id)
            except Exception:
                # The durable queue entry remains valid; a managed worker can
                # drain it later. Do not turn a successful submission into an
                # ambiguous retry.
                pass
        return {"job_id": submission.id, "created": submission.created, "state": store.QUEUED}

    def _status(self, job_id, auth):
        job = self._job_for(job_id, auth)
        budget_status = None
        if job.run_id:
            try:
                project = registry.resolve(
                    self.engine_root, job.envelope.get("project_id", ""), action=registry.INSPECT
                )
                config = platform_config.load(self.engine_root)
                report = budget.report(
                    self.engine_root,
                    config.limits_for(project.budget_class),
                    run_key=f"run-{job.run_id}",
                    mode=config.budget_mode,
                )
                budget_status = {
                    "reserved_tokens": report.reserved,
                    "consumed_tokens": report.consumed,
                    "calls": report.calls,
                    "limit_tokens": report.limit or None,
                    "remaining_tokens": report.remaining if report.limit else None,
                    "mode": report.mode,
                }
            except Exception:
                budget_status = None
        preview = preview_manager.get_for_job(self.engine_root, job.id)
        return {
            "job_id": job.id, "state": job.state,
            "submitted_at": job.submitted_at, "started_at": job.started_at,
            "finished_at": job.finished_at, "stage": job.stage or None,
            "branch": job.branch or None,
            "summary": (job.summary or "")[:1000] or None,
            "preview": preview.safe_dict() if preview else None,
            "budget": budget_status,
        }

    def _events(self, job_id, auth, environ):
        self._job_for(job_id, auth)
        try:
            query_cursor = parse_qs(environ.get("QUERY_STRING", "")).get("cursor", [""])[0]
            cursor = int(query_cursor or environ.get("HTTP_LAST_EVENT_ID", "0") or "0")
            if cursor < 0:
                raise ValueError
        except ValueError:
            raise APIError(400, "invalid_cursor", "cursor must be a non-negative integer")
        return _SSE(self.engine_root, job_id, cursor, self.clock)

    def _cancel(self, job_id, auth):
        self._job_for(job_id, auth)
        changed = store.cancel(self.engine_root, job_id)
        return {"job_id": job_id, "cancelled": changed, "state": store.get(self.engine_root, job_id).state}

    def _approval(self, job_id, payload, auth):
        job = self._job_for(job_id, auth)
        approval_id = payload.get("approval_id")
        if approval_id is None:
            candidates = [item for item in approvals.pending(self.engine_root, limit=100)
                          if item.job_id == job_id]
            if len(candidates) != 1:
                raise APIError(400, "invalid_request", "approval_id is required when decisions are ambiguous")
            approval_id = candidates[0].id
        if not isinstance(approval_id, int) or approval_id < 1:
            raise APIError(400, "invalid_request", "approval_id must be a positive integer")
        approved = payload.get("approved")
        if approved is None:
            decision = str(payload.get("decision") or "").lower()
            approved = True if decision in {"approve", "approved"} else False if decision in {"deny", "denied"} else None
        if not isinstance(approved, bool):
            raise APIError(400, "invalid_request", "approved must be boolean or decision must be approve/deny")
        approval = approvals.get(self.engine_root, approval_id)
        if approval.job_id != job_id or approval.requested_by != str(auth.principal):
            raise APIError(404, "not_found", "resource not found")
        decided = approvals.decide(
            self.engine_root, approval_id, approved=approved,
            principal=str(auth.principal), note=str(payload.get("note") or "")[:1000],
        )
        if job.state == store.WAITING_APPROVAL:
            target = store.QUEUED if approved else store.FAILED
            store.transition(self.engine_root, job_id, target, note="approval granted" if approved else "approval denied")
            if approved:
                try: self.spawn(self.engine_root, job_id)
                except Exception: pass
        return {"job_id": job_id, "approval_id": approval.id, "state": decided.state,
                "job_state": store.get(self.engine_root, job_id).state}

    def _preview(self, job_id, auth):
        self._job_for(job_id, auth)
        preview = preview_manager.get_for_job(self.engine_root, job_id)
        if preview is None:
            raise APIError(404, "not_found", "preview not found")
        return preview.safe_dict()

    def _artifacts(self, job_id, auth):
        job = self._job_for(job_id, auth)
        branch = job.branch or None
        preview = preview_manager.get_for_job(self.engine_root, job.id)
        preview_url = preview.url if preview else job.envelope.get("preview_url")
        refs = [
            {"kind": "branch", "ref": branch, "available": bool(branch)},
            {"kind": "diff", "ref": f"/v1/jobs/{job.id}/artifacts/diff", "available": bool(branch)},
            {"kind": "log", "ref": f"/v1/jobs/{job.id}/artifacts/log", "available": bool(job.summary or job.detail)},
            {"kind": "preview", "ref": preview_url, "available": bool(preview_url)},
        ]
        if preview:
            refs[-1]["status"] = preview.status
            refs[-1]["expires_at"] = preview.expires_at
            refs[-1]["commit_sha"] = preview.commit_sha
        return {"job_id": job.id, "artifacts": refs}


class _SSE:
    def __init__(self, engine_root, job_id, cursor, clock, *, poll_seconds=0.5, max_seconds=30.0):
        self.engine_root, self.job_id, self.cursor, self.clock = engine_root, job_id, cursor, clock
        self.poll_seconds, self.max_seconds = poll_seconds, max_seconds

    def iter_bytes(self):
        deadline = self.clock() + self.max_seconds
        cursor = self.cursor
        while self.clock() < deadline:
            page = store.events_page(self.engine_root, self.job_id, after=cursor, limit=100)
            if page["events"]:
                for event in page["events"]:
                    cursor = event["id"]
                    yield self._format(event)
                if page["events"][-1]["event_type"] in {
                    "run.completed", "run.failed", "run.cancelled"
                }:
                    return
            else:
                yield b": keep-alive\n\n"
            time.sleep(self.poll_seconds)

    @staticmethod
    def _format(event):
        payload = json.dumps(event["payload"], separators=(",", ":"), ensure_ascii=False)
        return ("id: %s\nevent: %s\ndata: %s\n\n" % (event["id"], event["event_type"], payload)).encode("utf-8")

    def __iter__(self): return self.iter_bytes()


def create_app(engine_root: Path, authenticator: Authenticator, **kwargs):
    return RemoteAPI(engine_root, authenticator, **kwargs).application
