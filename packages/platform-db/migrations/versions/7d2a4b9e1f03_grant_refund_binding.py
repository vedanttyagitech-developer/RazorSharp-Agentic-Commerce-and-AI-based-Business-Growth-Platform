"""grant refund binding

Revision ID: 7d2a4b9e1f03
Revises: c6ffa021cbb0

ADR 0003 D10: a REFUND_EXECUTE Execution Grant is bound to the refunds row it authorizes.
Until now the "no replacement after consumption" rule in transaction_kernel.grants keyed
on (payment_attempt, operation) alone, so once the first partial refund's grant was
consumed no second partial refund could ever be admitted on that attempt. With the refund
id on the row the rule keys on (operation, refund_id) for refunds, and consume_grant
compares the refund id like every other bound field, so a command for refund B can never
spend the grant issued for refund A.

Nullable: payment grants have no refund. A real foreign key rather than a bare UUID so a
grant can never name a refund that does not exist; the refunds row is therefore inserted
before its grant, and deleted after it.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "7d2a4b9e1f03"
down_revision = "c6ffa021cbb0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "execution_grants",
        sa.Column("refund_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_execution_grants_refund_id"),
        "execution_grants",
        "refunds",
        ["refund_id"],
        ["id"],
    )
    op.create_index(
        "ix_execution_grants_tenant_refund",
        "execution_grants",
        ["tenant_id", "refund_id"],
        unique=False,
    )
    # Privileges are per table, so the existing kernel-only INSERT/UPDATE on
    # execution_grants already covers the new column; nothing to grant.


def downgrade() -> None:
    op.drop_index("ix_execution_grants_tenant_refund", table_name="execution_grants")
    op.drop_constraint(
        op.f("fk_execution_grants_refund_id"), "execution_grants", type_="foreignkey"
    )
    op.drop_column("execution_grants", "refund_id")
