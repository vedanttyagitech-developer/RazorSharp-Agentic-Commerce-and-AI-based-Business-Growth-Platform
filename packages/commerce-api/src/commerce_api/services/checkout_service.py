"""Checkout construction and the checkout read model. Step 3 of the demonstration.

Construction is one kernel transaction and four things that must be true together:
version 1 exists with the exact bytes the buyer was quoted, stock is held, a
Policy-at-Sale Receipt freezes the merchant's rules as they were at that instant, and the
basket is closed so a second checkout cannot be built from it. Either all four commit or
none does. That is why this module calls :func:`transaction_kernel.create_checkout` and
:func:`transaction_kernel.require_approval` inside one transaction owned by
:func:`commerce_api.deps.kernel_session` and never commits between them: a receipt without
its version, or a version without its hold, is worse than no checkout at all.

The receipt covers **every** :class:`~transaction_kernel.receipts.PolicyKind`. That is not
this module's choice -- :data:`transaction_kernel.receipts.REQUIRED_POLICY_KINDS` refuses
a draft with a gap -- and :func:`merchant_sim.receipt_inputs_for` supplies all six,
recording "no substitution programme" as ``{"allowed": false}`` rather than omitting the
kind. An omitted kind would later be filled from the merchant's *current* policy, which is
exactly the retroactive change a Policy-at-Sale Receipt exists to make impossible.

The read model is the other half. Specification 8.2 lists sixteen UI states, and every one
of them is rendered from :func:`read_checkout`: the head, every version with its hash and
its invalidation stamp, the approval card the buyer is looking at, the payment attempt if
admission has run, and the deltas between the version that was retired and the one that
replaced it. Reads run as the app role, which physically cannot write a financial table.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from commerce_domain import order_reference
from merchant_sim import content_from_quote, receipt_inputs_for
from platform_db import Approval, Checkout, Order
from sqlalchemy import select
from sqlalchemy.orm import Session
from transaction_kernel import (
    CheckoutState,
    Delta,
    create_checkout,
    read_head,
    read_versions,
    require_approval,
    supersede_checkout,
)
from transaction_kernel.checkouts import ApprovalCard, CheckoutVersionView
from transaction_kernel.reservations import Allocation, ReservationView, check_validity
from transaction_kernel.states import can_transition

from ..deps import RequestContext
from ..errors import ProblemError
from ..merchants import MerchantRegistry
from ..schemas import (
    ApprovalCardOut,
    ApprovalRecordOut,
    AttemptOut,
    CheckoutOut,
    DeltaOut,
    MoneyOut,
    QuoteOut,
    ReservationOut,
    VersionSummaryOut,
    rfc3339,
    uuid_str,
)
from . import basket_service, payment_service

__all__ = [
    "RESERVATION_TTL_SECONDS",
    "allocations_for",
    "approval_card_body",
    "deltas_between",
    "open_checkout",
    "read_checkout",
    "reservation_out",
]

#: ADR 0003 D13. Five minutes, where this used to be fifteen. Every second of it is stock
#: withheld from a buyer who is ready to pay on behalf of one who has not decided yet, and
#: a quarter of an hour of that is a shelf emptied by people who wandered off. Five is
#: still far longer than reading one approval card takes and longer than any demo runs, so
#: the buyer who is genuinely deciding loses nothing and the next one gets their milk back.
RESERVATION_TTL_SECONDS = 300


# ------------------------------------------------------------------------- helpers


def allocations_for(
    registry: MerchantRegistry, merchant_id: uuid.UUID, skus: Sequence[str]
) -> tuple[Allocation, ...]:
    """The scarce items this checkout must hold, with the stock figure re-read now.

    The *quantity* is deliberately not passed: the kernel derives it from the immutable
    version being reserved, so the number checked against stock and the number the buyer
    approved cannot drift apart. All this supplies is how many units exist.
    """
    store = registry.store(merchant_id)
    return tuple(
        Allocation(scarcity_key=sku, available_units=store.check_inventory(sku).available_units)
        for sku in sorted(set(skus))
    )


def reservation_out(
    session: Session, *, checkout_id: uuid.UUID, version: int
) -> ReservationOut | None:
    """The hold on one version as the database clock sees it, for display only.

    ``lock=False``: this is a countdown for a screen. Admission takes the same row
    ``FOR UPDATE`` and is the only reader whose answer may not change underneath it.
    """
    outcome = check_validity(session, checkout_id=checkout_id, checkout_version=version, lock=False)
    view: ReservationView | None = outcome.reservation
    if view is None:
        return None
    return ReservationOut(
        reservation_id=str(view.reservation_id),
        state=view.status.value,
        expires_at=rfc3339(view.expires_at),
    )


def deltas_between(previous: CheckoutVersionView, current: CheckoutVersionView) -> list[Delta]:
    """What changed between a retired version and the one that replaced it.

    The same two comparisons admission makes -- the total, and the set of line items --
    so the card a buyer re-reads after a supersede says the same thing the denial said.
    Derived from the two stored content documents rather than from the audit log, because
    the documents are the evidence and the log is a description of it.
    """
    deltas: list[Delta] = []
    if previous.total != current.total:
        deltas.append(
            Delta(
                field_path="total",
                approved=previous.total.minor,
                current=current.total.minor,
                reason="total_changed",
            )
        )
    before = dict(previous.content.get("line_items", {}))
    after = dict(current.content.get("line_items", {}))
    if before != after:
        deltas.append(
            Delta(
                field_path="line_items",
                approved=before,
                current=after,
                reason="availability_changed",
            )
        )
    return deltas


def approval_card_body(
    session: Session,
    card: ApprovalCard,
    *,
    previous_version: int | None = None,
    deltas: Sequence[Delta] = (),
) -> dict[str, Any]:
    """Render an approval card for the wire.

    ``expires_at`` is the reservation's expiry, not an approval's: the card is the thing
    the buyer is still deciding about, and the deadline that matters is the moment the
    held stock goes back on the shelf. The approval's own TTL starts when the buyer
    decides, and appears on :class:`~commerce_api.schemas.ApprovalRecordOut`.

    ``quote`` is read out of ``card.content`` -- the very document ``content_hash``
    covers -- rather than taken from whatever the caller happened to be holding. This
    function used to accept a live :class:`~merchant_sim.Quote` instead, and only the
    open path ever passed one: the supersede path had none to give, and
    :func:`read_checkout` sent ``null``. So the panel headed "What is in it" was empty on
    every screen a buyer actually reaches by loading a URL, and an approval card asking
    for a four-figure sum named no product at all. Deriving it from the approved bytes
    fixes all three call sites at once and makes the breakdown structurally incapable of
    disagreeing with the hash printed beside it.
    """
    body = ApprovalCardOut(
        checkout_id=str(card.checkout.checkout_id),
        version=card.checkout.version,
        content_hash=card.checkout.content_hash,
        policy_receipt_id=str(card.receipt_id),
        policy_receipt_hash=card.receipt_hash,
        amount_minor=card.total.minor,
        currency=card.total.currency,
        total=MoneyOut.of(card.total),
        expires_at=rfc3339(card.reservation_expires_at),
        reservation=reservation_out(
            session, checkout_id=card.checkout.checkout_id, version=card.checkout.version
        ),
        quote=QuoteOut.of_content(card.content, content_hash=card.checkout.content_hash),
        previous_version=previous_version,
        deltas=[DeltaOut.of(delta) for delta in deltas],
    )
    return body.model_dump(mode="json")


# -------------------------------------------------------------------- construction


def open_checkout(
    session: Session,
    ctx: RequestContext,
    registry: MerchantRegistry,
    *,
    basket_id: uuid.UUID,
) -> dict[str, Any]:
    """``POST /v1/baskets/{id}/checkout``: version 1, its receipt, its hold, one card.

    In order, inside the caller's kernel transaction:

    1. lock the basket and re-price it against live merchant state, so the version is
       built from what the merchant says now rather than from the stored quote;
    2. build canonical content through the kernel's own builder (ADR 0003 D6) and write
       version 1 with :func:`transaction_kernel.create_checkout`, which re-stamps the
       checkout id it mints into the document before hashing it;
    3. close the basket -- the kernel deliberately leaves that to this service, because
       the basket is the API's record, and it must close in the same transaction or a
       second checkout could be built from it;
    4. take the hold and issue the Policy-at-Sale Receipt with
       :func:`transaction_kernel.require_approval`, which moves the version
       ``QUOTED -> RESERVED -> APPROVAL_REQUIRED`` and makes it immutable.

    The reservation is taken with the stock figures re-read in this transaction, so two
    buyers racing for the last unit cannot both be handed an approval card for it.
    """
    ctx.require("checkout.create")
    basket = basket_service.lock_basket(session, ctx, basket_id)
    if basket.status != "OPEN":
        # The same rule a line write follows, for the same reason and by the same code. A
        # buyer who walked away from an approval card and came back to check out again is
        # asking for exactly what ``_reopen_for_edit`` grants: version N ends, the hold is
        # released, the cart reopens, and what they get is version N+1 below. Refusing here
        # while permitting it there would mean the shop's answer to "check out again"
        # depended on whether the buyer happened to change a line first.
        refusal = basket_service.reopen_for_edit(session, ctx, basket)
        if refusal is not None:
            raise ProblemError(
                409,
                "Cart is being paid for",
                "This cart's checkout has already gone to payment and cannot be reopened. "
                "Wait for the payment to finish, or start a new cart.",
                basket_id=str(basket_id),
                reason=refusal.reason,
                checkout_id=refusal.checkout_id,
                checkout_state=refusal.checkout_state,
            )
    quote = basket_service.basket_quote_or_refuse(basket, registry)

    # A cart the buyer took back from its own checkout already has one (see
    # ``basket_service._reopen_for_edit``), and one basket may hold only one checkout. So
    # the second time through, this is not a new checkout but the next version of the same
    # one: new content, new hash, its own approval, with the version the buyer walked away
    # from invalidated behind it. Both paths re-stamp the id and version into the document
    # before hashing, so the price is computed without knowing which it will be.
    existing = session.execute(
        select(Checkout).where(Checkout.tenant_id == ctx.tenant_id, Checkout.basket_id == basket.id)
    ).scalar_one_or_none()
    content = content_from_quote(
        quote,
        checkout_id=basket.id if existing is None else existing.id,
        version=1 if existing is None else existing.current_version + 1,
        policy_version=registry.policy_version(),
    )
    if existing is None:
        created = create_checkout(
            session,
            tenant_id=ctx.tenant_id,
            merchant_id=basket.merchant_id,
            basket_id=basket.id,
            buyer_ref=ctx.buyer_ref,
            content=content,
            correlation_id=ctx.correlation_id,
            principal=ctx.principal,
        )
    else:
        created = supersede_checkout(
            session,
            tenant_id=ctx.tenant_id,
            checkout_id=existing.id,
            content=content,
            correlation_id=ctx.correlation_id,
            principal=ctx.principal,
        )

    basket.status = "CHECKED_OUT"
    session.flush()

    card = require_approval(
        session,
        tenant_id=ctx.tenant_id,
        checkout=created.ref,
        receipt=receipt_inputs_for(registry.store(basket.merchant_id)),
        correlation_id=ctx.correlation_id,
        reservation_ttl_seconds=RESERVATION_TTL_SECONDS,
        allocations=allocations_for(
            registry, basket.merchant_id, [line.sku for line in quote.lines]
        ),
        principal=ctx.principal,
    )
    return approval_card_body(session, card)


# --------------------------------------------------------------------- read model


def _approval_records(
    session: Session, ctx: RequestContext, checkout_id: uuid.UUID
) -> dict[int, ApprovalRecordOut]:
    """The newest approval per version, keyed by version.

    Newest wins because reject-then-reapprove leaves an ``INVALIDATED`` row beside the
    live one, and the version's story is told by its latest decision.
    """
    rows = session.execute(
        select(Approval)
        .where(Approval.tenant_id == ctx.tenant_id, Approval.checkout_id == checkout_id)
        .order_by(Approval.checkout_version, Approval.issued_at, Approval.id)
    ).scalars()
    records: dict[int, ApprovalRecordOut] = {}
    for row in rows:
        records[row.checkout_version] = ApprovalRecordOut(
            approval_id=str(row.id),
            version=row.checkout_version,
            content_hash=row.content_hash,
            policy_receipt_hash=row.policy_receipt_hash or "",
            amount_minor=row.amount_minor,
            currency=row.currency,
            approved_at=rfc3339(row.issued_at),
            expires_at=rfc3339(row.expires_at),
            authority_epoch=row.authority_epoch or 0,
        )
    return records


def _attempt_out(
    session: Session, ctx: RequestContext, checkout_id: uuid.UUID
) -> AttemptOut | None:
    """The checkout's current payment attempt, newest first.

    Delegated to :mod:`commerce_api.services.payment_service` rather than assembled here.
    This function used to build its own ``AttemptOut`` with ``grant_id``,
    ``capture_evidence`` and ``reconciliation_attempts`` hard-coded empty, on the reasoning
    that capture evidence belongs to the order. Reading the order's evidence is not
    inventing it -- the kernel writes that column only from verified WEBHOOK or
    PROVIDER_FETCH evidence (ADR 0003 D8), and ``attempt_summary`` filters any other
    source out. The cost of the near-copy was that one attempt had two descriptions:
    ``GET /v1/checkouts/{id}`` reported no grant and no capture for the very attempt that
    ``GET /v1/orders/{id}`` showed captured under a named grant.
    """
    row = payment_service.latest_attempt(session, tenant_id=ctx.tenant_id, checkout_id=checkout_id)
    if row is None:
        return None
    return payment_service.attempt_summary(session, tenant_id=ctx.tenant_id, attempt=row)


def read_checkout(session: Session, ctx: RequestContext, checkout_id: uuid.UUID) -> dict[str, Any]:
    """``GET /v1/checkouts/{id}``: everything specification 8.2 renders a state from.

    Every version is returned, invalidated ones included. A superseded version is not
    noise: it is the evidence that the old approval was refused, and hiding it would
    leave the buyer surface unable to show what changed.
    """
    head = read_head(session, tenant_id=ctx.tenant_id, checkout_id=checkout_id)
    if head is None or head.buyer_ref != ctx.buyer_ref:
        raise ProblemError(
            404,
            "Checkout not found",
            "No checkout with that identifier belongs to this session.",
            checkout_id=str(checkout_id),
        )
    versions = read_versions(session, tenant_id=ctx.tenant_id, checkout_id=checkout_id)
    approvals = _approval_records(session, ctx, checkout_id)

    summaries = [
        VersionSummaryOut(
            version=view.version,
            state=view.status,
            content_hash=view.content_hash,
            policy_receipt_hash=view.policy_receipt_hash,
            amount_minor=view.total.minor,
            currency=view.total.currency,
            created_at=rfc3339(view.created_at),
            approval=approvals.get(view.version),
        )
        for view in versions
    ]

    current = versions[-1] if versions else None
    deltas: list[Delta] = []
    if current is not None and len(versions) > 1:
        previous = versions[-2]
        if previous.invalidated_at is not None:
            deltas = deltas_between(previous, current)

    card_body: ApprovalCardOut | None = None
    if (
        current is not None
        and current.status is CheckoutState.APPROVAL_REQUIRED
        and current.policy_receipt_id is not None
        and current.policy_receipt_hash is not None
    ):
        reservation = reservation_out(session, checkout_id=checkout_id, version=current.version)
        card_body = ApprovalCardOut(
            checkout_id=str(checkout_id),
            version=current.version,
            content_hash=current.content_hash,
            policy_receipt_id=str(current.policy_receipt_id),
            policy_receipt_hash=current.policy_receipt_hash,
            amount_minor=current.total.minor,
            currency=current.total.currency,
            total=MoneyOut.of(current.total),
            expires_at=reservation.expires_at if reservation else rfc3339(current.created_at),
            reservation=reservation,
            # The breakdown comes out of this version's own stored content, never out of
            # a fresh quote. A read must not re-price: the merchant may have moved since
            # the buyer was shown this card, and the amount on a consent screen has to be
            # the amount consent was asked for. Admission is where a moved price is
            # caught, and it answers with a new version rather than a quietly edited one.
            quote=QuoteOut.of_content(current.content, content_hash=current.content_hash),
            previous_version=versions[-2].version if len(versions) > 1 else None,
            deltas=[DeltaOut.of(delta) for delta in deltas],
        )

    order_id = session.execute(
        select(Order.id).where(Order.tenant_id == ctx.tenant_id, Order.checkout_id == checkout_id)
    ).scalar_one_or_none()

    body = CheckoutOut(
        checkout_id=str(checkout_id),
        basket_id=str(head.basket_id),
        state=head.status,
        current_version=head.current_version,
        versions=summaries,
        approval_card=card_body,
        attempt=_attempt_out(session, ctx, checkout_id),
        order_id=uuid_str(order_id),
        order_reference=None if order_id is None else order_reference(order_id),
        deltas=[DeltaOut.of(delta) for delta in deltas],
        # The state table decides, not this module: every state with a CANCELLED edge is
        # cancellable, and AWAITING_PAYMENT is not, because money may already be moving.
        cancellable=can_transition(head.status, CheckoutState.CANCELLED),
        updated_at=rfc3339(head.updated_at),
    )
    return body.model_dump(mode="json")
