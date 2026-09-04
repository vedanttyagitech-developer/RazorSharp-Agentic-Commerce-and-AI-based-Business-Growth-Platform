"""Root conftest implementing ADR 0003 D12 skip prevention policy.

ADR 0003 D12: "no test may skip because a dependency is unconfigured; a skipped
test is an integration failure."

Any skipped test during test execution causes pytest to exit with failure status (1).
"""

from __future__ import annotations

import pytest


def pytest_sessionfinish(
    session: pytest.Session,
    exitstatus: int | pytest.ExitCode,  # noqa: ARG001
) -> None:
    """Fail the test session if any tests were skipped."""
    terminalreporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if terminalreporter is None:
        return

    skipped_reports = terminalreporter.stats.get("skipped", [])
    if skipped_reports:
        # ADR 0003 D12 enforcement: treat any skip as a test failure
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter,
    exitstatus: int | pytest.ExitCode,  # noqa: ARG001
    config: pytest.Config,  # noqa: ARG001
) -> None:
    """Report detailed reasons for any skipped tests."""
    skipped_reports = terminalreporter.stats.get("skipped", [])
    if skipped_reports:
        terminalreporter.section(
            "FORBIDDEN TEST SKIPS DETECTED (ADR 0003 D12)", red=True, bold=True
        )
        terminalreporter.write_line(
            f"ERROR: {len(skipped_reports)} test(s) skipped. "
            "Skipping tests due to missing database, unbootstrapped roles, "
            "or missing configuration is prohibited in CI.\n"
        )
        for rep in skipped_reports:
            nodeid = getattr(rep, "nodeid", "unknown")
            longrepr = str(getattr(rep, "longrepr", "No reason provided"))
            terminalreporter.write_line(f"  - {nodeid}: {longrepr}")
        terminalreporter.write_line(
            "\nRemediation: Configure required database, roles, or environment variables.\n"
        )
