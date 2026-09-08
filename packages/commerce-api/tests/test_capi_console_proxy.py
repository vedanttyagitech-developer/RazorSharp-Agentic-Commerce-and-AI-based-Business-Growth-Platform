"""The console's proxy must know about every route that needs the operator key.

The merchant console never lets the browser hold a credential. Its own route handler
attaches the bearer and the ``X-Scenario-Key`` server-side, and it attaches the key only to
the paths in an allowlist -- ``SCENARIO_KEY_PATHS`` in
``apps/merchant-console/src/app/api/backend/[...path]/route.ts``.

That list has now fallen behind the API twice. Each time the symptom was the same and the
diagnosis was expensive: a screen that had just been written rendered "could not be read",
the API answered 401 to every request from it, and the API was working perfectly. A 401 on
a live backend reads as a broken backend. Nobody looks at the proxy first.

So the drift gets a test, and it lives here rather than in the console because only this
side knows the answer. The API's router objects carry their own ``require_scenario_key``
dependency; the console's list is a literal array in a TypeScript file. This walks the
first and greps the second.

Reading a sibling app's source from a Python test is unusual, and the alternative is worse:
the console cannot import FastAPI routers, and a runtime check would only fail once
somebody opened the page. This fails in the suite, on the commit that adds the router.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest
from commerce_api.deps import require_scenario_key
from commerce_api.routers import ROUTERS
from fastapi import APIRouter

#: The console's proxy, relative to this test. Absent in a checkout that ships only the
#: Python packages, which is why the tests below skip rather than fail when it is missing:
#: a missing sibling app is a different fact from a stale list, and conflating them would
#: make this test fail for a reason it cannot fix.
PROXY: Final[Path] = (
    Path(__file__).resolve().parents[3]
    / "apps"
    / "merchant-console"
    / "src"
    / "app"
    / "api"
    / "backend"
    / "[...path]"
    / "route.ts"
)

#: Prefixes the console deliberately does not read, so their absence is not drift.
#:
#: ``/v1/scenario`` is the injection surface: the console has its own controls for it and
#: they were removed with the old screens, so the entry that is there today is ahead of the
#: list rather than behind it. Nothing else is exempt, and adding to this set is a decision
#: about what the console is for, not a way to quiet a failing test.
NOT_READ_BY_THE_CONSOLE: Final[frozenset[str]] = frozenset()


def _gated_prefixes() -> list[str]:
    """Every router prefix whose whole surface requires the scenario key.

    Read from the dependency objects rather than from a list of names, so a router that
    gains the gate later is covered without anybody remembering this file exists.
    """
    gated: list[str] = []
    for router in ROUTERS:
        assert isinstance(router, APIRouter)
        for dependency in router.dependencies:
            if getattr(dependency, "dependency", None) is require_scenario_key:
                gated.append(router.prefix)
                break
    return gated


def _allowlist() -> list[str]:
    """The array's entries, with comment lines dropped before the strings are read.

    The comments in that block quote error messages, and a naive string scan takes
    ``"could not be read"`` for a path. Stripping the ``//`` lines first is not fussiness:
    a false entry here makes the drift test below fail for a reason that has nothing to do
    with drift, which is exactly how a guard gets deleted.
    """
    source = PROXY.read_text()
    block = re.search(r"const SCENARIO_KEY_PATHS = \[(.*?)\];", source, re.S)
    assert block, "SCENARIO_KEY_PATHS is not an array literal any more; this test is stale"
    code = "\n".join(
        line for line in block.group(1).splitlines() if not line.strip().startswith("//")
    )
    return re.findall(r'"([^"]+)"', code)


def _covers(entry: str, prefix: str) -> bool:
    """Whether one allowlist entry attaches the key to this router, as the proxy decides.

    The proxy compares a leading-slash-stripped request path against each entry with
    equality or ``startsWith``. This reproduces that rather than approximating it, because a
    guard that models the thing it guards only roughly is one that fails on the wrong days.
    """
    stripped = prefix.strip("/")
    return stripped == entry.strip("/") or stripped.startswith(entry.lstrip("/"))


def test_the_api_has_routers_behind_the_operator_key() -> None:
    """Guards the test below against passing because it found nothing to check."""
    assert len(_gated_prefixes()) >= 3, _gated_prefixes()


@pytest.mark.skipif(not PROXY.exists(), reason="the merchant console is not in this checkout")
def test_every_key_gated_router_is_in_the_console_allowlist() -> None:
    allowed = _allowlist()
    missing = [
        prefix
        for prefix in _gated_prefixes()
        if prefix not in NOT_READ_BY_THE_CONSOLE
        # Matched the way the proxy matches: its entries are prefixes, tested with
        # `startsWith`, so `v1/ops/` covers every route under it. Comparing for equality
        # instead reports a router as missing while the proxy is in fact attaching the key
        # to it -- a false alarm, which is the failure mode that gets a guard deleted.
        and not any(_covers(entry, prefix) for entry in allowed)
    ]
    assert missing == [], (
        "these routers require X-Scenario-Key but the merchant console's proxy does not "
        "attach it, so every request from the console to them answers 401 while the API is "
        f"working: {missing}. Add them to SCENARIO_KEY_PATHS in {PROXY.name}."
    )


@pytest.mark.skipif(not PROXY.exists(), reason="the merchant console is not in this checkout")
def test_the_allowlist_names_no_prefix_the_api_does_not_serve() -> None:
    """Drift in the other direction: an entry for a route that no longer exists.

    Harmless at run time -- a key attached to a path that ignores it changes nothing -- but
    it is a claim in a file that somebody will read as documentation of the API's shape, and
    a stale entry makes the list look maintained when it is not.

    Widening prefixes are allowed to name routes the key merely *widens* rather than gates,
    so this checks the entry matches some real path rather than a gated router.
    """
    served = {route.path for router in ROUTERS for route in router.routes}  # type: ignore[attr-defined]
    orphans = [
        entry
        for entry in _allowlist()
        if not any(path.lstrip("/").startswith(entry.strip("/")) for path in served)
    ]
    assert orphans == [], f"the console attaches the key to paths the API does not serve: {orphans}"
