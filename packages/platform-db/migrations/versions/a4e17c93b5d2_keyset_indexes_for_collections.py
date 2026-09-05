"""Keyset indexes for the order and refund collections.

``GET /v1/orders`` and ``GET /v1/refunds`` page by keyset on ``(created_at, id)``
descending within a tenant. Without a composite index PostgreSQL sorts the tenant's
whole history to return the first page; with one it walks the index backwards from the
cursor tuple. The tenant column leads because every query is tenant-scoped first.

Revision ID: a4e17c93b5d2
Revises: 7d2a4b9e1f03
"""

from __future__ import annotations

from alembic import op

revision = "a4e17c93b5d2"
down_revision = "7d2a4b9e1f03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_orders_tenant_created", "orders", ["tenant_id", "created_at", "id"])
    op.create_index("ix_refunds_tenant_created", "refunds", ["tenant_id", "created_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_refunds_tenant_created", table_name="refunds")
    op.drop_index("ix_orders_tenant_created", table_name="orders")
