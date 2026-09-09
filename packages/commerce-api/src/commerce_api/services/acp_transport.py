"""What an ACP checkout session *is* in this platform, and what its five operations do.

``commerce_protocols.acp`` authenticates an external AI buyer, maps its request onto one
typed intent, and enforces the version, approval, revocation and payment-state invariants
on the way through. It knows nothing about carts. This module is the other half: it
projects this platform's own state into the :class:`~commerce_protocols.acp.AcpSession`
that mapping reasons over, and then honours the intent by calling the same services the
buyer's browser calls.

An ACP session is a cart
--------------------------
ACP's session is a mutable document an external buyer edits until it is happy. This
platform has a cart, and then an immutable hashed checkout version built from it. Those
are the same object at two stages of its life, so the ACP session id **is** the cart id,
and the projection is read from real rows every time rather than kept anywhere:

=========================  ===================================================
``session_id``             ``carts.id``
``status``                 derived from the cart, its checkout and its approval
``checkout_id/version``    the ``checkouts`` row built from that cart
``content_hash``           the current version's canonical hash
``approved_version``       the live ``RECORDED`` approval, and nothing else
``order_id``               the ``orders`` row, once capture evidence wrote one
=========================  ===================================================

No new table, and the reason is worth stating rather than assuming: a durable ACP session
record would be a second answer to "what did the buyer agree to", and the first answer --
the approval bound to a content hash -- is the one the kernel enforces. Two records of one
fact eventually disagree, and the one that would lose is the one that matters.

One thing this platform does not store, said plainly
----------------------------------------------------
``REQUIRED_FOR_READINESS`` is ``{items, buyer, fulfillment}``. Items are the cart's lines
and the buyer is the credential's ``buyer_ref``, both durable. **Fulfilment is not stored
anywhere**: the merchant simulator prices delivery from a policy rather than from an
address, so there is no column for one and inventing a place to keep it would be inventing
state the platform does not otherwise have.

So a fulfilment block is accepted, recorded verbatim in the evidence chain, and counted
toward readiness for the request that carries it. A caller that sends items and fulfilment
together -- which is what an ACP client does -- reaches a frozen version in one call. A
caller that sends fulfilment first and items later must repeat the fulfilment block on the
second call. That is a narrowing of the public contract, it is inside the
``COMPATIBLE_INTERFACE`` boundary specification 13.2 pins ACP at, and the projection echoes
``supplied`` on every response so a client can see exactly what this platform counted.

What an ACP caller still cannot do
----------------------------------
Approve. There is no ``APPROVE`` in ``IntentKind``, no consent capability in the protocol
ceiling, and :func:`~commerce_protocols.acp.map_request` refuses a completion that is not
backed by an approval recorded on the trusted surface against the exact version and hash
being completed. A completion carries payment credentials the external platform collected
from its own user; their presence is not this platform's buyer having agreed to anything,
and the refusal is the absence of a representable request rather than a policy that a later
edit could relax.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from commerce_domain import AdmissionDecision, Money
from commerce_protocols.acp import (
    MAX_BODY_BYTES,
    AcpOperation,
    AcpSession,
    AcpSessionStatus,
    AdmittedRequest,
    TokenBucketLimiter,
)
from commerce_protocols.acp.sessions import MUTATION_OPERATION, REQUIRED_FOR_READINESS
from commerce_protocols.core import SchemaRejected, StateRejected
from platform_db import Approval, Cart, Checkout, DelegatedAuthority, Order
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from transaction_kernel import TERMINAL_PAYMENT_STATES, CheckoutState, PaymentState, read_versions

from ..deps import RequestContext
from ..merchants import MerchantRegistry
from . import admission_service, cart_service, checkout_service

__all__ = [
    "MAX_BODY_BYTES",
    "MAX_ITEMS_PER_REQUEST",
    "MUTATION_OPERATION",
    "AcpOutcome",
    "Projected",
    "honour",
    "load_session",
    "new_limiter",
    "session_document",
    "supplied_by",
]

#: How many line items one ACP request may name. A quick-commerce cart is a handful of
#: things; a request naming hundreds is not a shopper, and each one is a lock on a cart
#: row and a re-quote against merchant state.
MAX_ITEMS_PER_REQUEST: Final[int] = 50

#: Checkout states in which a version has been frozen and is waiting on a human.
_AWAITING_DECISION: Final[frozenset[CheckoutState]] = frozenset(
    {CheckoutState.RESERVED, CheckoutState.APPROVAL_REQUIRED, CheckoutState.APPROVED}
)

#: Checkout states in which admission has run and money may be moving. Nothing may amend,
#: re-complete or cancel a session in one of these -- that is specification 16.1's
#: payment-state invariant, and ``AcpSessionStatus.IN_PROGRESS`` is how it is expressed to
#: the protocol layer.
_IN_FLIGHT: Final[frozenset[CheckoutState]] = frozenset(
    {
        CheckoutState.EXECUTION_PENDING,
        CheckoutState.AWAITING_PAYMENT,
        CheckoutState.PAYMENT_UNKNOWN,
        CheckoutState.INVALIDATED_AWAITING_PAYMENT_RESULT,
    }
)

#: Terminal, in ACP's vocabulary. ``PAID`` is completed; the rest are the session having
#: ended without a sale.
_COMPLETED: Final[frozenset[CheckoutState]] = frozenset({CheckoutState.PAID})
_CANCELLED: Final[frozenset[CheckoutState]] = frozenset(
    {
        CheckoutState.CANCELLED,
        CheckoutState.EXPIRED,
        CheckoutState.PAYMENT_FAILED,
        CheckoutState.INVALIDATED,
    }
)


def new_limiter() -> TokenBucketLimiter:
    """A fresh limiter for one app's ACP surface. Separate from the MCP surface's.

    Separate because both key their buckets the same way, so one instance serving both
    would let a client's traffic on one surface spend its budget on the other and hide
    which door was actually being pushed.
    """
    return TokenBucketLimiter()


# ------------------------------------------------------------------------ the projection


@dataclass(frozen=True, slots=True)
class Projected:
    """The platform rows one ACP session is read from, and the session they project to.

    The rows travel beside the projection because the operations need them -- the cart to
    amend, the checkout to complete -- and re-reading them would be a second snapshot that
    could disagree with the one the mapping was decided against.
    """

    session: AcpSession
    cart: Cart
    checkout: Checkout | None
    approval: Approval | None
    current_epoch: int


def load_session(
    db: Session,
    ctx: RequestContext,
    *,
    session_id: str,
    supplied_now: frozenset[str],
) -> Projected | None:
    """Project one ACP session from this platform's own rows, or ``None`` if there is none.

    Scoped by tenant *and* by ``buyer_ref``. Row-level security already scopes the tenant;
    the buyer predicate is what stops one ACP client reading another buyer's cart inside
    the same tenant, and it is written out here rather than inherited because this is the
    only place an ACP caller names a cart by id.

    ``supplied_now`` is what *this* request's body carried. It is unioned into the
    projection's ``supplied`` set for the reason in the module docstring: items and buyer
    are durable facts, fulfilment is not, and pretending otherwise would either freeze a
    checkout nobody supplied an address for or refuse one that had been.
    """
    try:
        cart_id = uuid.UUID(session_id)
    except ValueError:
        # An ACP session id is opaque to the caller and a cart id to this platform. One
        # that will not parse names nothing here, and saying "not found" rather than
        # "malformed" keeps the two answers indistinguishable.
        return None

    cart = db.execute(
        select(Cart).where(
            Cart.id == cart_id,
            Cart.tenant_id == ctx.tenant_id,
            Cart.buyer_ref == ctx.buyer_ref,
        )
    ).scalar_one_or_none()
    if cart is None:
        return None

    checkout = db.execute(
        select(Checkout).where(Checkout.tenant_id == ctx.tenant_id, Checkout.cart_id == cart.id)
    ).scalar_one_or_none()

    supplied = set(supplied_now)
    if cart.lines:
        supplied.add("items")
    # The credential names the buyer. There is no request field that could name a
    # different one and none that is read if it tries.
    supplied.add("buyer")

    if checkout is None:
        return Projected(
            session=AcpSession(
                session_id=session_id,
                status=AcpSessionStatus.NOT_READY_FOR_PAYMENT,
                supplied=frozenset(supplied),
            ),
            cart=cart,
            checkout=None,
            approval=None,
            current_epoch=0,
        )

    versions = read_versions(db, tenant_id=ctx.tenant_id, checkout_id=checkout.id)
    current = versions[-1] if versions else None
    # Scoped to the live version, and to an approval the clock has not passed.
    #
    # Without the version this asked for "the RECORDED approval on this checkout", and a
    # checkout can legitimately hold more than one: the partial unique index is keyed by
    # (tenant, checkout, **version**), and supersede leaves the retired version's approval
    # RECORDED -- `_INVALIDATE_RECORDED` is scoped `AND checkout_version = :v` and only
    # `reject_approval` calls it. So the platform's own supersede-and-reapprove flow, which
    # is the demonstration's hero moment, produced two RECORDED rows and this read raised
    # `MultipleResultsFound`. Unhandled, that is a 500 on every later ACP request for that
    # session: the buyer has consented, nothing is charged, and it healed only when
    # housekeeping expired the old approval.
    #
    # `expires_at` for the same reason `admission_service._recorded_approval` gives: the
    # sweep is periodic, so RECORDED alone can name one the clock has already passed.
    #
    # This was the only unscoped reader. `admission_service._recorded_approval` and
    # `proof_chain._approval_for` both handle it, and this is now the third agreement
    # rather than the one dissent.
    approval = (
        None
        if current is None
        else db.execute(
            select(Approval).where(
                Approval.tenant_id == ctx.tenant_id,
                Approval.checkout_id == checkout.id,
                Approval.checkout_version == current.version,
                Approval.status == "RECORDED",
                Approval.expires_at > func.now(),
            )
        ).scalar_one_or_none()
    )
    order_id = db.execute(
        select(Order.id).where(Order.tenant_id == ctx.tenant_id, Order.checkout_id == checkout.id)
    ).scalar_one_or_none()

    state = CheckoutState(checkout.status)
    return Projected(
        session=AcpSession(
            session_id=session_id,
            status=_status_for(state),
            supplied=frozenset(supplied),
            checkout_id=checkout.id,
            checkout_version=None if current is None else current.version,
            content_hash=None if current is None else current.content_hash,
            amount=None if approval is None else Money(approval.amount_minor, approval.currency),
            approved_version=None if approval is None else approval.checkout_version,
            approval_id=None if approval is None else approval.id,
            approval_expires_at=None if approval is None else approval.expires_at,
            authority_epoch=(
                0
                if approval is None or approval.authority_epoch is None
                else approval.authority_epoch
            ),
            order_id=order_id,
        ),
        cart=cart,
        checkout=checkout,
        approval=approval,
        current_epoch=_current_epoch(db, ctx, approval),
    )


def _status_for(state: CheckoutState) -> AcpSessionStatus:
    """This platform's checkout state, in ACP's five-word vocabulary.

    ``READY_FOR_PAYMENT`` means a version is frozen and a human is being asked, which is
    the only state ``COMPLETE_SESSION`` may be attempted from. Whether the human has
    actually said yes is not encoded here and must not be: that is
    :attr:`AcpSession.approved_version`, compared against the version being completed, and
    collapsing the two would make "the session is ready" and "the buyer agreed" the same
    sentence.
    """
    if state in _IN_FLIGHT:
        return AcpSessionStatus.IN_PROGRESS
    if state in _COMPLETED:
        return AcpSessionStatus.COMPLETED
    if state in _CANCELLED:
        return AcpSessionStatus.CANCELED
    if state in _AWAITING_DECISION:
        return AcpSessionStatus.READY_FOR_PAYMENT
    return AcpSessionStatus.NOT_READY_FOR_PAYMENT


def _current_epoch(db: Session, ctx: RequestContext, approval: Approval | None) -> int:
    """The revocation epoch this session's approval must still match.

    Zero when the approval names no delegated authority, which is every approval this
    platform's own trusted surface records today -- ``admission_service.approve_version``
    passes neither ``authority_id`` nor ``authority_epoch``. Both sides of the comparison
    are then zero, and the comparison passes.

    That is not the "two defaults agreeing with themselves" failure
    ``map_request`` warns about, and the difference is that this zero is *read* rather than
    defaulted: there is no authority, so there is no epoch, so there is nothing a
    revocation could have moved. When an approval does name an authority the epoch comes
    from that row, and a revocation between approval and completion is refused here as well
    as under lock at admission.
    """
    if approval is None or approval.authority_id is None:
        return 0
    epoch = db.execute(
        select(DelegatedAuthority.revocation_epoch).where(
            DelegatedAuthority.id == approval.authority_id,
            DelegatedAuthority.tenant_id == ctx.tenant_id,
        )
    ).scalar_one_or_none()
    # An approval naming an authority row that is not there is not something to shrug at:
    # the epoch cannot be observed, so the comparison must fail rather than default.
    return -1 if epoch is None else int(epoch)


# --------------------------------------------------------------------------- the answer


@dataclass(frozen=True, slots=True)
class AcpOutcome:
    """What one ACP operation produced: the document, and the decision if there was one.

    ``decision`` is present only for a completion that reached admission. It is carried
    separately from the document so the router can put it at the top level of the response
    unchanged -- a kernel decision is reported verbatim, and a nested copy would be a
    second place for it to be reshaped.
    """

    document: dict[str, Any]
    decision: dict[str, Any] | None = None
    #: The kernel's own answer, when admission ran. ``None`` for every other operation and
    #: for ADR 0003 D9's duplicate, where the single-winner index decided and no admission
    #: happened -- the router records the evidence differently in that case for exactly
    #: that reason.
    kernel_decision: AdmissionDecision | None = None


def honour(
    db: Session,
    ctx: RequestContext,
    registry: MerchantRegistry,
    *,
    operation: AcpOperation,
    admitted: AdmittedRequest,
    projected: Projected | None,
    idempotency_key: str,
) -> AcpOutcome:
    """Do what the mapped intent asked, using the services the trusted surface uses.

    Called after :func:`~commerce_protocols.acp.map_request` has decided the request is
    admissible. That function owns the protocol invariants; this one owns the platform
    ones, and where they overlap this defers -- a checkout is opened by
    ``checkout_service.open_checkout`` and a completion is admitted by
    ``admission_service.admit_approved_version``, both unchanged and both the same code
    path a browser reaches.
    """
    match operation:
        case AcpOperation.CREATE_SESSION:
            return _create(db, ctx, registry, admitted=admitted)
        case AcpOperation.UPDATE_SESSION:
            return _update(db, ctx, registry, admitted=admitted, projected=_required(projected))
        case AcpOperation.RETRIEVE_SESSION:
            return AcpOutcome(session_document(db, ctx, registry, _required(projected)))
        case AcpOperation.CANCEL_SESSION:
            return _propose_cancellation(db, ctx, registry, admitted, _required(projected))
        case AcpOperation.COMPLETE_SESSION:
            return _complete(
                db,
                ctx,
                registry,
                admitted=admitted,
                projected=_required(projected),
                idempotency_key=idempotency_key,
            )


def _required(projected: Projected | None) -> Projected:
    """The projection every operation but create needs. ``map_request`` already refused a
    missing one, so this narrows the type rather than adding a check."""
    if projected is None:  # pragma: no cover - map_request raises acp_session_not_found
        raise StateRejected("acp_session_not_found")
    return projected


def _create(
    db: Session, ctx: RequestContext, registry: MerchantRegistry, *, admitted: AdmittedRequest
) -> AcpOutcome:
    """Open a cart, apply the items, and freeze a version if the session is now ready."""
    created = cart_service.create_cart(db, ctx, registry)
    cart_id = uuid.UUID(str(created["cart_id"]))
    _apply_items(db, ctx, registry, cart_id=cart_id, body=admitted.body)

    supplied = supplied_by(admitted.body)
    if cart_service.load_cart(db, ctx, cart_id).lines:
        supplied |= {"items"}
    projected = load_session(db, ctx, session_id=str(cart_id), supplied_now=supplied)
    projected = _required(projected)
    if _is_ready(projected):
        checkout_service.open_checkout(db, ctx, registry, cart_id=cart_id)
        projected = _required(load_session(db, ctx, session_id=str(cart_id), supplied_now=supplied))
    return AcpOutcome(session_document(db, ctx, registry, projected))


def _update(
    db: Session,
    ctx: RequestContext,
    registry: MerchantRegistry,
    *,
    admitted: AdmittedRequest,
    projected: Projected,
) -> AcpOutcome:
    """Amend the cart, and freeze a version if this is the request that completes it.

    An update that names items once a version has been frozen is refused, and the refusal
    is a platform fact rather than a protocol one. The version is immutable and a human is
    looking at it: changing what is in the cart underneath would change what they are
    being asked to approve without changing the hash they were shown. The remedy is the
    supersede path, which only admission may start -- so this refusal names
    ``STALE_CHECKOUT``, telling the caller to re-read rather than to try again.
    """
    if projected.checkout is not None and _names_items(admitted.body):
        raise StateRejected(
            "acp_session_is_frozen_for_a_human_decision",
            session_id=projected.session.session_id,
            checkout_version=projected.session.checkout_version,
            status=projected.session.status.value,
        )
    if projected.checkout is None:
        _apply_items(db, ctx, registry, cart_id=projected.cart.id, body=admitted.body)

    supplied = supplied_by(admitted.body) | projected.session.supplied
    refreshed = _required(
        load_session(db, ctx, session_id=projected.session.session_id, supplied_now=supplied)
    )
    if refreshed.checkout is None and _is_ready(refreshed):
        checkout_service.open_checkout(db, ctx, registry, cart_id=projected.cart.id)
        refreshed = _required(
            load_session(db, ctx, session_id=projected.session.session_id, supplied_now=supplied)
        )
    return AcpOutcome(session_document(db, ctx, registry, refreshed))


def _complete(
    db: Session,
    ctx: RequestContext,
    registry: MerchantRegistry,
    *,
    admitted: AdmittedRequest,
    projected: Projected,
    idempotency_key: str,
) -> AcpOutcome:
    """The one ACP operation that reaches money, through the one admission.

    Everything about *whether* it may is already settled: ``map_request`` compared the
    echoed version and hash against the frozen ones, required an approval recorded on the
    trusted surface for that exact version, checked the revocation epoch and the approval's
    expiry against the database clock, and compared the presented total against the
    approved one. What is left is to admit it, and admission -- not this function -- decides
    whether money moves.

    The answer is HTTP 200 either way (ADR 0003 D15). A denial comes back as a structured
    decision with its recovery code and its deltas, because a denial is the platform
    working and an external buyer told 409 would retry a human's consent.
    """
    checkout_id = projected.session.checkout_id
    version = projected.session.checkout_version
    if checkout_id is None or version is None:  # pragma: no cover - map_request refused it
        raise StateRejected("acp_session_has_no_frozen_version")

    outcome = admission_service.admit_approved_version(
        db,
        ctx,
        registry,
        checkout_id=checkout_id,
        version=version,
        idempotency_key=idempotency_key,
        expected_content_hash=str(admitted.body.get("content_hash")),
    )
    # Flushed and expired before the session is projected again, and both halves matter.
    # Admission moved the checkout head to ``EXECUTION_PENDING``; without the flush that
    # write is still in the unit of work, and without the expiry the re-read is answered
    # from the identity map with the row as it was *before* the submit. Either way the
    # response would report ``READY_FOR_PAYMENT`` for a session whose payment is already in
    # flight -- which is the one thing this surface must not say, because an external buyer
    # reading it would offer to complete the session again.
    db.flush()
    db.expire_all()
    refreshed = _required(
        load_session(
            db,
            ctx,
            session_id=projected.session.session_id,
            supplied_now=projected.session.supplied,
        )
    )
    return AcpOutcome(
        session_document(db, ctx, registry, refreshed),
        decision=outcome.body,
        kernel_decision=outcome.decision,
    )


def _propose_cancellation(
    db: Session,
    ctx: RequestContext,
    registry: MerchantRegistry,
    admitted: AdmittedRequest,
    projected: Projected,
) -> AcpOutcome:
    """A request for a human, and nothing else. ``IntentKind`` has no ``CANCEL``.

    An external buyer asking to cancel is exactly that: an ask. The platform's own cancel
    is a buyer capability on the trusted surface, gated by the state table -- once a
    checkout is ``AWAITING_PAYMENT`` money may already be moving, and cancelling something
    somebody has been charged for is the one outcome worse than not cancelling.

    What this leaves behind is the interaction's evidence stream, which is a real
    hash-chained record a person can open. The response says so and names it, rather than
    reporting a cancellation that did not happen.
    """
    document = session_document(db, ctx, registry, projected)
    document["cancellation_request"] = {
        "accepted": True,
        "cancelled": False,
        "recorded_as": str(admitted.interaction.interaction_id),
        "inspect_at": f"/v1/inspector/protocols/{admitted.interaction.interaction_id}",
        "next_step": (
            "A person decides. This platform's cancellation is a buyer action on the "
            "trusted surface and is refused outright once payment may be in flight."
        ),
    }
    return AcpOutcome(document)


# ------------------------------------------------------------------------------ helpers


def _names_items(body: Mapping[str, Any]) -> bool:
    return bool(body.get("items"))


def supplied_by(body: Mapping[str, Any]) -> frozenset[str]:
    """Which readiness requirements this body satisfies. A present but empty key does not.

    The names come from ``REQUIRED_FOR_READINESS`` rather than being written out again, so
    this function and the mapping that decides the intent are reading one list. An ACP
    client that sends ``"items": []`` has named the field without supplying anything, and
    counting that as readiness would freeze a checkout with nothing in it for a human to
    approve.
    """
    return frozenset(name for name in REQUIRED_FOR_READINESS if body.get(name))


def _is_ready(projected: Projected) -> bool:
    return projected.session.supplied >= REQUIRED_FOR_READINESS


def _apply_items(
    db: Session,
    ctx: RequestContext,
    registry: MerchantRegistry,
    *,
    cart_id: uuid.UUID,
    body: Mapping[str, Any],
) -> None:
    """Set each named line to an absolute quantity, refusing anything else.

    Absolute rather than incremental, matching ``cart_service.set_line`` and for the same
    reason: an increment retried under the same idempotency key adds the item twice, and an
    absolute quantity retried is the same quantity.

    Quantities are validated as integers here as well as in ``set_line``. JSON has one
    number type, so ``2.0`` arrives as a float, and a surface that silently accepted it
    would be a surface where "two" and "about two" were the same request.
    """
    items = body.get("items")
    if items is None:
        return
    if not isinstance(items, list):
        raise SchemaRejected("acp_items_is_not_a_list")
    if len(items) > MAX_ITEMS_PER_REQUEST:
        raise SchemaRejected("acp_too_many_items", count=len(items), maximum=MAX_ITEMS_PER_REQUEST)
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise SchemaRejected("acp_item_is_not_an_object", index=index)
        sku = item.get("sku")
        quantity = item.get("quantity")
        if not isinstance(sku, str) or not sku.strip():
            raise SchemaRejected("acp_item_names_no_sku", index=index)
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise SchemaRejected(
                "acp_item_quantity_is_not_an_integer",
                index=index,
                presented_type=type(quantity).__name__,
            )
        cart_service.set_line(
            db, ctx, registry, cart_id=cart_id, sku=sku.strip(), quantity=quantity
        )


def session_document(
    db: Session, ctx: RequestContext, registry: MerchantRegistry, projected: Projected
) -> dict[str, Any]:
    """One ACP session as this surface reports it.

    ``supplied`` and ``requires`` are echoed because of the fulfilment note in the module
    docstring: a client that cannot see what the platform counted cannot tell why its
    session is not ready, and "not ready, and I will not say what is missing" is a contract
    nobody can integrate against.

    ``payment`` reports the live attempt rather than an outcome. An ACP session is
    ``COMPLETED`` only when capture evidence wrote an order, never when a payment was
    merely admitted, because an admitted payment is money in flight and reporting it as a
    completed sale is the one lie this platform must not tell.
    """
    session = projected.session
    cart = cart_service.cart_body(projected.cart, registry=registry)
    priced = cart.get("quote")
    quote: Mapping[str, Any] = priced if isinstance(priced, Mapping) else {}
    checkout: Mapping[str, Any] = (
        {}
        if projected.checkout is None
        else checkout_service.read_checkout(db, ctx, projected.checkout.id)
    )
    attempt = checkout.get("attempt")
    return {
        "id": session.session_id,
        "status": session.status.value,
        "supplied": sorted(session.supplied),
        "requires": sorted(REQUIRED_FOR_READINESS - session.supplied),
        "currency": quote.get("currency"),
        "line_items": [
            {"sku": line["sku"], "quantity": line["quantity"]} for line in cart.get("lines") or []
        ],
        "totals": {
            "items_subtotal_minor": quote.get("items_subtotal_minor"),
            "delivery_fee_minor": quote.get("delivery_fee_minor"),
            # Without this an outside buyer is handed components that do not reach the
            # total, and the one figure that explains the gap is the one we withheld.
            "discount_minor": quote.get("discount_minor"),
            "total_minor": quote.get("total_minor"),
        },
        "checkout": None
        if session.checkout_id is None
        else {
            "checkout_id": str(session.checkout_id),
            "version": session.checkout_version,
            "content_hash": session.content_hash,
            "state": checkout.get("state"),
        },
        "approval": None
        if session.approval_id is None
        else {
            "approved_version": session.approved_version,
            "amount_minor": None if session.amount is None else session.amount.minor,
            "currency": None if session.amount is None else session.amount.currency,
            "expires_at": None
            if session.approval_expires_at is None
            else session.approval_expires_at.isoformat(),
        },
        "payment": None
        if not isinstance(attempt, Mapping)
        else {
            "state": attempt.get("state"),
            # Reported, not inferred. ``TERMINAL_PAYMENT_STATES`` is the kernel's own list,
            # and it is what decides whether this session may be amended again -- so the
            # answer an external buyer reads is the answer the state table will give it.
            "terminal": _is_terminal(attempt.get("state")),
        },
        "order_id": None if session.order_id is None else str(session.order_id),
        "messages": _messages(session),
    }


def _is_terminal(state: object) -> bool | None:
    """Whether a payment state is one nothing moves out of, by the kernel's own list."""
    if not isinstance(state, str) or not state:
        return None
    return PaymentState(state) in TERMINAL_PAYMENT_STATES


def _messages(session: AcpSession) -> list[dict[str, str]]:
    """What the external buyer must be told, in this surface's own words.

    ACP's own vocabulary has no way to say "a human on a surface you cannot reach has to
    agree to this first", so the surface says it. A client that renders these has told its
    user the truth; one that ignores them still cannot complete the session, because the
    refusal is enforced in :func:`~commerce_protocols.acp.map_request` and not here.
    """
    if session.status is AcpSessionStatus.READY_FOR_PAYMENT and session.approval_id is None:
        return [
            {
                "type": "info",
                "code": "awaiting_buyer_approval",
                "content": (
                    "This version is frozen and awaiting the buyer's decision on the "
                    "merchant's own trusted surface. Completing it before that decision "
                    "is recorded will be refused."
                ),
            }
        ]
    if session.status is AcpSessionStatus.IN_PROGRESS:
        return [
            {
                "type": "info",
                "code": "payment_in_flight",
                "content": (
                    "A payment attempt is in flight. The session cannot be amended, "
                    "re-completed or cancelled until it reaches a terminal state."
                ),
            }
        ]
    return []
