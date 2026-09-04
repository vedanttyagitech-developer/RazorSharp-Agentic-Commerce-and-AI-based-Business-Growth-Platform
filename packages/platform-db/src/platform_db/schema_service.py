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
    """A pseudonymous buyer or agent session minted by ``POST /v1/demo/sessions``.

    Deliberately NOT under row-level security. The bearer token is looked up before any
    tenant is known, and the row is what tells the request which tenant it belongs to.
    Only the token's SHA-256 is stored, so a leaked table cannot be replayed.
    """

    __tablename__ = "api_sessions"
    __table_args__ = (
        CheckConstraint("actor_type IN ('BUYER','AGENT','OPERATOR')", name="actor_type_enum"),
        Index("ix_api_sessions_tenant_buyer", "tenant_id", "buyer_ref"),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    merchant_id: Mapped[uuid.UUID] = _merchant_fk()
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    buyer_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ------------------------------------------------------------------- baskets + checkouts


class Basket(Base):
    """Pre-checkout intent. Lines and the last quote are JSON because a basket has no
    invariant of its own: the deterministic quote is recomputed on every read and the
    binding copy lives in ``checkout_versions.content`` once checkout starts."""

    __tablename__ = "baskets"
    __table_args__ = (
        CheckConstraint("status IN ('OPEN','CHECKED_OUT','ABANDONED')", name="status_enum"),
        Index("ix_baskets_tenant_buyer", "tenant_id", "buyer_ref"),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    merchant_id: Mapped[uuid.UUID] = _merchant_fk()
    buyer_ref: Mapped[str] = mapped_column(String(128), nullable=False)
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
        UniqueConstraint("tenant_id", "basket_id"),
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
    basket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("baskets.id"), nullable=False
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


#: Every service table, in FK-safe creation order.
SERVICE_TABLES: Final[tuple[str, ...]] = (
    "api_sessions",
    "baskets",
    "checkouts",
    "webhook_inbox",
    "orders",
    "provider_requests",
    "reconciliation_runs",
    "scenario_faults",
    "scenario_runs",
)

#: The tenant-owned subset that receives row-level security. ``api_sessions`` is excluded
#: on purpose (see :class:`ApiSession`). :data:`platform_db.schema.RLS_TABLES` lists the
#: same names; a test asserts the two never drift.
SERVICE_RLS_TABLES: Final[tuple[str, ...]] = tuple(t for t in SERVICE_TABLES if t != "api_sessions")
