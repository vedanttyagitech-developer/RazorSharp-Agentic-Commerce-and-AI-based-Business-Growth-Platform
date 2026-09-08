"""Trusted approval, holding, rejection, submission and cancellation.

Steps 4 and 6 to 8 of the demonstration. An approval binds to one immutable version
and its canonical hash; a submit is a kernel admission whose decision is returned
verbatim as HTTP 200, denial included (ADR 0003 D15).

Three of the routes here are the buyer answering the same question. ``approve-and-pay``
is yes, and does the approval and the admission under one lock so the promise of a single
confirmation is kept by the kernel and not by the browser. ``hold`` is not now, and
changes nothing: the version stays open, the stock stays held, the hash stays approvable,
and only the fact that the buyer was asked is written down. ``reject`` is no, and retires
the version. ``approve`` and ``submit`` remain as the two halves anyone can still drive
one at a time, which is how the price-shift walkthrough is meant to be watched.

Shares the ``/v1/checkouts`` prefix with :mod:`commerce_api.routers.checkouts`:
FastAPI merges routers on one prefix, so the two units never touch the same file.

Owned by build unit B. Every route here is a mutation, so every route declares
``key: IdempotencyKey`` and wraps its work in
:func:`commerce_api.idempotency.idempotent_mutation`: a retried submit must replay its
stored decision rather than run a second admission.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..deps import IdempotencyKey, KernelSession, SessionContext, merchant_registry
from ..idempotency import idempotent_mutation, request_fingerprint
from ..merchants import MerchantRegistry
from ..services import admission_service

router = APIRouter(prefix="/v1/checkouts", tags=["approvals"])

Registry = Annotated[MerchantRegistry, Depends(merchant_registry)]


class ApproveRequest(BaseModel):
    """The buyer echoes exactly what was displayed.

    All three fields are compared against the locked version row by
    :func:`transaction_kernel.record_approval`, and a mismatch is refused. That is what
    makes an approval a decision about *bytes* rather than about a checkout id whose
    contents somebody could have changed in between.
    """

    model_config = ConfigDict(extra="forbid")

    content_hash: Annotated[str, Field(min_length=1, max_length=128)]
    amount_minor: Annotated[int, Field(ge=0)]
    currency: Annotated[str, Field(min_length=3, max_length=3)]


class RejectRequest(BaseModel):
    """A decline, naming the bytes being declined and why."""

    model_config = ConfigDict(extra="forbid")

    content_hash: Annotated[str, Field(min_length=1, max_length=128)]
    reason: Annotated[str, Field(min_length=1, max_length=64)] = "buyer_declined"


class HoldRequest(BaseModel):
    """A "not now", naming the bytes the buyer was shown and passed on.

    The same echo as every other decision on this router, and for the same reason: an
    audit event saying the buyer declined a card has to name the card, or a client
    rendering a stale one would write evidence about a screen nobody saw.
    """

    model_config = ConfigDict(extra="forbid")

    content_hash: Annotated[str, Field(min_length=1, max_length=128)]
    reason: Annotated[str, Field(min_length=1, max_length=64)] = "buyer_not_now"


class CancelRequest(BaseModel):
    """A cancellation request. The reason is a stable key, not prose for a human."""

    model_config = ConfigDict(extra="forbid")

    reason: Annotated[str, Field(min_length=1, max_length=64)] = "buyer_cancelled"


@router.post(
    "/{checkout_id}/versions/{version}/approve",
    summary="Record the buyer's approval of this exact version",
)
def approve_version(
    checkout_id: uuid.UUID,
    version: int,
    body: ApproveRequest,
    ctx: SessionContext,
    session: KernelSession,
    key: IdempotencyKey,
) -> JSONResponse:
    """Steps 4 and 8: the trusted surface records consent, bound to the hash it showed.

    Only a buyer session holds ``checkout.approve``. An agent session deliberately does
    not: consent is not delegable to the thing that proposed the purchase
    (specification 5.3, Registry A against Registry B).
    """
    payload = request_fingerprint(
        path_params={"checkout_id": checkout_id, "version": version},
        body=body.model_dump(),
    )
    with idempotent_mutation(session, ctx, key, "CHECKOUT_APPROVE", payload) as slot:
        result = admission_service.approve_version(
            session,
            ctx,
            checkout_id=checkout_id,
            version=version,
            content_hash=body.content_hash,
            amount_minor=body.amount_minor,
            currency=body.currency,
        )
        slot.store(result)
    return JSONResponse(content=result)


@router.post(
    "/{checkout_id}/versions/{version}/approve-and-pay",
    summary="One confirmation: record the approval and admit it in one transaction",
)
def approve_and_pay_version(
    checkout_id: uuid.UUID,
    version: int,
    body: ApproveRequest,
    ctx: SessionContext,
    session: KernelSession,
    registry: Registry,
    key: IdempotencyKey,
) -> JSONResponse:
    """Steps 4, 6 and 7 as the single press the buyer actually made. **Always HTTP 200**.

    Same body as ``approve``, because it is the same decision about the same bytes. What
    is different is that the admission happens under the lock the approval took, so there
    is no instant at which this version is ``APPROVED`` with nothing spending it, and
    ``one confirmation`` is a property of the kernel rather than of a screen that fires
    ``submit`` for you.

    The response is the submit decision with the recorded approval alongside it, so a
    caller reads ``allowed`` exactly as it does on ``submit`` and needs no second request
    to learn what was consented to. A price that moved denies with ``REAPPROVAL_REQUIRED``
    and version N+1's card, which is the demonstration, not a failure of it.

    ``approve`` and ``submit`` are both still here and both unchanged. This does not
    replace them: the price-shift walkthrough drives the two of them deliberately, one
    step at a time, because seeing the approved version sit there is the point of it.
    """
    payload = request_fingerprint(
        path_params={"checkout_id": checkout_id, "version": version},
        body=body.model_dump(),
    )
    try:
        with idempotent_mutation(session, ctx, key, "CHECKOUT_APPROVE_AND_PAY", payload) as slot:
            result = admission_service.approve_and_submit(
                session,
                ctx,
                registry,
                checkout_id=checkout_id,
                version=version,
                content_hash=body.content_hash,
                amount_minor=body.amount_minor,
                currency=body.currency,
                idempotency_key=key,
            )
            slot.store(result)
    except admission_service.ConcurrentAdmission:
        # Exactly as ``submit`` handles it, and for the same reason: ``admit`` has rolled
        # the whole transaction back, this request's approval and idempotency claim with
        # it, so the answer has to be assembled outside the block. Nothing was consented
        # to that survived, and the key stays usable.
        result = admission_service.duplicate_after_race(session, ctx, checkout_id)
    return JSONResponse(content=result)


@router.post(
    "/{checkout_id}/versions/{version}/hold",
    summary="Record that the buyer was asked and declined for now",
)
def defer_version(
    checkout_id: uuid.UUID,
    version: int,
    body: HoldRequest,
    ctx: SessionContext,
    session: KernelSession,
    key: IdempotencyKey,
) -> JSONResponse:
    """The buyer said not now. The version, the cart and the hold are all left alone.

    Distinct from ``reject`` on purpose. A buyer who declines at the card has not asked
    for their cart to be cancelled and their stock returned; they have declined to pay
    at this moment. So this writes one ``approval.held`` audit event and applies no
    transition at all, and the same version and the same hash can be approved afterwards.
    ``reject`` remains the endpoint for an actual cancellation.
    """
    payload = request_fingerprint(
        path_params={"checkout_id": checkout_id, "version": version},
        body=body.model_dump(),
    )
    with idempotent_mutation(session, ctx, key, "CHECKOUT_HOLD", payload) as slot:
        result = admission_service.defer_version(
            session,
            ctx,
            checkout_id=checkout_id,
            version=version,
            content_hash=body.content_hash,
            reason=body.reason,
        )
        slot.store(result)
    return JSONResponse(content=result)


@router.post(
    "/{checkout_id}/versions/{version}/reject",
    summary="Decline this version and release its reservation",
)
def reject_version(
    checkout_id: uuid.UUID,
    version: int,
    body: RejectRequest,
    ctx: SessionContext,
    session: KernelSession,
    key: IdempotencyKey,
) -> JSONResponse:
    """The buyer declines. The version is retired and the stock goes back on the shelf."""
    payload = request_fingerprint(
        path_params={"checkout_id": checkout_id, "version": version},
        body=body.model_dump(),
    )
    with idempotent_mutation(session, ctx, key, "CHECKOUT_REJECT", payload) as slot:
        result = admission_service.reject_version(
            session,
            ctx,
            checkout_id=checkout_id,
            version=version,
            content_hash=body.content_hash,
            reason=body.reason,
        )
        slot.store(result)
    return JSONResponse(content=result)


@router.post(
    "/{checkout_id}/versions/{version}/submit",
    summary="Kernel admission: one attempt and one grant, or an exact delta",
)
def submit_version(
    checkout_id: uuid.UUID,
    version: int,
    ctx: SessionContext,
    session: KernelSession,
    registry: Registry,
    key: IdempotencyKey,
) -> JSONResponse:
    """Steps 6 and 7, the headline. **Always HTTP 200** (ADR 0003 D15).

    Allowed: exactly one payment attempt and exactly one Execution Grant exist, and the
    create-order command that carries that grant to the worker is in the outbox, linked
    to it, committed in the same transaction.

    Denied with ``REAPPROVAL_REQUIRED``: no attempt was created, no Razorpay order will
    be, version N is permanently invalidated, and version N+1 is already waiting with its
    own Policy-at-Sale Receipt, its own hold and the exact deltas. The response carries
    the deltas and ``next_version`` so the buyer can be shown what moved and approve the
    new total. That refusal is the product feature, not a failure of it -- which is why
    it is a 200 and not a 409.
    """
    payload = request_fingerprint(path_params={"checkout_id": checkout_id, "version": version})
    try:
        with idempotent_mutation(session, ctx, key, "PAYMENT_CREATE_ORDER", payload) as slot:
            result = admission_service.submit_checkout(
                session,
                ctx,
                registry,
                checkout_id=checkout_id,
                version=version,
                idempotency_key=key,
            )
            slot.store(result)
    except admission_service.ConcurrentAdmission:
        # The single-winner index refused this attempt and the kernel rolled the whole
        # transaction back, this request's idempotency claim included. Answer with the
        # winner rather than with a 500: one live attempt is the correct state, and the
        # unused key stays usable for a retry.
        result = admission_service.duplicate_after_race(session, ctx, checkout_id)
    return JSONResponse(content=result)


@router.post(
    "/{checkout_id}/cancel",
    summary="Cancel within policy, or say why not",
)
def cancel_checkout(
    checkout_id: uuid.UUID,
    body: CancelRequest,
    ctx: SessionContext,
    session: KernelSession,
    key: IdempotencyKey,
) -> JSONResponse:
    """Cancel the current version. **Always HTTP 200**, allowed or refused.

    Once the checkout is ``AWAITING_PAYMENT`` or ``PAYMENT_UNKNOWN`` the state table
    refuses, and that refusal comes back as a structured code rather than as an error:
    money may already be moving, and cancelling a checkout somebody has been charged for
    is the one outcome worse than not cancelling. The remedy is invalidation and
    reconciliation (specification 10.8).
    """
    payload = request_fingerprint(path_params={"checkout_id": checkout_id}, body=body.model_dump())
    with idempotent_mutation(session, ctx, key, "CHECKOUT_CANCEL", payload) as slot:
        result = admission_service.cancel_checkout(
            session, ctx, checkout_id=checkout_id, reason=body.reason
        )
        slot.store(result)
    return JSONResponse(content=result)
