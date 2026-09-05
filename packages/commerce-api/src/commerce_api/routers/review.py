"""Operator reads over the three deterministic support services.

Specification 6.4. Four routes, all reads, all behind the operator key:

============================================  ==================================================
``GET /v1/review/reconciliation``             What the provider said against what the platform
                                              recorded, per payment attempt, with the findings
``GET /v1/review/reconciliation/{attempt}``   One attempt in full: rounds, refunds, provider
                                              statement, findings and what would settle each
``GET /v1/review/queue``                      The human-review queue
``GET /v1/review/queue/{case_key}``           One case with its evidence
============================================  ==================================================

Guarded the way ``routers/ops.py`` is
-------------------------------------
``X-Scenario-Key`` as a router-level dependency, plus a bearer session on every route. The
key says the caller may operate the apparatus; the session says on whose tenant, because
row-level security scopes every read to the tenant bound to the transaction and the only
thing on this platform that establishes a tenant without being told one is an
``api_sessions`` row. Accepting a tenant from a header would hand anybody holding the demo
key the whole cluster.

This is the ops pattern rather than the evidence one deliberately. In ``routers/evidence.py``
the key *widens* a buyer-facing read, so it is checked non-raising and an absent key falls
through to an ownership test. Nothing here is buyer-facing: a reconciliation finding names
another buyer's stuck payment, and a case carries the money at risk on it. So the key gates
rather than widens, and its absence is a refusal rather than a narrower answer.

**No route here writes.** Every one runs on the app role, which physically cannot write a
financial table, and no route offers an assign, a decision, a note or a resolve. P0's human
review is the queue and its evidence; ``scope`` says so in the response rather than leaving
a reviewer to infer it from an absence of buttons.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from ..deps import AppSession, SessionContext, require_scenario_key
from ..errors import ProblemError
from ..schemas import MoneyOut, rfc3339, uuid_str
from ..services import human_review_service as review
from ..services import reconciliation_service as recon
from ..services import resolution_service as resolve
from ..services import timeline

router = APIRouter(
    prefix="/v1/review",
    tags=["review"],
    dependencies=[Depends(require_scenario_key)],
)

__all__ = ["router"]


# ------------------------------------------------------------------------- wire shapes


class _Out(BaseModel):
    """Strict responses, specification 24.1."""

    model_config = ConfigDict(extra="forbid")


class VerifiedStateOut(_Out):
    """What the provider last said, or ``present: false`` when it has not said anything.

    Every field is nullable and none of them has a default that reads as data. An attempt
    whose create-order response was lost has no provider statement at all, and a zero in
    ``amount_refunded_minor`` would be a figure no provider ever gave.
    """

    present: bool
    source: str | None
    status: str | None
    provider_status: str | None
    provider_payment_id: str | None
    provider_order_id: str | None
    amount_minor: int | None
    currency: str | None
    amount_refunded_minor: int | None
    observed_at: str | None
    audit_event_id: str | None

    @classmethod
    def of(cls, state: recon.VerifiedState) -> VerifiedStateOut:
        return cls(
            present=state.present,
            source=state.source,
            status=state.status,
            provider_status=state.provider_status,
            provider_payment_id=state.provider_payment_id,
            provider_order_id=state.provider_order_id,
            amount_minor=state.amount_minor,
            currency=state.currency,
            amount_refunded_minor=state.amount_refunded_minor,
            observed_at=None if state.observed_at is None else rfc3339(state.observed_at),
            audit_event_id=uuid_str(state.audit_event_id),
        )


class RunOut(_Out):
    """One reconciliation round, as ``reconciliation_runs`` recorded it."""

    attempt_number: int
    reason: str
    decision: str
    resulting_transition: str | None
    identifiers_queried: dict[str, Any]
    next_scheduled_attempt: str | None
    correlation_id: str
    created_at: str

    @classmethod
    def of(cls, run: recon.RunView) -> RunOut:
        return cls(
            attempt_number=run.attempt_number,
            reason=run.reason,
            decision=run.decision,
            resulting_transition=run.resulting_transition,
            identifiers_queried=dict(run.identifiers_queried),
            next_scheduled_attempt=_moment(run.next_scheduled_attempt),
            correlation_id=str(run.correlation_id),
            created_at=rfc3339(run.created_at),
        )


class RefundStateOut(_Out):
    """One refund against the attempt, with its own reconciliation budget."""

    refund_id: str
    status: str
    amount: MoneyOut
    reason_code: str
    provider_refund_id: str | None
    provider_originated: bool
    reserves_money: bool
    attempts_used: int
    attempts_remaining: int
    next_scheduled_attempt: str | None
    reason_family: str | None
    created_at: str

    @classmethod
    def of(cls, refund: recon.RefundView) -> RefundStateOut:
        return cls(
            refund_id=str(refund.refund_id),
            status=refund.status.value,
            amount=MoneyOut.of(refund.amount),
            reason_code=refund.reason_code,
            provider_refund_id=refund.provider_refund_id,
            provider_originated=refund.provider_originated,
            reserves_money=refund.reserves_money,
            attempts_used=refund.attempts_used,
            attempts_remaining=refund.attempts_remaining,
            next_scheduled_attempt=_moment(refund.next_scheduled_attempt),
            reason_family=refund.reason_family,
            created_at=rfc3339(refund.created_at),
        )


class FindingOut(_Out):
    """One divergence. ``provider_state`` is null when nothing has been verified yet."""

    finding_id: str
    code: recon.FindingCode
    subject: recon.Subject
    payment_attempt_id: str
    checkout_id: str
    checkout_version: int
    refund_id: str | None
    recorded_state: str
    provider_state: str | None
    reason_family: str | None
    exposure: MoneyOut
    detected_at: str
    correlation_id: str | None
    detail: str

    @classmethod
    def of(cls, finding: recon.Finding) -> FindingOut:
        return cls(
            finding_id=finding.finding_id,
            code=finding.code,
            subject=finding.subject,
            payment_attempt_id=str(finding.payment_attempt_id),
            checkout_id=str(finding.checkout_id),
            checkout_version=finding.checkout_version,
            refund_id=uuid_str(finding.refund_id),
            recorded_state=finding.recorded_state,
            provider_state=finding.provider_state,
            reason_family=finding.reason_family,
            exposure=MoneyOut.of(finding.exposure),
            detected_at=rfc3339(finding.detected_at),
            correlation_id=None if finding.correlation_id is None else str(finding.correlation_id),
            detail=finding.detail,
        )


class ProjectionOut(_Out):
    """Specification 6.4.1's projection for one attempt.

    ``recorded_state`` and ``verified`` are reported side by side and never merged: one is
    what this platform's row says, the other is what the provider's own answer established,
    and a single field would have to pick one and present it as both.
    """

    payment_attempt_id: str
    checkout_id: str
    checkout_version: int
    recorded_state: str
    amount: MoneyOut
    receipt: str
    provider_order_id: str | None
    provider_payment_id: str | None
    order_id: str | None
    verified: VerifiedStateOut
    refused_evidence: dict[str, Any] | None
    reason_family: str | None
    attempts_used: int
    attempts_remaining: int
    attempts_bound: int
    last_run_at: str | None
    next_scheduled_attempt: str | None
    captured_minor: int
    refunds_reserved_minor: int
    refunds_settled_minor: int
    runs: list[RunOut]
    refunds: list[RefundStateOut]
    findings: list[FindingOut]
    created_at: str
    updated_at: str

    @classmethod
    def of(cls, projection: recon.Projection, *, include_runs: bool) -> ProjectionOut:
        return cls(
            payment_attempt_id=str(projection.payment_attempt_id),
            checkout_id=str(projection.checkout_id),
            checkout_version=projection.checkout_version,
            recorded_state=projection.recorded_state.value,
            amount=MoneyOut.of(projection.amount),
            receipt=projection.receipt,
            provider_order_id=projection.provider_order_id,
            provider_payment_id=projection.provider_payment_id,
            order_id=uuid_str(projection.order_id),
            verified=VerifiedStateOut.of(projection.verified),
            refused_evidence=(
                None
                if projection.refused_evidence is None
                else dict(timeline.redact(dict(projection.refused_evidence)))
            ),
            reason_family=projection.reason_family,
            attempts_used=projection.attempts_used,
            attempts_remaining=projection.attempts_remaining,
            attempts_bound=recon.ATTEMPT_BOUND,
            last_run_at=_moment(projection.last_run_at),
            next_scheduled_attempt=_moment(projection.next_scheduled_attempt),
            # The capture ledger, as three integers the kernel wrote. The surface shows
            # them rather than a single "refundable", so nobody downstream has to subtract.
            captured_minor=projection.amount.minor,
            refunds_reserved_minor=projection.refund_reserved_minor,
            refunds_settled_minor=projection.refund_settled_minor,
            # The list view omits the rounds: a survey of fifty attempts would otherwise
            # carry three hundred rows nobody asked for. The detail view carries them all.
            runs=[RunOut.of(run) for run in projection.runs] if include_runs else [],
            refunds=[RefundStateOut.of(refund) for refund in projection.refunds],
            findings=[FindingOut.of(finding) for finding in projection.findings],
            created_at=rfc3339(projection.created_at),
            updated_at=rfc3339(projection.updated_at),
        )


class ReconciliationPageOut(_Out):
    """A page of projections, with the finding counts across it."""

    attempts: list[ProjectionOut]
    finding_counts: dict[str, int]
    limit: int


class PlanOptionOut(_Out):
    """One remedy the Resolution Service would offer, with its at-sale citation."""

    outcome: resolve.Outcome
    amount: MoneyOut
    policy_kind: str
    policy_id: str
    policy_version: int
    confirmation: resolve.Confirmation
    basis: str

    @classmethod
    def of(cls, option: resolve.PlanOption) -> PlanOptionOut:
        return cls(
            outcome=option.outcome,
            amount=MoneyOut.of(option.amount),
            policy_kind=option.policy_kind.value,
            policy_id=option.policy_id,
            policy_version=option.policy_version,
            confirmation=option.confirmation,
            basis=option.basis,
        )


class WithheldOptionOut(_Out):
    """One remedy considered and not offered, and the closed reason it was not."""

    outcome: resolve.Outcome
    reason: resolve.WithheldReason
    detail: str

    @classmethod
    def of(cls, withheld: resolve.WithheldOption) -> WithheldOptionOut:
        return cls(outcome=withheld.outcome, reason=withheld.reason, detail=withheld.detail)


class ResolutionOut(_Out):
    """What would settle one finding.

    ``plan_issued`` is false and ``plan_id`` null for every code except
    ``RESOLUTION_PLAN_ISSUED``, and ``recorded`` is false everywhere: no
    ``resolution_plans`` row exists in P0, so no confirmation can name this plan and
    nothing applies it automatically. The field is on the wire rather than in a document
    because a client that assumed otherwise would be wrong in the direction of money.
    """

    finding_id: str
    code: str
    plan_issued: bool
    plan_id: str | None
    recorded: bool
    options: list[PlanOptionOut]
    withheld: list[WithheldOptionOut]
    payment_attempt_id: str
    checkout_id: str
    refund_id: str | None
    policy_receipt_id: str | None
    policy_receipt_hash: str | None
    policy_binding: str
    captured_minor: int
    refunds_reserved_minor: int
    refundable_minor: int
    currency: str
    evaluated_at: str
    valid_until: str | None
    explanation: str

    @classmethod
    def of(cls, resolution: resolve.Resolution) -> ResolutionOut:
        return cls(
            finding_id=resolution.finding_id,
            code=resolution.code.value,
            plan_issued=resolution.plan_issued,
            plan_id=resolution.plan_id,
            recorded=resolution.recorded,
            options=[PlanOptionOut.of(option) for option in resolution.options],
            withheld=[WithheldOptionOut.of(item) for item in resolution.withheld],
            payment_attempt_id=str(resolution.payment_attempt_id),
            checkout_id=str(resolution.checkout_id),
            refund_id=uuid_str(resolution.refund_id),
            policy_receipt_id=uuid_str(resolution.policy_receipt_id),
            policy_receipt_hash=resolution.policy_receipt_hash,
            policy_binding=resolution.policy_binding,
            captured_minor=resolution.captured_minor,
            refunds_reserved_minor=resolution.refunds_reserved_minor,
            refundable_minor=resolution.refundable_minor,
            currency=resolution.currency,
            evaluated_at=rfc3339(resolution.evaluated_at),
            valid_until=_moment(resolution.valid_until),
            explanation=resolution.explanation,
        )


class AttemptReviewOut(_Out):
    """One attempt, its findings, and what would settle each of them."""

    attempt: ProjectionOut
    resolutions: list[ResolutionOut]
    plan_ttl_seconds: int


class ProofChainRefOut(_Out):
    """Where the proof chain is. A reference, verified on request, never a stored copy."""

    checkout_id: str
    payment_attempt_id: str | None
    href: str
    audit_streams: list[dict[str, str]]

    @classmethod
    def of(cls, ref: review.ProofChainRef) -> ProofChainRefOut:
        return cls(
            checkout_id=str(ref.checkout_id),
            payment_attempt_id=uuid_str(ref.payment_attempt_id),
            href=ref.href,
            audit_streams=[
                {"aggregate_type": kind, "aggregate_id": str(value)}
                for kind, value in ref.audit_streams
            ],
        )


class CaseOut(_Out):
    """One human-review case.

    ``monetary_exposure`` is null when the escalating path recorded no amount. It is not
    filled in from anywhere else and never rendered as zero: "we did not record what was
    at risk" and "nothing was at risk" are different facts about a case.
    """

    case_key: str
    state: review.CaseState
    priority: review.Priority
    reason_code: str
    reason_family: str
    checkout_id: str
    payment_attempt_id: str | None
    refund_id: str | None
    monetary_exposure: MoneyOut | None
    opened_at: str
    opened_by: str
    target_response_by: str
    target_response_seconds: int
    correlation_id: str
    attempts_used: int | None
    attempts_bound: int
    detections: int
    audit_event_id: str
    audit_aggregate_type: str
    audit_aggregate_id: str
    audit_seq: int
    audit_self_hash: str
    proof_chain: ProofChainRefOut

    @classmethod
    def of(cls, case: review.Case) -> CaseOut:
        return cls(
            case_key=case.case_key,
            state=case.state,
            priority=case.priority,
            reason_code=case.reason_code.value,
            reason_family=case.reason_family,
            checkout_id=str(case.checkout_id),
            payment_attempt_id=uuid_str(case.payment_attempt_id),
            refund_id=uuid_str(case.refund_id),
            monetary_exposure=(
                None if case.monetary_exposure is None else MoneyOut.of(case.monetary_exposure)
            ),
            opened_at=rfc3339(case.opened_at),
            opened_by=case.opened_by,
            target_response_by=rfc3339(case.target_response_by),
            target_response_seconds=case.target_response_seconds,
            correlation_id=str(case.correlation_id),
            attempts_used=case.attempts_used,
            attempts_bound=case.attempts_bound,
            detections=case.detections,
            audit_event_id=str(case.audit_event_id),
            audit_aggregate_type=case.audit_aggregate_type,
            audit_aggregate_id=str(case.audit_aggregate_id),
            audit_seq=case.audit_seq,
            audit_self_hash=case.audit_self_hash,
            proof_chain=ProofChainRefOut.of(case.proof_chain),
        )


class QueueOut(_Out):
    """A page of cases, and what this surface does and does not do."""

    cases: list[CaseOut]
    priority_counts: dict[str, int]
    limit: int
    scope: str


class CaseEventOut(_Out):
    """One redacted timeline row, as a reviewer reads it."""

    id: str
    occurred_at: str
    source: timeline.TimelineSource
    actor: str
    action: str
    summary: str
    correlation_id: str
    scenario_injection: bool
    checkout_version: int | None
    payment_attempt_id: str | None
    details: dict[str, Any]

    @classmethod
    def of(cls, entry: timeline.TimelineEntry) -> CaseEventOut:
        return cls(
            id=entry.cursor,
            occurred_at=rfc3339(entry.occurred_at),
            source=entry.source,
            actor=entry.actor,
            action=entry.action,
            summary=entry.summary,
            correlation_id=str(entry.correlation_id),
            scenario_injection=entry.scenario_injection,
            checkout_version=entry.checkout_version,
            payment_attempt_id=uuid_str(entry.payment_attempt_id),
            details=dict(entry.details),
        )


class CaseDetailOut(_Out):
    """One case with the evidence specification 6.4.3 asks a reviewer to be handed."""

    case: CaseOut
    verified_provider_state: VerifiedStateOut
    refused_evidence: dict[str, Any] | None
    attempt: ProjectionOut | None
    resolutions: list[ResolutionOut]
    timeline: list[CaseEventOut]
    scope: str


# ----------------------------------------------------------------------------- routes


@router.get(
    "/reconciliation",
    response_model=ReconciliationPageOut,
    summary="Provider state against recorded state, per payment attempt",
)
def list_reconciliation(
    ctx: SessionContext,
    session: AppSession,
    unresolved_only: Annotated[
        bool,
        Query(description="Keep only attempts that produced at least one finding."),
    ] = False,
    checkout_id: Annotated[
        uuid.UUID | None, Query(description="Narrow to one checkout's attempts.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=recon.MAX_SURVEY_LIMIT)] = recon.DEFAULT_SURVEY_LIMIT,
) -> ReconciliationPageOut:
    """The reconciliation projection for this tenant, newest attempt first.

    The counts are over the page rather than the tenant, and the field name says so: a
    figure labelled as a total that was really a page's worth would be the sort of number
    an operator makes a decision on and should not.
    """
    projections = recon.survey(
        session,
        tenant_id=ctx.tenant_id,
        checkout_id=checkout_id,
        unresolved_only=unresolved_only,
        limit=limit,
    )
    counts: dict[str, int] = {code.value: 0 for code in recon.FindingCode}
    for projection in projections:
        for finding in projection.findings:
            counts[finding.code.value] += 1
    return ReconciliationPageOut(
        attempts=[ProjectionOut.of(item, include_runs=False) for item in projections],
        finding_counts=counts,
        limit=limit,
    )


@router.get(
    "/reconciliation/{payment_attempt_id}",
    response_model=AttemptReviewOut,
    summary="One attempt, its findings, and the plan that would settle each",
)
def read_reconciliation(
    payment_attempt_id: uuid.UUID,
    ctx: SessionContext,
    session: AppSession,
) -> AttemptReviewOut:
    """The full projection for one attempt, with a resolution per finding.

    The resolutions are evaluated in the same read transaction as the projection they are
    derived from, so the ledger a plan was priced against is the ledger shown beside it.
    Evaluating them in a second call would let a refund settle in between and put an
    option on screen that the figures above it no longer support.
    """
    projection = recon.project(
        session, tenant_id=ctx.tenant_id, payment_attempt_id=payment_attempt_id
    )
    if projection is None:
        raise ProblemError(
            404,
            "Payment attempt not found",
            "No payment attempt with that identifier is visible to this tenant.",
            payment_attempt_id=str(payment_attempt_id),
        )
    resolutions = [
        resolve.evaluate(session, tenant_id=ctx.tenant_id, finding=finding, projection=projection)
        for finding in projection.findings
    ]
    return AttemptReviewOut(
        attempt=ProjectionOut.of(projection, include_runs=True),
        resolutions=[ResolutionOut.of(item) for item in resolutions],
        plan_ttl_seconds=resolve.PLAN_TTL_SECONDS,
    )


@router.get("/queue", response_model=QueueOut, summary="The human-review queue")
def list_queue(
    ctx: SessionContext,
    session: AppSession,
    limit: Annotated[int, Query(ge=1, le=review.MAX_QUEUE_LIMIT)] = review.DEFAULT_QUEUE_LIMIT,
) -> QueueOut:
    """Every case in this tenant, most recently opened first.

    Read-only, and deliberately so: there is no assign, no decision, no note and no
    resolve on this router. ``scope`` states that in the response, because a surface that
    simply lacked the controls would leave a reviewer guessing whether they were missing
    or merely elsewhere.
    """
    cases = review.queue(session, tenant_id=ctx.tenant_id, limit=limit)
    counts: dict[str, int] = {priority.value: 0 for priority in review.Priority}
    for case in cases:
        counts[case.priority.value] += 1
    return QueueOut(
        cases=[CaseOut.of(case) for case in cases],
        priority_counts=counts,
        limit=limit,
        scope=review.SCOPE_NOTE,
    )


@router.get(
    "/queue/{case_key}",
    response_model=CaseDetailOut,
    summary="One case, with its redacted evidence",
)
def read_queue_case(
    case_key: str,
    ctx: SessionContext,
    session: AppSession,
) -> CaseDetailOut:
    """One case: the blocking reason, the redacted timeline, the proof-chain reference,
    the verified provider state at the moment it was escalated, and the options the
    Resolution Service could and could not offer.

    404 for a key this tenant cannot see, which is also the answer for another tenant's
    key. A distinct refusal would confirm that a case exists somewhere, and a case key is
    a hash somebody could otherwise probe.
    """
    detail = review.read_case(session, tenant_id=ctx.tenant_id, case_key=case_key)
    if detail is None:
        raise ProblemError(
            404,
            "Case not found",
            "No human-review case with that key is visible to this tenant.",
            case_key=case_key,
        )
    return CaseDetailOut(
        case=CaseOut.of(detail.case),
        verified_provider_state=VerifiedStateOut.of(detail.verified_provider_state),
        refused_evidence=(
            None if detail.refused_evidence is None else dict(detail.refused_evidence)
        ),
        attempt=(
            None
            if detail.projection is None
            else ProjectionOut.of(detail.projection, include_runs=True)
        ),
        resolutions=[ResolutionOut.of(item) for item in detail.resolutions],
        timeline=[CaseEventOut.of(entry) for entry in detail.events],
        scope=review.SCOPE_NOTE,
    )


def _moment(value: datetime | None) -> str | None:
    return None if value is None else rfc3339(value)
