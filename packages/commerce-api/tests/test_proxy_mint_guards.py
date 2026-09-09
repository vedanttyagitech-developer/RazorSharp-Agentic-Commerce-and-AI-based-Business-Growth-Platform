"""Regression: no browser-facing proxy will forward the session-mint route.

Found by the security review of 2026-09-05. A Next.js credential proxy mints a session
server-side and attaches its bearer token upstream, so the browser never holds one.
``POST /v1/demo/sessions`` answers ``201`` with a *raw bearer token in a readable JSON
body*, and it honours a caller-supplied ``buyer_ref``. The console proxy refused to proxy
``/v1/demo/*`` for a page; the buyer proxy did not, so a same-origin script could read a
working token bound to an identity of its choosing -- exactly the property the proxy exists
to guarantee, undone.

**This test went to sleep, and that is the more useful half of its history.** It named two
proxy files by path and skipped when they were absent. Both applications were deleted on
2026-09-09 and a new one was written with a differently-shaped proxy at a different path,
so the test skipped through the entire rewrite: green, silent, and guarding nothing. A
guard that reports "not applicable" when its subject moves is worse than no guard, because
the suite goes on saying the property is held.

So it discovers proxies instead of naming them, and it distinguishes two absences that used
to look alike: **no front end at all** is a skip, and **a front end with no proxy this test
recognises** is a failure. The second is what a rewrite produces.

It also holds the property rather than one implementation of it. The original proxy refused
a ``v1/demo`` prefix before forwarding; the current one carries a table of permitted paths
and forwards nothing outside it. Both are correct, and what is actually being asserted is
neither of those: it is that **nothing the proxy will forward reaches the mint route.**

The Python suite cannot drive the Next runtime, so this remains a source-level check. It is
a tripwire on a property that is otherwise only reviewed by eye.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
APPS: Final[Path] = REPO_ROOT / "apps"

#: A browser-facing proxy: a Next route handler under an `api/` segment that forwards to
#: the commerce API. Discovered rather than named, so a rewrite cannot move out from under
#: this test without either being found or failing it.
_ROUTE_GLOB: Final[str] = "*/app/api/**/route.ts"

#: How a handler reaches the backend. A file with none of these forwards nothing and is
#: not a proxy.
_FORWARDS: Final[tuple[str, ...]] = ("${API_BASE}", "${base}/v1/", "COMMERCE_API_URL")

#: The route that must never be reachable through a proxy, in the forms a path may take.
_MINT: Final[tuple[str, ...]] = ("demo/sessions", "v1/demo")


def _proxies() -> list[Path]:
    return [
        path
        for path in sorted(APPS.glob(_ROUTE_GLOB))
        if "node_modules" not in path.parts
        and any(marker in path.read_text(encoding="utf-8") for marker in _FORWARDS)
    ]


def _forwardable_paths(source: str) -> list[str]:
    """Every path pattern this proxy is willing to forward, as written in its allowlist.

    Read from the `pattern:` entries of the table rather than inferred, because the table
    *is* the security boundary in the current shape: a path absent from it is answered 404
    before anything is sent upstream.
    """
    literal = re.findall(r"pattern:\s*/\^?(.*?)\$?/[gimsuy]*\s*,", source)
    templated = re.findall(r"pattern:\s*new RegExp\(`\^?(.*?)\$?`\)", source)
    # `\/` is how a slash is written inside a regex literal; the path it means has a plain
    # one, and comparing the escaped form would never match anything this test looks for.
    # Getting that wrong is not hypothetical: the first version of this function stopped at
    # the first slash, so every multi-segment path came back truncated, and the test passed
    # with `demo/sessions` sitting in the allowlist.
    return [entry.replace("\\/", "/") for entry in literal + templated]


def test_the_front_end_has_a_proxy_this_test_recognises() -> None:
    """The absence that must not be quiet.

    No `apps/` at all is a skip: the front end was deleted on 2026-09-09 and rebuilding it
    is ongoing work. An `apps/` that exists with no recognisable proxy is a failure, because
    that is what a rewrite looks like from here -- and it is precisely how this file spent a
    day reporting success while watching nothing.
    """
    if not APPS.is_dir():
        pytest.skip("no apps/ directory: the front end is absent, not unguarded")
    found = _proxies()
    assert found, (
        f"apps/ exists but no proxy matching {_ROUTE_GLOB} forwards to the commerce API. "
        "Either the front end no longer proxies (in which case delete this file and say why "
        "in the commit), or it moved and this guard has stopped watching it."
    )


def test_no_proxy_will_forward_the_session_mint_route() -> None:
    if not APPS.is_dir():
        pytest.skip("no apps/ directory: the front end is absent, not unguarded")
    for path in _proxies():
        source = path.read_text(encoding="utf-8")
        allowed = _forwardable_paths(source)
        if allowed:
            # Allowlist shape: the boundary is the table, so the assertion is about it.
            offending = [entry for entry in allowed if any(m in entry for m in _MINT)]
            assert not offending, (
                f"{path.relative_to(REPO_ROOT)} permits {offending}, which reaches "
                "POST /v1/demo/sessions -- a page could proxy it and read a raw bearer "
                "token bound to an identity it chose."
            )
            continue
        # Refusal shape: an explicit guard on the prefix, before anything is forwarded.
        assert any(marker in source for marker in _MINT), (
            f"{path.relative_to(REPO_ROOT)} has neither an allowlist nor a v1/demo refusal, "
            "so the mint route is reachable from a page."
        )


def test_a_proxy_still_mints_server_side() -> None:
    """The other half of the same property, and the reason the route is refused at all.

    Refusing to proxy the mint route is only safe because the proxy mints one itself, on
    the server, and keeps the token out of the page. A proxy that refused the route and
    minted nothing would pass the test above and leave the browser unable to do anything --
    which is the failure this pins, having watched exactly that happen for real today when a
    stale cookie could not be replaced.
    """
    if not APPS.is_dir():
        pytest.skip("no apps/ directory: the front end is absent, not unguarded")
    for path in _proxies():
        source = path.read_text(encoding="utf-8")
        assert "demo/sessions" in source, (
            f"{path.relative_to(REPO_ROOT)} never mints a session server-side, so either the "
            "browser holds a token or it has none at all."
        )
        assert "HttpOnly" in source, (
            f"{path.relative_to(REPO_ROOT)} sets no HttpOnly cookie: a token a page can read "
            "is a token the page holds."
        )
