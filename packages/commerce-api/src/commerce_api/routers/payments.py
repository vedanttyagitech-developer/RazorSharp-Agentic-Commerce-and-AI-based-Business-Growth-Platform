"""Payment handoff and client-return verification.

ADR 0003 D8: verifying the browser callback records ``BROWSER_CALLBACK`` evidence
and enqueues a reconciliation. It never sets a payment to captured. Capture is
applied only from ``WEBHOOK`` or ``PROVIDER_FETCH`` evidence, monotonically.

**Owned by build unit D.**

This module's router carries no prefix, and every path below is written out in full. The
two endpoints it owns hang off different nouns -- ``/v1/checkouts/{id}/payment`` is the
handoff and ``/v1/payments/verify`` is the return -- and the ADR's endpoint catalogue
fixes both spellings. A single router prefix cannot produce them, and the alternative,
scattering one unit's routes across two other units' modules, is how two engineers end up
editing the same file. ``commerce_api.routers.ROUTERS`` includes this router before the
``evidence`` router, which claims the bare ``/v1`` prefix, so these exact paths win.
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from transaction_kernel import PaymentState

from ..deps import (
    AppSession,
    IdempotencyKey,
    KernelSession,
    OwnedCheckout,
    SessionContext,
    assert_owner,
    require_owner,
    settings_of,
)
from ..idempotency import idempotent_mutation, request_fingerprint
from ..services.payment_service import (
    BROWSER_CALLBACK_MESSAGE,
    build_handoff,
    verify_client_return,
)

router = APIRouter(tags=["payments"])


class PaymentHandoffOut(BaseModel):
    """What the storefront needs to open Razorpay Checkout.

    ``razorpay_key_id`` is the public key id and is meant to be in the page; the key
    secret and the webhook secret never leave the server. ``razorpay_order_id`` is
    ``null`` until the worker has created the order under its Execution Grant, which is
    the honest way to say "the authorised operation has not happened yet" -- the API
    never creates a provider order on a read.
    """

    model_config = ConfigDict(extra="forbid")

    checkout_id: str
    version: int
    attempt_id: str | None
    state: PaymentState | None
    provider: Literal["razorpay"] = "razorpay"
    razorpay_key_id: str
    razorpay_order_id: str | None
    amount_minor: int
    currency: str
    merchant_name: str
    description: str
    payment_window_expires_at: str | None = None
    server_now: str | None = None
    window_closed: bool = False


class VerifyRequest(BaseModel):
    """The three values Razorpay Checkout hands the browser, plus the checkout they concern.

    ``checkout_id`` is carried so the request names what it is about, but it is not
    authority: the attempt is found from ``razorpay_order_id`` and ownership comes from
    the bearer session. A mismatch between the two is refused rather than resolved in
    favour of either.
    """

    model_config = ConfigDict(extra="forbid")

    checkout_id: uuid.UUID
    razorpay_order_id: str = Field(min_length=1, max_length=64)
    razorpay_payment_id: str = Field(min_length=1, max_length=64)
    razorpay_signature: str = Field(min_length=1, max_length=256)


class VerifyResponse(BaseModel):
    """ADR 0003 D8, in the shape of a response.

    ``evidence_kind`` is a literal, not a variable. This endpoint can produce exactly one
    kind of evidence and the type says so, so that no future edit can make it claim to
    have produced capture evidence.
    """

    model_config = ConfigDict(extra="forbid")

    accepted: bool
    attempt_id: str | None
    state: PaymentState
    evidence_kind: Literal["BROWSER_CALLBACK"] = "BROWSER_CALLBACK"
    message: str


@router.get(
    "/v1/checkouts/{checkout_id}/payment",
    response_model=PaymentHandoffOut,
    tags=["checkouts"],
    summary="Payment handoff: provider order id, public key id, amount",
)
def payment_handoff(
    checkout_id: uuid.UUID,  # noqa: ARG001 - resolved by require_owner, kept for the path
    request: Request,
    ctx: SessionContext,
    session: AppSession,
    owner: Annotated[OwnedCheckout, Depends(require_owner)],
) -> PaymentHandoffOut:
    """Everything the browser needs to hand this checkout to Razorpay.

    A read on the app role, which physically cannot write a financial table -- so this
    endpoint could not create a provider order even if somebody later tried to make it,
    and the buyer surface can poll it while the worker works.
    """
    handoff = build_handoff(
        session,
        ctx,
        checkout_id=owner.checkout_id,
        merchant_id=owner.merchant_id,
        version=owner.current_version,
        settings=settings_of(request),
    )
    return PaymentHandoffOut(
        checkout_id=str(handoff.checkout_id),
        version=handoff.version,
        attempt_id=None if handoff.attempt_id is None else str(handoff.attempt_id),
        state=handoff.state,
        razorpay_key_id=handoff.razorpay_key_id,
        razorpay_order_id=handoff.razorpay_order_id,
        amount_minor=handoff.amount.minor,
        currency=handoff.amount.currency,
        merchant_name=handoff.merchant_name,
        description=handoff.description,
        payment_window_expires_at=handoff.payment_window_expires_at,
        server_now=handoff.server_now,
        window_closed=handoff.window_closed,
    )


@router.post(
    "/v1/payments/verify",
    response_model=VerifyResponse,
    summary="Record a verified client return (never capture evidence)",
)
def verify_payment(
    body: VerifyRequest,
    request: Request,
    ctx: SessionContext,
    session: KernelSession,
    key: IdempotencyKey,
) -> JSONResponse:
    """Verify the browser's report, record it, and ask the provider what really happened.

    Answers 200 whether or not the kernel accepted the callback, because both outcomes
    are the system working: an accepted callback recorded a payment id, and a refused one
    means a payment id was already recorded and this browser message disagreed with it.
    The buyer surface renders "verifying" for both, which is the truth in both cases.

    401 for a signature that does not verify and 404 for an order id this session does
    not own -- neither writes anything.

    The response body is bound to the ``Idempotency-Key`` before it is returned, so a
    retried callback (a double-clicked "return to store", a browser that resent the form)
    replays the first answer with ``Idempotent-Replayed: true`` rather than enqueuing a
    second reconciliation.
    """
    payload = request_fingerprint(
        body={
            "checkout_id": str(body.checkout_id),
            "razorpay_order_id": body.razorpay_order_id,
            "razorpay_payment_id": body.razorpay_payment_id,
            "razorpay_signature": body.razorpay_signature,
        }
    )
    with idempotent_mutation(session, ctx, key, "PAYMENT_VERIFY", payload) as slot:
        # Ownership is asserted again inside verify_client_return, against the checkout
        # the attempt actually belongs to. This first call refuses a body naming a
        # checkout this session does not own before any provider identifier is looked up.
        assert_owner(session, ctx, body.checkout_id)
        outcome = verify_client_return(
            session,
            ctx,
            checkout_id=body.checkout_id,
            provider_order_id=body.razorpay_order_id,
            provider_payment_id=body.razorpay_payment_id,
            signature=body.razorpay_signature,
            settings=settings_of(request),
        )
        response = VerifyResponse(
            accepted=outcome.accepted,
            attempt_id=str(outcome.attempt_id),
            state=outcome.state,
            message=BROWSER_CALLBACK_MESSAGE,
        ).model_dump(mode="json")
        slot.store(response)
    return JSONResponse(content=response, status_code=200)


class ReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    checkout_id: uuid.UUID


@router.post("/v1/payments/reconcile", summary="Request provider verification of an owned attempt")
def request_reconciliation(
    body: ReconcileRequest, ctx: SessionContext, session: KernelSession
) -> JSONResponse:
    from durable_work.commands import ReconcilePaymentCommand, enqueue_command

    from ..services.payment_service import latest_attempt

    ctx.require("payment.verify")
    assert_owner(session, ctx, body.checkout_id)
    attempt = latest_attempt(session, tenant_id=ctx.tenant_id, checkout_id=body.checkout_id)
    if attempt is None or not (attempt.provider_order_id or "").startswith("order_"):
        return JSONResponse({"queued": False, "reason": "no_provider_attempt"})
    # A buyer who is still choosing a payment method needs a status read, not an
    # unknown-payment escalation chain. Coalesce these reads per server minute so
    # a later capture remains discoverable even after an earlier empty order read.
    status_probe = attempt.state in {PaymentState.SUBMITTED, PaymentState.AUTHORIZED}
    probe_window = int(time.time()) // 60
    recovery_key = f"provider-recovery:{attempt.attempt_id}"
    if status_probe:
        recovery_key = f"{recovery_key}:status:{probe_window}"
    payload = request_fingerprint(body={"attempt_id": str(attempt.attempt_id)})
    with idempotent_mutation(session, ctx, recovery_key, "PAYMENT_RECONCILE", payload) as slot:
        queued = attempt.state in {
            PaymentState.SUBMITTED,
            PaymentState.AUTHORIZED,
            PaymentState.UNKNOWN,
            PaymentState.RECONCILING,
        }
        if queued:
            enqueue_command(
                session,
                ReconcilePaymentCommand(
                    tenant_id=str(ctx.tenant_id),
                    payment_attempt_id=str(attempt.attempt_id),
                    reason=f"buyer_status_{probe_window}" if status_probe else "buyer_recovery",
                    attempt_number=1,
                    correlation_id=str(ctx.correlation_id),
                ),
                idempotency_key=None,
            )
        result = {"queued": queued, "attempt_id": str(attempt.attempt_id)}
        slot.store(result)
    return JSONResponse(result)
