"""One live Reserve permission per buyer and merchant; preserve existing evidence."""
from alembic import op
import sqlalchemy as sa

revision = "e8b421cc901a"
down_revision = "d7f301a592e8"
branch_labels = None
depends_on = None


def upgrade():
    # Stop concurrent creation while checking historical data and installing the index.
    op.execute("LOCK TABLE delegated_authorities IN SHARE ROW EXCLUSIVE MODE")
    duplicates = op.get_bind().execute(sa.text("""
        SELECT tenant_id, merchant_id, buyer_ref, count(*) AS n
        FROM delegated_authorities
        WHERE kind='RESERVE' AND status IN ('ACTIVE','EXHAUSTED','RECONCILING')
        GROUP BY tenant_id, merchant_id, buyer_ref HAVING count(*) > 1
    """)).fetchall()
    if duplicates:
        raise RuntimeError(
            f"{len(duplicates)} buyer/merchant scopes have multiple live Reserve permissions. "
            "Inspect and explicitly revoke superseded permissions through the audited kernel "
            "before retrying this migration; no permissions were deleted or rewritten."
        )
    op.create_index(
        "uq_reserve_live_buyer_merchant", "delegated_authorities",
        ["tenant_id", "merchant_id", "buyer_ref"], unique=True,
        postgresql_where=sa.text("kind='RESERVE' AND status IN ('ACTIVE','EXHAUSTED','RECONCILING')"),
    )


def downgrade():
    op.drop_index("uq_reserve_live_buyer_merchant", table_name="delegated_authorities")
