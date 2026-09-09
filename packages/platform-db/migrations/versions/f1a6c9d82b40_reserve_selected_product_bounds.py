"""Persist per-purchase and selected-product authority bounds.

Legacy authorities retain their existing semantics through nullable bounds.
New Reserve Pay setup must always supply both bounds. No historical audit event changes.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f1a6c9d82b40"
down_revision = "c4e17b90d3a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "delegated_authorities",
        sa.Column("per_purchase_limit_minor", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "delegated_authorities", sa.Column("allowed_skus", postgresql.JSONB(), nullable=True)
    )
    op.create_check_constraint(
        "purchase_limit_within_capacity",
        "delegated_authorities",
        "per_purchase_limit_minor IS NULL OR (per_purchase_limit_minor > 0 "
        "AND per_purchase_limit_minor <= max_amount_minor)",
    )
    op.create_check_constraint(
        "selected_products_nonempty",
        "delegated_authorities",
        "allowed_skus IS NULL OR (jsonb_typeof(allowed_skus) = 'array' "
        "AND jsonb_array_length(allowed_skus) BETWEEN 1 AND 100)",
    )


def downgrade() -> None:
    op.drop_constraint("selected_products_nonempty", "delegated_authorities", type_="check")
    op.drop_constraint("purchase_limit_within_capacity", "delegated_authorities", type_="check")
    op.drop_column("delegated_authorities", "allowed_skus")
    op.drop_column("delegated_authorities", "per_purchase_limit_minor")
