"""The simulator is a shop, not a party to the money, and this keeps it that way.

The rule this defends is one hop removed from where it is written down, which is why it
needed a test of its own rather than being obvious.

The buyer copilot may not reach the Transaction Trust Kernel. It has its own boundary test
saying so, and its own manifest no longer names the kernel. But the copilot imports this
package for the catalogue -- a locale, a text folder, a store to search -- and this package
used to carry ``kernel_adapter``, re-exported from its ``__init__``. So importing anything
at all from the simulator imported the kernel, and removing the copilot's declared
dependency moved its reach one hop instead of closing it: ``import agent_runtime`` still
loaded the money code into the process.

The bridge moved to ``merchant-adapter``, which depends on both sides, and nothing
model-facing may depend on *that*. What is left here is a catalogue, a fee engine, a store
and the scenario levers.

**Source only, on purpose.** Several tests in this package assert what the kernel is told
about a price or an offer, and they reach the adapter to do it. That is the right way round
-- the adapter depends on the simulator, never the reverse -- and it puts nothing in the
wheel: hatch packages ``src/merchant_sim`` alone, and the manifest assertion below is what
decides the dependency graph. A test importing upwards costs nothing; a source file doing
it would restore exactly the coupling this package was split to remove.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path
from typing import Final

PACKAGE_ROOT: Final = Path(__file__).resolve().parents[1]
SOURCE_ROOT: Final = PACKAGE_ROOT / "src" / "merchant_sim"

OWNED: Final[tuple[Path, ...]] = tuple(
    sorted(p for p in SOURCE_ROOT.rglob("*.py") if "__pycache__" not in p.parts)
)

MODULE: Final = "transaction_kernel"
DISTRIBUTION: Final = "transaction-kernel"


def _kernel_imports(path: Path) -> list[str]:
    """Import statements naming the kernel, at any depth, including inside a function."""
    tree = ast.parse(path.read_text(), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == MODULE or alias.name.startswith(f"{MODULE}."):
                    where = f"{path.relative_to(SOURCE_ROOT)}:{node.lineno}"
                    found.append(f"{where}: import {alias.name}")
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and (node.module == MODULE or node.module.startswith(f"{MODULE}."))
        ):
            names = ", ".join(a.name for a in node.names)
            where = f"{path.relative_to(SOURCE_ROOT)}:{node.lineno}"
            found.append(f"{where}: from {node.module} import {names}")
    return found


def test_this_package_owns_files_to_scan() -> None:
    """A glob that quietly matches nothing makes every assertion over it vacuously true."""
    assert len(OWNED) > 8, f"only found {len(OWNED)} files under {SOURCE_ROOT}"


def test_no_source_file_imports_the_kernel() -> None:
    offenders = [line for path in OWNED for line in _kernel_imports(path)]
    assert offenders == [], (
        "merchant-sim is imported by the buyer copilot, so a kernel import here reaches "
        "the copilot too. The bridge to the kernel is merchant-adapter, which may depend "
        "on both.\n  " + "\n  ".join(offenders)
    )


def test_the_dependency_is_not_declared() -> None:
    """The graph, not the source. This is the assertion ``uv tree`` would otherwise be."""
    manifest = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text())
    declared: list[str] = list(manifest["project"].get("dependencies", []))
    named = [d for d in declared if DISTRIBUTION in d]
    assert named == [], f"{DISTRIBUTION} is declared again: {named}"
