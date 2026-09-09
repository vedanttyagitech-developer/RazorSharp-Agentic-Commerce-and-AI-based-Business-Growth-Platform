"""Every unit that moved gets a row, and the stock number becomes their sum.

A sold unit did nothing to the shop's stock number. Nothing at all: ``stock_units`` was a
figure only a merchant could change, and the only trace of a sale was the reservation row
that had held the units. The platform therefore held two half-answers to "how many are
left" -- what the merchant declared, which never fell, and what had been taken, which only
rose -- and subtracted one from the other at check time.

That never oversold, so no money was ever wrong. It simply never settled. The two figures
drifted for as long as the shop ran, and a store eventually refused every checkout because
its sales had outgrown a number that had not moved since the day it was seeded. In this
database that point was measured: 136 units of one SKU sold against a declared 48.

``inventory_movements`` is the fix and the audit trail at once. A sale, a delivery and a
correction are all rows; ``units`` is signed; and what the shop has is the sum. A stock
level stops being an opinion maintained beside the facts and becomes a balance derived
from them, which is how the rest of this platform already treats every number that
matters.

Two constraints carry the design. ``units <> 0``, because a movement that moves nothing is
not a movement. And the sign must match the kind -- ``RECEIVED`` and ``RETURNED`` add,
``SOLD`` takes away, and only ``ADJUSTED`` may go either way, which is exactly why an
adjustment is the one a person has to sign for. A ``SOLD`` row that added stock would leave
a balance that was right for the wrong reason, and a ledger that cannot explain itself is
worse than no ledger.

A partial unique index makes one sale move a SKU once: a retried admission, a replayed
outbox row or a second executor is refused by the database rather than by whoever
remembered to check first.

Empty on creation. Opening balances are the application's to write, per shop, because the
quantities come from the merchant simulator's fixture and a migration that reached for it
would make this package depend on the thing it stores.

Revision ID: e91c4d7a2b58
Revises: d4b7a1e93c60
"""

from __future__ import annotations

from alembic import op
from platform_db.rls import table_statements
from platform_db.schema_service import InventoryMovement

revision = "e91c4d7a2b58"
down_revision = "d4b7a1e93c60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    InventoryMovement.__table__.create(bind=op.get_bind())
    for statement in table_statements("inventory_movements"):
        op.execute(statement)


def downgrade() -> None:
    InventoryMovement.__table__.drop(bind=op.get_bind())
