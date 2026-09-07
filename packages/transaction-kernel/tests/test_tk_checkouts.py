"""Checkout lifecycle, ADR 0003 D5 and D6.

What is proven here, against real PostgreSQL as ``commerce_test_kernel``:

* ``create_checkout`` writes the head and version 1 together, re-stamps the canonical
  content and refuses a second checkout for one basket through the unique constraint.
* ``freeze_for_approval`` walks the only legal path (QUOTED -> RESERVED ->
  APPROVAL_REQUIRED), binds a receipt, takes the hold, freezes the version, and also
  accepts admission's supersede-shaped N+1 (APPROVAL_REQUIRED, no receipt).
* ``cancel`` is decided by the state table: allowed states release the hold, expire a
  CREATED attempt and revoke an ISSUED grant in lock order; AWAITING_PAYMENT is a
  structured refusal that changes nothing and is still audited.
* ``invalidate_open`` keeps the reservation, because a late capture may still arrive.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from commerce_domain import Money, canonical_hash, uuid7
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session
from transaction_kernel import grants, receipts, reservations
from transaction_kernel.checkout_content import ContentLine, build_checkout_content, content_hash
from transaction_kernel.checkouts import (
    CheckoutConcurrencyError,
    CheckoutReservationError,
    CheckoutStateError,
    CheckoutTenantError,
    CheckoutUsageError,
    ReceiptInputs,
    cancel,
    create_checkout,
    current_version,
    freeze_for_approval,
    invalidate_open,
    read_head,
    read_versions,
    transition,
)
from transaction_kernel.contracts import ActorType, AgentPrincipal, CheckoutRef, Operation
from transaction_kernel.receipts import BuyerVisibleRef, MerchantPolicy, PolicyKind
from transaction_kernel.recovery import RecoveryCode
from transaction_kernel.states import CheckoutState

pytestmark = pytest.mark.db

SET_TENANT = text("SELECT set_config('app.tenant_id', :t, true)")
TOTAL = Money(57995, "INR")
RICE = "INDI-STPL-001"
MILK = "AMUL-DAIRY-001"


# ----------------------------------------------------------------------------- fixtures


@dataclass(frozen=True, slots=True)
class World:
    tenant_id: uuid.UUID
    merchant_id: uuid.UUID
    basket_id: uuid.UUID
    buyer_ref: str
    principal: AgentPrincipal


@pytest.fixture
def world(adm_admin_engine: Engine) -> Iterator[World]:
    tenant_id, merchant_id, basket_id = uuid.uuid4(), uuid7(), uuid7()
    slug = f"ck-{tenant_id.hex[:8]}"
    with adm_admin_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, home_region) "
                "VALUES (:id, :s, :n, 'asia-south1')"
            ),
            {"id": tenant_id, "s": slug, "n": slug},
        )
        conn.execute(SET_TENANT, {"t": str(tenant_id)})
        conn.execute(
            text(
                "INSERT INTO merchants (id, tenant_id, slug, name, currency) "
                "VALUES (:id, :t, :s, :n, 'INR')"
            ),
            {"id": merchant_id, "t": tenant_id, "s": slug, "n": slug},
        )
        conn.execute(
            text(
                "INSERT INTO carts (id, tenant_id, merchant_id, buyer_ref, lines, status) "
                "VALUES (:id, :t, :m, 'buyer-1', '[]'::jsonb, 'OPEN')"
            ),
            {"id": basket_id, "t": tenant_id, "m": merchant_id},
        )
    yield World(
        tenant_id=tenant_id,
        merchant_id=merchant_id,
        basket_id=basket_id,
        buyer_ref="buyer-1",
        principal=AgentPrincipal(
            principal_id="buyer-1",
            tenant_id=tenant_id,
            actor_type=ActorType.BUYER,
            merchant_id=merchant_id,
            buyer_ref="buyer-1",
        ),
    )
    with adm_admin_engine.begin() as conn:
        conn.execute(SET_TENANT, {"t": str(tenant_id)})
        for table in (
            "audit_events",
            "approvals",
            "execution_grants",
            "payment_attempts",
            "reservations",
            "checkout_versions",
            "policy_at_sale_receipts",
            "checkouts",
            "carts",
            "merchants",
        ):
            # S608: `table` iterates the literal tuple above, never request data.
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant_id})  # noqa: S608
        conn.execute(SET_TENANT, {"t": None})
        conn.execute(text("DELETE FROM tenants WHERE id = :i"), {"i": tenant_id})


@contextmanager
def kernel_tx(engine: Engine, tenant_id: uuid.UUID) -> Iterator[Session]:
    session = Session(engine, expire_on_commit=False)
    try:
        with session.begin():
            session.execute(SET_TENANT, {"t": str(tenant_id)})
            yield session
    finally:
        session.close()


def content(checkout_id: uuid.UUID | None = None, version: int = 1) -> dict[str, Any]:
    return build_checkout_content(
        checkout_id=checkout_id or uuid7(),
        version=version,
        currency="INR",
        lines=(
            ContentLine(RICE, "Basmati rice 5 kg", 1, 49900, 49900, 2495),
            ContentLine(MILK, "Toned milk 1 L", 2, 2800, 5600, 0),
        ),
        subtotal_minor=55500,
        tax_minor=2495,
        delivery_fee_minor=0,
        discount_minor=0,
        total_minor=57995,
        policy_version="pol-v12",
        catalogue_revision=0,
        source_id="merchant-sim:demo-grocery/v1",
    )


def receipt_inputs() -> ReceiptInputs:
    return ReceiptInputs(
        policies=tuple(
            MerchantPolicy(
                kind=kind,
                policy_id=f"pol-{kind.value.lower()}",
                policy_version=12,
                terms={"summary": f"{kind.value} terms"},
            )
            for kind in PolicyKind
        ),
        tax_policy_version="3",
        rounding_policy_version="1",
        buyer_visible_refs=(
            BuyerVisibleRef(
                label="Refund policy",
                uri="https://demo.invalid/policies/refund",
                text_hash=canonical_hash({"policy": "refund", "version": 12}),
            ),
        ),
    )


def created(engine: Engine, world: World) -> CheckoutRef:
    with kernel_tx(engine, world.tenant_id) as session:
        return create_checkout(
            session,
            tenant_id=world.tenant_id,
            merchant_id=world.merchant_id,
            basket_id=world.basket_id,
            buyer_ref=world.buyer_ref,
            content=content(),
            correlation_id=uuid7(),
        ).ref


def awaiting_approval(engine: Engine, world: World) -> CheckoutRef:
    ref = created(engine, world)
    with kernel_tx(engine, world.tenant_id) as session:
        freeze_for_approval(
            session,
            tenant_id=world.tenant_id,
            checkout=ref,
            receipt=receipt_inputs(),
            correlation_id=uuid7(),
        )
    return ref


def approved(engine: Engine, world: World) -> CheckoutRef:
    """APPROVED via the plain transition: the approvals module has its own suite."""
    ref = awaiting_approval(engine, world)
    with kernel_tx(engine, world.tenant_id) as session:
        transition(
            session,
            tenant_id=world.tenant_id,
            checkout=ref,
            target=CheckoutState.APPROVED,
            reason="test",
            actor=ActorType.BUYER,
            correlation_id=uuid7(),
        )
    return ref


def moved(engine: Engine, world: World, ref: CheckoutRef, *targets: CheckoutState) -> None:
    for target in targets:
        with kernel_tx(engine, world.tenant_id) as session:
            transition(
                session,
                tenant_id=world.tenant_id,
                checkout=ref,
                target=target,
                reason="test",
                actor=ActorType.WORKER,
                correlation_id=uuid7(),
            )


def version_row(engine: Engine, world: World, ref: CheckoutRef) -> Any:
    with kernel_tx(engine, world.tenant_id) as session:
        return session.execute(
            text(
                "SELECT status, immutable, policy_receipt_id, policy_receipt_hash, "
                "invalidated_at, content FROM checkout_versions "
                "WHERE checkout_id = :c AND version = :v"
            ),
            {"c": ref.checkout_id, "v": ref.version},
        ).one()


def head_row(engine: Engine, world: World, ref: CheckoutRef) -> Any:
    with kernel_tx(engine, world.tenant_id) as session:
        return session.execute(
            text("SELECT status, current_version FROM checkouts WHERE id = :c"),
            {"c": ref.checkout_id},
        ).one()


def hold(engine: Engine, world: World, ref: CheckoutRef) -> reservations.ReservationView | None:
    with kernel_tx(engine, world.tenant_id) as session:
        return reservations.check_validity(
            session, checkout_id=ref.checkout_id, checkout_version=ref.version, lock=False
        ).reservation


def events(engine: Engine, world: World, ref: CheckoutRef) -> list[tuple[str, dict[str, Any]]]:
    with kernel_tx(engine, world.tenant_id) as session:
        rows = session.execute(
            text(
                "SELECT event_type, payload FROM audit_events WHERE aggregate_id = :c ORDER BY seq"
            ),
            {"c": ref.checkout_id},
        ).all()
        return [(str(row.event_type), dict(row.payload)) for row in rows]


def seed_attempt(admin: Engine, world: World, ref: CheckoutRef) -> uuid.UUID:
    attempt_id = uuid7()
    with admin.begin() as conn:
        conn.execute(SET_TENANT, {"t": str(world.tenant_id)})
        conn.execute(
            text(
                "INSERT INTO payment_attempts (id, tenant_id, checkout_id, checkout_version, "
                "status, amount_minor, currency, receipt) "
                "VALUES (:id, :t, :c, :v, 'CREATED', 57995, 'INR', :r)"
            ),
            {
                "id": attempt_id,
                "t": world.tenant_id,
                "c": ref.checkout_id,
                "v": ref.version,
                "r": f"rcpt-{attempt_id.hex[:10]}",
            },
        )
    return attempt_id


# ------------------------------------------------------------------------------- create


class TestCreateCheckout:
    def test_writes_head_and_version_one_together(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            result = create_checkout(
                session,
                tenant_id=world.tenant_id,
                merchant_id=world.merchant_id,
                basket_id=world.basket_id,
                buyer_ref=world.buyer_ref,
                content=content(),
                correlation_id=uuid7(),
            )
        assert result.version == 1
        assert result.total == TOTAL
        row = version_row(adm_kernel_engine, world, result.ref)
        assert row.status == "QUOTED"
        assert row.immutable is False
        # The stored document is re-stamped with the minted id and hashes to the reported hash.
        assert row.content["checkout_id"] == str(result.checkout_id)
        assert row.content["version"] == 1
        assert content_hash(row.content) == result.content_hash
        head = head_row(adm_kernel_engine, world, result.ref)
        assert (head.status, head.current_version) == ("QUOTED", 1)
        assert [e for e, _ in events(adm_kernel_engine, world, result.ref)] == [
            "checkout.version_created"
        ]

    def test_a_caller_supplied_id_is_honoured(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        chosen = uuid7()
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            result = create_checkout(
                session,
                tenant_id=world.tenant_id,
                merchant_id=world.merchant_id,
                basket_id=world.basket_id,
                buyer_ref=world.buyer_ref,
                content=content(chosen),
                correlation_id=uuid7(),
                checkout_id=chosen,
            )
        assert result.checkout_id == chosen
        assert result.content_hash == content_hash(content(chosen))

    def test_one_checkout_per_basket_is_a_database_rule(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        created(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(CheckoutConcurrencyError) as info:
                create_checkout(
                    session,
                    tenant_id=world.tenant_id,
                    merchant_id=world.merchant_id,
                    basket_id=world.basket_id,
                    buyer_ref=world.buyer_ref,
                    content=content(),
                    correlation_id=uuid7(),
                )
            assert info.value.code is RecoveryCode.DUPLICATE_OPERATION
            assert session.execute(text("SELECT 1")).scalar_one() == 1

    def test_another_buyers_basket_is_refused(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(CheckoutStateError) as info:
                create_checkout(
                    session,
                    tenant_id=world.tenant_id,
                    merchant_id=world.merchant_id,
                    basket_id=world.basket_id,
                    buyer_ref="somebody-else",
                    content=content(),
                    correlation_id=uuid7(),
                )
        assert info.value.reason == "basket_buyer_mismatch"
        assert info.value.code is RecoveryCode.AUTHORITY_INSUFFICIENT

    def test_a_missing_basket_is_refused(self, adm_kernel_engine: Engine, world: World) -> None:
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(CheckoutStateError) as info:
                create_checkout(
                    session,
                    tenant_id=world.tenant_id,
                    merchant_id=world.merchant_id,
                    basket_id=uuid7(),
                    buyer_ref=world.buyer_ref,
                    content=content(),
                    correlation_id=uuid7(),
                )
        assert info.value.reason == "basket_missing"

    def test_tenant_argument_is_checked_against_the_guc(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(CheckoutTenantError):
                create_checkout(
                    session,
                    tenant_id=uuid.uuid4(),
                    merchant_id=world.merchant_id,
                    basket_id=world.basket_id,
                    buyer_ref=world.buyer_ref,
                    content=content(),
                    correlation_id=uuid7(),
                )

    def test_requires_an_open_transaction(self, adm_kernel_engine: Engine, world: World) -> None:
        session = Session(adm_kernel_engine)
        try:
            with pytest.raises(CheckoutUsageError):
                read_head(session, tenant_id=world.tenant_id, checkout_id=uuid7())
        finally:
            session.close()


# ----------------------------------------------------------------------- require approval


class TestRequireApproval:
    def test_freezes_the_version_with_receipt_and_hold(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        ref = created(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            card = freeze_for_approval(
                session,
                tenant_id=world.tenant_id,
                checkout=ref,
                receipt=receipt_inputs(),
                correlation_id=uuid7(),
            )
        assert card.checkout == ref
        assert card.total == TOTAL
        assert card.merchant_id == world.merchant_id
        assert card.content["total_minor"] == 57995
        row = version_row(adm_kernel_engine, world, ref)
        assert row.status == "APPROVAL_REQUIRED"
        assert row.immutable is True
        assert row.policy_receipt_id == card.receipt_id
        assert row.policy_receipt_hash == card.receipt_hash
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            assert receipts.verify_binding(session, ref).ok
        held = hold(adm_kernel_engine, world, ref)
        assert held is not None
        assert held.status is reservations.ReservationStatus.ACTIVE
        assert held.expires_at == card.reservation_expires_at
        assert head_row(adm_kernel_engine, world, ref).status == "APPROVAL_REQUIRED"
        payload = dict(events(adm_kernel_engine, world, ref))["checkout.approval_required"]
        assert payload["path"] == ["QUOTED", "RESERVED", "APPROVAL_REQUIRED"]
        assert payload["receipt_hash"] == card.receipt_hash

    def test_capacity_check_runs_when_allocations_are_given(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        ref = created(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(CheckoutReservationError) as info:
                freeze_for_approval(
                    session,
                    tenant_id=world.tenant_id,
                    checkout=ref,
                    receipt=receipt_inputs(),
                    correlation_id=uuid7(),
                    allocations=(reservations.Allocation(MILK, 1),),  # basket wants 2
                )
        assert info.value.code is RecoveryCode.STALE_CHECKOUT
        assert version_row(adm_kernel_engine, world, ref).status == "QUOTED"

    def test_enough_stock_passes_the_capacity_check(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        ref = created(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            freeze_for_approval(
                session,
                tenant_id=world.tenant_id,
                checkout=ref,
                receipt=receipt_inputs(),
                correlation_id=uuid7(),
                allocations=(reservations.Allocation(MILK, 2), reservations.Allocation(RICE, 1)),
            )
        assert version_row(adm_kernel_engine, world, ref).status == "APPROVAL_REQUIRED"

    def test_a_second_request_is_a_duplicate(self, adm_kernel_engine: Engine, world: World) -> None:
        ref = awaiting_approval(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(CheckoutStateError) as info:
                freeze_for_approval(
                    session,
                    tenant_id=world.tenant_id,
                    checkout=ref,
                    receipt=receipt_inputs(),
                    correlation_id=uuid7(),
                )
        assert info.value.reason == "already_awaiting_approval"
        assert info.value.code is RecoveryCode.DUPLICATE_OPERATION

    def test_accepts_admissions_supersede_shaped_n_plus_one(
        self, adm_kernel_engine: Engine, adm_admin_engine: Engine, world: World
    ) -> None:
        """Admission writes N+1 in APPROVAL_REQUIRED with no receipt (ADR D4c) and then
        calls this to give it a receipt and a fresh hold, without any transition."""
        ref = approved(adm_kernel_engine, world)
        next_content = content(ref.checkout_id, version=2)
        next_hash = content_hash(next_content)
        with adm_admin_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(world.tenant_id)})
            conn.execute(
                text(
                    "INSERT INTO checkout_versions (id, tenant_id, merchant_id, checkout_id, "
                    "version, content, content_hash, currency, total_minor, status, immutable) "
                    "VALUES (:id, :t, :m, :c, 2, CAST(:content AS jsonb), :h, 'INR', 57995, "
                    "'APPROVAL_REQUIRED', false)"
                ),
                {
                    "id": uuid7(),
                    "t": world.tenant_id,
                    "m": world.merchant_id,
                    "c": ref.checkout_id,
                    "content": __import__("json").dumps(next_content, sort_keys=True),
                    "h": next_hash,
                },
            )
        next_ref = CheckoutRef(ref.checkout_id, 2, next_hash)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            card = freeze_for_approval(
                session,
                tenant_id=world.tenant_id,
                checkout=next_ref,
                receipt=receipt_inputs(),
                correlation_id=uuid7(),
            )
        row = version_row(adm_kernel_engine, world, next_ref)
        assert row.status == "APPROVAL_REQUIRED"
        assert row.policy_receipt_id == card.receipt_id
        assert row.immutable is True
        assert hold(adm_kernel_engine, world, next_ref) is not None
        head = head_row(adm_kernel_engine, world, next_ref)
        assert (head.status, head.current_version) == ("APPROVAL_REQUIRED", 2)
        payload = [
            p for e, p in events(adm_kernel_engine, world, ref) if e == "checkout.approval_required"
        ]
        assert payload[-1]["path"] == ["APPROVAL_REQUIRED"]

    def test_refuses_from_approved(self, adm_kernel_engine: Engine, world: World) -> None:
        ref = approved(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(CheckoutStateError) as info:
                freeze_for_approval(
                    session,
                    tenant_id=world.tenant_id,
                    checkout=ref,
                    receipt=receipt_inputs(),
                    correlation_id=uuid7(),
                )
        assert info.value.reason == "wrong_status"


# --------------------------------------------------------------------------- transition


class TestTransition:
    def test_a_legal_step_updates_version_head_and_audit(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        ref = approved(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            result = transition(
                session,
                tenant_id=world.tenant_id,
                checkout=ref,
                target=CheckoutState.EXECUTION_PENDING,
                reason="grant_issued",
                actor=ActorType.SYSTEM,
                correlation_id=uuid7(),
            )
        assert (result.from_state, result.to_state) == (
            CheckoutState.APPROVED,
            CheckoutState.EXECUTION_PENDING,
        )
        assert result.head_updated is True
        assert version_row(adm_kernel_engine, world, ref).status == "EXECUTION_PENDING"
        assert head_row(adm_kernel_engine, world, ref).status == "EXECUTION_PENDING"
        payload = [
            p for e, p in events(adm_kernel_engine, world, ref) if e == "checkout.transitioned"
        ]
        assert payload[-1]["from"] == "APPROVED"
        assert payload[-1]["to"] == "EXECUTION_PENDING"
        assert payload[-1]["reason"] == "grant_issued"

    def test_an_illegal_step_is_refused_with_both_states(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        ref = created(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(CheckoutStateError) as info:
                transition(
                    session,
                    tenant_id=world.tenant_id,
                    checkout=ref,
                    target=CheckoutState.PAID,
                    reason="nope",
                    actor=ActorType.SYSTEM,
                    correlation_id=uuid7(),
                )
        assert info.value.reason == "illegal_transition"
        assert (info.value.current, info.value.target) == (CheckoutState.QUOTED, CheckoutState.PAID)
        assert version_row(adm_kernel_engine, world, ref).status == "QUOTED"

    def test_a_stale_hash_is_refused(self, adm_kernel_engine: Engine, world: World) -> None:
        ref = created(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(CheckoutStateError) as info:
                transition(
                    session,
                    tenant_id=world.tenant_id,
                    checkout=CheckoutRef(ref.checkout_id, 1, "forged"),
                    target=CheckoutState.CANCELLED,
                    reason="nope",
                    actor=ActorType.SYSTEM,
                    correlation_id=uuid7(),
                )
        assert info.value.reason == "hash_mismatch"


# ------------------------------------------------------------------------------- cancel


class TestCancel:
    def test_cancel_from_approved_releases_the_hold(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        ref = approved(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            result = cancel(
                session,
                tenant_id=world.tenant_id,
                checkout=ref,
                principal=world.principal,
                reason="buyer_changed_mind",
                correlation_id=uuid7(),
            )
        assert result.allowed
        assert result.code is RecoveryCode.OK
        assert result.from_state is CheckoutState.APPROVED
        assert result.reservation_release is RecoveryCode.OK
        assert result.grants_revoked == ()
        assert result.attempt_expired is None
        assert version_row(adm_kernel_engine, world, ref).status == "CANCELLED"
        assert head_row(adm_kernel_engine, world, ref).status == "CANCELLED"
        held = hold(adm_kernel_engine, world, ref)
        assert held is not None and held.status is reservations.ReservationStatus.RELEASED
        payload = dict(events(adm_kernel_engine, world, ref))["checkout.cancelled"]
        assert payload["reason"] == "buyer_changed_mind"
        assert payload["from"] == "APPROVED"

    def test_cancel_after_admission_revokes_the_grant_and_expires_the_attempt(
        self, adm_kernel_engine: Engine, adm_admin_engine: Engine, world: World
    ) -> None:
        ref = approved(adm_kernel_engine, world)
        attempt_id = seed_attempt(adm_admin_engine, world, ref)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            # What admission does: consume the hold, issue the grant, move the version on.
            assert reservations.consume(
                session, checkout_id=ref.checkout_id, checkout_version=ref.version
            ).ok
            grant = grants.issue_grant(
                session,
                tenant=world.tenant_id,
                checkout_ref=ref,
                payment_attempt_id=attempt_id,
                operation=Operation.PAYMENT_CREATE_ORDER,
                amount=TOTAL,
                kernel_decision_id=uuid7(),
                ttl_seconds=300,
            )
            grant_id = grant.id
        moved(adm_kernel_engine, world, ref, CheckoutState.EXECUTION_PENDING)

        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            result = cancel(
                session,
                tenant_id=world.tenant_id,
                checkout=ref,
                principal=world.principal,
                reason="buyer_cancelled",
                correlation_id=uuid7(),
            )
        assert result.allowed
        assert result.grants_revoked == (grant_id,)
        assert result.attempt_expired == attempt_id
        assert result.reservation_release is RecoveryCode.OK
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            grant_status = session.execute(
                text("SELECT status FROM execution_grants WHERE id = :g"), {"g": grant_id}
            ).scalar_one()
            attempt_status = session.execute(
                text("SELECT status FROM payment_attempts WHERE id = :a"), {"a": attempt_id}
            ).scalar_one()
        assert grant_status == "REVOKED"
        assert attempt_status == "EXPIRED"
        assert version_row(adm_kernel_engine, world, ref).status == "CANCELLED"
        held = hold(adm_kernel_engine, world, ref)
        assert held is not None and held.status is reservations.ReservationStatus.RELEASED

    def test_cancel_while_the_payment_surface_is_open_is_a_structured_denial(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        ref = approved(adm_kernel_engine, world)
        moved(
            adm_kernel_engine,
            world,
            ref,
            CheckoutState.EXECUTION_PENDING,
            CheckoutState.AWAITING_PAYMENT,
        )
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            result = cancel(
                session,
                tenant_id=world.tenant_id,
                checkout=ref,
                principal=world.principal,
                reason="buyer_cancelled",
                correlation_id=uuid7(),
            )
        assert not result.allowed
        assert result.code is RecoveryCode.PAYMENT_PENDING
        assert result.explanation == "payment_surface_open"
        assert result.from_state is CheckoutState.AWAITING_PAYMENT
        # Nothing moved: the version, the head and the hold are exactly as they were.
        assert version_row(adm_kernel_engine, world, ref).status == "AWAITING_PAYMENT"
        assert head_row(adm_kernel_engine, world, ref).status == "AWAITING_PAYMENT"
        held = hold(adm_kernel_engine, world, ref)
        assert held is not None and held.status is reservations.ReservationStatus.ACTIVE
        payload = dict(events(adm_kernel_engine, world, ref))["checkout.cancel_denied"]
        assert payload["code"] == "PAYMENT_PENDING"

    def test_cancel_under_unknown_outcome_is_refused_with_payment_unknown(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        ref = approved(adm_kernel_engine, world)
        moved(
            adm_kernel_engine,
            world,
            ref,
            CheckoutState.EXECUTION_PENDING,
            CheckoutState.AWAITING_PAYMENT,
            CheckoutState.PAYMENT_UNKNOWN,
        )
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            result = cancel(
                session,
                tenant_id=world.tenant_id,
                checkout=ref,
                principal=world.principal,
                reason="buyer_cancelled",
                correlation_id=uuid7(),
            )
        assert result.code is RecoveryCode.PAYMENT_UNKNOWN

    def test_cancel_twice_is_a_duplicate(self, adm_kernel_engine: Engine, world: World) -> None:
        ref = approved(adm_kernel_engine, world)
        for expected in (RecoveryCode.OK, RecoveryCode.DUPLICATE_OPERATION):
            with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
                result = cancel(
                    session,
                    tenant_id=world.tenant_id,
                    checkout=ref,
                    principal=world.principal,
                    reason="buyer_cancelled",
                    correlation_id=uuid7(),
                )
            assert result.code is expected

    def test_a_stale_hash_is_denied_not_raised(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        ref = approved(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            result = cancel(
                session,
                tenant_id=world.tenant_id,
                checkout=CheckoutRef(ref.checkout_id, ref.version, "forged"),
                principal=world.principal,
                reason="buyer_cancelled",
                correlation_id=uuid7(),
            )
        assert result.code is RecoveryCode.STALE_CHECKOUT
        assert result.explanation == "approved_hash_does_not_match_stored"
        assert version_row(adm_kernel_engine, world, ref).status == "APPROVED"

    def test_an_attempt_past_created_blocks_cancellation_before_any_write(
        self, adm_kernel_engine: Engine, adm_admin_engine: Engine, world: World
    ) -> None:
        ref = approved(adm_kernel_engine, world)
        attempt_id = seed_attempt(adm_admin_engine, world, ref)
        with adm_admin_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(world.tenant_id)})
            conn.execute(
                text("UPDATE payment_attempts SET status = 'SUBMITTED' WHERE id = :a"),
                {"a": attempt_id},
            )
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            result = cancel(
                session,
                tenant_id=world.tenant_id,
                checkout=ref,
                principal=world.principal,
                reason="buyer_cancelled",
                correlation_id=uuid7(),
            )
        assert result.code is RecoveryCode.PAYMENT_PENDING
        assert result.explanation == "attempt_in_flight"
        held = hold(adm_kernel_engine, world, ref)
        assert held is not None and held.status is reservations.ReservationStatus.ACTIVE


# ------------------------------------------------------------------------ invalidate open


class TestInvalidateOpen:
    def test_keeps_the_reservation_and_stamps_invalidated_at(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        ref = approved(adm_kernel_engine, world)
        moved(
            adm_kernel_engine,
            world,
            ref,
            CheckoutState.EXECUTION_PENDING,
            CheckoutState.AWAITING_PAYMENT,
        )
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            result = invalidate_open(
                session,
                tenant_id=world.tenant_id,
                checkout=ref,
                reason="merchant_state_changed",
                correlation_id=uuid7(),
            )
        assert result.to_state is CheckoutState.INVALIDATED_AWAITING_PAYMENT_RESULT
        row = version_row(adm_kernel_engine, world, ref)
        assert row.status == "INVALIDATED_AWAITING_PAYMENT_RESULT"
        assert row.invalidated_at is not None
        held = hold(adm_kernel_engine, world, ref)
        assert held is not None and held.status is reservations.ReservationStatus.ACTIVE
        assert (
            head_row(adm_kernel_engine, world, ref).status == "INVALIDATED_AWAITING_PAYMENT_RESULT"
        )
        payload = dict(events(adm_kernel_engine, world, ref))["checkout.invalidated_open"]
        assert payload["reservation_held"] is True

    def test_refused_before_the_payment_surface_opens(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        ref = approved(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(CheckoutStateError) as info:
                invalidate_open(
                    session,
                    tenant_id=world.tenant_id,
                    checkout=ref,
                    reason="merchant_state_changed",
                    correlation_id=uuid7(),
                )
        assert info.value.reason == "illegal_transition"
        assert version_row(adm_kernel_engine, world, ref).invalidated_at is None


# -------------------------------------------------------------------------------- reads


class TestReads:
    def test_head_versions_and_current(self, adm_kernel_engine: Engine, world: World) -> None:
        ref = awaiting_approval(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            head = read_head(session, tenant_id=world.tenant_id, checkout_id=ref.checkout_id)
            versions = read_versions(
                session, tenant_id=world.tenant_id, checkout_id=ref.checkout_id
            )
            latest = current_version(
                session, tenant_id=world.tenant_id, checkout_id=ref.checkout_id
            )
            missing = read_head(session, tenant_id=world.tenant_id, checkout_id=uuid7())
            none_at_all = read_versions(session, tenant_id=world.tenant_id, checkout_id=uuid7())
        assert head is not None
        assert head.basket_id == world.basket_id
        assert head.buyer_ref == world.buyer_ref
        assert head.status is CheckoutState.APPROVAL_REQUIRED
        assert head.current_version == 1
        assert len(versions) == 1
        assert versions[0].ref == ref
        assert versions[0].immutable is True
        assert versions[0].total == TOTAL
        assert versions[0].policy_receipt_id is not None
        assert latest == versions[0]
        assert missing is None
        assert none_at_all == ()
