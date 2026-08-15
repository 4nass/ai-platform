"""Channel-neutral mobile notifications with durable idempotent delivery (#42).

OpenClaw or another gateway owns the concrete Signal/WhatsApp/Telegram client.
This module owns the stable event-to-message contract, preference filtering,
safe rendering and the durable outbox. A failed notification is never allowed
to alter the engineering job.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol

from core.jobs import store
from core.telemetry import store as telemetry

DB_PATH = Path("notifications.sqlite")
BUSY_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_CHARS = 1200
DEFAULT_RETRY_SECONDS = 5
MAX_RETRY_SECONDS = 15 * 60
CHANNEL_LIMITS = {"signal": 6000, "whatsapp": 4096, "telegram": 4096, "browser": 12000}
SEVERITY = {"info": 10, "warning": 20, "error": 30, "critical": 40}

EVENT_POLICY = {
    "approval.required": ("warning", "Approval needed"),
    "run.failed": ("error", "Run failed"),
    "preview.failed": ("error", "Preview failed"),
    "preview.ready": ("info", "Preview ready"),
    "run.completed": ("info", "Run completed"),
}
SECRET_RE = re.compile(
    r"(?i)(?:bearer\s+|(?:sk|ghp|github_pat|xox[baprs]-)[A-Za-z0-9_./+=-]{8,}"
    r"|(?:api[_-]?key|token|secret|password|authorization)\s*[:=]\s*\S+)"
)
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

SCHEMA = """
CREATE TABLE IF NOT EXISTS notification_preferences (
  id INTEGER PRIMARY KEY,
  project_id TEXT NOT NULL,
  channel TEXT NOT NULL,
  recipient TEXT NOT NULL,
  min_severity INTEGER NOT NULL DEFAULT 10,
  events TEXT NOT NULL DEFAULT '[]',
  enabled INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL,
  UNIQUE(project_id, channel, recipient)
);
CREATE INDEX IF NOT EXISTS idx_notification_preferences_project
  ON notification_preferences(project_id, channel);
CREATE TABLE IF NOT EXISTS notification_deliveries (
  id INTEGER PRIMARY KEY,
  event_id INTEGER NOT NULL,
  job_id INTEGER NOT NULL,
  project_id TEXT NOT NULL,
  channel TEXT NOT NULL,
  recipient TEXT NOT NULL,
  event_type TEXT NOT NULL,
  severity INTEGER NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL,
  delivered_at TEXT,
  last_error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(event_id, channel, recipient)
);
CREATE INDEX IF NOT EXISTS idx_notification_delivery_due
  ON notification_deliveries(status, next_attempt_at);
"""

@dataclass(frozen=True)
class Preference:
    project_id: str
    channel: str
    recipient: str
    min_severity: int = SEVERITY["info"]
    events: tuple[str, ...] = ()
    enabled: bool = True

@dataclass(frozen=True)
class Notification:
    event_id: int
    job_id: int
    project_id: str
    channel: str
    recipient: str
    event_type: str
    severity: str
    title: str
    body: str
    details_url: str = ""

@dataclass(frozen=True)
class Delivery:
    id: int
    event_id: int
    job_id: int
    channel: str
    recipient: str
    status: str
    attempts: int
    last_error: str

@dataclass(frozen=True)
class DeliveryResult:
    delivery: Delivery
    delivered: bool

class Sink(Protocol):
    def send(self, notification: Notification) -> None: ...

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _connect(engine_root: Path):
    con = sqlite3.connect(Path(engine_root) / DB_PATH, timeout=BUSY_TIMEOUT_SECONDS)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con

def _safe(value: object, limit: int = 500) -> str:
    text = CONTROL_RE.sub("", str(value or "")).strip()
    text = SECRET_RE.sub("[REDACTED]", text)
    return text[:limit]

def _safe_url(value: object) -> str:
    url = _safe(value, 200)
    if url.startswith("https://") or url.startswith("/v1/"):
        return url
    return ""

def _severity_number(value: str | int) -> int:
    if isinstance(value, int):
        return value
    try:
        return SEVERITY[str(value).lower()]
    except KeyError:
        raise ValueError("severity must be info, warning, error or critical") from None

def configure_preference(
    engine_root: Path, *, project_id: str, channel: str, recipient: str,
    min_severity: str | int = "info", events: tuple[str, ...] = (), enabled: bool = True,
) -> Preference:
    if not project_id or not channel or not recipient:
        raise ValueError("project_id, channel and recipient are required")
    channel = channel.lower()
    if channel not in CHANNEL_LIMITS:
        raise ValueError(f"unsupported notification channel: {channel}")
    event_names = tuple(dict.fromkeys(events))
    unknown = set(event_names) - set(EVENT_POLICY)
    if unknown:
        raise ValueError(f"unsupported notification event: {sorted(unknown)}")
    pref = Preference(project_id, channel, recipient, _severity_number(min_severity), event_names, enabled)
    with _connect(engine_root) as con:
        con.execute(
            "INSERT INTO notification_preferences(project_id,channel,recipient,min_severity,events,enabled,updated_at)"
            " VALUES(?,?,?,?,?,?,?) ON CONFLICT(project_id,channel,recipient) DO UPDATE SET"
            " min_severity=excluded.min_severity,events=excluded.events,enabled=excluded.enabled,"
            " updated_at=excluded.updated_at",
            (pref.project_id, pref.channel, pref.recipient, pref.min_severity,
             json.dumps(pref.events), int(pref.enabled), _now()),
        )
        con.commit()
    return pref

def preferences(engine_root: Path, *, project_id: str, channel: str | None = None) -> list[Preference]:
    query = "SELECT * FROM notification_preferences WHERE project_id=?"
    params: list[object] = [project_id]
    if channel:
        query += " AND channel=?"
        params.append(channel.lower())
    query += " ORDER BY channel, recipient"
    with _connect(engine_root) as con:
        rows = con.execute(query, params).fetchall()
    out = []
    for row in rows:
        try:
            events = tuple(json.loads(row["events"] or "[]"))
        except (TypeError, json.JSONDecodeError):
            events = ()
        out.append(Preference(row["project_id"], row["channel"], row["recipient"],
                              row["min_severity"], events, bool(row["enabled"])))
    return out

def _default_preference(job) -> list[Preference]:
    envelope = job.envelope
    channel = str(envelope.get("channel") or "").lower()
    recipient = str(envelope.get("chat_id") or envelope.get("sender_id") or "")
    if channel not in CHANNEL_LIMITS or not recipient:
        return []
    return [Preference(job.envelope.get("project_id") or job.project, channel, recipient)]

def _matches(pref: Preference, event_type: str, severity: str) -> bool:
    return pref.enabled and _severity_number(severity) >= pref.min_severity and (
        not pref.events or event_type in pref.events
    )

def _usage(engine_root: Path, run_id: int | None) -> str:
    if not run_id:
        return ""
    try:
        totals = telemetry.run_totals(engine_root, run_id)
    except Exception:
        return ""
    tokens = int(totals.get("input_tokens", 0) or 0) + int(totals.get("output_tokens", 0) or 0)
    calls = int(totals.get("calls", 0) or 0)
    if not tokens and not calls:
        return ""
    return f"Usage: {tokens:,} tokens / {calls} call(s)"

def render_event(
    engine_root: Path, event: dict, *, channel: str, recipient: str = "",
    details_url: str = "", max_chars: int | None = None,
) -> Notification | None:
    event_type = str(event.get("event_type") or "")
    policy = EVENT_POLICY.get(event_type)
    if policy is None:
        return None
    channel = channel.lower()
    if channel not in CHANNEL_LIMITS:
        raise ValueError(f"unsupported notification channel: {channel}")
    job_id = int(event.get("job_id") or 0)
    job = store.get(engine_root, job_id)
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    severity, title = policy
    project = _safe(job.envelope.get("project_id") or job.project or "project", 120)
    summary = _safe(payload.get("summary") or job.summary or job.detail or event.get("note"), 500)
    branch = _safe(payload.get("branch") or job.branch, 160)
    lines = [f"{title} - {project}", f"Run #{job_id} - state: {_safe(job.state, 40)}"]
    if summary:
        lines.append(f"Summary: {summary}")
    if branch:
        lines.append(f"Branch: {branch}")
    usage = _usage(engine_root, event.get("run_id") or job.run_id)
    if usage:
        lines.append(usage)
    if event_type == "approval.required":
        lines.append(f"Next: approve or deny job #{job_id}.")
    elif event_type in {"run.failed", "preview.failed"}:
        lines.append(f"Next: inspect status/events for job #{job_id}.")
    elif event_type == "preview.ready":
        lines.append("Next: validate the authenticated preview.")
    else:
        lines.append("Next: review the branch and preview.")
    safe_url = _safe_url(details_url or payload.get("details_url") or payload.get("url"))
    if safe_url:
        lines.append(f"Details: {safe_url}")
    text = "\n".join(lines)
    limit = min(max_chars or DEFAULT_MAX_CHARS, CHANNEL_LIMITS[channel])
    if len(text) > limit:
        text = text[: max(0, limit - 32)].rstrip() + "\n & Full details in authenticated view."
    return Notification(int(event.get("id") or 0), job_id, project, channel, recipient,
                        event_type, severity, title, text, safe_url)

def enqueue(engine_root: Path, notification: Notification, *, now: str | None = None) -> Delivery:
    now = now or _now()
    with _connect(engine_root) as con:
        con.execute(
            "INSERT INTO notification_deliveries(event_id,job_id,project_id,channel,recipient,event_type,severity,title,body,next_attempt_at,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(event_id,channel,recipient) DO NOTHING",
            (notification.event_id, notification.job_id, notification.project_id, notification.channel,
             notification.recipient, notification.event_type, _severity_number(notification.severity),
             notification.title, notification.body, now, now),
        )
        row = con.execute(
            "SELECT * FROM notification_deliveries WHERE event_id=? AND channel=? AND recipient=?",
            (notification.event_id, notification.channel, notification.recipient),
        ).fetchone()
        con.commit()
    return Delivery(row["id"], row["event_id"], row["job_id"], row["channel"], row["recipient"],
                    row["status"], row["attempts"], row["last_error"])

def _delivery(row) -> Delivery:
    return Delivery(row["id"], row["event_id"], row["job_id"], row["channel"], row["recipient"],
                    row["status"], row["attempts"], row["last_error"])

def deliver(
    engine_root: Path, notification: Notification, sink: Sink | Callable[[Notification], None],
    *, now: datetime | None = None, max_attempts: int = 8,
) -> DeliveryResult:
    current = now or datetime.now(timezone.utc)
    delivery = enqueue(engine_root, notification, now=current.isoformat())
    if delivery.status == "delivered":
        return DeliveryResult(delivery, True)
    with _connect(engine_root) as con:
        row = con.execute("SELECT * FROM notification_deliveries WHERE id=?", (delivery.id,)).fetchone()
        due = datetime.fromisoformat(row["next_attempt_at"])
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        if due > current:
            return DeliveryResult(_delivery(row), False)
        attempts = row["attempts"] + 1
        try:
            if hasattr(sink, "send"):
                sink.send(notification)
            else:
                sink(notification)
        except Exception as exc:
            delay = min(MAX_RETRY_SECONDS, DEFAULT_RETRY_SECONDS * (2 ** max(0, attempts - 1)))
            status = "failed" if attempts >= max_attempts else "pending"
            con.execute(
                "UPDATE notification_deliveries SET status=?,attempts=?,next_attempt_at=?,last_error=? WHERE id=?",
                (status, attempts, (current + timedelta(seconds=delay)).isoformat(),
                 _safe(f"{type(exc).__name__}: {exc}", 300), delivery.id),
            )
            con.commit()
            row = con.execute("SELECT * FROM notification_deliveries WHERE id=?", (delivery.id,)).fetchone()
            return DeliveryResult(_delivery(row), False)
        con.execute(
            "UPDATE notification_deliveries SET status='delivered',attempts=?,delivered_at=?,last_error='' WHERE id=?",
            (attempts, current.isoformat(), delivery.id),
        )
        con.commit()
        row = con.execute("SELECT * FROM notification_deliveries WHERE id=?", (delivery.id,)).fetchone()
    return DeliveryResult(_delivery(row), True)

def notify_event(
    engine_root: Path, event: dict, sinks: dict[str, Sink | Callable[[Notification], None]],
    *, details_url: str = "", now: datetime | None = None,
) -> list[DeliveryResult]:
    event_type = str(event.get("event_type") or "")
    policy = EVENT_POLICY.get(event_type)
    if policy is None:
        return []
    job = store.get(engine_root, int(event["job_id"]))
    configured = preferences(engine_root, project_id=job.envelope.get("project_id") or job.project)
    prefs = configured or _default_preference(job)
    results = []
    for pref in prefs:
        if not _matches(pref, event_type, policy[0]) or pref.channel not in sinks:
            continue
        notification = render_event(engine_root, event, channel=pref.channel,
                                    recipient=pref.recipient, details_url=details_url)
        if notification:
            results.append(deliver(engine_root, notification, sinks[pref.channel], now=now))
    return results

def pending(engine_root: Path, *, limit: int = 100) -> list[Delivery]:
    with _connect(engine_root) as con:
        rows = con.execute(
            "SELECT * FROM notification_deliveries WHERE status IN ('pending','failed')"
            " AND next_attempt_at <= ? ORDER BY id LIMIT ?", (_now(), limit),
        ).fetchall()
    return [_delivery(row) for row in rows]


def retry_pending(
    engine_root: Path, sinks: dict[str, Sink | Callable[[Notification], None]], *,
    limit: int = 100, now: datetime | None = None,
) -> list[DeliveryResult]:
    """Retry due outbox rows after a gateway restart.

    The rendered body is persisted with the outbox row, so retry does not
    depend on the source event still being available or on a second render
    producing different text.
    """
    current = now or datetime.now(timezone.utc)
    with _connect(engine_root) as con:
        rows = con.execute(
            "SELECT * FROM notification_deliveries WHERE status IN ('pending','failed')"
            " AND next_attempt_at <= ? ORDER BY id LIMIT ?", (current.isoformat(), limit),
        ).fetchall()
    results = []
    for row in rows:
        sink = sinks.get(row["channel"])
        if sink is None:
            continue
        notification = Notification(
            row["event_id"], row["job_id"], row["project_id"], row["channel"],
            row["recipient"], row["event_type"], row["severity"], row["title"], row["body"],
        )
        results.append(deliver(engine_root, notification, sink, now=current))
    return results
