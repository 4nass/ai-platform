"""Tests for core.jobs.envelope — identity and delivery (issue #26).

Two things a prompt must never decide: who is speaking, and whether this is
the same request as last time. These check that neither can be influenced by
the request text, and that a channel with nothing to key on says so instead of
inventing something.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.jobs.envelope import (
    Envelope,
    Principal,
    ReplayError,
    payload_fingerprint,
)


def _delivered(**overrides) -> Envelope:
    base = {
        "channel": "whatsapp",
        "sender_id": "+33600000000",
        "chat_id": "chat-1",
        "message_id": "msg-1",
    }
    return Envelope(**{**base, **overrides})


# --- identity ---


def test_a_local_principal_comes_from_the_process_not_the_request(monkeypatch) -> None:
    """A local CLI has already been authenticated by the operating system.
    Asking for identity would add a field to lie in without adding a check."""
    monkeypatch.setattr("getpass.getuser", lambda: "anass")

    principal = Principal.local()

    assert principal.id == "anass"
    assert principal.channel == "cli"
    assert str(principal) == "cli:anass"


def test_a_principal_survives_an_unavailable_os_user(monkeypatch) -> None:
    monkeypatch.setattr("getpass.getuser", lambda: (_ for _ in ()).throw(OSError()))

    assert Principal.local().id == "unknown"


def test_a_principal_is_channel_scoped(monkeypatch) -> None:
    """The same id on two channels is two different principals: `+33600000000`
    on WhatsApp and on SMS are not established by the same authority."""
    assert str(Principal(id="x", channel="whatsapp")) != str(Principal(id="x", channel="sms"))


# --- the idempotency key ---


def test_the_key_is_derived_from_transport_fields(monkeypatch) -> None:
    assert _delivered().idempotency_key


def test_the_same_delivery_produces_the_same_key() -> None:
    assert _delivered().idempotency_key == _delivered().idempotency_key


def test_the_key_ignores_the_request_text_entirely() -> None:
    """Deriving it from the prompt would make two genuinely different asks that
    happen to read the same one request, and one request rephrased by a
    retrying client two — exactly backwards. The envelope carries no request,
    which is how that is enforced rather than merely intended."""
    assert not hasattr(_delivered(), "request")


def test_a_different_message_is_a_different_key() -> None:
    assert _delivered().idempotency_key != _delivered(message_id="msg-2").idempotency_key


def test_the_same_message_id_in_another_chat_is_a_different_key() -> None:
    """Message ids are only unique within their conversation."""
    assert _delivered().idempotency_key != _delivered(chat_id="chat-2").idempotency_key


def test_the_same_message_id_from_another_sender_is_a_different_key() -> None:
    assert _delivered().idempotency_key != _delivered(sender_id="+33699999999").idempotency_key


def test_a_channel_with_no_message_id_has_no_key() -> None:
    """Empty rather than invented: every `ai-platform submit` is a deliberate,
    separate act. A made-up key would either collapse two genuine requests or
    be useless — both worse than admitting the channel cannot support the
    guarantee."""
    assert Envelope().idempotency_key == ""


def test_the_key_does_not_carry_the_identifiers_in_the_clear() -> None:
    """It is stored, indexed and logged, and a chat id is a phone number."""
    key = _delivered().idempotency_key

    assert "+33600000000" not in key
    assert "chat-1" not in key


# --- the replay window ---


def _sent(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def test_a_fresh_submission_passes() -> None:
    _delivered(sent_at=_sent(5)).check_freshness()


def test_a_stale_submission_is_refused() -> None:
    with pytest.raises(ReplayError, match="replay window"):
        _delivered(sent_at=_sent(3600)).check_freshness()


def test_a_channel_that_declares_no_time_is_unchecked() -> None:
    """Absent means unchecked, which is the honest state for a channel that
    cannot sign a timestamp — not a silent pass presented as verification."""
    _delivered(sent_at="").check_freshness()


def test_an_unparseable_timestamp_is_refused_rather_than_ignored() -> None:
    with pytest.raises(ReplayError, match="Unparseable"):
        _delivered(sent_at="last tuesday").check_freshness()


def test_a_naive_timestamp_is_read_as_utc() -> None:
    """Refusing it outright would reject well-formed callers over a formatting
    detail; guessing local time would silently widen the window."""
    naive = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    _delivered(sent_at=naive).check_freshness()


# --- the payload fingerprint ---


def test_the_same_request_fingerprints_the_same() -> None:
    a = payload_fingerprint(project="/repo", request="add oauth", envelope=_delivered())
    b = payload_fingerprint(project="/repo", request="add oauth", envelope=_delivered())

    assert a == b


def test_a_different_request_fingerprints_differently() -> None:
    a = payload_fingerprint(project="/repo", request="add oauth", envelope=_delivered())
    b = payload_fingerprint(project="/repo", request="delete everything", envelope=_delivered())

    assert a != b


def test_a_different_target_fingerprints_differently() -> None:
    """The same words against another repository is another request."""
    a = payload_fingerprint(project="/repo", request="add oauth", envelope=_delivered())
    b = payload_fingerprint(project="/other", request="add oauth", envelope=_delivered())

    assert a != b


# --- persistence shape ---


def test_an_envelope_round_trips_through_its_stored_form() -> None:
    original = _delivered(session_id="s1", dirty_policy="reject", project_id="mine")

    restored = Envelope.from_dict(original.as_dict())

    assert restored == original


def test_unknown_stored_fields_survive_as_extra() -> None:
    """A row written by a newer engine must not lose fields when an older one
    reads it — the envelope is the audit record of what was submitted."""
    restored = Envelope.from_dict({"channel": "cli", "future_field": 1})

    assert restored.extra == {"future_field": 1}
    assert restored.as_dict()["future_field"] == 1
