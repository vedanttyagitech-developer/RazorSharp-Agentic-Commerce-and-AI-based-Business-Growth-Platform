"""The Razorpay webhook receiver.

ADR 0003 D7, in this exact order: read raw bytes, cap at 256 KiB, verify the HMAC in
constant time, resolve the tenant **from the route slug**, claim an inbox row, enqueue
APPLY_WEBHOOK_EVENT, answer 200. JSON is parsed only after the signature verifies.

No prefix and no session: this is a public endpoint with its own authentication
policy (specification 21.4).

**Owned by build unit D.**

The order above is the entire security property, so it is worth saying what each step
prevents:

*Raw bytes first, capped.* The HMAC covers the exact bytes Razorpay sent. Decoding,
parsing and re-serialising produces a different byte sequence -- ``{"a":1, "b":2}`` and
``{"b":2,"a":1}`` mean the same thing and sign differently -- so a handler that parses
first can never verify a genuine event. The 256 KiB cap is enforced *while* streaming by
:func:`commerce_api.security.read_capped_body`; ``await request.body()`` buffers whatever
arrives and lets you measure it afterwards, which makes the limit advisory against
exactly the caller it exists for.

*Verify before anything else touches the database.* An unverified body is an anonymous
stranger's bytes. Nothing it says -- not the tenant, not the event id, not the order --
may cause a row to be written or even a lookup to be made, so a forged delivery cannot
claim a deduplication key and suppress the genuine event that follows it.

*Tenant from the route, never from the body.* The slug is in the URL Razorpay was
configured with. A ``tenant_id`` in a webhook payload is a value the sender chose.

*Parse only after verification.* A malformed body from an unverified sender is a 401,
never a JSON parse error, because answering "your JSON is broken" tells an attacker their
signature got them past the gate.

*Claim, then enqueue, then 200.* The claim and the command commit together, so a
recorded delivery always has something that will process it. The answer is quick because
nothing is applied here; specification 11.3 wants the acknowledgement fast and the work
asynchronous, and the worker re-reads the stored bytes under the kernel role rather than
trusting anything this route passes it.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from commerce_domain import uuid7
from durable_work.commands import ApplyWebhookEventCommand, enqueue_command
from fastapi import APIRouter, Depends, Request
from payment_adapters import verify_webhook_signature
from payment_adapters.razorpay.webhooks import SIGNATURE_HEADER, header_value
from platform_db import Tenant, set_tenant
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import session_scope_for, settings_of, unbound_app_session
from ..errors import ProblemError
from ..security import BodyTooLargeError, read_capped_body
from ..services.inbox_store import PostgresInboxStore, inbox_for

router = APIRouter(prefix="", tags=["webhooks"])


class WebhookAck(BaseModel):
    """The acknowledgement. Razorpay reads the status code; the body is for the demo.

    ``duplicate`` is reported rather than hidden because a redelivery is the normal case
    the platform is built to survive, and showing the count on the Inspector is how a
    reviewer sees that "at-least-once delivery, exactly-once effect" is real rather than
    asserted.
    """

    model_config = ConfigDict(extra="forbid")

    received: bool
    duplicate: bool
    inbox_id: str | None
    event_type: str


@router.post(
    "/webhooks/razorpay/{tenant_slug}",
    response_model=WebhookAck,
    summary="Razorpay webhook inbox (raw-body HMAC, ADR 0003 D7)",
)
async def receive_razorpay_webhook(
    tenant_slug: str,
    request: Request,
    lookup: Annotated[Session, Depends(unbound_app_session)],
) -> WebhookAck:
    """Receive one Razorpay delivery. Verifies, records, enqueues; applies nothing.

    Two database sessions, on purpose. ``lookup`` is an app-role transaction with no
    tenant bound -- the one documented exception, for a path that is *discovering* its
    tenant -- and it does exactly one thing: turn the route's slug into a tenant id. The
    claim and the enqueue then run in a short ``commerce_kernel`` transaction, because
    ``webhook_inbox`` and ``outbox_events`` are kernel-write tables and the app role
    physically cannot write either. Least privilege for the untrusted half, and the
    privileged half opens only after the signature has verified.
    """
    settings = settings_of(request)
    try:
        raw_body = await read_capped_body(request)
    except BodyTooLargeError as exc:
        raise ProblemError(
            413,
            "Payload too large",
            f"A webhook body may not exceed {exc.limit} bytes.",
            limit=exc.limit,
        ) from None

    signature = header_value(dict(request.headers), SIGNATURE_HEADER) or ""
    razorpay = settings.razorpay()
    if not verify_webhook_signature(raw_body, signature, razorpay.webhook_secret):
        # Nothing has been parsed and nothing has been read from the database. A forged
        # delivery leaves no trace it could later use, including a claimed dedup key.
        raise ProblemError(
            401,
            "Signature verification failed",
            f"{SIGNATURE_HEADER} did not verify over the request body.",
        )

    tenant_id = lookup.execute(
        select(Tenant.id).where(Tenant.slug == tenant_slug)
    ).scalar_one_or_none()
    if tenant_id is None:
        raise ProblemError(
            404,
            "Tenant not found",
            "No tenant has that slug.",
            tenant_slug=tenant_slug,
        )

    correlation_id = uuid7()
    with session_scope_for(settings.database_url_kernel) as session:
        set_tenant(session, tenant_id)
        store = PostgresInboxStore(
            session,
            tenant_id=tenant_id,
            raw_body=raw_body,
            headers=dict(request.headers),
        )
        # The adapter verifies again before it claims. That repetition is deliberate: the
        # component whose tests assert "verify first, claim second" is the one that does
        # both, so no edit to this route can reorder them.
        admission = inbox_for(store).admit(
            raw_body=raw_body, headers=dict(request.headers), config=razorpay
        )
        event_type = "" if admission.event is None else admission.event.event_type

        if not admission.accepted:
            if not admission.is_duplicate:  # pragma: no cover - the signature verified above
                # The only other way the adapter refuses is a signature failure, which
                # this route has already excluded. Answering "duplicate" here would tell
                # a caller its delivery was recorded when it was not.
                raise ProblemError(
                    401,
                    "Signature verification failed",
                    f"{SIGNATURE_HEADER} did not verify over the request body.",
                )
            return _duplicate(store, admission.dedup_key, event_type)

        inbox_id = store.claim_result.inbox_id
        if inbox_id is None:  # pragma: no cover - claim() sets it whenever it returns True
            raise ProblemError(500, "Inbox row was claimed without an identifier", None)
        enqueue_command(
            session,
            ApplyWebhookEventCommand(
                tenant_id=str(tenant_id),
                inbox_id=str(inbox_id),
                correlation_id=str(correlation_id),
            ),
            idempotency_key=admission.dedup_key,
        )
        return WebhookAck(
            received=True,
            duplicate=False,
            inbox_id=str(inbox_id),
            event_type=event_type,
        )


def _duplicate(store: PostgresInboxStore, dedup_key: str | None, event_type: str) -> WebhookAck:
    """Answer a redelivery: 200, marked duplicate, with the original row's identifier.

    200 rather than 409. Razorpay retries anything that is not a success, and a redelivery
    of an event already held is not a failure -- it is the provider doing exactly what
    at-least-once delivery means. Answering an error would keep it retrying forever an
    event this platform has already recorded.

    No command is enqueued: the transaction that won the claim enqueued one, and it
    committed with the row.
    """
    existing: uuid.UUID | None = None
    if dedup_key is not None:
        existing = store.bump_duplicate(dedup_key)
    return WebhookAck(
        received=True,
        duplicate=True,
        inbox_id=None if existing is None else str(existing),
        event_type=event_type,
    )
