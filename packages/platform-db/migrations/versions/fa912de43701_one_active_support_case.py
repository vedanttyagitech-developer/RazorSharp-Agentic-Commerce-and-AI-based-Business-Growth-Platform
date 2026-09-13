"""One active support case per buyer/order; retain duplicate history."""
from alembic import op
import sqlalchemy as sa

revision = "fa912de43701"
down_revision = "e8b421cc901a"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("LOCK TABLE support_cases IN SHARE ROW EXCLUSIVE MODE")
    # Keep the oldest active case. Preserve every duplicate and its original notes;
    # close it with an explicit reference to the surviving case, never delete history.
    op.execute("""
        WITH ranked AS (
            SELECT id, first_value(id) OVER (
                PARTITION BY tenant_id, order_id, buyer_ref ORDER BY created_at, id
            ) AS retained_id,
            row_number() OVER (
                PARTITION BY tenant_id, order_id, buyer_ref ORDER BY created_at, id
            ) AS n
            FROM support_cases WHERE status IN ('OPEN', 'ACKNOWLEDGED')
        )
        UPDATE support_cases AS c SET status='CLOSED', updated_at=now(),
            resolution_note=concat_ws(E'\\n', nullif(c.resolution_note, ''),
                'Migration: duplicate active case closed; continue case ' || r.retained_id::text)
        FROM ranked AS r WHERE c.id=r.id AND r.n > 1
    """)
    op.create_index("uq_support_active_buyer_order", "support_cases",
                    ["tenant_id", "order_id", "buyer_ref"], unique=True,
                    postgresql_where=sa.text("status IN ('OPEN','ACKNOWLEDGED')"))


def downgrade():
    op.drop_index("uq_support_active_buyer_order", table_name="support_cases")
    # Do not reopen superseded cases: subsequent support work may depend on their closure.
