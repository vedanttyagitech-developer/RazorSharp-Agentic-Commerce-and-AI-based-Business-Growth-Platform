"""Approve, reject, submit, cancel. Steps 4 and 6 to 8 of the demonstration.

This is the part most conversational-commerce demonstrations skip, and it is the reason
this project is a payments project rather than a shopping chatbot. Four operations:

``approve``
    The buyer's decision, bound to bytes rather than to an identifier. The request body
    **echoes** the content hash, the amount and the currency, and
    :func:`transaction_kernel.record_approval` compares all three against the row it has
    locked. A client that echoes a hash the version does not carry is refused, which is
    what makes "I approved this" mean "I approved *these bytes*".

``reject``
    The buyer declines. The version is retired and the held stock goes back on the shelf
    in the same transaction, because a decline that leaves inventory reserved is a
    decline that costs the merchant a sale.

``submit``
    Steps 6 and 7, the headline. One kernel transaction re-reads merchant truth under
    locks and either authorises exactly one payment attempt with exactly one Execution
    Grant, or refuses and hands back the exact delta with version N+1 already waiting.

``cancel``
    Within policy, and refused with a structured reason once money may be moving.

Two rules govern every response here, and they are not negotiable:

**A kernel denial is HTTP 200** (ADR 0003 D15). A refused submit is the platform working
exactly as designed. Returning 409 would tell every HTTP client in the chain to treat a
buyer's consent as a transient failure and retry it, which is how somebody gets asked to
approve one purchase four times.

**The grant and its command commit together.** On an allowed admission the create-order
command is enqueued and :func:`transaction_kernel.link_command` ties it to the grant
inside the *same* transaction the grant was issued in. A grant with no command is
authority nobody will exercise; a command with no grant is a provider call nobody
authorised. Neither can exist here, because both are written or neither is.

Two places where this service supplies what the kernel deliberately leaves to its caller
---------------------------------------------------------------------------------------
:func:`transaction_kernel.admit` does not consume the approval it was handed, and it does
not give version N+1 its Policy-at-Sale Receipt. Both omissions are by design -- the
kernel's ``require_approval`` documents the supersede path as an entry point *for its
caller* -- and both are completed here, in the same transaction as the admission, so a
crash cannot leave a spent approval unspent or an unapprovable N+1. See
:func:`submit_checkout` for the ordering and the reasoning.
"""

from __future__ import annotations

import uuid
from typing import Any

from commerce_domain import Money
from durable_work.commands import CreateOrderCommand, enqueue_command
from merchant_sim import receipt_inputs_for
from platform_db import Approval, PaymentAttempt, set_tenant
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from transaction_kernel import (
    ActorType,
    AdmissionRequest,
    CheckoutRef,
    CheckoutState,
    KernelDecision,
    Operation,
    PaymentState,
    RecoveryCode,
    admit,
    cancel,
    consume_recorded,
    link_command,
    read_versions,
    record_approval,
    reject_approval,
    require_approval,
    transition,
)
from transaction_kernel.checkouts import CheckoutVersionView
from transaction_kernel.reservations import ReleaseCause, release

from ..deps import RequestContext, assert_owner
from ..errors import ProblemError, decision_payload
from ..merchants import MerchantRegistry
from ..schemas import ApprovalRecordOut, CheckoutRefOut, rfc3339
from . import checkout_service

__all__ = [
    "ConcurrentAdmission",
    "approve_version",
    "cancel_checkout",
    "duplicate_after_race",
    "reject_version",
    "submit_checkout",
]


class ConcurrentAdmission(Exception):  # noqa: N818 - a control-flow signal, not a failure
    """Two submits reached the single-winner index together; this one lost.

    A control-flow signal rather than an error, for the same reason
    :class:`commerce_api.idempotency.IdempotentReplay` is one: the correct answer is a
    200 naming the winner (ADR 0003 D9), and the only reason it cannot be built here is
    that ``admit`` has already rolled the transaction back. The router catches this
    outside the idempotency block and calls :func:`duplicate_after_race`.
    """

    def __init__(self, checkout_id: uuid.UUID) -> None:
        super().__init__(f"another submit won admission for checkout {checkout_id}")
        self.checkout_id = checkout_id


#: Payment states in which an attempt is still the checkout's one live attempt. Copied
#: from the partial unique index ``uq_payment_attempts_one_non_terminal``: the index is
#: what actually enforces single-winner, and this list only decides what a *second*
#: submit is told about the winner.
_LIVE_ATTEMPT_STATES: tuple[str, ...] = (
    PaymentState.CREATED.value,
    PaymentState.SUBMITTED.value,
    PaymentState.AUTHORIZED.value,
    PaymentState.UNKNOWN.value,
    PaymentState.RECONCILING.value,
)


# ------------------------------------------------------------------------- helpers


def _version(
    session: Session, ctx: RequestContext, checkout_id: uuid.UUID, version: int
) -> CheckoutVersionView:
    """One stored version, or 404. Read, never locked: the kernel takes the lock."""
    for view in read_versions(session, tenant_id=ctx.tenant_id, checkout_id=checkout_id):
        if view.version == version:
            return view
    raise ProblemError(
        404,
        "Checkout version not found",
        "This checkout has no such version.",
        checkout_id=str(checkout_id),
        version=version,
    )


def _recorded_approval(
    session: Session, ctx: RequestContext, checkout_id: uuid.UUID, version: int
) -> Approval | None:
    """The one ``RECORDED`` approval for this version, or ``None``.

    ``None`` has two very different causes and the caller must tell them apart: nobody
    has approved this version, or somebody has and a concurrent submit already spent it.
    See :func:`submit_checkout`.
    """
    return session.execute(
        select(Approval).where(
            Approval.tenant_id == ctx.tenant_id,
            Approval.checkout_id == checkout_id,
            Approval.checkout_version == version,
            Approval.status == "RECORDED",
        )
    ).scalar_one_or_none()


def _live_attempt(
    session: Session, ctx: RequestContext, checkout_id: uuid.UUID
) -> PaymentAttempt | None:
    """The checkout's one non-terminal attempt, if admission has already run.

    A plain ``SELECT`` with no lock, taken before the kernel locks anything: ADR 0003 D5
    says an attempt is *located* without a lock and locked only after
    ``checkout_versions``, and reversing that is how cancel and evidence application
    deadlock against each other.
    """
    return session.execute(
        select(PaymentAttempt)
        .where(
            PaymentAttempt.tenant_id == ctx.tenant_id,
            PaymentAttempt.checkout_id == checkout_id,
            PaymentAttempt.status.in_(_LIVE_ATTEMPT_STATES),
        )
        .order_by(PaymentAttempt.created_at.desc(), PaymentAttempt.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _decision_body(decision: KernelDecision, extra: dict[str, Any]) -> dict[str, Any]:
    """The decision's own fields at the top level, plus this route's siblings (D15).

    Built as a plain dictionary rather than through
    :func:`commerce_api.errors.decision_response` because the body has to be handed to
    ``slot.store`` before it is handed to the client: a replay must return the same
    bytes, and a ``JSONResponse`` is not something the idempotency record can hold.
    """
    body = decision_payload(decision).model_dump(mode="json")
    collisions = sorted(set(extra) & set(body))
    if collisions:  # pragma: no cover - the extras below are fixed literals
        raise ValueError(f"extra keys {collisions} would overwrite the kernel's own fields")
    body.update(extra)
    return body


# ------------------------------------------------------------------- step 4 and 8


def approve_version(
    session: Session,
    ctx: RequestContext,
    *,
    checkout_id: uuid.UUID,
    version: int,
    content_hash: str,
    amount_minor: int,
    currency: str,
) -> dict[str, Any]:
    """Record the buyer's approval of exactly these bytes, this amount, this currency.

    The echo is the whole mechanism. The client sends back the hash and the amount it
    displayed; :func:`transaction_kernel.record_approval` compares them against the
    locked version row and refuses a mismatch with ``STALE_CHECKOUT``. So an approval can
    never be recorded for a total the version does not carry, and a client that renders
    one number while echoing another fails here rather than at the payment provider.

    Only a trusted-surface buyer session holds ``checkout.approve``; an agent session
    deliberately does not. Consent is not delegable to the thing that proposed the
    purchase (specification 5.3, Registry A against Registry B).
    """
    ctx.require("checkout.approve")
    assert_owner(session, ctx, checkout_id)
    checkout = CheckoutRef(checkout_id=checkout_id, version=version, content_hash=content_hash)
    record = record_approval(
        session,
        tenant_id=ctx.tenant_id,
        checkout=checkout,
        amount=Money(amount_minor, currency),
        principal=ctx.principal,
        correlation_id=ctx.correlation_id,
    )
    return {
        "checkout": CheckoutRefOut.of(checkout).model_dump(mode="json"),
        "approval": ApprovalRecordOut(
            approval_id=str(record.approval_id),
            version=record.checkout.version,
            content_hash=record.checkout.content_hash,
            policy_receipt_hash=record.policy_receipt_hash or "",
            amount_minor=record.amount.minor,
            currency=record.amount.currency,
            approved_at=rfc3339(record.issued_at),
            expires_at=rfc3339(record.expires_at),
            authority_epoch=record.authority_epoch or 0,
        ).model_dump(mode="json"),
        "state": CheckoutState.APPROVED.value,
    }


def reject_version(
    session: Session,
    ctx: RequestContext,
    *,
    checkout_id: uuid.UUID,
    version: int,
    content_hash: str,
    reason: str,
) -> dict[str, Any]:
    """The buyer declines. The version is retired and the hold is released.

    The hash is echoed here too, for the same reason it is echoed on approval: a decline
    must name what was declined, or a client showing a stale card would retire a version
    the buyer never saw.
    """
    ctx.require("checkout.reject")
    assert_owner(session, ctx, checkout_id)
    rejection = reject_approval(
        session,
        tenant_id=ctx.tenant_id,
        checkout=CheckoutRef(checkout_id=checkout_id, version=version, content_hash=content_hash),
        principal=ctx.principal,
        reason=reason,
        correlation_id=ctx.correlation_id,
    )
    return {
        "checkout": CheckoutRefOut.of(rejection.checkout).model_dump(mode="json"),
        "from_state": rejection.from_state.value,
        "invalidated_approvals": [str(item) for item in rejection.approval_ids],
        "reservation_release": rejection.reservation_release.value,
        "state": CheckoutState.CANCELLED.value,
    }


# --------------------------------------------------------------- steps 6, 7 and 8


def _supersede(
    session: Session,
    ctx: RequestContext,
    registry: MerchantRegistry,
    *,
    checkout_id: uuid.UUID,
    retired_version: int,
    next_version: int,
    merchant_id: uuid.UUID,
) -> dict[str, Any]:
    """Give version N+1 its Policy-at-Sale Receipt and a fresh hold. ADR 0003 D4c.

    ``admit`` has already invalidated N and written N+1 in ``APPROVAL_REQUIRED`` with no
    receipt bound. That is exactly the entry state
    :func:`transaction_kernel.require_approval` documents as the supersede path: it
    applies no transition, issues the receipt and takes the hold. Doing it here, in the
    admission's own transaction, is what stops a crash from leaving an N+1 that can never
    be approved.

    N's hold is released **first**, and it has to be. The reservation module counts every
    live hold on other versions of the same checkout against the same scarce item, so
    N+1's reserve would be refused ``CONCURRENT_OPERATION`` by stock that N is still
    withholding on behalf of a version that no longer exists.
    """
    release(
        session,
        checkout_id=checkout_id,
        checkout_version=retired_version,
        cause=ReleaseCause.CANCELLED,
    )
    superseding = _version(session, ctx, checkout_id, next_version)
    card = require_approval(
        session,
        tenant_id=ctx.tenant_id,
        checkout=superseding.ref,
        receipt=receipt_inputs_for(registry.store(merchant_id)),
        correlation_id=ctx.correlation_id,
        reservation_ttl_seconds=checkout_service.RESERVATION_TTL_SECONDS,
        allocations=checkout_service.allocations_for(
            registry, merchant_id, list(superseding.content.get("line_items", {}))
        ),
        principal=ctx.principal,
    )
    retired = _version(session, ctx, checkout_id, retired_version)
    return checkout_service.approval_card_body(
        session,
        card,
        previous_version=retired_version,
        deltas=checkout_service.deltas_between(retired, superseding),
    )


def submit_checkout(
    session: Session,
    ctx: RequestContext,
    registry: MerchantRegistry,
    *,
    checkout_id: uuid.UUID,
    version: int,
    idempotency_key: str,
) -> dict[str, Any]:
    """Submit an approved version for admission. The demonstration's headline.

    Order of operations, all in the caller's single kernel transaction:

    1. **Ownership and capability.** ``checkout.submit_approved`` is the capability
       :func:`transaction_kernel.admit` itself checks by name for an agent principal; it
       is checked here too so a buyer session without it fails before any lock is taken.
    2. **A live attempt already?** A plain ``SELECT`` before any lock. If one exists this
       is the second of two concurrent submits, and ADR 0003 D9 says the answer is 200
       with ``DUPLICATE_OPERATION`` and the *winner's* attempt id -- the buyer sees one
       live payment, which is the two-tabs demonstration.
    3. **Admission.** ``admit`` locks the operating mode, the version, the reservation and
       the single-winner row, re-reads merchant truth through the registry's state source,
       and answers.
    4. **On ALLOWED**: spend the approval, move the version to ``EXECUTION_PENDING``,
       enqueue the create-order command and link it to the grant. The approval is spent
       *after* admission rather than before, because ``admit`` holds the version lock for
       the whole call and a consume before it would spend a decision for a payment that
       might then be refused.
    5. **On REAPPROVAL_REQUIRED**: finish the supersede -- N's hold released, N+1 given
       its receipt and a fresh hold -- and return the decision with the new card.

    Every one of those is one transaction. There is no point in it where a grant exists
    without a command, an attempt exists without a grant, or an approval has been spent
    for an admission that did not happen.
    """
    ctx.require("checkout.submit_approved")
    owner = assert_owner(session, ctx, checkout_id)
    view = _version(session, ctx, checkout_id, version)

    existing = _live_attempt(session, ctx, checkout_id)
    if existing is not None:
        return _duplicate_body(checkout_id, existing)

    approval = _recorded_approval(session, ctx, checkout_id, version)
    if approval is None:
        # Two causes, and only one of them is a client error. Under READ COMMITTED the
        # pre-check above and this read see different snapshots, so a submit that raced
        # and lost can find the approval already CONSUMED by the winner's admission even
        # though the attempt was invisible a statement earlier. Look again: if the winner
        # is now visible this is D9's duplicate, not "you never approved this".
        raced = _live_attempt(session, ctx, checkout_id)
        if raced is not None:
            return _duplicate_body(checkout_id, raced)
        raise ProblemError(
            409,
            "No recorded approval",
            "This version carries no live buyer approval; approve it before submitting.",
            checkout_id=str(checkout_id),
            version=version,
            code=RecoveryCode.REAPPROVAL_REQUIRED.value,
        )
    amount = Money(approval.amount_minor, approval.currency)
    checkout = CheckoutRef(checkout_id=checkout_id, version=version, content_hash=view.content_hash)
    try:
        decision = admit(
            session,
            AdmissionRequest(
                tenant_id=ctx.tenant_id,
                merchant_id=owner.merchant_id,
                checkout=checkout,
                amount=amount,
                operation=Operation.PAYMENT_CREATE_ORDER,
                idempotency_key=idempotency_key,
                principal=ctx.principal,
                correlation_id=ctx.correlation_id,
                approval_id=approval.id,
            ),
            merchant_state=registry.state_source(owner.merchant_id),
        )
    except IntegrityError as exc:
        # The one-non-terminal-attempt index refused this INSERT: two submits reached it
        # in the same instant and PostgreSQL picked the winner. ``admit`` has already
        # rolled the transaction back, so nothing of this request survives -- which is
        # exactly right, and is why the answer must be assembled outside it.
        raise ConcurrentAdmission(checkout_id) from exc

    if decision.allowed:
        return _on_allowed(
            session,
            ctx,
            decision=decision,
            checkout=checkout,
            amount=amount,
            approval_id=approval.id,
            idempotency_key=idempotency_key,
        )

    # A denial that lost a race names the winner. ADR 0003 D9: the buyer must see the one
    # live attempt rather than an outcome with nothing attached to it. Read after the
    # denial, because the winner committed while this transaction waited on the version
    # lock, and only now is its row visible.
    winner = (
        _live_attempt(session, ctx, checkout_id)
        if decision.code in (RecoveryCode.DUPLICATE_OPERATION, RecoveryCode.CONCURRENT_OPERATION)
        else None
    )
    extra: dict[str, Any] = {
        "outcome": decision.code.value,
        "attempt_id": None if winner is None else str(winner.id),
    }
    if decision.code is RecoveryCode.REAPPROVAL_REQUIRED and decision.next_version is not None:
        extra["approval_card"] = _supersede(
            session,
            ctx,
            registry,
            checkout_id=checkout_id,
            retired_version=version,
            next_version=decision.next_version,
            merchant_id=owner.merchant_id,
        )
    return _decision_body(decision, extra)


def duplicate_after_race(
    session: Session, ctx: RequestContext, checkout_id: uuid.UUID
) -> dict[str, Any]:
    """Assemble D9's answer after ``admit`` rolled this request's transaction back.

    A fresh transaction on the same session, opened explicitly and with the tenant re-bound
    as its first statement. Both halves matter: ``set_tenant`` writes a transaction-local
    setting and refuses to run outside a transaction, and row-level security is
    transaction-scoped, so without the re-bind every table would return nothing and this
    would answer 409 for a checkout that has a perfectly good live attempt.

    Nothing is written. The losing request's idempotency claim went with the rollback, so
    its key stays usable and a retry finds the winner through the ordinary pre-check.
    """
    session.rollback()
    if not session.in_transaction():
        session.begin()
    set_tenant(session, ctx.tenant_id)
    winner = _live_attempt(session, ctx, checkout_id)
    if winner is None:  # pragma: no cover - the index refused us, so a winner exists
        raise ProblemError(
            409,
            "Concurrent operation",
            "Another submit is in flight for this checkout; read it before retrying.",
            checkout_id=str(checkout_id),
            code=RecoveryCode.CONCURRENT_OPERATION.value,
        )
    return _duplicate_body(checkout_id, winner)


def _duplicate_body(checkout_id: uuid.UUID, attempt: PaymentAttempt) -> dict[str, Any]:
    """ADR 0003 D9: a second submit is told about the winner, not given a second attempt.

    Deliberately *not* a kernel decision. No admission ran -- the single-winner index
    already decided -- so inventing a :class:`~transaction_kernel.KernelDecision` here
    would put a decision id in the evidence for a decision the kernel never made.
    ``allowed`` is false and the code is ``DUPLICATE_OPERATION``, which is one of the two
    codes an agent may present to a buyer as an operation that is already under way.
    """
    return {
        "decision_id": None,
        "allowed": False,
        "code": RecoveryCode.DUPLICATE_OPERATION.value,
        "explanation": "attempt_already_exists_for_checkout",
        "checkout": {
            "checkout_id": str(checkout_id),
            "version": attempt.checkout_version,
            "content_hash": None,
        },
        "deltas": [],
        "grant_id": None,
        "payment_attempt_id": str(attempt.id),
        "next_version": None,
        "correlation_id": None,
        "outcome": RecoveryCode.DUPLICATE_OPERATION.value,
        "attempt_id": str(attempt.id),
    }


def _on_allowed(
    session: Session,
    ctx: RequestContext,
    *,
    decision: KernelDecision,
    checkout: CheckoutRef,
    amount: Money,
    approval_id: uuid.UUID,
    idempotency_key: str,
) -> dict[str, Any]:
    """Spend the approval, hand the grant to the worker, and say so. One transaction."""
    # KernelDecision.__post_init__ guarantees both on an allowed decision; narrowed here
    # so mypy sees it and so a future kernel change fails loudly rather than silently
    # enqueueing a command with "None" where the grant id belongs.
    attempt_id = decision.payment_attempt_id
    grant_id = decision.grant_id
    if attempt_id is None or grant_id is None:  # pragma: no cover - kernel invariant
        raise ProblemError(
            500,
            "Incomplete admission",
            "An allowed admission must name both its payment attempt and its grant.",
        )

    # ADR 0003 D4a. One guarded UPDATE moves RECORDED -> CONSUMED only where the tenant,
    # checkout, version, hash, amount and expiry all still match; a replay finds it
    # CONSUMED and refuses. Run after admit so the version lock is still held by this
    # transaction and the two cannot interleave.
    consume_recorded(
        session,
        tenant_id=ctx.tenant_id,
        approval_id=approval_id,
        checkout=checkout,
        amount=amount,
        correlation_id=ctx.correlation_id,
        actor=ctx.actor_type,
        principal_id=ctx.principal.principal_id,
    )

    # APPROVED -> EXECUTION_PENDING: a grant is issued and the create-order command is in
    # the outbox. The worker's record_create_order_result moves it on to AWAITING_PAYMENT,
    # and it can only do that from this state.
    transition(
        session,
        tenant_id=ctx.tenant_id,
        checkout=checkout,
        target=CheckoutState.EXECUTION_PENDING,
        reason="admitted",
        actor=ctx.actor_type if ctx.actor_type is not ActorType.SYSTEM else ActorType.BUYER,
        correlation_id=ctx.correlation_id,
        principal_id=ctx.principal.principal_id,
    )

    receipt = session.execute(
        select(PaymentAttempt.receipt).where(
            PaymentAttempt.tenant_id == ctx.tenant_id, PaymentAttempt.id == attempt_id
        )
    ).scalar_one()

    command = CreateOrderCommand(
        tenant_id=str(ctx.tenant_id),
        payment_attempt_id=str(attempt_id),
        grant_id=str(grant_id),
        checkout_id=str(checkout.checkout_id),
        checkout_version=checkout.version,
        content_hash=checkout.content_hash,
        amount_minor=amount.minor,
        currency=amount.currency,
        receipt=receipt,
        # What a reviewer sees in the Razorpay dashboard beside the order. The command
        # refuses a note that disagrees with its own field, so this cannot drift.
        notes={
            "tenant_id": str(ctx.tenant_id),
            "checkout_id": str(checkout.checkout_id),
            "payment_attempt_id": str(attempt_id),
            "checkout_version": str(checkout.version),
            "content_hash": checkout.content_hash,
        },
        correlation_id=str(ctx.correlation_id),
    )
    enqueued = enqueue_command(session, command, idempotency_key=idempotency_key)
    # The link is what the proof chain follows from a grant to the provider request that
    # consumed it. In this transaction, so the grant and its command commit together or
    # not at all.
    link_command(
        session,
        tenant_id=ctx.tenant_id,
        grant_id=grant_id,
        outbox_command_id=enqueued.command_id,
        correlation_id=ctx.correlation_id,
    )
    return _decision_body(
        decision,
        {
            "outcome": RecoveryCode.OK.value,
            "attempt_id": str(attempt_id),
            "command_id": str(enqueued.command_id),
            "state": CheckoutState.EXECUTION_PENDING.value,
        },
    )


# ---------------------------------------------------------------------- cancellation


def cancel_checkout(
    session: Session,
    ctx: RequestContext,
    *,
    checkout_id: uuid.UUID,
    reason: str,
) -> dict[str, Any]:
    """Cancel the current version within policy, or explain why not. Always HTTP 200.

    :func:`transaction_kernel.cancel` never raises for a refusal, and this never turns one
    into an error status. "You cannot cancel while a payment is in flight" is the system
    protecting a buyer from a cancelled order they have already been charged for; the
    remedy is invalidation and reconciliation (specification 10.8), not a 409 the client
    will retry.

    The shape mirrors a kernel decision -- ``allowed``, ``code``, ``explanation``,
    ``checkout`` -- but it is deliberately not one: no admission ran, and a
    :class:`~transaction_kernel.KernelDecision` in the evidence should mean a decision the
    kernel actually made.
    """
    ctx.require("checkout.cancel")
    owner = assert_owner(session, ctx, checkout_id)
    view = _version(session, ctx, checkout_id, owner.current_version)
    result = cancel(
        session,
        tenant_id=ctx.tenant_id,
        checkout=view.ref,
        principal=ctx.principal,
        reason=reason,
        correlation_id=ctx.correlation_id,
    )
    return {
        "allowed": result.allowed,
        "code": result.code.value,
        "explanation": result.explanation,
        "checkout": CheckoutRefOut.of(result.checkout).model_dump(mode="json"),
        "from_state": None if result.from_state is None else result.from_state.value,
        "grants_revoked": [str(item) for item in result.grants_revoked],
        "attempt_expired": None if result.attempt_expired is None else str(result.attempt_expired),
        "reservation_release": None
        if result.reservation_release is None
        else result.reservation_release.value,
    }
