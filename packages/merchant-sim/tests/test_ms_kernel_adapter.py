"""The simulator as a MerchantStateSource, ADR 0003 D6 and admission step 8.

Two claims matter most and both are pinned against the real kernel and real PostgreSQL:

1. An unchanged store reproduces the approved content hash byte for byte. If it did not,
   every honest re-submission would be refused as a material change.
2. A price change moves the total and the hash; a stock decrement below the approved
   quantity flips ``all_available``. Those are the two levers of the demo's N -> N+1
   moment, and the last test runs the actual ``admit()`` through them.

The pure tests need no database. The ``db``-marked ones run as ``commerce_test_kernel``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from commerce_domain import Money, canonical_hash, uuid7
from merchant_sim import (
    BasketLine,
    MerchantStore,
    ScenarioController,
    SimMerchantStateSource,
    content_from_quote,
    quote_basket,
    receipt_inputs_for,
)
from merchant_sim.kernel_adapter import RevalidationError
from merchant_sim.policy import DEFAULT_FEE_POLICY
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session
from transaction_kernel import approvals, checkouts
from transaction_kernel.admission import AdmissionRequest, admit
from transaction_kernel.checkout_content import (
    CONTENT_VERSION,
    content_hash,
    units_of,
    validate_checkout_content,
)
from transaction_kernel.contracts import ActorType, AgentPrincipal, CheckoutRef, Operation
from transaction_kernel.receipts import PolicyKind, ReceiptDraft, build_receipt_content
from transaction_kernel.recovery import RecoveryCode

pytestmark = pytest.mark.db

RICE = "INDI-STPL-001"  # 49900 paise, 5% GST -- exactly on the free-delivery threshold
MILK = "AMUL-DAIRY-001"  # 2800 paise, 0% GST
BASKET = (BasketLine(RICE, 1), BasketLine(MILK, 2))
POLICY_VERSION = "demo-grocery-policy/1"

KERNEL_URL = os.environ.get(
    "DATABASE_URL_TEST_KERNEL",
    "postgresql+psycopg://commerce_test_kernel:testpw@localhost:5432/commerce_test",
)
ADMIN_URL = os.environ.get(
    "DATABASE_URL_TEST_ADMIN",
    "postgresql+psycopg://vedanttyagi@localhost:5432/commerce_test",
)
SET_TENANT = text("SELECT set_config('app.tenant_id', :t, true)")


def frozen_clock() -> datetime:
    return datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def store() -> MerchantStore:
    return MerchantStore(clock=frozen_clock)


# ------------------------------------------------------------------------- pure: content


class TestContentFromQuote:
    def test_produces_canonical_content_the_kernel_accepts(self, store: MerchantStore) -> None:
        quote = quote_basket(BASKET, store=store).require()
        checkout_id = uuid7()
        content = content_from_quote(
            quote, checkout_id=checkout_id, version=1, policy_version=POLICY_VERSION
        )
        validate_checkout_content(content)
        assert content["content_version"] == CONTENT_VERSION
        assert content["checkout_id"] == str(checkout_id)
        assert content["total_minor"] == quote.total.minor
        assert content["subtotal_minor"] == quote.items_subtotal.minor
        assert content["tax_minor"] == quote.items_tax.minor + quote.delivery_tax.minor
        assert content["delivery_fee_minor"] == quote.delivery_fee.minor
        assert content["discount_minor"] == 0
        assert content["catalogue_revision"] == store.revision
        assert content["source_id"] == quote.freshness.source
        assert units_of(content) == {MILK: 2, RICE: 1}

    def test_lines_carry_the_reservation_shape(self, store: MerchantStore) -> None:
        quote = quote_basket(BASKET, store=store).require()
        content = content_from_quote(quote, checkout_id=uuid7(), version=1, policy_version="p")
        milk = next(line for line in content["lines"] if line["sku"] == MILK)
        assert milk["quantity"] == 2
        assert milk["unit_minor"] == 2800
        assert milk["line_minor"] == 5600

    def test_the_same_quote_hashes_the_same_regardless_of_clock(self) -> None:
        checkout_id = uuid7()
        early = MerchantStore(clock=lambda: datetime(1999, 1, 1, tzinfo=UTC))
        late = MerchantStore(clock=frozen_clock)
        one = content_from_quote(
            quote_basket(BASKET, store=early).require(),
            checkout_id=checkout_id,
            version=1,
            policy_version="p",
        )
        two = content_from_quote(
            quote_basket(BASKET, store=late).require(),
            checkout_id=checkout_id,
            version=1,
            policy_version="p",
        )
        assert content_hash(one) == content_hash(two)

    def test_delivery_fee_and_its_tax_enter_the_content(self, store: MerchantStore) -> None:
        quote = quote_basket((BasketLine(MILK, 1),), store=store).require()  # below threshold
        content = content_from_quote(quote, checkout_id=uuid7(), version=1, policy_version="p")
        assert content["delivery_fee_minor"] == 2500
        assert content["tax_minor"] == 450  # 18% on the delivery fee, no tax on milk
        assert content["total_minor"] == 2800 + 2500 + 450


# ------------------------------------------------------------------------- pure: receipt


class TestReceiptInputs:
    def test_covers_every_policy_kind_and_builds_a_valid_draft(self, store: MerchantStore) -> None:
        inputs = receipt_inputs_for(store)
        assert {policy.kind for policy in inputs.policies} == set(PolicyKind)
        draft = ReceiptDraft(
            tenant_id=uuid7(),
            merchant_id=uuid7(),
            checkout_id=uuid7(),
            checkout_version=1,
            checkout_hash="h",
            policies=inputs.policies,
            tax_policy_version=inputs.tax_policy_version,
            rounding_policy_version=inputs.rounding_policy_version,
            buyer_visible_refs=inputs.buyer_visible_refs,
            correlation_id=uuid7(),
        )
        canonical_hash(build_receipt_content(draft, created_at_ms=1))

    def test_delivery_terms_follow_the_fee_policy_in_force(self, store: MerchantStore) -> None:
        before = receipt_inputs_for(store)
        ScenarioController(store).set_delivery_fee(Money(4000, "INR"))
        after = receipt_inputs_for(store)
        delivery_before = next(p for p in before.policies if p.kind is PolicyKind.DELIVERY)
        delivery_after = next(p for p in after.policies if p.kind is PolicyKind.DELIVERY)
        assert delivery_before.terms["base_fee"] == {"currency": "INR", "minor": 2500}
        assert delivery_after.terms["base_fee"] == {"currency": "INR", "minor": 4000}
        assert before.buyer_visible_refs[0].text_hash != after.buyer_visible_refs[0].text_hash

    def test_accepts_a_fee_policy_directly(self) -> None:
        inputs = receipt_inputs_for(DEFAULT_FEE_POLICY, policy_version=3)
        assert all(policy.policy_version == 3 for policy in inputs.policies)

    def test_source_constructor_refuses_empty_versions(self, store: MerchantStore) -> None:
        with pytest.raises(ValueError, match="policy_version"):
            SimMerchantStateSource(store, policy_version="")
        with pytest.raises(ValueError, match="source_id"):
            SimMerchantStateSource(store, policy_version="p", source_id="")


# ------------------------------------------------------------------------------ database


def _engine(url: str) -> Engine:
    engine = create_engine(url, future=True, pool_size=2, max_overflow=2)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"PostgreSQL not reachable: {exc}")
    return engine


@pytest.fixture(scope="session")
def kernel_engine() -> Engine:
    engine = _engine(KERNEL_URL)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
    assert not row.rolsuper and not row.rolbypassrls, "adapter tests must not bypass RLS"
    return engine


@pytest.fixture(scope="session")
def admin_engine() -> Engine:
    return _engine(ADMIN_URL)


@dataclass(frozen=True, slots=True)
class World:
    tenant_id: uuid.UUID
    merchant_id: uuid.UUID
    basket_id: uuid.UUID
    principal: AgentPrincipal


@pytest.fixture
def world(admin_engine: Engine) -> Iterator[World]:
    tenant_id, merchant_id, basket_id = uuid.uuid4(), uuid7(), uuid7()
    slug = f"ms-{tenant_id.hex[:8]}"
    with admin_engine.begin() as conn:
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
        principal=AgentPrincipal(
            principal_id="buyer-1",
            tenant_id=tenant_id,
            actor_type=ActorType.BUYER,
            merchant_id=merchant_id,
            buyer_ref="buyer-1",
            capabilities=frozenset({"checkout.submit_approved"}),
        ),
    )
    with admin_engine.begin() as conn:
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


def approved_checkout(engine: Engine, world: World, store: MerchantStore) -> CheckoutRef:
    """Quote -> create -> freeze_for_approval -> record_approval, all through the real modules."""
    quote = quote_basket(BASKET, store=store).require()
    with kernel_tx(engine, world.tenant_id) as session:
        created = checkouts.create_checkout(
            session,
            tenant_id=world.tenant_id,
            merchant_id=world.merchant_id,
            basket_id=world.basket_id,
            buyer_ref="buyer-1",
            content=content_from_quote(
                quote, checkout_id=uuid7(), version=1, policy_version=POLICY_VERSION
            ),
            correlation_id=uuid7(),
        )
        checkouts.freeze_for_approval(
            session,
            tenant_id=world.tenant_id,
            checkout=created.ref,
            receipt=receipt_inputs_for(store),
            correlation_id=uuid7(),
        )
    with kernel_tx(engine, world.tenant_id) as session:
        approvals.record_approval(
            session,
            tenant_id=world.tenant_id,
            checkout=created.ref,
            amount=quote.total,
            principal=world.principal,
            correlation_id=uuid7(),
        )
    return created.ref


@pytest.mark.db
class TestRevalidate:
    def test_an_unchanged_store_reproduces_the_approved_hash_exactly(
        self, kernel_engine: Engine, world: World, store: MerchantStore
    ) -> None:
        ref = approved_checkout(kernel_engine, world, store)
        source = SimMerchantStateSource(store, policy_version=POLICY_VERSION)
        with kernel_tx(kernel_engine, world.tenant_id) as session:
            state = source.revalidate(session, checkout_id=ref.checkout_id, version=ref.version)
        assert state.all_available is True
        assert state.total == Money(57995, "INR")
        assert state.policy_version == POLICY_VERSION
        assert state.content is not None
        assert content_hash(state.content) == ref.content_hash
        # What admission actually hashes, after its own re-stamp.
        assert (
            canonical_hash(state.content_for_hash(ref.checkout_id, ref.version)) == ref.content_hash
        )

    def test_a_price_change_moves_the_total_and_the_hash(
        self, kernel_engine: Engine, world: World, store: MerchantStore
    ) -> None:
        ref = approved_checkout(kernel_engine, world, store)
        ScenarioController(store).set_price(MILK, Money(3000, "INR"))
        source = SimMerchantStateSource(store, policy_version=POLICY_VERSION)
        with kernel_tx(kernel_engine, world.tenant_id) as session:
            state = source.revalidate(session, checkout_id=ref.checkout_id, version=ref.version)
        assert state.all_available is True
        assert state.total == Money(57995 + 400, "INR")
        assert state.content is not None
        assert content_hash(state.content) != ref.content_hash
        assert state.content["catalogue_revision"] == 1

    def test_a_stock_decrement_below_the_approved_quantity_flips_availability(
        self, kernel_engine: Engine, world: World, store: MerchantStore
    ) -> None:
        ref = approved_checkout(kernel_engine, world, store)
        ScenarioController(store).set_stock(MILK, 1)  # the basket wants 2
        source = SimMerchantStateSource(store, policy_version=POLICY_VERSION)
        with kernel_tx(kernel_engine, world.tenant_id) as session:
            state = source.revalidate(session, checkout_id=ref.checkout_id, version=ref.version)
        assert state.all_available is False
        # The reported basket is what can still be fulfilled: rice alone, at its own total.
        assert dict(state.line_items) == {RICE: 1}
        assert state.total == Money(49900 + 2495, "INR")
        assert state.content is not None
        assert content_hash(state.content) != ref.content_hash

    def test_nothing_fulfillable_reports_a_zero_total_and_no_content(
        self, kernel_engine: Engine, world: World, store: MerchantStore
    ) -> None:
        ref = approved_checkout(kernel_engine, world, store)
        controller = ScenarioController(store)
        controller.sell_out(MILK)
        controller.make_unavailable(RICE)
        source = SimMerchantStateSource(store, policy_version=POLICY_VERSION)
        with kernel_tx(kernel_engine, world.tenant_id) as session:
            state = source.revalidate(session, checkout_id=ref.checkout_id, version=ref.version)
        assert state.all_available is False
        assert state.total == Money.zero("INR")
        assert dict(state.line_items) == {}
        assert state.content is None

    def test_an_invisible_version_fails_closed(
        self, kernel_engine: Engine, world: World, store: MerchantStore
    ) -> None:
        source = SimMerchantStateSource(store, policy_version=POLICY_VERSION)
        with kernel_tx(kernel_engine, world.tenant_id) as session:
            with pytest.raises(RevalidationError):
                source.revalidate(session, checkout_id=uuid7(), version=1)


@pytest.mark.db
class TestThroughAdmission:
    """The whole unit under the real admission transaction."""

    def _request(self, world: World, ref: CheckoutRef, total: Money) -> AdmissionRequest:
        return AdmissionRequest(
            tenant_id=world.tenant_id,
            merchant_id=world.merchant_id,
            checkout=ref,
            amount=total,
            operation=Operation.PAYMENT_CREATE_ORDER,
            idempotency_key=f"idem-{uuid7().hex[:12]}",
            principal=world.principal,
            correlation_id=uuid7(),
            approval_id=uuid7(),
        )

    def test_unchanged_store_is_admitted(
        self, kernel_engine: Engine, world: World, store: MerchantStore
    ) -> None:
        ref = approved_checkout(kernel_engine, world, store)
        source = SimMerchantStateSource(store, policy_version=POLICY_VERSION)
        with kernel_tx(kernel_engine, world.tenant_id) as session:
            decision = admit(session, self._request(world, ref, Money(57995, "INR")), source)
        assert decision.allowed, decision
        assert decision.grant_id is not None

    def test_an_injection_after_approval_is_refused_and_n_plus_one_is_canonical(
        self, kernel_engine: Engine, world: World, store: MerchantStore
    ) -> None:
        ref = approved_checkout(kernel_engine, world, store)
        ScenarioController(store).set_price(MILK, Money(3000, "INR"))
        source = SimMerchantStateSource(store, policy_version=POLICY_VERSION)
        with kernel_tx(kernel_engine, world.tenant_id) as session:
            decision = admit(session, self._request(world, ref, Money(57995, "INR")), source)
        assert not decision.allowed
        assert decision.code is RecoveryCode.REAPPROVAL_REQUIRED
        assert decision.next_version == 2
        delta = next(d for d in decision.deltas if d.field_path == "total")
        assert (delta.approved, delta.current) == (57995, 58395)

        with kernel_tx(kernel_engine, world.tenant_id) as session:
            versions = checkouts.read_versions(
                session, tenant_id=world.tenant_id, checkout_id=ref.checkout_id
            )
        assert [v.status.value for v in versions] == ["INVALIDATED", "APPROVAL_REQUIRED"]
        n_plus_one = versions[1]
        # Admission wrote N+1 from the adapter's content: it is canonical, re-stamped to
        # version 2, and the stored hash is the canonical hash of that document.
        validate_checkout_content(n_plus_one.content)
        assert n_plus_one.content["version"] == 2
        assert n_plus_one.content["total_minor"] == 58395
        assert content_hash(n_plus_one.content) == n_plus_one.content_hash
        assert n_plus_one.policy_receipt_id is None

        # The supersede path: give N+1 its receipt and hold, then it can be approved.
        with kernel_tx(kernel_engine, world.tenant_id) as session:
            card = checkouts.freeze_for_approval(
                session,
                tenant_id=world.tenant_id,
                checkout=n_plus_one.ref,
                receipt=receipt_inputs_for(store),
                correlation_id=uuid7(),
            )
            assert card.total == Money(58395, "INR")
        with kernel_tx(kernel_engine, world.tenant_id) as session:
            record = approvals.record_approval(
                session,
                tenant_id=world.tenant_id,
                checkout=n_plus_one.ref,
                amount=Money(58395, "INR"),
                principal=world.principal,
                correlation_id=uuid7(),
            )
        assert record.checkout == n_plus_one.ref
