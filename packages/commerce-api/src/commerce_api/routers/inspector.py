"""The payment-attempt inspector document.

One attempt, all of it: state history in order, the grant lifecycle with its
``consumed_at``, the durable command and its delivery attempts, every provider request
redacted, every webhook delivery with its dedupe status, the reconciliation runs, the
order and the refunds. The debugging surface that makes the invariants checkable by
someone who did not write them.

Why one document rather than six endpoints
------------------------------------------
The invariants worth checking are relationships, not values. "Every provider mutation
consumed exactly one Execution Grant" is only visible when the grant and the provider
request are on the same page; so is "the browser callback did not set this to captured".
A reviewer stitching six responses together by hand will make the mistake this document
exists to prevent.

Nothing here is computed. The ``findings`` block restates relationships that already hold
in the rows above it, and every one names the rows it read, so a disagreement between a
finding and the rows is a bug in this file rather than an unexplained verdict.

Access follows :mod:`commerce_api.routers.evidence` exactly -- an attempt is visible when
its checkout is -- and imports that rule rather than restating it.
"""

from __future__ import annotations

import uuid
from collections import Counter
from typing import Any

import transaction_kernel as tk
from fastapi import APIRouter
from platform_db import ExecutionGrant, Order, ProviderRequest, ReconciliationRun, Refund
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Row, select
from sqlalchemy.orm import Session

from ..deps import AppSession, SessionContext
from ..schemas import rfc3339, uuid_str
from ..services import timeline
from .evidence import Operator, visible_attempt

router = APIRouter(prefix="/v1/inspector", tags=["inspector"])


class _Out(BaseModel):
    """Strict responses, specification 24.1."""

    model_config = ConfigDict(extra="forbid")


class InspectorOut(_Out):
    """One payment attempt, whole.

    The blocks are deliberately flat dictionaries rather than a model per row type: this
    is a debugging document whose value is completeness, and a strict model per block
    would silently drop a column the day the schema gains one.
    """

    payment_attempt_id: str
    checkout_id: str
    checkout_version: int
    state: tk.PaymentState
    amount_minor: int
    currency: str
    receipt: str
    provider_order_id: str | None
    provider_payment_id: str | None

    state_history: list[dict[str, Any]]
    grants: list[dict[str, Any]]
    commands: list[dict[str, Any]]
    provider_requests: list[dict[str, Any]]
    webhook_deliveries: list[dict[str, Any]]
    reconciliation_runs: list[dict[str, Any]]
    order: dict[str, Any] | None
    refunds: list[dict[str, Any]]
    findings: list[dict[str, Any]]


@router.get(
    "/payment-attempts/{payment_attempt_id}",
    response_model=InspectorOut,
    summary="Everything recorded about one payment attempt",
)
def inspect_attempt(
    payment_attempt_id: uuid.UUID,
    ctx: SessionContext,
    session: AppSession,
    operator: Operator,
) -> InspectorOut:
    """Assemble the inspector document for one attempt. Reads only; writes nothing."""
    attempt = visible_attempt(session, ctx, payment_attempt_id, operator=operator)

    grants = _grants(session, ctx.tenant_id, payment_attempt_id)
    requests = _provider_requests(session, ctx.tenant_id, payment_attempt_id)
    commands = _commands(session, ctx.tenant_id, attempt)
    deliveries = _deliveries(session, ctx.tenant_id, attempt)
    runs = _runs(session, ctx.tenant_id, payment_attempt_id)
    order = _order(session, ctx.tenant_id, payment_attempt_id)
    refunds = _refunds(session, ctx.tenant_id, payment_attempt_id)
    history = _state_history(session, ctx.tenant_id, attempt)

    return InspectorOut(
        payment_attempt_id=str(attempt.attempt_id),
        checkout_id=str(attempt.checkout_id),
        checkout_version=attempt.checkout_version,
        state=attempt.status,
        amount_minor=attempt.amount.minor,
        currency=attempt.amount.currency,
        receipt=attempt.receipt,
        provider_order_id=attempt.provider_order_id,
        provider_payment_id=attempt.provider_payment_id,
        state_history=history,
        grants=grants,
        commands=commands,
        provider_requests=requests,
        webhook_deliveries=deliveries,
        reconciliation_runs=runs,
        order=order,
        refunds=refunds,
        findings=_findings(attempt, grants, requests, deliveries, order, refunds),
    )


# --------------------------------------------------------------------------- blocks


def _state_history(
    session: Session, tenant_id: uuid.UUID, attempt: tk.AttemptView
) -> list[dict[str, Any]]:
    """Every recorded move of this attempt, from the audit streams that carry them.

    ``payment_attempts.status`` holds only where the row is *now*. How it got there lives
    in the audit events -- which is the right place for it, because a state history stored
    as a mutable column would be editable by whoever last wrote the column.
    """
    events = [
        *tk.read_stream(
            session,
            tenant=tenant_id,
            aggregate_type="checkout",
            aggregate_id=attempt.checkout_id,
        ),
        *tk.read_stream(
            session,
            tenant=tenant_id,
            aggregate_type="payment_attempt",
            aggregate_id=attempt.attempt_id,
        ),
    ]
    history: list[dict[str, Any]] = []
    for event in events:
        payload = event.payload
        before, after = payload.get("state_before"), payload.get("state_after")
        if before is None and after is None:
            continue
        if payload.get("payment_attempt_id") not in (None, str(attempt.attempt_id)):
            continue
        history.append(
            {
                "occurred_at": rfc3339(event.occurred_at),
                "event_type": event.event_type,
                "actor_type": event.actor_type,
                "state_before": before,
                "state_after": after,
                "changed": payload.get("changed", before != after),
                "reason": payload.get("reason") or payload.get("code"),
                "audit_event_id": str(event.event_id),
                "audit_seq": event.seq,
                "stream": event.aggregate_type,
            }
        )
    history.sort(key=lambda row: (row["occurred_at"], row["stream"], row["audit_seq"]))
    return history


def _grants(session: Session, tenant_id: uuid.UUID, attempt_id: uuid.UUID) -> list[dict[str, Any]]:
    """The grant lifecycle. ``consumed_at`` is the field the single-use claim rests on."""
    rows = session.execute(
        select(ExecutionGrant)
        .where(
            ExecutionGrant.tenant_id == tenant_id,
            ExecutionGrant.payment_attempt_id == attempt_id,
        )
        .order_by(ExecutionGrant.issued_at, ExecutionGrant.id)
    ).scalars()
    return [
        {
            "grant_id": str(row.id),
            "operation": row.operation,
            "status": row.status,
            "checkout_version": row.checkout_version,
            "content_hash": row.content_hash,
            "amount_minor": row.amount_minor,
            "currency": row.currency,
            "kernel_decision_id": str(row.kernel_decision_id),
            "outbox_command_id": uuid_str(row.outbox_command_id),
            "refund_id": uuid_str(row.refund_id),
            "issued_at": rfc3339(row.issued_at),
            "expires_at": rfc3339(row.expires_at),
            "consumed_at": None if row.consumed_at is None else rfc3339(row.consumed_at),
        }
        for row in rows
    ]


def _commands(
    session: Session, tenant_id: uuid.UUID, attempt: tk.AttemptView
) -> list[dict[str, Any]]:
    """The durable commands for this attempt, with their delivery attempts and lease.

    ``attempts`` is the outbox's own counter, so "this refund has been tried six times"
    is a fact from the queue rather than an inference from six log lines.
    """
    rows = timeline.outbox_for(
        session,
        tenant_id=tenant_id,
        checkout_id=attempt.checkout_id,
        attempts=_attempt_rows(session, tenant_id, attempt),
        inbox_ids=[],
    )
    return [
        {
            "outbox_command_id": str(row.command_id),
            "command_type": row.command_type,
            "status": row.status,
            "attempts": row.attempts,
            "correlation_id": str(row.correlation_id),
            "created_at": rfc3339(row.created_at),
            "available_at": rfc3339(row.available_at),
            "leased_until": None if row.leased_until is None else rfc3339(row.leased_until),
            "payload": timeline.redact(row.payload),
        }
        for row in rows
        if row.payload.get("payment_attempt_id") in (None, str(attempt.attempt_id))
    ]


def _attempt_rows(session: Session, tenant_id: uuid.UUID, attempt: tk.AttemptView) -> list[Any]:
    """The ORM rows ``timeline.outbox_for`` needs, narrowed to this attempt."""
    return [
        row
        for row in timeline.attempts_of(
            session, tenant_id=tenant_id, checkout_id=attempt.checkout_id
        )
        if row.id == attempt.attempt_id
    ]


def _provider_requests(
    session: Session, tenant_id: uuid.UUID, attempt_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Every HTTP call recorded against this attempt.

    Already redacted at rest by the kernel -- the URL carries no query string, the body is
    a digest and only header *names* are stored -- so this block can be shown to a reviewer
    without a second sanitising pass changing what they see.
    """
    rows = session.execute(
        select(ProviderRequest)
        .where(
            ProviderRequest.tenant_id == tenant_id,
            ProviderRequest.payment_attempt_id == attempt_id,
        )
        .order_by(ProviderRequest.request_at, ProviderRequest.id)
    ).scalars()
    return [
        {
            "provider_request_id": str(row.id),
            "operation": row.operation,
            "method": row.method,
            "url": row.url,
            "body_hash": row.body_hash,
            "header_names": list(row.header_names),
            "http_status": row.http_status,
            "provider_id": row.provider_id,
            "outcome_code": row.outcome_code,
            "provider_error_code": row.provider_error_code,
            "response_digest": row.response_digest,
            "transport_error": row.transport_error,
            "grant_id": uuid_str(row.grant_id),
            "refund_id": uuid_str(row.refund_id),
            "request_at": rfc3339(row.request_at),
        }
        for row in rows
    ]


def _deliveries(
    session: Session, tenant_id: uuid.UUID, attempt: tk.AttemptView
) -> list[dict[str, Any]]:
    """Webhook deliveries naming this attempt's provider ids, with their dedupe status.

    ``duplicate_count`` above zero is the redelivery evidence of specification 31.1 step
    14: the second delivery of one event increments a counter and changes no state.
    """
    rows: tuple[Row[Any], ...] = timeline.webhooks_of(
        session,
        tenant_id=tenant_id,
        attempts=_attempt_rows(session, tenant_id, attempt),
    )
    return [
        {
            "inbox_id": str(row.id),
            "dedup_key": row.dedup_key,
            "provider_event_id": row.provider_event_id,
            "event_type": row.event_type,
            "signature_verified": row.signature_verified,
            "body_digest": row.body_digest,
            "apply_status": row.apply_status,
            "apply_reason": row.apply_reason,
            "state_before": row.state_before,
            "state_after": row.state_after,
            "changed": row.changed,
            "duplicate": row.duplicate_count > 0,
            "duplicate_count": row.duplicate_count,
            "outbox_command_id": uuid_str(row.outbox_command_id),
            "received_at": rfc3339(row.received_at),
            "applied_at": None if row.applied_at is None else rfc3339(row.applied_at),
        }
        for row in rows
    ]


def _runs(session: Session, tenant_id: uuid.UUID, attempt_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = session.execute(
        select(ReconciliationRun)
        .where(
            ReconciliationRun.tenant_id == tenant_id,
            ReconciliationRun.payment_attempt_id == attempt_id,
        )
        .order_by(ReconciliationRun.attempt_number, ReconciliationRun.created_at)
    ).scalars()
    return [
        {
            "reconciliation_run_id": str(row.id),
            "attempt_number": row.attempt_number,
            "reason": row.reason,
            "identifiers_queried": timeline.redact(row.identifiers_queried),
            "raw_evidence_digest": row.raw_evidence_digest,
            "decision": row.decision,
            "resulting_transition": row.resulting_transition,
            "next_scheduled_attempt": None
            if row.next_scheduled_attempt is None
            else rfc3339(row.next_scheduled_attempt),
            "refund_id": uuid_str(row.refund_id),
            "correlation_id": str(row.correlation_id),
            "created_at": rfc3339(row.created_at),
        }
        for row in rows
    ]


def _order(session: Session, tenant_id: uuid.UUID, attempt_id: uuid.UUID) -> dict[str, Any] | None:
    row = session.execute(
        select(Order).where(Order.tenant_id == tenant_id, Order.payment_attempt_id == attempt_id)
    ).scalar_one_or_none()
    if row is None:
        return None
    return {
        "order_id": str(row.id),
        "state": row.status,
        "checkout_version": row.checkout_version,
        "policy_receipt_id": str(row.policy_receipt_id),
        "policy_receipt_hash": row.policy_receipt_hash,
        "amount_minor": row.total_minor,
        "currency": row.currency,
        "capture_evidence": timeline.redact(dict(row.capture_evidence)),
        "created_at": rfc3339(row.created_at),
    }


def _refunds(session: Session, tenant_id: uuid.UUID, attempt_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = session.execute(
        select(Refund)
        .where(Refund.tenant_id == tenant_id, Refund.payment_attempt_id == attempt_id)
        .order_by(Refund.created_at, Refund.id)
    ).scalars()
    return [
        {
            "refund_id": str(row.id),
            "state": row.status,
            "amount_minor": row.amount_minor,
            "currency": row.currency,
            "reason_code": row.reason_code,
            "idem_key": row.idem_key,
            "provider_refund_id": row.provider_refund_id,
            "provider_originated": row.provider_originated,
            "created_at": rfc3339(row.created_at),
            "updated_at": rfc3339(row.updated_at),
        }
        for row in rows
    ]


# -------------------------------------------------------------------------- findings


def _findings(
    attempt: tk.AttemptView,
    grants: list[dict[str, Any]],
    requests: list[dict[str, Any]],
    deliveries: list[dict[str, Any]],
    order: dict[str, Any] | None,
    refunds: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The invariants this document exists to make checkable, restated over its own rows.

    Each finding names the rows it read. A reader who disbelieves one can recount it from
    the blocks above without leaving the page, which is the difference between a document
    that is evidence and a document that is a verdict.
    """
    mutation_ops = {op.value for op in tk.Operation}
    mutations = [row for row in requests if row["operation"] in mutation_ops]
    consumed = [row for row in grants if row["consumed_at"] is not None]
    ungranted = [row for row in mutations if row["grant_id"] is None]
    # Two grants cannot authorize one call and one grant cannot authorize two: the second
    # is the failure mode that matters, because it is what a replayed command looks like.
    per_grant = Counter(str(row["grant_id"]) for row in mutations if row["grant_id"])
    reused = sorted(grant_id for grant_id, count in per_grant.items() if count > 1)
    callback_only = [
        row
        for row in deliveries
        if row["apply_status"] == "APPLIED" and not row["signature_verified"]
    ]
    duplicates = [row for row in deliveries if row["duplicate_count"] > 0]
    settled = sum(row["amount_minor"] for row in refunds if row["state"] in ("PROCESSED",))
    return [
        {
            "name": "one_grant_per_provider_mutation",
            "ok": not ungranted and not reused,
            "detail": (
                f"{len(mutations)} provider mutation(s), {len(consumed)} consumed grant(s), "
                f"{len(ungranted)} mutation(s) with no grant, "
                f"{len(reused)} grant(s) used more than once"
            ),
        },
        {
            "name": "no_unconsumed_grant_left_live",
            "ok": all(row["status"] != "ISSUED" for row in grants)
            or attempt.status.value in ("CREATED", "SUBMITTED"),
            "detail": "; ".join(f"{row['grant_id']}={row['status']}" for row in grants) or "none",
        },
        {
            "name": "capture_came_from_a_verified_channel",
            "ok": order is None
            or str(order["capture_evidence"].get("source", "")) != "BROWSER_CALLBACK",
            "detail": "no order"
            if order is None
            else f"order confirmed from {order['capture_evidence'].get('source')!r}",
        },
        {
            "name": "every_applied_webhook_was_signature_verified",
            "ok": not callback_only,
            "detail": f"{len(deliveries)} delivery(ies), {len(callback_only)} applied unverified",
        },
        {
            "name": "redeliveries_changed_nothing",
            "ok": all(row["changed"] in (None, False) for row in duplicates),
            "detail": f"{len(duplicates)} redelivered event(s)",
        },
        {
            "name": "refunds_within_capture",
            "ok": settled <= attempt.amount.minor,
            "detail": f"{settled} refunded of {attempt.amount.minor} {attempt.amount.currency}",
        },
    ]
