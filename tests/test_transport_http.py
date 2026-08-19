from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.jobs import store
from core.orchestrator.registry import Project
from core.actions.executor import PreviewDeployPlan, PREVIEW_DEPLOY
from core.previews.manager import PreviewDeployment, PreviewManager
from core.transport.auth import Authenticator, ReplayStore, TransportCredential
from core.transport.http import RemoteAPI


NOW = 1_700_000_000


def credential():
    return TransportCredential(
        key_id="key-1", principal_id="owner-1", channel="openclaw",
        secret="secret", scopes=frozenset({"jobs:submit", "jobs:read", "jobs:cancel", "jobs:approve"}),
    )


def request(app, method, path, body=b"", *, credential_obj=None, nonce="nonce_1234567890",
            channel="openclaw", sender="owner-1", chat="chat-1", message="message-1",
            query="", extra=None):
    c = credential_obj or credential()
    headers = {
        "HTTP_X_API_KEY": c.key_id,
        "HTTP_X_TIMESTAMP": str(NOW),
        "HTTP_X_NONCE": nonce,
        "HTTP_X_CHANNEL": channel,
        "HTTP_X_SENDER_ID": sender,
        "HTTP_X_CHAT_ID": chat,
        "HTTP_X_MESSAGE_ID": message,
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
    }
    headers["HTTP_X_SIGNATURE"] = c.sign(
        method=method, path=path, body=body, timestamp=NOW, nonce=nonce
    )
    if extra:
        headers.update(extra)
    out = {}
    def start(status, response_headers):
        out["status"] = status
        out["headers"] = dict(response_headers)
    result = app(headers, start)
    if "text/event-stream" in out.get("headers", {}).get("Content-Type", ""):
        data = next(result)
        return out, data
    data = b"".join(result)
    return out, json.loads(data)


@pytest.fixture
def api(tmp_path: Path, monkeypatch):
    c = credential()
    auth = Authenticator({c.key_id: c}, ReplayStore(tmp_path / "transport.sqlite"), clock=lambda: NOW)
    monkeypatch.setattr(
        "core.transport.http.registry.resolve",
        lambda *args, **kwargs: SimpleNamespace(path=tmp_path / "target"),
    )
    (tmp_path / "target").mkdir()
    return RemoteAPI(tmp_path, auth, spawn=lambda root, job_id: 123), c


def test_submit_is_authenticated_allowlisted_and_idempotent(api):
    app, c = api
    body = json.dumps({
        "project_id": "demo", "request": "run tests",
        "envelope": {"channel": "openclaw", "sender_id": "owner-1",
                     "chat_id": "chat-1", "message_id": "message-1"},
    }, separators=(",", ":")).encode()
    first_status, first = request(app.application, "POST", "/v1/jobs", body, credential_obj=c)
    second_status, second = request(app.application, "POST", "/v1/jobs", body, credential_obj=c,
                                    nonce="nonce_1234567891")
    assert first_status["status"] == "200 OK"
    assert first["created"] is True
    assert second["created"] is False
    assert second["job_id"] == first["job_id"]
    job = store.get(app.engine_root, first["job_id"])
    assert job.principal == "openclaw:owner-1"


def test_status_and_events_do_not_disclose_other_principals(api):
    app, c = api
    job_id = store.submit(
        app.engine_root, project="/safe", request="x", principal="openclaw:other",
        idempotency_key="", envelope={"project_id": "demo"},
    ).id
    status, value = request(app.application, "GET", f"/v1/jobs/{job_id}", credential_obj=c)
    assert status["status"] == "404 Not Found"
    assert value["error"]["code"] == "not_found"


def test_cancel_is_idempotent_and_audited(api):
    app, c = api
    job_id = store.submit(
        app.engine_root, project="/safe", request="x", principal="openclaw:owner-1",
        idempotency_key="", envelope={"project_id": "demo"},
    ).id
    first_status, first = request(app.application, "POST", f"/v1/jobs/{job_id}/cancel",
                                  b"{}", credential_obj=c, nonce="nonce_cancel_123456")
    second_status, second = request(app.application, "POST", f"/v1/jobs/{job_id}/cancel",
                                    b"{}", credential_obj=c, nonce="nonce_cancel_1234567")
    assert first_status["status"] == "200 OK"
    assert first["cancelled"] is True
    assert second["cancelled"] is False
    assert store.get(app.engine_root, job_id).state == store.CANCELLED


def test_sse_replays_with_stable_id(api):
    app, c = api
    job_id = store.submit(
        app.engine_root, project="/safe", request="x", principal="openclaw:owner-1",
        envelope={"project_id": "demo"},
    ).id
    status, data = request(app.application, "GET", f"/v1/jobs/{job_id}/events",
                           credential_obj=c, query="cursor=0")
    assert status["status"] == "200 OK"
    assert data.startswith(b"id: 1\nevent: run.queued\n")

def test_approval_is_principal_bound_and_requeues_waiting_job(api):
    app, c = api
    job_id = store.submit(
        app.engine_root, project="/safe", request="x", principal="openclaw:owner-1",
        envelope={"project_id": "demo"},
    ).id
    store.claim(app.engine_root, job_id, worker_pid=1)
    store.transition(app.engine_root, job_id, store.WAITING_APPROVAL)
    from core.jobs import approvals
    approval = approvals.request(
        app.engine_root, action="budget", target=f"job-{job_id}",
        detail={"extra_tokens": 10}, job_id=job_id, requested_by="openclaw:owner-1",
    )
    body = json.dumps({"approval_id": approval.id, "approved": True},
                      separators=(",", ":")).encode()
    status, value = request(
        app.application, "POST", f"/v1/jobs/{job_id}/approval", body,
        credential_obj=c, nonce="nonce_approval_12345",
    )
    assert status["status"] == "200 OK"
    assert value["state"] == "approved"
    assert store.get(app.engine_root, job_id).state == store.QUEUED


def test_malformed_or_oversized_requests_fail_without_disclosure(api):
    app, c = api
    status, value = request(
        app.application, "POST", "/v1/jobs", b"not-json", credential_obj=c,
        nonce="nonce_bad_json_123",
    )
    assert status["status"] == "400 Bad Request"
    assert value["error"]["code"] == "invalid_json"


def test_preview_status_and_artifact_link_are_principal_bound(api, tmp_path):
    app, c = api
    job_id = store.submit(
        app.engine_root, project="/safe", request="x", principal="openclaw:owner-1",
        envelope={"project_id": "demo"},
    ).id

    class Provider:
        def deploy(self, plan, context):
            return PreviewDeployment(
                "ci", "deployment-1", "https://run.preview.example.com/",
                plan.commit_sha, "provider", "https://logs.preview.example.com/1",
            )
        def cleanup(self, preview, context):
            from core.previews.manager import PreviewCleanup
            return PreviewCleanup(True, "cleaned")

    project = Project(
        id="demo", path=tmp_path, remote="https://git.example.com/demo.git",
        base_branch="main", allowed_actions=(PREVIEW_DEPLOY,),
    )
    preview = PreviewManager(
        app.engine_root, Provider(), allowed_hosts=("preview.example.com",)
    ).deploy(
        PreviewDeployPlan(
            project_id="demo", service="web", environment="pr-1",
            commit_sha="a" * 40, ttl_seconds=60,
        ),
        project=project, principal="openclaw:owner-1",
        request_id="api-preview", job_id=job_id, run_id=3,
    )

    status, value = request(
        app.application, "GET", f"/v1/jobs/{job_id}/preview",
        credential_obj=c, nonce="nonce_preview_status_1",
    )
    assert status["status"] == "200 OK"
    assert value["preview_id"] == preview.id
    assert value["commit_sha"] == "a" * 40

    status, value = request(
        app.application, "GET", f"/v1/jobs/{job_id}/artifacts",
        credential_obj=c, nonce="nonce_preview_artifacts_1",
    )
    assert status["status"] == "200 OK"
    item = next(item for item in value["artifacts"] if item["kind"] == "preview")
    assert item["ref"] == preview.url
    assert item["available"] is True

def test_access_logs_are_useful_without_recording_request_secrets(api, caplog) -> None:
    app, c = api
    caplog.set_level(logging.INFO, logger="ai_platform.transport.access")
    secret_path = "/v1/not-a-route/sk-test-12345678901234567890"
    status, value = request(app.application, "GET", secret_path, credential_obj=c)

    assert status["status"] == "404 Not Found"
    assert value["error"]["code"] == "not_found"
    messages = [record.getMessage() for record in caplog.records]
    assert any("status=404" in message and "route=invalid" in message for message in messages)
    assert all("sk-test-12345678901234567890" not in message for message in messages)


def test_access_logs_authentication_failures(api, caplog) -> None:
    app, c = api
    caplog.set_level(logging.WARNING, logger="ai_platform.transport.access")
    status, value = request(
        app.application,
        "GET",
        "/v1/jobs/1",
        credential_obj=c,
        extra={"HTTP_X_SIGNATURE": "not-a-valid-signature"},
    )

    assert status["status"] == "401 Unauthorized"
    assert value["error"]["code"] == "unauthorized"
    assert any(
        "status=401" in record.getMessage() and "outcome=authentication_failed" in record.getMessage()
        for record in caplog.records
    )
