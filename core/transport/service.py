"""Transport-to-job boundary helpers (issue #44).

The future HTTP/OpenClaw adapter should call this small service after
:func:`core.transport.auth.Authenticator.verify`. It keeps principal and
idempotency propagation in one tested path without opening a network socket or
starting a worker.
"""

from __future__ import annotations

from pathlib import Path

from core.jobs import envelope as envelope_module
from core.jobs import store
from core.transport.auth import AuthenticatedRequest, AuthenticationError


def submit_verified(
    engine_root: Path,
    *,
    project: str,
    project_id: str,
    request: str,
    authenticated: AuthenticatedRequest,
) -> store.Submission:
    """Persist one verified remote submission and nothing else.

    Project path/action authorization remains the caller's registry boundary;
    this function only refuses a mismatch between the authenticated structured
    project id and the id that was resolved to ``project``. It never trusts
    project or identity text from the natural-language request.
    """
    authenticated.require("jobs:submit")
    if not project_id or authenticated.envelope.project_id != project_id:
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
