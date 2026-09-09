"""Buyer-owned Reserve permissions and exact reviewed-bill submission.

Provider execution is a server-side simulator in development/demo only. A model cannot
create permission, approve a bill or choose a simulated provider result through this API.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

import transaction_kernel.reserve as tk_reserve
from commerce_domain import ActorType, Money
from durable_work.commands import ReserveReconcileCommand, enqueue_command
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session
from transaction_kernel import Operation, append, authority, safe_mode

from ..deps import (
    IdempotencyKey,
    KernelSession,
    RequestContext,
    SessionContext,
    assert_owner,
    merchant_registry,
    require_scenario_key,
    settings_of,
)
from ..errors import ProblemError
from ..idempotency import idempotent_mutation, request_fingerprint
from ..merchants import MerchantRegistry
from ..services import admission_service

router = APIRouter(prefix="/v1/reserve", tags=["reserve"])
Registry = Annotated[MerchantRegistry, Depends(merchant_registry)]


def buyer_only(ctx: RequestContext) -> None:
    if ctx.actor_type is not ActorType.BUYER or not ctx.buyer_ref:
        raise ProblemError(403, "Buyer required", "Only the buyer may manage Reserve permission.")


def simulation_only(request: Request) -> None:
    if settings_of(request).profile.is_production:
        raise ProblemError(404, "Not available", "Reserve provider integration is not enabled.")


def owned(
    session: Session, ctx: RequestContext, authority_id: uuid.UUID
) -> authority.AuthoritySnapshot:
    buyer_only(ctx)
    snapshot = authority.lock_authority(session, authority_id)
    if (
        snapshot is None
        or snapshot.buyer_ref != ctx.buyer_ref
        or snapshot.merchant_id != ctx.merchant_id
        or snapshot.kind is not authority.AuthorityKind.RESERVE
    ):
        raise ProblemError(404, "Permission not found", "No permission exists for this buyer.")
    return snapshot


def present(snapshot: authority.AuthoritySnapshot) -> dict[str, Any]:
    return {
        "authority_id": str(snapshot.authority_id),
        "epoch": snapshot.revocation_epoch,
        "status": "EXPIRED" if snapshot.expired else snapshot.status.value,
        # ``None`` rather than ``[]``, because a client has to be able to tell "covers
        # everything" from "covers a list", and `[]` reads as the second while meaning the
        # first. It is never ambiguous: an empty scope cannot be stored -- the Kernel
        # refuses it and the table's CHECK requires 1 to 100 entries -- so a null here has
        # exactly one meaning.
        "allowed_skus": None if snapshot.allowed_skus is None else sorted(snapshot.allowed_skus),
        "per_purchase_limit_minor": snapshot.per_purchase_limit.minor
        if snapshot.per_purchase_limit
        else None,
        "capacity_minor": snapshot.max_amount.minor,
        "allocated_minor": snapshot.consumed_amount.minor,
        "available_minor": snapshot.remaining.minor,
        "currency": snapshot.currency,
        "expires_at": snapshot.expires_at.isoformat(),
        "provider_mode": "SIMULATED",
    }


class PermissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    #: Which products this permission covers, or ``None`` for every product this
    #: merchant sells.
    #:
    #: Optional because the Kernel has always treated a null scope as "not scoped by
    #: product" (``authority.check_authority`` skips the test entirely when
    #: ``allowed_skus`` is None, and the table's CHECK is ``IS NULL OR 1..100``). Only
    #: this field made it unreachable: it was required with ``min_length=1``, so the API
    #: could not express the permission the storage and the Kernel were both already
    #: built to hold.
    #:
    #: Omitting it removes the *product* bound and nothing else. The per-purchase limit,
    #: the capacity, the buyer and merchant scope, the expiry, the revocation epoch and
    #: Safe Mode are all unchanged, and each is still checked on every debit.
    allowed_skus: list[Annotated[str, Field(min_length=1, max_length=128)]] | None = Field(
        default=None, min_length=1, max_length=100
    )
    per_purchase_limit_minor: int = Field(ge=100, le=500000)
    capacity_minor: int = Field(ge=100, le=10000000)
    validity_days: int = Field(ge=1, le=30, default=7)

    @model_validator(mode="after")
    def validate_bounds(self) -> PermissionRequest:
        if self.capacity_minor < self.per_purchase_limit_minor:
            raise ValueError("Capacity must cover the per-purchase limit")
        if self.allowed_skus is not None and len(set(self.allowed_skus)) != len(self.allowed_skus):
            raise ValueError("Selected products must be unique")
        return self


@router.post("/authorities")
def create_permission(
    body: PermissionRequest,
    ctx: SessionContext,
    session: KernelSession,
    key: IdempotencyKey,
    registry: Registry,
    request: Request,
) -> dict[str, Any]:
    simulation_only(request)
    buyer_only(ctx)
    # Only a scope that was actually named can name an unknown product. An unscoped
    # permission covers this merchant's catalogue as it stands at each debit, which is
    # the live registry the Kernel prices against rather than a list frozen here.
    if body.allowed_skus is not None and not set(body.allowed_skus).issubset(
        set(registry.store(ctx.merchant_id).all_skus())
    ):
        raise ProblemError(
            422, "Unknown product", "Select products from this merchant's catalogue."
        )
    with idempotent_mutation(session, ctx, key, "RESERVE_AUTHORISE", body.model_dump()) as slot:
        permitted, code = safe_mode.is_permitted(
            session, ctx.tenant_id, safe_mode.GuardedOperation.DELEGATED_AUTHORITY_CREATE
        )
        if not permitted:
            raise ProblemError(
                409, "Safe Mode", "New Reserve permissions are paused.", code=code.value
            )
        identifier = authority.grant_authority(
            session,
            tenant_id=ctx.tenant_id,
            merchant_id=ctx.merchant_id,
            buyer_ref=str(ctx.buyer_ref),
            kind=authority.AuthorityKind.RESERVE,
            max_amount=Money(body.capacity_minor, "INR"),
            per_purchase_limit=Money(body.per_purchase_limit_minor, "INR"),
            allowed_skus=None if body.allowed_skus is None else frozenset(body.allowed_skus),
            ttl_seconds=body.validity_days * 86400,
        )
        result = present(owned(session, ctx, identifier))
        append(
            session,
            tenant=ctx.tenant_id,
            aggregate_type="authority",
            aggregate_id=identifier,
            event_type="reserve.permission_created",
            actor_type=ctx.actor_type,
            principal_id=ctx.principal.principal_id,
            correlation_id=ctx.correlation_id,
            payload=result,
        )
        slot.store(result)
    return result


@router.get("/authorities")
def list_permissions(ctx: SessionContext, session: KernelSession) -> dict[str, Any]:
    buyer_only(ctx)
    ids = session.execute(
        text(
            "SELECT id FROM delegated_authorities WHERE tenant_id=:t "
            "AND merchant_id=:m AND buyer_ref=:b AND kind='RESERVE' "
            "ORDER BY expires_at DESC LIMIT 100"
        ),
        {"t": ctx.tenant_id, "m": ctx.merchant_id, "b": ctx.buyer_ref},
    ).scalars()
    return {"authorities": [present(owned(session, ctx, i)) for i in list(ids)]}


@router.get("/authorities/{authority_id}")
def read_permission(
    authority_id: uuid.UUID, ctx: SessionContext, session: KernelSession
) -> dict[str, Any]:
    return present(owned(session, ctx, authority_id))


@router.post("/authorities/{authority_id}/revoke")
def revoke_permission(
    authority_id: uuid.UUID, ctx: SessionContext, session: KernelSession, key: IdempotencyKey
) -> dict[str, Any]:
    buyer_only(ctx)
    with idempotent_mutation(
        session, ctx, key, "RESERVE_REVOKE", {"authority_id": str(authority_id)}
    ) as slot:
        owned(session, ctx, authority_id)
        authority.revoke(session, authority_id)
        result = present(owned(session, ctx, authority_id))
        append(
            session,
            tenant=ctx.tenant_id,
            aggregate_type="authority",
            aggregate_id=authority_id,
            event_type="reserve.permission_revoked",
            actor_type=ctx.actor_type,
            principal_id=ctx.principal.principal_id,
            correlation_id=ctx.correlation_id,
            payload={"authority_id": str(authority_id), "epoch": result["epoch"]},
        )
        slot.store(result)
    return result


class ReservePaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    authority_id: uuid.UUID
    authority_epoch: int = Field(ge=0)
    content_hash: str = Field(min_length=1, max_length=128)
    amount_minor: int = Field(gt=0)
    currency: Literal["INR"]


@router.post("/checkouts/{checkout_id}/versions/{version}/pay")
def pay(
    checkout_id: uuid.UUID,
    version: int,
    body: ReservePaymentRequest,
    ctx: SessionContext,
    session: KernelSession,
    key: IdempotencyKey,
    registry: Registry,
    request: Request,
) -> JSONResponse:
    simulation_only(request)
    buyer_only(ctx)
    ctx.require("checkout.approve")
    ctx.require("checkout.submit_approved")
    # Read ownership without locking authority before the checkout (global lock order).
    snapshot = session.execute(
        text(
            # `allowed_skus IS NOT NULL` used to be here and was never a control: it
            # only described the shape of permission this route was written for, back
            # when every one of them carried a product scope. Left in place it refuses an
            # unscoped permission as "not found", which is a 404 for a row that exists and
            # is valid. What actually bounds the debit is `authority.admit_debit`, under
            # the authority's own row lock, and it is unchanged.
            "SELECT id FROM delegated_authorities WHERE tenant_id=:t "
            "AND merchant_id=:m AND buyer_ref=:b AND id=:a AND kind='RESERVE' "
            "AND per_purchase_limit_minor IS NOT NULL"
        ),
        {"t": ctx.tenant_id, "m": ctx.merchant_id, "b": ctx.buyer_ref, "a": body.authority_id},
    ).scalar_one_or_none()
    if snapshot is None:
        raise ProblemError(404, "Permission not found", "Set up a Reserve permission first.")
    payload = request_fingerprint(
        path_params={"checkout_id": checkout_id, "version": version},
        body=body.model_dump(mode="json"),
    )
    try:
        with idempotent_mutation(session, ctx, key, "RESERVE_DEBIT", payload) as slot:
            assert_owner(session, ctx, checkout_id)
            existing = admission_service._live_attempt(session, ctx, checkout_id)
            if existing:
                result = admission_service._duplicate_body(checkout_id, existing)
            else:
                admission_service.approve_version(
                    session,
                    ctx,
                    checkout_id=checkout_id,
                    version=version,
                    content_hash=body.content_hash,
                    amount_minor=body.amount_minor,
                    currency=body.currency,
                    authority_id=body.authority_id,
                    authority_epoch=body.authority_epoch,
                    operation=Operation.RESERVE_DEBIT,
                )
                result = admission_service.admit_approved_version(
                    session,
                    ctx,
                    registry,
                    checkout_id=checkout_id,
                    version=version,
                    idempotency_key=key,
                    operation=Operation.RESERVE_DEBIT,
                    authority_id=body.authority_id,
                    authority_epoch=body.authority_epoch,
                ).body
            result = {**result, "provider_mode": "SIMULATED"}
            slot.store(result)
    except admission_service.ConcurrentAdmission:
        result = admission_service.duplicate_after_race(session, ctx, checkout_id)
    return JSONResponse(content=result)


@router.get("/payments/{attempt_id}")
def payment_status(
    attempt_id: uuid.UUID, ctx: SessionContext, session: KernelSession
) -> dict[str, Any]:
    buyer_only(ctx)
    row = session.execute(
        text(
            "SELECT p.*, o.id AS order_id FROM payment_attempts p "
            "JOIN checkouts c ON c.id=p.checkout_id AND c.tenant_id=p.tenant_id "
            "LEFT JOIN orders o ON o.payment_attempt_id=p.id AND o.tenant_id=p.tenant_id "
            "WHERE p.tenant_id=:t AND p.id=:p AND c.buyer_ref=:b AND c.merchant_id=:m "
            "AND p.reserve_authority_id IS NOT NULL"
        ),
        {"t": ctx.tenant_id, "p": attempt_id, "b": ctx.buyer_ref, "m": ctx.merchant_id},
    ).one_or_none()
    if row is None:
        raise ProblemError(404, "Payment not found", "No Reserve payment exists for this buyer.")
    return {
        "attempt_id": str(row.id),
        "status": row.status,
        "allocation": row.reserve_allocation,
        "order_id": str(row.order_id) if row.order_id else None,
        "provider_mode": "SIMULATED",
        "authority_id": str(row.reserve_authority_id),
    }


class SimulatorOutcome(BaseModel):
    outcome: Literal["captured", "failed", "unknown"]


@router.post("/simulator/{attempt_id}", dependencies=[Depends(require_scenario_key)])
def simulate(
    attempt_id: uuid.UUID,
    body: SimulatorOutcome,
    request: Request,
    ctx: SessionContext,
    session: KernelSession,
    key: IdempotencyKey,
) -> dict[str, Any]:
    simulation_only(request)
    payment_status(attempt_id, ctx, session)
    with idempotent_mutation(
        session, ctx, key, "RESERVE_SIMULATOR", {"attempt_id": str(attempt_id), **body.model_dump()}
    ) as slot:
        # Through the Kernel: `payment_attempts` is a financial table with one writer
        # (ADR 0003 D1). `only_while_held` keeps this route able to choose a *future*
        # outcome without rewriting one that has already settled.
        changed = tk_reserve.record_simulation_outcome(
            session, attempt_id, outcome=body.outcome, only_while_held=True
        )
        if changed:
            payload = session.execute(
                text(
                    "SELECT payload FROM outbox_events "
                    "WHERE tenant_id=:t AND command_type='RESERVE_DEBIT' "
                    "AND payload->>'payment_attempt_id'=:p ORDER BY created_at LIMIT 1"
                ),
                {"t": ctx.tenant_id, "p": str(attempt_id)},
            ).scalar_one()
            enqueue_command(
                session,
                ReserveReconcileCommand.from_payload({**payload, "round": 1}),
                idempotency_key=key,
            )
        result = payment_status(attempt_id, ctx, session)
        slot.store(result)
    return result
