"""The buyer copilot cannot reach the money code, and this is what says so.

The rule is that a package a model drives may not import the Transaction Trust Kernel. It
was true in practice for a long time and false as a property: this package declared
``transaction-kernel`` as a dependency in order to name seven value types, which also put
``admit``, ``authorize``, ``consume_grant`` and every financial write one import line away.
Nothing in the build refused that line. Nobody had written it, which is a fact about the
past rather than a guarantee about the next change.

So the seven moved to ``commerce-domain`` and the dependency was removed outright. These
tests are the part that keeps it removed. Two of them, because the two failures are
different and only one is loud:

* An import added to the source fails at run time, which a test suite catches.
* The dependency re-appearing in ``pyproject.toml`` fails nothing at all. It is the door
  being unlocked again, and everything keeps working, which is precisely why it needs an
  assertion of its own.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path
from typing import Final

PACKAGE_ROOT: Final = Path(__file__).resolve().parents[1]

#: Every Python file this package owns, source and tests alike. Tests count: a fixture that
#: imports the kernel puts it back in ``pyproject.toml`` as a test dependency, and the
#: dependency is the thing being removed.
OWNED: Final[tuple[Path, ...]] = tuple(
    sorted(p for p in PACKAGE_ROOT.rglob("*.py") if "__pycache__" not in p.parts)
)

MODULE: Final = "transaction_kernel"
DISTRIBUTION: Final = "transaction-kernel"


def _kernel_imports(path: Path) -> list[str]:
    """Import statements naming the kernel, anywhere in the file, at any depth.

    The AST is walked rather than the module inspected, because an import inside a function
    body, inside ``if TYPE_CHECKING`` or behind a ``try`` is still this package naming the
    kernel and none of those is visible in ``sys.modules`` after a plain import.

    Prose is deliberately not an offence. A docstring that explains what the kernel sends
    this package is documentation, and forbidding it would buy nothing except vaguer
    documentation: a sentence cannot call ``admit``. What is forbidden is the statement that
    makes the call possible.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == MODULE or alias.name.startswith(f"{MODULE}."):
                    found.append(
                        f"{path.relative_to(PACKAGE_ROOT)}:{node.lineno}: import {alias.name}"
                    )
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and (node.module == MODULE or node.module.startswith(f"{MODULE}."))
        ):
            names = ", ".join(a.name for a in node.names)
            where = f"{path.relative_to(PACKAGE_ROOT)}:{node.lineno}"
            found.append(f"{where}: from {node.module} import {names}")
    return found


def test_this_package_owns_files_to_scan() -> None:
    """Guards the two tests below against passing because they found nothing.

    A glob that silently matches no files makes every assertion over it vacuously true,
    which is the failure mode a boundary test can least afford: it would report the door
    locked while never having looked at it.
    """
    assert len(OWNED) > 20, f"only found {len(OWNED)} files under {PACKAGE_ROOT}"


def test_no_file_imports_the_kernel() -> None:
    offenders = [line for path in OWNED for line in _kernel_imports(path)]
    assert offenders == [], (
        "the buyer copilot may not import the Transaction Trust Kernel. The shared "
        "value types live in commerce_domain; anything else there is a capability this "
        "package is not allowed to hold.\n  " + "\n  ".join(offenders)
    )


def test_the_dependency_is_not_declared() -> None:
    """The quiet half. A restored dependency breaks nothing, which is the problem."""
    manifest = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text())
    declared: list[str] = list(manifest["project"].get("dependencies", []))
    for group in manifest.get("dependency-groups", {}).values():
        declared.extend(str(entry) for entry in group)
    for extra in manifest["project"].get("optional-dependencies", {}).values():
        declared.extend(str(entry) for entry in extra)
    named = [d for d in declared if DISTRIBUTION in d]
    assert named == [], f"{DISTRIBUTION} is declared again: {named}"
