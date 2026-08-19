"""Transport-to-job boundary helpers (issue #44).

The future HTTP/OpenClaw adapter should call this small service after
:func:`core.transport.auth.Authenticator.verify`. It keeps principal and
idempotency propagation in one tested path without opening a network socket or
starting a worker.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from core.jobs import envelope as envelope_module
from core.jobs import store
from core.transport.auth import AuthenticatedRequest, AuthenticationError


class OwnedResourceNotFound(Exception):
    """A resource is missing or belongs to another principal.

    Callers intentionally receive one outcome for both cases, so probing a job
    id cannot disclose another principal's work.
    """


def job_for_principal(engine_root: Path, job_id: int, principal: object) -> store.Job:
    """Load a job only when it belongs to the verified principal."""
    try:
        job = store.get(engine_root, job_id)
    except store.JobError:
        raise OwnedResourceNotFound from None
    if job.principal != str(principal):
        raise OwnedResourceNotFound
    return job


def submit_verified(
    engine_root: Path,
    *,
    project: str,
    project_id: str,
    request: str,
    body: bytes,
    authenticated: AuthenticatedRequest,
) -> store.Submission:
    """Persist one verified remote submission and nothing else.

    Project path/action authorization remains the caller's registry boundary;
    this function only refuses a mismatch between the authenticated structured
    project id and the id that was resolved to ``project``. It also checks the
    request text against the exact signed JSON body. It never trusts project or
    identity text from the natural-language request.
    """
    authenticated.require("jobs:submit")
    if hashlib.sha256(body).hexdigest() != authenticated.body_hash:
        raise AuthenticationError("signed request body does not match the authenticated request")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AuthenticationError("signed request body is not valid JSON") from None
    if not isinstance(payload, dict) or payload.get("request") != request:
        raise AuthenticationError("signed request text does not match the submitted request")
    if not project_id or payload.get("project_id") != project_id:
        raise AuthenticationError("signed request project does not match the requested project")
    if authenticated.envelope.project_id != project_id:
        raise AuthenticationError("signed envelope project does not match the requested project")

    principal = authenticated.principal
    envelope = authenticated.envelope
    return store.submit(
        engine_root,
        project=project,
        request=request,
        channel=principal.channel,
        submitted_by=principal.display,
        principal=str(principal),
        envelope=envelope.as_dict(),
        idempotency_key=envelope.idempotency_key,
        payload_hash=envelope_module.payload_fingerprint(
            project=project, request=request, envelope=envelope
        ),
    )
