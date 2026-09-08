"""The cases that need a person, and the evidence they need to look at.

Specification 6.4.3. Deterministic code, not an agent (``docs/briefs/AGENT_ROSTER.md``).

**This ships the queue and the evidence, not a resolution workflow.** There is no assign, no
decision recording, no reviewer note and no resolve action anywhere in this module or the
router over it, and that is a decision rather than an omission -- see the section below on
why a support case, which *is* resolvable, is a different object. A button that does nothing
is worse than no button: it tells a judge the workflow exists, and the first click proves
it does not. :data:`SCOPE_NOTE` is carried in the response so the surface states the limit
in words instead of implying a capability with a control.

Not the same thing as a support case
------------------------------------
``support_cases`` exists now and has its own queue, its own router and a merchant screen
that answers it. It is a different object from this one and the two must not be merged. A
support case is a buyer saying something is wrong with an order they bought, in their own
words, and a person answers it. A human review case is the kernel saying it does not know
what happened to a payment, and nothing in the product can answer that -- it is reconciled,
not decided. Putting them on one queue would bury the one a person can actually act on
underneath the one they cannot.

So the paragraph below still holds for *this* module, and the resolve workflow the helpdesk
gained is not the missing piece here.

Where a case lives
------------------
A case *is* the kernel's ``human_review.opened`` audit event -- the same row
:func:`transaction_kernel.escalate` and
:func:`transaction_kernel.refunds.escalate_refund` append under the attempt's row lock,
carrying the deterministic ``case_key`` of specification 6.4.3. That has three
consequences worth stating plainly rather than hiding behind a projection:

* **Exactly-once creation is already real.** The kernel scans the stream for the same key
  under the lock before appending, so three detectors of one stuck payment produce one
  event. :attr:`Case.detections` reports how many events share the key, so the guarantee
  is visible in the response and a test can assert it rather than trust it.
* **The case is hash-chained.** Its ``self_hash`` links it to the stream it sits in, which
  means a case cannot be edited or removed without breaking the chain the proof endpoint
  verifies. A mutable ``support_cases`` row would have been weaker evidence, not stronger.
* **A case has one reachable state.** :class:`CaseState` mirrors the specification's
  vocabulary, and every P0 case is ``AWAITING_HUMAN``: nothing in the product advances it,
  because nothing in the product resolves a case. Reporting ``OPEN`` and then never moving
  it would be a state machine that is really a constant, described as if it were not.

Two streams, one queue
----------------------
The payment path appends its case to the *checkout*'s stream and the refund path to the
*payment attempt*'s, because a refund is an operation on an attempt rather than on a
version. So the queue reads ``human_review.opened`` across every aggregate type rather
than one, and resolves each case's subject from the payload -- by provider order id where
the payment path recorded no attempt id, which is unique per tenant and is the handle the
provider was actually queried by.

What a case carries
-------------------
Specification 6.4.3's list, and each item is read rather than composed: the blocking
reason code, a redacted action timeline, the Money Action Proof Chain **reference**,
correlation ids, the options the Resolution Service could and could not offer, the
verified provider state at the moment of escalation, and a target response time.

Two of those need their honesty stated:

``verified_provider_state``
    "At the moment of escalation" is enforced, not approximated: the last applied evidence
    event *at or before* the case's own timestamp. A case opened because the provider's
    entity contradicted the record has no verified state at all, and
    :attr:`CaseDetail.refused_evidence` carries what was refused instead. Folding the two
    together would present an entity that may belong to another checkout as this one's
    provider truth.

``target_response_by``
    Derived from :data:`TARGET_RESPONSE_SECONDS`, a declared platform target applied to
    the case's priority. It is recorded for prioritisation and P0 promises nothing about
    meeting it, which is why :data:`SCOPE_NOTE` says so and why the Support Agent is told
    never to quote it to a buyer as a guarantee.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from commerce_domain import Money, RecoveryCode
from platform_db import AuditEvent, PaymentAttempt
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import reconciliation_service as recon
from . import resolution_service as resolve
from . import timeline

__all__ = [
    "SCOPE_NOTE",
    "TARGET_RESPONSE_SECONDS",
    "Case",
    "CaseDetail",
    "CaseState",
    "Priority",
    "ProofChainRef",
    "queue",
    "read_case",
]

#: What this surface is, in words, because it is not what a reviewer might assume. Carried
#: in every response so the limit travels with the data rather than living in a document.
SCOPE_NOTE: Final[str] = (
    "P0 ships the human-review queue and its evidence. No case is assigned, decided, "
    "annotated or resolved here, and no control on this surface changes financial state. "
    "A reviewer acts outside this surface, through the trusted operator path, where the "
    "decision passes the same admission transaction and consumes its own Execution Grant. "
    "The target response time is recorded for prioritisation and is not a commitment."
)

#: How long the platform aims to take over each priority, in seconds. A declared target
#: applied to committed rows -- not a measurement, and not a promise.
TARGET_RESPONSE_SECONDS: Final[Mapping[str, int]] = {
    "P1": 3600,
    "P2": 14400,
    "P3": 86400,
}

#: How many cases one page of the queue returns.
DEFAULT_QUEUE_LIMIT: Final[int] = 50
MAX_QUEUE_LIMIT: Final[int] = 200


class CaseState(StrEnum):
    """Specification 6.4.3's states. Only ``AWAITING_HUMAN`` is reachable in P0.

    The other three are declared because they are the closed vocabulary a later operator
    increment will move a case through, and half a vocabulary is harder to extend
    correctly than none. Nothing in this package returns them.
    """

    OPEN = "OPEN"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class Priority(StrEnum):
    """How a case is ordered for a reviewer. Derived from rows, never assigned by hand."""

    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


@dataclass(frozen=True, slots=True)
class ProofChainRef:
    """Where the Money Action Proof Chain for this case is, not a copy of it.

    A reference rather than the document: the chain is verified by recomputation at read
    time, and a copy pasted into a case would be a snapshot that could disagree with the
    verifier the next time anyone ran it.
    """

    checkout_id: uuid.UUID
    payment_attempt_id: uuid.UUID | None
    href: str
    audit_streams: tuple[tuple[str, uuid.UUID], ...]


@dataclass(frozen=True, slots=True)
class Case:
    """One escalation, as the queue lists it."""

    case_key: str
    state: CaseState
    priority: Priority
    reason_code: RecoveryCode
    reason_family: str
    checkout_id: uuid.UUID
    payment_attempt_id: uuid.UUID | None
    refund_id: uuid.UUID | None
    monetary_exposure: Money | None
    opened_at: datetime
    target_response_by: datetime
    correlation_id: uuid.UUID
    opened_by: str
    attempts_used: int | None
    attempts_bound: int
    detections: int
    audit_event_id: uuid.UUID
    audit_aggregate_type: str
    audit_aggregate_id: uuid.UUID
    audit_seq: int
    audit_self_hash: str
    proof_chain: ProofChainRef

    @property
    def target_response_seconds(self) -> int:
        return TARGET_RESPONSE_SECONDS[self.priority.value]


@dataclass(frozen=True, slots=True)
class CaseDetail:
    """One case with everything specification 6.4.3 asks a reviewer to be given."""

    case: Case
    verified_provider_state: recon.VerifiedState
    refused_evidence: Mapping[str, Any] | None
    projection: recon.Projection | None
    resolutions: tuple[resolve.Resolution, ...]
    events: tuple[timeline.TimelineEntry, ...]


# --------------------------------------------------------------------------- queue


def queue(
    session: Session, *, tenant_id: uuid.UUID, limit: int = DEFAULT_QUEUE_LIMIT
) -> tuple[Case, ...]:
    """Every open case in this tenant, most recent first.

    Runs on the app role inside the caller's read transaction and writes nothing.
    """
    bounded = max(1, min(limit, MAX_QUEUE_LIMIT))
    events = _case_events(session, tenant_id)
    grouped = _by_case_key(events)
    subjects = _subjects(session, tenant_id, [first for first, _ in grouped.values()])
    projections = recon.project_many(
        session,
        tenant_id=tenant_id,
        payment_attempt_ids=[value for value in subjects.values() if value is not None],
    )
    cases = [
        _case(event, detections=count, subject=subjects.get(event.id), projections=projections)
        for event, count in grouped.values()
    ]
    cases.sort(key=lambda item: (item.opened_at, item.case_key), reverse=True)
    return tuple(cases[:bounded])


def read_case(session: Session, *, tenant_id: uuid.UUID, case_key: str) -> CaseDetail | None:
    """One case with its redacted timeline, its provider state and its resolutions.

    Returns ``None`` when this tenant has no case under that key, which is also the answer
    for a key belonging to another tenant: row-level security scopes the read, and a
    distinct refusal would turn this endpoint into a way to learn that a case exists
    somewhere else.
    """
    events = _case_events(session, tenant_id)
    grouped = _by_case_key(events)
    entry = grouped.get(case_key)
    if entry is None:
        return None
    event, detections = entry

    subjects = _subjects(session, tenant_id, [event])
    attempt_id = subjects.get(event.id)
    projections = recon.project_many(
        session,
        tenant_id=tenant_id,
        payment_attempt_ids=[] if attempt_id is None else [attempt_id],
    )
    case = _case(event, detections=detections, subject=attempt_id, projections=projections)
    projection = None if attempt_id is None else projections.get(attempt_id)

    # The attempt's provider order is what ties an evidence row to this case rather than
    # to a sibling attempt on the same checkout; a case with no attempt resolved has no
    # provider statement to show, and says so.
    provider_order_id = None if projection is None else projection.provider_order_id
    verified = _verified_at_escalation(session, tenant_id, case, provider_order_id)
    refused = _refused_at_escalation(session, tenant_id, case, provider_order_id)
    resolutions = (
        ()
        if projection is None
        else tuple(
            resolve.evaluate(session, tenant_id=tenant_id, finding=finding, projection=projection)
            for finding in projection.findings
        )
    )
    entries = timeline.collect(session, tenant_id=tenant_id, checkout_id=case.checkout_id)
    return CaseDetail(
        case=case,
        verified_provider_state=verified,
        refused_evidence=refused,
        projection=projection,
        resolutions=resolutions,
        events=entries,
    )


# ------------------------------------------------------------------------- reads


def _case_events(session: Session, tenant_id: uuid.UUID) -> tuple[AuditEvent, ...]:
    """Every ``human_review.opened`` event in the tenant, oldest first, across both streams."""
    return tuple(
        session.execute(
            select(AuditEvent)
            .where(
                AuditEvent.tenant_id == tenant_id,
                AuditEvent.event_type == recon.CASE_OPENED_EVENT,
            )
            .order_by(AuditEvent.occurred_at, AuditEvent.seq)
        )
        .scalars()
        .all()
    )


def _by_case_key(events: Sequence[AuditEvent]) -> dict[str, tuple[AuditEvent, int]]:
    """Collapse events onto their case key, keeping the first and counting the rest.

    The kernel already guarantees one event per key, so the count is normally one. It is
    reported rather than assumed: a two would mean the exactly-once check had failed, and
    the queue should show that rather than quietly present the earliest of two cases as if
    it were the only one.
    """
    grouped: dict[str, tuple[AuditEvent, int]] = {}
    for event in events:
        key = event.payload.get("case_key")
        if not isinstance(key, str):
            continue
        existing = grouped.get(key)
        grouped[key] = (event, 1) if existing is None else (existing[0], existing[1] + 1)
    return grouped


def _subjects(
    session: Session, tenant_id: uuid.UUID, events: Sequence[AuditEvent]
) -> dict[uuid.UUID, uuid.UUID | None]:
    """Map each case event to the payment attempt it is about.

    The refund path names the attempt outright. The payment path does not: its case is
    appended to the checkout's stream and records the provider identifiers, so the
    provider order id -- unique per tenant by a partial unique index -- is the handle back
    to the row. Nothing here guesses: a case whose payload names neither maps to ``None``
    and the queue reports the attempt as absent rather than picking a likely one.
    """
    named: dict[uuid.UUID, uuid.UUID | None] = {}
    wanted_orders: dict[str, list[uuid.UUID]] = {}
    for event in events:
        payload = event.payload or {}
        attempt = _as_uuid(payload.get("payment_attempt_id"))
        if attempt is not None:
            named[event.id] = attempt
            continue
        order_id = payload.get("provider_order_id")
        if isinstance(order_id, str) and order_id:
            wanted_orders.setdefault(order_id, []).append(event.id)
        else:
            named[event.id] = None

    if wanted_orders:
        rows = session.execute(
            select(PaymentAttempt.id, PaymentAttempt.provider_order_id).where(
                PaymentAttempt.tenant_id == tenant_id,
                PaymentAttempt.provider_order_id.in_(list(wanted_orders)),
            )
        ).all()
        by_order = {str(row[1]): row[0] for row in rows}
        for order_id, event_ids in wanted_orders.items():
            for event_id in event_ids:
                named[event_id] = by_order.get(order_id)
    return named


# --------------------------------------------------------------------- assembly


def _case(
    event: AuditEvent,
    *,
    detections: int,
    subject: uuid.UUID | None,
    projections: Mapping[uuid.UUID, recon.Projection],
) -> Case:
    payload = event.payload or {}
    projection = None if subject is None else projections.get(subject)
    checkout_id = (
        _as_uuid(payload.get("checkout_id"))
        or (projection.checkout_id if projection is not None else None)
        or event.aggregate_id
    )
    exposure = _exposure(payload)
    priority = _priority(exposure, projection)
    return Case(
        case_key=str(payload.get("case_key")),
        # Every P0 case is awaiting a person: nothing in this product advances it.
        state=CaseState.AWAITING_HUMAN,
        priority=priority,
        reason_code=_reason_code(payload),
        reason_family=str(payload.get("reason_family") or ""),
        checkout_id=checkout_id,
        payment_attempt_id=subject,
        refund_id=_as_uuid(payload.get("refund_id")),
        monetary_exposure=exposure,
        opened_at=event.occurred_at,
        target_response_by=event.occurred_at
        + timedelta(seconds=TARGET_RESPONSE_SECONDS[priority.value]),
        correlation_id=event.correlation_id,
        opened_by=event.actor_type,
        attempts_used=_integer(payload.get("attempts")),
        attempts_bound=_integer(payload.get("max_attempts")) or recon.ATTEMPT_BOUND,
        detections=detections,
        audit_event_id=event.id,
        audit_aggregate_type=event.aggregate_type,
        audit_aggregate_id=event.aggregate_id,
        audit_seq=event.seq,
        audit_self_hash=event.self_hash,
        proof_chain=_proof_ref(checkout_id, subject),
    )


def _proof_ref(checkout_id: uuid.UUID, attempt_id: uuid.UUID | None) -> ProofChainRef:
    href = f"/v1/checkouts/{checkout_id}/proof"
    if attempt_id is not None:
        href = f"{href}?payment_attempt_id={attempt_id}"
    streams: list[tuple[str, uuid.UUID]] = [("checkout", checkout_id)]
    if attempt_id is not None:
        streams.append(("payment_attempt", attempt_id))
    return ProofChainRef(
        checkout_id=checkout_id,
        payment_attempt_id=attempt_id,
        href=href,
        audit_streams=tuple(streams),
    )


def _exposure(payload: Mapping[str, Any]) -> Money | None:
    """The money at risk, as the escalating path recorded it.

    Two shapes, because two paths write it: the payment path records ``amount_minor`` and
    ``currency`` as separate fields, and the refund path records a ``Money``, which the
    audit canonicaliser expands to ``{"currency", "minor"}``. Neither is reconstructed
    from anywhere else -- a case whose payload records no amount reports ``None``, and the
    surface says the exposure was not recorded rather than showing a zero.
    """
    nested = payload.get("monetary_exposure")
    if isinstance(nested, Mapping):
        minor = _integer(nested.get("minor"))
        currency = nested.get("currency")
        if minor is not None and isinstance(currency, str):
            return Money(minor, currency)
    minor = _integer(payload.get("amount_minor"))
    currency = payload.get("currency")
    if minor is not None and isinstance(currency, str):
        return Money(minor, currency)
    return None


def _reason_code(payload: Mapping[str, Any]) -> RecoveryCode:
    """The blocking code the case recorded, or the code an escalation means by definition.

    The refund path stamps ``code`` on the payload; the payment path does not, because an
    escalation has exactly one meaning. Falling back to ``HUMAN_REVIEW_REQUIRED`` is not an
    inference: it is what ``escalate`` is.
    """
    recorded = payload.get("code")
    if isinstance(recorded, str):
        try:
            return RecoveryCode(recorded)
        except ValueError:
            return RecoveryCode.HUMAN_REVIEW_REQUIRED
    return RecoveryCode.HUMAN_REVIEW_REQUIRED


def _priority(exposure: Money | None, projection: recon.Projection | None) -> Priority:
    """Order the queue by how little is known about money that may have moved.

    ``P1`` is a case whose attempt has no verified provider statement at all, or one whose
    provider entity was refused as contradictory: money may have moved and nothing in the
    record can say. ``P2`` is a case with an exposure and a provider answer to read.
    ``P3`` is a case whose payload recorded no amount, which is the only situation where
    there is nothing to weigh.
    """
    if exposure is None:
        return Priority.P3
    if projection is None or not projection.verified.present or projection.refused_evidence:
        return Priority.P1
    return Priority.P2


# ----------------------------------------------------- state at the moment of escalation


def _verified_at_escalation(
    session: Session, tenant_id: uuid.UUID, case: Case, provider_order_id: str | None
) -> recon.VerifiedState:
    """The last applied provider evidence about this attempt, at or before the escalation.

    Bounded by the case's own timestamp rather than read as "latest", so a webhook that
    arrived after the escalation cannot be shown as what a reviewer was looking at. Where
    the evidence and the case were written in one transaction they share a timestamp, and
    the evidence is included: it is what caused the escalation.
    """
    if provider_order_id is None:
        return recon.VerifiedState()
    row = _latest_before(
        session,
        tenant_id,
        aggregate_id=case.checkout_id,
        event_type=recon.EVIDENCE_APPLIED_EVENT,
        moment=case.opened_at,
        provider_order_key="provider_order_id",
        provider_order_id=provider_order_id,
    )
    if row is None:
        return recon.VerifiedState()
    payload = row.payload or {}
    return recon.VerifiedState(
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


def _refused_at_escalation(
    session: Session, tenant_id: uuid.UUID, case: Case, provider_order_id: str | None
) -> Mapping[str, Any] | None:
    """The provider entity the kernel refused as evidence, when that is why the case exists.

    Matched on ``expected_provider_order_id``, which the kernel stamps with the attempt's
    own order rather than the entity's -- the mismatch is precisely that the two differ.
    """
    if provider_order_id is None:
        return None
    row = _latest_before(
        session,
        tenant_id,
        aggregate_id=case.checkout_id,
        event_type=recon.EVIDENCE_MISMATCH_EVENT,
        moment=case.opened_at,
        provider_order_key="expected_provider_order_id",
        provider_order_id=provider_order_id,
    )
    return None if row is None else timeline.redact(dict(row.payload or {}))


def _latest_before(
    session: Session,
    tenant_id: uuid.UUID,
    *,
    aggregate_id: uuid.UUID,
    event_type: str,
    moment: datetime,
    provider_order_key: str,
    provider_order_id: str,
) -> AuditEvent | None:
    """The newest event of one type on this checkout's stream, about this provider order."""
    return session.execute(
        select(AuditEvent)
        .where(
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.aggregate_type == "checkout",
            AuditEvent.aggregate_id == aggregate_id,
            AuditEvent.event_type == event_type,
            AuditEvent.payload[provider_order_key].astext == provider_order_id,
            AuditEvent.occurred_at <= moment,
        )
        .order_by(AuditEvent.occurred_at.desc(), AuditEvent.seq.desc())
        .limit(1)
    ).scalar_one_or_none()


def _as_uuid(value: object) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
