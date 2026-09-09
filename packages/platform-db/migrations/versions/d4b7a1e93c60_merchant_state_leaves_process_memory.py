"""The shop remembers its own prices and stock across a restart.

Until now the merchant simulator's live state -- every price, every stock level, the fee
policy, the running offer and the catalogue revision -- lived in the API process (ADR 0003
D14). Two costs were paid for that every day, and neither was visible from inside a single
run:

* **The shop forgot itself.** Every restart and every deploy reseeded stock and prices from
  the fixture. On Kubernetes that is a `Recreate` rollout, so a redeploy silently returned
  the store to its opening-day numbers in the middle of a demonstration.
* **There could only ever be one API process.** A second would hold a second, disagreeing
  copy, so ``WEB_CONCURRENCY > 1`` was refused and the Deployment was pinned to one replica.

Two tables, split the way the data splits. ``merchant_state`` is per shop and holds the
things there is exactly one of: the revision counter, the fee policy, the one running
offer. ``merchant_sku_state`` is per product and holds the three numbers a merchant
actually moves: price, stock and whether it is listed.

The catalogue itself does not move here. Names, units, tax rates and images are a fixture
and stay one; only the part a merchant changes becomes a row.

Both tables are tenant-owned and take row-level security, and neither is financial: what a
shop charges is not money that moved. The grants say so -- the app role may insert a shop's
opening state because provisioning is its job, and only the kernel may change it, because a
price change and the audit event that explains it commit together.

Empty on creation. Seeding is the application's, per shop, from the catalogue fixture that
the previous design read at construction time; a migration that reached into
``merchant_sim`` for it would make this package depend on the simulator it stores.

Reversible: ``downgrade`` drops both tables. Nothing references them by foreign key -- a SKU
is a string here, as it is in ``merchant_actions``, because the catalogue is a fixture and
not a table.

Revision ID: d4b7a1e93c60
Revises: a72e890c14d6
"""

from __future__ import annotations

from alembic import op
from platform_db.rls import table_statements
from platform_db.schema_service import MerchantSkuState, MerchantState

revision = "d4b7a1e93c60"
down_revision = "a72e890c14d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    MerchantState.__table__.create(bind=op.get_bind())
    MerchantSkuState.__table__.create(bind=op.get_bind())
    # Replayed from the generator rather than written out here, so the privileges a
    # deployed database holds and the privileges the code declares cannot drift.
    for table in ("merchant_state", "merchant_sku_state"):
        for statement in table_statements(table):
            op.execute(statement)


def downgrade() -> None:
    MerchantSkuState.__table__.drop(bind=op.get_bind())
    MerchantState.__table__.drop(bind=op.get_bind())
