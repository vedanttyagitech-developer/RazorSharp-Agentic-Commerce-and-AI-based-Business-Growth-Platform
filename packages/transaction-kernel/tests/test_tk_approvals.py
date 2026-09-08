"""Recorded approvals, specification 10.2 and ADR 0003 D4a.

The adversary here is a caller who wants an approval to authorise something the buyer
did not decide: a different amount, a re-priced version, a second use, a use after the
window closed. Every test is one of those attempts being refused by a row, a clock or a
constraint rather than by a check that could be skipped.

Runs against real PostgreSQL as ``commerce_test_kernel`` (NOSUPERUSER NOBYPASSRLS): the
one-RECORDED-per-version rule is a partial unique index and expiry is ``now()``, neither
of which SQLite can show. The fixtures below build a checkout through the production path
(``create_checkout`` then ``freeze_for_approval``) so a refusal is caused by the thing under
test and not by hand-rolled rows.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from commerce_domain import (
    ActorType,
    AgentPrincipal,
    CheckoutRef,
    Money,
    PolicyKind,
    RecoveryCode,
    canonical_hash,
    uuid7,
)
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session
from transaction_kernel import approvals, checkouts, reservations
from transaction_kernel.approvals import (
    ApprovalConflictError,
    ApprovalInvalidReason,
    ApprovalNotValidError,
    ApprovalStateError,
    ApprovalStatus,
    ApprovalTenantError,
    consume_recorded,
    defer_approval,
    expire_stale_approvals,
    record_approval,
    reject_approval,
)
from transaction_kernel.checkout_content import ContentLine, build_checkout_content
from transaction_kernel.checkouts import ReceiptInputs
from transaction_kernel.receipts import BuyerVisibleRef, SaleTerm
from transaction_kernel.states import CheckoutState

pytestmark = pytest.mark.db

SET_TENANT = text("SELECT set_config('app.tenant_id', :t, true)")
TOTAL = Money(57995, "INR")


# ----------------------------------------------------------------------------- fixtures


@dataclass(frozen=True, slots=True)
class World:
    tenant_id: uuid.UUID
    merchant_id: uuid.UUID
    cart_id: uuid.UUID
    buyer_ref: str
    principal: AgentPrincipal


@pytest.fixture
def world(adm_admin_engine: Engine) -> Iterator[World]:
    """One tenant, merchant and open cart, removed afterwards children-first."""
    tenant_id, merchant_id, cart_id = uuid.uuid4(), uuid7(), uuid7()
    slug = f"ap-{tenant_id.hex[:8]}"
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
            {"id": cart_id, "t": tenant_id, "m": merchant_id},
        )
    yield World(
        tenant_id=tenant_id,
        merchant_id=merchant_id,
        cart_id=cart_id,
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
            ContentLine("INDI-STPL-001", "Basmati rice 5 kg", 1, 49900, 49900, 2495),
            ContentLine("AMUL-DAIRY-001", "Toned milk 1 L", 2, 2800, 5600, 0),
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
            SaleTerm(
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


def awaiting_approval_card(
    engine: Engine, world: World
) -> tuple[CheckoutRef, checkouts.ApprovalCard]:
    """The same production path, keeping the card the buyer would have been shown.

    Separate from :func:`awaiting_approval` only because most tests want the reference and
    the amount tests want the card; both come from one call so the card and the version a
    test asserts about can never be built from two different checkouts.
    """
    with kernel_tx(engine, world.tenant_id) as session:
        created = checkouts.create_checkout(
            session,
            tenant_id=world.tenant_id,
            merchant_id=world.merchant_id,
            cart_id=world.cart_id,
            buyer_ref=world.buyer_ref,
            content=content(),
            correlation_id=uuid7(),
        )
        card = checkouts.freeze_for_approval(
            session,
            tenant_id=world.tenant_id,
            checkout=created.ref,
            receipt=receipt_inputs(),
            correlation_id=uuid7(),
        )
        return created.ref, card


def awaiting_approval(engine: Engine, world: World) -> CheckoutRef:
    """A checkout at APPROVAL_REQUIRED with its receipt and hold, via the production path."""
    return awaiting_approval_card(engine, world)[0]


def approved(engine: Engine, world: World) -> tuple[CheckoutRef, approvals.ApprovalRecord]:
    ref = awaiting_approval(engine, world)
    with kernel_tx(engine, world.tenant_id) as session:
        record = record_approval(
            session,
            tenant_id=world.tenant_id,
            checkout=ref,
            amount=TOTAL,
            principal=world.principal,
            correlation_id=uuid7(),
        )
    return ref, record


def version_status(engine: Engine, world: World, ref: CheckoutRef) -> str:
    with kernel_tx(engine, world.tenant_id) as session:
        return str(
            session.execute(
                text(
                    "SELECT status FROM checkout_versions WHERE checkout_id = :c AND version = :v"
                ),
                {"c": ref.checkout_id, "v": ref.version},
            ).scalar_one()
        )


def head_status(engine: Engine, world: World, ref: CheckoutRef) -> tuple[str, int]:
    with kernel_tx(engine, world.tenant_id) as session:
        row = session.execute(
            text("SELECT status, current_version FROM checkouts WHERE id = :c"),
            {"c": ref.checkout_id},
        ).one()
        return str(row.status), int(row.current_version)


def approval_status(engine: Engine, world: World, approval_id: uuid.UUID) -> str:
    with kernel_tx(engine, world.tenant_id) as session:
        return str(
            session.execute(
                text("SELECT status FROM approvals WHERE id = :id"), {"id": approval_id}
            ).scalar_one()
        )


def events(engine: Engine, world: World, ref: CheckoutRef) -> list[tuple[str, dict[str, Any]]]:
    with kernel_tx(engine, world.tenant_id) as session:
        rows = session.execute(
            text(
                "SELECT event_type, payload FROM audit_events WHERE aggregate_id = :c ORDER BY seq"
            ),
            {"c": ref.checkout_id},
        ).all()
        return [(str(row.event_type), dict(row.payload)) for row in rows]


# ------------------------------------------------------------------------------- record


class TestRecordApproval:
    def test_records_and_moves_the_version_to_approved(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        ref, record = approved(adm_kernel_engine, world)
        assert record.status is ApprovalStatus.RECORDED
        assert record.checkout == ref
        assert record.amount == TOTAL
        assert record.action == "PAYMENT_CREATE_ORDER"
        assert record.policy_receipt_hash is not None
        assert record.expires_at > record.issued_at
        assert version_status(adm_kernel_engine, world, ref) == "APPROVED"
        assert head_status(adm_kernel_engine, world, ref) == ("APPROVED", 1)

    def test_audits_the_hash_and_the_amount(self, adm_kernel_engine: Engine, world: World) -> None:
        ref, record = approved(adm_kernel_engine, world)
        recorded = [p for e, p in events(adm_kernel_engine, world, ref) if e == "approval.recorded"]
        assert len(recorded) == 1
        payload = recorded[0]
        assert payload["content_hash"] == ref.content_hash
        assert payload["amount"] == {"currency": "INR", "minor": 57995}
        assert payload["approval_id"] == str(record.approval_id)
        assert payload["policy_receipt_hash"] == record.policy_receipt_hash

    def test_copies_the_receipt_hash_onto_the_approval(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        """Specification 10.2.1: the receipt hash is bound into the approval."""
        ref, record = approved(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            bound = session.execute(
                text(
                    "SELECT policy_receipt_hash FROM checkout_versions "
                    "WHERE checkout_id = :c AND version = :v"
                ),
                {"c": ref.checkout_id, "v": ref.version},
            ).scalar_one()
        assert record.policy_receipt_hash == bound

    def test_a_second_recording_is_a_duplicate(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        ref, _ = approved(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(ApprovalStateError) as info:
                record_approval(
                    session,
                    tenant_id=world.tenant_id,
                    checkout=ref,
                    amount=TOTAL,
                    principal=world.principal,
                    correlation_id=uuid7(),
                )
        assert info.value.reason == "already_approved"
        assert info.value.code is RecoveryCode.DUPLICATE_OPERATION

    def test_a_different_amount_is_refused(self, adm_kernel_engine: Engine, world: World) -> None:
        ref = awaiting_approval(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(ApprovalStateError) as info:
                record_approval(
                    session,
                    tenant_id=world.tenant_id,
                    checkout=ref,
                    amount=Money(57994, "INR"),
                    principal=world.principal,
                    correlation_id=uuid7(),
                )
        assert info.value.reason == "amount_mismatch"
        assert info.value.code is RecoveryCode.STALE_CHECKOUT
        assert version_status(adm_kernel_engine, world, ref) == "APPROVAL_REQUIRED"

    def test_a_different_hash_is_refused(self, adm_kernel_engine: Engine, world: World) -> None:
        ref = awaiting_approval(adm_kernel_engine, world)
        forged = CheckoutRef(ref.checkout_id, ref.version, "not-the-hash")
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(ApprovalStateError) as info:
                record_approval(
                    session,
                    tenant_id=world.tenant_id,
                    checkout=forged,
                    amount=TOTAL,
                    principal=world.principal,
                    correlation_id=uuid7(),
                )
        assert info.value.reason == "hash_mismatch"

    def test_a_version_without_a_receipt_is_refused(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        """An approval without frozen terms is not a decision about anything."""
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            created = checkouts.create_checkout(
                session,
                tenant_id=world.tenant_id,
                merchant_id=world.merchant_id,
                cart_id=world.cart_id,
                buyer_ref=world.buyer_ref,
                content=content(),
                correlation_id=uuid7(),
            )
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(ApprovalStateError) as info:
                record_approval(
                    session,
                    tenant_id=world.tenant_id,
                    checkout=created.ref,
                    amount=TOTAL,
                    principal=world.principal,
                    correlation_id=uuid7(),
                )
        # QUOTED is refused as the wrong status before the receipt is even considered.
        assert info.value.reason == "wrong_status"

    def test_one_recorded_approval_per_version_is_a_database_rule(
        self, adm_kernel_engine: Engine, adm_admin_engine: Engine, world: World
    ) -> None:
        """A RECORDED row that slipped in beside APPROVAL_REQUIRED loses to the index."""
        ref = awaiting_approval(adm_kernel_engine, world)
        with adm_admin_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(world.tenant_id)})
            conn.execute(
                text(
                    "INSERT INTO approvals (id, tenant_id, checkout_id, checkout_version, "
                    "content_hash, amount_minor, currency, action, status, expires_at) "
                    "VALUES (:id, :t, :c, :v, :h, 57995, 'INR', 'PAYMENT_CREATE_ORDER', "
                    "'RECORDED', now() + interval '10 minutes')"
                ),
                {
                    "id": uuid7(),
                    "t": world.tenant_id,
                    "c": ref.checkout_id,
                    "v": ref.version,
                    "h": ref.content_hash,
                },
            )
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(ApprovalConflictError) as info:
                record_approval(
                    session,
                    tenant_id=world.tenant_id,
                    checkout=ref,
                    amount=TOTAL,
                    principal=world.principal,
                    correlation_id=uuid7(),
                )
            assert info.value.code is RecoveryCode.CONCURRENT_OPERATION
            # The SAVEPOINT kept the caller's transaction usable.
            assert session.execute(text("SELECT 1")).scalar_one() == 1

    def test_tenant_argument_must_match_the_bound_tenant(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        ref = awaiting_approval(adm_kernel_engine, world)
        other = uuid.uuid4()
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(checkouts.CheckoutTenantError):
                record_approval(
                    session,
                    tenant_id=other,
                    checkout=ref,
                    amount=TOTAL,
                    principal=world.principal,
                    correlation_id=uuid7(),
                )

    def test_principal_of_another_tenant_is_refused(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        ref = awaiting_approval(adm_kernel_engine, world)
        foreign = AgentPrincipal(
            principal_id="p", tenant_id=uuid.uuid4(), actor_type=ActorType.BUYER
        )
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(ApprovalTenantError):
                record_approval(
                    session,
                    tenant_id=world.tenant_id,
                    checkout=ref,
                    amount=TOTAL,
                    principal=foreign,
                    correlation_id=uuid7(),
                )

    def test_another_tenant_cannot_see_the_version(
        self, adm_kernel_engine: Engine, adm_admin_engine: Engine, world: World
    ) -> None:
        ref = awaiting_approval(adm_kernel_engine, world)
        stranger = uuid.uuid4()
        with adm_admin_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, home_region) "
                    "VALUES (:id, :s, :n, 'asia-south1')"
                ),
                {"id": stranger, "s": f"st-{stranger.hex[:8]}", "n": f"st-{stranger.hex[:8]}"},
            )
        try:
            with kernel_tx(adm_kernel_engine, stranger) as session:
                with pytest.raises(ApprovalStateError) as info:
                    record_approval(
                        session,
                        tenant_id=stranger,
                        checkout=ref,
                        amount=TOTAL,
                        principal=AgentPrincipal(
                            principal_id="s", tenant_id=stranger, actor_type=ActorType.BUYER
                        ),
                        correlation_id=uuid7(),
                    )
            assert info.value.reason == "version_missing"
        finally:
            with adm_admin_engine.begin() as conn:
                conn.execute(text("DELETE FROM tenants WHERE id = :i"), {"i": stranger})

    def test_requires_an_open_transaction(self, adm_kernel_engine: Engine, world: World) -> None:
        session = Session(adm_kernel_engine)
        try:
            with pytest.raises(checkouts.CheckoutUsageError):
                record_approval(
                    session,
                    tenant_id=world.tenant_id,
                    checkout=CheckoutRef(uuid7(), 1, "h"),
                    amount=TOTAL,
                    principal=world.principal,
                    correlation_id=uuid7(),
                )
        finally:
            session.close()


# ------------------------------------------------------------------------------ consume


class TestConsumeRecorded:
    def test_spends_the_approval_exactly_once(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        ref, record = approved(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            spent = consume_recorded(
                session,
                tenant_id=world.tenant_id,
                approval_id=record.approval_id,
                checkout=ref,
                amount=TOTAL,
                correlation_id=uuid7(),
            )
        assert spent.status is ApprovalStatus.CONSUMED
        assert approval_status(adm_kernel_engine, world, record.approval_id) == "CONSUMED"
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(ApprovalNotValidError) as info:
                consume_recorded(
                    session,
                    tenant_id=world.tenant_id,
                    approval_id=record.approval_id,
                    checkout=ref,
                    amount=TOTAL,
                )
        assert info.value.why is ApprovalInvalidReason.CONSUMED
        assert info.value.code is RecoveryCode.AUTHORITY_INSUFFICIENT
        kinds = [e for e, _ in events(adm_kernel_engine, world, ref)]
        assert kinds.count("approval.consumed") == 1

    def test_a_different_amount_does_not_match(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        ref, record = approved(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(ApprovalNotValidError) as info:
                consume_recorded(
                    session,
                    tenant_id=world.tenant_id,
                    approval_id=record.approval_id,
                    checkout=ref,
                    amount=Money(57996, "INR"),
                )
        assert info.value.why is ApprovalInvalidReason.MISMATCH
        assert approval_status(adm_kernel_engine, world, record.approval_id) == "RECORDED"

    def test_a_different_version_or_hash_does_not_match(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        ref, record = approved(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(ApprovalNotValidError) as info:
                consume_recorded(
                    session,
                    tenant_id=world.tenant_id,
                    approval_id=record.approval_id,
                    checkout=CheckoutRef(ref.checkout_id, ref.version + 1, ref.content_hash),
                    amount=TOTAL,
                )
        assert info.value.why is ApprovalInvalidReason.MISMATCH

    def test_an_unknown_id_is_missing(self, adm_kernel_engine: Engine, world: World) -> None:
        ref, _ = approved(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(ApprovalNotValidError) as info:
                consume_recorded(
                    session,
                    tenant_id=world.tenant_id,
                    approval_id=uuid7(),
                    checkout=ref,
                    amount=TOTAL,
                )
        assert info.value.why is ApprovalInvalidReason.MISSING

    def test_expiry_is_decided_by_the_database_clock(
        self, adm_kernel_engine: Engine, adm_admin_engine: Engine, world: World
    ) -> None:
        ref, record = approved(adm_kernel_engine, world)
        with adm_admin_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(world.tenant_id)})
            conn.execute(
                text(
                    "UPDATE approvals SET expires_at = now() - interval '1 second' WHERE id = :id"
                ),
                {"id": record.approval_id},
            )
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(ApprovalNotValidError) as info:
                consume_recorded(
                    session,
                    tenant_id=world.tenant_id,
                    approval_id=record.approval_id,
                    checkout=ref,
                    amount=TOTAL,
                )
        assert info.value.why is ApprovalInvalidReason.EXPIRED
        # Still RECORDED: consume never edits a row it did not spend; the sweep retires it.
        assert approval_status(adm_kernel_engine, world, record.approval_id) == "RECORDED"


# ------------------------------------------------------------------------------- reject


class TestRejectApproval:
    def test_reject_from_approved_cancels_and_releases(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        ref, record = approved(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            rejection = reject_approval(
                session,
                tenant_id=world.tenant_id,
                checkout=ref,
                principal=world.principal,
                reason="buyer_declined",
                correlation_id=uuid7(),
            )
        assert rejection.from_state is CheckoutState.APPROVED
        assert rejection.approval_ids == (record.approval_id,)
        assert rejection.reservation_release is RecoveryCode.OK
        assert approval_status(adm_kernel_engine, world, record.approval_id) == "INVALIDATED"
        assert version_status(adm_kernel_engine, world, ref) == "CANCELLED"
        assert head_status(adm_kernel_engine, world, ref) == ("CANCELLED", 1)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            hold = reservations.check_validity(
                session, checkout_id=ref.checkout_id, checkout_version=ref.version, lock=False
            )
        assert hold.reservation is not None
        assert hold.reservation.status is reservations.ReservationStatus.RELEASED
        rejected = [p for e, p in events(adm_kernel_engine, world, ref) if e == "approval.rejected"]
        assert rejected[0]["reason"] == "buyer_declined"
        assert rejected[0]["approval_ids"] == [str(record.approval_id)]

    def test_reject_before_deciding_invalidates_nothing_but_still_cancels(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        ref = awaiting_approval(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            rejection = reject_approval(
                session,
                tenant_id=world.tenant_id,
                checkout=ref,
                principal=world.principal,
                reason="buyer_declined",
                correlation_id=uuid7(),
            )
        assert rejection.from_state is CheckoutState.APPROVAL_REQUIRED
        assert rejection.approval_ids == ()
        assert version_status(adm_kernel_engine, world, ref) == "CANCELLED"

    def test_reject_twice_is_a_duplicate(self, adm_kernel_engine: Engine, world: World) -> None:
        ref, _ = approved(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            reject_approval(
                session,
                tenant_id=world.tenant_id,
                checkout=ref,
                principal=world.principal,
                reason="buyer_declined",
                correlation_id=uuid7(),
            )
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(ApprovalStateError) as info:
                reject_approval(
                    session,
                    tenant_id=world.tenant_id,
                    checkout=ref,
                    principal=world.principal,
                    reason="buyer_declined",
                    correlation_id=uuid7(),
                )
        assert info.value.code is RecoveryCode.DUPLICATE_OPERATION

    def test_reject_with_a_live_attempt_is_refused(
        self, adm_kernel_engine: Engine, adm_admin_engine: Engine, world: World
    ) -> None:
        """Once admission has run, cancellation is checkouts.cancel's job."""
        ref, _ = approved(adm_kernel_engine, world)
        with adm_admin_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(world.tenant_id)})
            conn.execute(
                text(
                    "INSERT INTO payment_attempts (id, tenant_id, checkout_id, checkout_version, "
                    "status, amount_minor, currency, receipt) "
                    "VALUES (:id, :t, :c, :v, 'CREATED', 57995, 'INR', :r)"
                ),
                {
                    "id": uuid7(),
                    "t": world.tenant_id,
                    "c": ref.checkout_id,
                    "v": ref.version,
                    "r": f"rcpt-{uuid7().hex[:10]}",
                },
            )
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(ApprovalStateError) as info:
                reject_approval(
                    session,
                    tenant_id=world.tenant_id,
                    checkout=ref,
                    principal=world.principal,
                    reason="buyer_declined",
                    correlation_id=uuid7(),
                )
        assert info.value.reason == "attempt_in_flight"
        assert info.value.code is RecoveryCode.CONCURRENT_OPERATION
        assert version_status(adm_kernel_engine, world, ref) == "APPROVED"


# ------------------------------------------------------------------------------- expire


class TestExpireStaleApprovals:
    def test_retires_lapsed_approvals_and_their_versions(
        self, adm_kernel_engine: Engine, adm_admin_engine: Engine, world: World
    ) -> None:
        ref, record = approved(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            assert expire_stale_approvals(session, tenant_id=world.tenant_id) == 0
        with adm_admin_engine.begin() as conn:
            conn.execute(SET_TENANT, {"t": str(world.tenant_id)})
            conn.execute(
                text(
                    "UPDATE approvals SET expires_at = now() - interval '1 second' WHERE id = :id"
                ),
                {"id": record.approval_id},
            )
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            assert expire_stale_approvals(session, tenant_id=world.tenant_id) == 1
        assert approval_status(adm_kernel_engine, world, record.approval_id) == "EXPIRED"
        # APPROVED -> EXPIRED is the legal edge for a version whose consent window closed;
        # there is no way back to APPROVAL_REQUIRED, and nobody cancelled or invalidated it.
        assert version_status(adm_kernel_engine, world, ref) == "EXPIRED"
        assert head_status(adm_kernel_engine, world, ref) == ("EXPIRED", 1)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            hold = reservations.check_validity(
                session, checkout_id=ref.checkout_id, checkout_version=ref.version, lock=False
            )
        assert hold.reservation is not None
        assert hold.reservation.status is reservations.ReservationStatus.RELEASED
        expired = [p for e, p in events(adm_kernel_engine, world, ref) if e == "approval.expired"]
        assert expired[0]["version_transition"] == {"from": "APPROVED", "to": "EXPIRED"}
        # Idempotent: a second sweep finds nothing.
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            assert expire_stale_approvals(session, tenant_id=world.tenant_id) == 0

    def test_a_live_approval_is_left_alone(self, adm_kernel_engine: Engine, world: World) -> None:
        ref, record = approved(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            assert expire_stale_approvals(session, tenant_id=world.tenant_id, limit=5) == 0
        assert approval_status(adm_kernel_engine, world, record.approval_id) == "RECORDED"
        assert version_status(adm_kernel_engine, world, ref) == "APPROVED"

    def test_limit_must_be_positive(self, adm_kernel_engine: Engine, world: World) -> None:
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(approvals.ApprovalError):
                expire_stale_approvals(session, tenant_id=world.tenant_id, limit=0)


# --------------------------------------------------------------------------------- hold


class TestHoldApproval:
    """A "not now" is a decision the platform can prove, and one that costs nothing.

    Every test here is the same assertion from a different angle: after a hold, the world
    is byte-for-byte where it was, plus one audit event. If any of these ever fail by
    finding something released, retired or transitioned, then the surface has quietly
    turned a buyer's hesitation into a cancellation.
    """

    def test_a_hold_changes_nothing_and_leaves_the_evidence(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        ref = awaiting_approval(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            held = defer_approval(
                session,
                tenant_id=world.tenant_id,
                checkout=ref,
                principal=world.principal,
                reason="buyer_not_now",
                correlation_id=uuid7(),
            )

        assert held.checkout == ref
        assert held.state is CheckoutState.APPROVAL_REQUIRED
        assert held.reservation is not None
        assert held.reservation.status is reservations.ReservationStatus.ACTIVE
        assert held.reservation.seconds_remaining > 0

        # The version, the head and the hold are all exactly where the buyer left them.
        assert version_status(adm_kernel_engine, world, ref) == "APPROVAL_REQUIRED"
        assert head_status(adm_kernel_engine, world, ref) == ("APPROVAL_REQUIRED", 1)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            hold = reservations.check_validity(
                session, checkout_id=ref.checkout_id, checkout_version=ref.version, lock=False
            )
            approvals_written = session.execute(
                text("SELECT count(*) FROM approvals WHERE checkout_id = :c"),
                {"c": ref.checkout_id},
            ).scalar_one()
        assert hold.code is RecoveryCode.OK
        assert hold.reservation is not None
        assert hold.reservation.status is reservations.ReservationStatus.ACTIVE
        # Declining is not deciding: there is no approval row to spend, replay or expire.
        assert approvals_written == 0

        written = [p for e, p in events(adm_kernel_engine, world, ref) if e == "approval.held"]
        assert len(written) == 1
        assert written[0]["content_hash"] == ref.content_hash
        assert written[0]["version"] == ref.version
        assert written[0]["reason"] == "buyer_not_now"
        assert written[0]["version_status"] == "APPROVAL_REQUIRED"
        assert written[0]["reservation_id"] == str(held.reservation.reservation_id)
        assert written[0]["reservation_status"] == "ACTIVE"
        assert written[0]["reservation_seconds_remaining"] > 0

    def test_the_same_hash_is_still_approvable_afterwards(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        """The whole point. A buyer who wanted a minute has lost nothing by taking it."""
        ref = awaiting_approval(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            defer_approval(
                session,
                tenant_id=world.tenant_id,
                checkout=ref,
                principal=world.principal,
                reason="buyer_not_now",
                correlation_id=uuid7(),
            )
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            record = record_approval(
                session,
                tenant_id=world.tenant_id,
                checkout=ref,
                amount=TOTAL,
                principal=world.principal,
                correlation_id=uuid7(),
            )
        assert record.checkout == ref
        assert record.status is ApprovalStatus.RECORDED
        assert version_status(adm_kernel_engine, world, ref) == "APPROVED"

    def test_holding_twice_is_two_events_and_still_no_transition(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        """Being asked twice and declining twice is two facts, not a duplicate operation."""
        ref = awaiting_approval(adm_kernel_engine, world)
        for reason in ("buyer_not_now", "buyer_asked_again"):
            with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
                defer_approval(
                    session,
                    tenant_id=world.tenant_id,
                    checkout=ref,
                    principal=world.principal,
                    reason=reason,
                    correlation_id=uuid7(),
                )
        written = [p for e, p in events(adm_kernel_engine, world, ref) if e == "approval.held"]
        assert [payload["reason"] for payload in written] == [
            "buyer_not_now",
            "buyer_asked_again",
        ]
        assert version_status(adm_kernel_engine, world, ref) == "APPROVAL_REQUIRED"

    def test_a_hold_from_approved_is_refused(self, adm_kernel_engine: Engine, world: World) -> None:
        """There is nothing left to decline once a decision is recorded; that is reject."""
        ref, record = approved(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(ApprovalStateError) as info:
                defer_approval(
                    session,
                    tenant_id=world.tenant_id,
                    checkout=ref,
                    principal=world.principal,
                    reason="buyer_not_now",
                    correlation_id=uuid7(),
                )
        assert info.value.reason == "wrong_status"
        assert info.value.code is RecoveryCode.STALE_CHECKOUT
        assert version_status(adm_kernel_engine, world, ref) == "APPROVED"
        assert approval_status(adm_kernel_engine, world, record.approval_id) == "RECORDED"

    def test_a_hold_after_a_rejection_is_refused(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        ref = awaiting_approval(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            reject_approval(
                session,
                tenant_id=world.tenant_id,
                checkout=ref,
                principal=world.principal,
                reason="buyer_declined",
                correlation_id=uuid7(),
            )
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(ApprovalStateError) as info:
                defer_approval(
                    session,
                    tenant_id=world.tenant_id,
                    checkout=ref,
                    principal=world.principal,
                    reason="buyer_not_now",
                    correlation_id=uuid7(),
                )
        assert info.value.reason == "wrong_status"
        assert version_status(adm_kernel_engine, world, ref) == "CANCELLED"

    def test_a_hash_the_buyer_did_not_see_is_refused(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        """A decline that names the wrong card is evidence about nothing."""
        ref = awaiting_approval(adm_kernel_engine, world)
        forged = CheckoutRef(ref.checkout_id, ref.version, "not-the-hash")
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(ApprovalStateError) as info:
                defer_approval(
                    session,
                    tenant_id=world.tenant_id,
                    checkout=forged,
                    principal=world.principal,
                    reason="buyer_not_now",
                    correlation_id=uuid7(),
                )
        assert info.value.reason == "hash_mismatch"
        assert [e for e, _ in events(adm_kernel_engine, world, ref)].count("approval.held") == 0

    def test_a_reason_is_required(self, adm_kernel_engine: Engine, world: World) -> None:
        ref = awaiting_approval(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(approvals.ApprovalError) as info:
                defer_approval(
                    session,
                    tenant_id=world.tenant_id,
                    checkout=ref,
                    principal=world.principal,
                    reason="",
                    correlation_id=uuid7(),
                )
        assert info.value.reason == "bad_reason"

    def test_a_principal_of_another_tenant_is_refused(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        ref = awaiting_approval(adm_kernel_engine, world)
        foreign = AgentPrincipal(
            principal_id="p", tenant_id=uuid.uuid4(), actor_type=ActorType.BUYER
        )
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            with pytest.raises(ApprovalTenantError):
                defer_approval(
                    session,
                    tenant_id=world.tenant_id,
                    checkout=ref,
                    principal=foreign,
                    reason="buyer_not_now",
                    correlation_id=uuid7(),
                )


# ------------------------------------------------------------------- the amount is bound


class TestTheApprovedAmountIsTheCardsAmount:
    """The button says "Approve to pay X"; this proves the row cannot say anything else.

    Between the card the surface draws and the ``approvals`` row an admission later spends
    there are three copies of one number -- the card's total, the request's echo, and the
    stored ``amount_minor`` -- and a payments platform is only honest if all three are the
    same number. These tests read the stored row rather than the returned record, because
    the returned record is built by the code under test and the row is what a grant, a
    provider call and an auditor will actually see.
    """

    def test_the_stored_amount_is_the_card_total_to_the_paisa(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        ref, card = awaiting_approval_card(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            record = record_approval(
                session,
                tenant_id=world.tenant_id,
                checkout=ref,
                amount=card.total,
                principal=world.principal,
                correlation_id=uuid7(),
            )
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            row = session.execute(
                text(
                    "SELECT amount_minor, currency, content_hash, checkout_version "
                    "FROM approvals WHERE id = :id"
                ),
                {"id": record.approval_id},
            ).one()
        assert row.amount_minor == card.total.minor
        assert row.currency == card.total.currency
        # And the amount is bound to the same bytes and version the card named, so the
        # number cannot be right about a document the buyer was not shown.
        assert row.content_hash == card.checkout.content_hash
        assert row.checkout_version == card.checkout.version
        assert record.amount == card.total

    def test_the_audited_amount_is_the_card_total(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        """The evidence a reviewer reads carries the figure, not just a reference to it."""
        ref, card = awaiting_approval_card(adm_kernel_engine, world)
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            record_approval(
                session,
                tenant_id=world.tenant_id,
                checkout=ref,
                amount=card.total,
                principal=world.principal,
                correlation_id=uuid7(),
            )
        recorded = [p for e, p in events(adm_kernel_engine, world, ref) if e == "approval.recorded"]
        assert recorded[0]["amount"] == {
            "currency": card.total.currency,
            "minor": card.total.minor,
        }

    def test_one_paisa_either_way_is_refused_and_writes_nothing(
        self, adm_kernel_engine: Engine, world: World
    ) -> None:
        """A button that rounded, re-rendered or drifted by a paisa never reaches a row."""
        ref, card = awaiting_approval_card(adm_kernel_engine, world)
        for drift in (-1, 1):
            with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
                with pytest.raises(ApprovalStateError) as info:
                    record_approval(
                        session,
                        tenant_id=world.tenant_id,
                        checkout=ref,
                        amount=Money(card.total.minor + drift, card.total.currency),
                        principal=world.principal,
                        correlation_id=uuid7(),
                    )
            assert info.value.reason == "amount_mismatch"
        with kernel_tx(adm_kernel_engine, world.tenant_id) as session:
            written = session.execute(
                text("SELECT count(*) FROM approvals WHERE checkout_id = :c"),
                {"c": ref.checkout_id},
            ).scalar_one()
        assert written == 0
        assert version_status(adm_kernel_engine, world, ref) == "APPROVAL_REQUIRED"
