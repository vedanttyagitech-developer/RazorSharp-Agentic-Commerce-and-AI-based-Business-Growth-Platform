"""The boundary, asserted twice: in the source, and in the database.

The rule is "only transaction-kernel writes financial tables". A lint of the source is
worth having because it fails in review rather than in production, and it is not
sufficient on its own -- a determined caller can always import a different module. So the
last test in this file connects as ``commerce_worker`` and tries the write for real.

The source checks read the worker's own SQL rather than a list somebody maintains by
hand: :data:`platform_db.FINANCIAL_TABLES` is the same constant the grants are generated
from, so a table added there is covered here on the next run without anyone remembering.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Final

import pytest
from platform_db import FINANCIAL_TABLES
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

SOURCE_ROOT: Final = Path(__file__).resolve().parents[1] / "src" / "action_executor"

#: The only tables this package may write with its own SQL. ``outbox_events`` is absent
#: on purpose: the worker writes it exclusively through ``durable_work``, which owns the
#: leasing protocol, and a hand-written UPDATE here would be a second implementation of it.
WRITABLE_TABLES: Final[frozenset[str]] = frozenset({"scenario_faults"})

#: ORM classes that map a financial table. Importing one into this package would be the
#: first step of writing it, and there is no legitimate second step.
FINANCIAL_MODELS: Final[frozenset[str]] = frozenset(
    {
        "Approval",
        "CheckoutVersion",
        "DelegatedAuthority",
        "ExecutionGrant",
        "IdempotencyRecord",
        "Order",
        "PaymentAttempt",
        "PolicyAtSaleReceipt",
        "ProviderRequest",
        "ReconciliationRun",
        "Refund",
        "Reservation",
    }
)

#: ``INSERT INTO x`` / ``UPDATE x`` with the table optionally quoted or schema-qualified.
_WRITE = re.compile(
    r"\b(?:insert\s+into|update)\s+(?:public\.)?\"?([a-z_][a-z0-9_]*)\"?",
    re.IGNORECASE,
)

#: Words that follow UPDATE without naming a table. ``SKIP`` is the one that matters:
#: ``SELECT ... FOR UPDATE SKIP LOCKED`` is a lock, not a write.
_SQL_KEYWORDS: Final[frozenset[str]] = frozenset({"skip", "set", "on", "of", "nowait"})


def source_files() -> list[Path]:
    return sorted(SOURCE_ROOT.rglob("*.py"))


def sql_literals(path: Path) -> list[str]:
    """Every string handed to ``sqlalchemy.text(...)`` in a module.

    Only these, and not every string constant: prose such as "holds UPDATE on
    ``scenario_faults``" would otherwise read as a write, and a check that cries wolf on
    its own documentation gets deleted. SQLAlchemy 2 refuses a bare string in
    ``execute()``, so ``text()`` really is the only door raw SQL comes through.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else None
        if isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name != "text":
            continue
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                found.append(argument.value)
    return found


def written_tables(path: Path) -> set[str]:
    found: set[str] = set()
    for statement in sql_literals(path):
        collapsed = " ".join(statement.split())
        found.update(match.group(1).lower() for match in _WRITE.finditer(collapsed))
    return found - _SQL_KEYWORDS


def imported_names(path: Path) -> set[tuple[str, str]]:
    """``(module, name)`` for every ``from module import name`` in the file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.update((node.module, alias.name) for alias in node.names)
    return names


class TestSourceBoundary:
    def test_there_is_source_to_check(self) -> None:
        """A boundary test that silently checks nothing is worse than no test at all."""
        assert len(source_files()) >= 10

    def test_no_module_writes_a_financial_table(self) -> None:
        offenders = {
            path.name: sorted(written_tables(path) & set(FINANCIAL_TABLES))
            for path in source_files()
            if written_tables(path) & set(FINANCIAL_TABLES)
        }
        assert offenders == {}, (
            "the worker calls kernel functions and never writes money itself; "
            f"found direct writes: {offenders}"
        )

    def test_the_only_table_the_worker_writes_itself_is_the_demo_apparatus(self) -> None:
        written: set[str] = set()
        for path in source_files():
            written |= written_tables(path)
        assert written <= WRITABLE_TABLES, (
            f"unexpected direct writes to {sorted(written - WRITABLE_TABLES)}; "
            "every other table is written through transaction_kernel or durable_work"
        )

    def test_no_module_imports_a_financial_orm_model(self) -> None:
        offenders = {
            path.name: sorted(
                name
                for module, name in imported_names(path)
                if module.startswith("platform_db") and name in FINANCIAL_MODELS
            )
            for path in source_files()
        }
        assert {k: v for k, v in offenders.items() if v} == {}, (
            f"financial ORM models must not be reachable from this package: {offenders}"
        )

    def test_the_kernel_does_not_import_the_worker_or_its_dependencies(self) -> None:
        """ADR 0003 D2: the dependency direction is one-way, and a cycle would break it."""
        kernel_root = SOURCE_ROOT.parents[2] / "transaction-kernel" / "src" / "transaction_kernel"
        assert kernel_root.is_dir()
        forbidden = ("action_executor", "durable_work", "payment_adapters", "commerce_api")
        offenders = {
            path.name: sorted(
                module for module, _ in imported_names(path) if module.split(".")[0] in forbidden
            )
            for path in sorted(kernel_root.rglob("*.py"))
        }
        assert {k: v for k, v in offenders.items() if v} == {}


@pytest.mark.db
class TestDatabaseBoundary:
    def test_the_worker_role_cannot_write_a_payment_attempt(
        self, dwk_worker_engine: Engine
    ) -> None:
        """The lint above can be bypassed by importing something else. This cannot.

        ``commerce_worker`` holds SELECT on every financial table and no INSERT or UPDATE
        on any of them, so the boundary survives a mistake in the Python.
        """
        with pytest.raises(ProgrammingError) as refused, dwk_worker_engine.begin() as conn:
            conn.execute(text("UPDATE payment_attempts SET status = 'CAPTURED'"))
        assert "permission denied" in str(refused.value).lower()

    def test_the_worker_role_cannot_write_an_execution_grant(
        self, dwk_worker_engine: Engine
    ) -> None:
        with pytest.raises(ProgrammingError) as refused, dwk_worker_engine.begin() as conn:
            conn.execute(text("UPDATE execution_grants SET status = 'ISSUED'"))
        assert "permission denied" in str(refused.value).lower()

    def test_the_worker_role_may_still_read_what_it_must_reconcile(
        self, dwk_worker_engine: Engine
    ) -> None:
        """Least privilege, not blindness: the worker reads financial state, it just
        cannot change it."""
        with dwk_worker_engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', NULL, true)"))
            assert conn.execute(text("SELECT count(*) FROM payment_attempts")).scalar() == 0


class TestTheAuditStreamsThisPackageAuthors:
    """The declared append scope and the appends this package makes must not drift.

    ``platform_db.roles.APPEND_SCOPE`` narrows the worker credential to one audit stream,
    and the database enforces it with a restrictive policy. That is the right way round --
    the executor should not be able to author a checkout's evidence -- but it means a
    handler that starts appending a second aggregate type on a worker session gets refused
    at runtime. The one row this scope covers is the dead-letter record, which is the
    message with nowhere else to go, so the failure has to be found here instead.

    The source scan is deliberately literal about sessions. An ``append`` reached from a
    ``kernel_session`` block is unaffected by the policy, and this package makes several;
    only the ones under ``worker_session`` are bound by it.
    """

    def test_the_scope_names_exactly_the_dead_letter_stream(self) -> None:
        from action_executor.loop import _DEAD_LETTER_AGGREGATE
        from platform_db.roles import APPEND_SCOPE, WORKER

        assert APPEND_SCOPE["audit_events"][WORKER] == (_DEAD_LETTER_AGGREGATE,)

    def test_no_source_file_appends_under_a_worker_session_beyond_that(self) -> None:
        """Read the source rather than trusting the class docstring above to stay true."""
        from action_executor.loop import _DEAD_LETTER_AGGREGATE

        offenders: list[str] = []
        for path in sorted(SOURCE_ROOT.rglob("*.py")):
            lines = path.read_text().splitlines()
            for number, line in enumerate(lines):
                if "worker_session()" not in line or line.lstrip().startswith("def "):
                    continue
                indent = len(line) - len(line.lstrip())
                body: list[str] = []
                for following in lines[number + 1 :]:
                    if following.strip() and (len(following) - len(following.lstrip())) <= indent:
                        break
                    body.append(following)
                block = "\n".join(body)
                if "aggregate_type=" in block and _DEAD_LETTER_AGGREGATE not in block:
                    offenders.append(f"{path.name}:{number + 1}")
        assert offenders == [], (
            "an append under a worker session names an aggregate type outside "
            f"APPEND_SCOPE, and the database will refuse it: {offenders}"
        )
