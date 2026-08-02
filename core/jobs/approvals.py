"""Consequential actions wait for a person, and the wait is auditable.

"Fix this and show me a preview" does not authorize pushing a branch,
deploying, migrating a database or spending another two million tokens. A
remote request buys one thing: the run. Anything whose consequences outlive
the run, or leave the machine, is a separate decision. Issue
[#28](https://github.com/4nass/ai-platform/issues/28).

**Approval is bound to inputs, not to a name.** Approving "push" is approving
*that* push — this branch, this diff, this target. The record carries a
fingerprint of exactly what was shown, and consuming it requires the same
fingerprint. A new commit, a different deployment target, a changed command or
a larger budget amount all produce a different fingerprint and therefore need a
new decision. Without that, approval becomes a bearer token for a class of
action and the thing eventually done is not the thing that was read.

**Single-use, and expiring.** A token that can be replayed is a standing
authorization nobody granted, and one that never expires is a standing
authorization nobody remembers granting. Both states are consumed exactly once
and both are recorded.

**Approval comes from the principal who asked.** The engine has one owner
today, so this reads as a formality — but the check is written now, against
`core.jobs.envelope.Principal`, because retrofitting an identity check onto a
flow that never had one is how the check ends up absent when a second channel
appears.

**Local interactive use is not a weaker policy, it is a different channel.**
Someone at the terminal can approve their own request in the same breath
(`--approve`), because they are present and the decision is synchronous. A job
running with nobody attached cannot, and goes to `waiting_approval`. The
difference is who is there to decide, not how much is required — the gateway
default does not move.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.jobs.store import connect

PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"
EXPIRED = "expired"
CONSUMED = "consumed"

AUTOMATIC = "automatic"
"""No decision needed. What ordinary run stages are."""

REQUIRES_APPROVAL = "approval_required"
DENIED_BY_POLICY = "denied"

DEFAULT_TTL_SECONDS = 3600.0
"""How long an approval stays usable. An hour is long enough to walk away from
a phone and come back, short enough that a decision made against a diff is
still being applied to roughly that diff."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY,
  token       TEXT NOT NULL UNIQUE,
  job_id      INTEGER,
  run_key     TEXT NOT NULL DEFAULT '',
  action      TEXT NOT NULL,
  target      TEXT NOT NULL DEFAULT '',
  -- Binds the decision to exactly what was displayed. Consuming requires the
  -- same value, so a changed diff/command/amount needs a new decision.
  fingerprint TEXT NOT NULL,
  detail      TEXT NOT NULL DEFAULT '{}',
  requested_by TEXT NOT NULL DEFAULT '',
  state       TEXT NOT NULL,
  requested_at TEXT NOT NULL,
  expires_at   TEXT NOT NULL,
  decided_at   TEXT,
  decided_by   TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_approvals_state ON approvals(state);
CREATE INDEX IF NOT EXISTS idx_approvals_run   ON approvals(run_key);

-- Append-only, separate from the approvals row so a decision's history
-- survives the row being consumed. "Every request, approval, denial and
-- action result is audit logged" is not satisfiable by mutable state alone:
-- the current state cannot say what it used to be.
CREATE TABLE IF NOT EXISTS approval_events (
  id INTEGER PRIMARY KEY,
  approval_id INTEGER NOT NULL,
  event       TEXT NOT NULL,
  at          TEXT NOT NULL,
  actor       TEXT NOT NULL DEFAULT '',
  note        TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_approval_events ON approval_events(approval_id);
"""


class ApprovalError(Exception):
    """An approval that cannot be used: unknown, expired, already spent,
    decided by the wrong person, or bound to different inputs."""


class ActionDenied(Exception):
    """An action this project's policy refuses outright. Not something an
    approval can unlock — a denial is a decision already made."""


@dataclass(frozen=True)
class Approval:
    id: int
    token: str
    job_id: int | None
    run_key: str
    action: str
    target: str
    fingerprint: str
    detail: dict
    requested_by: str
    state: str
    requested_at: str
    expires_at: str
    decided_at: str | None
    decided_by: str

    @property
    def expired(self) -> bool:
        return datetime.now(timezone.utc) > datetime.fromisoformat(self.expires_at)

    def describe(self) -> str:
        """What a person is being asked to authorize, in one line.

        Deliberately built from the stored fields rather than from a message
        composed at request time: the thing displayed and the thing bound to
        the fingerprint have to be the same thing, or the display is theatre.
        """
        parts = [f"{self.action} on {self.target}" if self.target else self.action]
        for key, value in sorted(self.detail.items()):
            parts.append(f"{key}={value}")
        return " · ".join(parts)


def fingerprint(action: str, target: str, detail: dict | None = None) -> str:
    """A stable id for *this exact* action, over the inputs a decision is made
    against.

    `detail` is sorted so an equivalent request fingerprints identically
    regardless of key order — otherwise a re-request nobody changed would look
    like a different action and ask again.
    """
    material = json.dumps(
        {"action": action, "target": target, "detail": detail or {}}, sort_keys=True
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def classify(project, action: str) -> str:
    """What this project's policy says about an action.

    Three outcomes, not two. `denied` is not "approval-required and nobody
    approved it" — it is a decision already made, and offering to ask about it
    would invite someone to approve what the policy exists to refuse.
    """
    if project is None:
        return AUTOMATIC
    if not project.permits(action):
        return DENIED_BY_POLICY
    if action in project.approval_required:
        return REQUIRES_APPROVAL
    return AUTOMATIC


def _ensure(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)


def _row(row: sqlite3.Row) -> Approval:
    data = dict(row)
    data["detail"] = json.loads(data.get("detail") or "{}")
    return Approval(**data)


def _log(con: sqlite3.Connection, approval_id: int, event: str, *, actor: str = "", note: str = "") -> None:
    con.execute(
        "INSERT INTO approval_events(approval_id, event, at, actor, note) VALUES(?,?,?,?,?)",
        (approval_id, event, datetime.now(timezone.utc).isoformat(), actor, note),
    )


def request(
    engine_root: Path,
    *,
    action: str,
    target: str = "",
    detail: dict | None = None,
    job_id: int | None = None,
    run_key: str = "",
    requested_by: str = "",
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
) -> Approval:
    """Records that something is waiting on a decision, and returns it.

    The token is generated here rather than supplied: a caller-chosen token is
    a caller-guessable one, and this is the only thing standing between an
    unapproved action and a performed one.
    """
    now = datetime.now(timezone.utc)
    detail = detail or {}
    with connect(engine_root) as con:
        _ensure(con)
        cursor = con.execute(
            "INSERT INTO approvals(token, job_id, run_key, action, target, fingerprint, detail,"
            " requested_by, state, requested_at, expires_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                secrets.token_urlsafe(24),
                job_id,
                run_key,
                action,
                target,
                fingerprint(action, target, detail),
                json.dumps(detail, sort_keys=True),
                requested_by,
                PENDING,
                now.isoformat(),
                (now + timedelta(seconds=ttl_seconds)).isoformat(),
            ),
        )
        approval_id = int(cursor.lastrowid)
        _log(con, approval_id, "requested", actor=requested_by, note=f"{action} {target}".strip())
        return _row(con.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone())


def get(engine_root: Path, approval_id: int) -> Approval:
    with connect(engine_root) as con:
        _ensure(con)
        row = con.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    if row is None:
        raise ApprovalError(f"No approval {approval_id}")
    return _row(row)


def pending(engine_root: Path, *, limit: int = 50) -> list[Approval]:
    """Everything currently waiting on someone. Expired requests are reported
    as expired rather than silently dropped — "nothing is waiting" and "you
    missed it" are different answers."""
    with connect(engine_root) as con:
        _ensure(con)
        rows = con.execute(
            "SELECT * FROM approvals WHERE state = ? ORDER BY id DESC LIMIT ?", (PENDING, limit)
        ).fetchall()
    return [_row(row) for row in rows]


def events(engine_root: Path, approval_id: int) -> list[dict]:
    with connect(engine_root) as con:
        _ensure(con)
        return [
            dict(row)
            for row in con.execute(
                "SELECT event, at, actor, note FROM approval_events"
                " WHERE approval_id = ? ORDER BY id",
                (approval_id,),
            )
        ]


def decide(
    engine_root: Path,
    approval_id: int,
    *,
    approved: bool,
    principal: str,
    note: str = "",
) -> Approval:
    """Approves or denies a pending request.

    Refuses a decision from anyone other than the principal who asked. The
    engine has one owner today, so this is close to a formality — but a flow
    that never checked identity is one where the check is missing on the day a
    second channel appears, and this is the flow whose whole purpose is
    authorization.
    """
    with connect(engine_root) as con:
        _ensure(con)
        row = con.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            raise ApprovalError(f"No approval {approval_id}")
        approval = _row(row)

        if approval.state != PENDING:
            raise ApprovalError(
                f"Approval {approval_id} is already {approval.state} — decisions are made once."
            )
        if approval.expired:
            con.execute("UPDATE approvals SET state = ? WHERE id = ?", (EXPIRED, approval_id))
            _log(con, approval_id, EXPIRED, actor=principal, note="decided after expiry")
            # Committed before raising: `connect` commits only on a clean exit,
            # so the exception would otherwise roll back both the state change
            # and the audit line that explains it — leaving a request that
            # still reads `pending` after it was refused for being expired.
            con.commit()
            raise ApprovalError(
                f"Approval {approval_id} expired at {approval.expires_at}. "
                "Re-request it: what it was shown against may have moved."
            )
        if approval.requested_by and principal != approval.requested_by:
            _log(
                con,
                approval_id,
                "refused",
                actor=principal,
                note="decision attempted by a different principal",
            )
            con.commit()
            raise ApprovalError(
                f"Approval {approval_id} was requested by someone else and can only be "
                "decided by them."
            )

        state = APPROVED if approved else DENIED
        con.execute(
            "UPDATE approvals SET state = ?, decided_at = ?, decided_by = ? WHERE id = ?",
            (state, datetime.now(timezone.utc).isoformat(), principal, approval_id),
        )
        _log(con, approval_id, state, actor=principal, note=note)
        return _row(con.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone())


def consume(
    engine_root: Path,
    approval_id: int,
    *,
    action: str,
    target: str = "",
    detail: dict | None = None,
) -> Approval:
    """Spends an approval to perform the action it authorized.

    Every reason this refuses is a way an authorization could otherwise be
    stretched beyond what was read:

    - not approved: nothing was granted;
    - expired: granted against a state that has had time to move;
    - already consumed: single-use is what stops one decision authorizing two
      actions;
    - a different fingerprint: the diff, target, command or amount is not the
      one that was displayed, so this is a *different* action wearing an
      approval granted for another.
    """
    expected = fingerprint(action, target, detail or {})
    with connect(engine_root) as con:
        _ensure(con)
        row = con.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            raise ApprovalError(f"No approval {approval_id}")
        approval = _row(row)

        if approval.state == CONSUMED:
            raise ApprovalError(f"Approval {approval_id} has already been used.")
        if approval.state != APPROVED:
            raise ApprovalError(f"Approval {approval_id} is {approval.state}, not approved.")
        if approval.expired:
            con.execute("UPDATE approvals SET state = ? WHERE id = ?", (EXPIRED, approval_id))
            _log(con, approval_id, EXPIRED, note="expired before it was used")
            con.commit()
            raise ApprovalError(f"Approval {approval_id} expired before it was used.")
        if approval.fingerprint != expected:
            _log(
                con,
                approval_id,
                "refused",
                note="inputs changed since the decision was made",
            )
            con.commit()
            raise ApprovalError(
                f"Approval {approval_id} was granted for different inputs. What changed "
                "since it was shown needs a new decision."
            )

        con.execute("UPDATE approvals SET state = ? WHERE id = ?", (CONSUMED, approval_id))
        _log(con, approval_id, CONSUMED, actor=approval.decided_by)
        return _row(con.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone())


def expire_stale(engine_root: Path) -> int:
    """Moves pending requests past their expiry into `expired`.

    Deterministic rather than lazy: a request that quietly stays `pending`
    forever reads as "still waiting for you", which is the one thing it is not.
    """
    now = datetime.now(timezone.utc).isoformat()
    with connect(engine_root) as con:
        _ensure(con)
        rows = con.execute(
            "SELECT id FROM approvals WHERE state = ? AND expires_at < ?", (PENDING, now)
        ).fetchall()
        for row in rows:
            con.execute("UPDATE approvals SET state = ? WHERE id = ?", (EXPIRED, row["id"]))
            _log(con, int(row["id"]), EXPIRED, note="no decision before expiry")
        return len(rows)


def granted_extension(engine_root: Path, run_key: str) -> int:
    """Extra tokens a person has approved for this run (issue #27's `strict`
    pause).

    Sums *consumed* approvals only. An approved-but-unused grant is not
    spendable capacity — it becomes one at the moment it is consumed, which is
    what keeps single-use meaningful for budgets as well as for actions.
    """
    with connect(engine_root) as con:
        _ensure(con)
        rows = con.execute(
            "SELECT detail FROM approvals WHERE run_key = ? AND action = ? AND state = ?",
            (run_key, "budget", CONSUMED),
        ).fetchall()
    total = 0
    for row in rows:
        try:
            total += int(json.loads(row["detail"] or "{}").get("extra_tokens", 0))
        except (TypeError, ValueError):
            continue
    return total
