"""Durable Reserve simulator. No NPCI/bank connection; never used in production.

The consumed grant and simulated provider acceptance commit together because this
provider is local. Redelivery reads its durable outcome and never re-admits a debit.
A future network adapter MUST use a committed send boundary and provider lookup.
"""

from __future__ import annotations

import uuid

import transaction_kernel as tk
from commerce_domain import ActorType, RecoveryCode, canonicalize, sha256_hex
from durable_work.commands import ReserveDebitCommand, ReserveReconcileCommand, enqueue_command
from platform_db import set_tenant
from sqlalchemy import text
from transaction_kernel import authority, reservations, safe_mode
from transaction_kernel.evidence import EvidenceSource, ProviderEvidence
from transaction_kernel.grants import (
    GrantAlreadyConsumedError,
    GrantExpiredError,
    GrantRevokedError,
)
from transaction_kernel.payments import ProviderOrderOutcome
from transaction_kernel.reserve import lock_allocation_context, settle_allocation

from ..settings import WorkerRuntime
from . import HandlerResult


def handle_reserve(runtime: WorkerRuntime, command: ReserveDebitCommand) -> HandlerResult:
    if runtime.settings.profile.is_production:
        raise RuntimeError("Reserve simulator is disabled in production")
    tenant = uuid.UUID(command.tenant_id)
    attempt = uuid.UUID(command.payment_attempt_id)
    correlation = uuid.UUID(command.correlation_id)
    checkout = uuid.UUID(command.checkout_id)
    with runtime.kernel_session() as session:
        set_tenant(session, tenant)
        safe_mode.lock_money_action(session, tenant)
        # Same checkout -> authority -> reservation -> attempt order as admission.
        version = session.execute(
            text(
                "SELECT version, content_hash FROM checkout_versions WHERE tenant_id=:t "
                "AND checkout_id=:c AND version=:v FOR UPDATE"
            ),
            {"t": tenant, "c": checkout, "v": command.checkout_version},
        ).one()
        lock_allocation_context(session, attempt)
        reservations.check_validity(
            session, checkout_id=checkout, checkout_version=command.checkout_version, lock=True
        )
        row = session.execute(
            text("SELECT * FROM payment_attempts WHERE tenant_id=:t AND id=:p FOR UPDATE"),
            {"t": tenant, "p": attempt},
        ).one()
        if (
            row.checkout_id != checkout
            or row.checkout_version != command.checkout_version
            or row.amount_minor != command.amount_minor
            or row.currency != command.currency
            or version.content_hash != command.content_hash
        ):
            raise RuntimeError("Reserve command differs from the admitted purchase")
        if row.reserve_authority_id is None:
            raise RuntimeError("Reserve command does not name a Reserve attempt")
        if row.reserve_allocation != "HELD":
            return HandlerResult(code=RecoveryCode.OK, detail="reserve_already_settled")
        snapshot = authority.lock_authority(session, row.reserve_authority_id)
        # Only CREATED has not yet reached the simulated provider. Revocation cannot
        # erase an accepted debit or turn an uncertain debit into a known failure.
        if row.status == "CREATED" and isinstance(command, ReserveReconcileCommand):
            return HandlerResult(code=RecoveryCode.OK, detail="reserve_send_not_reached")
        if row.status == "CREATED":
            from transaction_kernel.reserve_proofs import verify_authority

            valid = (
                verify_authority(session, row.reserve_authority_id)
                and snapshot is not None
                and not snapshot.expired
                and snapshot.revocation_epoch == row.reserve_authority_epoch
                and snapshot.status
                in (authority.AuthorityStatus.ACTIVE, authority.AuthorityStatus.EXHAUSTED)
            )
            if not valid:
                tk.record_create_order_result(
                    session,
                    tenant_id=tenant,
                    payment_attempt_id=attempt,
                    outcome=ProviderOrderOutcome(
                        "failed",
                        None,
                        RecoveryCode.AUTHORITY_INSUFFICIENT,
                        "reserve_authority_lapsed_before_send",
                    ),
                    correlation_id=correlation,
                )
                settle_allocation(session, attempt, correlation_id=correlation)
                return HandlerResult(code=RecoveryCode.OK, detail="reserve_refused_before_send")
            try:
                tk.consume_grant(session, uuid.UUID(command.grant_id), command.grant_binding())
            except GrantAlreadyConsumedError:
                # This simulator commits consumption with acceptance. A consumed grant
                # on CREATED is inconsistent state; never manufacture success or re-send.
                raise RuntimeError(
                    "Consumed Reserve grant has no durable simulator acceptance"
                ) from None
            except GrantExpiredError, GrantRevokedError:
                tk.record_create_order_result(
                    session,
                    tenant_id=tenant,
                    payment_attempt_id=attempt,
                    outcome=ProviderOrderOutcome(
                        "failed", None, RecoveryCode.AUTHORITY_INSUFFICIENT, "reserve_grant_refused"
                    ),
                    correlation_id=correlation,
                )
                settle_allocation(session, attempt, correlation_id=correlation)
                return HandlerResult(code=RecoveryCode.OK, detail="reserve_grant_refused")
            tk.append(
                session,
                tenant=tenant,
                aggregate_type="checkout",
                aggregate_id=checkout,
                event_type="reserve.simulated_provider_accepted",
                actor_type=ActorType.WORKER,
                principal_id=runtime.settings.worker_id,
                correlation_id=correlation,
                payload={
                    "simulated": True,
                    "payment_attempt_id": str(attempt),
                    "authority_id": str(row.reserve_authority_id),
                    "authority_epoch": row.reserve_authority_epoch,
                    "grant_id": command.grant_id,
                    "content_hash": command.content_hash,
                },
            )
            order_id = f"sim_order_{attempt.hex}"
            tk.record_provider_request(
                session,
                tenant_id=tenant,
                payment_attempt_id=attempt,
                grant_id=uuid.UUID(command.grant_id),
                refund_id=None,
                operation=tk.Operation.RESERVE_DEBIT,
                method="POST",
                url="https://reserve-simulator.invalid/debits",
                body_hash=sha256_hex(canonicalize(command.to_payload())),
                header_names=[],
                http_status=200,
                provider_id=order_id,
                outcome_code=RecoveryCode.OK,
                provider_error_code=None,
                response_digest=None,
                transport_error="SIMULATED_NO_NETWORK",
                correlation_id=correlation,
            )
            tk.record_create_order_result(
                session,
                tenant_id=tenant,
                payment_attempt_id=attempt,
                outcome=ProviderOrderOutcome(
                    "unknown" if row.reserve_simulation_outcome == "unknown" else "ok",
                    None if row.reserve_simulation_outcome == "unknown" else order_id,
                    RecoveryCode.OK,
                    "simulated_reserve_accepted",
                ),
                correlation_id=correlation,
            )
        else:
            order_id = row.provider_order_id
        outcome = row.reserve_simulation_outcome
        if outcome == "unknown":
            # Known provider debit identity, unknown settlement: retain HELD and do not
            # submit again. A protected simulator update queues another read of it.
            tk.append(
                session,
                tenant=tenant,
                aggregate_type="checkout",
                aggregate_id=checkout,
                event_type="reserve.simulated_outcome_unknown",
                actor_type=ActorType.WORKER,
                principal_id=runtime.settings.worker_id,
                correlation_id=correlation,
                payload={"simulated": True, "payment_attempt_id": str(attempt)},
            )
            round_number = command.round if isinstance(command, ReserveReconcileCommand) else 0
            if round_number < 6:
                payload = command.to_payload()
                payload["round"] = round_number + 1
                enqueue_command(
                    session,
                    ReserveReconcileCommand.from_payload(payload),
                    idempotency_key=None,
                    available_in_seconds=5 * (round_number + 1),
                )
            return HandlerResult(
                code=RecoveryCode.OK, detail="reserve_outcome_unknown_capacity_held"
            )
        if outcome not in ("captured", "failed"):
            raise RuntimeError("Missing simulated provider outcome")
        if row.status in ("UNKNOWN", "RECONCILING"):
            tk.begin_reconciling(
                session, tenant_id=tenant, payment_attempt_id=attempt, correlation_id=correlation
            )
            order_id = f"sim_order_{attempt.hex}"
            tk.record_recovered_order(
                session,
                tenant_id=tenant,
                payment_attempt_id=attempt,
                provider_order_id=order_id,
                correlation_id=correlation,
            )
        tk.apply_provider_evidence(
            session,
            tenant_id=tenant,
            payment_attempt_id=attempt,
            evidence=ProviderEvidence(
                source=EvidenceSource.PROVIDER_FETCH,
                provider_payment_id=f"sim_pay_{attempt.hex}",
                provider_order_id=order_id,
                amount_minor=command.amount_minor,
                currency=command.currency,
                status=outcome,
                provider_status=f"SIMULATED_{outcome.upper()}",
                raw_digest=sha256_hex(f"simulated:{attempt}:{outcome}".encode()),
                method="simulated_uap",
                http_status=200,
            ),
            correlation_id=correlation,
        )
        settle_allocation(session, attempt, correlation_id=correlation)
    return HandlerResult(code=RecoveryCode.OK, detail=f"reserve_simulated_{outcome}")
