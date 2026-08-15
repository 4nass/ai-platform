from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import notifications
from core.jobs import store


def _event(engine: Path, event_type="run.completed", payload=None):
    sub = store.submit(
        engine,
        project="/workspace/demo",
        request="fix it",
        envelope={"project_id": "demo", "channel": "telegram", "chat_id": "chat-1"},
    )
    event_id = store.emit_event(engine, sub.id, event_type, payload=payload or {}, run_id=7)
    events = store.events_page(engine, sub.id)["events"]
    return sub.id, next(item for item in events if item["event_type"] == event_type), event_id


def test_render_compact_event_redacts_untrusted_text_and_includes_usage(tmp_path, monkeypatch):
    job_id, event, _ = _event(
        tmp_path,
        payload={"summary": "token sk-secret-123456789 leaked", "url": "javascript:bad"},
    )
    monkeypatch.setattr(
        notifications.telemetry,
        "run_totals",
        lambda *_: {"input_tokens": 10, "output_tokens": 5, "calls": 2},
    )
    message = notifications.render_event(
        tmp_path, event, channel="telegram", recipient="chat-1", max_chars=220
    )
    assert message is not None
    assert message.job_id == job_id
    assert "[REDACTED]" in message.body
    assert "sk-secret" not in message.body
    assert "15 tokens / 2 call(s)" in message.body
    assert "javascript:" not in message.body


def test_non_meaningful_events_are_suppressed(tmp_path):
    _, event, _ = _event(tmp_path, event_type="stage.started")
    assert notifications.render_event(tmp_path, event, channel="signal") is None


def test_preferences_filter_and_duplicate_delivery_is_idempotent(tmp_path):
    _, event, _ = _event(tmp_path, payload={"summary": "done"})
    notifications.configure_preference(
        tmp_path, project_id="demo", channel="telegram", recipient="chat-1",
        min_severity="error",
    )
    sent = []
    assert notifications.notify_event(tmp_path, event, {"telegram": sent.append}) == []
    notifications.configure_preference(
        tmp_path, project_id="demo", channel="telegram", recipient="chat-1",
        min_severity="info",
    )
    first = notifications.notify_event(tmp_path, event, {"telegram": sent.append})
    second = notifications.notify_event(tmp_path, event, {"telegram": sent.append})
    assert first[0].delivered and second[0].delivered
    assert len(sent) == 1


def test_failed_delivery_can_retry_without_changing_run(tmp_path):
    _, event, _ = _event(tmp_path, event_type="run.failed", payload={"summary": "boom"})
    attempts = []
    def sink(_):
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("temporary provider outage")
    clock = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first = notifications.notify_event(tmp_path, event, {"telegram": sink}, now=clock)
    assert not first[0].delivered
    second = notifications.retry_pending(
        tmp_path, {"telegram": sink}, now=clock + timedelta(seconds=5)
    )
    assert second[0].delivered
    assert len(attempts) == 2
    assert store.get(tmp_path, event["job_id"]).state == store.QUEUED
