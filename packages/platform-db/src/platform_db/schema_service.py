"""Service-layer schema: the tables the HTTP API and the worker own around the kernel.

These tables carry no money decision of their own. They are the head records, inbox and
evidence that let the eleven-step demonstration (ADR 0003) run over HTTP and leave a
trail a reviewer can inspect. Three of them -- ``orders``, ``provider_requests`` and
``reconciliation_runs`` -- are financial evidence and are therefore kernel-write-only
(:data:`platform_db.roles.FINANCIAL_TABLES`); the rest are written by the API under the
app role or by the worker.

They share ``Base`` with :mod:`platform_db.schema` so one metadata, one naming convention
and one Alembic history cover both halves. The rules of that module hold here too: every
tenant-owned table has a non-null ``tenant_id`` in every uniqueness constraint, money is
``BigInteger`` minor units beside an ISO 4217 code, and CHECK constraints on a ``status``
column are written ``status IN (...)`` so the state-agreement test's parser can read them.

``api_sessions`` is the one table here without row-level security, like ``tenants``:
resolving a bearer token is the step that *discovers* the tenant, so it cannot already
require one.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .schema import Base, _now, _pk, _tenant_fk


def _merchant_fk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), ForeignKey("merchants.id"), nullable=False)


def _updated_at() -> Mapped[datetime]:
    """Set on INSERT by the server; writers must set it on UPDATE (no trigger exists)."""
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ------------------------------------------------------------------------------ sessions


class ApiSession(Base):
    """One minted session: a buyer, the agent acting for them, an operator, or a merchant.

    Deliberately NOT under row-level security. The bearer token is looked up before any
    tenant is known, and the row is what tells the request which tenant it belongs to.
    Only the token's SHA-256 is stored, so a leaked table cannot be replayed.
    """

    __tablename__ = "api_sessions"
    __table_args__ = (
        CheckConstraint(
            "actor_type IN ('BUYER','AGENT','OPERATOR','MERCHANT')", name="actor_type_enum"
        ),
        # A merchant session has no buyer, and the database says so rather than the
        # application remembering to. Without it the only thing stopping a merchant row
        # from carrying a buyer reference is that nothing currently writes one, and a
        # merchant session that looked like a buyer's would be routed to the buyer's
        # copilot by ``is_buyer_principal``.
        CheckConstraint(
            "(actor_type = 'MERCHANT') = (buyer_ref IS NULL)", name="merchant_has_no_buyer"
        ),
        Index("ix_api_sessions_tenant_buyer", "tenant_id", "buyer_ref"),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    merchant_id: Mapped[uuid.UUID] = _merchant_fk()
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    #: Null for a merchant session and never for any other, which the check constraint
    #: above enforces in both directions. It was NOT NULL until merchant sessions existed,
    #: and a placeholder here would have made a merchant look like a buyer to every caller
    #: that asks ``buyer_ref is not None``.
    buyer_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ------------------------------------------------------------------- carts + checkouts


class Cart(Base):
    """Pre-checkout intent. Lines and the last quote are JSON because a cart has no
    invariant of its own: the deterministic quote is recomputed on every read and the
    binding copy lives in ``checkout_versions.content`` once checkout starts."""

    __tablename__ = "carts"
    __table_args__ = (
        CheckConstraint("status IN ('OPEN','CHECKED_OUT','ABANDONED')", name="status_enum"),
        Index("ix_carts_tenant_buyer", "tenant_id", "buyer_ref"),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    merchant_id: Mapped[uuid.UUID] = _merchant_fk()
    buyer_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    shopping_context: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    lines: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    quote: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    catalogue_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'OPEN'"))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _updated_at()


class Checkout(Base):
    """The checkout head: identity plus a denormalised pointer to the current version.

    ``id`` is the ``checkout_id`` every kernel table already carries, so this row is the
    thing those foreign-key-less references finally point at. ``status`` mirrors the
    current version's ``CheckoutState`` for cheap reads; the version row stays the truth,
    which is why the CHECK is a copy of ``checkout_versions.status_enum`` and not a new
    vocabulary.
    """

    __tablename__ = "checkouts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "cart_id"),
        CheckConstraint("current_version >= 1", name="current_version_positive"),
        CheckConstraint(
            "status IN ('DRAFT','QUOTED','RESERVED','APPROVAL_REQUIRED','APPROVED',"
            "'EXECUTION_PENDING','AWAITING_PAYMENT','PAID','PAYMENT_FAILED',"
            "'PAYMENT_UNKNOWN','INVALIDATED','INVALIDATED_AWAITING_PAYMENT_RESULT',"
            "'CANCELLED','EXPIRED')",
            name="status_enum",
        ),
        Index("ix_checkouts_tenant_buyer", "tenant_id", "buyer_ref"),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    merchant_id: Mapped[uuid.UUID] = _merchant_fk()
    cart_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("carts.id"), nullable=False
    )
    buyer_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(48), nullable=False)
    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _updated_at()


# ------------------------------------------------------------------------ webhook inbox


class WebhookInboxRow(Base):
    """One received Razorpay webhook, stored as the exact bytes that were signed.

    The receiver (ADR D7) claims a row with ``INSERT ... ON CONFLICT (tenant_id,
    dedup_key) DO NOTHING`` before it parses anything, so a redelivered event is a
    no-op at the database rather than a second state change. ``raw_body`` is kept so the
    scenario controller can replay the real bytes and so the Inspector can show what was
    actually received; ``headers_redacted`` carries only the event id, the signature and
    the content type.
    """

    __tablename__ = "webhook_inbox"
    __table_args__ = (
        UniqueConstraint("tenant_id", "dedup_key"),
        CheckConstraint(
            "apply_status IN ('RECEIVED','APPLIED','IGNORED','FAILED')",
            name="apply_status_enum",
        ),
        CheckConstraint("duplicate_count >= 0", name="duplicate_count_non_negative"),
        Index("ix_webhook_inbox_tenant_order", "tenant_id", "order_id"),
        Index("ix_webhook_inbox_tenant_payment", "tenant_id", "payment_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    dedup_key: Mapped[str] = mapped_column(String(160), nullable=False)
    provider_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    body_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_body: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    headers_redacted: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    signature_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)

    payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    refund_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    received_at: Mapped[datetime] = _now()
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    apply_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'RECEIVED'")
    )
    apply_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state_before: Mapped[str | None] = mapped_column(String(24), nullable=True)
    state_after: Mapped[str | None] = mapped_column(String(24), nullable=True)
    changed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    outbox_command_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


# --------------------------------------------------------------------- financial evidence


class Order(Base):
    """The confirmed sale: written once, by the kernel, from verified capture evidence.

    ``UNIQUE (tenant_id, payment_attempt_id)`` is the ON CONFLICT target that makes
    confirmation idempotent when a webhook and a reconciliation fetch both report the
    same capture. The receipt hash is copied in so the order stays bound to the policy
    the buyer saw even if the version row is later read through a different path.
    """

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("tenant_id", "payment_attempt_id"),
        CheckConstraint("checkout_version >= 1", name="checkout_version_positive"),
        CheckConstraint("total_minor >= 0", name="total_non_negative"),
        CheckConstraint(
            "status IN ('CONFIRMED','FULFILMENT_BLOCKED','CANCELLED','PARTIALLY_REFUNDED',"
            "'REFUNDED')",
            name="status_enum",
        ),
        Index("ix_orders_tenant_checkout", "tenant_id", "checkout_id"),
        # The order list pages by keyset on (created_at, id) descending within a
        # tenant. Without this the console's first page sorts the tenant's whole
        # order history; with it PostgreSQL walks the index backwards from the cursor.
        Index("ix_orders_tenant_created", "tenant_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    merchant_id: Mapped[uuid.UUID] = _merchant_fk()
    checkout_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    checkout_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payment_attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_attempts.id"), nullable=False
    )
    policy_receipt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("policy_at_sale_receipts.id"), nullable=False
    )
    policy_receipt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    total_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    # The CaptureEvidence that confirmed the order (source, provider ids, digest), so
    # "how did we learn CAPTURED" is a column on the order, not a search of the audit log.
    capture_evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _updated_at()


class ProviderRequest(Base):
    """One HTTP call to Razorpay, recorded by the kernel: proof-chain link 8.

    Exactly one row per consumed Execution Grant is what the proof verifier asserts, so
    this table is how "one grant, one provider mutation" becomes checkable after the
    fact. Nothing secret is stored: the URL carries no auth, the body is a digest and
    only header *names* are kept.
    """

    __tablename__ = "provider_requests"
    __table_args__ = (
        CheckConstraint(
            "http_status IS NULL OR http_status BETWEEN 100 AND 599", name="http_status_range"
        ),
        Index("ix_provider_requests_tenant_attempt", "tenant_id", "payment_attempt_id"),
        Index("ix_provider_requests_tenant_grant", "tenant_id", "grant_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    payment_attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_attempts.id"), nullable=False
    )
    grant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("execution_grants.id"), nullable=True
    )
    refund_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("refunds.id"), nullable=True
    )
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    header_names: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    request_at: Mapped[datetime] = _now()
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome_code: Mapped[str] = mapped_column(String(48), nullable=False)
    provider_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    response_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    transport_error: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ReconciliationRun(Base):
    """One bounded attempt to resolve an UNKNOWN outcome from provider evidence.

    ``UNIQUE (tenant_id, payment_attempt_id, reason, attempt_number)`` means a redelivered
    RECONCILE command cannot record the same attempt twice, which keeps "6 attempts then
    ESCALATED" (ADR D13) an honest count.
    """

    __tablename__ = "reconciliation_runs"
    __table_args__ = (
        # Named by hand: the convention's generated name exceeds PostgreSQL's 63-character
        # identifier limit and would be truncated differently by the server and the ORM.
        UniqueConstraint(
            "tenant_id",
            "payment_attempt_id",
            "reason",
            "attempt_number",
            name="uq_reconciliation_runs_attempt_reason_number",
        ),
        CheckConstraint("attempt_number >= 1", name="attempt_number_positive"),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    payment_attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_attempts.id"), nullable=False
    )
    refund_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("refunds.id"), nullable=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    identifiers_queried: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    raw_evidence_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision: Mapped[str] = mapped_column(String(64), nullable=False)
    resulting_transition: Mapped[str | None] = mapped_column(String(64), nullable=True)
    next_scheduled_attempt: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = _now()


# ------------------------------------------------------------------- scenario controller


class ScenarioFault(Base):
    """A worker-side fault armed by the scenario controller (ADR D11).

    Lives in the database rather than in API-process memory because the worker is a
    separate process. A fault is armed before the payment attempt it will hit exists, so
    it targets a checkout (or the whole tenant) and the worker fills in the attempt it
    consumed it against. Never mixed with organic data: rows here are demo apparatus.
    """

    __tablename__ = "scenario_faults"
    __table_args__ = (Index("ix_scenario_faults_tenant_armed", "tenant_id", "armed"),)

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    checkout_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    payment_attempt_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    armed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _now()


class ScenarioRun(Base):
    """Record of one scenario injection, cross-linked to its SCENARIO_INJECTION audit row
    so the timeline can label injected merchant changes as such."""

    __tablename__ = "scenario_runs"
    __table_args__ = (Index("ix_scenario_runs_tenant_merchant", "tenant_id", "merchant_id"),)

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    merchant_id: Mapped[uuid.UUID] = _merchant_fk()
    injection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    audit_event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = _now()


class SupportCase(Base):
    """A buyer asked for help, and a person will answer.

    This is where a refund request goes, and it is deliberately not where a refund goes.
    Nothing here carries an amount, a currency or a decision, because nothing on this path
    is entitled to compute one: the support agent may open a case and may say what the
    Policy-at-Sale Receipt promised, and it may do nothing else. What the buyer is owed is
    settled by a person on the merchant's side, reading this row.

    So the columns are exactly what a human needs to pick the case up -- who, which order,
    what they said was wrong -- and no more. A column for an amount would be a place for
    somebody to write a number nothing had authorised.

    Not a financial table: it moves no money and the kernel does not write it. It is
    tenant- and merchant-owned like every other service row, so the merchant's own helpdesk
    reads it under the same isolation as their orders.
    """

    __tablename__ = "support_cases"
    __table_args__ = (
        CheckConstraint(
            "status IN ('OPEN','ACKNOWLEDGED','RESOLVED','CLOSED')", name="support_status_enum"
        ),
        # The merchant's queue, oldest first.
        Index("ix_support_cases_tenant_merchant", "tenant_id", "merchant_id", "created_at"),
        # The helpdesk's own read: what is still waiting, oldest first. Without the status
        # in the index that page scans every case the merchant has ever had, and the
        # queue is the one read that happens on every page load rather than on demand.
        Index(
            "ix_support_cases_tenant_status",
            "tenant_id",
            "status",
            "created_at",
        ),
        # One buyer's cases on one order, for the storefront to show what it already raised.
        Index("ix_support_cases_tenant_order", "tenant_id", "order_id"),
        Index(
            "uq_support_active_buyer_order",
            "tenant_id",
            "order_id",
            "buyer_ref",
            unique=True,
            postgresql_where=text("status IN ('OPEN','ACKNOWLEDGED')"),
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    #: Whose queue this lands in. The merchant answers for their own sales.
    merchant_id: Mapped[uuid.UUID] = _merchant_fk()
    #: The order the buyer is asking about. Required: a case with no order is a support
    #: conversation, and this table is for the ones that name a purchase.
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False
    )
    #: The buyer who raised it, compared against the session before the case is shown back.
    buyer_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    #: One of the buyer-facing reason keys the order screen already uses --
    #: ``item_damaged``, ``wrong_item``, ``item_not_delivered`` and the rest. Lower case,
    #: because those strings are already on screens and in tests.
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    #: What the buyer typed, if anything. Free text, shown to a person, never parsed.
    note: Mapped[str] = mapped_column(String(1000), nullable=False, server_default=text("''"))
    #: How it was raised: the order screen, or the support agent on the buyer's behalf.
    #: Recorded because "a model opened this" is a fact a human answering it should have.
    opened_by: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'OPEN'"))
    #: Who on the merchant's side picked it up, as the operator session's own actor
    #: reference. Null until somebody does. Not a foreign key: the merchant staff table
    #: does not exist yet, and inventing one to satisfy a constraint would be inventing
    #: merchant identity, which is a larger decision than this column.
    handled_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: What the person decided, in their words, for the buyer and for the next reader.
    #: Free text and never parsed, exactly like the buyer's own note.
    #:
    #: No amount, and no column for one. A figure written here would be a number somebody
    #: typed rather than one the kernel resolved, and the moment it were displayed beside
    #: the case it would read as a promise. What is still refundable is asked of the kernel
    #: at the moment it is needed, by the helpdesk and by the buyer's own screen, through
    #: the same route.
    resolution_note: Mapped[str] = mapped_column(
        String(1000), nullable=False, server_default=text("''")
    )
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MerchantState(Base):
    """One shop's live state: the numbers a merchant can change about their own store.

    This table is where the merchant simulator's world became durable. It used to live in
    the API process (ADR 0003 D14), which had two costs that were paid every day: the shop
    forgot its prices, its stock and its running offer on every restart and every deploy,
    and no second API process could exist because it would hold a second, disagreeing copy.

    ``revision`` is the freshness token the whole platform quotes against -- a quote taken
    at revision N is stale once the shop reaches N+1, which is how a price change becomes a
    reapproval instead of a surprise. It lives here rather than in memory so that a stale
    quote stays stale across a restart, which is the honest answer: the shop did move.

    ``promotion`` is a whole offer as one JSONB document rather than five columns, because
    the simulator only ever holds one at a time and replaces it wholesale. Null means no
    offer is running, which is not the same as an offer with a zero discount.

    Not a financial table. Nothing here moves money: it is what the shop charges and what
    it has in stock, and a sale is admitted by the kernel against a quote taken from it.
    """

    __tablename__ = "merchant_state"
    __table_args__ = (
        UniqueConstraint("tenant_id", "merchant_id"),
        CheckConstraint("revision >= 0", name="merchant_state_revision_non_negative"),
        CheckConstraint("delivery_fee_minor >= 0", name="merchant_state_delivery_fee_non_negative"),
        CheckConstraint(
            "free_delivery_threshold_minor >= 0", name="merchant_state_threshold_non_negative"
        ),
        CheckConstraint(
            "delivery_tax_bp >= 0 AND delivery_tax_bp <= 10000",
            name="merchant_state_delivery_tax_in_range",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    merchant_id: Mapped[uuid.UUID] = _merchant_fk()
    #: Advances by exactly one per applied injection. The compare-and-set that used to be
    #: an unlocked read-then-write in process memory is now a locked row update.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    delivery_fee_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    free_delivery_threshold_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    delivery_tax_bp: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The one running offer, or null. Shape is ``merchant_sim.policy.Promotion``.
    promotion: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _updated_at()


class MerchantSkuState(Base):
    """What one product costs and how many of it the shop has, right now.

    One row per SKU per shop. The catalogue itself -- names, units, tax rates, images --
    is a fixture and stays one; this is only the part a merchant changes.

    ``stock_units`` carries a non-negative CHECK deliberately. It is the number the
    oversell guard is measured against, and a guard whose baseline can go negative is not
    a guard. A write that would take it below zero fails the transaction, which is the
    fail-closed direction for inventory.
    """

    __tablename__ = "merchant_sku_state"
    __table_args__ = (
        UniqueConstraint("tenant_id", "merchant_id", "sku"),
        CheckConstraint("stock_units >= 0", name="merchant_sku_stock_non_negative"),
        CheckConstraint("unit_price_minor > 0", name="merchant_sku_price_positive"),
        # The whole shop in one read: every quote needs every line's price and stock.
        Index("ix_merchant_sku_state_shop", "tenant_id", "merchant_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    merchant_id: Mapped[uuid.UUID] = _merchant_fk()
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    unit_price_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    stock_units: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The merchant's decision to sell, kept apart from having units, so that "delisted"
    #: and "sold out" remain two different answers.
    is_listed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _updated_at()


class InventoryMovement(Base):
    """Every unit that ever moved, and why. The stock number is the sum of these rows.

    THE DEFECT THIS EXISTS TO REMOVE
    --------------------------------
    A sold unit did nothing to the shop's stock number. Nothing at all. ``stock_units`` was
    a figure only a merchant could change, and the only trace of a sale was the reservation
    row that had held the units. So the platform had two half-answers to "how many are
    left" -- what the merchant declared, which never fell, and what had been taken, which
    only ever rose -- and it subtracted one from the other at check time. That never
    oversold, so no money was ever wrong; it simply never settled. The two numbers drifted
    apart for as long as the shop ran, and a demo store eventually refused every checkout
    because its sales had outgrown a figure that had not moved since the day it was seeded.

    So a sale is a movement now, and so is a delivery from a supplier, and so is a
    correction. ``units`` is signed and the sum of every row for a SKU is what the shop has:
    a stock level is a balance, not an opinion, and it is derived from the things that
    happened rather than maintained beside them.

    ``kind`` and the sign of ``units`` are constrained together. A ``SOLD`` row that added
    stock, or a ``RECEIVED`` row that removed it, would be a rounding error nobody could
    find afterwards -- the balance would be right for the wrong reasons and the ledger
    would stop explaining itself. ``ADJUSTED`` is the one kind that may go either way, and
    that is what makes it the kind a person has to sign for.

    Append-only in practice rather than by membership of ``APPEND_ONLY_TABLES``: that set
    is kernel-and-worker-only, and a shop's opening balance is written by the same act that
    creates the shop, which is the app role's. No role holds UPDATE or DELETE here, which is
    what actually makes a movement permanent.

    Provenance is per kind and each is nullable, because each names a different cause. A
    ``SOLD`` row names the checkout version that consumed the hold; a movement a merchant
    asked for names the action they approved. A row with neither is a seed, and the reason
    key says so.
    """

    __tablename__ = "inventory_movements"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('RECEIVED','SOLD','RETURNED','ADJUSTED')",
            name="inventory_movement_kind_enum",
        ),
        # Zero is not a movement. A row that moves nothing still advances a ledger somebody
        # will read, and the honest way to record "nothing happened" is not to record it.
        CheckConstraint("units <> 0", name="inventory_movement_is_a_movement"),
        # The sign belongs to the kind. Stated here rather than in the writer, because the
        # writer is today's code and the constraint is what survives it being replaced.
        CheckConstraint(
            "(kind = 'RECEIVED' AND units > 0) OR (kind = 'RETURNED' AND units > 0) "
            "OR (kind = 'SOLD' AND units < 0) OR kind = 'ADJUSTED'",
            name="inventory_movement_sign_matches_kind",
        ),
        CheckConstraint(
            "checkout_version IS NULL OR checkout_version >= 1",
            name="inventory_movement_version_positive",
        ),
        # One sale moves a SKU once. The unique index below is what stops a retried
        # admission, a replayed outbox row or a second executor from selling the same units
        # twice: the second insert is refused by the database rather than by whoever
        # remembered to check first.
        Index(
            "uq_inventory_movements_sale",
            "tenant_id",
            "checkout_id",
            "checkout_version",
            "sku",
            unique=True,
            postgresql_where=text("kind = 'SOLD'"),
        ),
        # The balance read: every movement for one SKU in one shop, which is the whole
        # question this table answers.
        Index("ix_inventory_movements_sku", "tenant_id", "merchant_id", "sku"),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    merchant_id: Mapped[uuid.UUID] = _merchant_fk()
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Signed. Positive adds to the shelf, negative takes from it, and the sum is the shelf.
    units: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: A stable key, not prose: ``sale_admitted``, ``shop_opened``, ``merchant_receipt``.
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    checkout_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    checkout_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    merchant_action_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchant_actions.id"), nullable=True
    )
    occurred_at: Mapped[datetime] = _now()


class MerchantAction(Base):
    """One change a merchant proposed to their own shop, and who agreed to it.

    Not a financial table, and the kernel writes nothing here that decides anything. A
    merchant action changes a price, a stock level, a listing or an offer -- what buyers
    are shown -- and none of that moves money. A merchant-initiated financial remedy
    crosses a narrow financial boundary and goes through the kernel's admission, which is
    a different request rather than a wider version of this one.

    ``content_hash`` is the row's whole point. It is the digest of the canonical document
    in :mod:`merchant_controller.actions`, and an approval names it rather than naming this
    row. An edit produces a different digest, so an approved action that was then edited
    stops matching by construction and the executor refuses it -- rather than by anybody
    remembering to revoke the approval.

    ``proposed_by`` and ``approved_by`` are separate columns and never the same value when
    a model drafted the change. A model stays an AGENT principal for its whole life and is
    never written down as the person who agreed to what it proposed. That is the fact this
    table exists to keep.
    """

    __tablename__ = "merchant_actions"
    __table_args__ = (
        CheckConstraint(
            "state IN ('DRAFT','AWAITING_APPROVAL','APPROVED','QUEUED','EXECUTING',"
            "'SUCCEEDED','FAILED','UNKNOWN','REJECTED','EXPIRED','CANCELLED','STALE')",
            name="merchant_action_state_enum",
        ),
        # An approved action names who approved it. Enforced here because "we always set it
        # together" is a property of today's code rather than of the table, and the column
        # is the answer to "who agreed to this" long after that code has changed.
        CheckConstraint(
            "(state = 'DRAFT' OR state = 'AWAITING_APPROVAL' OR state = 'REJECTED' "
            "OR state = 'CANCELLED' OR state = 'EXPIRED') OR approved_by IS NOT NULL",
            name="executed_action_names_its_approver",
        ),
        # The merchant's own queue, newest first: unlike the support helpdesk this is a
        # worklist the merchant scans, not a backlog somebody is owed an answer on.
        Index("ix_merchant_actions_tenant_merchant", "tenant_id", "merchant_id", "created_at"),
        Index("ix_merchant_actions_tenant_state", "tenant_id", "state", "created_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    merchant_id: Mapped[uuid.UUID] = _merchant_fk()
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    #: What is being changed -- a SKU, an offer id, a policy family -- as one string.
    #: One thing per action, because a list invites a batch nobody reviewed line by line.
    target: Mapped[str] = mapped_column(String(128), nullable=False)
    #: The typed body, exactly as hashed. Integers, strings, booleans and flat lists of
    #: those; the shape is validated by the Controller before it reaches here.
    proposal: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    #: The catalogue revision this was approved against. An action whose world has moved
    #: on is stale, and this is what makes that detectable rather than assumed.
    expected_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'DRAFT'"))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proposed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: Why it was rejected, or how it failed. Free text for a person, never parsed.
    #: What the change actually moved, as ``[{"field", "before", "after"}]``.
    #:
    #: Written at execution and null until then: a draft has changed nothing, so it has
    #: nothing to have changed from, and filling this at proposal time would record a guess
    #: about a shop that can move before the approval lands. Null forever on an action that
    #: never succeeded.
    applied: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    outcome_note: Mapped[str] = mapped_column(
        String(1000), nullable=False, server_default=text("''")
    )
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MerchantPolicyVersion(Base):
    """One published version of a merchant's non-financial terms, and it never changes.

    What a shop promises about cancelling, refunding, substituting and fulfilling was a
    constant in the simulator until this table existed. The Policy-at-Sale Receipt has
    always frozen those terms onto each order so that a later change cannot narrow them
    retroactively -- but with nothing able to change them, the guarantee was aimed at an
    event that could not happen. This is the table that lets it happen, so the guarantee
    can be attacked rather than only asserted.

    Immutable by construction and by grant. A version is inserted and never updated: the
    app role holds INSERT and no UPDATE, so narrowing a term means publishing a new version
    beside the old one, and an order pointing at version three still reads version three.
    That is the whole mechanism. A mutable row would make every receipt that names a
    version a receipt that names whatever the row says today.

    Self-contained on purpose. Each row carries the complete set of families, not a diff
    against its predecessor, so reading the terms an order was sold under is one row and
    never a replay. A diff would be smaller and would make the oldest receipt the hardest
    to verify.

    The current version is the highest one for the merchant. There is deliberately no
    "current" pointer column: a pointer is a second source of truth that can disagree with
    the rows, and the only thing it would buy is publishing a version without making it
    effective -- which is scheduling, and there is no scheduler.

    Financial terms are absent. Delivery charges come from the fee policy and discounts
    from the running promotion, both of which already change and already reach the receipt.
    Mixing them in here would give two writers one field.
    """

    __tablename__ = "merchant_policy_versions"
    __table_args__ = (
        # Two publications racing produce two versions or one failure, never one version
        # with two meanings.
        UniqueConstraint("tenant_id", "merchant_id", "version", name="one_version_per_merchant"),
        CheckConstraint("version >= 1", name="version_starts_at_one"),
        Index("ix_merchant_policy_versions_current", "tenant_id", "merchant_id", "version"),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    merchant_id: Mapped[uuid.UUID] = _merchant_fk()
    #: Monotonic per merchant, starting at one. Named in every receipt issued under it.
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The complete term set: one entry per non-financial policy family, each a flat
    #: mapping of the kind a person can read before agreeing to it.
    terms: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    #: The merchant action this came from, so a term can be traced back to who approved it.
    #: Nullable because the first version of every shop is seeded rather than proposed.
    action_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    published_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = _now()


#: Every service table, in FK-safe creation order.
class ReservePasskey(Base):
    __tablename__ = "reserve_passkeys"
    __table_args__ = (
        UniqueConstraint("tenant_id", "merchant_id", "buyer_ref", name="uq_reserve_passkey_owner"),
        UniqueConstraint("rp_id", "credential_id", name="uq_reserve_passkey_credential"),
    )
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    merchant_id: Mapped[uuid.UUID] = _merchant_fk()
    buyer_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    rp_id: Mapped[str] = mapped_column(String(253), nullable=False)
    credential_id: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    public_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    user_handle: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sign_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = _now()


class ReserveConsentChallenge(Base):
    __tablename__ = "reserve_consent_challenges"
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    merchant_id: Mapped[uuid.UUID] = _merchant_fk()
    buyer_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    challenge: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    terms: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    terms_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    rp_id: Mapped[str] = mapped_column(String(253), nullable=False)
    origin: Mapped[str] = mapped_column(String(512), nullable=False)
    user_handle: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    consent_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _now()


SERVICE_TABLES: Final[tuple[str, ...]] = (
    "api_sessions",
    "carts",
    "checkouts",
    "webhook_inbox",
    "orders",
    "provider_requests",
    "reconciliation_runs",
    "scenario_faults",
    "scenario_runs",
    "support_cases",
    "merchant_actions",
    "merchant_policy_versions",
    "merchant_state",
    "merchant_sku_state",
    "inventory_movements",
    "reserve_passkeys",
    "reserve_consent_challenges",
)

#: The tenant-owned subset that receives row-level security. ``api_sessions`` is excluded
#: on purpose (see :class:`ApiSession`). :data:`platform_db.schema.RLS_TABLES` lists the
#: same names; a test asserts the two never drift.
SERVICE_RLS_TABLES: Final[tuple[str, ...]] = tuple(t for t in SERVICE_TABLES if t != "api_sessions")
