"""Bootstrap reruns must not restore mutable evidence or bypass tenant isolation."""

from pathlib import Path

import pytest
from platform_db.rls import grants_sql, rls_statements
from sqlalchemy import text

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("profile", ["dev", "test"])
def test_bootstrap_preserves_repaired_privileges(admin_engine, profile):
    with admin_engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(
                text("GRANT INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO commerce_kernel")
            )
            connection.execute(text("GRANT UPDATE ON webhook_inbox TO commerce_worker"))
            connection.execute(text("ALTER TABLE carts DISABLE ROW LEVEL SECURITY"))
            # The same repair the migration installs, including narrowing older grants.
            for statement in [*rls_statements("carts"), *grants_sql()]:
                connection.execute(text(statement))
            script = (ROOT / "scripts" / f"bootstrap_{profile}_roles.sql").read_text()
            for _ in range(2):
                connection.exec_driver_sql(script)
                for role, table in [
                    ("commerce_kernel", "merchant_policy_versions"),
                    ("commerce_kernel", "verified_authority_proofs"),
                    ("commerce_kernel", "inventory_movements"),
                    ("commerce_worker", "webhook_inbox"),
                ]:
                    assert (
                        connection.execute(
                            text("SELECT has_table_privilege(:r, :t, 'UPDATE')"),
                            {"r": role, "t": table},
                        ).scalar()
                        is False
                    )
                assert (
                    connection.execute(
                        text(
                            "SELECT relrowsecurity AND relforcerowsecurity "
                            "FROM pg_class WHERE oid = 'carts'::regclass"
                        )
                    ).scalar()
                    is True
                )
                assert (
                    connection.execute(
                        text(
                            "SELECT has_table_privilege('commerce_kernel', "
                            "'payment_attempts', 'UPDATE')"
                        )
                    ).scalar()
                    is True
                )
                assert (
                    connection.execute(
                        text("SELECT has_table_privilege('commerce_app', 'carts', 'UPDATE')")
                    ).scalar()
                    is True
                )
                assert (
                    connection.execute(
                        text(
                            "SELECT has_column_privilege('commerce_worker', "
                            "'outbox_events', 'status', 'UPDATE')"
                        )
                    ).scalar()
                    is True
                )
                assert (
                    connection.execute(
                        text(
                            "SELECT has_column_privilege('commerce_worker', "
                            "'outbox_events', 'payload', 'UPDATE')"
                        )
                    ).scalar()
                    is False
                )
        finally:
            transaction.rollback()
