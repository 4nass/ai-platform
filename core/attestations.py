"""Operator attestations, and the record of what each GO was issued against.

Some controls this engine depends on are not observable from inside it. TLS is
terminated *upstream* — by a reverse proxy the engine never sees — and rate
limiting lives in the same place. Asking an environment variable whether they
exist produces a boolean an operator sets, not evidence: the previous gate read
`AI_PLATFORM_TLS_TERMINATED=true` and reported PASS, which is a claim dressed
as a check. Splitting that into four variables would only have produced four.

So the honest shape is an attestation: a person states what they verified, that
statement is recorded with who said it and when, and it expires. The report then
says `ATTESTED` rather than `PASS`, which is what actually happened.

**Bound to a fingerprint.** An attestation about TLS is about *this* exposure —
this bind address, this termination endpoint, this rate-limit policy. Move the
bind from a loopback address to `0.0.0.0` and the statement no longer describes
what is running, so it stops counting. The fingerprint deliberately covers only
those parameters: made any wider, every unrelated edit to `platform.yaml` would
void it and the operator would re-attest by reflex, which is the same as not
attesting at all.

**What this does not defend against.** The engine runs as one user on one
machine. Whoever can record an attestation can also open `jobs.sqlite` and
write one directly. This is not tamper-proof and must not be described as such.
What it buys is real but narrower: accidental drift is caught, every statement
is attributed and dated, expiry is enforced without anyone remembering to, and
a GO is tied to the configuration it was issued against — so one obtained on a
loopback profile cannot silently cover a public one.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from core.jobs.store import connect

TLS_TERMINATION = "tls_termination"
RATE_LIMIT = "rate_limit"

CONTROLS = (TLS_TERMINATION, RATE_LIMIT)
"""The controls that can only be attested, never checked from here."""

MAX_TTL_DAYS = 90
"""Longest life an attestation may be given. An attestation with no horizon is
a permanent claim about a system that changes, which is the thing being fixed."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS attestations (
  id           INTEGER PRIMARY KEY,
  control      TEXT NOT NULL,
  fingerprint  TEXT NOT NULL,
  statement    TEXT NOT NULL,
  attested_by  TEXT NOT NULL,
  attested_at  TEXT NOT NULL,
  expires_at   TEXT NOT NULL,
  revoked_at   TEXT,
  revoked_by   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_attestations_lookup
  ON attestations(control, fingerprint, expires_at);

CREATE TABLE IF NOT EXISTS security_decisions (
  id           INTEGER PRIMARY KEY,
  decision     TEXT NOT NULL,
  fingerprint  TEXT NOT NULL,
  remote_ready INTEGER NOT NULL,
  actor        TEXT NOT NULL DEFAULT '',
  context      TEXT NOT NULL DEFAULT '',
  decided_at   TEXT NOT NULL,
  report       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_security_decisions_at ON security_decisions(decided_at);
"""


class AttestationError(Exception):
    """A statement that cannot be recorded as given."""


@dataclass(frozen=True)
class Attestation:
    id: int
    control: str
    fingerprint: str
    statement: str
    attested_by: str
    attested_at: str
    expires_at: str
    revoked_at: str | None = None

    def active_at(self, now: datetime) -> bool:
        if self.revoked_at:
            return False
        return _parse(self.expires_at) > now

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "control": self.control,
            "fingerprint": self.fingerprint,
            "statement": self.statement,
            "attested_by": self.attested_by,
            "attested_at": self.attested_at,
            "expires_at": self.expires_at,
            "revoked_at": self.revoked_at,
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _ensure(con) -> None:
    con.executescript(SCHEMA)


def _row(row) -> Attestation:
    return Attestation(
        id=int(row["id"]),
        control=row["control"],
        fingerprint=row["fingerprint"],
        statement=row["statement"],
        attested_by=row["attested_by"],
        attested_at=row["attested_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
    )


def deployment_fingerprint(env: Mapping[str, str]) -> str:
    """Identify the exposure an attestation is about, and nothing else.

    Four values, because those are the four an operator's statement actually
    concerns. `AI_PLATFORM_TLS_ENDPOINT` and `AI_PLATFORM_RATE_LIMIT_POLICY`
    are identifiers of the upstream pieces — a proxy hostname, a policy name —
    not assertions that they work; changing which proxy fronts the engine has
    to invalidate a statement made about the previous one.
    """
    material = "\x1f".join(
        (
            env.get("AI_PLATFORM_BIND_HOST", "127.0.0.1"),
            str(env.get("AI_PLATFORM_BIND_PORT", "8787")),
            env.get("AI_PLATFORM_TLS_ENDPOINT", ""),
            env.get("AI_PLATFORM_RATE_LIMIT_POLICY", ""),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def record(
    engine_root: Path,
    *,
    control: str,
    fingerprint: str,
    statement: str,
    attested_by: str,
    ttl_days: int = 30,
) -> Attestation:
    """Write down what someone says they verified, and when it stops counting."""
    if control not in CONTROLS:
        raise AttestationError(
            f"Unknown control {control!r}. Attestable: {', '.join(CONTROLS)}"
        )
    if not statement.strip():
        raise AttestationError(
            "an attestation must say what was verified — an empty statement "
            "records a signature on nothing"
        )
    if not attested_by.strip():
        raise AttestationError("an attestation must name who is making it")
    if not 1 <= ttl_days <= MAX_TTL_DAYS:
        raise AttestationError(f"ttl_days must be between 1 and {MAX_TTL_DAYS}")

    now = _now()
    expires = now + timedelta(days=ttl_days)
    with connect(engine_root) as con:
        _ensure(con)
        cursor = con.execute(
            "INSERT INTO attestations(control, fingerprint, statement, attested_by,"
            " attested_at, expires_at) VALUES(?,?,?,?,?,?)",
            (control, fingerprint, statement.strip(), attested_by.strip(),
             now.isoformat(), expires.isoformat()),
        )
        row = con.execute(
            "SELECT * FROM attestations WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return _row(row)


def active(
    engine_root: Path, *, control: str, fingerprint: str, now: datetime | None = None
) -> Attestation | None:
    """The live attestation for this control on this exposure, if there is one.

    Matched on the fingerprint rather than on the control alone: a statement
    about a different deployment is not evidence about this one.
    """
    moment = now or _now()
    with connect(engine_root) as con:
        _ensure(con)
        rows = con.execute(
            "SELECT * FROM attestations WHERE control = ? AND fingerprint = ?"
            " AND revoked_at IS NULL ORDER BY id DESC",
            (control, fingerprint),
        ).fetchall()
    for row in rows:
        attestation = _row(row)
        if attestation.active_at(moment):
            return attestation
    return None


def latest(
    engine_root: Path, *, control: str, fingerprint: str
) -> Attestation | None:
    """The most recent statement for this exposure, live or not.

    What lets a report say "expired on 3 March" instead of "missing", which are
    different problems with different fixes.
    """
    with connect(engine_root) as con:
        _ensure(con)
        row = con.execute(
            "SELECT * FROM attestations WHERE control = ? AND fingerprint = ?"
            " ORDER BY id DESC LIMIT 1",
            (control, fingerprint),
        ).fetchone()
    return _row(row) if row else None


def revoke(engine_root: Path, attestation_id: int, *, actor: str) -> bool:
    """Withdraw a statement. The row stays; withdrawal is itself a fact."""
    with connect(engine_root) as con:
        _ensure(con)
        cursor = con.execute(
            "UPDATE attestations SET revoked_at = ?, revoked_by = ?"
            " WHERE id = ? AND revoked_at IS NULL",
            (_now().isoformat(), actor, attestation_id),
        )
        return cursor.rowcount == 1


def recent(engine_root: Path, *, limit: int = 50) -> list[Attestation]:
    with connect(engine_root) as con:
        _ensure(con)
        rows = con.execute(
            "SELECT * FROM attestations ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row(row) for row in rows]


def record_decision(
    engine_root: Path,
    *,
    decision: str,
    fingerprint: str,
    remote_ready: bool,
    actor: str = "",
    context: str = "",
    report: str = "{}",
) -> int:
    """Record a GO/NO_GO against the configuration it was issued for.

    Kept out of `evaluate()` on purpose — evaluating is a read, and a read that
    writes cannot be used to answer a question without also changing the
    answer's history. Callers that are *making* a decision record it; the ones
    merely looking do not.
    """
    with connect(engine_root) as con:
        _ensure(con)
        cursor = con.execute(
            "INSERT INTO security_decisions(decision, fingerprint, remote_ready, actor,"
            " context, decided_at, report) VALUES(?,?,?,?,?,?,?)",
            (decision, fingerprint, int(remote_ready), actor, context,
             _now().isoformat(), report),
        )
        return int(cursor.lastrowid)


def decisions(engine_root: Path, *, limit: int = 20) -> list[dict]:
    with connect(engine_root) as con:
        _ensure(con)
        rows = con.execute(
            "SELECT id, decision, fingerprint, remote_ready, actor, context, decided_at"
            " FROM security_decisions ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]
