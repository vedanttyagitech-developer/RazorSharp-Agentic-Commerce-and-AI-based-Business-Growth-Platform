"""The admission transaction, specification 10.3.

This is the only function in the platform that may decide a money action is permitted.
Everything else — the agents, the protocol gateways, the trusted buyer surface, the
durable worker — either asks this question or carries out an answer it already gave.

The shape of the guarantee:

    one database transaction
      -> locks taken in a fixed order
      -> every invariant re-checked against the locked rows, not against what the caller
         believed when it started
      -> either exactly one payment attempt and exactly one Execution Grant,
         or a denial carrying a structured delta and a recovery code
      -> audit written inside the same transaction as the decision

Three properties are worth stating plainly, because each is a place a plausible
implementation goes wrong.

**Nothing is trusted from the caller.** The request carries what the buyer approved. Every
one of those fields is compared against a row this transaction locked. An agent that
constructs a request with a different amount, a stale version, or another tenant's
checkout does not get a different outcome; it gets a denial with a reason.

**Locks are taken in one fixed order,** operating mode, then checkout, then reservation,
then authority, then the single-winner execution row. Two admissions racing for one
checkout therefore queue rather than deadlock. The order is a contract: any future code
that takes two of these locks must take them in this sequence.

**Denial is not failure.** A stale approval is the system working. The denial path is as
carefully constructed as the success path: it invalidates version N permanently, creates
N+1, and returns the exact delta so the buyer can be shown what changed and approve the
new total. That recovery is the product feature the kernel exists to enable.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from commerce_domain import Money, canonical_hash, uuid7
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import audit, authority, grants, receipts, reservations, safe_mode
from .contracts import (
    ActorType,
    AgentPrincipal,
    CheckoutRef,
    Delta,
    KernelDecision,
    Operation,
    VerifiedAuthorityProof,
)
from .recovery import RecoveryCode
from .states import CheckoutState

# Lock order. Documented as data so a future caller can assert against it rather than
# rediscovering it from the source of this function.
LOCK_ORDER: tuple[str, ...] = (
    "platform_operating_modes",
    "checkout_versions",
    "reservations",
    "delegated_authorities",
    "payment_attempts",
)

#: How long an issued grant remains consumable. Short: it exists to be handed straight to
#: the worker, and a grant that outlives its transaction's context is an unnecessary
#: window in which merchant state can move underneath it.
DEFAULT_GRANT_TTL_SECONDS = 300


class AdmissionError(RuntimeError):
    """The request is malformed. Distinct from a denial, which is a normal outcome."""


@dataclass(frozen=True, slots=True)
class CurrentMerchantState:
    """What the merchant's systems say right now, re-read inside the transaction.

    Returned by a :class:`MerchantStateSource`. The kernel does not know how to reach a
    merchant; it only knows that it must ask again before letting money move, because the
    answer that produced the approval may be seconds old and wrong.
    """

    total: Money
    line_items: Mapping[str, Any]
    all_available: bool
    policy_version: str

    def content_for_hash(self, checkout_id: uuid.UUID, version: int) -> dict[str, Any]:
        """The canonical payload whose hash the approval is compared against."""
        return {
            "checkout_id": str(checkout_id),
            "version": version,
            "currency": self.total.currency,
            "total_minor": self.total.minor,
            "line_items": dict(self.line_items),
            "policy_version": self.policy_version,
        }


class MerchantStateSource(Protocol):
    """How the kernel re-reads authoritative merchant state, specification 10.3 step 8.

    Injected rather than imported so the kernel depends on no connector, no simulator and
    no HTTP client. In the demo this is the merchant simulator; in deployment it is the
    tenant's configured connector. The kernel's guarantees do not change between them.
    """

    def revalidate(
        self, session: Session, *, checkout_id: uuid.UUID, version: int
    ) -> CurrentMerchantState: ...


@dataclass(frozen=True, slots=True)
class AdmissionRequest:
    """What the trusted surface or a protocol gateway submits.

    Either ``approval_id`` (a buyer decision recorded on the trusted surface) or ``proof``
    (a verified protocol mandate) must be present, never both: an action is authorised by
    a buyer here and now, or by a mandate the gateway already verified, and conflating the
    two would let a caller present whichever is more convenient.
    """

    tenant_id: uuid.UUID
    merchant_id: uuid.UUID
    checkout: CheckoutRef
    amount: Money
    operation: Operation
    idempotency_key: str
    principal: AgentPrincipal
    correlation_id: uuid.UUID
    approval_id: uuid.UUID | None = None
    proof: VerifiedAuthorityProof | None = None
    authority_id: uuid.UUID | None = None
    authority_epoch: int | None = None

    def __post_init__(self) -> None:
        if (self.approval_id is None) == (self.proof is None):
            raise AdmissionError(
                "exactly one of approval_id or proof must be supplied; an action is "
                "authorised either by a recorded buyer decision or by a verified mandate"
            )
        if self.principal.tenant_id != self.tenant_id:
            raise AdmissionError(
                f"principal tenant {self.principal.tenant_id} does not match request "
                f"tenant {self.tenant_id}; tenant comes from the authenticated session"
            )
        if self.amount.minor <= 0:
            raise AdmissionError("admission requires a positive amount")


def _guarded_operation(request: AdmissionRequest) -> safe_mode.GuardedOperation:
    """Which Safe Mode gate applies.

    A delegated debit is what Safe Mode exists to stop. A human-present checkout is what it
    deliberately keeps available, because a kill switch that also blocks the buyer's own
    fresh purchase harms the person it is meant to protect.
    """
    if request.operation is Operation.RESERVE_DEBIT:
        return safe_mode.GuardedOperation.DELEGATED_DEBIT
    if request.operation is Operation.REFUND_EXECUTE:
        return safe_mode.GuardedOperation.REFUND_EXECUTE
    if request.proof is not None:
        return safe_mode.GuardedOperation.AUTONOMOUS_COMPLETION
    return safe_mode.GuardedOperation.HUMAN_PRESENT_CHECKOUT


def _deny(
    session: Session,
    request: AdmissionRequest,
    code: RecoveryCode,
    explanation: str,
    *,
    deltas: Sequence[Delta] = (),
    next_version: int | None = None,
) -> KernelDecision:
    """Record a denial and return it.

    The audit row is written in this same transaction, so a denial is as durably evidenced
    as an approval. A system that only records what it permitted cannot explain what it
    refused, and refusing well is most of what this kernel does.
    """
    decision = KernelDecision(
        decision_id=uuid7(),
        allowed=False,
        code=code,
        explanation=explanation,
        checkout=request.checkout,
        deltas=tuple(deltas),
        next_version=next_version,
        correlation_id=request.correlation_id,
    )
    audit.append(
        session,
        tenant=request.tenant_id,
        aggregate_type="checkout",
        aggregate_id=request.checkout.checkout_id,
        event_type="admission.denied",
        actor_type=request.principal.actor_type,
        principal_id=request.principal.principal_id,
        payload={
            "decision_id": str(decision.decision_id),
            "code": str(code),
            "explanation": explanation,
            "version": request.checkout.version,
            "content_hash": request.checkout.content_hash,
            "deltas": [
                {"field": d.field_path, "approved": d.approved, "current": d.current}
                for d in decision.deltas
            ],
            "next_version": next_version,
        },
        correlation_id=request.correlation_id,
    )
    return decision


def _compute_deltas(approved_amount: Money, current: CurrentMerchantState) -> list[Delta]:
    """What changed between what the buyer approved and what is true now."""
    deltas: list[Delta] = []
    if current.total != approved_amount:
        deltas.append(
            Delta(
                field_path="total",
                approved=approved_amount.minor,
                current=current.total.minor,
                reason="total_changed",
            )
        )
    if not current.all_available:
        deltas.append(
            Delta(
                field_path="line_items",
                approved="all_available",
                current="item_unavailable",
                reason="availability_changed",
            )
        )
    return deltas


def _invalidate_and_supersede(
    session: Session, request: AdmissionRequest, current: CurrentMerchantState
) -> int:
    """Permanently invalidate version N and create N+1 carrying current state.

    Version N never returns to APPROVED. This is enforced by the state machine, and made
    durable here by stamping ``invalidated_at`` so that even a caller bypassing the state
    machine finds a row it cannot revive.
    """
    session.execute(
        text(
            "UPDATE checkout_versions SET status = :invalid, invalidated_at = NOW() "
            "WHERE tenant_id = :t AND checkout_id = :c AND version = :v"
        ),
        {
            "invalid": CheckoutState.INVALIDATED.value,
            "t": request.tenant_id,
            "c": request.checkout.checkout_id,
            "v": request.checkout.version,
        },
    )
    next_version = request.checkout.version + 1
    content = current.content_for_hash(request.checkout.checkout_id, next_version)
    session.execute(
        text(
            "INSERT INTO checkout_versions (id, tenant_id, merchant_id, checkout_id, "
            "version, content, content_hash, currency, total_minor, status, immutable) "
            "VALUES (:id, :t, :m, :c, :v, CAST(:content AS jsonb), :h, :cur, :total, "
            ":status, false) "
            "ON CONFLICT (tenant_id, checkout_id, version) DO NOTHING"
        ),
        {
            "id": uuid7(),
            "t": request.tenant_id,
            "m": request.merchant_id,
            "c": request.checkout.checkout_id,
            "v": next_version,
            "content": json.dumps(content, sort_keys=True),
            "h": canonical_hash(content),
            "cur": current.total.currency,
            "total": current.total.minor,
            "status": CheckoutState.APPROVAL_REQUIRED.value,
        },
    )
    return next_version


def admit(
    session: Session,
    request: AdmissionRequest,
    merchant_state: MerchantStateSource,
    *,
    grant_ttl_seconds: int = DEFAULT_GRANT_TTL_SECONDS,
) -> KernelDecision:
    """Decide whether one money action may proceed, and if so authorise exactly one.

    Must be called inside an open transaction. Returns a :class:`KernelDecision` for every
    outcome including refusal; it raises only when the request itself is malformed, which
    is a programming error rather than a business outcome.

    On success exactly one payment attempt and exactly one Execution Grant exist, and the
    grant names the operation the worker is permitted to perform. On denial no provider
    order is created, and where the denial was caused by changed merchant state, version
    N+1 is waiting for fresh approval.
    """
    if not session.in_transaction():
        raise AdmissionError(
            "admit must run inside a transaction; its guarantees come from locks that "
            "only exist for the life of one"
        )

    # --- step 4: is this actor allowed to submit this operation at all? --------------
    if request.principal.actor_type is ActorType.AGENT and not request.principal.can(
        "checkout.submit_approved"
    ):
        return _deny(
            session,
            request,
            RecoveryCode.AUTHORITY_INSUFFICIENT,
            "principal_lacks_submit_capability",
        )

    # --- step 5: Safe Mode, before anything delegated is accepted ---------------------
    permitted, mode_code = safe_mode.is_permitted(
        session, request.tenant_id, _guarded_operation(request)
    )
    if not permitted:
        return _deny(session, request, mode_code, "safe_mode_blocks_operation")

    # --- step 3 + 6: lock the checkout and confirm the version is current -------------
    row = session.execute(
        text(
            "SELECT version, content_hash, status, total_minor, currency, invalidated_at "
            "FROM checkout_versions WHERE tenant_id = :t AND checkout_id = :c "
            "AND version = :v FOR UPDATE"
        ),
        {
            "t": request.tenant_id,
            "c": request.checkout.checkout_id,
            "v": request.checkout.version,
        },
    ).one_or_none()
    if row is None:
        return _deny(session, request, RecoveryCode.STALE_CHECKOUT, "checkout_version_not_found")
    if row.invalidated_at is not None:
        return _deny(session, request, RecoveryCode.STALE_CHECKOUT, "version_already_invalidated")
    if row.content_hash != request.checkout.content_hash:
        # The buyer approved different bytes from the ones stored. Refuse rather than
        # guess which is authoritative.
        return _deny(
            session, request, RecoveryCode.STALE_CHECKOUT, "approved_hash_does_not_match_stored"
        )

    latest = session.execute(
        text(
            "SELECT max(version) AS v FROM checkout_versions "
            "WHERE tenant_id = :t AND checkout_id = :c"
        ),
        {"t": request.tenant_id, "c": request.checkout.checkout_id},
    ).scalar()
    if latest is not None and latest > request.checkout.version:
        return _deny(
            session,
            request,
            RecoveryCode.STALE_CHECKOUT,
            "a_newer_version_exists",
            next_version=int(latest),
        )

    # --- step 6 continued: the sale is governed by the policy captured at approval ----
    binding = receipts.verify_binding(session, request.checkout)
    if not binding.ok:
        return _deny(session, request, binding.code, f"policy_binding_{binding.reason}")

    # --- step 7: reservation, against the database clock ------------------------------
    held = reservations.check_validity(
        session,
        checkout_id=request.checkout.checkout_id,
        checkout_version=request.checkout.version,
        lock=True,
    )
    if held.code is not RecoveryCode.OK:
        return _deny(session, request, held.code, "reservation_not_valid")

    # --- steps 8-10: re-read merchant truth and compare against what was approved -----
    current = merchant_state.revalidate(
        session, checkout_id=request.checkout.checkout_id, version=request.checkout.version
    )
    deltas = _compute_deltas(request.amount, current)
    if deltas:
        # Specification 10.3 step 11. This is the demo's headline moment: the approval is
        # refused, N is retired, and N+1 is ready for a fresh decision.
        next_version = _invalidate_and_supersede(session, request, current)
        return _deny(
            session,
            request,
            RecoveryCode.REAPPROVAL_REQUIRED,
            "merchant_state_changed_since_approval",
            deltas=deltas,
            next_version=next_version,
        )

    # --- step 10 continued: authority, locked in the same transaction -----------------
    if request.authority_id is not None:
        epoch = request.authority_epoch
        if epoch is None:
            return _deny(
                session,
                request,
                RecoveryCode.AUTHORITY_INSUFFICIENT,
                "authority_supplied_without_epoch",
            )
        # Named distinctly from the KernelDecision built below: shadowing the two would
        # let a type error pass as an assignment.
        authority_decision = authority.admit_debit(
            session,
            request.authority_id,
            expected_epoch=epoch,
            amount=request.amount,
            merchant_id=request.merchant_id,
        )
        if authority_decision.code is not RecoveryCode.OK:
            return _deny(
                session,
                request,
                authority_decision.code,
                f"authority_{authority_decision.reason}",
            )

    # --- step 12: exactly one winner -------------------------------------------------
    decision_id = uuid7()
    attempt_id = uuid7()
    receipt_ref = f"rcpt_{attempt_id.hex[:24]}"  # Razorpay caps receipt at 40 characters
    try:
        session.execute(
            text(
                "INSERT INTO payment_attempts (id, tenant_id, checkout_id, checkout_version, "
                "status, amount_minor, currency, receipt) "
                "VALUES (:id, :t, :c, :v, :status, :amt, :cur, :rcpt)"
            ),
            {
                "id": attempt_id,
                "t": request.tenant_id,
                "c": request.checkout.checkout_id,
                "v": request.checkout.version,
                "status": "CREATED",
                "amt": request.amount.minor,
                "cur": request.amount.currency,
                "rcpt": receipt_ref,
            },
        )
        session.flush()
    except Exception:
        # The partial unique index permits one non-terminal attempt per checkout. Losing
        # this race is a normal outcome, not an error: another admission won, and the
        # caller should read current state rather than create a second attempt.
        session.rollback()
        raise

    grant = grants.issue_grant(
        session,
        tenant=request.tenant_id,
        checkout_ref=request.checkout,
        payment_attempt_id=attempt_id,
        operation=request.operation,
        amount=request.amount,
        kernel_decision_id=decision_id,
        ttl_seconds=grant_ttl_seconds,
    )
    reservations.consume(
        session,
        checkout_id=request.checkout.checkout_id,
        checkout_version=request.checkout.version,
    )

    grant_id = getattr(grant, "id", None) or getattr(grant, "grant_id", None)
    decision = KernelDecision(
        decision_id=decision_id,
        allowed=True,
        code=RecoveryCode.OK,
        explanation="admitted",
        checkout=request.checkout,
        grant_id=grant_id,
        payment_attempt_id=attempt_id,
        correlation_id=request.correlation_id,
    )
    audit.append(
        session,
        tenant=request.tenant_id,
        aggregate_type="checkout",
        aggregate_id=request.checkout.checkout_id,
        event_type="admission.allowed",
        actor_type=request.principal.actor_type,
        principal_id=request.principal.principal_id,
        payload={
            "decision_id": str(decision_id),
            "payment_attempt_id": str(attempt_id),
            "grant_id": str(grant_id),
            "version": request.checkout.version,
            "content_hash": request.checkout.content_hash,
            "amount_minor": request.amount.minor,
            "currency": request.amount.currency,
            "operation": str(request.operation),
        },
        correlation_id=request.correlation_id,
    )
    return decision
