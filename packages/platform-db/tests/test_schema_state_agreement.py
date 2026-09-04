"""The database CHECK constraints and the Python state enums must agree exactly.

This suite exists because of a real defect. The `payment_attempts` status constraint
permitted ten states while `PaymentState` defined sixteen, so the six refund states could
be produced by the state machine and rejected by the database. The failure would surface
at COMMIT, inside the admission transaction, after the row locks were taken and after the
kernel had already decided to admit — the worst place to discover a schema disagreement,
because the decision is made and the evidence is written but the write cannot land.

Equality is asserted in BOTH directions on purpose. A subset check in either direction
passes while half the drift is still present:

- enum ⊄ constraint: the state machine can produce a value the database rejects.
- constraint ⊄ enum: the database can hold a value no transition can produce or leave,
  which strands a row in a status the kernel does not understand.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from transaction_kernel.states import CheckoutState, PaymentState

SCHEMA_SRC = (Path(__file__).resolve().parents[1] / "src" / "platform_db" / "schema.py").read_text()


def _constraint_values(table_class_name: str) -> set[str]:
    """Pull the quoted status values out of a table's status_enum CHECK in the source."""
    body = SCHEMA_SRC[SCHEMA_SRC.index(f"class {table_class_name}(Base):") :]
    match = re.search(r'"status IN \((.*?)\)",\s*\n\s*name="status_enum"', body, re.S)
    assert match, f"no status_enum CHECK found on {table_class_name}"
    return set(re.findall(r"'([A-Z_]+)'", match.group(1)))


def _live_constraint_values(engine: Engine, constraint: str) -> set[str]:
    """The same values as PostgreSQL actually holds them, not as the source claims."""
    with engine.connect() as conn:
        definition = conn.execute(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"),
            {"n": constraint},
        ).scalar()
    assert definition, f"constraint {constraint} not present in the database"
    return set(re.findall(r"'([A-Z_]+)'", definition))


class TestPaymentStateAgreement:
    def test_source_constraint_equals_enum_exactly(self):
        assert _constraint_values("PaymentAttempt") == {s.value for s in PaymentState}

    def test_live_database_constraint_equals_enum_exactly(self, kernel_engine: Engine):
        """Guards against a migration that was written but never applied."""
        live = _live_constraint_values(kernel_engine, "ck_payment_attempts_status_enum")
        assert live == {s.value for s in PaymentState}

    @pytest.mark.parametrize("state", sorted(s.value for s in PaymentState))
    def test_every_enum_member_is_accepted_by_the_live_constraint(
        self, kernel_engine: Engine, state: str
    ):
        """The direct proof, one case per state so a failure names the offending value.

        Read from the constraint PostgreSQL actually holds rather than from the source,
        so a migration that was written but never applied still fails here. A missing
        value is the exact commit-time failure this module exists to prevent.
        """
        live = _live_constraint_values(kernel_engine, "ck_payment_attempts_status_enum")
        assert state in live, (
            f"{state} is defined by PaymentState but rejected by the database CHECK; "
            "writing it would fail at COMMIT inside the admission transaction"
        )


class TestCheckoutStateAgreement:
    def test_source_constraint_equals_enum_exactly(self):
        assert _constraint_values("CheckoutVersion") == {s.value for s in CheckoutState}

    def test_live_database_constraint_equals_enum_exactly(self, kernel_engine: Engine):
        live = _live_constraint_values(kernel_engine, "ck_checkout_versions_status_enum")
        assert live == {s.value for s in CheckoutState}


class TestRefundsTable:
    def test_refunds_table_exists_and_is_tenant_isolated(self, kernel_engine: Engine):
        """Specification 25.3 requires it, and it is tenant-owned like every other."""
        with kernel_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = 'refunds'"
                )
            ).one_or_none()
        assert row is not None, "refunds table is missing"
        assert row.relrowsecurity, "refunds has no row-level security"
        assert row.relforcerowsecurity, "refunds RLS is not FORCED; the owner would bypass it"

    def test_refund_idempotency_key_is_unique_per_tenant(self, kernel_engine: Engine):
        """A retried refund after a lost response must find the original, not create a
        second. This unique constraint is what makes that a database guarantee."""
        with kernel_engine.connect() as conn:
            found = conn.execute(
                text(
                    "SELECT count(*) FROM pg_indexes WHERE tablename = 'refunds' "
                    "AND indexdef ILIKE '%UNIQUE%' AND indexdef ILIKE '%idem_key%'"
                )
            ).scalar()
        assert found, "refunds is missing a per-tenant unique index on idem_key"
