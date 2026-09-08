"""Approve, hold, reject, submit, cancel. Steps 4 and 6 to 8 of the demonstration.

This is the part most conversational-commerce demonstrations skip, and it is the reason
this project is a payments project rather than a shopping chatbot. Six operations:

``approve``
    The buyer's decision, bound to bytes rather than to an identifier. The request body
    **echoes** the content hash, the amount and the currency, and
    :func:`transaction_kernel.record_approval` compares all three against the row it has
    locked. A client that echoes a hash the version does not carry is refused, which is
    what makes "I approved this" mean "I approved *these bytes*".

``approve and pay``
    That same decision and the admission it exists for, in one call. Approving and then
    submitting as two requests leaves a real window in which a version sits ``APPROVED``
    with nothing spending it, and it makes "one confirmation" true only on the screen that
    fires the second request automatically. :func:`approve_and_pay` runs both under the
    one version lock, so ``APPROVAL_REQUIRED -> APPROVED -> EXECUTION_PENDING`` is a
    single committed step and no other writer can see the middle of it.

``hold``
    The buyer was asked and said not now, which is not the same as cancelling. Nothing
    transitions, the hold keeps its stock and the same hash stays approvable; all that is
    written is the evidence that the question was put and declined.

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
kernel's ``freeze_for_approval`` documents the supersede path as an entry point *for its
caller* -- and both are completed here, in the same transaction as the admission, so a
crash cannot leave a spent approval unspent or an unapprovable N+1. See
:func:`submit_checkout` for the ordering and the reasoning.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from commerce_domain import Money
from durable_work.commands import CreateOrderCommand, enqueue_command
from merchant_sim import receipt_inputs_for
from platform_db import Approval, PaymentAttempt, set_tenant
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from transaction_kernel import (
    ActorType,
    AdmissionDecision,
    AdmissionRequest,
    CheckoutRef,
    CheckoutState,
    Operation,
    PaymentState,
    RecoveryCode,
    admit,
    bound_terms_for_requote,
    cancel,
    consume_recorded,
    defer_approval,
    freeze_for_approval,
    link_command,
    read_versions,
    record_approval,
    reject_approval,
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
    "SubmitOutcome",
    "admit_approved_version",
    "approve_and_submit",
    "approve_version",
    "cancel_checkout",
    "defer_version",
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


@dataclass(frozen=True, slots=True)
class SubmitOutcome:
    """What one submit produced: the body every caller returns, and the decision if any.

    ``decision`` is ``None`` for exactly one answer -- ADR 0003 D9's duplicate, where the
    single-winner index had already decided and no admission ran. See
    :func:`_duplicate_body` for why inventing a :class:`~transaction_kernel.AdmissionDecision`
    there would be dishonest.

    This shape exists because the protocol transports need the typed decision and the HTTP
    routers need the body, and building the body twice would be two chances for the two
    surfaces to report the same admission differently. A caller that needs the decision
    must handle its absence rather than assume one.
    """

    decision: AdmissionDecision | None
    body: dict[str, Any]


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


def _require_version(
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
            # The sweep that retires lapsed approvals is periodic, so "RECORDED" alone can
            # name one the clock has already passed. The kernel refuses it either way --
            # ``expires_at > now()`` is in the guarded UPDATE -- but refusing here means
            # the buyer is told their approval lapsed rather than told nothing was found.
            Approval.expires_at > func.now(),
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


def _decision_body(decision: AdmissionDecision, extra: dict[str, Any]) -> dict[str, Any]:
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


def defer_version(
    session: Session,
    ctx: RequestContext,
    *,
    checkout_id: uuid.UUID,
    version: int,
    content_hash: str,
    reason: str,
) -> dict[str, Any]:
    """The buyer was asked and said not now. Audited, and nothing else happens.

    A buyer who declines at the approval card is not usually cancelling their shopping.
    Until this existed the only "no" the surface had was :func:`reject_version`, which
    retires the version and hands the stock back, so "let me think" cost the buyer their
    cart's hold and cost the merchant the sale they were three seconds from making.
    :func:`transaction_kernel.defer_approval` records the decline and leaves every one of
    those things exactly where it was: the version is still ``APPROVAL_REQUIRED``, the
    reservation still holds its stock until its own deadline, and the same content hash is
    still approvable, by this endpoint's own rules and by the kernel's.

    The capability is ``checkout.reject`` rather than a new one, and that is deliberate:
    this is the buyer's "no", and an agent must no more be able to record that a buyer
    declined than that they consented. An agent writing "the buyer passed" into the audit
    stream is the same forgery as one writing "the buyer agreed", made one step earlier.

    ``reservation`` is rendered by the same helper the approval card uses, so the
    countdown the buyer sees after declining is the one they saw before it.
    """
    ctx.require("checkout.reject")
    assert_owner(session, ctx, checkout_id)
    hold = defer_approval(
        session,
        tenant_id=ctx.tenant_id,
        checkout=CheckoutRef(checkout_id=checkout_id, version=version, content_hash=content_hash),
        principal=ctx.principal,
        reason=reason,
        correlation_id=ctx.correlation_id,
    )
    reservation = checkout_service.reservation_out(
        session, checkout_id=checkout_id, version=version
    )
    return {
        "checkout": CheckoutRefOut.of(hold.checkout).model_dump(mode="json"),
        "state": hold.state.value,
        "reason": reason,
        "held_at": rfc3339(hold.held_at),
        "audit_event_id": str(hold.event_id),
        "reservation": None if reservation is None else reservation.model_dump(mode="json"),
    }


# --------------------------------------------------------------- steps 6, 7 and 8


def _freeze_successor(
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
    :func:`transaction_kernel.freeze_for_approval` documents as the supersede path: it
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
    # What the buyer already holds, read off the retired version's own receipt before the
    # replacement is frozen. A price moving is not a renegotiation: without this the
    # replacement's rules would be built wholly from current policy, and a merchant could
    # tighten a return window in the same breath as raising a price and have the buyer
    # accept it by accepting the amount.
    retired = _require_version(session, ctx, checkout_id, retired_version)
    carried = bound_terms_for_requote(session, retired.ref)
    if not carried:
        # The binding did not verify, so there is nothing to inherit and current policy is
        # not a substitute for it. Refusing is the whole point: silently narrower rights are
        # harder to notice than a failed requote.
        raise ProblemError(
            409,
            "This checkout cannot be re-priced",
            "The terms the original approval was made under could not be read back, and a "
            "replacement must not be offered under different ones. Start a new checkout.",
            checkout_id=str(checkout_id),
            reason="bound_terms_unreadable",
            retired_version=retired_version,
        )

    superseding = _require_version(session, ctx, checkout_id, next_version)
    card = freeze_for_approval(
        session,
        tenant_id=ctx.tenant_id,
        checkout=superseding.ref,
        receipt=receipt_inputs_for(registry.store(merchant_id), carry_forward=carried),
        correlation_id=ctx.correlation_id,
        reservation_ttl_seconds=checkout_service.RESERVATION_TTL_SECONDS,
        allocations=checkout_service.allocations_for(
            registry, merchant_id, list(superseding.content.get("line_items", {}))
        ),
        principal=ctx.principal,
    )
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
    """Submit an approved version for admission, and answer with the body. See below.

    The whole of the work is in :func:`admit_approved_version`; this is the form the HTTP
    routers want, which is the body alone. Two functions rather than one because the
    protocol transports need the typed :class:`~transaction_kernel.AdmissionDecision` as well,
    and a second implementation of the admission would be a second thing to keep in step
    with this one.
    """
    return admit_approved_version(
        session,
        ctx,
        registry,
        checkout_id=checkout_id,
        version=version,
        idempotency_key=idempotency_key,
    ).body


def admit_approved_version(
    session: Session,
    ctx: RequestContext,
    registry: MerchantRegistry,
    *,
    checkout_id: uuid.UUID,
    version: int,
    idempotency_key: str,
    expected_content_hash: str | None = None,
) -> SubmitOutcome:
    """Submit an approved version for admission. The demonstration's headline.

    ``expected_content_hash`` is the hash a caller believes it is submitting, echoed back.
    The trusted surface passes ``None`` -- its approve step already bound the buyer to the
    bytes, and the kernel re-checks the version's own hash under lock regardless. A
    protocol caller passes what it echoed, because it is further from the state than the
    browser is and a mismatch there means it has not re-read the checkout: refusing before
    a lock is taken is a better answer than a denial three services later. It is a
    pre-check and never a substitute for the kernel's own.

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
    view = _require_version(session, ctx, checkout_id, version)
    if expected_content_hash is not None and expected_content_hash != view.content_hash:
        raise ProblemError(
            409,
            "Checkout version superseded",
            "The content hash echoed back is not the one this version carries; re-read "
            "the checkout before submitting it.",
            checkout_id=str(checkout_id),
            version=version,
            code=RecoveryCode.STALE_CHECKOUT.value,
        )

    existing = _live_attempt(session, ctx, checkout_id)
    if existing is not None:
        return SubmitOutcome(None, _duplicate_body(checkout_id, existing))

    approval = _recorded_approval(session, ctx, checkout_id, version)
    if approval is None:
        # Two causes, and only one of them is a client error. Under READ COMMITTED the
        # pre-check above and this read see different snapshots, so a submit that raced
        # and lost can find the approval already CONSUMED by the winner's admission even
        # though the attempt was invisible a statement earlier. Look again: if the winner
        # is now visible this is D9's duplicate, not "you never approved this".
        raced = _live_attempt(session, ctx, checkout_id)
        if raced is not None:
            return SubmitOutcome(None, _duplicate_body(checkout_id, raced))
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
        return SubmitOutcome(
            decision,
            _spend_approval_and_enqueue(
                session,
                ctx,
                decision=decision,
                checkout=checkout,
                amount=amount,
                approval_id=approval.id,
                operation=Operation.PAYMENT_CREATE_ORDER,
                idempotency_key=idempotency_key,
            ),
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
    if decision.code is RecoveryCode.REAPPROVAL_REQUIRED:
        # Keyed on the code alone, and the successor asserted rather than tested. The
        # kernel answers SOLD_OUT for the one case that used to reach REAPPROVAL_REQUIRED
        # with nothing to approve, so a null successor here is now a kernel bug, and
        # skipping quietly would answer the buyer "approve the new version" while naming
        # no version. ``next_version`` is deliberately not the gate: it is also set on the
        # ``a_newer_version_exists`` denial, which writes no N+1 of its own.
        if decision.next_version is None:
            raise AssertionError(
                "REAPPROVAL_REQUIRED without a successor version: the kernel invalidated "
                "an approval and offered nothing in its place"
            )
        extra["approval_card"] = _freeze_successor(
            session,
            ctx,
            registry,
            checkout_id=checkout_id,
            retired_version=version,
            next_version=decision.next_version,
            merchant_id=owner.merchant_id,
        )
    return SubmitOutcome(decision, _decision_body(decision, extra))


# ------------------------------------------------------------- one confirmation


def approve_and_submit(
    session: Session,
    ctx: RequestContext,
    registry: MerchantRegistry,
    *,
    checkout_id: uuid.UUID,
    version: int,
    content_hash: str,
    amount_minor: int,
    currency: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Record the buyer's approval and admit it, under one lock, in one transaction.

    Why this exists beside :func:`approve_version` and :func:`submit_checkout`. Approving
    and submitting as two requests means a version really does sit ``APPROVED`` between
    them with nothing spending it -- that window is why
    :func:`transaction_kernel.expire_stale_approvals` has to exist -- and it means the
    "one confirmation" the buyer was promised is a property of the screen that fires the
    second request rather than of the kernel. Here the approval and the admission are the
    same transaction, and :func:`transaction_kernel.record_approval` takes the version
    lock at the top of it and holds it to the commit, so
    ``APPROVAL_REQUIRED -> APPROVED -> EXECUTION_PENDING`` has no observable middle: no
    other writer can supersede, reject or sweep the version between the decision and the
    admission it was made for.

    Nothing about the two-step path changes and nothing is weakened. The approval is still
    recorded against echoed bytes and still refused for an amount the version does not
    carry; admission still re-reads merchant truth under its own locks; the approval is
    still spent by :func:`consume_recorded` after the decision, never before it. This
    composes the two functions rather than reimplementing either, so the approval block in
    this body is byte-for-byte the one ``approve`` returns and the decision is the one
    ``submit`` returns.

    **Always HTTP 200 for a kernel answer**, denial included (ADR 0003 D15). A price that
    moved since the card was drawn answers ``allowed: false`` with
    ``REAPPROVAL_REQUIRED``, the exact deltas and version N+1's card under
    ``approval_card`` -- the same body ``submit`` produces, because it is produced by the
    same code. A refusal here is the product working; raising would teach every client in
    the chain to retry a buyer's consent.

    ``approval`` is the recorded decision, and it is ``null`` for exactly one answer: the
    ADR 0003 D9 duplicate below, where a live attempt already existed and nothing was
    recorded because there was nothing left to decide.
    """
    # Both halves are required by name and both are checked here, before a row is written.
    # The composed functions check them again, but the second of those checks would land
    # after the approval had already been recorded, and a session that may consent but may
    # not submit should be refused without having consented to anything.
    ctx.require("checkout.approve")
    ctx.require("checkout.submit_approved")
    assert_owner(session, ctx, checkout_id)

    # A checkout that already has a live attempt has already been paid for once, and ADR
    # 0003 D9's answer is the winner's id rather than a second attempt. Without this the
    # approval would be recorded onto a version admission then refuses for being past
    # ``APPROVAL_REQUIRED`` -- a 409 for a buyer whose payment is perfectly healthy.
    existing = _live_attempt(session, ctx, checkout_id)
    if existing is not None:
        return {**_duplicate_body(checkout_id, existing), "approval": None}

    approved = approve_version(
        session,
        ctx,
        checkout_id=checkout_id,
        version=version,
        content_hash=content_hash,
        amount_minor=amount_minor,
        currency=currency,
    )
    # ``expected_content_hash`` stays None here for the reason it does on the trusted
    # surface: the approval one statement earlier bound this caller to these bytes under
    # the lock admission is about to reuse, which is a stronger check than an echo.
    outcome = admit_approved_version(
        session,
        ctx,
        registry,
        checkout_id=checkout_id,
        version=version,
        idempotency_key=idempotency_key,
    )
    return {**outcome.body, "approval": approved["approval"]}


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
    already decided -- so inventing a :class:`~transaction_kernel.AdmissionDecision` here
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


def _spend_approval_and_enqueue(
    session: Session,
    ctx: RequestContext,
    *,
    decision: AdmissionDecision,
    checkout: CheckoutRef,
    amount: Money,
    approval_id: uuid.UUID,
    operation: Operation,
    idempotency_key: str,
) -> dict[str, Any]:
    """Spend the approval, hand the grant to the worker, and say so. One transaction."""
    # AdmissionDecision.__post_init__ guarantees both on an allowed decision; narrowed here
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
        # What the buyer agreed to *do*, not only how much for. The column has always been
        # written; comparing it stops a consent given for one operation from being spent
        # against another on the same bytes for the same amount.
        action=operation.value,
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
    :class:`~transaction_kernel.AdmissionDecision` in the evidence should mean a decision the
    kernel actually made.
    """
    ctx.require("checkout.cancel")
    owner = assert_owner(session, ctx, checkout_id)
    view = _require_version(session, ctx, checkout_id, owner.current_version)
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
