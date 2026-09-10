"""Backend routers retain their operator-key dependencies."""

from __future__ import annotations

from commerce_api.deps import require_scenario_key
from commerce_api.routers import ROUTERS
from fastapi import APIRouter


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


def test_the_api_has_routers_behind_the_operator_key() -> None:
    """The API keeps operator-only routes even without the retired console."""
    assert len(_gated_prefixes()) >= 3, _gated_prefixes()
