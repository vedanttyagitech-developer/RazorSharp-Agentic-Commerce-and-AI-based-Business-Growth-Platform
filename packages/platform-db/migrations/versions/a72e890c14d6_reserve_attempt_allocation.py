"""Bind Reserve Pay allocation and simulator outcome to the payment attempt."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a72e890c14d6"
down_revision = "f1a6c9d82b40"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("payment_attempts", sa.Column("reserve_authority_id", postgresql.UUID()))
    op.add_column("payment_attempts", sa.Column("reserve_authority_epoch", sa.BigInteger()))
    op.add_column("payment_attempts", sa.Column("reserve_allocation", sa.String(16)))
    op.add_column("payment_attempts", sa.Column("reserve_simulation_outcome", sa.String(16)))
    op.create_check_constraint(
        "reserve_allocation_valid",
        "payment_attempts",
        "reserve_allocation IS NULL OR reserve_allocation IN ('HELD','SPENT','RELEASED')",
    )
    op.create_check_constraint(
        "reserve_simulation_valid",
        "payment_attempts",
        "reserve_simulation_outcome IS NULL OR "
        "reserve_simulation_outcome IN ('captured','failed','unknown')",
    )


def downgrade():
    op.drop_constraint("reserve_simulation_valid", "payment_attempts", type_="check")
    op.drop_constraint("reserve_allocation_valid", "payment_attempts", type_="check")
    for name in (
        "reserve_simulation_outcome",
        "reserve_allocation",
        "reserve_authority_epoch",
        "reserve_authority_id",
    ):
        op.drop_column("payment_attempts", name)
