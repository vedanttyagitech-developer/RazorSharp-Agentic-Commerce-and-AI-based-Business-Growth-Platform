"""The structural claims, asserted against the source rather than trusted.

This package's central promise is that nothing in it can be on the path that decides
whether money moves. That is not something a docstring can guarantee and not something a
behavioural test can cover, because the failure mode is a future edit: somebody adds an
``httpx`` push exporter, or a background flush thread, or an import of
``transaction_kernel`` for a convenient enum, and the promise quietly stops being true
while every existing test still passes.

So the promise is checked where it can be broken -- in the imports, in the dependency
declaration, and in the workspace registration -- by reading the source.

The dependency check is the same technique ``test_import_boundary`` uses for the kernel's
own layering (ADR 0003 D2). Here the rule is stricter than "no cycles": **nothing outside
the standard library at all**. A package with no dependencies cannot be in another
package's dependency closure by accident, cannot pull a transitive version conflict into
the API, and cannot be tempted into importing a kernel type "just for the enum" -- which is
how an observability layer ends up on a money path in the first place.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

import platform_observability
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = Path(platform_observability.__file__).resolve().parent
SOURCES = sorted(PACKAGE_ROOT.glob("*.py"))

#: Modules whose presence would mean this package can reach the network, the disk or a
#: thread of its own -- the three things that let an observability layer block a payment.
FORBIDDEN_MODULES = frozenset(
    {
        "asyncio",
        "concurrent",
        "http",
        "multiprocessing",
        "queue",
        "select",
        "selectors",
        "shutil",
        "signal",
        "socket",
        "socketserver",
        "ssl",
        "subprocess",
        "tempfile",
        "urllib",
        "wsgiref",
    }
)


def top_level_imports(path: Path) -> set[str]:
    """Every top-level module this file imports, relative imports excluded."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


class TestNoDependencies:
    def test_there_are_sources_to_check(self) -> None:
        """A guard on the guard: a glob that matches nothing passes every test below."""
        assert len(SOURCES) >= 6, [path.name for path in SOURCES]

    @pytest.mark.parametrize("path", SOURCES, ids=lambda path: path.name)
    def test_every_import_is_from_the_standard_library(self, path: Path) -> None:
        """No workspace package, no third-party package, nothing.

        The moment this package imports ``transaction_kernel`` for a ``RecoveryCode``, the
        kernel has an observability layer in its reverse-dependency graph and a change here
        can break a money path. The reason codes are strings in the label values instead,
        which costs a little duplication and buys the entire guarantee.
        """
        foreign = {
            name
            for name in top_level_imports(path)
            if name not in sys.stdlib_module_names and name != "platform_observability"
        }
        assert not foreign, f"{path.name} imports {sorted(foreign)}"

    @pytest.mark.parametrize("path", SOURCES, ids=lambda path: path.name)
    def test_nothing_reaches_the_network_the_disk_or_a_thread_pool(self, path: Path) -> None:
        """``threading`` is permitted and used -- for one ``Lock``. A lock is not a thread,
        and the assertion below is what keeps that distinction honest."""
        assert not (top_level_imports(path) & FORBIDDEN_MODULES), path.name

    def test_threading_is_used_only_for_a_lock(self) -> None:
        source = (PACKAGE_ROOT / "metrics.py").read_text(encoding="utf-8")
        assert "threading.Lock()" in source
        assert "Thread(" not in source
        assert "start()" not in source

    def test_the_package_declares_no_dependencies(self) -> None:
        manifest = tomllib.loads(
            (REPO_ROOT / "packages/platform-observability/pyproject.toml").read_text("utf-8")
        )
        assert manifest["project"]["dependencies"] == []
        assert manifest["project"]["name"] == "platform-observability"


class TestWorkspaceRegistration:
    """A member missing from ``[tool.uv.sources]`` makes ``uv sync`` fail outright.

    Both halves are needed and it is the second that is easy to forget, so both are
    asserted here rather than discovered by the next person to run a clean sync.
    """

    def test_it_is_a_root_dependency_and_a_workspace_source(self) -> None:
        root = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text("utf-8"))
        assert "platform-observability" in root["project"]["dependencies"]
        assert root["tool"]["uv"]["sources"]["platform-observability"] == {"workspace": True}

    def test_the_package_directory_matches_the_workspace_glob(self) -> None:
        root = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text("utf-8"))
        assert root["tool"]["uv"]["workspace"]["members"] == ["packages/*"]
        assert (REPO_ROOT / "packages/platform-observability/pyproject.toml").is_file()

    def test_the_package_is_typed(self) -> None:
        assert (PACKAGE_ROOT / "py.typed").is_file()


class TestNothingGatesADecision:
    """No caller can observe whether a recording worked, so none can come to depend on it."""

    @pytest.mark.parametrize(
        "method",
        ["increment", "set_gauge", "observe", "add_gauge"],
    )
    def test_a_recording_method_is_annotated_as_returning_none(self, method: str) -> None:
        """Read off the annotation rather than a call, so a future edit that starts
        returning a success flag fails here rather than in review."""
        for owner in (
            platform_observability.MetricsRegistry,
            platform_observability.TenantMetrics,
        ):
            function = getattr(owner, method, None)
            if function is None:
                continue
            assert function.__annotations__["return"] == "None", (owner.__name__, method)

    def test_the_public_surface_exposes_no_way_to_read_a_failure(self) -> None:
        """``quarantined_sinks`` and the ``observability_*`` counters are the only routes to
        "the telemetry is broken", and both are for an operator rather than for a branch in
        commerce code."""
        registry = platform_observability.MetricsRegistry()
        assert registry.quarantined_sinks() == ()
        assert registry.value("observability_errors_total", operation="record") is None
