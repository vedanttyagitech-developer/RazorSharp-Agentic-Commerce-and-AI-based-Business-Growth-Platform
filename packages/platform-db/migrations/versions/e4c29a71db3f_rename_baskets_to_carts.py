"""rename baskets to carts and checkouts.basket_id to cart_id

Revision ID: e4c29a71db3f
Revises: b8f31c07a95e

Renames the pre-checkout intent table from baskets to carts and the
foreign key column on checkouts from basket_id to cart_id.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e4c29a71db3f"
down_revision = "b8f31c07a95e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Rename table baskets -> carts
    op.rename_table("baskets", "carts")

    # 2. Rename index and constraints on carts & checkouts safely
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'pk_baskets') THEN
                    ALTER TABLE carts RENAME CONSTRAINT pk_baskets TO pk_carts;
                END IF;
                IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_baskets_status_enum') THEN
                    ALTER TABLE carts RENAME CONSTRAINT ck_baskets_status_enum TO ck_carts_status_enum;
                END IF;
                IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_baskets_merchant_id') THEN
                    ALTER TABLE carts RENAME CONSTRAINT fk_baskets_merchant_id TO fk_carts_merchant_id;
                END IF;
                IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_baskets_tenant_id') THEN
                    ALTER TABLE carts RENAME CONSTRAINT fk_baskets_tenant_id TO fk_carts_tenant_id;
                END IF;
                IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_checkouts_basket_id') THEN
                    ALTER TABLE checkouts RENAME CONSTRAINT fk_checkouts_basket_id TO fk_checkouts_cart_id;
                END IF;
                IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_checkouts_tenant_id_basket_id') THEN
                    ALTER TABLE checkouts RENAME CONSTRAINT uq_checkouts_tenant_id_basket_id TO uq_checkouts_tenant_id_cart_id;
                END IF;
                IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'ix_baskets_tenant_buyer') THEN
                    ALTER INDEX ix_baskets_tenant_buyer RENAME TO ix_carts_tenant_buyer;
                END IF;
            END $$;
            """
        )
    )

    # 3. Rename checkouts.basket_id -> cart_id
    op.alter_column("checkouts", "basket_id", new_column_name="cart_id")

    # 4. Grants on renamed table
    op.execute(sa.text("GRANT SELECT, INSERT, UPDATE ON carts TO commerce_app, commerce_kernel"))


def downgrade() -> None:
    op.alter_column("checkouts", "cart_id", new_column_name="basket_id")

    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_checkouts_tenant_id_cart_id') THEN
                    ALTER TABLE checkouts RENAME CONSTRAINT uq_checkouts_tenant_id_cart_id TO uq_checkouts_tenant_id_basket_id;
                END IF;
                IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_checkouts_cart_id') THEN
                    ALTER TABLE checkouts RENAME CONSTRAINT fk_checkouts_cart_id TO fk_checkouts_basket_id;
                END IF;
                IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_carts_tenant_id') THEN
                    ALTER TABLE carts RENAME CONSTRAINT fk_carts_tenant_id TO fk_baskets_tenant_id;
                END IF;
                IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_carts_merchant_id') THEN
                    ALTER TABLE carts RENAME CONSTRAINT fk_carts_merchant_id TO fk_baskets_merchant_id;
                END IF;
                IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_carts_status_enum') THEN
                    ALTER TABLE carts RENAME CONSTRAINT ck_carts_status_enum TO ck_baskets_status_enum;
                END IF;
                IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'pk_carts') THEN
                    ALTER TABLE carts RENAME CONSTRAINT pk_carts TO pk_baskets;
                END IF;
                IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'ix_carts_tenant_buyer') THEN
                    ALTER INDEX ix_carts_tenant_buyer RENAME TO ix_baskets_tenant_buyer;
                END IF;
            END $$;
            """
        )
    )

    op.rename_table("carts", "baskets")
    op.execute(sa.text("GRANT SELECT, INSERT, UPDATE ON baskets TO commerce_app, commerce_kernel"))
