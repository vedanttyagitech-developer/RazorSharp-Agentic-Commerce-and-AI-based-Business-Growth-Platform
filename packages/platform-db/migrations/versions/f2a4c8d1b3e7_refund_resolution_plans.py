"""refund_resolution_plans: a remedy priced and written down before it is agreed to

Revision ID: f2a4c8d1b3e7
Revises: e4c29a71db3f
Create Date: 2026-09-08

One table, and the split inside it is the point. Everything down to ``status`` is what the
buyer was shown and is covered by ``plan_hash``; everything after it is what later
happened. That is what makes a confirmation unrepeatable: the status moves, and the hash it
moved under is still there to compare against.

There is deliberately no per-line or per-unit column. This platform has no paid-share
allocation, so a column for what one damaged unit is worth could only ever hold a figure
nothing derived. A plan says what this order is owed.

Row-level security and grants are applied in this same revision, so there is no window in
which a tenant-owned table is queryable without a policy. The table name is written
literally rather than read from ``FINANCIAL_TABLES``, so a later change to that tuple
cannot retroactively alter what this revision applied.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from platform_db.rls import table_statements
from sqlalchemy.dialects.postgresql import UUID

revision = "f2a4c8d1b3e7"
down_revision = "e4c29a71db3f"
branch_labels = None
depends_on = None

TABLE = "refund_resolution_plans"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
            index=True,
        ),
        # --- what the buyer was shown; covered by plan_hash, never updated ---------------
        sa.Column("buyer_ref", sa.String(128), nullable=False),
        sa.Column("order_id", UUID(as_uuid=True), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column(
            "payment_attempt_id",
            UUID(as_uuid=True),
            sa.ForeignKey("payment_attempts.id"),
            nullable=False,
        ),
        sa.Column("checkout_id", UUID(as_uuid=True), nullable=False),
        sa.Column("checkout_version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("policy_receipt_hash", sa.String(64), nullable=False),
        sa.Column("policy_id", sa.String(128), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("evaluator_version", sa.String(32), nullable=False),
        sa.Column("financial_fingerprint", sa.String(64), nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        # --- what happened afterwards; outside plan_hash, and the only part that moves ---
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refund_id", UUID(as_uuid=True), sa.ForeignKey("refunds.id"), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("amount_minor > 0", name="plan_amount_positive"),
        sa.CheckConstraint(
            "status IN ('ISSUED','CONFIRMED','CONSUMED','EXPIRED','SUPERSEDED')",
            name="plan_status_enum",
        ),
        sa.CheckConstraint(
            "(status = 'CONSUMED') = (refund_id IS NOT NULL)",
            name="plan_consumed_names_its_refund",
        ),
    )
    # One live plan per order. Two would let a buyer confirm the older and cheaper of two
    # answers to the same question, and there is no reading of that which is fair to both.
    op.create_index(
        "uq_refund_plans_one_live_per_order",
        TABLE,
        ["tenant_id", "order_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('ISSUED','CONFIRMED')"),
    )
    op.create_index("ix_refund_plans_tenant_created", TABLE, ["tenant_id", "created_at", "id"])

    for statement in table_statements(TABLE):
        op.execute(statement)


def downgrade() -> None:
    op.drop_index("ix_refund_plans_tenant_created", table_name=TABLE)
    op.drop_index("uq_refund_plans_one_live_per_order", table_name=TABLE)
    op.drop_table(TABLE)
