"""Regression: neither browser-facing proxy forwards the session-mint route.

Found by the security review of 2026-09-05. Both Next.js credential proxies
(``apps/*/src/app/api/backend/[...path]/route.ts``) mint a session server-side and attach
its bearer token upstream so the browser never holds one. ``POST /v1/demo/sessions``,
however, answers ``201`` with a *raw bearer token in a readable JSON body*, and it honours
a caller-supplied ``buyer_ref``. The console proxy already refused to proxy ``/v1/demo/*``
for a page; the buyer (storefront) proxy did not, so a same-origin script could read a
working token bound to an identity it chose -- exactly the "browser never sees a token"
property the proxy is built to guarantee, undone.

This test cannot drive the Next runtime from the Python suite (and the frontend test
directories belong to other build units), so it holds the invariant at the source: each
proxy's request handler must refuse a ``v1/demo`` path *before* it forwards anything
upstream. It fails if either guard is removed or moved after the forward.

The proxies are optional in some checkouts of this repo; the test skips (never silently
passes) when a file is absent, and says which.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]

PROXY_ROUTES: Final[tuple[tuple[str, Path], ...]] = (
    ("buyer-web", REPO_ROOT / "apps/buyer-web/src/app/api/backend/[...path]/route.ts"),
    (
        "merchant-console",
        REPO_ROOT / "apps/merchant-console/src/app/api/backend/[...path]/route.ts",
    ),
)

#: The upstream call in the browser-facing handler. Everything the browser asks for is
#: joined onto the API base here; the demo guard must run before it.
_FORWARD_MARKER: Final[str] = "${API_BASE}/${suffix}"

#: The guard the demo path must hit: a refusal keyed on the ``v1/demo/`` prefix.
_GUARD_MARKER: Final[str] = 'startsWith("v1/demo/")'


@pytest.mark.parametrize("name, path", PROXY_ROUTES, ids=[n for n, _ in PROXY_ROUTES])
def test_proxy_refuses_the_mint_route_before_forwarding(name: str, path: Path) -> None:
    if not path.exists():
        pytest.skip(f"{name} proxy not present at {path}")
    source = path.read_text(encoding="utf-8")

    assert _GUARD_MARKER in source, (
        f"{name} proxy has no v1/demo guard: a page could proxy POST /v1/demo/sessions and "
        f"read a raw bearer token out of the response body"
    )
    assert _FORWARD_MARKER in source, (
        f"{name} proxy forward marker moved; update this regression test to match"
    )

    guard_at = source.index(_GUARD_MARKER)
    forward_at = source.index(_FORWARD_MARKER)
    assert guard_at < forward_at, (
        f"{name} proxy's v1/demo guard runs after the upstream forward, so the mint route "
        f"is still reachable from a page"
    )

    # The guard must actually refuse: a 404 (the route "does not exist" from the browser's
    # side) appears between the guard and the forward, not merely a comment about one.
    window = source[guard_at:forward_at]
    assert "return" in window and "404" in window, (
        f"{name} proxy matches the v1/demo prefix but does not return a refusal for it"
    )
