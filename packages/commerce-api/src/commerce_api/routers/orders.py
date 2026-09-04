"""Confirmed orders, their capture evidence, and buyer-confirmed refunds.

An order exists only where verified capture evidence put it there. A refund is a
fresh kernel admission producing a new single-use Execution Grant, never a reused
one (specification 10.6).

**Owned by build unit D.**

Both routes are owner-scoped through the *checkout* the order was made from, not through
the order row itself: ownership lives on ``checkouts.buyer_ref``, and an order that
disagreed with its checkout about who bought it would be a data fault rather than an
authorisation question. A missing order and somebody else's order are both 404, because a
403 would confirm which order identifiers exist.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..deps import AppSession, IdempotencyKey, KernelSession, SessionContext, assert_owner
from ..errors import decision_payload
from ..idempotency import idempotent_mutation, request_fingerprint
from ..schemas import DecisionOut, OrderOut, RefundOut
from ..services.refund_service import load_order, order_payload, request_refund

router = APIRouter(prefix="/v1/orders", tags=["orders"])


class RefundRequest(BaseModel):
    """Ask for money back on a confirmed order.

    ``amount_minor`` omitted means "everything still refundable", resolved by the kernel
    against its own capture ledger. The API deliberately offers no way to compute that
    figure client-side and send it: a caller's arithmetic cannot see a refund that is
    in flight at the provider, and the kernel's can.
    """

    model_config = ConfigDict(extra="forbid")

    amount_minor: int | None = Field(default=None, gt=0)
    reason: str = Field(min_length=1, max_length=64)


class RefundResponse(BaseModel):
    """The kernel's decision, the refund it created, and the order as it now reads.

    ``refund`` is ``null`` on a denial and ``decision`` says why. This is HTTP 200 either
    way (ADR 0003 D15): "nothing left to refund" and "a refund is already in flight" are
    the platform working, and a 4xx would tell every client in the chain to retry consent.
    """

    model_config = ConfigDict(extra="forbid")

    decision: DecisionOut
    refund: RefundOut | None
    order: OrderOut


@router.get(
    "/{order_id}",
    response_model=OrderOut,
    summary="Order status, capture evidence, policy receipt and refunds",
)
def read_order(
    order_id: uuid.UUID,
    ctx: SessionContext,
    session: AppSession,
) -> OrderOut:
    """One confirmed sale, bound to the exact bytes and policy the buyer approved.

    ``payment.capture_evidence`` names how the platform learned the money moved, and it
    is never ``BROWSER_CALLBACK``: an order is written only from ``WEBHOOK`` or
    ``PROVIDER_FETCH`` evidence (ADR 0003 D8), so the field is a standing demonstration
    of that rule rather than a description of it.
    """
    order = load_order(session, ctx, order_id=order_id)
    assert_owner(session, ctx, order.checkout_id)
    return order_payload(session, ctx, order)


@router.post(
    "/{order_id}/refunds",
    response_model=RefundResponse,
    summary="Buyer-confirmed refund under a fresh Execution Grant",
)
def create_refund(
    order_id: uuid.UUID,
    body: RefundRequest,
    ctx: SessionContext,
    session: KernelSession,
    key: IdempotencyKey,
) -> JSONResponse:
    """Admit a refund, enqueue the command that executes it, and link the two.

    Everything below commits in one transaction: the ``refunds`` row, the single-use
    ``REFUND_EXECUTE`` grant bound to it, the outbox command carrying that grant, and the
    link from the grant to the command. A crash anywhere in the middle leaves no refund
    at all rather than a grant nobody will spend or a command with no authority behind it.

    Only a BUYER session may ask. ``refund.request`` is absent from the agent capability
    registry on purpose: a refund is consent about money, and consent is not delegable to
    the thing that proposed the purchase.
    """
    ctx.require("refund.request")
    payload = request_fingerprint(
        path_params={"order_id": order_id},
        body={"amount_minor": body.amount_minor, "reason": body.reason},
    )
    with idempotent_mutation(session, ctx, key, "REFUND_REQUEST", payload) as slot:
        order = load_order(session, ctx, order_id=order_id)
        assert_owner(session, ctx, order.checkout_id)
        requested = request_refund(
            session,
            ctx,
            order=order,
            amount_minor=body.amount_minor,
            reason=body.reason,
        )
        # Re-read the order: admission moved the attempt to REFUND_PENDING and wrote the
        # refunds row, and answering with the pre-admission snapshot would show the buyer
        # a payment state the transaction they are being answered from has already left.
        settled = load_order(session, ctx, order_id=order_id)
        response = RefundResponse(
            decision=decision_payload(requested.decision),
            refund=requested.refund,
            order=order_payload(session, ctx, settled),
        ).model_dump(mode="json")
        slot.store(response)
    return JSONResponse(content=response, status_code=200)
