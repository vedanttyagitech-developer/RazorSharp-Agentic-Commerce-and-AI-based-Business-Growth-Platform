"""Confirmed orders, their capture evidence, buyer-confirmed refunds, and support reads.

An order exists only where verified capture evidence put it there. A refund is a
fresh kernel admission producing a new single-use Execution Grant, never a reused
one (specification 10.6).

**Owned by build unit D.**

Every route here is owner-scoped through the *checkout* the order was made from, not
through the order row itself: ownership lives on ``checkouts.buyer_ref``, and an order that
disagreed with its checkout about who bought it would be a data fault rather than an
authorisation question. A missing order and somebody else's order are both 404, because a
403 would confirm which order identifiers exist.

Why the two support reads live here and not on ``routers/review.py``
--------------------------------------------------------------------
``GET /{order_id}/policy`` and ``GET /{order_id}/resolution`` answer the two questions the
Support Specialist (specification 6.4.4, ``Surface.BUYER``) has to ask before it may quote
anything. The review router already computes a resolution -- but it is keyed by
``payment_attempt_id`` and gated by ``X-Scenario-Key``, which is an operator surface by
construction: a buyer-facing agent pointed at it could read findings about other people's
stuck payments. So they are keyed by an *order the caller owns* and scoped by the same
ownership test the rest of this module uses, and the order -> attempt -> projection ->
findings walk happens server-side where the tenant is bound to the transaction.

Both are reads on the app role. Neither issues a plan, neither records one, and neither
moves money; ``resolution_service`` is authoritative for every figure they carry.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from transaction_kernel import CheckoutRef
from transaction_kernel.receipts import policy_for_order

from ..deps import (
    AppSession,
    IdempotencyKey,
    KernelSession,
    RequestContext,
    SessionContext,
    assert_owner,
)
from ..errors import ProblemError, decision_payload
from ..idempotency import idempotent_mutation, request_fingerprint
from ..schemas import DecisionOut, OrderOut, OrdersPageOut, OrderState, RefundOut
from ..services import listing
from ..services import reconciliation_service as recon
from ..services import resolution_service as resolve
from ..services.refund_service import OrderRecord, load_order, order_payload, request_refund
from .evidence import Operator

# The wire shape for a resolution is the review router's, imported rather than restated.
# Two definitions of a plan would eventually disagree about a money field, and the
# disagreement would surface as an operator console and a buyer being told different
# amounts for the same finding. Importing the model carries none of that router's
# dependencies: ``require_scenario_key`` is attached to its ``APIRouter``, not to its
# response classes, so the gate that belongs to the operator surface stays there.
from .review import ResolutionOut

router = APIRouter(prefix="/v1/orders", tags=["orders"])


def _assert_order_owner(
    session: Session, ctx: RequestContext, order: OrderRecord, order_id: uuid.UUID
) -> None:
    """Refuse an order this session does not own *as though it did not exist*.

    ``load_order`` is tenant-scoped, so a same-tenant order belonging to another buyer is
    a row this function is reached with. Calling :func:`assert_owner` on the order's
    checkout directly here answers ``404 "Checkout not found"`` and echoes the order's
    ``checkout_id`` -- a body that differs from the ``404 "Order not found"`` a genuinely
    missing id produces (an existence oracle) and that discloses a cross-buyer identifier
    besides. Both are exactly what this module's docstring forbids. So an ownership
    failure is translated into the identical response ``load_order`` raises for an id that
    is not there: same title, same detail, the order id echoed and never the checkout's.
    """
    try:
        assert_owner(session, ctx, order.checkout_id)
    except ProblemError:
        raise ProblemError(
            404,
            "Order not found",
            "No order with that identifier belongs to this session.",
            order_id=str(order_id),
        ) from None


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
    "",
    response_model=OrdersPageOut,
    summary="Orders in this scope, newest first, with counts by state",
)
def list_orders(
    ctx: SessionContext,
    session: AppSession,
    operator: Operator,
    status: Annotated[OrderState | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=listing.MAX_PAGE_SIZE)] = listing.DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
) -> OrdersPageOut:
    """A buyer sees their own orders; a scenario-key operator sees the tenant's.

    Keyset-paginated on ``(created_at, id)``: hand ``next_cursor`` back as ``cursor``
    and the page after it never repeats or skips a row, however many orders land in
    between. ``scope`` says which of the two views the caller received.
    """
    return listing.list_orders(
        session, ctx, operator=operator, status=status, limit=limit, cursor=cursor
    )


@router.get(
    "/{order_id}",
    response_model=OrderOut,
    summary="Order status, capture evidence, policy receipt and refunds",
)
def read_order(
    order_id: uuid.UUID,
    ctx: SessionContext,
    session: AppSession,
    operator: Operator,
) -> OrderOut:
    """One confirmed sale, bound to the exact bytes and policy the buyer approved.

    ``payment.capture_evidence`` names how the platform learned the money moved, and it
    is never ``BROWSER_CALLBACK``: an order is written only from ``WEBHOOK`` or
    ``PROVIDER_FETCH`` evidence (ADR 0003 D8), so the field is a standing demonstration
    of that rule rather than a description of it.
    """
    order = load_order(session, ctx, order_id=order_id)
    # A scenario-key operator may open any order in the tenant, which is what makes the
    # console's list clickable; a buyer only their own. Same rule as the evidence routes.
    if not operator:
        _assert_order_owner(session, ctx, order, order_id)
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
        _assert_order_owner(session, ctx, order, order_id)
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


# ------------------------------------------------------- the two support-surface reads


class PolicyDocumentOut(BaseModel):
    """One merchant rule as the Policy-at-Sale Receipt froze it, never as it reads today.

    ``terms`` is the receipt's own mapping, handed through unchanged. It is not flattened
    into a sentence here: the Support Specialist quotes a term and cites ``policy_id`` and
    ``policy_version`` beside it, and a summary written at this layer would be a second
    author of the rule with no version of its own.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str
    policy_id: str
    policy_version: int
    applies_to: list[str]
    terms: dict[str, Any]
    document_ref: str | None
    document_hash: str | None


class OrderPolicyOut(BaseModel):
    """The rules one sale was made under, and whether they can be relied on.

    ``binding_ok`` is the whole point of the response rather than a status field beside
    it. ``policy_for_order`` refuses to hand back terms when the checkout/receipt binding
    does not re-derive from stored rows, so a broken binding arrives here as
    ``binding_ok: false``, an empty ``policies`` list and the :class:`RecoveryCode` that
    says which way it broke. That is not an error: a sale whose receipt does not verify is
    a sale nobody can quote a rule for, and the honest answer is to say so rather than to
    return the merchant's current policy in its place.
    """

    model_config = ConfigDict(extra="forbid")

    order_id: str
    checkout_id: str
    checkout_version: int
    binding_ok: bool
    binding_code: str
    binding_reason: str
    policy_receipt_id: str | None
    policy_receipt_hash: str | None
    policies: list[PolicyDocumentOut]


class OrderResolutionOut(BaseModel):
    """What would settle every finding on this order, evaluated in one read transaction.

    An empty ``resolutions`` list is a measurement and not a gap: the Reconciliation
    Service looked at this order's payment attempt and found nothing diverging, so there
    is nothing for the Resolution Service to price. ``findings`` carries the same figure
    the list length does, so a client that renders "no remedy" can say *why* without
    inferring it from an absence.

    ``plan_ttl_seconds`` is the window one evaluation may be relied on for. It is on the
    wire because a plan quoted from a stale ledger is how a buyer is promised money that
    is no longer refundable, and a client that re-renders an old response needs to be able
    to tell that it is doing so.
    """

    model_config = ConfigDict(extra="forbid")

    order_id: str
    payment_attempt_id: str
    recorded_state: str
    findings: int
    resolutions: list[ResolutionOut]
    plan_ttl_seconds: int


@router.get(
    "/{order_id}/policy",
    response_model=OrderPolicyOut,
    summary="The Policy-at-Sale Receipt terms this order was sold under",
)
def read_order_policy(
    order_id: uuid.UUID,
    ctx: SessionContext,
    session: AppSession,
) -> OrderPolicyOut:
    """The frozen rules governing one sale, read the way the Resolution Service reads them.

    Resolved through :func:`transaction_kernel.receipts.policy_for_order`, which is the
    only resolver on this platform with no parameter, flag or fallback that reaches a live
    merchant-policy table. A merchant who tightened their refund rule yesterday therefore
    cannot narrow a sale made last week, and that promise is kept by the *absence* of a
    route into the current terms rather than by a caller remembering not to take one.

    ``policy.search`` and not ``order.read``: a buyer session may open its own order and
    already sees ``policy_receipt_hash`` there, which is an integrity handle. The terms
    themselves are the Support Specialist's read, and giving them their own capability is
    what lets that agent hold them without holding anything else.
    """
    ctx.require("policy.search")
    order = load_order(session, ctx, order_id=order_id)
    _assert_order_owner(session, ctx, order, order_id)
    policy = policy_for_order(
        session,
        CheckoutRef(order.checkout_id, order.checkout_version, order.content_hash),
    )
    documents: list[PolicyDocumentOut] = []
    if policy.content is not None:
        documents = [
            PolicyDocumentOut(
                kind=str(entry["kind"]),
                policy_id=str(entry["policy_id"]),
                policy_version=int(entry["policy_version"]),
                applies_to=[str(target) for target in entry["applies_to"]],
                terms=dict(entry["terms"]),
                document_ref=entry.get("document_ref"),
                document_hash=entry.get("document_hash"),
            )
            for entry in policy.policies
        ]
    return OrderPolicyOut(
        order_id=str(order.order_id),
        checkout_id=str(order.checkout_id),
        checkout_version=order.checkout_version,
        binding_ok=policy.ok,
        binding_code=policy.code.value,
        binding_reason=str(policy.reason),
        policy_receipt_id=None if policy.receipt_id is None else str(policy.receipt_id),
        policy_receipt_hash=policy.receipt_hash,
        policies=documents,
    )


@router.get(
    "/{order_id}/resolution",
    response_model=OrderResolutionOut,
    summary="What would settle each finding on this order, with exact amounts",
)
def read_order_resolution(
    order_id: uuid.UUID,
    ctx: SessionContext,
    session: AppSession,
) -> OrderResolutionOut:
    """Every finding on this order's payment attempt, and the plan that would settle each.

    The order is resolved to its attempt here rather than by the caller, because a
    ``payment_attempt_id`` is not an identifier a buyer surface holds and a route that
    accepted one would be a route on which a Support Specialist could name somebody else's
    stuck payment. The projection and the resolutions are read in the same transaction, so
    the ledger a plan was priced against is the ledger reported beside it.

    Keying on an order narrows *which* findings are reachable here, and the narrowing is
    the right one rather than an accident of the design. ``STALE_CAPTURE`` and
    ``PAYMENT_UNKNOWN`` confirm no order at all (specification 10.8: a capture that can
    never be fulfilled writes no ``orders`` row), so neither has a buyer-facing subject to
    ask about and both stay on the operator surface where a reviewer who can act on them
    is. What reaches this route is the post-purchase set -- a refund whose provider answer
    was lost, a provider reporting refunds this platform did not record, an escalated case
    -- which is exactly specification 6.4.4's remit.

    Nothing here is a promise. ``recorded`` is false on every resolution because no
    ``resolution_plans`` row exists in P0, so nothing can be confirmed against these plan
    ids and no remedy is applied by returning one. A remedy still travels the ordinary
    path: a buyer-confirmed refund through ``POST /v1/orders/{id}/refunds``, or the
    operator path for anything else.
    """
    ctx.require("resolution.evaluate")
    order = load_order(session, ctx, order_id=order_id)
    _assert_order_owner(session, ctx, order, order_id)
    projection = recon.project(
        session, tenant_id=ctx.tenant_id, payment_attempt_id=order.attempt.attempt_id
    )
    if projection is None:  # pragma: no cover - orders.payment_attempt_id is a foreign key
        raise ProblemError(
            500,
            "Order has no payment attempt",
            None,
            order_id=str(order_id),
        )
    resolutions = [
        resolve.evaluate(session, tenant_id=ctx.tenant_id, finding=finding, projection=projection)
        for finding in projection.findings
    ]
    return OrderResolutionOut(
        order_id=str(order.order_id),
        payment_attempt_id=str(projection.payment_attempt_id),
        recorded_state=projection.recorded_state.value,
        findings=len(projection.findings),
        resolutions=[ResolutionOut.of(item) for item in resolutions],
        plan_ttl_seconds=resolve.PLAN_TTL_SECONDS,
    )
