"""Close the buyer's payment window without inventing a provider failure.

The persisted deadline is immutable for an attempt. Window closure retires fulfilment;
any later capture follows the existing stale-capture/refund path. Financial allocation
remains held for uncertain outcomes. Legacy attempts with no deadline are unchanged.
"""

from __future__ import annotations

import uuid

from commerce_domain import ActorType, AgentPrincipal, CheckoutRef, RecoveryCode
from platform_db import require_tenant
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import audit, checkouts, payments, reservations
from .reserve import lock_allocation_context, settle_allocation
from .states import CheckoutState


def close_due(session: Session, attempt_id: uuid.UUID, *, correlation_id: uuid.UUID) -> bool:
    tenant = require_tenant(session)
    located = session.execute(
        text(
            "SELECT checkout_id, checkout_version FROM payment_attempts "
            "WHERE tenant_id=:t AND id=:p"
        ),
        {"t": tenant, "p": attempt_id},
    ).one_or_none()
    if located is None:
        return False
    version = session.execute(
        text(
            "SELECT content_hash, status FROM checkout_versions WHERE tenant_id=:t "
            "AND checkout_id=:c AND version=:v FOR UPDATE"
        ),
        {"t": tenant, "c": located.checkout_id, "v": located.checkout_version},
    ).one()
    lock_allocation_context(session, attempt_id)
    session.execute(
        text(
            "SELECT id FROM reservations WHERE tenant_id=:t AND checkout_id=:c "
            "AND checkout_version=:v FOR UPDATE"
        ),
        {"t": tenant, "c": located.checkout_id, "v": located.checkout_version},
    ).all()
    row = session.execute(
        text(
            "SELECT *, payment_window_expires_at <= clock_timestamp() AS due "
            "FROM payment_attempts WHERE tenant_id=:t AND id=:p FOR UPDATE"
        ),
        {"t": tenant, "p": attempt_id},
    ).one()
    if (
        not row.due
        or row.payment_window_closed_at is not None
        or row.status not in ("CREATED", "SUBMITTED", "AUTHORIZED", "UNKNOWN", "RECONCILING")
    ):
        return False
    grants = session.execute(
        text(
            "SELECT status FROM execution_grants WHERE tenant_id=:t AND payment_attempt_id=:p "
            "AND operation IN ('PAYMENT_CREATE_ORDER', 'RESERVE_DEBIT') FOR UPDATE"
        ),
        {"t": tenant, "p": attempt_id},
    ).all()
    sent = any(g.status == "CONSUMED" for g in grants) or row.status != "CREATED"
    ref = CheckoutRef(located.checkout_id, located.checkout_version, version.content_hash)
    if not sent:
        result = checkouts.cancel(
            session,
            tenant_id=tenant,
            checkout=ref,
            principal=AgentPrincipal("payment-window", tenant, ActorType.SYSTEM),
            reason="payment_window_expired_before_execution",
            correlation_id=correlation_id,
        )
        if not result.allowed:
            return False
        settle_allocation(session, attempt_id, correlation_id=correlation_id)
    else:
        state = CheckoutState(version.status)
        if row.status == "CREATED":
            payments.record_create_order_result(
                session,
                tenant_id=tenant,
                payment_attempt_id=attempt_id,
                correlation_id=correlation_id,
                outcome=payments.ProviderOrderOutcome(
                    "unknown",
                    None,
                    RecoveryCode.PAYMENT_UNKNOWN,
                    "payment_window_closed_during_execution",
                ),
            )
            state = CheckoutState.PAYMENT_UNKNOWN
        if state in (CheckoutState.AWAITING_PAYMENT, CheckoutState.PAYMENT_UNKNOWN):
            checkouts.invalidate_open(
                session,
                tenant_id=tenant,
                checkout=ref,
                reason="payment_window_expired",
                correlation_id=correlation_id,
            )
        elif state is not CheckoutState.INVALIDATED_AWAITING_PAYMENT_RESULT:
            return False
        # Fulfilment is now forbidden even if a late capture arrives. The financial
        # allocation is NOT released; the old purchase can only reconcile/refund.
        reservations.release(
            session,
            checkout_id=ref.checkout_id,
            checkout_version=ref.version,
            cause=reservations.ReleaseCause.CANCELLED,
        )
    session.execute(
        text(
            "UPDATE payment_attempts SET payment_window_closed_at=clock_timestamp() "
            "WHERE tenant_id=:t AND id=:p"
        ),
        {"t": tenant, "p": attempt_id},
    )
    audit.append(
        session,
        tenant=tenant,
        aggregate_type="checkout",
        aggregate_id=ref.checkout_id,
        event_type="checkout.payment_window_closed",
        actor_type=ActorType.SYSTEM,
        principal_id="payment-window",
        correlation_id=correlation_id,
        payload={
            "payment_attempt_id": str(attempt_id),
            "provider_outcome_unknown": sent,
            "deadline": row.payment_window_expires_at.isoformat(),
        },
    )
    return True
