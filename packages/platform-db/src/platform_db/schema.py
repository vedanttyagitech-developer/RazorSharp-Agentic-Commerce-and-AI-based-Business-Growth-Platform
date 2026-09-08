"""Transaction-path schema.

Scope: the tables the admission transaction locks, reads or writes. Catalogue, agent,
protocol and metric tables arrive with the services that own them.

Two rules hold everywhere in this module:

1. Every tenant-owned table carries a non-null ``tenant_id`` and every uniqueness
   constraint includes it. A uniqueness constraint without ``tenant_id`` is a
   cross-tenant collision waiting to happen.
2. Money is ``BigInteger`` minor units beside an ISO 4217 code. There is no NUMERIC
   money column and no FLOAT anywhere in this schema.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Explicit naming convention so Alembic autogenerate produces stable, reviewable names.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True)


def _tenant_fk() -> Mapped[uuid.UUID]:
    """Non-null tenant on every tenant-owned row. RLS depends on this column existing."""
    return mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )


def _now() -> Mapped[datetime]:
    """Server-side clock. Reservation and approval expiry are compared against the
    database clock, never an application clock, so that a skewed pod cannot admit an
    expired reservation."""
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


# --------------------------------------------------------------------------- tenancy


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = _pk()
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    home_region: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = _now()


class Merchant(Base):
    __tablename__ = "merchants"
    __table_args__ = (UniqueConstraint("tenant_id", "slug"),)

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    created_at: Mapped[datetime] = _now()


class OperatingMode(Base):
    """Safe Mode, specification 10.3.2.

    A null ``tenant_id`` is the global mode. Rows are append-only history; the current
    mode is the newest row, so a mode change can never silently lose its predecessor.
    """

    __tablename__ = "platform_operating_modes"
    __table_args__ = (
        CheckConstraint("mode IN ('NORMAL','SAFE_MODE')", name="mode_enum"),
        Index("ix_operating_modes_lookup", "tenant_id", "changed_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    # Nullable by design: the global switch has no tenant. RLS is not applied here;
    # the mode is read by the kernel under its own role.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=True
    )
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    changed_at: Mapped[datetime] = _now()


# ------------------------------------------------------------------- checkout + policy


class PolicyAtSaleReceipt(Base):
    """Specification 10.2.1. Immutable snapshot of the rules governing one sale."""

    __tablename__ = "policy_at_sale_receipts"
    __table_args__ = (UniqueConstraint("tenant_id", "checkout_id", "checkout_version"),)

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchants.id"), nullable=False
    )
    checkout_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    checkout_version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    receipt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = _now()


class CheckoutVersion(Base):
    """One immutable version of a checkout.

    ``content_hash`` is the canonical hash from ``commerce_domain.canonical_hash``.
    Version N is never revived: a material change invalidates it and creates N+1.
    """

    __tablename__ = "checkout_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "checkout_id", "version"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("total_minor >= 0", name="total_non_negative"),
        CheckConstraint(
            "status IN ('DRAFT','QUOTED','RESERVED','APPROVAL_REQUIRED','APPROVED',"
            "'EXECUTION_PENDING','AWAITING_PAYMENT','PAID','PAYMENT_FAILED',"
            "'PAYMENT_UNKNOWN','INVALIDATED','INVALIDATED_AWAITING_PAYMENT_RESULT',"
            "'CANCELLED','EXPIRED')",
            name="status_enum",
        ),
        Index("ix_checkout_versions_current", "tenant_id", "checkout_id", "version"),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchants.id"), nullable=False
    )
    checkout_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    total_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Mechanical binding, specification 10.2.1: a checkout version cannot be recombined
    # with a different policy receipt without breaking a verified binding.
    policy_receipt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("policy_at_sale_receipts.id"), nullable=True
    )
    policy_receipt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[str] = mapped_column(String(48), nullable=False)
    immutable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = _now()
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE','CONSUMED','RELEASED','EXPIRED')", name="status_enum"),
        Index("ix_reservations_checkout", "tenant_id", "checkout_id", "checkout_version"),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    checkout_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    checkout_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # Compared against the database clock at admission, never an application clock.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _now()


# ----------------------------------------------------------------- authority + approval


class DelegatedAuthority(Base):
    """Buyer authority with a monotonic revocation epoch, specification 10.2.

    Revocation increments the epoch under a row lock; admission locks the same row.
    That shared lock is the single linearization point between the two.
    """

    __tablename__ = "delegated_authorities"
    __table_args__ = (
        CheckConstraint("revocation_epoch >= 0", name="epoch_non_negative"),
        CheckConstraint("max_amount_minor >= 0", name="max_amount_non_negative"),
        CheckConstraint("consumed_amount_minor >= 0", name="consumed_non_negative"),
        CheckConstraint("consumed_amount_minor <= max_amount_minor", name="consumed_within_max"),
        CheckConstraint("kind IN ('SINGLE_USE','RESERVE')", name="kind_enum"),
        CheckConstraint(
            "status IN ('ACTIVE','EXHAUSTED','EXPIRED','REVOKED','RECONCILING')",
            name="status_enum",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchants.id"), nullable=False
    )
    buyer_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    revocation_epoch: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    max_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    consumed_amount_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _now()


class Approval(Base):
    """A recorded buyer decision, bound to exactly one immutable checkout version."""

    __tablename__ = "approvals"
    __table_args__ = (
        # One RECORDED approval per version. A full unique constraint including status would
        # also forbid a second EXPIRED or INVALIDATED row, which reject-then-reapprove needs.
        # Migration c6ffa021cbb0 replaced the old constraint with this partial index.
        Index(
            "uq_approvals_one_recorded_per_version",
            "tenant_id",
            "checkout_id",
            "checkout_version",
            unique=True,
            postgresql_where=text("status = 'RECORDED'"),
        ),
        CheckConstraint("amount_minor >= 0", name="amount_non_negative"),
        CheckConstraint(
            "status IN ('RECORDED','CONSUMED','INVALIDATED','EXPIRED')", name="status_enum"
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    checkout_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    checkout_version: Mapped[int] = mapped_column(Integer, nullable=False)

    # The exact bytes the buyer saw, hashed. Admission compares against this.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_receipt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)

    authority_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("delegated_authorities.id"), nullable=True
    )
    # The epoch observed when the approval was recorded. Admission requires equality.
    authority_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False)
    issued_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ------------------------------------------------------------------ payment + execution


class PaymentAttempt(Base):
    __tablename__ = "payment_attempts"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        # Must stay exactly equal to transaction_kernel.states.PaymentState. A state the
        # enum can produce but the constraint rejects fails at COMMIT -- inside the
        # admission transaction, after the locks are taken -- which is the worst possible
        # place to discover it. test_schema_state_agreement asserts equality in both
        # directions so neither side can drift.
        CheckConstraint(
            "status IN ('CREATED','SUBMITTED','AUTHORIZED','CAPTURED','FAILED','EXPIRED',"
            "'UNKNOWN','RECONCILING','ESCALATED','STALE_CAPTURE',"
            "'AUTO_REFUND_PENDING','REFUND_PENDING','PARTIALLY_REFUNDED','REFUNDED',"
            "'REFUND_UNKNOWN','REFUND_FAILED')",
            name="status_enum",
        ),
        UniqueConstraint("tenant_id", "receipt", name="receipt_unique_per_tenant"),
        # One Razorpay order per attempt, and a fast path from a provider order id back to
        # the attempt for webhook and client-return handling (migration c6ffa021cbb0).
        Index(
            "uq_payment_attempts_provider_order",
            "tenant_id",
            "provider_order_id",
            unique=True,
            postgresql_where=text("provider_order_id IS NOT NULL"),
        ),
        Index("ix_payment_attempts_provider_payment", "tenant_id", "provider_payment_id"),
        # Specification 10.6: at most one non-terminal attempt per checkout. This partial
        # unique index is what makes "single winner" a database guarantee rather than a
        # hopeful code path.
        Index(
            "uq_payment_attempts_one_non_terminal",
            "tenant_id",
            "checkout_id",
            unique=True,
            postgresql_where=text(
                "status IN ('CREATED','SUBMITTED','AUTHORIZED','UNKNOWN','RECONCILING')"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    checkout_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    checkout_version: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[str] = mapped_column(String(24), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    # Stable merchant-scoped business identifier; also the provider lookup key after a
    # lost create response, specification 10.6.
    receipt: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Refund(Base):
    """One refund operation against a captured payment, specification 25.3.

    Separate from ``payment_attempts`` because a single capture can be refunded several
    times: two partial refunds for two unavailable items are two provider operations with
    two outcomes, and collapsing them into the payment's status would lose the fact that
    one succeeded and the other is still unknown.

    The payment attempt keeps a *posture* (PARTIALLY_REFUNDED, REFUNDED); each individual
    operation lives here with its own status, idempotency key and Execution Grant.
    """

    __tablename__ = "refunds"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        CheckConstraint(
            "status IN ('PENDING','PROCESSED','FAILED','UNKNOWN','RECONCILING','ESCALATED')",
            name="status_enum",
        ),
        # A stable per-tenant idempotency key. A retried refund after a lost response must
        # find the original rather than create a second, which is how a buyer gets paid
        # back twice.
        UniqueConstraint("tenant_id", "idem_key", name="refund_idem_unique_per_tenant"),
        # Same keyset walk as orders, for the refund collection.
        Index("ix_refunds_tenant_created", "tenant_id", "created_at", "id"),
        # The refunds for one order, without going through the attempt.
        Index("ix_refunds_tenant_order", "tenant_id", "order_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    payment_attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_attempts.id"), nullable=False
    )
    #: The order this money is going back for.
    #:
    #: Reachable through the attempt as well, and held directly because the order id is the
    #: identifier this system is built around: an operator with an order number should get
    #: its refunds without knowing that an attempt sits between them.
    #:
    #: Nullable for one case the platform already has: a stale capture. Money captured
    #: against a version invalidated while the payment was in flight produces no order --
    #: correctly, because nothing was sold -- and the automatic full refund of it has no
    #: order to name. Empty here is therefore a real signal, "returned for something that
    #: was never an order", rather than a gap; every ordinary refund fills it.
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id"), nullable=True
    )
    checkout_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    idem_key: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_refund_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # True when Razorpay originated it (dashboard refund, capture-window auto-refund)
    # rather than this platform. Without it the local ledger drifts from the provider.
    provider_originated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ExecutionGrant(Base):
    """Single-use authority to perform exactly one provider operation, specification 10.3.1."""

    __tablename__ = "execution_grants"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        CheckConstraint("status IN ('ISSUED','CONSUMED','EXPIRED','REVOKED')", name="status_enum"),
        CheckConstraint(
            "operation IN ('PAYMENT_CREATE_ORDER','REFUND_EXECUTE','RESERVE_DEBIT')",
            name="operation_enum",
        ),
        # A grant is single-use: one CONSUMED grant per payment attempt, enforced here
        # rather than trusted to the worker.
        Index(
            "uq_execution_grants_one_active_per_attempt",
            "tenant_id",
            "payment_attempt_id",
            unique=True,
            postgresql_where=text("status = 'ISSUED'"),
        ),
        # Refund grants are looked up by the refund they authorize (ADR 0003 D10); the
        # payment path leaves refund_id NULL and never hits this index.
        Index("ix_execution_grants_tenant_refund", "tenant_id", "refund_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    checkout_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    checkout_version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    payment_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_attempts.id"), nullable=True
    )
    # ADR 0003 D10: a REFUND_EXECUTE grant is bound to exactly one refunds row, so a second
    # partial refund on the same attempt is a different binding and not a forbidden
    # replacement of the first. NULL for every payment grant. Migration
    # 7d2a4b9e1f03_grant_refund_binding.
    refund_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("refunds.id"), nullable=True
    )

    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    kernel_decision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    outbox_command_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False)
    issued_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IdempotencyRecord(Base):
    """Specification 10.6: a duplicate valid request returns the original result;
    the same key with a different payload is rejected rather than silently re-run."""

    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("tenant_id", "idem_key"),)

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    idem_key: Mapped[str] = mapped_column(String(128), nullable=False)
    operation: Mapped[str] = mapped_column(String(48), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = _now()


# --------------------------------------------------------------------- evidence + work


class AuditEvent(Base):
    """Append-only, hash-chained per aggregate stream, specification 26.2.

    ``seq`` is per (tenant, aggregate) so a gap or reordering is detectable, and
    ``prev_hash`` chains each event to its predecessor so a deleted row breaks the chain.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "aggregate_type", "aggregate_id", "seq"),
        CheckConstraint("seq >= 1", name="seq_positive"),
        Index("ix_audit_events_correlation", "tenant_id", "correlation_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    aggregate_type: Mapped[str] = mapped_column(String(48), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)

    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    principal_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    self_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    occurred_at: Mapped[datetime] = _now()


class OutboxEvent(Base):
    """Committed in the same transaction as the state change it describes.

    Pub/Sub and Cloud Tasks may wake a worker, but this table is what makes the command
    recoverable, specification 23.2.
    """

    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','LEASED','DONE','FAILED','DEAD')", name="status_enum"
        ),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        Index("ix_outbox_ready", "status", "available_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    command_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'PENDING'")
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = _now()


#: Tables that carry a tenant column and therefore receive row-level security.
#: ``platform_operating_modes`` is excluded deliberately: its tenant is nullable because
#: the global switch has no tenant, and only the kernel role reads it. ``api_sessions``
#: is excluded because resolving a bearer token is how a request learns its tenant.
#:
#: The service tables are listed by name rather than imported from ``schema_service``
#: because that module imports ``Base`` from here; a name list has no import cycle and
#: ``schema_service.SERVICE_RLS_TABLES`` is asserted equal to this tail by a test.
RLS_TABLES: tuple[str, ...] = (
    "merchants",
    "policy_at_sale_receipts",
    "checkout_versions",
    "reservations",
    "delegated_authorities",
    "approvals",
    "payment_attempts",
    "refunds",
    "execution_grants",
    "idempotency_records",
    "audit_events",
    "outbox_events",
    # service layer (platform_db.schema_service)
    "carts",
    "checkouts",
    "webhook_inbox",
    "orders",
    "provider_requests",
    "reconciliation_runs",
    "scenario_faults",
    "scenario_runs",
    "support_cases",
)
