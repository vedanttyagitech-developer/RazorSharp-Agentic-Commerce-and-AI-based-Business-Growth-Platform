"""The privilege boundary as a source rule: nothing outside the kernel writes money.

``test_pdb_role_privileges.py`` proves PostgreSQL refuses the write. This module proves
nobody is trying, which is a different and earlier signal: a grant refusal surfaces at
runtime, in a request, usually in a demonstration; a source rule surfaces in the diff that
introduced it. ADR 0003 D1 names exactly this pairing -- "the physical boundary is the
database grant set, enforced by ``test_import_boundary`` style source tests".

Four rules, in the order they are checked:

1. No module under ``packages/commerce-api/src`` writes a financial table, whether in a raw
   SQL string or through the ORM. Reads are the whole point of those modules and are left
   alone.
2. The same for ``packages/action-executor/src``. If that package has no source yet the
   test skips with a message that says so, because a silently-passing test over an empty
   directory is worse than no test.
3. No module outside ``transaction_kernel`` -- in any package -- uses the financial ORM
   models for writing. Importing them to build a ``select`` is expected and allowed.
4. ``transaction_kernel`` imports none of ``payment_adapters``, ``durable_work``,
   ``merchant_sim``, ``commerce_api`` or ``action_executor``, so the dependency direction
   of ADR 0003 D2 cannot invert and put an adapter inside the authorization path.

**This module never imports the API package.** Five agents are writing under
``commerce_api`` and ``action_executor`` while this runs; importing them would make this
suite fail for reasons that have nothing to do with the boundary. Everything here is file
reading plus :mod:`ast`. A file that does not parse yet is reported as a warning and
excluded rather than failing the run -- the analysis is skipped for that file only, and
the warning names it, so "half-written" never silently becomes "unchecked forever".

The one import that is made is :mod:`platform_db`, to read the authoritative list of
financial tables and the ORM class that maps to each. Retyping either list here would let
a new financial table be added and go unguarded.
"""

from __future__ import annotations

import ast
import warnings
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Final, NamedTuple

import pytest
from platform_db.roles import FINANCIAL_TABLES
from platform_db.schema import Base

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
PACKAGES: Final[Path] = REPO_ROOT / "packages"

API_SRC: Final[Path] = PACKAGES / "commerce-api" / "src"
WORKER_SRC: Final[Path] = PACKAGES / "action-executor" / "src"
KERNEL_SRC: Final[Path] = PACKAGES / "transaction-kernel" / "src"

#: ADR 0003 D2. The kernel sits below all of these; an import of any of them from inside
#: ``transaction_kernel`` would mean an adapter, the outbox or an HTTP layer had become a
#: dependency of the code that decides whether money may move.
FORBIDDEN_KERNEL_IMPORTS: Final[frozenset[str]] = frozenset(
    {"payment_adapters", "durable_work", "merchant_sim", "commerce_api", "action_executor"}
)

#: The ORM classes that map to a financial table, discovered from the mapper registry so
#: that a class renamed or a table promoted into ``FINANCIAL_TABLES`` is picked up without
#: editing this file. ``audit_events`` is deliberately absent: it is append-only rather
#: than kernel-only, and ``roles.APPEND_ONLY_TABLES`` grants the worker INSERT on it, so a
#: worker module appending an audit event is correct and must not be flagged here.
FINANCIAL_MODELS: Final[frozenset[str]] = frozenset(
    mapper.class_.__name__
    for mapper in Base.registry.mappers
    if getattr(mapper.class_, "__tablename__", None) in FINANCIAL_TABLES
)

#: Session methods that persist or remove whatever they are handed.
PERSISTING_METHODS: Final[frozenset[str]] = frozenset({"add", "add_all", "delete", "merge"})

#: SQLAlchemy DML constructors. ``select`` is absent on purpose: reading is allowed.
DML_CONSTRUCTORS: Final[frozenset[str]] = frozenset({"insert", "update", "delete"})


class Violation(NamedTuple):
    """One place a rule was broken, phrased so the failure message is actionable."""

    path: Path
    line: int
    detail: str
    subject: str = ""  # the financial table, or the ORM class, that was written
    kind: str = ""  # INSERT INTO / UPDATE / DELETE FROM / the ORM write shape

    @property
    def module(self) -> str:
        return str(self.path.relative_to(PACKAGES))

    def __str__(self) -> str:
        return f"{self.path.relative_to(REPO_ROOT)}:{self.line}: {self.detail}"


class Exemption(NamedTuple):
    """A write that is known, argued for, and deliberately not failing the build.

    Matched on module, subject and kind but never on line number, so moving the statement
    within its file does not break the build, while adding a second write does.
    """

    module: str
    subject: str
    kind: str
    reason: str

    def matches(self, violation: Violation) -> bool:
        return (violation.module, violation.subject, violation.kind) == (
            self.module,
            self.subject,
            self.kind,
        )


#: Every exemption is a debt, so each one names the file, the table, the statement kind and
#: the argument made for it, and :meth:`TestTheExemptionsAreHonest` fails if one stops
#: applying -- a list that can only grow is a list nobody reads.
#:
#: The scenario controller's fast-forward is the sole entry. It moves
#: ``reservations.expires_at`` to ``now()`` so that a demonstration need not wait out the
#: 300-second TTL, then calls ``transaction_kernel.reservations.release`` to make the
#: ACTIVE -> EXPIRED transition; ``status`` is never written outside the kernel. It is
#: still an API process issuing an UPDATE against a kernel-only table, and the grant only
#: permits it because API mutations run as ``commerce_kernel`` (ADR 0003 D1). The clean
#: shape is a kernel-owned ``fast_forward_reservation`` behind the scenario key; until that
#: exists this records the exception rather than hiding it.
KNOWN_EXCEPTIONS: Final[tuple[Exemption, ...]] = (
    Exemption(
        module="commerce-api/src/commerce_api/services/scenario_service.py",
        subject="reservations",
        kind="UPDATE",
        reason=(
            "scenario fast-forward moves the reservation deadline so an expiry can be "
            "demonstrated without waiting 300 seconds; the ACTIVE -> EXPIRED transition "
            "itself is still made by transaction_kernel.reservations.release. Belongs in "
            "the kernel behind the scenario key."
        ),
    ),
)


def _unexempted(violations: list[Violation]) -> list[Violation]:
    return [v for v in violations if not any(e.matches(v) for e in KNOWN_EXCEPTIONS)]


# ----------------------------------------------------------------------- source discovery


def _python_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _has_source(root: Path) -> bool:
    """True when the tree holds at least one module with a statement in it.

    A package skeleton -- an ``__init__.py`` holding only a docstring -- is not source.
    Rule 2 uses this to decide between running and skipping with an explanation.
    """
    for path in _python_files(root):
        tree = _parse(path)
        if tree is None:
            return True  # unparseable means somebody is writing in it right now
        body = [node for node in tree.body if not _is_docstring(node)]
        if body:
            return True
    return False


def _is_docstring(node: ast.stmt) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)


def _parse(path: Path) -> ast.Module | None:
    """Parse ``path``, or return ``None`` if it is mid-edit and does not compile yet."""
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError, UnicodeDecodeError:
        return None


def _parsed(root: Path) -> Iterator[tuple[Path, ast.Module]]:
    """Every module under ``root`` that currently parses, warning about those that do not.

    The warning is the honest half. Other agents are writing these files as this suite
    runs, so a transient syntax error must not turn the boundary check red -- but a file
    excluded from analysis has to be named somewhere, or a broken file becomes a permanent
    blind spot that nobody notices.
    """
    unparseable: list[str] = []
    for path in _python_files(root):
        tree = _parse(path)
        if tree is None:
            unparseable.append(str(path.relative_to(REPO_ROOT)))
            continue
        yield path, tree
    if unparseable:
        warnings.warn(
            "boundary check skipped files that do not parse yet: " + ", ".join(unparseable),
            stacklevel=2,
        )


# ------------------------------------------------------------------------- raw SQL rule


def _sql_strings(tree: ast.Module) -> Iterator[tuple[int, str]]:
    """Every string constant that is not a docstring.

    Docstrings are excluded because this codebase documents the SQL it is not allowed to
    write -- ``services/inbox_store.py`` opens by quoting an ``INSERT INTO webhook_inbox``
    to explain which role runs it -- and a rule that cannot tell prose from a statement
    would punish exactly the modules that were careful enough to explain themselves.
    """
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and _is_docstring(node.body[0])
        and isinstance(node.body[0], ast.Expr)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            yield node.lineno, node.value


def _sql_writes(text: str) -> Iterator[tuple[str, str]]:
    """``(table, statement kind)`` for each write this string aims at a financial table.

    Split on words rather than matched with one regex so that a statement built from
    concatenated fragments -- ``"INSERT INTO orders ("`` on one line, the column list on
    the next -- is still caught by the fragment that names the table.
    """
    words = text.replace("(", " ( ").replace(",", " , ").split()
    lowered = [w.lower() for w in words]
    for index, word in enumerate(lowered):
        target: str | None = None
        kind = ""
        if word == "into" and index and lowered[index - 1] == "insert":
            target, kind = _next_identifier(words, index + 1), "INSERT INTO"
        elif word == "from" and index and lowered[index - 1] == "delete":
            target, kind = _next_identifier(words, index + 1), "DELETE FROM"
        elif word == "update":
            candidate = _next_identifier(words, index + 1)
            # An UPDATE only counts when the statement really is one: the next keyword
            # after the table name has to be SET.
            if candidate and "set" in lowered[index + 1 : index + 4]:
                target, kind = candidate, "UPDATE"
        if target and target.strip('"').lower() in FINANCIAL_TABLES:
            yield target.strip('"').lower(), kind


def _next_identifier(words: list[str], start: int) -> str | None:
    for word in words[start:]:
        stripped = word.strip('";')
        if stripped and (stripped[0].isalpha() or stripped[0] == '"'):
            return stripped
        return None
    return None


def _raw_sql_violations(root: Path) -> list[Violation]:
    found: list[Violation] = []
    for path, tree in _parsed(root):
        for line, value in _sql_strings(tree):
            for table, kind in _sql_writes(value):
                found.append(Violation(path, line, f"raw SQL {kind} {table}", table, kind))
    return found


# ------------------------------------------------------------------------- ORM write rule


class _OrmWriteVisitor(ast.NodeVisitor):
    """Flags every use of a financial ORM model that is not a read.

    What counts as a write, and why each shape is here:

    * ``Order(...)`` -- a mapped instance is constructed in order to be persisted. There
      is no read that needs one.
    * ``insert(Order)`` / ``update(Order)`` / ``delete(Order)`` -- SQLAlchemy DML.
      ``select(Order)`` is absent from the list on purpose.
    * ``session.add(x)``, ``add_all``, ``delete``, ``merge`` -- the unit-of-work entry
      points.
    * ``attempt.status = ...`` where ``attempt`` is annotated ``PaymentAttempt`` -- the
      quietest shortcut of the lot, because it looks like ordinary attribute assignment
      and is flushed by whatever commits next. Annotations are how this is detected, so a
      completely unannotated helper could evade it; the database grant is what makes that
      evasion harmless.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.violations: list[Violation] = []
        self._financial_names: set[str] = set()

    # -- tracking which local names hold a financial row -------------------------------

    def _note_annotation(self, name: str, annotation: ast.expr | None) -> None:
        if annotation is not None and _annotation_names(annotation) & FINANCIAL_MODELS:
            self._financial_names.add(name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        args = node.args
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg):
            if arg is not None:
                self._note_annotation(arg.arg, arg.annotation)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        if isinstance(node.target, ast.Name):
            self._note_annotation(node.target.id, node.annotation)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        if _called_name(node.value) in FINANCIAL_MODELS:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self._financial_names.add(target.id)
        self._check_attribute_targets(node.targets, node.lineno)
        self.generic_visit(node)

    # -- the rules ---------------------------------------------------------------------

    def _check_attribute_targets(self, targets: Sequence[ast.expr], line: int) -> None:
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id in self._financial_names
            ):
                self.violations.append(
                    Violation(
                        self.path,
                        line,
                        f"assigns {target.value.id}.{target.attr}",
                        target.value.id,
                        "ATTRIBUTE ASSIGNMENT",
                    )
                )

    def visit_AugAssign(self, node: ast.AugAssign) -> None:  # noqa: N802
        self._check_attribute_targets([node.target], node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        name = _called_name(node)
        if name in FINANCIAL_MODELS:
            self.violations.append(
                Violation(self.path, node.lineno, f"constructs {name}()", name, "CONSTRUCTION")
            )
        elif name in DML_CONSTRUCTORS and node.args:
            first = node.args[0]
            if isinstance(first, ast.Name) and first.id in FINANCIAL_MODELS:
                self.violations.append(
                    Violation(
                        self.path,
                        node.lineno,
                        f"{name}({first.id}) DML on a financial table",
                        first.id,
                        name.upper(),
                    )
                )
        elif isinstance(node.func, ast.Attribute) and node.func.attr in PERSISTING_METHODS:
            for arg in node.args:
                if isinstance(arg, ast.Name) and arg.id in self._financial_names:
                    self.violations.append(
                        Violation(
                            self.path,
                            node.lineno,
                            f"session.{node.func.attr}({arg.id})",
                            arg.id,
                            f"SESSION.{node.func.attr.upper()}",
                        )
                    )
        self.generic_visit(node)


def _called_name(node: ast.expr | None) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _annotation_names(node: ast.expr) -> set[str]:
    """Bare names inside an annotation, so ``PaymentAttempt | None`` is recognised."""
    return {inner.id for inner in ast.walk(node) if isinstance(inner, ast.Name)} | {
        inner.value
        for inner in ast.walk(node)
        if isinstance(inner, ast.Constant) and isinstance(inner.value, str)
    }


def _orm_write_violations(root: Path) -> list[Violation]:
    found: list[Violation] = []
    for path, tree in _parsed(root):
        visitor = _OrmWriteVisitor(path)
        visitor.visit(tree)
        found.extend(visitor.violations)
    return found


def _format(violations: list[Violation], rule: str) -> str:
    listing = "\n  ".join(str(v) for v in violations)
    return (
        f"{rule}\nOnly transaction_kernel writes financial tables (ADR 0003 D1). "
        f"Call a kernel function instead.\n  {listing}"
    )


# ------------------------------------------------------------------------------- tests


def test_the_financial_model_list_was_discovered() -> None:
    """Guards the guard: an empty model set would make every rule below vacuous."""
    assert FINANCIAL_TABLES, "platform_db declares no financial tables"
    assert len(FINANCIAL_MODELS) == len(FINANCIAL_TABLES), (
        f"{len(FINANCIAL_MODELS)} ORM classes found for {len(FINANCIAL_TABLES)} financial "
        f"tables; the mapper registry and roles.FINANCIAL_TABLES have diverged: "
        f"{sorted(FINANCIAL_MODELS)}"
    )


class TestCommerceApiDoesNotWriteMoney:
    def test_no_raw_sql_write_to_a_financial_table(self) -> None:
        assert API_SRC.is_dir(), f"{API_SRC} does not exist"
        violations = _unexempted(_raw_sql_violations(API_SRC))
        assert violations == [], _format(violations, "commerce-api writes financial tables in SQL:")

    def test_no_orm_write_to_a_financial_table(self) -> None:
        violations = _unexempted(_orm_write_violations(API_SRC))
        assert violations == [], _format(violations, "commerce-api writes financial ORM models:")


class TestDurableWorkerDoesNotWriteMoney:
    """The worker holds a kernel engine (ADR 0003 D1) and so is the likelier shortcut.

    It has a legitimate reason to open a kernel-role connection -- it calls kernel
    functions -- which makes writing a payment row from a handler one line away rather
    than an obvious violation.
    """

    def test_no_raw_sql_write_to_a_financial_table(self) -> None:
        if not _has_source(WORKER_SRC):
            pytest.skip(
                f"{WORKER_SRC.relative_to(REPO_ROOT)} holds no Python source yet, so this "
                "rule checked nothing. It is not passing; it did not run."
            )
        violations = _unexempted(_raw_sql_violations(WORKER_SRC))
        assert violations == [], _format(
            violations, "action-executor writes financial tables in SQL:"
        )

    def test_no_orm_write_to_a_financial_table(self) -> None:
        if not _has_source(WORKER_SRC):
            pytest.skip(
                f"{WORKER_SRC.relative_to(REPO_ROOT)} holds no Python source yet, so this "
                "rule checked nothing. It is not passing; it did not run."
            )
        violations = _unexempted(_orm_write_violations(WORKER_SRC))
        assert violations == [], _format(violations, "action-executor writes financial ORM models:")


class TestOnlyTheKernelWritesFinancialModels:
    """Rule 3, widened past the two packages above to every package in the workspace.

    Importing ``PaymentAttempt`` to build a ``select`` is normal and appears in several
    API services; what may not appear anywhere outside ``transaction_kernel`` is a use of
    one of those classes that would produce an INSERT, UPDATE or DELETE.
    """

    @staticmethod
    def _other_package_sources() -> list[Path]:
        return [
            package / "src"
            for package in sorted(PACKAGES.iterdir())
            if package.is_dir()
            and package.name != "transaction-kernel"
            and (package / "src").is_dir()
        ]

    def test_no_package_outside_the_kernel_writes_a_financial_model(self) -> None:
        roots = self._other_package_sources()
        assert roots, "no package sources found; the scan would be vacuous"
        violations = _unexempted([v for root in roots for v in _orm_write_violations(root)])
        assert violations == [], _format(
            violations, "financial ORM models are written outside transaction_kernel:"
        )

    def test_no_package_outside_the_kernel_writes_financial_sql(self) -> None:
        roots = self._other_package_sources()
        violations = _unexempted([v for root in roots for v in _raw_sql_violations(root)])
        assert violations == [], _format(
            violations, "financial tables are written in SQL outside transaction_kernel:"
        )

    def test_the_kernel_itself_does_write_them(self) -> None:
        """Otherwise the three tests above would pass on a codebase that writes nothing.

        The kernel is the one place these writes belong, so finding them there is what
        makes their absence everywhere else meaningful.
        """
        writes = _orm_write_violations(KERNEL_SRC) + _raw_sql_violations(KERNEL_SRC)
        assert writes, (
            "transaction_kernel writes no financial table anywhere; either the scanner is "
            "broken or the kernel no longer owns the financial path"
        )


class TestTheExemptionsAreHonest:
    """An exemption list only works if it is smaller than it wants to be.

    Two failure modes are guarded. An entry that no longer matches anything is dead: the
    shortcut it excused has been removed, and leaving the entry behind would silently
    re-permit that write later. An entry with no argument written down is a rubber stamp.
    """

    def test_every_exemption_still_applies(self) -> None:
        """Scans exactly the file each exemption names, so nothing else can keep it alive."""
        dead: list[Exemption] = []
        for exemption in KNOWN_EXCEPTIONS:
            target = PACKAGES / exemption.module
            if not target.is_file():
                dead.append(exemption)
                continue
            found = _raw_sql_violations(target.parent) + _orm_write_violations(target.parent)
            if not any(exemption.matches(v) for v in found):
                dead.append(exemption)
        assert dead == [], (
            "these exemptions no longer match any code, so the shortcut they excused is "
            "gone. Delete them from KNOWN_EXCEPTIONS in this file:\n  "
            + "\n  ".join(f"{e.module}: {e.kind} {e.subject}" for e in dead)
        )

    def test_every_exemption_carries_an_argument(self) -> None:
        for exemption in KNOWN_EXCEPTIONS:
            assert len(exemption.reason) > 60, (
                f"{exemption.module} is exempted without a stated reason; an exemption "
                "nobody had to justify is just a hole"
            )


class TestDependencyDirection:
    """ADR 0003 D2. The kernel is below the adapters, the outbox and the HTTP layer."""

    def test_transaction_kernel_imports_no_higher_package(self) -> None:
        assert KERNEL_SRC.is_dir(), f"{KERNEL_SRC} does not exist"
        violations: list[Violation] = []
        for path, tree in _parsed(KERNEL_SRC):
            for node in ast.walk(tree):
                if not isinstance(node, ast.Import | ast.ImportFrom):
                    continue
                for module in _imported_modules(node):
                    root = module.split(".")[0]
                    if root in FORBIDDEN_KERNEL_IMPORTS:
                        violations.append(
                            Violation(path, node.lineno, f"imports {module}", root, "IMPORT")
                        )
        assert violations == [], (
            "transaction_kernel imports a package that sits above it (ADR 0003 D2), so the "
            "dependency direction has inverted:\n  " + "\n  ".join(str(v) for v in violations)
        )


def _imported_modules(node: ast.Import | ast.ImportFrom) -> Iterator[str]:
    if isinstance(node, ast.Import):
        for alias in node.names:
            yield alias.name
    elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
        yield node.module
