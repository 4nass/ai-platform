"""Authenticated transport request verification (issue #44).

The local job envelope already contains a :class:`Principal` and an
idempotency key, but those values only become trustworthy when a transport has
verified who produced them. This module is the transport-neutral boundary used
by a future REST/SSE server or OpenClaw adapter:

* each credential identifies one channel-scoped principal and a set of scopes;
* every request signs its method, path, body hash, key id, timestamp and nonce;
* HMAC keys can overlap during rotation and can be revoked independently;
* a durable nonce store rejects changed replays and marks an identical retry so
  the job store can return its existing idempotent submission.

No HTTP server lives here deliberately. Keeping verification independent from
FastAPI/Starlette/OpenClaw makes the security contract testable before a public
socket exists and keeps adapters replaceable.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Callable, Mapping

from core.jobs.envelope import Envelope, Principal

AUTH_VERSION = "AI-PLATFORM-SIGNATURE-V1"
DEFAULT_MAX_SKEW_SECONDS = 900.0
NONCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class TransportAuthError(Exception):
    """Base class for safe transport authentication failures."""


class AuthenticationError(TransportAuthError):
    """The credential, signature or signed request metadata is invalid."""


class AuthorizationError(TransportAuthError):
    """The authenticated principal lacks the requested operation scope."""


class ReplayError(TransportAuthError):
    """The request is stale or reuses a nonce with different content."""


@dataclass(frozen=True)
class TransportCredential:
    """One rotating HMAC credential for a channel-scoped principal.

    ``secret`` is accepted as bytes or text but is never included in repr or
    persisted by this module. Deployments should inject it from a secret
    manager/environment, not a repository config file.
    """

    key_id: str
    principal_id: str
    channel: str
    secret: bytes = field(repr=False)
    scopes: frozenset[str] = frozenset()
    display: str = ""
    not_before: float | None = None
    expires_at: float | None = None
    revoked: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.secret, str):
            object.__setattr__(self, "secret", self.secret.encode("utf-8"))
        if not isinstance(self.secret, bytes):
            raise ValueError("credential secret must be bytes or text")
        if not self.key_id or not self.principal_id or not self.channel:
            raise ValueError("key_id, principal_id and channel are required")
        if not self.secret:
            raise ValueError("credential secret must not be empty")

    def active_at(self, now: float) -> bool:
        return (
            not self.revoked
            and (self.not_before is None or now >= self.not_before)
            and (self.expires_at is None or now < self.expires_at)
        )

    def sign(
        self,
        *,
        method: str,
        path: str,
        body: bytes,
        timestamp: int,
        nonce: str,
    ) -> str:
        """Create the wire signature for an adapter or integration test."""
        return sign_request(
            secret=self.secret,
            key_id=self.key_id,
            method=method,
            path=path,
            body=body,
            timestamp=timestamp,
            nonce=nonce,
        )


@dataclass(frozen=True)
class AuthenticatedRequest:
    """Verified identity and envelope handed to the application layer."""

    principal: Principal
    scopes: frozenset[str]
    envelope: Envelope
    key_id: str
    nonce: str
    timestamp: int
    body_hash: str
    replayed: bool = False

    def require(self, scope: str) -> None:
        """Fail closed when an operation is outside the credential grant."""
        if scope not in self.scopes:
            raise AuthorizationError(f"principal is not authorized for {scope!r}")


class ReplayStore:
    """Durable nonce ledger shared across processes and restarts."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS transport_nonces (
      key_id TEXT NOT NULL,
      nonce TEXT NOT NULL,
      body_hash TEXT NOT NULL,
      expires_at REAL NOT NULL,
      created_at REAL NOT NULL,
      PRIMARY KEY (key_id, nonce)
    );
    CREATE INDEX IF NOT EXISTS idx_transport_nonces_expiry
      ON transport_nonces(expires_at);
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        with self._connect() as con:
            con.executescript(self.SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10.0)
        con.row_factory = sqlite3.Row
        return con

    def claim(
        self,
        *,
        key_id: str,
        nonce: str,
        body_hash: str,
        expires_at: float,
        now: float,
    ) -> bool:
        """Record a nonce; return True for an identical safe retry.

        A changed body under an existing nonce is always rejected. An
        identical retry is allowed through so the durable job idempotency index
        can return the original job instead of making a second one.
        """
        with self._lock, self._connect() as con:
            con.execute("DELETE FROM transport_nonces WHERE expires_at < ?", (now,))
            try:
                con.execute(
                    "INSERT INTO transport_nonces(key_id, nonce, body_hash, expires_at, created_at)"
                    " VALUES(?,?,?,?,?)",
                    (key_id, nonce, body_hash, expires_at, now),
                )
                return False
            except sqlite3.IntegrityError:
                row = con.execute(
                    "SELECT body_hash, expires_at FROM transport_nonces "
                    "WHERE key_id = ? AND nonce = ?",
                    (key_id, nonce),
                ).fetchone()
                if row is not None and row["body_hash"] == body_hash and row["expires_at"] >= now:
                    return True
                raise ReplayError("nonce was already used for different content") from None


def _canonical(
    *, key_id: str, method: str, path: str, body: bytes, timestamp: int, nonce: str
) -> bytes:
    if not method or not path:
        raise AuthenticationError("signed method and path are required")
    body_hash = hashlib.sha256(body).hexdigest()
    return "\n".join(
        (AUTH_VERSION, key_id, method.upper(), path, str(timestamp), nonce, body_hash)
    ).encode("utf-8")


def sign_request(
    *,
    secret: bytes | str,
    key_id: str,
    method: str,
    path: str,
    body: bytes,
    timestamp: int,
    nonce: str,
) -> str:
    """Return unpadded base64url HMAC-SHA256 for the canonical request."""
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    digest = hmac.new(
        secret,
        _canonical(
            key_id=key_id,
            method=method,
            path=path,
            body=body,
            timestamp=timestamp,
            nonce=nonce,
        ),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _parse_timestamp(value: int | str) -> int:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        raise AuthenticationError("invalid transport timestamp") from None
    if timestamp <= 0:
        raise AuthenticationError("invalid transport timestamp")
    return timestamp


class Authenticator:
    """Verify signed requests and return an authenticated application input."""

    def __init__(
        self,
        credentials: Mapping[str, TransportCredential],
        replay_store: ReplayStore,
        *,
        max_skew_seconds: float = DEFAULT_MAX_SKEW_SECONDS,
        clock: Callable[[], float] = time.time,
    ):
        self.credentials = dict(credentials)
        self.replay_store = replay_store
        self.max_skew_seconds = max_skew_seconds
        self.clock = clock

    def verify(
        self,
        *,
        method: str,
        path: str,
        body: bytes,
        key_id: str,
        timestamp: int | str,
        nonce: str,
        signature: str,
        envelope: Envelope,
        scope: str | None = None,
    ) -> AuthenticatedRequest:
        now = float(self.clock())
        timestamp_int = _parse_timestamp(timestamp)
        if abs(now - timestamp_int) > self.max_skew_seconds:
            raise ReplayError("transport timestamp is outside the replay window")
        if not NONCE_PATTERN.fullmatch(nonce or ""):
            raise ReplayError("invalid transport nonce")

        credential = self.credentials.get(key_id)
        if credential is None or not credential.active_at(now):
            raise AuthenticationError("unknown or inactive transport credential")
        expected = sign_request(
            secret=credential.secret,
            key_id=key_id,
            method=method,
            path=path,
            body=body,
            timestamp=timestamp_int,
            nonce=nonce,
        )
        if not signature or not hmac.compare_digest(expected, signature):
            raise AuthenticationError("invalid transport signature")

        if envelope.channel != credential.channel or envelope.sender_id != credential.principal_id:
            raise AuthenticationError("signed envelope does not match its principal")
        if not envelope.chat_id or not envelope.message_id:
            raise AuthenticationError("authenticated submissions require chat_id and message_id")

        body_hash = hashlib.sha256(body).hexdigest()
        replayed = self.replay_store.claim(
            key_id=key_id,
            nonce=nonce,
            body_hash=body_hash,
            expires_at=now + self.max_skew_seconds,
            now=now,
        )
        authenticated = AuthenticatedRequest(
            principal=Principal(
                id=credential.principal_id,
                channel=credential.channel,
                display=credential.display or credential.principal_id,
            ),
            scopes=credential.scopes,
            envelope=envelope,
            key_id=key_id,
            nonce=nonce,
            timestamp=timestamp_int,
            body_hash=body_hash,
            replayed=replayed,
        )
        if scope is not None:
            authenticated.require(scope)
        return authenticated


def credential_from_mapping(raw: Mapping[str, object]) -> TransportCredential:
    """Parse a secret-manager/config mapping without accepting implicit grants."""
    scopes = raw.get("scopes", ())
    if isinstance(scopes, str) or not isinstance(scopes, (list, tuple, set, frozenset)):
        raise ValueError("credential scopes must be a list")
    return TransportCredential(
        key_id=str(raw.get("key_id") or ""),
        principal_id=str(raw.get("principal_id") or ""),
        channel=str(raw.get("channel") or ""),
        secret=raw.get("secret", b""),
        scopes=frozenset(str(scope) for scope in scopes),
        display=str(raw.get("display") or ""),
        not_before=float(raw["not_before"]) if raw.get("not_before") is not None else None,
        expires_at=float(raw["expires_at"]) if raw.get("expires_at") is not None else None,
        revoked=bool(raw.get("revoked", False)),
    )
