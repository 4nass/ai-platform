"""Security tests for the authenticated transport boundary (issue #44)."""

from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from core.jobs import store
from core.jobs.envelope import Envelope
from core.transport.service import submit_verified
from core.transport.auth import (
    AuthenticationError,
    Authenticator,
    AuthorizationError,
    ReplayError,
    ReplayStore,
    TransportCredential,
    credential_from_mapping,
)

NOW = 1_700_000_000
PATH = "/v1/jobs"
BODY = b'{"project_id":"ai-platform","request":"run tests"}'
NONCE = "nonce_0123456789"


def _claim_nonce_in_child(database_path, ready, start, result):
    """Claim one nonce after both independent processes are ready."""
    try:
        ready.put(True)
        if not start.wait(timeout=10):
            raise RuntimeError("parent never released concurrent nonce claim")
        replayed = ReplayStore(Path(database_path)).claim(
            key_id="key-current",
            nonce=NONCE,
            body_hash="body-hash",
            expires_at=NOW + 900,
            now=NOW,
        )
        result.put(("ok", replayed))
    except BaseException as exc:
        result.put(("error", f"{type(exc).__name__}: {exc}"))


def _credential(**overrides) -> TransportCredential:
    values = {
        "key_id": "key-current",
        "principal_id": "owner-1",
        "channel": "openclaw",
        "secret": "top-secret",
        "scopes": frozenset({"jobs:submit", "jobs:read"}),
    }
    values.update(overrides)
    return TransportCredential(**values)


def _envelope(**overrides) -> Envelope:
    values = {
        "channel": "openclaw",
        "sender_id": "owner-1",
        "chat_id": "chat-1",
        "message_id": "message-1",
        "sent_at": "2023-11-14T22:13:20+00:00",
        "project_id": "ai-platform",
    }
    values.update(overrides)
    return Envelope(**values)


def _auth(tmp_path: Path, credential: TransportCredential | None = None) -> Authenticator:
    return Authenticator(
        {credential.key_id: credential} if credential else {_credential().key_id: _credential()},
        ReplayStore(tmp_path / "auth.sqlite"),
        clock=lambda: NOW,
    )


def _signature(credential: TransportCredential, *, body: bytes = BODY, nonce: str = NONCE) -> str:
    return credential.sign(
        method="POST", path=PATH, body=body, timestamp=NOW, nonce=nonce
    )


def test_valid_signed_request_establishes_principal_and_scope(tmp_path: Path) -> None:
    credential = _credential()

    request = _auth(tmp_path, credential).verify(
        method="POST",
        path=PATH,
        body=BODY,
        key_id=credential.key_id,
        timestamp=NOW,
        nonce=NONCE,
        signature=_signature(credential),
        envelope=_envelope(),
        scope="jobs:submit",
    )

    assert request.principal.id == "owner-1"
    assert request.principal.channel == "openclaw"
    assert request.scopes == frozenset({"jobs:submit", "jobs:read"})
    assert request.replayed is False


def test_signature_covers_method_path_and_body(tmp_path: Path) -> None:
    credential = _credential()
    auth = _auth(tmp_path, credential)

    with pytest.raises(AuthenticationError, match="signature"):
        auth.verify(
            method="POST",
            path="/v1/jobs/other",
            body=BODY,
            key_id=credential.key_id,
            timestamp=NOW,
            nonce=NONCE,
            signature=_signature(credential),
            envelope=_envelope(),
        )

    with pytest.raises(AuthenticationError, match="signature"):
        auth.verify(
            method="POST",
            path=PATH,
            body=b'{"project_id":"other"}',
            key_id=credential.key_id,
            timestamp=NOW,
            nonce="nonce_abcdefghijk",
            signature=_signature(credential),
            envelope=_envelope(),
        )


def test_principal_and_channel_fields_cannot_be_changed_by_the_body(tmp_path: Path) -> None:
    credential = _credential()

    with pytest.raises(AuthenticationError, match="does not match"):
        _auth(tmp_path, credential).verify(
            method="POST",
            path=PATH,
            body=BODY,
            key_id=credential.key_id,
            timestamp=NOW,
            nonce=NONCE,
            signature=_signature(credential),
            envelope=_envelope(sender_id="attacker"),
        )


def test_stale_future_and_malformed_nonces_are_rejected(tmp_path: Path) -> None:
    credential = _credential()
    auth = _auth(tmp_path, credential)

    for timestamp in (NOW - 901, NOW + 901):
        with pytest.raises(ReplayError, match="replay window"):
            auth.verify(
                method="POST", path=PATH, body=BODY, key_id=credential.key_id,
                timestamp=timestamp, nonce=NONCE, signature=_signature(credential),
                envelope=_envelope(),
            )

    with pytest.raises(ReplayError, match="nonce"):
        auth.verify(
            method="POST", path=PATH, body=BODY, key_id=credential.key_id,
            timestamp=NOW, nonce="short", signature=_signature(credential, nonce="short"),
            envelope=_envelope(),
        )


def test_same_nonce_and_body_is_an_idempotent_retry(tmp_path: Path) -> None:
    credential = _credential()
    auth = _auth(tmp_path, credential)
    first = auth.verify(
        method="POST", path=PATH, body=BODY, key_id=credential.key_id,
        timestamp=NOW, nonce=NONCE, signature=_signature(credential), envelope=_envelope(),
    )
    second = auth.verify(
        method="POST", path=PATH, body=BODY, key_id=credential.key_id,
        timestamp=NOW, nonce=NONCE, signature=_signature(credential), envelope=_envelope(),
    )

    assert first.replayed is False
    assert second.replayed is True


def test_a_future_dated_request_cannot_outlive_its_own_ledger_entry(tmp_path: Path) -> None:
    """The nonce must stay refused for as long as its signature is accepted.

    The skew check is two-sided, so a client may legally date a request into
    the future. If the ledger entry expired at arrival time plus the skew, a
    request dated NOW+899 would be forgotten at NOW+900 while remaining
    verifiable until NOW+1799 — and the captured payload would come back as a
    brand new request rather than a detected replay.
    """
    credential = _credential()
    clock = {"now": NOW}
    auth = Authenticator(
        {credential.key_id: credential},
        ReplayStore(tmp_path / "auth.sqlite"),
        clock=lambda: clock["now"],
    )
    future = NOW + 899
    signed = dict(
        method="POST", path=PATH, body=BODY, key_id=credential.key_id, timestamp=future,
        nonce=NONCE, envelope=_envelope(),
        signature=credential.sign(
            method="POST", path=PATH, body=BODY, timestamp=future, nonce=NONCE
        ),
    )

    assert auth.verify(**signed).replayed is False

    # Every later moment at which this signature still verifies must report a
    # replay rather than a fresh request.
    for offset in (901, 1200, 1798):
        clock["now"] = NOW + offset
        assert auth.verify(**signed).replayed is True, f"replay missed at NOW+{offset}"

    clock["now"] = NOW + 1800
    with pytest.raises(ReplayError, match="replay window"):
        auth.verify(**signed)


def test_nonce_ledger_survives_a_new_authenticator(tmp_path: Path) -> None:
    credential = _credential()
    _auth(tmp_path, credential).verify(
        method="POST", path=PATH, body=BODY, key_id=credential.key_id,
        timestamp=NOW, nonce=NONCE, signature=_signature(credential), envelope=_envelope(),
    )

    restored = _auth(tmp_path, credential).verify(
        method="POST", path=PATH, body=BODY, key_id=credential.key_id,
        timestamp=NOW, nonce=NONCE, signature=_signature(credential), envelope=_envelope(),
    )
    assert restored.replayed is True


def test_nonce_reuse_with_changed_content_is_refused(tmp_path: Path) -> None:
    credential = _credential()
    auth = _auth(tmp_path, credential)
    auth.verify(
        method="POST", path=PATH, body=BODY, key_id=credential.key_id,
        timestamp=NOW, nonce=NONCE, signature=_signature(credential), envelope=_envelope(),
    )

    changed = b'{"project_id":"ai-platform","request":"delete"}'
    with pytest.raises(ReplayError, match="different content"):
        auth.verify(
            method="POST", path=PATH, body=changed, key_id=credential.key_id,
            timestamp=NOW, nonce=NONCE,
            signature=_signature(credential, body=changed), envelope=_envelope(),
        )


def test_rotating_credentials_accepts_overlap_and_rejects_revocation(tmp_path: Path) -> None:
    current = _credential()
    previous = _credential(key_id="key-previous", secret="old-secret")
    auth = Authenticator(
        {current.key_id: current, previous.key_id: previous},
        ReplayStore(tmp_path / "auth.sqlite"),
        clock=lambda: NOW,
    )

    accepted = auth.verify(
        method="POST", path=PATH, body=BODY, key_id=previous.key_id,
        timestamp=NOW, nonce=NONCE, signature=_signature(previous), envelope=_envelope(),
    )
    assert accepted.key_id == "key-previous"

    revoked = _credential(key_id="key-revoked", revoked=True)
    with pytest.raises(AuthenticationError, match="inactive"):
        _auth(tmp_path / "revoked", revoked).verify(
            method="POST", path=PATH, body=BODY, key_id=revoked.key_id,
            timestamp=NOW, nonce="nonce_revoked123", signature=_signature(revoked, nonce="nonce_revoked123"),
            envelope=_envelope(),
        )


def test_scope_is_checked_after_identity_is_verified(tmp_path: Path) -> None:
    credential = _credential(scopes=frozenset({"jobs:read"}))

    with pytest.raises(AuthorizationError, match="jobs:submit"):
        _auth(tmp_path, credential).verify(
            method="POST", path=PATH, body=BODY, key_id=credential.key_id,
            timestamp=NOW, nonce=NONCE, signature=_signature(credential),
            envelope=_envelope(), scope="jobs:submit",
        )


def test_unknown_credentials_and_missing_transport_fields_fail_closed(tmp_path: Path) -> None:
    credential = _credential()
    auth = _auth(tmp_path, credential)
    with pytest.raises(AuthenticationError, match="unknown"):
        auth.verify(
            method="POST", path=PATH, body=BODY, key_id="unknown",
            timestamp=NOW, nonce=NONCE, signature="bad", envelope=_envelope(),
        )

    with pytest.raises(AuthenticationError, match="chat_id"):
        auth.verify(
            method="POST", path=PATH, body=BODY, key_id=credential.key_id,
            timestamp=NOW, nonce="nonce_missingchat", signature=_signature(credential, nonce="nonce_missingchat"),
            envelope=_envelope(chat_id=""),
        )


def test_credential_mapping_requires_explicit_scopes() -> None:
    credential = credential_from_mapping(
        {
            "key_id": "key-1",
            "principal_id": "owner-1",
            "channel": "openclaw",
            "secret": "secret",
            "scopes": ["jobs:read"],
        }
    )
    assert credential.scopes == frozenset({"jobs:read"})

    with pytest.raises(ValueError, match="scopes"):
        credential_from_mapping({"key_id": "key-1", "principal_id": "owner", "channel": "x", "secret": "s", "scopes": "jobs:read"})



def test_verified_principal_and_idempotency_are_persisted_on_the_job(tmp_path: Path) -> None:
    credential = _credential()
    auth = _auth(tmp_path, credential)
    authenticated = auth.verify(
        method="POST", path=PATH, body=BODY, key_id=credential.key_id,
        timestamp=NOW, nonce=NONCE, signature=_signature(credential), envelope=_envelope(),
        scope="jobs:submit",
    )

    first = submit_verified(
        tmp_path, project="/allowlisted/ai-platform", project_id="ai-platform",
        request="run tests", body=BODY, authenticated=authenticated,
    )
    second = submit_verified(
        tmp_path, project="/allowlisted/ai-platform", project_id="ai-platform",
        request="run tests", body=BODY, authenticated=authenticated,
    )

    assert first.created is True
    assert second == type(first)(id=first.id, created=False)
    job = store.get(tmp_path, first.id)
    assert job.principal == "openclaw:owner-1"
    assert job.envelope["project_id"] == "ai-platform"


def test_verified_submission_rejects_a_project_id_mismatch(tmp_path: Path) -> None:
    credential = _credential()
    auth = _auth(tmp_path, credential)
    authenticated = auth.verify(
        method="POST", path=PATH, body=BODY, key_id=credential.key_id,
        timestamp=NOW, nonce=NONCE, signature=_signature(credential), envelope=_envelope(),
    )

    with pytest.raises(AuthenticationError, match="project"):
        submit_verified(
            tmp_path, project="/allowlisted/other", project_id="other",
            request="run tests", body=BODY, authenticated=authenticated,
        )



def test_verified_submission_rejects_text_not_present_in_signed_body(tmp_path: Path) -> None:
    credential = _credential()
    auth = _auth(tmp_path, credential)
    authenticated = auth.verify(
        method="POST", path=PATH, body=BODY, key_id=credential.key_id,
        timestamp=NOW, nonce=NONCE, signature=_signature(credential), envelope=_envelope(),
    )

    with pytest.raises(AuthenticationError, match="text"):
        submit_verified(
            tmp_path, project="/allowlisted/ai-platform", project_id="ai-platform",
            request="different request", body=BODY, authenticated=authenticated,
        )

def test_nonce_claim_is_atomic_across_independent_processes(tmp_path: Path) -> None:
    """The durable ledger, not the in-process lock, decides the winner."""
    context = multiprocessing.get_context("spawn")
    ready, start, result = context.Queue(), context.Event(), context.Queue()
    processes = [
        context.Process(
            target=_claim_nonce_in_child,
            args=(str(tmp_path / "auth.sqlite"), ready, start, result),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    try:
        assert ready.get(timeout=10) is True
        assert ready.get(timeout=10) is True
        start.set()
        outcomes = [result.get(timeout=10), result.get(timeout=10)]
    finally:
        start.set()
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)

    assert all(process.exitcode == 0 for process in processes)
    assert sorted(outcomes) == [("ok", False), ("ok", True)]
