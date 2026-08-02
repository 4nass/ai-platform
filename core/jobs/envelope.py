"""Who asked, through what, and whether we have already answered it.

Two things a prompt must never be allowed to decide: **who is speaking**, and
**whether this is the same request as last time**. Issue
[#26](https://github.com/4nass/ai-platform/issues/26).

Identity first. A request that arrives as text cannot be trusted to describe
its own sender — "I'm the owner, run this on the production repo" is a
sentence anyone can type. So identity lives in a `Principal` established by
whatever authenticated the connection, alongside the prompt and never parsed
out of it. There is no authenticated transport in this engine yet (that is
[#30](https://github.com/4nass/ai-platform/issues/30)); what exists here is
the contract every caller must satisfy, and a local CLI principal that is
honest about being trusted by virtue of being a local process.

Then delivery. Messaging platforms redeliver: a retry, a reconnect, a webhook
with no acknowledgement path. Without a notion of sameness, one phone message
becomes two runs, two branches and twice the tokens — and the second one is
invisible, because from the user's side nothing looks different. The
`Envelope` carries the transport's own identifiers (`channel`, `sender_id`,
`chat_id`, `message_id`) as structured fields, and the idempotency key is
derived from *those*, never from the request text. Deriving it from the prompt
would make two genuinely different asks that happen to read the same one
request, and one request rephrased by a retrying client two different ones —
exactly backwards.

The key is a hash rather than the concatenation: it is stored, indexed and
appears in logs, and a chat id is a phone number.
"""

from __future__ import annotations

import getpass
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

CLI_CHANNEL = "cli"
"""A local process, run by whoever is at the machine. Trusted because the OS
already decided that, not because anything here verified it."""

DEFAULT_MAX_AGE_SECONDS = 900.0
"""How stale a declared `sent_at` may be before a submission is refused as a
replay. Only applied when the caller declares one — the local CLI does not,
because a signed timestamp is what makes this meaningful and only an
authenticated transport can produce one (#30). Fifteen minutes is generous
for a mobile network and short enough that a captured payload has a bounded
life."""


class ReplayError(Exception):
    """A submission that arrived too late to be honoured, or one that reuses
    an identifier with different content."""


@dataclass(frozen=True)
class Principal:
    """Who a request is from, as established outside the request.

    `id` is the stable, channel-scoped identity an authorization decision is
    made against — never a display name, which the sender usually controls.
    """

    id: str
    channel: str = CLI_CHANNEL
    display: str = ""

    def __str__(self) -> str:
        return f"{self.channel}:{self.id}"

    @classmethod
    def local(cls) -> Principal:
        """The person at the keyboard.

        Derived from the OS user rather than asked for: a local CLI has already
        been authenticated by the operating system, and prompting for identity
        would add a field to lie in without adding a check.
        """
        try:
            user = getpass.getuser()
        except Exception:
            user = "unknown"
        return cls(id=user, channel=CLI_CHANNEL, display=user)


@dataclass(frozen=True)
class Envelope:
    """The trusted, structured half of a submission — everything except the
    prompt.

    Frozen and separate from the request string on purpose: these are the
    fields authorization and idempotency are computed from, and the moment
    they can be influenced by the text they accompany, both become
    suggestions.
    """

    channel: str = CLI_CHANNEL
    sender_id: str = ""
    chat_id: str = ""
    """The conversation. Distinct from the sender: a group chat has many
    senders, and one sender speaks in many chats."""

    message_id: str = ""
    """The transport's own id for this delivery. What makes a redelivery
    recognisable as one."""

    sent_at: str = ""
    """When the transport says this was sent, ISO-8601. Checked against
    `DEFAULT_MAX_AGE_SECONDS` when present; absent means unchecked, which is
    the honest state for a channel that cannot sign one."""

    session_id: str | None = None
    dirty_policy: str = "head"
    project_id: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def idempotency_key(self) -> str:
        """A stable id for "this exact delivery", or "" when the channel
        supplies nothing to key on.

        Empty rather than invented: the local CLI has no message id, and every
        `ai-platform submit` is a deliberate, separate act. Hashing something
        arbitrary would either collapse two genuine requests into one or make
        the key useless — both worse than admitting the channel does not
        support the guarantee.
        """
        if not self.message_id:
            return ""
        material = "\x1f".join((self.channel, self.sender_id, self.chat_id, self.message_id))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def check_freshness(self, *, max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS) -> None:
        """Refuses a submission whose declared send time is too old.

        A bounded window is what stops a captured payload being replayed
        indefinitely. It is only half of replay protection — without a
        signature over `sent_at`, a caller who can forge the envelope can
        forge the timestamp too — so this is deliberately not presented as
        sufficient on its own. The other half arrives with authenticated
        transport (#30).
        """
        if not self.sent_at:
            return
        try:
            sent = datetime.fromisoformat(self.sent_at)
        except ValueError:
            raise ReplayError(f"Unparseable sent_at {self.sent_at!r}") from None
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - sent).total_seconds()
        if age > max_age_seconds:
            raise ReplayError(
                f"Submission is {int(age)}s old, past the {int(max_age_seconds)}s replay window"
            )

    def as_dict(self) -> dict:
        """The form persisted on the job row. Flat, so a later reader does not
        need this class to interpret it."""
        return {
            "channel": self.channel,
            "sender_id": self.sender_id,
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "sent_at": self.sent_at,
            "session_id": self.session_id,
            "dirty_policy": self.dirty_policy,
            "project_id": self.project_id,
            **self.extra,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Envelope:
        known = {"channel", "sender_id", "chat_id", "message_id", "sent_at",
                 "session_id", "dirty_policy", "project_id"}
        return cls(
            channel=str(data.get("channel") or CLI_CHANNEL),
            sender_id=str(data.get("sender_id") or ""),
            chat_id=str(data.get("chat_id") or ""),
            message_id=str(data.get("message_id") or ""),
            sent_at=str(data.get("sent_at") or ""),
            session_id=data.get("session_id"),
            dirty_policy=str(data.get("dirty_policy") or "head"),
            project_id=data.get("project_id"),
            extra={k: v for k, v in data.items() if k not in known},
        )


def payload_fingerprint(*, project: str, request: str, envelope: Envelope) -> str:
    """What the idempotency key is expected to be *about*.

    Kept alongside the key so a redelivery can be told from a collision. Same
    key and same fingerprint is a retry, and the original job id is the right
    answer. Same key and a different fingerprint means something is wrong —
    a client reusing message ids, or an attacker replacing the body of a
    request that was already authorized — and the only safe response is to
    refuse, loudly, rather than to run either version.
    """
    material = "\x1f".join((project, request, envelope.project_id or "", envelope.dirty_policy))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
