"""Policy-at-Sale Receipt tests, specification 10.2.1 and 29.1.

The suite is built around one adversary: a merchant (or a bug, or an attacker with an
UPDATE) who wants the rules of a concluded sale to be different from the rules the buyer
agreed to. Every test below is a way of trying that and being caught.

These run against real PostgreSQL as ``commerce_test_kernel``, a NOSUPERUSER NOBYPASSRLS
role, because the binding is enforced by rows and constraints, not by Python. SQLite would
prove nothing here.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

import pytest
from commerce_domain import (
    CheckoutRef,
    Money,
    PolicyKind,
    RecoveryCode,
    canonical_hash,
    canonicalize,
    uuid7,
)
from platform_db import PolicyAtSaleReceipt, set_tenant
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from transaction_kernel.receipts import (
    TARGET_ORDER,
    BindingReason,
    BuyerVisibleRef,
    ReceiptBindingError,
    ReceiptContentError,
    ReceiptDraft,
    ReceiptError,
    ReceiptImmutableError,
    SaleTerm,
    build_receipt_content,
    database_now_ms,
    issue_receipt,
    item_target,
    load_receipt,
    policy_for_order,
    return_offer_at_sale,
    verify_binding,
)

pytestmark = pytest.mark.db

KERNEL_URL = os.environ.get(
    "DATABASE_URL_TEST_KERNEL",
    "postgresql+psycopg://commerce_test_kernel:testpw@localhost:5432/commerce_test",
)
# Application roles have no DELETE on any table by design, so fixtures seed and tear down
# through an owner connection. Granting the kernel DELETE to make tests tidy would erase
# the guarantee the schema exists to provide.
ADMIN_URL = os.environ.get(
    "DATABASE_URL_TEST_ADMIN",
    "postgresql+psycopg://vedanttyagi@localhost:5432/commerce_test",
)


# ----------------------------------------------------------------------------- fixtures


def _require_db(url: str) -> Engine:
    # Two simultaneous kernel sessions are required: the contention test opens a second
    # one while the first still holds its row lock. The headroom is overflow rather than
    # a larger pool because overflow connections are closed on return -- a bigger pool
    # would sit on idle connections for the rest of the session, and this suite shares one
    # PostgreSQL instance with every other package's tests.
    engine = create_engine(url, future=True, pool_size=2, max_overflow=2)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"PostgreSQL not reachable for receipt tests: {exc}")
    return engine


@pytest.fixture(scope="session")
def kernel_engine() -> Engine:
    engine = _require_db(KERNEL_URL)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
    # A superuser bypasses row-level security unconditionally, so a suite run as one
    # passes while proving nothing about tenant scoping.
    assert row.rolsuper is False, "receipt tests must not run as a superuser"
    assert row.rolbypassrls is False, "receipt tests must not run as a BYPASSRLS role"
    return engine


@pytest.fixture(scope="session")
def admin_engine() -> Engine:
    return _require_db(ADMIN_URL)


@dataclass
class World:
    """One tenant with one merchant, plus a way to make checkout versions."""

    tenant_id: uuid.UUID
    merchant_id: uuid.UUID
    admin: Engine
    kernel: Engine
    checkouts: list[uuid.UUID] = field(default_factory=list)

    def new_checkout(
        self,
        *,
        version: int = 7,
        status: str = "APPROVAL_REQUIRED",
        total_minor: int = 39500,
    ) -> CheckoutRef:
        """Insert one checkout version and return the reference the kernel would hold."""
        checkout_id = uuid7()
        content = {
            "checkout_id": str(checkout_id),
            "version": version,
            "currency": "INR",
            "total_minor": total_minor,
            "lines": [{"sku": "sku-1", "qty": 1, "unit_minor": total_minor}],
        }
        content_hash = canonical_hash(content)
        with self.admin.begin() as conn:
            conn.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(self.tenant_id)}
            )
            conn.execute(
                text(
                    "INSERT INTO checkout_versions (id, tenant_id, merchant_id, checkout_id, "
                    "version, content, content_hash, currency, total_minor, status, immutable) "
                    "VALUES (:id, :t, :m, :c, :v, :content, :h, 'INR', :total, :status, true)"
                ),
                {
                    "id": uuid7(),
                    "t": self.tenant_id,
                    "m": self.merchant_id,
                    "c": checkout_id,
                    "v": version,
                    "content": json.dumps(content),
                    "h": content_hash,
                    "total": total_minor,
                    "status": status,
                },
            )
        self.checkouts.append(checkout_id)
        return CheckoutRef(checkout_id=checkout_id, version=version, content_hash=content_hash)

    @contextmanager
    def tx(self, tenant_id: uuid.UUID | None = None) -> Iterator[Session]:
        """A kernel-role transaction with a tenant bound, committed on clean exit.

        Each call gets a fresh Session on purpose: a reused Session's identity map would
        hand back the ORM object it loaded earlier, which would hide exactly the row edits
        these tests make behind its back.
        """
        factory = sessionmaker(bind=self.kernel, expire_on_commit=False, future=True)
        session = factory()
        try:
            with session.begin():
                set_tenant(session, tenant_id or self.tenant_id)
                yield session
        finally:
            session.close()

    def sql(self, statement: str, **params: Any) -> list[Any]:
        """Run one statement as the owner, with the tenant bound for RLS-checked tables.

        Used to read rows behind the kernel's back and to perform the tampering the
        binding tests exist to detect. The kernel role could not do the UPDATEs below on
        another tenant's rows at all, which is the point.
        """
        with self.admin.begin() as conn:
            conn.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(self.tenant_id)}
            )
            result = conn.execute(text(statement), params)
            return list(result.fetchall()) if result.returns_rows else []


@contextmanager
def _tenant_world(admin_engine: Engine, kernel_engine: Engine) -> Iterator[World]:
    """One isolated tenant, torn down whatever the test does.

    The teardown is in a ``finally`` because a failing assertion must not leave rows
    behind: the next test's ``world`` is a different tenant, so leaked rows do not fail
    anything, they just accumulate until someone reads a stale row and disbelieves it.
    """
    tenant_id, merchant_id = uuid7(), uuid7()
    with admin_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, home_region) "
                "VALUES (:id, :slug, :name, 'asia-south1')"
            ),
            # The whole hex, not a prefix: a uuid7's leading hex digits are the millisecond
            # timestamp, so two tenants built in the same test collide on the slug's
            # unique index. The random tail is the only part that distinguishes them.
            {"id": tenant_id, "slug": f"t-{tenant_id.hex}", "name": "receipt test tenant"},
        )
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        conn.execute(
            text(
                "INSERT INTO merchants (id, tenant_id, slug, name, currency) "
                "VALUES (:id, :t, :slug, :name, 'INR')"
            ),
            {
                "id": merchant_id,
                "t": tenant_id,
                "slug": f"m-{merchant_id.hex}",
                "name": "receipt test merchant",
            },
        )
    try:
        yield World(
            tenant_id=tenant_id, merchant_id=merchant_id, admin=admin_engine, kernel=kernel_engine
        )
    finally:
        with admin_engine.begin() as conn:
            conn.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
            )
            # checkout_versions references policy_at_sale_receipts, so it goes first.
            conn.execute(
                text("DELETE FROM checkout_versions WHERE tenant_id = :t"), {"t": tenant_id}
            )
            conn.execute(
                text("DELETE FROM policy_at_sale_receipts WHERE tenant_id = :t"), {"t": tenant_id}
            )
            conn.execute(text("DELETE FROM merchants WHERE tenant_id = :t"), {"t": tenant_id})
            conn.execute(text("SELECT set_config('app.tenant_id', NULL, true)"))
            conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})


@pytest.fixture
def world(admin_engine: Engine, kernel_engine: Engine) -> Iterator[World]:
    with _tenant_world(admin_engine, kernel_engine) as one:
        yield one


@pytest.fixture
def other_world(admin_engine: Engine, kernel_engine: Engine) -> Iterator[World]:
    """A second tenant, with its own merchant, checkouts and receipts.

    Isolation is only tested against a row that actually exists. Asking for an id that
    matches nothing returns None whether row-level security is enforced or switched off,
    so it proves nothing about tenant scoping.
    """
    with _tenant_world(admin_engine, kernel_engine) as two:
        yield two


# ------------------------------------------------------- the merchant's *current* policy

# Fixed identifiers for the pure content tests, so a hash comparison is about the policy
# ordering under test and not about two freshly generated UUIDs.
FIXED_TENANT = uuid.UUID("0192f000-0000-7000-8000-000000000001")
FIXED_MERCHANT = uuid.UUID("0192f000-0000-7000-8000-000000000002")
FIXED_CHECKOUT = uuid.UUID("0192f000-0000-7000-8000-000000000003")
FIXED_CORRELATION = uuid.UUID("0192f000-0000-7000-8000-000000000004")


def current_policies(
    *, refund_window_days: int = 30, refund_policy_version: int = 4
) -> tuple[SaleTerm, ...]:
    """Stand-in for the merchant's live policy configuration.

    Calling this with different arguments is what "the merchant edited their policy" means
    in these tests. Nothing in ``receipts`` may consult it after a receipt exists.
    """
    return (
        SaleTerm(
            kind=PolicyKind.REFUND,
            policy_id="pol-refund",
            policy_version=refund_policy_version,
            # Basis points, not a float percentage: a float cannot be canonicalized.
            terms={
                "window_days": refund_window_days,
                "restocking_fee_bps": 500,
                "cash_refund_allowed": True,
                "store_credit_cap": Money(200000, "INR"),
            },
            applies_to=(TARGET_ORDER, item_target("sku-1")),
            document_ref="https://merchant.example/policies/refund",
            document_hash="ZmFrZS1kb2MtaGFzaA",
        ),
        SaleTerm(
            kind=PolicyKind.RETURN,
            policy_id="pol-return",
            policy_version=1,
            terms={"allowed": True, "window_days": 7, "condition": "UNOPENED"},
        ),
        SaleTerm(
            kind=PolicyKind.CANCELLATION,
            policy_id="pol-cancel",
            policy_version=2,
            terms={"cutoff_minutes_after_order": 60, "fee_minor": 0},
        ),
        SaleTerm(
            kind=PolicyKind.SUBSTITUTION,
            policy_id="pol-sub",
            policy_version=1,
            terms={"allowed": False},
        ),
        SaleTerm(
            kind=PolicyKind.DELIVERY,
            policy_id="pol-delivery",
            policy_version=3,
            terms={"promised_days": 4, "late_refund_bps": 1000},
        ),
        SaleTerm(
            kind=PolicyKind.DISCOUNT,
            policy_id="pol-discount",
            policy_version=9,
            terms={"code": "DIWALI", "amount_off": Money(5000, "INR"), "stackable": False},
        ),
        SaleTerm(
            kind=PolicyKind.FULFILMENT,
            policy_id="pol-fulfil",
            policy_version=5,
            terms={"ships_from": "BLR", "partial_shipment_allowed": True},
        ),
    )


def pure_draft(**overrides: Any) -> ReceiptDraft:
    """A valid draft with no database behind it, for the structural rules."""
    fields: dict[str, Any] = {
        "tenant_id": FIXED_TENANT,
        "merchant_id": FIXED_MERCHANT,
        "checkout_id": FIXED_CHECKOUT,
        "checkout_version": 7,
        "checkout_hash": "checkout-hash",
        "policies": current_policies(),
        "tax_policy_version": "tax-2026.1",
        "rounding_policy_version": "round-half-even.2",
        "buyer_visible_refs": (BuyerVisibleRef(label="Terms", uri="https://x.example/t"),),
        "correlation_id": FIXED_CORRELATION,
    }
    fields.update(overrides)
    return ReceiptDraft(**fields)


def policy_index(content: Any, kind: PolicyKind) -> int:
    """Position of ``kind`` in the receipt's canonically sorted policy list.

    The list is sorted by kind, so no test may assume index 0 is the refund policy; a
    hard-coded index would quietly start asserting about a different rule.
    """
    for index, policy in enumerate(content["policies"]):
        if policy["kind"] == str(kind):
            return index
    raise AssertionError(f"{kind} missing from receipt content")


def draft_for(
    world: World, checkout: CheckoutRef, policies: tuple[SaleTerm, ...] | None = None
) -> ReceiptDraft:
    return ReceiptDraft(
        tenant_id=world.tenant_id,
        merchant_id=world.merchant_id,
        checkout_id=checkout.checkout_id,
        checkout_version=checkout.version,
        checkout_hash=checkout.content_hash,
        policies=policies if policies is not None else current_policies(),
        tax_policy_version="tax-2026.1",
        rounding_policy_version="round-half-even.2",
        buyer_visible_refs=(
            BuyerVisibleRef(label="Refund policy", uri="https://merchant.example/refunds"),
            BuyerVisibleRef(label="Shown at checkout", text_hash="c2hvd24tdGV4dC1oYXNo"),
        ),
        correlation_id=uuid7(),
    )


def _store_legacy_receipt(
    world: World, checkout: CheckoutRef, policies: tuple[SaleTerm, ...]
) -> None:
    """A receipt with a family missing, which is what every pre-RETURN receipt is.

    It cannot be issued: `build_receipt_content` refuses a draft that omits a required
    kind, and that guard is correct and stays. So one is issued properly and then the
    family is removed from the stored document and the document rehashed -- including the
    version row's second copy of the hash, or the binding would fail for tampering and the
    test would prove that instead of what it means to prove.

    What is left is a row that verifies and does not mention returns: not a forgery, just
    a receipt written before anybody had thought of the question.
    """
    with world.tx() as session:
        issued = issue_receipt(session, draft_for(world, checkout, policies=None))
    content = {
        key: (
            [entry for entry in value if entry["kind"] != str(PolicyKind.RETURN)]
            if key == "policies"
            else value
        )
        for key, value in issued.content.items()
    }
    rehashed = canonical_hash(content)
    world.sql(
        "UPDATE policy_at_sale_receipts SET content = CAST(:c AS jsonb), receipt_hash = :h "
        "WHERE id = :i",
        c=json.dumps(content),
        h=rehashed,
        i=issued.receipt_id,
    )
    world.sql(
        "UPDATE checkout_versions SET policy_receipt_hash = :h "
        "WHERE tenant_id = :t AND checkout_id = :c AND version = :v",
        h=rehashed,
        t=world.tenant_id,
        c=checkout.checkout_id,
        v=checkout.version,
    )
    del policies


# =========================================================== content: pure, no database


class TestReceiptContent:
    """Draft validation. These need no database; the rules are structural."""

    def test_policy_order_cannot_change_the_hash(self) -> None:
        """Two callers assembling the same rules in different orders must agree.

        If order leaked into the hash, a rebuilt draft would look like a policy change and
        an intact binding would read as broken.
        """
        forward = current_policies()

        def build(policies: tuple[SaleTerm, ...]) -> ReceiptDraft:
            return ReceiptDraft(
                tenant_id=FIXED_TENANT,
                merchant_id=FIXED_MERCHANT,
                checkout_id=FIXED_CHECKOUT,
                checkout_version=7,
                checkout_hash="checkout-hash",
                policies=policies,
                tax_policy_version="tax-2026.1",
                rounding_policy_version="round-half-even.2",
                buyer_visible_refs=(BuyerVisibleRef(label="Terms", uri="https://x.example/t"),),
                correlation_id=FIXED_CORRELATION,
            )

        a = build(forward)
        b = build(tuple(reversed(forward)))
        assert canonicalize(build_receipt_content(a, created_at_ms=1)) == canonicalize(
            build_receipt_content(b, created_at_ms=1)
        )

    def test_a_float_term_is_refused(self) -> None:
        """A 2.5% fee as a float cannot be canonicalized, so it could never be re-verified."""
        with pytest.raises(ReceiptContentError, match="integers only"):
            SaleTerm(
                kind=PolicyKind.REFUND,
                policy_id="pol-refund",
                policy_version=1,
                terms={"restocking_fee_pct": 2.5},
            )

    def test_a_decimal_term_is_refused(self) -> None:
        with pytest.raises(ReceiptContentError, match="integers only"):
            SaleTerm(
                kind=PolicyKind.REFUND,
                policy_id="pol-refund",
                policy_version=1,
                terms={"cap": Decimal("395.00")},
            )

    def test_a_datetime_term_is_refused(self) -> None:
        """A datetime's bytes depend on tzinfo and repr, so the hash would not reproduce.

        The receipt must be re-verifiable in five years by a process that never saw the
        one that wrote it, so a deadline is an integer epoch or an explicit ISO string.
        """
        # Matched on the guidance, not on the type name: the catch-all at the end of
        # _jsonify also says "datetime", so a looser pattern would pass with this rule
        # deleted and prove nothing.
        with pytest.raises(ReceiptContentError, match="integer epoch or an explicit"):
            SaleTerm(
                kind=PolicyKind.REFUND,
                policy_id="pol-refund",
                policy_version=1,
                terms={"window_closes_at": datetime(2026, 10, 1, 12, 0, tzinfo=UTC)},
            )
        with pytest.raises(ReceiptContentError, match="integer epoch or an explicit"):
            SaleTerm(
                kind=PolicyKind.REFUND,
                policy_id="pol-refund",
                policy_version=1,
                terms={"window_closes_on": date(2026, 10, 1)},
            )

    def test_a_bytes_term_is_refused(self) -> None:
        with pytest.raises(ReceiptContentError, match="raw bytes"):
            SaleTerm(
                kind=PolicyKind.REFUND,
                policy_id="pol-refund",
                policy_version=1,
                terms={"signature": b"\x00\x01"},
            )

    def test_an_empty_term_set_is_refused(self) -> None:
        """A kind present with no terms is the omitted-kind failure wearing a disguise.

        It satisfies the coverage check and still leaves a later resolution with nothing
        to read, which is where current policy gets substituted in.
        """
        with pytest.raises(ReceiptContentError, match="non-empty mapping"):
            SaleTerm(kind=PolicyKind.SUBSTITUTION, policy_id="pol-sub", policy_version=1, terms={})

    def test_the_same_policy_twice_for_one_kind_is_refused(self) -> None:
        """Two entries for one (kind, document) make policy_for() ambiguous forever."""
        duplicated = (
            *current_policies(),
            SaleTerm(
                kind=PolicyKind.REFUND,
                policy_id="pol-refund",
                policy_version=4,
                terms={"window_days": 30},
            ),
        )
        with pytest.raises(ReceiptContentError, match="appears twice"):
            pure_draft(policies=duplicated)

    def test_a_policy_version_below_one_is_refused(self) -> None:
        """Version 0 is the shape of an unset field, and would argue a dispute against
        a document the merchant never published."""
        with pytest.raises(ReceiptContentError, match="must be >= 1"):
            SaleTerm(
                kind=PolicyKind.REFUND,
                policy_id="pol-refund",
                policy_version=0,
                terms={"window_days": 30},
            )
        with pytest.raises(ReceiptContentError, match="must be an int"):
            SaleTerm(
                kind=PolicyKind.REFUND,
                policy_id="pol-refund",
                policy_version=cast(int, "4"),
                terms={"window_days": 30},
            )

    def test_a_draft_with_no_checkout_hash_is_refused(self) -> None:
        """Without it the receipt names no checkout body and binds to any of them."""
        with pytest.raises(ReceiptContentError, match="checkout_hash is required"):
            pure_draft(checkout_hash="")

    def test_a_non_integer_created_at_is_refused(self) -> None:
        """The frozen moment is hashed, so a float here would make the receipt unhashable."""
        with pytest.raises(ReceiptContentError, match="integer epoch"):
            build_receipt_content(pure_draft(), created_at_ms=cast(int, 1.5))

    def test_buyer_visible_reference_order_and_duplication_do_not_change_the_hash(self) -> None:
        """Same evidence listed twice, or listed backwards, is the same evidence.

        Without this an intact binding would read as broken the first time a caller
        rebuilt the draft from a differently ordered source.
        """
        terms = BuyerVisibleRef(label="Terms", uri="https://x.example/t")
        shown = BuyerVisibleRef(label="Shown at checkout", text_hash="c2hvd24")
        messy = pure_draft(buyer_visible_refs=(shown, terms, shown))
        tidy = pure_draft(buyer_visible_refs=(terms, shown))
        assert canonicalize(build_receipt_content(messy, created_at_ms=1)) == canonicalize(
            build_receipt_content(tidy, created_at_ms=1)
        )

    def test_money_terms_are_recorded_as_integer_minor_units(self) -> None:
        policy = SaleTerm(
            kind=PolicyKind.REFUND,
            policy_id="pol-refund",
            policy_version=1,
            terms={"cap": Money(39500, "INR")},
        )
        assert policy.terms["cap"] == {"currency": "INR", "minor": 39500}

    def test_a_missing_policy_kind_is_refused(self) -> None:
        """The failure mode: an omitted kind gets filled from current policy later."""
        partial = tuple(p for p in current_policies() if p.kind is not PolicyKind.SUBSTITUTION)
        with pytest.raises(ReceiptContentError, match="SUBSTITUTION"):
            ReceiptDraft(
                tenant_id=uuid7(),
                merchant_id=uuid7(),
                checkout_id=uuid7(),
                checkout_version=1,
                checkout_hash="h",
                policies=partial,
                tax_policy_version="t",
                rounding_policy_version="r",
                buyer_visible_refs=(BuyerVisibleRef(label="T", uri="https://x.example"),),
                correlation_id=uuid7(),
            )

    def test_one_document_at_two_versions_is_refused(self) -> None:
        clash = (
            *current_policies(),
            SaleTerm(
                kind=PolicyKind.DELIVERY,
                policy_id="pol-refund",  # same document id, different version
                policy_version=99,
                terms={"promised_days": 1},
            ),
        )
        with pytest.raises(ReceiptContentError, match="one sale is governed by one version"):
            ReceiptDraft(
                tenant_id=uuid7(),
                merchant_id=uuid7(),
                checkout_id=uuid7(),
                checkout_version=1,
                checkout_hash="h",
                policies=clash,
                tax_policy_version="t",
                rounding_policy_version="r",
                buyer_visible_refs=(BuyerVisibleRef(label="T", uri="https://x.example"),),
                correlation_id=uuid7(),
            )

    def test_a_reference_with_neither_uri_nor_text_hash_is_refused(self) -> None:
        with pytest.raises(ReceiptContentError, match="uri or a text_hash"):
            BuyerVisibleRef(label="Terms")

    def test_a_receipt_with_no_buyer_visible_reference_is_refused(self) -> None:
        """Evidence of what the buyer was shown is part of the receipt, not a nicety.

        Without it a dispute comes down to the merchant's word about what was on screen.
        """
        with pytest.raises(ReceiptContentError, match="buyer-visible"):
            pure_draft(buyer_visible_refs=())

    def test_a_missing_tax_or_rounding_policy_version_is_refused(self) -> None:
        """Admission fails on one paisa, so the rounding rule in force is a term of sale."""
        with pytest.raises(ReceiptContentError, match="rounding_policy_version"):
            pure_draft(tax_policy_version="")
        with pytest.raises(ReceiptContentError, match="rounding_policy_version"):
            pure_draft(rounding_policy_version="")

    def test_a_policy_with_no_applies_to_target_is_refused(self) -> None:
        """A term that names no target governs nothing, and reads as coverage anyway."""
        with pytest.raises(ReceiptContentError, match="at least one target"):
            SaleTerm(
                kind=PolicyKind.REFUND,
                policy_id="pol-refund",
                policy_version=1,
                terms={"window_days": 30},
                applies_to=(),
            )

    def test_applies_to_order_and_duplication_do_not_change_the_hash(self) -> None:
        """Targets are a set: repeating or reordering one is not a change of terms."""
        messy = SaleTerm(
            kind=PolicyKind.REFUND,
            policy_id="pol-refund",
            policy_version=1,
            terms={"window_days": 30},
            applies_to=(item_target("sku-1"), TARGET_ORDER, item_target("sku-1")),
        )
        tidy = SaleTerm(
            kind=PolicyKind.REFUND,
            policy_id="pol-refund",
            policy_version=1,
            terms={"window_days": 30},
            applies_to=(TARGET_ORDER, item_target("sku-1")),
        )
        assert messy.as_content() == tidy.as_content()


# ============================================================== issuance and immutability


class TestIssuance:
    def test_receipt_hash_is_canonical_hash_of_stored_content(self, world: World) -> None:
        """Invariant 3, checked against what PostgreSQL actually stored, not what we sent.

        JSONB does not preserve key order, so this also proves the hash survives storage.
        """
        checkout = world.new_checkout()
        with world.tx() as session:
            issued = issue_receipt(session, draft_for(world, checkout))

        rows = world.sql(
            "SELECT content, receipt_hash FROM policy_at_sale_receipts WHERE id = :i",
            i=issued.receipt_id,
        )
        stored_content, stored_hash = rows[0]
        assert stored_hash == issued.receipt_hash
        assert canonical_hash(stored_content) == stored_hash

    def test_binding_is_written_onto_the_checkout_version(self, world: World) -> None:
        """Invariant 2: the binding is two stored columns, not a convention."""
        checkout = world.new_checkout()
        with world.tx() as session:
            issued = issue_receipt(session, draft_for(world, checkout))

        rows = world.sql(
            "SELECT policy_receipt_id, policy_receipt_hash FROM checkout_versions "
            "WHERE checkout_id = :c AND version = :v",
            c=checkout.checkout_id,
            v=checkout.version,
        )
        assert rows[0][0] == issued.receipt_id
        assert rows[0][1] == issued.receipt_hash

    def test_created_at_comes_from_the_database_clock(self, world: World) -> None:
        """A pod skewed by a minute must not be able to stamp a receipt.

        ``now()`` is the transaction timestamp, so the value frozen into the hashed content
        must be exactly equal to the row's server-side ``created_at``. An application clock
        would land microseconds away and this equality would fail.
        """
        checkout = world.new_checkout()
        with world.tx() as session:
            transaction_now = database_now_ms(session)
            issued = issue_receipt(session, draft_for(world, checkout))
            assert issued.created_at_ms == transaction_now

        rows = world.sql(
            "SELECT (content->>'created_at_ms')::bigint, "
            "(EXTRACT(EPOCH FROM created_at) * 1000)::bigint "
            "FROM policy_at_sale_receipts WHERE id = :i",
            i=issued.receipt_id,
        )
        content_ms, row_created_ms = rows[0]
        assert content_ms == row_created_ms == issued.created_at_ms

    def test_a_second_receipt_for_the_same_sale_is_refused(self, world: World) -> None:
        """Invariant 1: a merchant who changed policy cannot re-freeze a frozen sale."""
        checkout = world.new_checkout()
        with world.tx() as session:
            issue_receipt(session, draft_for(world, checkout))

        with pytest.raises(ReceiptImmutableError, match="already bound"):
            with world.tx() as session:
                issue_receipt(
                    session,
                    draft_for(world, checkout, current_policies(refund_window_days=7)),
                )

        rows = world.sql(
            "SELECT count(*) FROM policy_at_sale_receipts WHERE checkout_id = :c",
            c=checkout.checkout_id,
        )
        assert rows[0][0] == 1

    def test_clearing_the_binding_column_does_not_unfreeze_the_sale(self, world: World) -> None:
        """The obvious way around invariant 1, and the reason the second check exists.

        ``policy_receipt_id`` lives on a table the kernel role may UPDATE. An operator who
        wants a tightened policy to govern a concluded sale does not need to forge
        anything: they null the binding and re-issue. The receipt row is still there, and
        the receipt table's own unique key on (tenant, checkout, version) is what makes the
        sale stay frozen -- one sale can only ever have one receipt.
        """
        checkout = world.new_checkout()
        with world.tx() as session:
            issued = issue_receipt(session, draft_for(world, checkout))

        world.sql(
            "UPDATE checkout_versions SET policy_receipt_id = NULL, policy_receipt_hash = NULL "
            "WHERE checkout_id = :c AND version = :v",
            c=checkout.checkout_id,
            v=checkout.version,
        )
        with pytest.raises(ReceiptImmutableError, match="already governs"):
            with world.tx() as session:
                issue_receipt(
                    session, draft_for(world, checkout, current_policies(refund_window_days=7))
                )

        rows = world.sql(
            "SELECT count(*) FROM policy_at_sale_receipts WHERE checkout_id = :c",
            c=checkout.checkout_id,
        )
        assert rows[0][0] == 1
        with world.tx() as session:
            reread = load_receipt(session, issued.receipt_id)
        assert reread is not None
        refund = reread.content["policies"][policy_index(reread.content, PolicyKind.REFUND)]
        assert refund["terms"]["window_days"] == 30

    def test_two_concurrent_issuers_serialize_on_the_checkout_version_lock(
        self, world: World
    ) -> None:
        """Invariant 1 under real contention: two sessions, two connections, one lock.

        The sequential test above cannot distinguish a row lock from no lock at all. Here
        the first transaction issues and then *holds* its FOR UPDATE lock while a second
        kernel session tries to freeze the same sale. The second must block -- proven by
        it still being alive after the first has finished its work -- and must lose with
        ReceiptImmutableError once the first commits.

        Without the lock the second issuer would read an unbound row, reach its own
        INSERT and lose to the unique constraint instead: still one receipt, but an
        IntegrityError that names a constraint rather than a refusal a caller can act on.
        """
        checkout = world.new_checkout()
        inside_issue = threading.Event()
        outcome: dict[str, str] = {}

        def contend() -> None:
            factory = sessionmaker(bind=world.kernel, expire_on_commit=False, future=True)
            session = factory()
            try:
                with session.begin():
                    set_tenant(session, world.tenant_id)
                    # Bounded, so a lock that is never released fails this test instead of
                    # hanging the suite forever.
                    session.execute(text("SET LOCAL lock_timeout = '30s'"))
                    inside_issue.set()
                    issue_receipt(
                        session,
                        draft_for(world, checkout, current_policies(refund_window_days=7)),
                    )
                outcome["result"] = "issued"
            except Exception as exc:  # noqa: BLE001 - the type is the assertion
                outcome["result"] = type(exc).__name__
                outcome["detail"] = str(exc)
            finally:
                session.close()

        factory = sessionmaker(bind=world.kernel, expire_on_commit=False, future=True)
        first = factory()
        contender = threading.Thread(target=contend, daemon=True)
        try:
            with first.begin():
                set_tenant(first, world.tenant_id)
                issue_receipt(first, draft_for(world, checkout))
                contender.start()
                assert inside_issue.wait(timeout=10), "the second issuer never started"
                contender.join(timeout=2.0)
                assert contender.is_alive(), "the second issuer did not block on the row lock"
                assert "result" not in outcome
            # The commit happens here, releasing the lock the contender is waiting on.
        finally:
            first.close()

        contender.join(timeout=45)
        assert not contender.is_alive(), "the second issuer never unblocked"
        assert outcome["result"] == "ReceiptImmutableError", outcome
        assert "already bound" in outcome["detail"]

        rows = world.sql(
            "SELECT count(*) FROM policy_at_sale_receipts WHERE checkout_id = :c",
            c=checkout.checkout_id,
        )
        assert rows[0][0] == 1

    def test_a_draft_naming_another_merchant_is_refused(self, world: World) -> None:
        """The receipt records whose rules these are; the wrong merchant is not a binding."""
        checkout = world.new_checkout()
        draft = draft_for(world, checkout)
        foreign = ReceiptDraft(
            tenant_id=draft.tenant_id,
            merchant_id=uuid7(),
            checkout_id=draft.checkout_id,
            checkout_version=draft.checkout_version,
            checkout_hash=draft.checkout_hash,
            policies=draft.policies,
            tax_policy_version=draft.tax_policy_version,
            rounding_policy_version=draft.rounding_policy_version,
            buyer_visible_refs=draft.buyer_visible_refs,
            correlation_id=draft.correlation_id,
        )
        with pytest.raises(ReceiptBindingError, match="belongs to"):
            with world.tx() as session:
                issue_receipt(session, foreign)

        rows = world.sql(
            "SELECT count(*) FROM policy_at_sale_receipts WHERE checkout_id = :c",
            c=checkout.checkout_id,
        )
        assert rows[0][0] == 0

    def test_issuing_before_approval_required_is_refused(self, world: World) -> None:
        """Freezing terms the buyer has not been shown records the wrong agreement."""
        checkout = world.new_checkout(status="QUOTED")
        with pytest.raises(ReceiptBindingError, match="APPROVAL_REQUIRED"):
            with world.tx() as session:
                issue_receipt(session, draft_for(world, checkout))

    def test_a_draft_naming_the_wrong_checkout_body_is_refused(self, world: World) -> None:
        """The receipt names the checkout body it governs; a wrong hash is not a binding."""
        checkout = world.new_checkout()
        wrong = CheckoutRef(
            checkout_id=checkout.checkout_id,
            version=checkout.version,
            content_hash=canonical_hash({"not": "this checkout"}),
        )
        with pytest.raises(ReceiptBindingError, match="checkout_hash"):
            with world.tx() as session:
                issue_receipt(session, draft_for(world, wrong))

    def test_issuing_for_a_tenant_other_than_the_bound_one_is_refused(self, world: World) -> None:
        """Caught in code rather than as an empty RLS result, which reads as lost data."""
        checkout = world.new_checkout()
        draft = draft_for(world, checkout)
        foreign = ReceiptDraft(
            tenant_id=uuid7(),
            merchant_id=draft.merchant_id,
            checkout_id=draft.checkout_id,
            checkout_version=draft.checkout_version,
            checkout_hash=draft.checkout_hash,
            policies=draft.policies,
            tax_policy_version=draft.tax_policy_version,
            rounding_policy_version=draft.rounding_policy_version,
            buyer_visible_refs=draft.buyer_visible_refs,
            correlation_id=draft.correlation_id,
        )
        with pytest.raises(ReceiptBindingError, match="not the tenant bound"):
            with world.tx() as session:
                issue_receipt(session, foreign)

    def test_load_receipt_refuses_a_row_that_no_longer_hashes_to_its_stored_hash(
        self, world: World
    ) -> None:
        """A caller fetching by id has no other way to learn the row was edited.

        ``load_receipt`` is the path that does not go through the checkout version, so the
        second copy of the binding is not available to it. Recomputing the hash is the only
        check it has, and it must raise rather than hand back edited terms.
        """
        checkout = world.new_checkout()
        with world.tx() as session:
            issued = issue_receipt(session, draft_for(world, checkout))
        index = policy_index(issued.content, PolicyKind.REFUND)

        world.sql(
            "UPDATE policy_at_sale_receipts "
            "SET content = jsonb_set(content, CAST(:path AS text[]), '7') WHERE id = :i",
            path=f"{{policies,{index},terms,window_days}}",
            i=issued.receipt_id,
        )
        with pytest.raises(ReceiptError, match="does not reproduce its stored hash"):
            with world.tx() as session:
                load_receipt(session, issued.receipt_id)

    def test_load_receipt_returns_none_for_an_unknown_id(self, world: World) -> None:
        with world.tx() as session:
            assert load_receipt(session, uuid7()) is None

    def test_another_tenants_receipt_is_invisible_and_governs_nothing_here(
        self, world: World, other_world: World
    ) -> None:
        """Row-level security, tested against a receipt that exists rather than an absence.

        Both reads matter: a receipt fetched by id must not cross the tenant boundary, and
        the resolver must not answer for another tenant's checkout either. A leak here
        would let one merchant's refund terms govern another merchant's sale.
        """
        their_checkout = other_world.new_checkout()
        with other_world.tx() as session:
            theirs = issue_receipt(session, draft_for(other_world, their_checkout))

        # The row really is there when its own tenant asks.
        with other_world.tx() as session:
            assert load_receipt(session, theirs.receipt_id) is not None

        with world.tx() as session:
            assert load_receipt(session, theirs.receipt_id) is None
            resolved = policy_for_order(session, their_checkout)
        assert not resolved.ok
        assert resolved.content is None
        assert resolved.reason is BindingReason.CHECKOUT_VERSION_MISSING

    def test_mutating_a_loaded_receipt_cannot_reach_the_stored_row(self, world: World) -> None:
        """``load_receipt`` hands out a deep copy for the same reason the resolver does.

        It is the path a dispute tool takes. If it returned the live JSONB attribute, a
        caller that edited what it was shown would have edited the receipt for every later
        reader in that session -- including this module's own recomputation, which would
        then report an untouched row as tampered with and stall a real refund.
        """
        checkout = world.new_checkout()
        with world.tx() as session:
            issued = issue_receipt(session, draft_for(world, checkout))
        index = policy_index(issued.content, PolicyKind.REFUND)

        with world.tx() as session:
            live_row = session.get(PolicyAtSaleReceipt, issued.receipt_id)
            assert live_row is not None
            loaded = load_receipt(session, issued.receipt_id)
            assert loaded is not None
            handed_out = cast(dict[str, Any], loaded.content)
            assert handed_out is not live_row.content, "the live row was handed out"
            handed_out["policies"][index]["terms"]["window_days"] = 1

            assert live_row.content["policies"][index]["terms"]["window_days"] == 30
            assert verify_binding(session, checkout).ok

        rows = world.sql(
            "SELECT content->'policies'->:idx->'terms'->>'window_days' "
            "FROM policy_at_sale_receipts WHERE id = :i",
            idx=index,
            i=issued.receipt_id,
        )
        assert rows[0][0] == "30"

    def test_issuing_for_an_unknown_checkout_version_is_refused(self, world: World) -> None:
        checkout = world.new_checkout()
        ghost = CheckoutRef(
            checkout_id=checkout.checkout_id, version=99, content_hash=checkout.content_hash
        )
        with pytest.raises(ReceiptBindingError, match="does not exist"):
            with world.tx() as session:
                issue_receipt(session, draft_for(world, ghost))


class TestImmutabilityAcrossPolicyChange:
    def test_a_later_policy_change_leaves_the_receipt_byte_identical(self, world: World) -> None:
        """Invariant 1, the headline case.

        Freeze a sale under a 30-day refund window, then have the merchant tighten it to 7
        days, then re-read the receipt in a fresh transaction and compare canonical bytes.
        """
        checkout = world.new_checkout()
        with world.tx() as session:
            issued = issue_receipt(session, draft_for(world, checkout, current_policies()))
        before = canonicalize(issued.content)

        # The merchant edits their policy. Nothing about the stored sale may move.
        tightened = current_policies(refund_window_days=7, refund_policy_version=5)
        assert tightened != current_policies()

        with world.tx() as session:
            reread = load_receipt(session, issued.receipt_id)
        assert reread is not None
        assert canonicalize(reread.content) == before
        assert reread.receipt_hash == issued.receipt_hash
        refund = reread.content["policies"][policy_index(reread.content, PolicyKind.REFUND)]
        assert refund["policy_version"] == 4
        assert refund["terms"]["window_days"] == 30

    def test_the_change_applies_to_the_next_sale_and_only_the_next_sale(self, world: World) -> None:
        """The receipt is not a freeze on the merchant, only on concluded sales."""
        old_sale = world.new_checkout()
        with world.tx() as session:
            issue_receipt(session, draft_for(world, old_sale, current_policies()))

        new_sale = world.new_checkout(version=1)
        with world.tx() as session:
            issue_receipt(
                session,
                draft_for(world, new_sale, current_policies(refund_window_days=7)),
            )

        with world.tx() as session:
            old_terms = policy_for_order(session, old_sale).terms_for(PolicyKind.REFUND)
            new_terms = policy_for_order(session, new_sale).terms_for(PolicyKind.REFUND)
        assert old_terms["window_days"] == 30
        assert new_terms["window_days"] == 7


# ============================================================== invariant 2: the binding


class TestBindingVerification:
    def test_an_untouched_binding_verifies(self, world: World) -> None:
        checkout = world.new_checkout()
        with world.tx() as session:
            issued = issue_receipt(session, draft_for(world, checkout))
        with world.tx() as session:
            verdict = verify_binding(session, checkout)
        assert verdict.ok
        assert verdict.code is RecoveryCode.OK
        assert verdict.reason is BindingReason.OK
        assert verdict.receipt_id == issued.receipt_id

    def test_repointing_the_checkout_at_another_receipt_is_detected(self, world: World) -> None:
        """Checkout v7 bound to receipt v12 cannot be recombined with receipt v13."""
        v7 = world.new_checkout(version=7)
        v13_sale = world.new_checkout(version=1)
        with world.tx() as session:
            issue_receipt(session, draft_for(world, v7))
            other = issue_receipt(
                session, draft_for(world, v13_sale, current_policies(refund_window_days=7))
            )

        world.sql(
            "UPDATE checkout_versions SET policy_receipt_id = :r "
            "WHERE checkout_id = :c AND version = :v",
            r=other.receipt_id,
            c=v7.checkout_id,
            v=v7.version,
        )
        with world.tx() as session:
            verdict = verify_binding(session, v7)
        assert not verdict.ok
        assert verdict.reason is BindingReason.RECEIPT_SWAPPED
        assert verdict.code is RecoveryCode.HUMAN_REVIEW_REQUIRED

    def test_repointing_id_and_hash_together_is_still_detected(self, world: World) -> None:
        """The stronger attack: update both halves of the binding consistently.

        It still fails, because the receipt names its own tenant, merchant, checkout id,
        version and checkout hash. A receipt for another sale cannot describe this one.
        """
        v7 = world.new_checkout(version=7)
        other_sale = world.new_checkout(version=1)
        with world.tx() as session:
            issue_receipt(session, draft_for(world, v7))
            other = issue_receipt(
                session, draft_for(world, other_sale, current_policies(refund_window_days=7))
            )

        world.sql(
            "UPDATE checkout_versions SET policy_receipt_id = :r, policy_receipt_hash = :h "
            "WHERE checkout_id = :c AND version = :v",
            r=other.receipt_id,
            h=other.receipt_hash,
            c=v7.checkout_id,
            v=v7.version,
        )
        with world.tx() as session:
            verdict = verify_binding(session, v7)
        assert not verdict.ok
        assert verdict.reason is BindingReason.RECEIPT_FOREIGN
        assert verdict.code is RecoveryCode.HUMAN_REVIEW_REQUIRED

    def test_a_receipt_whose_own_columns_were_repointed_is_still_foreign(
        self, world: World
    ) -> None:
        """The strongest column-level attack: make every column agree, and still lose.

        ``test_repointing_id_and_hash_together_is_still_detected`` is caught by the receipt
        row's own ``checkout_id``/``checkout_version`` columns, so it does not show that
        the *content* carries an independent copy. Here the attacker rewrites those columns
        too -- and deletes the real receipt to free the (tenant, checkout, version) unique
        slot -- so that nothing outside the hashed document contradicts the theft. The
        document still names the sale it was issued for, and that is the copy that catches
        it. This is the third of the three independent copies, tested alone.
        """
        v7 = world.new_checkout(version=7)
        other_sale = world.new_checkout(version=1)
        with world.tx() as session:
            mine = issue_receipt(session, draft_for(world, v7))
            other = issue_receipt(
                session, draft_for(world, other_sale, current_policies(refund_window_days=7))
            )

        world.sql(
            "UPDATE checkout_versions SET policy_receipt_id = :r, policy_receipt_hash = :h "
            "WHERE checkout_id = :c AND version = :v",
            r=other.receipt_id,
            h=other.receipt_hash,
            c=v7.checkout_id,
            v=v7.version,
        )
        world.sql("DELETE FROM policy_at_sale_receipts WHERE id = :i", i=mine.receipt_id)
        world.sql(
            "UPDATE policy_at_sale_receipts SET checkout_id = :c, checkout_version = :v "
            "WHERE id = :i",
            c=v7.checkout_id,
            v=v7.version,
            i=other.receipt_id,
        )

        with world.tx() as session:
            verdict = verify_binding(session, v7)
        assert not verdict.ok
        assert verdict.reason is BindingReason.RECEIPT_FOREIGN
        assert verdict.code is RecoveryCode.HUMAN_REVIEW_REQUIRED

        with world.tx() as session:
            resolved = policy_for_order(session, v7)
        assert resolved.content is None, "a stolen receipt must govern nothing"

    def test_rewriting_the_checkout_body_under_a_receipt_is_detected(self, world: World) -> None:
        """A checkout version is immutable by rule; nothing in the database enforces it.

        So an attacker edits the approved body and its ``content_hash`` together and
        presents the new hash, which slips past the staleness check because the stored row
        agrees with them. The receipt still names the body the buyer actually approved, and
        that mismatch is what stops the edited checkout from inheriting the old terms.
        """
        checkout = world.new_checkout()
        with world.tx() as session:
            issue_receipt(session, draft_for(world, checkout))

        rewritten = canonical_hash({"checkout_id": str(checkout.checkout_id), "total_minor": 1})
        world.sql(
            "UPDATE checkout_versions SET content_hash = :h "
            "WHERE checkout_id = :c AND version = :v",
            h=rewritten,
            c=checkout.checkout_id,
            v=checkout.version,
        )

        with world.tx() as session:
            verdict = verify_binding(
                session,
                CheckoutRef(
                    checkout_id=checkout.checkout_id,
                    version=checkout.version,
                    content_hash=rewritten,
                ),
            )
        assert not verdict.ok
        assert verdict.reason is BindingReason.RECEIPT_CHECKOUT_HASH_MISMATCH
        assert verdict.code is RecoveryCode.HUMAN_REVIEW_REQUIRED

    def test_content_that_is_not_a_receipt_document_is_a_verdict_not_an_exception(
        self, world: World
    ) -> None:
        """A hash that reproduces is not proof the row holds a receipt.

        ``canonical_hash`` canonicalizes a bare array perfectly well, so an attacker can
        store ``[]`` with its own honest hash and copy that hash onto the checkout version.
        Recomputation passes and both stored copies agree; only the document's shape is
        wrong. That has to reach the admission transaction as a verdict, because an
        AttributeError here rolls the transaction back with nothing to tell the buyer.
        """
        checkout = world.new_checkout()
        with world.tx() as session:
            issued = issue_receipt(session, draft_for(world, checkout))

        forged: list[str] = ["not", "a", "receipt"]
        forged_hash = canonical_hash(forged)
        world.sql(
            "UPDATE policy_at_sale_receipts SET content = CAST(:c AS jsonb), receipt_hash = :h "
            "WHERE id = :i",
            c=json.dumps(forged),
            h=forged_hash,
            i=issued.receipt_id,
        )
        world.sql(
            "UPDATE checkout_versions SET policy_receipt_hash = :h "
            "WHERE checkout_id = :c AND version = :v",
            h=forged_hash,
            c=checkout.checkout_id,
            v=checkout.version,
        )

        with world.tx() as session:
            verdict = verify_binding(session, checkout)
        assert not verdict.ok
        assert verdict.reason is BindingReason.RECEIPT_CONTENT_TAMPERED
        assert verdict.code is RecoveryCode.HUMAN_REVIEW_REQUIRED

        with world.tx() as session:
            resolved = policy_for_order(session, checkout)
        assert resolved.content is None

        # load_receipt has no second copy to fall back on, so it must refuse in its own
        # documented way rather than raising TypeError out of a dictionary read.
        with pytest.raises(ReceiptError, match="not a receipt document"):
            with world.tx() as session:
                load_receipt(session, issued.receipt_id)

    def test_a_half_cleared_binding_does_not_verify(self, world: World) -> None:
        """Both binding columns are required. One of them alone is not a binding."""
        checkout = world.new_checkout()
        with world.tx() as session:
            issue_receipt(session, draft_for(world, checkout))
        world.sql(
            "UPDATE checkout_versions SET policy_receipt_hash = NULL "
            "WHERE checkout_id = :c AND version = :v",
            c=checkout.checkout_id,
            v=checkout.version,
        )
        with world.tx() as session:
            verdict = verify_binding(session, checkout)
        assert verdict.reason is BindingReason.RECEIPT_NOT_BOUND
        assert verdict.code is RecoveryCode.HUMAN_REVIEW_REQUIRED

    def test_editing_a_stored_term_is_detected(self, world: World) -> None:
        """The cheapest forgery: change a term, leave the hash. Recomputation catches it."""
        checkout = world.new_checkout()
        with world.tx() as session:
            issued = issue_receipt(session, draft_for(world, checkout))

        index = policy_index(issued.content, PolicyKind.REFUND)
        world.sql(
            "UPDATE policy_at_sale_receipts "
            "SET content = jsonb_set(content, CAST(:path AS text[]), '7') WHERE id = :i",
            path=f"{{policies,{index},terms,window_days}}",
            i=issued.receipt_id,
        )
        with world.tx() as session:
            verdict = verify_binding(session, checkout)
        assert verdict.reason is BindingReason.RECEIPT_CONTENT_TAMPERED
        assert verdict.code is RecoveryCode.HUMAN_REVIEW_REQUIRED

    def test_editing_a_term_and_its_hash_together_is_still_detected(self, world: World) -> None:
        """Rehashing the edited document defeats recomputation but not the second copy.

        The checkout version still holds the hash of the receipt as issued, so a receipt
        rewritten in place no longer matches the version it is supposed to govern.
        """
        checkout = world.new_checkout()
        with world.tx() as session:
            issued = issue_receipt(session, draft_for(world, checkout))

        index = policy_index(issued.content, PolicyKind.REFUND)
        forged = dict(issued.content)
        forged["policies"] = [dict(p) for p in forged["policies"]]
        forged["policies"][index]["terms"] = {
            **forged["policies"][index]["terms"],
            "window_days": 7,
        }
        world.sql(
            "UPDATE policy_at_sale_receipts SET content = CAST(:c AS jsonb), receipt_hash = :h "
            "WHERE id = :i",
            c=json.dumps(forged),
            h=canonical_hash(forged),
            i=issued.receipt_id,
        )
        with world.tx() as session:
            verdict = verify_binding(session, checkout)
        assert verdict.reason is BindingReason.RECEIPT_SWAPPED
        assert verdict.code is RecoveryCode.HUMAN_REVIEW_REQUIRED

    def test_content_that_cannot_be_canonicalized_is_a_verdict_not_an_exception(
        self, world: World
    ) -> None:
        """verify_binding runs inside the admission transaction and must never raise.

        A float in the stored JSONB cannot have come from ``issue_receipt`` -- the profile
        refuses one -- so recomputation throws. That has to reach the kernel as a verdict
        carrying a recovery code, not as an unhandled exception that rolls the transaction
        back with nothing to tell the buyer.
        """
        checkout = world.new_checkout()
        with world.tx() as session:
            issued = issue_receipt(session, draft_for(world, checkout))
        index = policy_index(issued.content, PolicyKind.REFUND)

        world.sql(
            "UPDATE policy_at_sale_receipts "
            "SET content = jsonb_set(content, CAST(:path AS text[]), '2.5') WHERE id = :i",
            path=f"{{policies,{index},terms,restocking_fee_bps}}",
            i=issued.receipt_id,
        )
        with world.tx() as session:
            verdict = verify_binding(session, checkout)
        assert verdict.reason is BindingReason.RECEIPT_CONTENT_TAMPERED
        assert verdict.code is RecoveryCode.HUMAN_REVIEW_REQUIRED

    def test_an_unbound_checkout_version_does_not_verify(self, world: World) -> None:
        checkout = world.new_checkout()
        with world.tx() as session:
            verdict = verify_binding(session, checkout)
        assert verdict.reason is BindingReason.RECEIPT_NOT_BOUND
        assert verdict.receipt_id is None

    def test_a_stale_checkout_hash_is_reported_as_stale_not_as_tampering(
        self, world: World
    ) -> None:
        """A caller holding a superseded body needs STALE_CHECKOUT, not a review queue."""
        checkout = world.new_checkout()
        with world.tx() as session:
            issue_receipt(session, draft_for(world, checkout))
        stale = CheckoutRef(
            checkout_id=checkout.checkout_id,
            version=checkout.version,
            content_hash=canonical_hash({"an": "older body"}),
        )
        with world.tx() as session:
            verdict = verify_binding(session, stale)
        assert verdict.reason is BindingReason.CHECKOUT_HASH_MISMATCH
        assert verdict.code is RecoveryCode.STALE_CHECKOUT

    def test_a_missing_checkout_version_does_not_verify(self, world: World) -> None:
        ghost = CheckoutRef(checkout_id=uuid7(), version=1, content_hash="x")
        with world.tx() as session:
            verdict = verify_binding(session, ghost)
        assert verdict.reason is BindingReason.CHECKOUT_VERSION_MISSING


# ================================================= invariant 4: the at-sale policy resolver


class TestPolicyForOrder:
    def test_it_returns_the_at_sale_rule_not_the_tightened_one(self, world: World) -> None:
        """A merchant who tightened their refund rule yesterday must not narrow last week.

        The sale is frozen at 30 days and a 5% restocking fee. The merchant then moves to
        7 days. The resolver still answers 30.
        """
        checkout = world.new_checkout()
        with world.tx() as session:
            issue_receipt(session, draft_for(world, checkout, current_policies()))

        tightened = current_policies(refund_window_days=7, refund_policy_version=5)
        assert tightened[0].terms["window_days"] == 7  # the merchant really did change it

        with world.tx() as session:
            resolved = policy_for_order(session, checkout)
        assert resolved.ok
        assert resolved.code is RecoveryCode.OK
        assert resolved.terms_for(PolicyKind.REFUND)["window_days"] == 30
        assert resolved.policy_for(PolicyKind.REFUND)["policy_version"] == 4
        assert resolved.terms_for(PolicyKind.REFUND)["store_credit_cap"] == {
            "currency": "INR",
            "minor": 200000,
        }

    def test_it_refuses_to_answer_when_the_binding_is_broken(self, world: World) -> None:
        """A forged receipt must govern nothing. Returning terms here funds a bad refund."""
        v7 = world.new_checkout(version=7)
        other_sale = world.new_checkout(version=1)
        with world.tx() as session:
            issue_receipt(session, draft_for(world, v7))
            other = issue_receipt(
                session, draft_for(world, other_sale, current_policies(refund_window_days=7))
            )
        world.sql(
            "UPDATE checkout_versions SET policy_receipt_id = :r, policy_receipt_hash = :h "
            "WHERE checkout_id = :c AND version = :v",
            r=other.receipt_id,
            h=other.receipt_hash,
            c=v7.checkout_id,
            v=v7.version,
        )
        with world.tx() as session:
            resolved = policy_for_order(session, v7)
        assert not resolved.ok
        assert resolved.content is None
        assert resolved.code is RecoveryCode.HUMAN_REVIEW_REQUIRED
        with pytest.raises(ReceiptError, match="no at-sale policy"):
            _ = resolved.policies

    def test_mutating_the_returned_content_cannot_reach_the_stored_receipt(
        self, world: World
    ) -> None:
        """The JSONB column is a live ORM attribute shared through the session identity map.

        Handing it out directly would mean a caller who shortened a refund window on the
        object it was given had, in that same session, edited the receipt every later
        reader sees -- including this module's own hash recomputation, which would then
        report an untouched row as tampered with and stall a real refund.

        ``live_row`` is loaded and held for the length of the block on purpose. SQLAlchemy's
        identity map holds weak references, so a receipt nobody keeps is silently re-loaded
        from the database and the corruption hides itself. The admission transaction does
        hold its rows, so this is the realistic shape.
        """
        checkout = world.new_checkout()
        with world.tx() as session:
            issued = issue_receipt(session, draft_for(world, checkout))
        index = policy_index(issued.content, PolicyKind.REFUND)

        with world.tx() as session:
            live_row = session.get(PolicyAtSaleReceipt, issued.receipt_id)
            assert live_row is not None
            resolved = policy_for_order(session, checkout)
            assert resolved.content is not None
            # cast: the API hands out a Mapping precisely so this is not a normal move.
            handed_out = cast(dict[str, Any], resolved.content)
            assert handed_out is not live_row.content, "the live row was handed out"
            handed_out["policies"][index]["terms"]["window_days"] = 1
            handed_out["schema"] = "tampered"

            again = policy_for_order(session, checkout)
            assert again.code is RecoveryCode.OK, "the caller's edit corrupted the session"
            assert again.terms_for(PolicyKind.REFUND)["window_days"] == 30
            assert verify_binding(session, checkout).ok
            assert live_row.content["policies"][index]["terms"]["window_days"] == 30

        rows = world.sql(
            "SELECT receipt_hash, content->'policies'->:idx->'terms'->>'window_days', "
            "content->>'schema' FROM policy_at_sale_receipts WHERE id = :i",
            idx=index,
            i=issued.receipt_id,
        )
        assert rows[0][0] == issued.receipt_hash
        assert rows[0][1] == "30"
        assert rows[0][2] == "policy_at_sale_receipt/1"

    def test_every_required_policy_kind_survives_to_resolution(self, world: World) -> None:
        """A gap at resolution time is what gets filled from current policy. There is none."""
        checkout = world.new_checkout()
        with world.tx() as session:
            issue_receipt(session, draft_for(world, checkout))
        with world.tx() as session:
            resolved = policy_for_order(session, checkout)
        for kind in PolicyKind:
            assert resolved.terms_for(kind), f"{kind} missing from the at-sale receipt"
        assert resolved.content is not None
        assert resolved.content["tax_policy_version"] == "tax-2026.1"
        assert resolved.content["rounding_policy_version"] == "round-half-even.2"


class TestReturnOfferAtSale:
    """Whether the goods may go back, answered from the sale's own receipt.

    The rule this class pins is the one that separates a return from a refund: a refund
    window is a restriction on a promise already made, so silence is generous; a return is
    the promise itself, so silence is a no. Getting that backwards would commit a shop to
    receiving, inspecting and accepting goods on terms nobody ever wrote.
    """

    def _offer(self, world: World, checkout: Any, *, now_ms: int | None = None) -> Any:
        with world.tx() as session:
            at = database_now_ms(session) if now_ms is None else now_ms
            return return_offer_at_sale(session, checkout, now_ms=at)

    def test_a_shop_that_offers_returns_offers_them(self, world: World) -> None:
        checkout = world.new_checkout()
        with world.tx() as session:
            issue_receipt(session, draft_for(world, checkout, current_policies()))

        offer = self._offer(world, checkout)
        assert offer.offered
        assert offer.window_days == 7
        assert offer.condition == "UNOPENED"
        assert offer.closes_at_ms is not None and offer.closes_at_ms > offer.now_ms

    def test_a_receipt_with_no_return_family_offers_nothing(self, world: World) -> None:
        """The case every receipt issued before this family existed is in.

        Read as an offer, silence would grow a Return button on every historical order in
        the shop, for a promise no merchant made. It is the one place where answering "not
        offered" to a question the record cannot answer is the generous reading, because
        what is withheld is a control rather than somebody's money.
        """
        without = tuple(p for p in current_policies() if p.kind is not PolicyKind.RETURN)
        checkout = world.new_checkout()
        # Built around the guard, because `issue_receipt` refuses an incomplete draft --
        # which is exactly why this state can only be a receipt from before it existed.
        _store_legacy_receipt(world, checkout, without)

        offer = self._offer(world, checkout)
        assert not offer.offered
        assert offer.window_days is None

    def test_a_merchant_who_withdraws_returns_does_not_reach_a_finished_sale(
        self, world: World
    ) -> None:
        """The receipt's whole claim, on this family.

        The sale is made while returns are offered. The merchant then stops offering them.
        The resolver still answers for the sale, because there is no path from it to what
        the shop says today.
        """
        checkout = world.new_checkout()
        with world.tx() as session:
            issue_receipt(session, draft_for(world, checkout, current_policies()))

        withdrawn = tuple(
            SaleTerm(
                kind=p.kind,
                policy_id=p.policy_id,
                policy_version=p.policy_version + 1,
                terms={"allowed": False} if p.kind is PolicyKind.RETURN else p.terms,
                applies_to=p.applies_to,
            )
            for p in current_policies()
        )
        assert next(p for p in withdrawn if p.kind is PolicyKind.RETURN).terms == {"allowed": False}

        assert self._offer(world, checkout).offered, (
            "a sale made while returns were offered keeps them"
        )

    def test_a_window_that_has_closed_is_no_longer_an_offer(self, world: World) -> None:
        checkout = world.new_checkout()
        with world.tx() as session:
            issue_receipt(session, draft_for(world, checkout, current_policies()))

        with world.tx() as session:
            far = database_now_ms(session) + 8 * 86_400_000
        offer = self._offer(world, checkout, now_ms=far)
        assert not offer.offered
        assert offer.window_days == 7, "the terms still say what they said; the clock moved"

    def test_a_binding_that_does_not_verify_is_not_an_offer(self, world: World) -> None:
        checkout = world.new_checkout()
        with world.tx() as session:
            issue_receipt(session, draft_for(world, checkout, current_policies()))

        stale = CheckoutRef(checkout.checkout_id, checkout.version, "not-the-stored-hash")
        assert not self._offer(world, stale).offered
