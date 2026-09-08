"""The merchant is a party the platform can authenticate, and not a buyer with a label.

``api_sessions`` admitted three actors: a buyer, the agent acting for them, and an
operator. A merchant -- the human who runs the shop and approves what changes on its
shelves -- had nowhere to be. The nearest fit was OPERATOR, but that is the platform's own
operator, reached with the scenario key, and the two differ in exactly the way that
matters: an operator runs the apparatus and holds the kill switch, a merchant runs one shop
and should not.

Two changes, and the second is the one that would have bitten.

``actor_type`` gains MERCHANT. That is a widened CHECK, so it is safe in both directions:
no stored row becomes invalid, and a rollback is refused only if a merchant session exists
at the time, which ``downgrade`` deletes first rather than leaving the migration unable to
run.

``buyer_ref`` becomes nullable, and a second CHECK ties the two columns together: a
merchant session has no buyer, and every other session has one. Until now the column was
NOT NULL, and any session without a real buyer was given a minted placeholder. A merchant
row carrying one would have satisfied ``is_buyer_principal`` -- which asks only whether a
buyer reference is present -- and therefore failed ``is_merchant_principal``, and a merchant
would have been routed to the buyer's copilot. The constraint states the rule in the place
that cannot forget it, in both directions, so a buyer row with no reference is refused too.

Reversible. ``downgrade`` deletes merchant sessions before narrowing the CHECK, because a
constraint cannot be added to a table that already violates it, and a migration that can
only run on a lucky database is one nobody can deploy.

Constraint names are given in their bare form. The metadata naming convention prepends
``ck_<table>_`` itself, so passing the stored name here asks Postgres to drop
``ck_api_sessions_ck_api_sessions_actor_type_enum``, which is nothing.

Revision ID: e5c76b21a9f4
Revises: c9a4e2b16d38
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e5c76b21a9f4"
down_revision = "c9a4e2b16d38"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("actor_type_enum", "api_sessions", type_="check")
    op.create_check_constraint(
        "actor_type_enum",
        "api_sessions",
        "actor_type IN ('BUYER','AGENT','OPERATOR','MERCHANT')",
    )
    op.alter_column("api_sessions", "buyer_ref", existing_type=sa.String(128), nullable=True)
    op.create_check_constraint(
        "merchant_has_no_buyer",
        "api_sessions",
        "(actor_type = 'MERCHANT') = (buyer_ref IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("merchant_has_no_buyer", "api_sessions", type_="check")
    # Sessions are short-lived bearer tokens, so deleting the merchant ones costs their
    # holders a re-mint and nothing else. Leaving them would make the narrowed CHECK
    # unaddable and the rollback impossible on exactly the databases that used the feature.
    op.execute("DELETE FROM api_sessions WHERE actor_type = 'MERCHANT'")
    op.drop_constraint("actor_type_enum", "api_sessions", type_="check")
    op.create_check_constraint(
        "actor_type_enum",
        "api_sessions",
        "actor_type IN ('BUYER','AGENT','OPERATOR')",
    )
    op.execute("UPDATE api_sessions SET buyer_ref = 'buyer-unknown' WHERE buyer_ref IS NULL")
    op.alter_column("api_sessions", "buyer_ref", existing_type=sa.String(128), nullable=False)
