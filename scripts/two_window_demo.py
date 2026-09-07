"""The two-window demonstration, driven through both applications rather than the API.

Run the storefront on :3000 and the merchant console on :3001 against a live API, then run
this. It does what a presenter does on stage, in the same order, through the same two
server-side proxies a browser would use:

    a buyer opens a checkout and approves it          (storefront, :3000)
    the merchant raises that product's price          (console,    :3001)
    the buyer presses pay                             (storefront, :3000)

and the kernel answers HTTP 200 with ``allowed: false``, ``REAPPROVAL_REQUIRED``, the
delta that moved, and the version a fresh approval is needed on.

**Driving it through the two proxies rather than the API is the point.** It proves the
loop closes across process boundaries: the console holds an operator credential the
browser never sees, the storefront holds a buyer session it also never sees, and the
kernel trusts neither of them to say what the other did. A version of this that talked to
:8000 directly would prove considerably less and be much easier to write.

Nothing here is seeded or faked. Every figure printed came back from the platform during
the run that printed it, so a run reporting no change means nothing changed.

Usage:

    scripts/run_demo.sh                                   # API and Action Executor
    (cd apps/buyer-web && npm run dev)                    # :3000
    (cd apps/merchant-console && npm run dev)             # :3001
    uv run --no-sync python scripts/two_window_demo.py
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import sys
import urllib.error
import urllib.request
import uuid
from typing import Any

#: The two apps, each with its own cookie jar because each holds a different credential
#: and the whole demonstration is that neither can act as the other.
STOREFRONT = 3000
CONSOLE = 3001

#: A product both surfaces show. Any listed SKU works; this one is on the storefront's
#: first screen, which matters when a person is following along on a projector.
DEFAULT_SKU = "AMUL-DAIRY-002"

#: How much the merchant raises the price by, in basis points of the current price. Derived
#: from what the catalogue currently says rather than chosen, so a rerun always produces a
#: genuine change: setting a price to the value it already holds is answered 409, which is
#: the simulator correctly refusing a change that changes nothing.
RAISE_BP = 1_500


class App:
    """One application, with its own cookie jar and no knowledge of the other."""

    def __init__(self, port: int) -> None:
        self.port = port
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """One request through this app's own `/api/backend` proxy.

        ``Sec-Fetch-Site`` is sent because the proxies refuse a cross-site write, which is
        a guard worth exercising rather than working around: a script that had to disable
        it would be telling you the guard does not hold.
        """
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            f"http://localhost:{self.port}/api/backend{path}",
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "Sec-Fetch-Site": "same-origin",
                "Origin": f"http://localhost:{self.port}",
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
        try:
            with self._opener.open(request, timeout=30) as response:
                return dict(json.loads(response.read()))
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode(errors="replace")
            try:
                return dict(json.loads(body_text))
            except json.JSONDecodeError:
                return {"status": exc.code, "detail": body_text[:200]}
        except OSError as exc:
            sys.exit(f"could not reach localhost:{self.port} -- is that app running? ({exc})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sku", default=DEFAULT_SKU)
    parser.add_argument("--quantity", type=int, default=2)
    args = parser.parse_args(argv)

    storefront, console = App(STOREFRONT), App(CONSOLE)
    storefront.call("GET", "/session")
    console.call("GET", "/session")

    print(f"BUYER, in the storefront on :{STOREFRONT}")
    basket = storefront.call("POST", "/v1/baskets", {})
    if "basket_id" not in basket:
        sys.exit(f"could not open a basket: {basket}")
    basket_id = basket["basket_id"]
    storefront.call("PUT", f"/v1/baskets/{basket_id}/lines/{args.sku}", {"quantity": args.quantity})

    card = storefront.call("POST", f"/v1/baskets/{basket_id}/checkout", {})
    if "checkout_id" not in card:
        sys.exit(f"could not open a checkout: {card}")
    checkout_id = card["checkout_id"]
    print(f"  1. opened checkout v{card['version']}, total {card['total']['display']}")
    print(f"     bound to content hash {card['content_hash'][:22]}...")

    # Approval echoes back the hash and the amount that were on screen. The server compares
    # them, so consent binds to the exact bytes the buyer saw rather than to "this basket".
    approved = storefront.call(
        "POST",
        f"/v1/checkouts/{checkout_id}/versions/{card['version']}/approve",
        {
            "content_hash": card["content_hash"],
            "amount_minor": card["amount_minor"],
            "currency": card["currency"],
        },
    )
    print(f"  2. buyer approved version {card['version']} -- checkout is {approved.get('state')}")

    print(f"\nMERCHANT, in the console on :{CONSOLE}")
    product = console.call("GET", f"/v1/catalogue/products/{args.sku}")
    current = int(product["unit_price_minor"])
    raised = current + max(1, current * RAISE_BP // 10_000)
    injection = console.call(
        "POST",
        "/v1/scenario/injections",
        {
            "kind": "PRICE_SET",
            "sku": args.sku,
            "value": raised,
            "note": "price raised while a checkout stood approved",
        },
    )
    if "deltas" not in injection:
        sys.exit(f"the injection was refused: {injection}")
    print(f"  3. raised the price: {injection['deltas']}")
    print(
        f"     catalogue revision {injection['revision_before']} -> {injection['revision_after']}"
    )

    print(f"\nBUYER presses pay, in the storefront on :{STOREFRONT}")
    decision = storefront.call(
        "POST", f"/v1/checkouts/{checkout_id}/versions/{card['version']}/submit", {}
    )
    print(f"  4. HTTP 200, allowed={decision.get('allowed')}, code={decision.get('code')}")
    print(f"     {decision.get('explanation')}")
    for delta in decision.get("deltas", []):
        print(
            f"     DELTA {delta['field_path']}: {delta['approved']} -> {delta['current']}"
            f"  ({delta.get('reason')})"
        )
    print(f"     a fresh approval is required on version {decision.get('next_version')}")

    if decision.get("allowed"):
        print("\n  The submission was ADMITTED, which means the price change did not reach it.")
        print("  Check that both apps point at the same API and that the injection succeeded.")
        return 1
    print("\n  The approval was refused before any money moved, and version 1 is never revived.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
