import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import openclaw
from core.jobs import approvals, store
from core.jobs.envelope import Envelope, Principal
from core.transport.auth import AuthenticatedRequest, AuthorizationError


SCOPES = frozenset({"jobs:submit", "jobs:read", "jobs:cancel", "jobs:approve"})


def _signed(arguments, *, project="demo", message="msg-1", principal="owner-1"):
    body = json.dumps(arguments, separators=(",", ":")).encode()
    envelope = Envelope(
        channel="openclaw", sender_id=principal, chat_id="chat-1",
        message_id=message, project_id=project,
    )
    auth = AuthenticatedRequest(
        principal=Principal(principal, "openclaw"),
        scopes=SCOPES,
        envelope=envelope,
        key_id="key-1", nonce="nonce-1234567890", timestamp=1,
        body_hash=hashlib.sha256(body).hexdigest(),
    )
    return auth, body


@pytest.fixture
def tools(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setattr(
        openclaw.registry, "resolve",
        lambda *args, **kwargs: SimpleNamespace(id="demo", path=target),
    )
    spawned = []
    return openclaw.OpenClawTools(tmp_path, spawn=lambda root, job_id: spawned.append(job_id) or 123), spawned


def test_schemas_are_versioned_and_typed():
    schema = openclaw.tool_schemas()
    assert schema["version"] == "v1"
    assert {item["name"] for item in schema["tools"]} == set(openclaw.TOOL_MODELS)
    submit = next(item for item in schema["tools"] if item["name"] == "engineering_submit")
    assert submit["input_schema"]["properties"]["project"]["type"] == "string"


def test_submit_returns_durable_id_and_is_idempotent(tools):
    adapter, spawned = tools
    args = {"project_id": "demo", "request": "run tests"}
    auth, body = _signed(args)
    first = adapter.call("engineering_submit", {"project": "demo", "request": "run tests"},
                         authenticated=auth, signed_body=body)
    second = adapter.call("engineering_submit", {"project": "demo", "request": "run tests"},
                          authenticated=auth, signed_body=body)
    assert first["run_id"] == second["run_id"]
    assert first["created"] is True and second["created"] is False
    assert spawned == [first["run_id"]]
    assert store.get(adapter.engine_root, first["run_id"]).principal == "openclaw:owner-1"


def test_status_events_diff_cancel_survive_new_adapter_instance(tools):
    adapter, _ = tools
    job_id = store.submit(
        adapter.engine_root, project="/safe", request="x",
        principal="openclaw:owner-1", envelope={"project_id": "demo"},
    ).id
    restarted = openclaw.OpenClawTools(adapter.engine_root, spawn=lambda *_: 1)
    for name in ("engineering_status", "engineering_diff"):
        args = {"run_id": job_id}
        auth, body = _signed(args, message=f"{name}-1")
        value = restarted.call(name, args, authenticated=auth, signed_body=body)
        assert value["run_id"] == job_id
    events_args = {"run_id": job_id, "cursor": 0, "limit": 10}
    auth, body = _signed(events_args, message="events-1")
    events = restarted.call(
        "engineering_events", events_args,
        authenticated=auth, signed_body=body,
    )
    assert events["events"][0]["event_type"] == "run.queued"
    cancel_args = {"run_id": job_id}
    auth, body = _signed(cancel_args, message="cancel-1")
    cancelled = restarted.call("engineering_cancel", {"run_id": job_id},
                               authenticated=auth, signed_body=body)
    assert cancelled["state"] == store.CANCELLED


def test_approval_is_scoped_and_can_requeue(tools):
    adapter, spawned = tools
    job_id = store.submit(
        adapter.engine_root, project="/safe", request="x",
        principal="openclaw:owner-1", envelope={"project_id": "demo"},
    ).id
    store.claim(adapter.engine_root, job_id, worker_pid=1)
    store.transition(adapter.engine_root, job_id, store.WAITING_APPROVAL)
    approval = approvals.request(
        adapter.engine_root, action="budget", target=f"job-{job_id}",
        detail={"extra_tokens": 1}, job_id=job_id, requested_by="openclaw:owner-1",
    )
    approval_args = {"run_id": job_id, "approval_id": approval.id, "decision": "approve"}
    auth, body = _signed(approval_args, message="approval-1")
    result = adapter.call(
        "engineering_approve",
        approval_args,
        authenticated=auth, signed_body=body,
    )
    assert result["job_state"] == store.QUEUED
    assert spawned == [job_id]


def test_principal_cannot_read_another_run(tools):
    adapter, _ = tools
    job_id = store.submit(
        adapter.engine_root, project="/safe", request="x",
        principal="openclaw:other", envelope={"project_id": "demo"},
    ).id
    status_args = {"run_id": job_id}
    auth, body = _signed(status_args, principal="owner-1", message="foreign-1")
    with pytest.raises(openclaw.ToolError) as exc:
        adapter.call("engineering_status", status_args,
                     authenticated=auth, signed_body=body)
    assert exc.value.code == "run_not_found"


def test_signed_body_mismatch_is_rejected(tools):
    adapter, _ = tools
    auth, body = _signed({"project_id": "demo", "request": "one"})
    with pytest.raises(Exception):
        adapter.call(
            "engineering_submit", {"project": "demo", "request": "two"},
            authenticated=auth, signed_body=body,
        )
