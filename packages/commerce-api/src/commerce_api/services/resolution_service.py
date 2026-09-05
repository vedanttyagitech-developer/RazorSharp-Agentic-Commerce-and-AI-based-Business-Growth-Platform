"""What would settle a finding, as data. Deterministic, and it never executes.

Specification 6.4.2. Like the reconciliation service this is code and not an agent
(``docs/briefs/AGENT_ROSTER.md``): no model, no prompt, no judgement. An agent may read
what comes back and explain it; an agent may never do this job, because this is where the
amount is decided.

Four inputs, and one of them is the whole point
-----------------------------------------------
A resolution is decided from the payment attempt, the verified provider state
(:mod:`commerce_api.services.reconciliation_service`), the refund ledger, and **the
order's immutable Policy-at-Sale Receipt** -- never the merchant's current policy. The
receipt is read through :func:`transaction_kernel.receipts.policy_for_order`, which has no
parameter, flag or fallback that reaches a live policy table. A merchant who tightened
their refund rule yesterday cannot retroactively narrow a sale made last week, and the
only way to keep that promise is to make the current rule unreachable from here.

That function also refuses to hand back terms when the checkout/receipt binding does not
re-derive from stored rows. A broken binding therefore produces ``POLICY_EXCEPTION`` and
no plan rather than a remedy governed by a document that may have been edited: a refund
computed under a forged receipt is worse than no answer at all.

What "issues no plan" means here
--------------------------------
:class:`Resolution` always comes back, because a caller always deserves a code. A plan is
a different thing: ``plan_id`` is ``None`` and ``options`` is empty unless the code is
``RESOLUTION_PLAN_ISSUED``. Specification 6.4.2 is explicit that an unknown payment or
refund state issues no plan, and that no option satisfying policy issues no plan, so those
answers carry ``PAYMENT_UNKNOWN``, ``REFUND_REVIEW_REQUIRED``, ``HUMAN_REVIEW_REQUIRED``
or ``POLICY_EXCEPTION`` and nothing that could be mistaken for a remedy.

``withheld`` is the other half of the answer, and the human-review case needs it: the
specification asks each case to carry "the options the Resolution Service could and could
not offer". An empty options list with no reasons would tell a reviewer that nothing was
considered.

No ``resolution_plans`` row exists yet
--------------------------------------
The specification's plan is an immutable row issued through the kernel-internal
``resolution.plan_issue``, and that table is not in the schema today. So :attr:`Resolution.
recorded` is ``False`` and says so on the wire. The consequences are stated rather than
papered over:

* ``plan_id`` is **derived**, not assigned -- a canonical hash over every figure the plan
  rests on. Two evaluations of unchanged state produce the same id; a settled refund, a
  new capture or a different receipt produces a different one. That is what immutability
  can mean without a row: a plan id names one state of the world, and an id that still
  matches is a plan that has not changed underneath its holder;
* nothing can be *confirmed against* this id. The kernel's admission scope has no plan
  today, so no ``refund.confirm`` can name one, and P0 applies no plan automatically. A
  remedy still travels the ordinary path: a buyer-confirmed refund through
  ``admit_refund``, or the operator path for anything else;
* :attr:`Resolution.valid_until` is honest about the same gap. It is the TTL after which
  this evaluation must be re-run, enforced by re-evaluating rather than by a stored
  expiry, because an expired plan is re-evaluated and never reused.

The invariants, enforced in code rather than described
------------------------------------------------------
:class:`Resolution` refuses to be constructed when an option would return more than
``captured - refunds already issued or pending``, and when store credit is offered without
cash beside it. Both are specification 6.4.2 invariants, and a guard in ``__post_init__``
is worth more than a paragraph: the second one is vacuous today because no demo receipt
records a store-credit programme, and the day one does the pairing rule will already hold.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from commerce_domain import Money, canonical_hash
from platform_db import CheckoutVersion
from sqlalchemy import select
from sqlalchemy.orm import Session
from transaction_kernel import CheckoutRef, RecoveryCode
from transaction_kernel.receipts import (
    PolicyKind,
    ReceiptError,
    ResolvedPolicy,
    database_now_ms,
    policy_for_order,
)

from .reconciliation_service import Finding, FindingCode, Projection

__all__ = [
    "PLAN_TTL_SECONDS",
    "Confirmation",
    "Outcome",
    "PlanOption",
    "Resolution",
    "ResolutionError",
    "WithheldOption",
    "WithheldReason",
    "evaluate",
]

#: How long one evaluation may be relied on. Short on purpose (specification 6.4.2 asks
#: for a short TTL): every figure in a plan is a function of provider state that a webhook
#: can move in the next second, and a remedy quoted from a stale ledger is how a buyer is
#: promised money that is no longer refundable.
PLAN_TTL_SECONDS: Final[int] = 300

#: Version of the plan-identifier scheme. Part of the hash, so ids computed under two
#: schemes can never collide.
_ID_SCHEME_VERSION: Final[int] = 1


class ResolutionError(Exception):
    """A resolution was constructed that would break one of 6.4.2's invariants.

    Raised, never returned. A plan that offers more than the capture supports is not a
    plan the caller should be able to receive, log and hand to a reviewer; the code that
    built it has a bug and the request must fail rather than commit a number to a screen.
    """


class Outcome(StrEnum):
    """The remedies this service can name. Closed, and each maps to one kernel path."""

    REFUND_FULL = "REFUND_FULL"
    REFUND_PARTIAL = "REFUND_PARTIAL"
    ORDER_CANCEL = "ORDER_CANCEL"
    #: Never produced from a receipt that records no store-credit terms, which is every
    #: receipt this platform issues today. Declared because the cash-pairing invariant
    #: below is enforced against it, so the rule exists before the option does.
    STORE_CREDIT = "STORE_CREDIT"


class Confirmation(StrEnum):
    """Who must confirm before the kernel may admit this remedy."""

    BUYER_APPROVAL = "BUYER_APPROVAL"
    OPERATOR_APPROVAL = "OPERATOR_APPROVAL"


class WithheldReason(StrEnum):
    """Why an outcome was considered and not offered. Closed, never free text."""

    ESCALATED_TO_HUMAN = "ESCALATED_TO_HUMAN"
    PROVIDER_STATE_UNVERIFIED = "PROVIDER_STATE_UNVERIFIED"
    NOT_RECORDED_AT_SALE = "NOT_RECORDED_AT_SALE"
    REQUIRES_A_REQUESTED_AMOUNT = "REQUIRES_A_REQUESTED_AMOUNT"
    NOTHING_REFUNDABLE = "NOTHING_REFUNDABLE"
    POLICY_FORBIDS = "POLICY_FORBIDS"
    MONEY_ALREADY_CAPTURED = "MONEY_ALREADY_CAPTURED"


@dataclass(frozen=True, slots=True)
class PlanOption:
    """One remedy, its exact amount, and the at-sale rule that permits it.

    ``amount`` is integer minor units read from committed columns. The policy citation is
    the receipt's own ``policy_id`` and ``policy_version``, so a reviewer can open the
    frozen document and find the sentence this option rests on.
    """

    outcome: Outcome
    amount: Money
    policy_kind: PolicyKind
    policy_id: str
    policy_version: int
    confirmation: Confirmation
    basis: str


@dataclass(frozen=True, slots=True)
class WithheldOption:
    """One remedy that was considered and not offered, with the reason it was not."""

    outcome: Outcome
    reason: WithheldReason
    detail: str


@dataclass(frozen=True, slots=True)
class Resolution:
    """The answer for one finding: a code always, a plan only when one was issued."""

    finding_id: str
    code: RecoveryCode
    plan_id: str | None
    options: tuple[PlanOption, ...]
    withheld: tuple[WithheldOption, ...]
    payment_attempt_id: uuid.UUID
    checkout_id: uuid.UUID
    refund_id: uuid.UUID | None
    policy_receipt_id: uuid.UUID | None
    policy_receipt_hash: str | None
    policy_binding: str
    captured_minor: int
    refunds_reserved_minor: int
    refundable_minor: int
    currency: str
    evaluated_at: datetime
    valid_until: datetime | None
    explanation: str
    recorded: bool = False

    def __post_init__(self) -> None:
        if self.code is RecoveryCode.RESOLUTION_PLAN_ISSUED:
            if not self.options or self.plan_id is None or self.valid_until is None:
                raise ResolutionError(
                    "RESOLUTION_PLAN_ISSUED must carry a plan id, at least one option and "
                    "an expiry; a code that says a plan exists and a body that has none is "
                    "the one shape a caller cannot recover from"
                )
        elif self.options or self.plan_id is not None:
            raise ResolutionError(
                f"{self.code} issues no plan (specification 6.4.2), so it must carry no "
                "options and no plan id"
            )

        for option in self.options:
            if option.amount.currency != self.currency:
                raise ResolutionError(
                    f"option {option.outcome} is in {option.amount.currency} but the "
                    f"capture is in {self.currency}"
                )
            if option.amount.minor > self.refundable_minor:
                raise ResolutionError(
                    f"option {option.outcome} would return {option.amount.minor} of "
                    f"{self.refundable_minor} refundable minor units; total refundable "
                    "never exceeds captured minus refunds already issued or pending"
                )

        outcomes = {option.outcome for option in self.options}
        if Outcome.STORE_CREDIT in outcomes and Outcome.REFUND_FULL not in outcomes:
            raise ResolutionError(
                "store credit is offered without a cash refund beside it; specification "
                "6.4.2 keeps the cash option available whenever store credit is offered"
            )

    @property
    def plan_issued(self) -> bool:
        return self.code is RecoveryCode.RESOLUTION_PLAN_ISSUED


# --------------------------------------------------------------------- evaluation


def evaluate(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    finding: Finding,
    projection: Projection,
) -> Resolution:
    """Decide what would settle ``finding``, from committed rows and the at-sale receipt.

    Read-only. Runs on the app role inside the caller's read transaction and writes
    nothing: this service proposes, and in P0 nothing in the product applies what it
    proposes. ``projection`` must be the reconciliation projection of the finding's own
    attempt; passing another attempt's would resolve one checkout against another's money,
    so the mismatch is refused rather than tolerated.
    """
    if projection.payment_attempt_id != finding.payment_attempt_id:
        raise ResolutionError(
            f"projection {projection.payment_attempt_id} does not describe finding subject "
            f"{finding.payment_attempt_id}"
        )

    evaluated_at = _now(session)
    captured = projection.amount.minor
    reserved = projection.refund_reserved_minor
    refundable = max(0, captured - reserved)
    policy = _policy(session, tenant_id=tenant_id, projection=projection)

    ledger = _Ledger(
        captured_minor=captured,
        reserved_minor=reserved,
        refundable_minor=refundable,
        currency=projection.amount.currency,
    )

    if finding.code in _NO_REMEDY:
        return _refusal(
            finding,
            projection,
            policy,
            ledger,
            evaluated_at,
            code=_NO_REMEDY[finding.code],
            withheld=_all_withheld(_WITHHOLD_REASON[finding.code], _WITHHOLD_DETAIL[finding.code]),
            explanation=_NO_REMEDY_EXPLANATION[finding.code],
        )

    # Only STALE_CAPTURE reaches here: money moved, the checkout it was captured against
    # can never be fulfilled, and the at-sale refund policy governs what comes back.
    return _stale_capture(finding, projection, policy, ledger, evaluated_at, tenant_id=tenant_id)


@dataclass(frozen=True, slots=True)
class _Ledger:
    """The three integers every option is measured against, read from committed columns."""

    captured_minor: int
    reserved_minor: int
    refundable_minor: int
    currency: str


#: Findings that get a code and no plan, with the code each one gets. Specification 6.4.2:
#: an unverified provider outcome issues nothing, and an escalated case has intentionally
#: stopped the automated path.
_NO_REMEDY: Final[Mapping[FindingCode, RecoveryCode]] = {
    FindingCode.PAYMENT_UNKNOWN: RecoveryCode.PAYMENT_UNKNOWN,
    FindingCode.REFUND_UNKNOWN: RecoveryCode.REFUND_REVIEW_REQUIRED,
    FindingCode.PROVIDER_REFUND_UNRECORDED: RecoveryCode.REFUND_REVIEW_REQUIRED,
    FindingCode.ESCALATED: RecoveryCode.HUMAN_REVIEW_REQUIRED,
}

_WITHHOLD_REASON: Final[Mapping[FindingCode, WithheldReason]] = {
    FindingCode.PAYMENT_UNKNOWN: WithheldReason.PROVIDER_STATE_UNVERIFIED,
    FindingCode.REFUND_UNKNOWN: WithheldReason.PROVIDER_STATE_UNVERIFIED,
    FindingCode.PROVIDER_REFUND_UNRECORDED: WithheldReason.PROVIDER_STATE_UNVERIFIED,
    FindingCode.ESCALATED: WithheldReason.ESCALATED_TO_HUMAN,
}

_WITHHOLD_DETAIL: Final[Mapping[FindingCode, str]] = {
    FindingCode.PAYMENT_UNKNOWN: (
        "The provider has not established what happened to this payment, so no remedy "
        "can be priced against it. Reconciliation owns the attempt until it does."
    ),
    FindingCode.REFUND_UNKNOWN: (
        "This refund may already exist at the provider. Until a fetch proves it absent, "
        "no replacement refund may be issued and no amount may be quoted."
    ),
    FindingCode.PROVIDER_REFUND_UNRECORDED: (
        "The provider reports refunds this platform did not record. Until the ledgers "
        "agree, any figure quoted here could be money that has already gone back."
    ),
    FindingCode.ESCALATED: (
        "The kernel froze this subject and opened a human-review case. The automated "
        "path stops here by design; a person decides what happens next."
    ),
}

_NO_REMEDY_EXPLANATION: Final[Mapping[FindingCode, str]] = {
    FindingCode.PAYMENT_UNKNOWN: (
        "No plan is issued while the payment outcome is unverified. An unknown outcome is "
        "reconciled, never retried and never resolved from a guess."
    ),
    FindingCode.REFUND_UNKNOWN: (
        "No plan is issued while the refund outcome is unverified. A refund that may "
        "already have landed is reconciled before anything else is offered."
    ),
    FindingCode.PROVIDER_REFUND_UNRECORDED: (
        "No plan is issued while the provider's refunded total exceeds this platform's "
        "record of it. The difference is reconciled before a remedy is priced."
    ),
    FindingCode.ESCALATED: (
        "No plan is issued for an escalated case. Human review is an input to the kernel, "
        "never a bypass, and P0 records no reviewer decision."
    ),
}


def _all_withheld(reason: WithheldReason, detail: str) -> tuple[WithheldOption, ...]:
    """Every outcome, withheld for the same reason. Used where the finding blocks them all."""
    return tuple(
        WithheldOption(outcome=outcome, reason=reason, detail=detail) for outcome in Outcome
    )


def _refusal(
    finding: Finding,
    projection: Projection,
    policy: ResolvedPolicy,
    ledger: _Ledger,
    evaluated_at: datetime,
    *,
    code: RecoveryCode,
    withheld: tuple[WithheldOption, ...],
    explanation: str,
) -> Resolution:
    return Resolution(
        finding_id=finding.finding_id,
        code=code,
        plan_id=None,
        options=(),
        withheld=withheld,
        payment_attempt_id=projection.payment_attempt_id,
        checkout_id=projection.checkout_id,
        refund_id=finding.refund_id,
        policy_receipt_id=policy.receipt_id,
        policy_receipt_hash=policy.receipt_hash,
        policy_binding=str(policy.reason),
        captured_minor=ledger.captured_minor,
        refunds_reserved_minor=ledger.reserved_minor,
        refundable_minor=ledger.refundable_minor,
        currency=ledger.currency,
        evaluated_at=evaluated_at,
        valid_until=None,
        explanation=explanation,
    )


def _stale_capture(
    finding: Finding,
    projection: Projection,
    policy: ResolvedPolicy,
    ledger: _Ledger,
    evaluated_at: datetime,
    *,
    tenant_id: uuid.UUID,
) -> Resolution:
    """The remedy for money captured against a checkout that can never be fulfilled.

    The confirmation level is the operator's, not the buyer's, and that is deliberate: no
    buyer asked for this. The platform found the divergence itself, so the person who
    confirms it is the one holding the operator credential -- and in P0 nobody confirms it
    here at all, because this surface records no decision.
    """
    if policy.content is None:
        return _refusal(
            finding,
            projection,
            policy,
            ledger,
            evaluated_at,
            code=RecoveryCode.POLICY_EXCEPTION,
            withheld=_all_withheld(
                WithheldReason.NOT_RECORDED_AT_SALE,
                "The Policy-at-Sale Receipt for this sale did not re-derive from stored "
                f"rows ({policy.reason}), so no rule can be cited for any remedy.",
            ),
            explanation=(
                "No plan is issued: the at-sale policy binding does not verify, and a "
                "remedy governed by a document that may have been edited is worse than "
                "no remedy."
            ),
        )

    terms = _terms(policy, PolicyKind.REFUND)
    citation = _citation(policy, PolicyKind.REFUND)
    allowed = terms.get("allowed") is True
    partial_allowed = terms.get("partial_allowed") is True
    store_credit_recorded = "store_credit" in terms

    withheld: list[WithheldOption] = [
        WithheldOption(
            outcome=Outcome.ORDER_CANCEL,
            reason=WithheldReason.MONEY_ALREADY_CAPTURED,
            detail=(
                "The capture already happened, so cancelling the order returns nothing. "
                "What is owed is money, and only a refund moves it."
            ),
        ),
        WithheldOption(
            outcome=Outcome.STORE_CREDIT,
            reason=(
                WithheldReason.NOT_RECORDED_AT_SALE
                if not store_credit_recorded
                else WithheldReason.POLICY_FORBIDS
            ),
            detail=(
                "The Policy-at-Sale Receipt records no store-credit terms for this sale, "
                "so none may be offered against it."
            ),
        ),
        WithheldOption(
            outcome=Outcome.REFUND_PARTIAL,
            reason=(
                WithheldReason.REQUIRES_A_REQUESTED_AMOUNT
                if partial_allowed
                else WithheldReason.POLICY_FORBIDS
            ),
            detail=(
                "A partial refund needs a requested amount before an exact figure can be "
                "quoted, and this remedy came from a finding rather than from a request."
                if partial_allowed
                else "The at-sale refund policy does not permit a partial refund."
            ),
        ),
    ]

    if not allowed:
        withheld.insert(
            0,
            WithheldOption(
                outcome=Outcome.REFUND_FULL,
                reason=WithheldReason.POLICY_FORBIDS,
                detail=f"The at-sale refund policy {citation[0]} v{citation[1]} forbids a refund.",
            ),
        )
        return _refusal(
            finding,
            projection,
            policy,
            ledger,
            evaluated_at,
            code=RecoveryCode.POLICY_EXCEPTION,
            withheld=tuple(withheld),
            explanation=(
                "No option satisfies the policy this sale was made under, so no plan is "
                "issued and a human-review case is the remaining path."
            ),
        )

    if ledger.refundable_minor <= 0:
        withheld.insert(
            0,
            WithheldOption(
                outcome=Outcome.REFUND_FULL,
                reason=WithheldReason.NOTHING_REFUNDABLE,
                detail=(
                    f"{ledger.captured_minor} minor units were captured and "
                    f"{ledger.reserved_minor} are already refunded or reserved against "
                    "refunds that may exist at the provider."
                ),
            ),
        )
        return _refusal(
            finding,
            projection,
            policy,
            ledger,
            evaluated_at,
            code=RecoveryCode.POLICY_EXCEPTION,
            withheld=tuple(withheld),
            explanation=(
                "No plan is issued: nothing is left to refund once every refund that may "
                "exist at the provider is counted."
            ),
        )

    option = PlanOption(
        outcome=Outcome.REFUND_FULL,
        amount=Money(ledger.refundable_minor, ledger.currency),
        policy_kind=PolicyKind.REFUND,
        policy_id=citation[0],
        policy_version=citation[1],
        confirmation=Confirmation.OPERATOR_APPROVAL,
        basis=(
            f"{ledger.captured_minor} captured minus {ledger.reserved_minor} already "
            "refunded or reserved, both read from the ledger the kernel wrote."
        ),
    )
    options = (option,)
    valid_until = evaluated_at + timedelta(seconds=PLAN_TTL_SECONDS)
    return Resolution(
        finding_id=finding.finding_id,
        code=RecoveryCode.RESOLUTION_PLAN_ISSUED,
        plan_id=_plan_id(
            tenant_id=tenant_id,
            finding=finding,
            policy=policy,
            ledger=ledger,
            options=options,
        ),
        options=options,
        withheld=tuple(withheld),
        payment_attempt_id=projection.payment_attempt_id,
        checkout_id=projection.checkout_id,
        refund_id=finding.refund_id,
        policy_receipt_id=policy.receipt_id,
        policy_receipt_hash=policy.receipt_hash,
        policy_binding=str(policy.reason),
        captured_minor=ledger.captured_minor,
        refunds_reserved_minor=ledger.reserved_minor,
        refundable_minor=ledger.refundable_minor,
        currency=ledger.currency,
        evaluated_at=evaluated_at,
        valid_until=valid_until,
        explanation=(
            "The capture landed on a checkout version that was invalidated, so it is owed "
            "back in full under the refund rule frozen at sale. Nothing here executes it."
        ),
    )


# ------------------------------------------------------------------- the receipt


def _policy(session: Session, *, tenant_id: uuid.UUID, projection: Projection) -> ResolvedPolicy:
    """Resolve the at-sale policy for the finding's checkout version.

    The content hash comes from ``checkout_versions``, which is also what
    :func:`policy_for_order` re-derives the binding against; supplying it from anywhere
    else would let this call agree with itself instead of with the record.
    """
    content_hash = session.execute(
        select(CheckoutVersion.content_hash).where(
            CheckoutVersion.tenant_id == tenant_id,
            CheckoutVersion.checkout_id == projection.checkout_id,
            CheckoutVersion.version == projection.checkout_version,
        )
    ).scalar_one_or_none()
    if content_hash is None:
        # policy_for_order answers CHECKOUT_VERSION_MISSING for exactly this, so the empty
        # hash reaches it and comes back as the verdict rather than as an exception here.
        content_hash = ""
    return policy_for_order(
        session,
        CheckoutRef(projection.checkout_id, projection.checkout_version, str(content_hash)),
    )


def _terms(policy: ResolvedPolicy, kind: PolicyKind) -> Mapping[str, Any]:
    """The at-sale terms of one kind, or an empty mapping when the receipt has none.

    A receipt is required to record every :class:`PolicyKind`, so a missing one means the
    document is not what ``issue_receipt`` writes. Returning empty terms lets the caller
    treat it as "not permitted at sale", which is the safe reading: an absent rule has
    never authorised anything.
    """
    try:
        return policy.terms_for(kind)
    except ReceiptError:
        return {}


def _citation(policy: ResolvedPolicy, kind: PolicyKind) -> tuple[str, int]:
    """The receipt's own identifier and version for one policy kind."""
    try:
        recorded = policy.policy_for(kind)
    except ReceiptError:
        return ("", 0)
    version = recorded.get("policy_version")
    return (
        str(recorded.get("policy_id", "")),
        version if isinstance(version, int) and not isinstance(version, bool) else 0,
    )


def _plan_id(
    *,
    tenant_id: uuid.UUID,
    finding: Finding,
    policy: ResolvedPolicy,
    ledger: _Ledger,
    options: Sequence[PlanOption],
) -> str:
    """A plan identifier derived from everything the plan rests on.

    Covers the ledger figures and the receipt hash as well as the subject, so an id that
    still matches is a plan whose inputs have not moved. That is the strongest form of
    immutability available without a row to write.
    """
    return canonical_hash(
        {
            "v": _ID_SCHEME_VERSION,
            "tenant_id": str(tenant_id),
            "finding_id": finding.finding_id,
            "payment_attempt_id": str(finding.payment_attempt_id),
            "policy_receipt_hash": policy.receipt_hash,
            "captured_minor": ledger.captured_minor,
            "reserved_minor": ledger.reserved_minor,
            "refundable_minor": ledger.refundable_minor,
            "currency": ledger.currency,
            "options": [
                {
                    "outcome": option.outcome.value,
                    "amount_minor": option.amount.minor,
                    "policy_id": option.policy_id,
                    "policy_version": option.policy_version,
                }
                for option in options
            ],
        }
    )


def _now(session: Session) -> datetime:
    """The database transaction clock, never a local one.

    Two evaluations inside one transaction therefore agree, and a pod with a skewed clock
    cannot shorten or extend a plan's life.
    """
    stamp = database_now_ms(session)
    return datetime.fromtimestamp(stamp // 1000, tz=UTC) + timedelta(milliseconds=stamp % 1000)
