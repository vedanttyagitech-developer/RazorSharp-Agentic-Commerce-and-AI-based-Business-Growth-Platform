"""support_cases: a buyer asked for help, and a person will answer

Revision ID: d5b71e0a92c4
Revises: e4c29a71db3f
Create Date: 2026-09-08

This is where a refund request goes, and it is deliberately not where a refund goes. There
is no amount column and no currency column, because nothing on this path is entitled to
compute one: the support agent may open a case and may repeat what the Policy-at-Sale
Receipt promised, and it may do nothing else. What the buyer is owed is settled by a person
on the merchant's side, reading this row.

Not a financial table. It moves no money, so the kernel is granted nothing on it; the
buyer's own session inserts, and the merchant's side will update. Row-level security and
grants are applied in this same revision so no window exists where a tenant-owned table is
queryable without a policy.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from platform_db.rls import table_statements
from sqlalchemy.dialects.postgresql import UUID

revision = "d5b71e0a92c4"
down_revision = "e4c29a71db3f"
branch_labels = None
depends_on = None

TABLE = "support_cases"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "merchant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("merchants.id"),
            nullable=False,
        ),
        sa.Column("order_id", UUID(as_uuid=True), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("buyer_ref", sa.String(128), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("note", sa.String(1000), nullable=False, server_default=sa.text("''")),
        sa.Column("opened_by", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'OPEN'")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('OPEN','ACKNOWLEDGED','RESOLVED','CLOSED')", name="support_status_enum"
        ),
    )
    op.create_index(
        "ix_support_cases_tenant_merchant", TABLE, ["tenant_id", "merchant_id", "created_at"]
    )
    op.create_index("ix_support_cases_tenant_order", TABLE, ["tenant_id", "order_id"])

    for statement in table_statements(TABLE):
        op.execute(statement)


def downgrade() -> None:
    op.drop_index("ix_support_cases_tenant_order", table_name=TABLE)
    op.drop_index("ix_support_cases_tenant_merchant", table_name=TABLE)
    op.drop_table(TABLE)
