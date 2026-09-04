"""The Money Action Proof Chain and its verifier, specification 26.4.

Why a projection and a verifier, not a report
---------------------------------------------
Anyone can render ten headings and a green tick. What makes this evidence is that the
verifier recomputes rather than reads: it hashes the stored canonical content with
:func:`transaction_kernel.content_hash` and compares against the hash the approval was
recorded under, and it walks the audit chains with
:func:`transaction_kernel.verify_chain`. If somebody edits a stored payload through an
owner connection, the recomputation disagrees with the stored hash and the verdict fails
at the sequence number where the edit happened. A verifier that trusted the stored hash
would report "verified" on the tampered row, which is worse than having no verifier.

The ten links are specification 26.4's, in its order, each read from committed rows:

1. Intent, and the ``AgentPrincipal`` that proposed it -- audit.
2. Authoritative merchant-state snapshot and freshness -- the version's content.
3. Checkout id, version and canonical hash -- ``checkout_versions``.
4. Policy-at-Sale Receipt hash -- ``policy_at_sale_receipts``.
5. Approval and authority epoch -- ``approvals``.
6. The kernel decision -- audit.
7. Execution Grant and the durable command that carried it -- ``execution_grants``,
   ``outbox_events``.
8. Redacted Razorpay request reference -- ``provider_requests``.
9. Verified callback, webhook or reconciliation evidence -- ``webhook_inbox``,
   ``reconciliation_runs``, ``orders``.
10. Final payment, refund and order state -- ``payment_attempts``, ``refunds``, ``orders``.

Every link is a committed row. A missing link is reported as missing, never inferred: the
whole value of this document is that "we have no evidence of step 8" and "step 8 happened"
look completely different.

Tiers
-----
A chain is verified as far as it got. ``PROPOSED`` means a version exists; ``APPROVED``
adds a receipt and a recorded approval; ``ADMITTED`` adds a kernel decision, a grant and a
durable command; ``EXECUTED`` adds the provider request; ``COMPLETE`` adds verified capture
evidence and a confirmed order. A denied admission ends at ``APPROVED`` with ``ok`` true --
a refusal that left no provider order is the system working, and the chain says so rather
than reporting a gap.

Retained revenue
----------------
:func:`retained_revenue` is step 11 of the demonstration. Every figure is read from a
committed row: version N's approved-then-invalidated total, version N+1's total, and the
captured amount taken from the ``orders`` row that verified capture evidence produced.
Nothing here estimates, and the difference is reported with the direction it points in,
because a stale approval that was *higher* than the corrected one protects the buyer
rather than the merchant and calling both "retained" would be a sales figure.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

import transaction_kernel as tk
from platform_db import (
    Approval,
    ExecutionGrant,
    Merchant,
    Order,
    PolicyAtSaleReceipt,
    ProviderRequest,
    ReconciliationRun,
    Refund,
)
from sqlalchemy import select
from sqlalchemy.orm import Session
from transaction_kernel.audit import ChainVerification
from transaction_kernel.checkout_content import ContentContractError

from ..schemas import rfc3339
from .timeline import OutboxRow, attempts_of, outbox_for, redact, webhooks_of

__all__ = [
    "Check",
    "ProofChain",
    "ProofTier",
    "RetainedRevenue",
    "Verdict",
    "build",
    "retained_revenue",
    "verify_stream",
]

#: Evidence channels that may confirm an order. ``BROWSER_CALLBACK`` is deliberately
#: absent (ADR 0003 D8): the buyer's browser saying "paid" is a claim, not a settlement.
_SUFFICIENT_CAPTURE_SOURCES: Final[frozenset[str]] = frozenset(
    {tk.EvidenceSource.WEBHOOK.value, tk.EvidenceSource.PROVIDER_FETCH.value, "VERIFIED_WEBHOOK"}
)


class ProofTier(StrEnum):
    """How far this money action's evidence reaches. Ordered weakest to strongest."""

    EMPTY = "EMPTY"
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    ADMITTED = "ADMITTED"
    EXECUTED = "EXECUTED"
    COMPLETE = "COMPLETE"


@dataclass(frozen=True, slots=True)
class Check:
    """One thing the verifier tested, and what it found.

    ``ok`` false is a finding; ``ok`` true with ``applicable`` false means the check had
    nothing to test yet -- a chain that has not reached the provider has no provider
    request to verify, and reporting that as a pass would be as misleading as a failure.
    """

    name: str
    ok: bool
    applicable: bool
    detail: str


@dataclass(frozen=True, slots=True)
class Verdict:
    """The verifier's answer. ``ok`` is the conjunction of every applicable check."""

    tier: ProofTier
    ok: bool
    checks: tuple[Check, ...]

    @property
    def failures(self) -> tuple[Check, ...]:
        return tuple(check for check in self.checks if check.applicable and not check.ok)


@dataclass(frozen=True, slots=True)
class ProofChain:
    """The ten links plus the verdict, for one money action on one checkout."""

    checkout_id: uuid.UUID
    tenant_id: uuid.UUID
    merchant_id: uuid.UUID
    payment_attempt_id: uuid.UUID | None
    attempt_ids: tuple[uuid.UUID, ...]
    links: Mapping[str, Any]
    verdict: Verdict
    audit_streams: Mapping[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------------ chain build


def build(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    checkout_id: uuid.UUID,
    payment_attempt_id: uuid.UUID | None = None,
) -> ProofChain | None:
    """Assemble and verify the proof chain for one checkout's money action.

    ``payment_attempt_id`` selects which attempt to follow; the newest is used when it is
    omitted, which is the one a reviewer means by "this payment". Returns ``None`` when
    the checkout is not visible to this tenant.

    Runs on the app role inside the caller's read transaction and writes nothing.
    """
    head = tk.read_head(session, tenant_id=tenant_id, checkout_id=checkout_id)
    if head is None:
        return None

    versions = tk.read_versions(session, tenant_id=tenant_id, checkout_id=checkout_id)
    attempts = attempts_of(session, tenant_id=tenant_id, checkout_id=checkout_id)
    attempt = _select_attempt(attempts, payment_attempt_id)

    version = _version_for(versions, attempt.checkout_version if attempt else None)
    approvals = _approvals(session, tenant_id, checkout_id)
    approval = _approval_for(approvals, version.version if version else None)
    receipt = _receipt(session, tenant_id, version)
    decision = _decision_event(session, tenant_id, checkout_id, version)
    grant = _grant_for(session, tenant_id, checkout_id, attempt)
    command = _command_for(session, tenant_id, checkout_id, attempts, grant)
    requests = _provider_requests(session, tenant_id, attempt)
    order = _order(session, tenant_id, attempt)
    refunds = _refunds(session, tenant_id, attempt)
    runs = _reconciliation_runs(session, tenant_id, attempt)
    deliveries = webhooks_of(session, tenant_id=tenant_id, attempts=[attempt] if attempt else [])

    streams = _verify_streams(session, tenant_id, checkout_id, attempt)
    links = _links(
        head=head,
        version=version,
        receipt=receipt,
        approval=approval,
        decision=decision,
        grant=grant,
        command=command,
        requests=requests,
        deliveries=deliveries,
        runs=runs,
        order=order,
        attempt=attempt,
        refunds=refunds,
    )
    verdict = _verify(
        tenant_id=tenant_id,
        head=head,
        version=version,
        receipt=receipt,
        approval=approval,
        decision=decision,
        grant=grant,
        command=command,
        requests=requests,
        order=order,
        attempt=attempt,
        refunds=refunds,
        streams=streams,
    )
    return ProofChain(
        checkout_id=checkout_id,
        tenant_id=tenant_id,
        merchant_id=head.merchant_id,
        payment_attempt_id=attempt.id if attempt else None,
        attempt_ids=tuple(a.id for a in attempts),
        links=links,
        verdict=verdict,
        audit_streams={
            name: _stream_summary(result) for name, result in streams.items() if result is not None
        },
    )


def verify_stream(
    session: Session, *, tenant_id: uuid.UUID, aggregate_type: str, aggregate_id: uuid.UUID
) -> Mapping[str, Any]:
    """``tk.verify_chain`` as a response body: intact, or the first break.

    Thin on purpose. The kernel owns what a break means and which one to report; this
    turns its :class:`~transaction_kernel.audit.ChainVerification` into JSON and adds
    nothing to the verdict.
    """
    result = tk.verify_chain(
        session, tenant=tenant_id, aggregate_type=aggregate_type, aggregate_id=aggregate_id
    )
    return _stream_summary(result)


def _stream_summary(result: ChainVerification) -> dict[str, Any]:
    break_ = result.first_break
    return {
        "aggregate_type": result.aggregate_type,
        "aggregate_id": str(result.aggregate_id),
        "intact": result.intact,
        "empty": result.empty,
        "length": result.length,
        "events_verified": result.events_verified,
        "head_seq": result.head_seq,
        "head_hash": result.head_hash,
        "code": result.code.value,
        "first_break": None
        if break_ is None
        else {
            "kind": break_.kind.value,
            "at_seq": break_.at_seq,
            "event_id": None if break_.event_id is None else str(break_.event_id),
            "expected": break_.expected,
            "found": break_.found,
            "detail": break_.detail,
        },
    }


# ------------------------------------------------------------------------- row lookup


def _select_attempt(
    attempts: Sequence[Any], wanted: uuid.UUID | None
) -> Any:  # PaymentAttempt | None
    if not attempts:
        return None
    if wanted is None:
        return attempts[-1]
    for attempt in attempts:
        if attempt.id == wanted:
            return attempt
    return None


def _version_for(
    versions: Sequence[tk.CheckoutVersionView], wanted: int | None
) -> tk.CheckoutVersionView | None:
    """The version the money action concerns, or the newest when there is no attempt yet."""
    if not versions:
        return None
    if wanted is None:
        return versions[-1]
    for version in versions:
        if version.version == wanted:
            return version
    return None


def _approvals(
    session: Session, tenant_id: uuid.UUID, checkout_id: uuid.UUID
) -> tuple[Approval, ...]:
    rows = session.execute(
        select(Approval)
        .where(Approval.tenant_id == tenant_id, Approval.checkout_id == checkout_id)
        .order_by(Approval.checkout_version, Approval.issued_at)
    ).scalars()
    return tuple(rows)


def _approval_for(approvals: Sequence[Approval], version: int | None) -> Approval | None:
    if version is None:
        return approvals[-1] if approvals else None
    matching = [a for a in approvals if a.checkout_version == version]
    if not matching:
        return None
    # A version may hold one RECORDED approval plus earlier INVALIDATED or EXPIRED ones.
    # The consumed or recorded row is the decision that authorized this action; prefer it.
    for status in ("CONSUMED", "RECORDED"):
        for approval in matching:
            if approval.status == status:
                return approval
    return matching[-1]


def _receipt(
    session: Session, tenant_id: uuid.UUID, version: tk.CheckoutVersionView | None
) -> PolicyAtSaleReceipt | None:
    if version is None or version.policy_receipt_id is None:
        return None
    return session.execute(
        select(PolicyAtSaleReceipt).where(
            PolicyAtSaleReceipt.tenant_id == tenant_id,
            PolicyAtSaleReceipt.id == version.policy_receipt_id,
        )
    ).scalar_one_or_none()


def _decision_event(
    session: Session,
    tenant_id: uuid.UUID,
    checkout_id: uuid.UUID,
    version: tk.CheckoutVersionView | None,
) -> Mapping[str, Any] | None:
    """The kernel's own audit row for the admission of this version.

    Read from the audit stream rather than reconstructed, because the decision is not a
    table: ``admission.allowed`` and ``admission.denied`` are the only durable record of
    what the kernel ruled and why, which is exactly why the kernel writes them in the
    admission transaction.
    """
    if version is None:
        return None
    events = [
        event
        for event in tk.read_stream(
            session, tenant=tenant_id, aggregate_type="checkout", aggregate_id=checkout_id
        )
        if event.event_type in ("admission.allowed", "admission.denied")
        and event.payload.get("version") == version.version
    ]
    if not events:
        return None
    event = events[-1]
    return {
        "event_id": str(event.event_id),
        "seq": event.seq,
        "allowed": event.event_type == "admission.allowed",
        "occurred_at": event.occurred_at,
        "actor_type": event.actor_type,
        "principal_id": event.principal_id,
        "correlation_id": event.correlation_id,
        "payload": event.payload,
    }


def _grant_for(
    session: Session, tenant_id: uuid.UUID, checkout_id: uuid.UUID, attempt: Any
) -> ExecutionGrant | None:
    if attempt is None:
        return None
    return session.execute(
        select(ExecutionGrant)
        .where(
            ExecutionGrant.tenant_id == tenant_id,
            ExecutionGrant.checkout_id == checkout_id,
            ExecutionGrant.payment_attempt_id == attempt.id,
            ExecutionGrant.operation == tk.Operation.PAYMENT_CREATE_ORDER.value,
        )
        .order_by(ExecutionGrant.issued_at)
        .limit(1)
    ).scalar_one_or_none()


def _command_for(
    session: Session,
    tenant_id: uuid.UUID,
    checkout_id: uuid.UUID,
    attempts: Sequence[Any],
    grant: ExecutionGrant | None,
) -> OutboxRow | None:
    """The durable command the grant travelled on.

    Matched through ``execution_grants.outbox_command_id`` rather than by searching the
    queue: that column is written by ``tk.link_command`` in the admission transaction and
    is the authoritative link between an authority and the work that carried it.
    """
    if grant is None or grant.outbox_command_id is None:
        return None
    rows = outbox_for(
        session,
        tenant_id=tenant_id,
        checkout_id=checkout_id,
        attempts=attempts,
        inbox_ids=[],
    )
    for row in rows:
        if row.command_id == grant.outbox_command_id:
            return row
    return None


def _provider_requests(
    session: Session, tenant_id: uuid.UUID, attempt: Any
) -> tuple[ProviderRequest, ...]:
    if attempt is None:
        return ()
    rows = session.execute(
        select(ProviderRequest)
        .where(
            ProviderRequest.tenant_id == tenant_id,
            ProviderRequest.payment_attempt_id == attempt.id,
        )
        .order_by(ProviderRequest.request_at, ProviderRequest.id)
    ).scalars()
    return tuple(rows)


def _order(session: Session, tenant_id: uuid.UUID, attempt: Any) -> Order | None:
    if attempt is None:
        return None
    return session.execute(
        select(Order).where(Order.tenant_id == tenant_id, Order.payment_attempt_id == attempt.id)
    ).scalar_one_or_none()


def _refunds(session: Session, tenant_id: uuid.UUID, attempt: Any) -> tuple[Refund, ...]:
    if attempt is None:
        return ()
    rows = session.execute(
        select(Refund)
        .where(Refund.tenant_id == tenant_id, Refund.payment_attempt_id == attempt.id)
        .order_by(Refund.created_at, Refund.id)
    ).scalars()
    return tuple(rows)


def _reconciliation_runs(
    session: Session, tenant_id: uuid.UUID, attempt: Any
) -> tuple[ReconciliationRun, ...]:
    if attempt is None:
        return ()
    rows = session.execute(
        select(ReconciliationRun)
        .where(
            ReconciliationRun.tenant_id == tenant_id,
            ReconciliationRun.payment_attempt_id == attempt.id,
        )
        .order_by(ReconciliationRun.attempt_number, ReconciliationRun.created_at)
    ).scalars()
    return tuple(rows)


def _verify_streams(
    session: Session, tenant_id: uuid.UUID, checkout_id: uuid.UUID, attempt: Any
) -> dict[str, ChainVerification | None]:
    streams: dict[str, ChainVerification | None] = {
        "checkout": tk.verify_chain(
            session, tenant=tenant_id, aggregate_type="checkout", aggregate_id=checkout_id
        )
    }
    streams["payment_attempt"] = (
        None
        if attempt is None
        else tk.verify_chain(
            session, tenant=tenant_id, aggregate_type="payment_attempt", aggregate_id=attempt.id
        )
    )
    return streams


# ------------------------------------------------------------------------- projection


def _links(
    *,
    head: tk.CheckoutHead,
    version: tk.CheckoutVersionView | None,
    receipt: PolicyAtSaleReceipt | None,
    approval: Approval | None,
    decision: Mapping[str, Any] | None,
    grant: ExecutionGrant | None,
    command: OutboxRow | None,
    requests: Sequence[ProviderRequest],
    deliveries: Sequence[Any],
    runs: Sequence[ReconciliationRun],
    order: Order | None,
    attempt: Any,
    refunds: Sequence[Refund],
) -> dict[str, Any]:
    """The ten links as JSON-safe primitives. Hashes are full length here, not shorthand."""
    content = dict(version.content) if version is not None else {}
    return {
        "1_intent": None
        if decision is None
        else {
            "actor_type": decision["actor_type"],
            "principal_id": decision["principal_id"],
            "correlation_id": str(decision["correlation_id"]),
            "buyer_ref": head.buyer_ref[:8] + "…" if len(head.buyer_ref) > 8 else head.buyer_ref,
        },
        "2_merchant_state": None
        if version is None
        else {
            "policy_version": content.get("policy_version"),
            "catalogue_revision": content.get("catalogue_revision"),
            "source_id": content.get("source_id"),
            "line_items": content.get("line_items"),
            "observed_at": _iso(version.created_at),
        },
        "3_checkout": None
        if version is None
        else {
            "checkout_id": str(version.checkout_id),
            "version": version.version,
            "content_hash": version.content_hash,
            "content": content,
            "status": version.status.value,
            "amount_minor": version.total.minor,
            "currency": version.total.currency,
            "immutable": version.immutable,
            "invalidated_at": _iso(version.invalidated_at),
        },
        "4_policy_receipt": None
        if receipt is None
        else {
            "policy_receipt_id": str(receipt.id),
            "policy_receipt_hash": receipt.receipt_hash,
            "checkout_version": receipt.checkout_version,
            "created_at": _iso(receipt.created_at),
        },
        "5_approval": None
        if approval is None
        else {
            "approval_id": str(approval.id),
            "status": approval.status,
            "content_hash": approval.content_hash,
            "policy_receipt_hash": approval.policy_receipt_hash,
            "amount_minor": approval.amount_minor,
            "currency": approval.currency,
            "action": approval.action,
            "authority_id": None if approval.authority_id is None else str(approval.authority_id),
            "authority_epoch": approval.authority_epoch,
            "issued_at": _iso(approval.issued_at),
            "expires_at": _iso(approval.expires_at),
        },
        "6_kernel_decision": None
        if decision is None
        else {
            "decision_id": decision["payload"].get("decision_id"),
            "allowed": decision["allowed"],
            "code": decision["payload"].get("code", tk.RecoveryCode.OK.value),
            "explanation": decision["payload"].get("explanation", "admitted"),
            "next_version": decision["payload"].get("next_version"),
            "deltas": decision["payload"].get("deltas", []),
            "audit_event_id": decision["event_id"],
            "occurred_at": _iso(decision["occurred_at"]),
        },
        "7_grant_and_command": None
        if grant is None
        else {
            "grant_id": str(grant.id),
            "operation": grant.operation,
            "status": grant.status,
            "content_hash": grant.content_hash,
            "amount_minor": grant.amount_minor,
            "currency": grant.currency,
            "kernel_decision_id": str(grant.kernel_decision_id),
            "issued_at": _iso(grant.issued_at),
            "expires_at": _iso(grant.expires_at),
            "consumed_at": _iso(grant.consumed_at),
            "outbox_command_id": None
            if grant.outbox_command_id is None
            else str(grant.outbox_command_id),
            "command": None
            if command is None
            else {
                "command_type": command.command_type,
                "status": command.status,
                "attempts": command.attempts,
                "created_at": _iso(command.created_at),
                "payload": redact(command.payload),
            },
        },
        "8_provider_requests": [
            {
                "provider_request_id": str(row.id),
                "operation": row.operation,
                "method": row.method,
                "url": row.url,
                "body_hash": row.body_hash,
                "header_names": list(row.header_names),
                "http_status": row.http_status,
                "provider_id": row.provider_id,
                "outcome_code": row.outcome_code,
                "provider_error_code": row.provider_error_code,
                "response_digest": row.response_digest,
                "transport_error": row.transport_error,
                "grant_id": None if row.grant_id is None else str(row.grant_id),
                "refund_id": None if row.refund_id is None else str(row.refund_id),
                "request_at": _iso(row.request_at),
            }
            for row in requests
        ],
        "9_verified_evidence": {
            "capture_evidence": None if order is None else redact(dict(order.capture_evidence)),
            "webhook_deliveries": [
                {
                    "inbox_id": str(row.id),
                    "event_type": row.event_type,
                    "provider_event_id": row.provider_event_id,
                    "signature_verified": row.signature_verified,
                    "apply_status": row.apply_status,
                    "duplicate_count": row.duplicate_count,
                    "body_digest": row.body_digest,
                    "received_at": _iso(row.received_at),
                }
                for row in deliveries
            ],
            "reconciliation_runs": [
                {
                    "reconciliation_run_id": str(row.id),
                    "attempt_number": row.attempt_number,
                    "reason": row.reason,
                    "decision": row.decision,
                    "resulting_transition": row.resulting_transition,
                    "raw_evidence_digest": row.raw_evidence_digest,
                    "created_at": _iso(row.created_at),
                }
                for row in runs
            ],
        },
        "10_final_state": {
            "checkout_state": head.status.value,
            "payment": None
            if attempt is None
            else {
                "payment_attempt_id": str(attempt.id),
                "state": attempt.status,
                "amount_minor": attempt.amount_minor,
                "currency": attempt.currency,
                "receipt": attempt.receipt,
                "provider_order_id": attempt.provider_order_id,
                "provider_payment_id": attempt.provider_payment_id,
            },
            "order": None
            if order is None
            else {
                "order_id": str(order.id),
                "state": order.status,
                "amount_minor": order.total_minor,
                "currency": order.currency,
                "policy_receipt_hash": order.policy_receipt_hash,
                "created_at": _iso(order.created_at),
            },
            "refunds": [
                {
                    "refund_id": str(row.id),
                    "state": row.status,
                    "amount_minor": row.amount_minor,
                    "currency": row.currency,
                    "reason_code": row.reason_code,
                    "provider_refund_id": row.provider_refund_id,
                    "provider_originated": row.provider_originated,
                }
                for row in refunds
            ],
        },
    }


def _iso(moment: datetime | None) -> str | None:
    """RFC 3339 UTC, or ``None``. One renderer for every timestamp in this document."""
    return None if moment is None else rfc3339(moment)


# --------------------------------------------------------------------------- verifier


def _verify(
    *,
    tenant_id: uuid.UUID,
    head: tk.CheckoutHead,
    version: tk.CheckoutVersionView | None,
    receipt: PolicyAtSaleReceipt | None,
    approval: Approval | None,
    decision: Mapping[str, Any] | None,
    grant: ExecutionGrant | None,
    command: OutboxRow | None,
    requests: Sequence[ProviderRequest],
    order: Order | None,
    attempt: Any,
    refunds: Sequence[Refund],
    streams: Mapping[str, ChainVerification | None],
) -> Verdict:
    """Run every check the chain's reach allows, and name the tier it reached."""
    checks: list[Check] = []
    add = checks.append

    # --- link 3: the hash is recomputed, never trusted -------------------------------
    if version is None:
        add(Check("content_hash_recomputed", True, False, "no checkout version exists yet"))
    else:
        try:
            recomputed = tk.content_hash(version.content)
        except ContentContractError as exc:
            # A stored document the canonical builder would refuse cannot be hashed, and
            # that is itself the finding: a hash could still be computed over the bytes,
            # but it would be a hash of something no approval could have been recorded
            # against. Reported as a failure rather than raised, so the rest of the chain
            # is still visible to whoever has to investigate it.
            add(
                Check(
                    "content_hash_recomputed",
                    False,
                    True,
                    f"stored content is not a canonical checkout document: {exc}",
                )
            )
        else:
            add(
                Check(
                    "content_hash_recomputed",
                    recomputed == version.content_hash,
                    True,
                    f"recomputed {recomputed} against stored {version.content_hash}",
                )
            )

    # --- link 4: the receipt describes this version ----------------------------------
    if receipt is None or version is None:
        add(Check("receipt_bound_to_version", True, False, "no Policy-at-Sale Receipt yet"))
    else:
        bound = (
            receipt.receipt_hash == version.policy_receipt_hash
            and receipt.checkout_id == version.checkout_id
            and receipt.checkout_version == version.version
        )
        add(
            Check(
                "receipt_bound_to_version",
                bound,
                True,
                f"receipt {receipt.id} names checkout version {receipt.checkout_version}",
            )
        )

    # --- link 5: consent names the bytes the buyer was shown --------------------------
    if approval is None or version is None:
        add(Check("approval_binds_content", True, False, "no approval has been recorded"))
    else:
        bound = (
            approval.content_hash == version.content_hash
            and approval.amount_minor == version.total.minor
            and approval.currency == version.total.currency
        )
        add(
            Check(
                "approval_binds_content",
                bound,
                True,
                f"approval {approval.id} names hash {approval.content_hash} "
                f"for {approval.amount_minor} {approval.currency}",
            )
        )

    # --- link 6: the decision concerns this version -----------------------------------
    if decision is None or version is None:
        add(Check("decision_names_version", True, False, "admission has not been attempted"))
    else:
        payload = decision["payload"]
        add(
            Check(
                "decision_names_version",
                payload.get("content_hash") == version.content_hash
                and payload.get("version") == version.version,
                True,
                f"decision {payload.get('decision_id')} names version {payload.get('version')}",
            )
        )

    # --- link 7: the grant is bound to the decision that issued it --------------------
    if grant is None or version is None or attempt is None:
        add(Check("grant_binds_decision", True, False, "no Execution Grant was issued"))
        add(Check("grant_consumed_once", True, False, "no Execution Grant was issued"))
        add(Check("command_carries_grant", True, False, "no Execution Grant was issued"))
    else:
        decision_id = None if decision is None else decision["payload"].get("decision_id")
        add(
            Check(
                "grant_binds_decision",
                grant.content_hash == version.content_hash
                and grant.payment_attempt_id == attempt.id
                and grant.amount_minor == attempt.amount_minor
                and grant.currency == attempt.currency
                and (decision_id is None or str(grant.kernel_decision_id) == decision_id),
                True,
                f"grant {grant.id} binds decision {grant.kernel_decision_id}, "
                f"attempt {grant.payment_attempt_id}, hash {grant.content_hash}",
            )
        )
        mutations = [row for row in requests if row.grant_id == grant.id]
        consumed = grant.status == "CONSUMED" and grant.consumed_at is not None
        add(
            Check(
                "grant_consumed_once",
                consumed and len(mutations) <= 1,
                consumed or bool(mutations),
                f"grant status {grant.status}; {len(mutations)} provider request(s) "
                "recorded against it",
            )
        )
        add(
            Check(
                "command_carries_grant",
                command is not None and command.command_id == grant.outbox_command_id,
                grant.outbox_command_id is not None,
                "durable command "
                + (
                    f"{grant.outbox_command_id} carries this grant"
                    if command is not None
                    else "is missing from the outbox"
                ),
            )
        )

    # --- link 8: exactly one provider mutation, and it consumed a grant ---------------
    mutation_ops = {op.value for op in tk.Operation}
    mutations = [row for row in requests if row.operation in mutation_ops]
    ungranted = [row for row in mutations if row.grant_id is None]
    add(
        Check(
            "every_mutation_consumed_a_grant",
            not ungranted,
            bool(mutations),
            f"{len(mutations)} provider mutation(s), {len(ungranted)} without a grant",
        )
    )

    # --- link 9: capture came from a server-side channel (ADR D8) ---------------------
    if order is None:
        add(Check("capture_evidence_is_verified", True, False, "no order has been confirmed"))
    else:
        source = str(dict(order.capture_evidence).get("source", ""))
        channel = str(dict(order.capture_evidence).get("channel", ""))
        add(
            Check(
                "capture_evidence_is_verified",
                source in _SUFFICIENT_CAPTURE_SOURCES or channel in _SUFFICIENT_CAPTURE_SOURCES,
                True,
                f"order confirmed from source {source!r} / channel {channel!r}; "
                "a browser callback is never sufficient",
            )
        )

    # --- amounts, currency, tenancy ---------------------------------------------------
    add(_amount_check(version, approval, grant, attempt, order))
    add(_tenancy_check(tenant_id, head, receipt, approval, grant, attempt, order))
    add(_ordering_check(version, receipt, approval, grant, requests, order))
    add(_final_state_check(attempt, order, refunds))

    # --- the audit chains themselves ---------------------------------------------------
    for name, result in streams.items():
        if result is None:
            add(Check(f"audit_chain_{name}", True, False, "stream does not exist yet"))
            continue
        detail = (
            f"{result.events_verified} of {result.length} events verified"
            if result.intact
            else f"{result.first_break.kind.value} at seq {result.first_break.at_seq}"
            if result.first_break is not None
            else "broken"
        )
        add(Check(f"audit_chain_{name}", result.intact, not result.empty, detail))

    tier = _tier(
        version=version,
        receipt=receipt,
        approval=approval,
        decision=decision,
        grant=grant,
        requests=requests,
        order=order,
    )
    ok = all(check.ok for check in checks if check.applicable)
    return Verdict(tier=tier, ok=ok, checks=tuple(checks))


def _amount_check(
    version: tk.CheckoutVersionView | None,
    approval: Approval | None,
    grant: ExecutionGrant | None,
    attempt: Any,
    order: Order | None,
) -> Check:
    """One amount and one currency from the quote to the confirmed order.

    Compared as integer minor units beside the ISO code. Nothing converts or rounds: this
    is the check that catches a currency mismatch pretending to be an amount match.
    """
    amounts: list[tuple[str, int, str]] = []
    if version is not None:
        amounts.append(("checkout_version", version.total.minor, version.total.currency))
    if approval is not None:
        amounts.append(("approval", approval.amount_minor, approval.currency))
    if grant is not None:
        amounts.append(("execution_grant", grant.amount_minor, grant.currency))
    if attempt is not None:
        amounts.append(("payment_attempt", attempt.amount_minor, attempt.currency))
    if order is not None:
        amounts.append(("order", order.total_minor, order.currency))
    if len(amounts) < 2:
        return Check("amounts_agree", True, False, "fewer than two rows carry an amount")
    distinct = {(minor, currency) for _, minor, currency in amounts}
    return Check(
        "amounts_agree",
        len(distinct) == 1,
        True,
        "; ".join(f"{name}={minor} {currency}" for name, minor, currency in amounts),
    )


def _tenancy_check(
    tenant_id: uuid.UUID,
    head: tk.CheckoutHead,
    receipt: PolicyAtSaleReceipt | None,
    approval: Approval | None,
    grant: ExecutionGrant | None,
    attempt: Any,
    order: Order | None,
) -> Check:
    """Every row belongs to this tenant, and to this checkout's merchant.

    Row-level security already scopes the reads. This asserts it a second time on the
    values themselves, because "RLS was on" is a claim about configuration and this is a
    claim about the rows in front of the reviewer.
    """
    mismatches: list[str] = []
    for name, row_tenant in (
        ("checkout", head.tenant_id),
        ("policy_receipt", None if receipt is None else receipt.tenant_id),
        ("approval", None if approval is None else approval.tenant_id),
        ("execution_grant", None if grant is None else grant.tenant_id),
        ("payment_attempt", None if attempt is None else attempt.tenant_id),
        ("order", None if order is None else order.tenant_id),
    ):
        if row_tenant is not None and row_tenant != tenant_id:
            mismatches.append(name)
    for name, merchant in (
        ("policy_receipt", None if receipt is None else receipt.merchant_id),
        ("order", None if order is None else order.merchant_id),
    ):
        if merchant is not None and merchant != head.merchant_id:
            mismatches.append(f"{name}.merchant")
    return Check(
        "tenant_and_merchant_correlate",
        not mismatches,
        True,
        "every row belongs to this tenant and merchant"
        if not mismatches
        else f"mismatched: {', '.join(mismatches)}",
    )


def _ordering_check(
    version: tk.CheckoutVersionView | None,
    receipt: PolicyAtSaleReceipt | None,
    approval: Approval | None,
    grant: ExecutionGrant | None,
    requests: Sequence[ProviderRequest],
    order: Order | None,
) -> Check:
    """Evidence happened in the order it claims.

    Compared with ``<=`` rather than ``<``: several of these rows are written in one
    transaction and share its timestamp, which is correct and must not read as a fault.
    A strictly-increasing rule here would fail every honest chain.
    """
    stages: list[tuple[str, datetime]] = []
    if version is not None:
        stages.append(("version", version.created_at))
    if receipt is not None:
        stages.append(("receipt", receipt.created_at))
    if approval is not None:
        stages.append(("approval", approval.issued_at))
    if grant is not None:
        stages.append(("grant", grant.issued_at))
    if requests:
        stages.append(("provider_request", requests[0].request_at))
    if order is not None:
        stages.append(("order", order.created_at))
    if len(stages) < 2:
        return Check("evidence_in_order", True, False, "fewer than two dated rows")
    out_of_order = [
        f"{stages[i][0]} after {stages[i + 1][0]}"
        for i in range(len(stages) - 1)
        if stages[i][1] > stages[i + 1][1]
    ]
    return Check(
        "evidence_in_order",
        not out_of_order,
        True,
        " -> ".join(name for name, _ in stages)
        if not out_of_order
        else f"out of order: {', '.join(out_of_order)}",
    )


def _final_state_check(attempt: Any, order: Order | None, refunds: Sequence[Refund]) -> Check:
    """The final states agree with each other.

    Two rules, and both have been real bugs elsewhere: a captured payment must have
    produced exactly one order, and refunds must not exceed what was captured. A
    ``STALE_CAPTURE`` deliberately has *no* order (specification 10.8) and is checked as
    such rather than treated as a missing one.
    """
    if attempt is None:
        return Check("final_state_consistent", True, False, "no payment attempt exists")
    state = str(attempt.status)
    problems: list[str] = []
    if state == tk.PaymentState.CAPTURED.value and order is None:
        problems.append("payment is CAPTURED but no order was confirmed")
    if state == tk.PaymentState.STALE_CAPTURE.value and order is not None:
        problems.append("a stale capture must never confirm an order")
    if order is not None and state not in (
        tk.PaymentState.CAPTURED.value,
        tk.PaymentState.PARTIALLY_REFUNDED.value,
        tk.PaymentState.REFUNDED.value,
        tk.PaymentState.REFUND_PENDING.value,
        tk.PaymentState.REFUND_UNKNOWN.value,
        tk.PaymentState.REFUND_FAILED.value,
        tk.PaymentState.AUTO_REFUND_PENDING.value,
    ):
        problems.append(f"an order exists while the payment reads {state}")
    settled = sum(row.amount_minor for row in refunds if row.status in ("PROCESSED", "PENDING"))
    if settled > int(attempt.amount_minor):
        problems.append(f"refunds total {settled} against a capture of {attempt.amount_minor}")
    return Check(
        "final_state_consistent",
        not problems,
        True,
        f"payment {state}, order {'confirmed' if order else 'absent'}, {len(refunds)} refund(s)"
        if not problems
        else "; ".join(problems),
    )


def _tier(
    *,
    version: tk.CheckoutVersionView | None,
    receipt: PolicyAtSaleReceipt | None,
    approval: Approval | None,
    decision: Mapping[str, Any] | None,
    grant: ExecutionGrant | None,
    requests: Sequence[ProviderRequest],
    order: Order | None,
) -> ProofTier:
    if version is None:
        return ProofTier.EMPTY
    if receipt is None or approval is None:
        return ProofTier.PROPOSED
    if decision is None or grant is None:
        return ProofTier.APPROVED
    if not requests:
        return ProofTier.ADMITTED
    if order is None:
        return ProofTier.EXECUTED
    return ProofTier.COMPLETE


# ------------------------------------------------------------------- retained revenue


@dataclass(frozen=True, slots=True)
class RetainedRevenue:
    """Step 11: what refusing a stale approval was worth, in committed rows.

    ``direction`` names who the difference protected, because the arithmetic alone does
    not: a corrected total *above* the stale one is revenue the merchant would have lost,
    and one *below* it is money the buyer would have overpaid. Reporting both as
    "retained" would turn a correctness guarantee into a sales figure.
    """

    checkout_id: uuid.UUID
    merchant_id: uuid.UUID
    currency: str
    stale_version: int | None
    stale_approved_minor: int | None
    stale_invalidated_at: datetime | None
    corrected_version: int | None
    corrected_total_minor: int | None
    captured_minor: int | None
    captured_from: str | None
    difference_minor: int | None
    direction: str
    refunded_minor: int
    net_retained_minor: int | None
    controlled_scenario: bool
    explanation: str


def retained_revenue(
    session: Session, *, tenant_id: uuid.UUID, merchant_id: uuid.UUID, checkout_id: uuid.UUID
) -> RetainedRevenue | None:
    """Derive step 11's figures for one checkout. Every number comes from a row.

    The stale version is the newest one that carried an approval and was then invalidated
    -- the approval the kernel refused to honour. The corrected version is the one that
    was actually paid for. The captured amount is read from the ``orders`` row, which the
    kernel writes only from verified capture evidence, so this figure can never be a
    browser's claim about a payment.

    Returns ``None`` when the checkout is not visible to this tenant or belongs to another
    merchant. A checkout that never had a stale approval still answers, with the stale
    figures null: "nothing was retained here" is an honest result and the demonstration
    needs to be able to show it beside the one where something was.
    """
    head = tk.read_head(session, tenant_id=tenant_id, checkout_id=checkout_id)
    if head is None or head.merchant_id != merchant_id:
        return None

    versions = tk.read_versions(session, tenant_id=tenant_id, checkout_id=checkout_id)
    approvals = _approvals(session, tenant_id, checkout_id)
    approved_versions = {a.checkout_version for a in approvals}

    stale = next(
        (
            v
            for v in reversed(versions)
            if v.invalidated_at is not None and v.version in approved_versions
        ),
        None,
    )
    corrected = _paid_version(session, tenant_id, checkout_id, versions)
    attempt = _captured_attempt(session, tenant_id, checkout_id)
    order = _order(session, tenant_id, attempt)
    refunds = _refunds(session, tenant_id, attempt)

    currency = versions[-1].total.currency if versions else _merchant_currency(session, merchant_id)
    stale_minor = stale.total.minor if stale is not None else None
    captured_minor = order.total_minor if order is not None else None
    captured_from = (
        None if order is None else str(dict(order.capture_evidence).get("source", "unknown"))
    )
    refunded = sum(r.amount_minor for r in refunds if r.status == "PROCESSED")

    difference = (
        captured_minor - stale_minor
        if captured_minor is not None and stale_minor is not None
        else None
    )
    direction, explanation = _direction(stale, corrected, difference, captured_minor)
    net = None if captured_minor is None else captured_minor - refunded

    return RetainedRevenue(
        checkout_id=checkout_id,
        merchant_id=merchant_id,
        currency=currency,
        stale_version=None if stale is None else stale.version,
        stale_approved_minor=stale_minor,
        stale_invalidated_at=None if stale is None else stale.invalidated_at,
        corrected_version=None if corrected is None else corrected.version,
        corrected_total_minor=None if corrected is None else corrected.total.minor,
        captured_minor=captured_minor,
        captured_from=captured_from,
        difference_minor=difference,
        direction=direction,
        refunded_minor=refunded,
        net_retained_minor=net,
        # Specification 9.3: every figure produced by the scenario controller is labelled.
        # The demonstration injects the price change, so this is always a controlled run
        # until an organic merchant connector is wired in.
        controlled_scenario=True,
        explanation=explanation,
    )


def _direction(
    stale: tk.CheckoutVersionView | None,
    corrected: tk.CheckoutVersionView | None,
    difference: int | None,
    captured: int | None,
) -> tuple[str, str]:
    if stale is None:
        return (
            "NONE",
            "No approved version was invalidated on this checkout, so nothing was retained "
            "by refusing one.",
        )
    if difference is None or captured is None:
        return (
            "UNSETTLED",
            f"Version {stale.version}'s approval was invalidated, but no capture has been "
            "verified yet, so no difference can be stated.",
        )
    corrected_version = "unknown" if corrected is None else str(corrected.version)
    if difference > 0:
        return (
            "MERCHANT",
            f"Version {stale.version} was approved and invalidated before any provider "
            f"order existed. Version {corrected_version} was approved afresh and captured, "
            f"so the merchant retained {difference} minor units it would have lost by "
            "honouring the stale approval.",
        )
    if difference < 0:
        return (
            "BUYER",
            f"Version {stale.version}'s stale approval was higher than the corrected total. "
            f"Refusing it saved the buyer {-difference} minor units; the merchant retained "
            "nothing here, and reporting this as merchant revenue would be false.",
        )
    return (
        "NEUTRAL",
        f"The corrected total equalled version {stale.version}'s. Refusing the stale "
        "approval changed no amount; it prevented a payment against invalidated terms.",
    )


def _paid_version(
    session: Session,
    tenant_id: uuid.UUID,
    checkout_id: uuid.UUID,
    versions: Sequence[tk.CheckoutVersionView],
) -> tk.CheckoutVersionView | None:
    """The version an order was confirmed against, or the newest live one."""
    order = session.execute(
        select(Order)
        .where(Order.tenant_id == tenant_id, Order.checkout_id == checkout_id)
        .order_by(Order.created_at)
        .limit(1)
    ).scalar_one_or_none()
    if order is not None:
        for version in versions:
            if version.version == order.checkout_version:
                return version
    return next((v for v in reversed(versions) if v.invalidated_at is None), None)


def _captured_attempt(session: Session, tenant_id: uuid.UUID, checkout_id: uuid.UUID) -> Any:
    """The attempt an order was confirmed against, else the newest attempt."""
    attempts = attempts_of(session, tenant_id=tenant_id, checkout_id=checkout_id)
    for attempt in reversed(attempts):
        if _order(session, tenant_id, attempt) is not None:
            return attempt
    return attempts[-1] if attempts else None


def _merchant_currency(session: Session, merchant_id: uuid.UUID) -> str:
    """Fallback currency for a checkout with no versions. Never guessed as INR."""
    row = session.execute(
        select(Merchant.currency).where(Merchant.id == merchant_id)
    ).scalar_one_or_none()
    return str(row) if row is not None else ""
