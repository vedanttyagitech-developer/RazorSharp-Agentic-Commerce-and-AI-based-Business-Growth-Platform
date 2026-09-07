"""What the provider said, against what the platform recorded, and the difference.

Specification 6.4.1. This module is deterministic code and explicitly not an agent
(``docs/briefs/AGENT_ROSTER.md``): no model, no prompt, no judgement. Every value it
returns is read from a committed row, and every classification below is a total function
of those rows.

Where the provider calls actually happen
----------------------------------------
They happen in the Action Executor, not here. ``action_executor.handlers.reconcile`` holds
the lease, queries Razorpay by authoritative identifier, and hands what comes back to the
kernel, which joins it monotonically and writes one ``reconciliation_runs`` row per
attempt. This module reads that record. So "what the provider says" means, precisely,
*the provider's last statement as verified evidence recorded it* -- the
``payment.evidence_applied`` audit row, the ``provider_requests`` rows and the
``reconciliation_runs`` rounds -- and never a live call made from an HTTP request. An API
read that could reach out to Razorpay would put an external dependency on the money path's
read model and would let a page refresh count as reconciliation.

**This module writes nothing.** It runs on the app role, which physically cannot write a
financial table, and where it finds a divergence it produces a :class:`Finding` -- a
value, not a row and not a transition. Only the kernel changes state, and only from
evidence the worker fetched.

The projection
--------------
:class:`Projection` is specification 6.4.1's read-only projection: the state the platform
recorded, the state verified evidence established, the evidence source, the last run,
attempts used, attempts remaining and the next scheduled attempt. Two states are reported
rather than one because they answer different questions. ``recorded_state`` is what the
``payment_attempts`` row says; ``verified`` is what the provider's own answer established,
and it is *absent* until a webhook or a fetch has been applied. A surface that showed one
number would have to choose, and either choice reads as a claim the platform cannot back:
"unknown" hides a capture that a webhook already proved, and "captured" invents a
settlement the provider never confirmed.

The findings
------------
A finding is a divergence between the two, or an outcome that is not established at all.
``PAYMENT_UNKNOWN`` and ``REFUND_UNKNOWN`` are the subject matter; the other three are the
divergences the recorded rows can actually prove:

``STALE_CAPTURE``
    Money was captured against a checkout version that was invalidated, so there is no
    ``orders`` row and the capture is owed back (specification 10.8). The provider says
    paid and the platform says there is nothing to fulfil; both are correct, and the
    difference is the finding.

``PROVIDER_REFUND_UNRECORDED``
    The provider's own refunded total exceeds every refund this platform has recorded,
    counting each one that may exist at the provider. Money went back that nothing here
    initiated -- a dashboard refund, or a capture-window auto-refund.

    Only this direction is reported. The provider's total is a *snapshot* taken when the
    last evidence was applied, so a refund admitted after that moment leaves the local
    ledger legitimately ahead of it, and reporting "the platform recorded more than the
    provider shows" would raise a finding on every refund still in flight. The reverse
    comparison cannot be stale in that way: a snapshot can only lag, so a provider total
    *above* the local ledger is a real gap however old the snapshot is.

``ESCALATED``
    The kernel froze the attempt or the refund and opened a human-review case, because a
    bounded number of rounds did not resolve it or because the evidence contradicted the
    record. ``reason_family`` carries which detector said so, read from the case's own
    audit row rather than guessed from the state.

Findings are identified by :func:`finding_id`, a hash of tenant, subject and code. Nothing
is stored, so the identifier has to be derivable: two reads of unchanged rows produce the
same id, and a subject whose classification changes produces a different one rather than
silently reusing the old identity.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

import transaction_kernel as tk
from commerce_domain import Money, canonical_hash
from platform_db import AuditEvent, Order, PaymentAttempt, ReconciliationRun, Refund
from sqlalchemy import select
from sqlalchemy.orm import Session
from transaction_kernel.payments import RECONCILIATION_ATTEMPT_BOUND
from transaction_kernel.refunds import RESERVING_STATUSES, RefundStatus

__all__ = [
    "ATTEMPT_BOUND",
    "CASE_OPENED_EVENT",
    "EVIDENCE_APPLIED_EVENT",
    "EVIDENCE_MISMATCH_EVENT",
    "UNRESOLVED_PAYMENT_STATES",
    "UNRESOLVED_REFUND_STATES",
    "Finding",
    "FindingCode",
    "Projection",
    "RefundView",
    "RunView",
    "Subject",
    "VerifiedState",
    "finding_id",
    "project",
    "project_many",
    "survey",
]

#: ADR 0003 D13's bound, re-exported so a surface reporting "attempts remaining" and the
#: worker enforcing the bound cannot disagree about the number.
ATTEMPT_BOUND: Final[int] = RECONCILIATION_ATTEMPT_BOUND

#: Audit event types this module reads. Named constants because each is also matched in
#: SQL, and a typo in a string literal would silently produce "no evidence recorded"
#: instead of an error.
EVIDENCE_APPLIED_EVENT: Final[str] = "payment.evidence_applied"
EVIDENCE_MISMATCH_EVENT: Final[str] = "evidence.mismatch"
CASE_OPENED_EVENT: Final[str] = "human_review.opened"

#: Attempt states in which the provider's answer is not established. Built from the
#: kernel's own ``UNCERTAIN_PAYMENT_STATES`` rather than restated, plus ``RECONCILING``:
#: an attempt being looked at is still an attempt nobody can answer for.
UNRESOLVED_PAYMENT_STATES: Final[frozenset[tk.PaymentState]] = tk.UNCERTAIN_PAYMENT_STATES | {
    tk.PaymentState.RECONCILING
}

#: The same posture on a refund row.
UNRESOLVED_REFUND_STATES: Final[frozenset[RefundStatus]] = frozenset(
    {RefundStatus.UNKNOWN, RefundStatus.RECONCILING}
)

#: How many attempts one survey returns by default.
DEFAULT_SURVEY_LIMIT: Final[int] = 50
MAX_SURVEY_LIMIT: Final[int] = 200

#: Version of the finding-identifier scheme. Bumping it changes every id, which is the
#: point: an id computed under two different schemes must never collide.
_ID_SCHEME_VERSION: Final[int] = 1


class Subject(StrEnum):
    """What a finding is about. A refund is its own subject, not a facet of the payment."""

    PAYMENT_ATTEMPT = "PAYMENT_ATTEMPT"
    REFUND = "REFUND"


class FindingCode(StrEnum):
    """The divergences this service reports. Closed, and each derivable from rows."""

    PAYMENT_UNKNOWN = "PAYMENT_UNKNOWN"
    REFUND_UNKNOWN = "REFUND_UNKNOWN"
    STALE_CAPTURE = "STALE_CAPTURE"
    PROVIDER_REFUND_UNRECORDED = "PROVIDER_REFUND_UNRECORDED"
    ESCALATED = "ESCALATED"


# ------------------------------------------------------------------------- values


@dataclass(frozen=True, slots=True)
class VerifiedState:
    """The provider's last statement about one attempt, as verified evidence recorded it.

    Every field is optional and :attr:`present` is false when nothing has been applied
    yet. That is not defensiveness: an attempt whose create-order response was lost has no
    provider statement at all, and rendering a default -- "0 refunded", "status pending" --
    would put a figure on screen that no provider ever said.
    """

    source: str | None = None
    status: str | None = None
    provider_status: str | None = None
    provider_payment_id: str | None = None
    provider_order_id: str | None = None
    amount_minor: int | None = None
    currency: str | None = None
    amount_refunded_minor: int | None = None
    observed_at: datetime | None = None
    audit_event_id: uuid.UUID | None = None

    @property
    def present(self) -> bool:
        """True when a webhook or a provider fetch has been applied to this attempt."""
        return self.source is not None


@dataclass(frozen=True, slots=True)
class RunView:
    """One ``reconciliation_runs`` row: what the round queried by, and what it decided."""

    attempt_number: int
    reason: str
    decision: str
    resulting_transition: str | None
    identifiers_queried: Mapping[str, Any]
    next_scheduled_attempt: datetime | None
    correlation_id: uuid.UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RefundView:
    """One refund against an attempt, with its own reconciliation budget.

    A refund's rounds are counted apart from the payment's because the worker enqueues
    them separately (``reconciliation_runs.refund_id``); merging the two would tell a
    reviewer that a payment which reconciled cleanly had used up its attempts.
    """

    refund_id: uuid.UUID
    status: RefundStatus
    amount: Money
    reason_code: str
    provider_refund_id: str | None
    provider_originated: bool
    created_at: datetime
    attempts_used: int
    next_scheduled_attempt: datetime | None
    reason_family: str | None

    @property
    def attempts_remaining(self) -> int:
        return max(0, ATTEMPT_BOUND - self.attempts_used)

    @property
    def reserves_money(self) -> bool:
        """True while this refund may exist at the provider (the kernel's ledger rule)."""
        return self.status in RESERVING_STATUSES


@dataclass(frozen=True, slots=True)
class Finding:
    """One divergence, as data. Never a row, never a transition, never an instruction.

    ``exposure`` is the money at risk, copied from committed integer columns. Nothing here
    is estimated and nothing is rounded: the only arithmetic is subtracting two integers
    the database already held.
    """

    finding_id: str
    code: FindingCode
    subject: Subject
    payment_attempt_id: uuid.UUID
    checkout_id: uuid.UUID
    checkout_version: int
    refund_id: uuid.UUID | None
    recorded_state: str
    provider_state: str | None
    reason_family: str | None
    exposure: Money
    detected_at: datetime
    correlation_id: uuid.UUID | None
    detail: str


@dataclass(frozen=True, slots=True)
class Projection:
    """Specification 6.4.1's read-only projection for one payment attempt.

    Carries the attempt, its refunds, its reconciliation rounds, the provider's last
    verified statement and the findings derived from all of it, so a caller that needs the
    whole picture makes one call rather than five that could observe five different
    moments.
    """

    payment_attempt_id: uuid.UUID
    checkout_id: uuid.UUID
    checkout_version: int
    recorded_state: tk.PaymentState
    amount: Money
    receipt: str
    provider_order_id: str | None
    provider_payment_id: str | None
    order_id: uuid.UUID | None
    verified: VerifiedState
    refused_evidence: Mapping[str, Any] | None
    reason_family: str | None
    attempts_used: int
    last_run_at: datetime | None
    next_scheduled_attempt: datetime | None
    runs: tuple[RunView, ...]
    refunds: tuple[RefundView, ...]
    findings: tuple[Finding, ...]
    created_at: datetime
    updated_at: datetime

    @property
    def attempts_remaining(self) -> int:
        return max(0, ATTEMPT_BOUND - self.attempts_used)

    @property
    def refund_reserved_minor(self) -> int:
        """Every refund that may exist at the provider, in minor units.

        The kernel's ledger rule, applied through its own ``RESERVING_STATUSES`` rather
        than restated here: ``PENDING`` is reserved money, ``UNKNOWN``, ``RECONCILING``
        and ``ESCALATED`` may already have landed, ``PROCESSED`` has, and only a
        provider-confirmed ``FAILED`` releases its amount.
        """
        return sum(refund.amount.minor for refund in self.refunds if refund.reserves_money)

    @property
    def refund_settled_minor(self) -> int:
        """The subset the provider confirmed."""
        return sum(
            refund.amount.minor
            for refund in self.refunds
            if refund.status is RefundStatus.PROCESSED
        )


# ------------------------------------------------------------------- identifiers


def finding_id(
    *, tenant_id: uuid.UUID, subject: Subject, subject_id: uuid.UUID, code: FindingCode
) -> str:
    """The stable identifier of one finding.

    Nothing stores a finding, so the identifier is derived rather than assigned. Keyed on
    the subject and the classification and nothing else: an id that also covered the
    exposure would change every time a partial refund settled, and a caller holding it
    could no longer ask about the same finding twice.
    """
    return canonical_hash(
        {
            "v": _ID_SCHEME_VERSION,
            "tenant_id": str(tenant_id),
            "subject": subject.value,
            "subject_id": str(subject_id),
            "code": code.value,
        }
    )


# ------------------------------------------------------------------------- reads


def survey(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    checkout_id: uuid.UUID | None = None,
    unresolved_only: bool = False,
    limit: int = DEFAULT_SURVEY_LIMIT,
) -> tuple[Projection, ...]:
    """Project every payment attempt in this tenant, newest first.

    ``unresolved_only`` keeps the attempts that produced at least one finding, which is
    the reconciliation queue an operator actually reads. The filter is applied after
    projection rather than in SQL because a finding is a function of several tables at
    once, and a WHERE clause that approximated it would drift from the classification the
    detail view shows.

    Runs on the app role inside the caller's read transaction and writes nothing.
    """
    bounded = max(1, min(limit, MAX_SURVEY_LIMIT))
    query = select(PaymentAttempt).where(PaymentAttempt.tenant_id == tenant_id)
    if checkout_id is not None:
        query = query.where(PaymentAttempt.checkout_id == checkout_id)
    rows = (
        session.execute(
            query.order_by(PaymentAttempt.created_at.desc(), PaymentAttempt.id.desc()).limit(
                bounded
            )
        )
        .scalars()
        .all()
    )
    projections = _project_all(session, tenant_id=tenant_id, attempts=rows)
    if unresolved_only:
        return tuple(item for item in projections if item.findings)
    return projections


def project(
    session: Session, *, tenant_id: uuid.UUID, payment_attempt_id: uuid.UUID
) -> Projection | None:
    """Project one attempt, or ``None`` when this tenant cannot see it."""
    row = session.execute(
        select(PaymentAttempt).where(
            PaymentAttempt.tenant_id == tenant_id, PaymentAttempt.id == payment_attempt_id
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    projections = _project_all(session, tenant_id=tenant_id, attempts=[row])
    return projections[0]


def project_many(
    session: Session, *, tenant_id: uuid.UUID, payment_attempt_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, Projection]:
    """Project several named attempts in one pass, keyed by attempt id.

    The human-review queue is the caller: a page of cases names a handful of attempts and
    needs all of their projections at once, and asking for them one at a time would read
    each supporting table once per case.
    """
    if not payment_attempt_ids:
        return {}
    rows = (
        session.execute(
            select(PaymentAttempt).where(
                PaymentAttempt.tenant_id == tenant_id,
                PaymentAttempt.id.in_(list(payment_attempt_ids)),
            )
        )
        .scalars()
        .all()
    )
    return {
        item.payment_attempt_id: item
        for item in _project_all(session, tenant_id=tenant_id, attempts=rows)
    }


def _project_all(
    session: Session, *, tenant_id: uuid.UUID, attempts: Sequence[PaymentAttempt]
) -> tuple[Projection, ...]:
    """Build every projection from four batched reads rather than four reads per attempt.

    Batched because the survey is the operator's first screen and an N+1 across
    ``refunds``, ``reconciliation_runs``, ``orders`` and ``audit_events`` would make its
    cost a function of how much trouble the tenant is in -- slowest exactly when somebody
    is looking because something is wrong.
    """
    if not attempts:
        return ()
    attempt_ids = [attempt.id for attempt in attempts]
    checkout_ids = {attempt.checkout_id for attempt in attempts}

    refunds = _refunds_by_attempt(session, tenant_id, attempt_ids)
    runs = _runs_by_attempt(session, tenant_id, attempt_ids)
    orders = _orders_by_attempt(session, tenant_id, attempt_ids)
    evidence = _latest_evidence(session, tenant_id, checkout_ids)
    mismatches = _latest_mismatch(session, tenant_id, checkout_ids)
    families = _reason_families(session, tenant_id, checkout_ids, attempt_ids)

    return tuple(
        _project_one(
            attempt,
            tenant_id=tenant_id,
            refund_rows=refunds.get(attempt.id, ()),
            run_rows=runs.get(attempt.id, ()),
            order_id=orders.get(attempt.id),
            # Keyed by provider order id, not by checkout. A checkout that failed once and
            # was retried has two attempts on one stream, and handing the newer attempt's
            # capture to the older one would show a reviewer a settlement that belongs to
            # a different Execution Grant. An attempt with no provider order has no
            # evidence by construction: the kernel refuses to apply any without one.
            verified=evidence.get(attempt.provider_order_id or "", VerifiedState()),
            refused=mismatches.get(attempt.provider_order_id or ""),
            families=families,
        )
        for attempt in attempts
    )


def _project_one(
    attempt: PaymentAttempt,
    *,
    tenant_id: uuid.UUID,
    refund_rows: Sequence[Refund],
    run_rows: Sequence[ReconciliationRun],
    order_id: uuid.UUID | None,
    verified: VerifiedState,
    refused: Mapping[str, Any] | None,
    families: Mapping[str, str],
) -> Projection:
    payment_runs = [row for row in run_rows if row.refund_id is None]
    refunds = tuple(
        _refund_view(
            row,
            runs=[run for run in run_rows if run.refund_id == row.id],
            families=families,
        )
        for row in refund_rows
    )
    recorded = tk.PaymentState(attempt.status)
    amount = Money(int(attempt.amount_minor), str(attempt.currency))
    latest_run = payment_runs[-1] if payment_runs else None

    projection = Projection(
        payment_attempt_id=attempt.id,
        checkout_id=attempt.checkout_id,
        checkout_version=attempt.checkout_version,
        recorded_state=recorded,
        amount=amount,
        receipt=attempt.receipt,
        provider_order_id=attempt.provider_order_id,
        provider_payment_id=attempt.provider_payment_id,
        order_id=order_id,
        verified=verified,
        refused_evidence=refused,
        # The payment path's case is appended to the checkout's stream and names neither
        # the attempt nor the checkout in its payload, so the provider order id -- unique
        # per tenant -- is what ties it back to this row. The checkout is the last resort
        # and is consulted only for an attempt the kernel actually froze: on any other
        # attempt it would attach a sibling attempt's escalation to a healthy row.
        reason_family=_family_for(
            families,
            str(attempt.id),
            attempt.provider_order_id,
            str(attempt.checkout_id) if recorded is tk.PaymentState.ESCALATED else None,
        ),
        attempts_used=len(payment_runs),
        last_run_at=None if latest_run is None else latest_run.created_at,
        next_scheduled_attempt=None if latest_run is None else latest_run.next_scheduled_attempt,
        runs=tuple(_run_view(row) for row in payment_runs),
        refunds=refunds,
        findings=(),
        created_at=attempt.created_at,
        updated_at=attempt.updated_at,
    )
    # The findings are a function of the finished projection, so the projection is built
    # once without them and replaced with the same values plus what they imply. Computing
    # them from loose locals instead would let the two views of one attempt disagree.
    return _with_findings(projection, tenant_id=tenant_id)


def _run_view(row: ReconciliationRun) -> RunView:
    return RunView(
        attempt_number=int(row.attempt_number),
        reason=str(row.reason),
        decision=str(row.decision),
        resulting_transition=row.resulting_transition,
        identifiers_queried=dict(row.identifiers_queried or {}),
        next_scheduled_attempt=row.next_scheduled_attempt,
        correlation_id=row.correlation_id,
        created_at=row.created_at,
    )


def _refund_view(
    row: Refund, *, runs: Sequence[ReconciliationRun], families: Mapping[str, str]
) -> RefundView:
    latest = runs[-1] if runs else None
    return RefundView(
        refund_id=row.id,
        status=RefundStatus(row.status),
        amount=Money(int(row.amount_minor), str(row.currency)),
        reason_code=str(row.reason_code),
        provider_refund_id=row.provider_refund_id,
        provider_originated=bool(row.provider_originated),
        created_at=row.created_at,
        attempts_used=len(runs),
        next_scheduled_attempt=None if latest is None else latest.next_scheduled_attempt,
        reason_family=_family_for(families, str(row.id)),
    )


def _family_for(families: Mapping[str, str], *keys: str | None) -> str | None:
    """The first reason family any of these identifiers has a case recorded under."""
    for key in keys:
        if key is not None and key in families:
            return families[key]
    return None


# ---------------------------------------------------------------- classification


def _with_findings(projection: Projection, *, tenant_id: uuid.UUID) -> Projection:
    """Classify one projection. Total, ordered, and derived from the projection alone."""
    findings: list[Finding] = []
    subject_id = projection.payment_attempt_id

    def _payment_finding(code: FindingCode, exposure: Money, detail: str) -> Finding:
        return Finding(
            finding_id=finding_id(
                tenant_id=tenant_id,
                subject=Subject.PAYMENT_ATTEMPT,
                subject_id=subject_id,
                code=code,
            ),
            code=code,
            subject=Subject.PAYMENT_ATTEMPT,
            payment_attempt_id=subject_id,
            checkout_id=projection.checkout_id,
            checkout_version=projection.checkout_version,
            refund_id=None,
            recorded_state=projection.recorded_state.value,
            provider_state=projection.verified.status,
            reason_family=projection.reason_family,
            exposure=exposure,
            detected_at=projection.updated_at,
            correlation_id=(projection.runs[-1].correlation_id if projection.runs else None),
            detail=detail,
        )

    state = projection.recorded_state
    currency = projection.amount.currency

    if state in UNRESOLVED_PAYMENT_STATES:
        findings.append(
            _payment_finding(
                FindingCode.PAYMENT_UNKNOWN,
                projection.amount,
                "The provider's outcome for this attempt is not established. "
                f"{projection.attempts_used} of {ATTEMPT_BOUND} reconciliation rounds have run.",
            )
        )

    if state is tk.PaymentState.STALE_CAPTURE:
        # captured minus what is already reserved against it: the same subtraction the
        # kernel's ledger performs, over integers it wrote.
        owed = projection.amount.minor - projection.refund_reserved_minor
        findings.append(
            _payment_finding(
                FindingCode.STALE_CAPTURE,
                Money(max(0, owed), currency),
                "Money was captured against a checkout version that had been invalidated, "
                "so no order was confirmed and the capture is owed back.",
            )
        )

    if state is tk.PaymentState.ESCALATED:
        findings.append(
            _payment_finding(
                FindingCode.ESCALATED,
                projection.amount,
                "The kernel froze this attempt and opened a human-review case"
                + (f" ({projection.reason_family})." if projection.reason_family else "."),
            )
        )

    unrecorded = _provider_refund_gap(projection)
    if unrecorded > 0:
        findings.append(
            _payment_finding(
                FindingCode.PROVIDER_REFUND_UNRECORDED,
                Money(unrecorded, currency),
                "The provider reports more refunded against this payment than this "
                "platform has recorded, counting every refund that may exist there.",
            )
        )

    for refund in projection.refunds:
        code: FindingCode | None = None
        detail = ""
        if refund.status in UNRESOLVED_REFUND_STATES:
            code = FindingCode.REFUND_UNKNOWN
            detail = (
                "The provider's outcome for this refund is not established, so it may "
                "already have landed. No replacement refund may be issued until it is."
            )
        elif refund.status is RefundStatus.ESCALATED:
            code = FindingCode.ESCALATED
            detail = "The kernel froze this refund and opened a human-review case" + (
                f" ({refund.reason_family})." if refund.reason_family else "."
            )
        if code is None:
            continue
        findings.append(
            Finding(
                finding_id=finding_id(
                    tenant_id=tenant_id,
                    subject=Subject.REFUND,
                    subject_id=refund.refund_id,
                    code=code,
                ),
                code=code,
                subject=Subject.REFUND,
                payment_attempt_id=projection.payment_attempt_id,
                checkout_id=projection.checkout_id,
                checkout_version=projection.checkout_version,
                refund_id=refund.refund_id,
                recorded_state=refund.status.value,
                provider_state=projection.verified.status,
                reason_family=refund.reason_family,
                exposure=refund.amount,
                detected_at=refund.created_at,
                correlation_id=(projection.runs[-1].correlation_id if projection.runs else None),
                detail=detail,
            )
        )

    return replace(projection, findings=tuple(findings))


def _provider_refund_gap(projection: Projection) -> int:
    """How much more the provider says it refunded than this platform has recorded.

    Zero unless a provider statement exists: with no verified evidence there is no
    provider figure to compare against, and treating an absent snapshot as zero would
    report every settled refund as unrecorded. See the module docstring for why only this
    direction is reported.
    """
    reported = projection.verified.amount_refunded_minor
    if reported is None:
        return 0
    return max(0, reported - projection.refund_reserved_minor)


# ---------------------------------------------------------------- batched reads


def _refunds_by_attempt(
    session: Session, tenant_id: uuid.UUID, attempt_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[Refund, ...]]:
    rows = (
        session.execute(
            select(Refund)
            .where(Refund.tenant_id == tenant_id, Refund.payment_attempt_id.in_(attempt_ids))
            .order_by(Refund.created_at, Refund.id)
        )
        .scalars()
        .all()
    )
    grouped: dict[uuid.UUID, list[Refund]] = {}
    for row in rows:
        grouped.setdefault(row.payment_attempt_id, []).append(row)
    return {key: tuple(value) for key, value in grouped.items()}


def _runs_by_attempt(
    session: Session, tenant_id: uuid.UUID, attempt_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[ReconciliationRun, ...]]:
    rows = (
        session.execute(
            select(ReconciliationRun)
            .where(
                ReconciliationRun.tenant_id == tenant_id,
                ReconciliationRun.payment_attempt_id.in_(attempt_ids),
            )
            .order_by(ReconciliationRun.attempt_number, ReconciliationRun.created_at)
        )
        .scalars()
        .all()
    )
    grouped: dict[uuid.UUID, list[ReconciliationRun]] = {}
    for row in rows:
        grouped.setdefault(row.payment_attempt_id, []).append(row)
    return {key: tuple(value) for key, value in grouped.items()}


def _orders_by_attempt(
    session: Session, tenant_id: uuid.UUID, attempt_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    rows = session.execute(
        select(Order.payment_attempt_id, Order.id).where(
            Order.tenant_id == tenant_id, Order.payment_attempt_id.in_(attempt_ids)
        )
    ).all()
    return {row[0]: row[1] for row in rows}


def _latest_evidence(
    session: Session, tenant_id: uuid.UUID, checkout_ids: set[uuid.UUID]
) -> dict[str, VerifiedState]:
    """The last applied provider statement, keyed by the provider order it is about.

    Read from the *checkout* stream, because that is where the kernel appends its payment
    events: a refund lives on the attempt's stream, but evidence about the payment is part
    of the consent story and stays in one order with it.

    Keyed by provider order id rather than by checkout, though, because a checkout can
    accumulate more than one attempt -- a failed create followed by a fresh admission --
    and each attempt's evidence belongs to exactly one of them. The kernel cross-checks
    every evidence record against the attempt's own provider order before applying it, so
    that identifier is the only correct join.
    """
    rows = (
        session.execute(
            select(AuditEvent)
            .where(
                AuditEvent.tenant_id == tenant_id,
                AuditEvent.aggregate_type == "checkout",
                AuditEvent.aggregate_id.in_(checkout_ids),
                AuditEvent.event_type == EVIDENCE_APPLIED_EVENT,
            )
            .order_by(AuditEvent.aggregate_id, AuditEvent.seq)
        )
        .scalars()
        .all()
    )
    latest: dict[str, VerifiedState] = {}
    for row in rows:
        payload = row.payload or {}
        order_id = _string(payload.get("provider_order_id"))
        if order_id is None:
            continue
        latest[order_id] = VerifiedState(
            source=_string(payload.get("source")),
            status=_string(payload.get("status")),
            provider_status=_string(payload.get("provider_status")),
            provider_payment_id=_string(payload.get("provider_payment_id")),
            provider_order_id=_string(payload.get("provider_order_id")),
            amount_minor=_integer(payload.get("amount_minor")),
            currency=_string(payload.get("currency")),
            amount_refunded_minor=_integer(payload.get("amount_refunded_minor")),
            observed_at=row.occurred_at,
            audit_event_id=row.id,
        )
    return latest


def _latest_mismatch(
    session: Session, tenant_id: uuid.UUID, checkout_ids: set[uuid.UUID]
) -> dict[str, Mapping[str, Any]]:
    """The last provider entity the kernel *refused* as evidence, per attempt.

    Kept apart from :class:`VerifiedState` on purpose. A mismatched entity may be perfectly
    valid evidence about some other checkout, and folding it into "the verified provider
    state" would settle this one with another's money -- in a reviewer's head rather than
    in the ledger, but with the same conclusion.

    Keyed on ``expected_provider_order_id``, which the kernel stamps with the *attempt's*
    own order rather than the entity's, for the same reason the applied evidence is: one
    checkout can hold two attempts and this record belongs to exactly one of them.
    """
    rows = (
        session.execute(
            select(AuditEvent)
            .where(
                AuditEvent.tenant_id == tenant_id,
                AuditEvent.aggregate_type == "checkout",
                AuditEvent.aggregate_id.in_(checkout_ids),
                AuditEvent.event_type == EVIDENCE_MISMATCH_EVENT,
            )
            .order_by(AuditEvent.aggregate_id, AuditEvent.seq)
        )
        .scalars()
        .all()
    )
    refused: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        payload = dict(row.payload or {})
        order_id = _string(payload.get("expected_provider_order_id"))
        if order_id is None:
            continue
        refused[order_id] = payload
    return refused


def _reason_families(
    session: Session,
    tenant_id: uuid.UUID,
    checkout_ids: set[uuid.UUID],
    attempt_ids: Sequence[uuid.UUID],
) -> dict[str, str]:
    """Map each escalated subject id to the reason family its case recorded.

    Both streams are read. The payment path opens its case on the checkout stream and the
    refund path on the attempt's, so a lookup that knew only one of them would report a
    frozen refund with no reason at all.
    """
    aggregates = list(checkout_ids) + list(attempt_ids)
    rows = (
        session.execute(
            select(AuditEvent)
            .where(
                AuditEvent.tenant_id == tenant_id,
                AuditEvent.aggregate_id.in_(aggregates),
                AuditEvent.event_type == CASE_OPENED_EVENT,
            )
            .order_by(AuditEvent.occurred_at, AuditEvent.seq)
        )
        .scalars()
        .all()
    )
    families: dict[str, str] = {}
    for row in rows:
        payload = row.payload or {}
        family = _string(payload.get("reason_family"))
        if family is None:
            continue
        # The refund path names its subjects outright; the payment path names only the
        # provider identifiers and the stream it was appended to. Every token a case
        # carries is recorded, and the reader tries them in order of precision.
        for key in ("refund_id", "payment_attempt_id", "provider_order_id"):
            subject = _string(payload.get(key))
            if subject is not None:
                families.setdefault(subject, family)
        families.setdefault(str(row.aggregate_id), family)
    return families


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _integer(value: object) -> int | None:
    # bool is an int in Python and would silently become 0 or 1 in a money column.
    return value if isinstance(value, int) and not isinstance(value, bool) else None
