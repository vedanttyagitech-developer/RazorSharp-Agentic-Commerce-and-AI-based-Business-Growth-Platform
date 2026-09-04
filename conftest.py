"""Repository-wide pytest policy: a database test may never quietly skip in CI.

ADR 0003 D12 says CI "applies migrations and bootstraps roles, and fails if any `db`
test is skipped". That rule exists because the evidence table in docs/STATUS.md claims
row-level security and single-winner admission are proven, and those proofs live entirely
in database-backed suites. If the roles are not bootstrapped, those suites skip, and CI
reports green while proving none of it.

The rule is deliberately narrow on two axes, because a broader one costs more than it
buys:

- Only ``db``-marked tests. A ``razorpay_live`` or ``slow`` test that skips because an
  optional credential is absent is working as designed; failing the run for it would
  punish the correct behaviour.
- Only where the database is supposed to exist: CI, or a developer who opts in with
  REQUIRE_DB=1. A laptop with no PostgreSQL should still be able to run the pure suites
  in commerce-domain, payment-adapters and merchant-sim and get a truthful green.

The exit status is escalated, never overwritten: an interrupted or internally-errored run
keeps its own more serious code.
"""

from __future__ import annotations

import os

import pytest

#: Only these skips are treated as failures. See the module docstring.
ENFORCED_MARKER = "db"


def _enforcing() -> bool:
    """CI sets CI=true; a developer opts in with REQUIRE_DB=1."""
    return bool(os.environ.get("CI") or os.environ.get("REQUIRE_DB"))


def _offending(terminalreporter: pytest.TerminalReporter) -> list[object]:
    return [
        rep
        for rep in terminalreporter.stats.get("skipped", [])
        if ENFORCED_MARKER in getattr(rep, "keywords", {})
    ]


def pytest_sessionfinish(session: pytest.Session, exitstatus: int | pytest.ExitCode) -> None:
    """Fail the session when a database suite skipped somewhere it should not have."""
    if not _enforcing():
        return
    terminalreporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if terminalreporter is None or not _offending(terminalreporter):
        return
    # Escalate only from a clean run: INTERRUPTED and INTERNAL_ERROR say more than this
    # does, and overwriting them would hide why the run actually stopped.
    if exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter,
    exitstatus: int | pytest.ExitCode,  # noqa: ARG001
    config: pytest.Config,  # noqa: ARG001
) -> None:
    """Name the suites that skipped, so a red run says which dependency was missing."""
    offending = _offending(terminalreporter)
    if not offending:
        return
    enforced = _enforcing()
    terminalreporter.section(
        "database suites skipped" + ("" if enforced else " (not enforced here)"),
        red=enforced,
        bold=True,
    )
    for rep in offending:
        nodeid = getattr(rep, "nodeid", "unknown")
        terminalreporter.write_line(f"  {nodeid}: {getattr(rep, 'longrepr', 'no reason given')}")
    terminalreporter.write_line(
        "\nThese prove row-level security and single-winner admission, so CI treats a skip "
        "as a failure (ADR 0003 D12). Run scripts/setup_ci_db.py to bootstrap the database "
        "and roles; set REQUIRE_DB=1 to enforce this locally.\n"
    )
