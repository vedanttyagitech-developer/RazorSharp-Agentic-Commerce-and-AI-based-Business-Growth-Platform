"""The durable webhook inbox: one row per delivery, claimed before anything is processed.

:class:`payment_adapters.WebhookInbox` owns the *order* of a delivery -- verify, then
deduplicate, then record -- and delegates the single-winner claim to an
:class:`~payment_adapters.razorpay.webhooks.InboxStore`. This module is that store,
backed by PostgreSQL.

Why a database claim rather than a check-then-write:

    INSERT INTO webhook_inbox (...) VALUES (...)
    ON CONFLICT (tenant_id, dedup_key) DO NOTHING
    RETURNING id

Razorpay retries an event until it is acknowledged, and it may deliver the same event to
two API pods at the same instant. A ``SELECT ... IF NOT EXISTS THEN INSERT`` lets both
pods see "absent" and both enqueue an apply command, which is how one capture gets
applied twice. The unique index makes exactly one of them get a row back; everybody else
gets nothing and is a duplicate by definition. That is a database guarantee rather than a
hopeful code path (ADR 0003 D7).

**This store must be given a kernel-role session.** ``webhook_inbox`` is INSERT/UPDATE for
``commerce_kernel`` and ``UPDATE`` for ``commerce_worker`` only
(``platform_db.roles.WRITE_GRANTS``); the app role can read it and nothing more. The
receiving route therefore resolves its tenant on an app-role read and does the claim in a
short kernel transaction, which is also the transaction the outbox command is enqueued in
so a claimed row can never exist without the command that processes it.

The raw bytes are stored exactly as they arrived. They are the bytes the HMAC covers, so
the scenario controller can replay a real delivery and the Inspector can show what was
actually received rather than a re-serialised paraphrase of it.
"""

from __future__ import annotations

import json
import uuid
from typing import Final

from commerce_domain import uuid7
from payment_adapters import WebhookInbox
from payment_adapters.razorpay.webhooks import EVENT_ID_HEADER, SIGNATURE_HEADER, InboxRecord
from sqlalchemy import text
from sqlalchemy.orm import Session

__all__ = [
    "REDACTED_HEADERS",
    "InboxClaim",
    "PostgresInboxStore",
    "inbox_for",
    "redact_headers",
]

#: The only headers kept beside the body. Everything else on an inbound webhook is either
#: transport noise or -- for a request that arrived through a proxy -- somebody's address.
#: The signature is kept because it is the evidence that this delivery verified, and it is
#: not a secret: it is a MAC over a body anybody holding the webhook secret can recompute.
REDACTED_HEADERS: Final[tuple[str, ...]] = (
    EVENT_ID_HEADER,
    SIGNATURE_HEADER,
    "content-type",
    "user-agent",
)

_CLAIM = text(
    "INSERT INTO webhook_inbox ("
    "  id, tenant_id, dedup_key, provider_event_id, event_type, body_digest, raw_body,"
    "  headers_redacted, signature_verified, payment_id, order_id, refund_id, apply_status"
    ") VALUES ("
    "  :id, :tenant, :dedup_key, :provider_event_id, :event_type, :body_digest, :raw_body,"
    "  CAST(:headers AS jsonb), :verified, :payment_id, :order_id, :refund_id, 'RECEIVED'"
    ") ON CONFLICT (tenant_id, dedup_key) DO NOTHING RETURNING id"
)

_READ = text(
    "SELECT id, dedup_key, event_type, body_digest, provider_event_id, payment_id, "
    "order_id, refund_id FROM webhook_inbox WHERE tenant_id = :tenant AND dedup_key = :key"
)

#: A redelivery is not an error and not a second event: it is the same event arriving
#: again, and the count of how often that happened is operational evidence the Inspector
#: shows. ``RETURNING id`` gives the caller the original row so its answer can name it.
_BUMP_DUPLICATE = text(
    "UPDATE webhook_inbox SET duplicate_count = duplicate_count + 1 "
    "WHERE tenant_id = :tenant AND dedup_key = :key RETURNING id"
)


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Keep only :data:`REDACTED_HEADERS`, lower-cased.

    An allow-list rather than a deny-list. A deny-list is wrong the first time a provider
    or a proxy adds a header nobody anticipated, and the cost of being wrong here is a
    forwarded ``Authorization`` or ``Cookie`` sitting in a table the demo shows on screen.
    """
    wanted = {name.lower() for name in REDACTED_HEADERS}
    return {key.lower(): value for key, value in headers.items() if key.lower() in wanted}


class InboxClaim:
    """The outcome of one delivery's claim attempt, filled in by :class:`PostgresInboxStore`.

    Held apart from :class:`~payment_adapters.razorpay.webhooks.WebhookAdmission` because
    the adapter's verdict says *whether* this caller won, and this says *which row* it
    won -- the identifier the ``APPLY_WEBHOOK_EVENT`` command has to carry.
    """

    __slots__ = ("inbox_id",)

    def __init__(self) -> None:
        self.inbox_id: uuid.UUID | None = None


class PostgresInboxStore:
    """Single-winner claim on ``(tenant_id, dedup_key)``, in the caller's transaction.

    One instance per delivery: it carries that delivery's raw bytes and headers, which
    the protocol's :class:`~payment_adapters.razorpay.webhooks.InboxRecord` does not, and
    it records the identifier of whichever row it wrote.

    Never commits. The claim becomes durable exactly when the enqueue that follows it
    does, which is what makes "a delivery is recorded but nothing will ever process it"
    unrepresentable.
    """

    __slots__ = ("_claim", "_headers", "_raw_body", "_session", "_signature_verified", "_tenant_id")

    def __init__(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        raw_body: bytes,
        headers: dict[str, str],
        signature_verified: bool = True,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._raw_body = raw_body
        self._headers = redact_headers(headers)
        self._signature_verified = signature_verified
        self._claim = InboxClaim()

    @property
    def claim_result(self) -> InboxClaim:
        """The row this store wrote, once :meth:`claim` has returned True."""
        return self._claim

    def claim(self, record: InboxRecord) -> bool:
        """Write ``record`` unless its key is already taken. True when this caller won.

        The insert names ``tenant_id`` explicitly as well as relying on row-level
        security, because the ON CONFLICT target is ``(tenant_id, dedup_key)``: two
        tenants may legitimately receive an event id from the same Razorpay account only
        if the platform is misconfigured, but the key must still be scoped or one
        tenant's delivery would suppress another's.
        """
        inbox_id = uuid7()
        written = self._session.execute(
            _CLAIM,
            {
                "id": inbox_id,
                "tenant": self._tenant_id,
                "dedup_key": record.dedup_key,
                "provider_event_id": record.provider_event_id,
                "event_type": record.event_type,
                "body_digest": record.body_digest,
                "raw_body": self._raw_body,
                "headers": json.dumps(self._headers, sort_keys=True),
                "verified": self._signature_verified,
                "payment_id": record.payment_id,
                "order_id": record.order_id,
                "refund_id": record.refund_id,
            },
        ).scalar()
        if written is None:
            return False
        self._claim.inbox_id = uuid.UUID(str(written))
        return True

    def get(self, dedup_key: str) -> InboxRecord | None:
        """The stored record for a key, or ``None``. Part of the ``InboxStore`` protocol."""
        row = self._session.execute(
            _READ, {"tenant": self._tenant_id, "key": dedup_key}
        ).one_or_none()
        if row is None:
            return None
        return InboxRecord(
            dedup_key=row.dedup_key,
            event_type=row.event_type,
            body_digest=row.body_digest,
            provider_event_id=row.provider_event_id,
            payment_id=row.payment_id,
            order_id=row.order_id,
            refund_id=row.refund_id,
        )

    def bump_duplicate(self, dedup_key: str) -> uuid.UUID | None:
        """Count one redelivery of an event already held, returning the original row's id.

        Returns ``None`` only if the winning row vanished between the failed claim and
        this update, which cannot happen inside one transaction and is reported as absent
        rather than asserted away.
        """
        row = self._session.execute(
            _BUMP_DUPLICATE, {"tenant": self._tenant_id, "key": dedup_key}
        ).scalar()
        return None if row is None else uuid.UUID(str(row))


def inbox_for(store: PostgresInboxStore) -> WebhookInbox:
    """The adapter's receiver driving this store.

    A one-line factory so every caller gets the verify-then-claim ordering from the
    component whose tests assert it, rather than reimplementing that order beside the
    HTTP handler where a later edit could reorder the two lines.
    """
    return WebhookInbox(store)
