#!/usr/bin/env python3
"""Drive the demonstration tenant into a state worth showing, through the real paths.

``seed_demo_tenant.py`` creates the tenant and the merchant and stops, which is correct:
everything past that point is *money state*, and money state has exactly one legitimate
origin. So this script owns no INSERT of its own. It mints a session, builds baskets,
opens checkouts, approves exact versions, submits them for kernel admission and applies
provider capture evidence -- the same sequence a buyer, an agent and the durable worker
perform between them. What lands in ``orders`` and ``refunds`` lands there because the
kernel put it there.

**No ``orders`` or ``refunds`` row is ever written directly.** That rule is the whole
value of the exercise. ``/evidence`` exists to be checked: it reads the invalidated
version's approved total, the corrected version's total and the captured amount from the
``orders`` row the kernel wrote from verified capture evidence, and it recomputes the
arithmetic in front of the viewer. A seeder that wrote those rows itself would produce a
database that renders correctly and proves nothing, and a panel that asked "how do you
know the capture was real" would be owed an apology rather than an answer.

What it leaves behind
---------------------

* **Confirmed orders across a range of amounts**, each with real capture evidence, so
  ``/operations`` lists them and ``/evidence`` has captured revenue to account for.
* **One refused approval that was then re-approved and paid**: version 1 approved, a
  ``PRICE_SET`` injection, version 1 submitted and refused ``REAPPROVAL_REQUIRED``,
  version 2 approved and admitted. ``/evidence`` reads retained revenue from exactly this
  shape and has nothing to compute without one.
* **Refunds in three genuinely different states.** ``REFUND_PENDING`` (admitted, the
  provider has not been asked), ``REFUND_UNKNOWN`` (asked, the answer was lost, only
  reconciliation may say) and ``REFUND_FAILED`` (the provider said no). Conflating the
  first two is how a buyer gets refunded twice, so the console has to be able to show
  that it does not.
* **A dead outbox command**, so the operations tab's revive control has a subject.
* **A cancelled and a rejected checkout**, so the state vocabulary is visible.

The three seams, named
----------------------

Three things happen here that the platform does not do to itself. Each is a laptop
standing in for a world the laptop does not have, each is marked ``SEEDING SEAM`` at the
call site, and there is deliberately no fourth.

1. **Capture evidence is applied by this script, not by a webhook.** Razorpay posts
   webhooks to a public URL and a laptop has none (``docs/DEMO.md`` troubleshooting 3),
   so the delivery never arrives. The application itself is not simulated: it is
   :func:`transaction_kernel.apply_provider_evidence` with
   :attr:`~transaction_kernel.EvidenceSource.WEBHOOK`, byte for byte the call
   ``action_executor.handlers.apply_webhook`` makes once a delivery has verified. The
   monotonic apply, the ``orders`` insert and the audit row are the kernel's.

2. **One Execution Grant is expired early.** A grant lives 300 seconds (ADR 0003 D13) and
   a seeder cannot wait five minutes to produce one dead letter, so its ``expires_at`` is
   moved into the past -- the same trick the scenario controller already offers for
   reservations. Everything after that is the worker's own judgement: it finds the grant
   unusable, refuses ``AUTHORITY_INSUFFICIENT``, and the outbox buries the command. That
   refusal is correct and is not engineered around.

3. **Two queued commands are held.** Their ``available_at`` is pushed past the demo so the
   worker does not run them yet. This is what keeps one refund honestly ``REFUND_PENDING``
   -- nothing has been sent, which is what the row says -- and stops the ``REFUND_UNKNOWN``
   row's bounded reconciliation (six rounds, then ``ESCALATED``) from chasing a payment
   that only ever existed as evidence and escalating a state the demo needs to show.

Idempotence
-----------

The script is a convergence, not a recipe. It surveys the tenant first, creates only what
is missing, and prints found-versus-created for every line. Running it twice writes
nothing the second time. ``--reset`` clears the tenant's rows and rebuilds from empty;
because no platform role holds DELETE on any table -- deliberately, so nothing in the
running system can erase a financial row -- the reset needs an administrative connection
that the API and the worker do not have.

Usage::

    export PATH="$HOME/.local/bin:$PATH"
    uv run --no-sync python scripts/seed_demo_state.py
    uv run --no-sync python scripts/seed_demo_state.py --reset
    uv run --no-sync python scripts/seed_demo_state.py --orders 8 --no-catalogue-reset

The stack must be up: ``make demo`` (the API on :8000 and the Action Executor beside it).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlparse, urlunparse

import httpx
from sqlalchemy import text

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from sqlalchemy.orm import Session

# --------------------------------------------------------------------------- defaults

#: The demonstration database, never the test database, and the same default as
#: ``seed_demo_tenant.py``. ``DATABASE_URL_KERNEL`` is not consulted for the same reason
#: it is not consulted there: in this repository it names ``commerce_test``.
#: Read from ``DEMO_DATABASE_URL``; the fallback names the role and the database and carries
#: no password, so a password never lives in this file. Set the variable to the kernel
#: role's URL for the development database before running this.
DEFAULT_DATABASE_URL: Final = os.environ.get(
    "DEMO_DATABASE_URL", "postgresql+psycopg://commerce_dev_kernel@localhost:5432/commerce_dev"
)

#: ``--reset`` only. No platform role is granted DELETE on any table, which is why the
#: reset cannot run as one of them: an operator's identity is required to undo what the
#: platform is not allowed to undo.
DEFAULT_ADMIN_DATABASE_URL: Final = "postgresql+psycopg://localhost:5432/commerce_dev"

DEFAULT_API_BASE: Final = "http://127.0.0.1:8000"
DEFAULT_SCENARIO_KEY: Final = "local-demo-scenario-key"
DEFAULT_TENANT_SLUG: Final = "demo"
DEFAULT_MERCHANT_SLUG: Final = "demo-grocery"

#: How many orders the tenant should end up holding. Five is the smallest useful number:
#: three carry the three refund states, because one payment attempt cannot hold three
#: refunds in three different states; one is a clean sale, so the list is not uniformly
#: "something went wrong here"; and one is the refused-then-re-approved sale below.
DEFAULT_ORDERS: Final = 5

#: How many of those orders should be the refused-then-re-approved shape `/evidence`
#: accounts for. One is enough to make the headline screen work.
DEFAULT_REFUSALS: Final = 1

#: Seconds to wait for the worker to reach a state before giving up and saying so. The
#: worker polls once a second; a create-order round trip to Razorpay test mode is well
#: under a second on a working connection.
DEFAULT_TIMEOUT: Final = 45.0

#: How far a held command's ``available_at`` is pushed. Long enough to outlast any
#: demonstration, short enough that a forgotten seed eventually runs rather than sitting
#: in the outbox for ever.
HOLD_DAYS: Final = 7

# ------------------------------------------------------------------------- the baskets
#
# Amounts assume the fixture baseline (milk Rs 28.00, rice Rs 499.00, atta Rs 255.00,
# free delivery at Rs 499.00 of items), which is what `--catalogue-reset` restores before
# any of this runs. They are listed as a reader's aid; nothing below depends on them --
# every amount is read back from the approval card the API actually returned.

Basket = tuple[tuple[str, int], ...]

#: Baskets spanning roughly Rs 85 to Rs 1,048, so the orders list shows a spread rather
#: than several copies of one number. The first is below the free-delivery threshold and
#: the rest are above it, which puts both fee outcomes on screen.
#:
#: They also spread across SKUs on purpose. Opening a checkout takes a five-minute
#: inventory hold, and a tenant several people have been clicking through can easily have
#: forty live holds on the popular item: the merchant then refuses a further hold
#: ``CONCURRENT_OPERATION``, which is the oversell guard working. :func:`open_checkout`
#: walks this list when that happens, so a congested SKU costs a different basket rather
#: than an abandoned run.
ORDER_BASKETS: Final[tuple[Basket, ...]] = (
    (("AMUL-DAIRY-001", 2),),  # ~Rs 85.50, delivery fee charged
    (("AASH-STPL-002", 1),),  # ~Rs 297.25, delivery fee charged
    (("INDI-STPL-001", 1),),  # ~Rs 523.95, free delivery
    (("TOOR-STPL-004", 2), ("TATA-STPL-003", 1)),
    (("AASH-STPL-002", 2),),  # ~Rs 560.50, free delivery
    (("MADH-STPL-006", 2),),
    (("COLD-STPL-007", 3),),
    (("RAJM-STPL-009", 2), ("POHA-STPL-010", 2)),
    (("NEST-DAIRY-006", 2),),
    (("CHAN-STPL-005", 3),),
    (("FORT-STPL-008", 2),),
    (("AMUL-DAIRY-003", 2), ("ONIO-PROD-001", 2)),
)

#: The runbook's own basket first: 2 x milk + 1 x rice = Rs 579.95, which becomes Rs 681.95
#: once the milk is injected to Rs 79.00, and steps 2 to 8 of ``docs/DEMO.md`` are exactly
#: that. The rest keep the shape -- two units of a cheap line whose price is raised, plus
#: one expensive line -- so a fallback still produces a delta worth pointing at.
REFUSAL_BASKETS: Final[tuple[Basket, ...]] = (
    (("AMUL-DAIRY-001", 2), ("INDI-STPL-001", 1)),
    (("TATA-STPL-003", 2), ("AASH-STPL-002", 1)),
    (("AMUL-DAIRY-003", 2), ("TOOR-STPL-004", 1)),
    (("POHA-STPL-010", 2), ("FORT-STPL-008", 1)),
)

#: The price step 5 injects, and the reason the retained-revenue arithmetic reads
#: 2 x (Rs 79.00 - Rs 28.00) = Rs 102.00. A concurrent session may have set it already, in
#: which case the injection is refused 409 and a nearby value is used instead; the figures
#: are read from the response either way.
REFUSAL_PRICE_MINOR: Final = 7900

CANCELLED_BASKETS: Final[tuple[Basket, ...]] = ((("TATA-STPL-003", 1),), *ORDER_BASKETS)
REJECTED_BASKETS: Final[tuple[Basket, ...]] = ((("ONIO-PROD-001", 3),), *ORDER_BASKETS)
DEAD_LETTER_BASKETS: Final[tuple[Basket, ...]] = ((("COLD-STPL-007", 2),), *ORDER_BASKETS)

#: Refund amounts, in paise, small enough to be a partial refund of any basket above.
#: Keyed by the ``refunds.status`` each one is aiming at, so a reader of the console's
#: refund list can tell the three rows apart by their amounts alone.
REFUND_AMOUNTS: Final[Mapping[str, int]] = {"FAILED": 2500, "UNKNOWN": 1500, "PENDING": 1000}


class SeedError(RuntimeError):
    """The tenant could not be driven into the requested state, and here is why."""


class ApiError(SeedError):
    """An HTTP error from the API, with the ``code`` its problem document carried.

    The code is kept rather than folded into the message because some of these are
    conditions to work around rather than failures to report -- a stock hold that another
    checkout took is the platform refusing to oversell, and the answer is a different
    basket, not an abandoned run.
    """

    def __init__(self, *, method: str, path: str, status: int, code: str, detail: str) -> None:
        super().__init__(f"{method} {path} answered {status} {code}: {detail}")
        self.status = status
        self.code = code


# --------------------------------------------------------------------------- reporting


@dataclass(frozen=True, slots=True)
class Survey:
    """What the tenant already holds. Counted before and again after, from committed rows.

    Every field is a count of something a console surface renders, which is what makes the
    before/after report a claim a reader can check rather than a log of intentions.
    """

    confirmed_orders: int
    refused_then_paid: int
    refunds_pending: int
    refunds_unknown: int
    refunds_failed: int
    dead_commands: int
    cancelled_checkouts: int
    rejected_checkouts: int

    def lines(self) -> tuple[tuple[str, int], ...]:
        return (
            ("orders from capture evidence", self.confirmed_orders),
            ("  of those, refused then re-approved", self.refused_then_paid),
            ("refunds REFUND_PENDING", self.refunds_pending),
            ("refunds REFUND_UNKNOWN", self.refunds_unknown),
            ("refunds REFUND_FAILED", self.refunds_failed),
            ("dead outbox commands", self.dead_commands),
            ("cancelled checkouts", self.cancelled_checkouts),
            ("rejected checkouts", self.rejected_checkouts),
        )


@dataclass(slots=True)
class SeedResult:
    """What the run found, what it made, and what it could not make.

    ``shortfalls`` is not decoration. A seeder that cannot reach a state and says nothing
    hands you a demo that is missing a screen, and you find out in front of the panel.
    """

    tenant_id: uuid.UUID
    tenant_slug: str
    merchant_id: uuid.UUID
    merchant_slug: str
    before: Survey
    after: Survey
    reset: bool
    deleted: Mapping[str, int] = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)
    shortfalls: list[str] = field(default_factory=list)
    #: Checkouts this run drove through refusal and re-approval, oldest first. Kept so the
    #: report can hand out a `/evidence?checkout_id=` link: the page defaults to the
    #: *newest* refused approval in the tenant, which on a tenant several people are
    #: clicking through is whoever refused last, not necessarily this run.
    refusals: list[str] = field(default_factory=list)
    retained_revenue: Mapping[str, Any] | None = None
    retained_revenue_default: Mapping[str, Any] | None = None

    @property
    def created_anything(self) -> bool:
        return bool(self.actions)

    def did(self, line: str) -> None:
        print(f"  + {line}")
        self.actions.append(line)

    def missed(self, line: str) -> None:
        print(f"  ! {line}")
        self.shortfalls.append(line)


# ------------------------------------------------------------------------ url handling


def redacted(url: str) -> str:
    """A connection URL with its password removed, safe to print or log."""
    parsed = urlparse(url)
    if parsed.hostname is None:
        return "<unparseable-url>"
    user = f"{parsed.username}:***@" if parsed.username else ""
    port = f":{parsed.port}" if parsed.port else ""
    return urlunparse(
        parsed._replace(netloc=f"{user}{parsed.hostname}{port}", query="", fragment="")
    )


def database_name(url: str) -> str:
    return urlparse(url).path.lstrip("/") or "<none>"


# ------------------------------------------------------------------------- the HTTP API


class Api:
    """One demo session, and the header discipline every mutation in this repository needs.

    Three rules, and every 401 or 422 during a demonstration is one of them (``docs/DEMO.md``
    section 2): every mutation carries a unique ``Idempotency-Key``; scenario, ops and
    evidence routes carry *both* the bearer token and ``X-Scenario-Key``; and a kernel
    denial is an HTTP 200 with a structured decision, so :meth:`post` must not treat one as
    an error.
    """

    __slots__ = ("_client", "_scenario_key", "_token", "buyer_ref", "merchant_id", "tenant_id")

    def __init__(self, client: httpx.Client, *, scenario_key: str) -> None:
        self._client = client
        self._scenario_key = scenario_key
        self._token = ""
        self.tenant_id = uuid.UUID(int=0)
        self.merchant_id = uuid.UUID(int=0)
        self.buyer_ref = ""

    def mint(self, *, tenant_slug: str, merchant_slug: str) -> None:
        """Open a buyer session. Identity comes from the token from here on, never a body."""
        body = self._json(
            self._client.post(
                "/v1/demo/sessions",
                json={
                    "tenant_slug": tenant_slug,
                    "merchant_slug": merchant_slug,
                    "actor_type": "BUYER",
                },
            )
        )
        self._token = str(body["token"])
        self.tenant_id = uuid.UUID(str(body["tenant_id"]))
        self.merchant_id = uuid.UUID(str(body["merchant_id"]))
        self.buyer_ref = str(body["buyer_ref"])

    # -- transport ----------------------------------------------------------------

    def _headers(self, *, idempotent: bool, scenario: bool) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._token}"}
        if idempotent:
            headers["Idempotency-Key"] = f"seed-{secrets.token_hex(12)}"
        if scenario:
            headers["X-Scenario-Key"] = self._scenario_key
        return headers

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 400:
            problem: Any = {}
            try:
                problem = response.json()
            except ValueError:
                problem = {}
            raise ApiError(
                method=response.request.method,
                path=response.request.url.path,
                status=response.status_code,
                code=str(problem.get("code", "")) if isinstance(problem, dict) else "",
                detail=(
                    str(problem.get("detail", ""))
                    if isinstance(problem, dict) and problem.get("detail")
                    else response.text[:400]
                ),
            )
        parsed: Any = response.json()
        if not isinstance(parsed, dict):
            raise SeedError(f"{response.request.url.path} did not answer with an object")
        return parsed

    def get(self, path: str, *, scenario: bool = False) -> dict[str, Any]:
        return self._json(
            self._client.get(path, headers=self._headers(idempotent=False, scenario=scenario))
        )

    def post(
        self,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        scenario: bool = False,
    ) -> dict[str, Any]:
        return self._json(
            self._client.post(
                path,
                json=dict(body) if body is not None else None,
                headers=self._headers(idempotent=True, scenario=scenario),
            )
        )

    def put(self, path: str, *, body: Mapping[str, Any]) -> dict[str, Any]:
        return self._json(
            self._client.put(
                path, json=dict(body), headers=self._headers(idempotent=True, scenario=False)
            )
        )

    def inject(self, body: Mapping[str, Any]) -> dict[str, Any] | None:
        """Post a scenario injection, answering ``None`` on the documented 409.

        The merchant simulator refuses a change that changes nothing. That is not an error
        to retry with the same value, it is an instruction to pick a different one.
        """
        response = self._client.post(
            "/v1/scenario/injections",
            json=dict(body),
            headers=self._headers(idempotent=False, scenario=True),
        )
        if response.status_code == 409:
            return None
        return self._json(response)


# ---------------------------------------------------------------- kernel-role database


@contextmanager
def kernel_session(tenant_id: uuid.UUID) -> Iterator[Session]:
    """One ``commerce_kernel`` transaction with the tenant bound. Commits on success.

    Imported inside the function, after :func:`main` has set ``DATABASE_URL_KERNEL``, so
    the engine resolves the URL this run was told to use. Without :func:`set_tenant` every
    SELECT below returns nothing and every UPDATE touches nothing -- row-level security
    working, not a bug.
    """
    from platform_db import session_scope, set_tenant

    with session_scope("KERNEL") as session:
        set_tenant(session, tenant_id)
        yield session


def scalar_int(session: Session, sql: str, params: Mapping[str, Any]) -> int:
    return int(session.execute(text(sql), dict(params)).scalar_one())


def survey(tenant_id: uuid.UUID) -> Survey:
    """Count what the tenant holds, from committed rows, as the kernel role.

    Read straight from the tables rather than from the console's own endpoints: the point
    of the before/after report is to be independent of the surfaces it is preparing.
    """
    args = {"t": tenant_id}
    with kernel_session(tenant_id) as session:
        refunds = {
            str(row[0]): int(row[1])
            for row in session.execute(
                text("SELECT status, count(*) FROM refunds WHERE tenant_id = :t GROUP BY status"),
                args,
            ).all()
        }
        return Survey(
            # Every row in `orders` was written by the kernel from verified capture
            # evidence and by nothing else, so the count needs no status predicate: a row
            # that later goes PARTIALLY_REFUNDED is still a sale that happened.
            confirmed_orders=scalar_int(
                session, "SELECT count(*) FROM orders WHERE tenant_id = :t", args
            ),
            # An order whose checkout also carries an approved-then-invalidated version:
            # the refusal that `/evidence` accounts for. Counted through the join rather
            # than by a marker column, because the shape *is* the natural key.
            refused_then_paid=scalar_int(
                session,
                "SELECT count(DISTINCT o.checkout_id) FROM orders o WHERE o.tenant_id = :t "
                "AND EXISTS (SELECT 1 FROM checkout_versions cv JOIN approvals a "
                "  ON a.tenant_id = cv.tenant_id AND a.checkout_id = cv.checkout_id "
                " AND a.checkout_version = cv.version "
                " WHERE cv.tenant_id = o.tenant_id AND cv.checkout_id = o.checkout_id "
                "   AND cv.invalidated_at IS NOT NULL)",
                args,
            ),
            refunds_pending=refunds.get("PENDING", 0),
            refunds_unknown=refunds.get("UNKNOWN", 0),
            refunds_failed=refunds.get("FAILED", 0),
            dead_commands=scalar_int(
                session,
                "SELECT count(*) FROM outbox_events WHERE tenant_id = :t AND status = 'DEAD'",
                args,
            ),
            # Cancel and reject both land the checkout in CANCELLED, so the audit stream is
            # what tells them apart -- which is the honest place to ask, since the
            # difference is which decision was taken and by whom.
            cancelled_checkouts=scalar_int(
                session,
                "SELECT count(DISTINCT aggregate_id) FROM audit_events "
                "WHERE tenant_id = :t AND event_type = 'checkout.cancelled'",
                args,
            ),
            rejected_checkouts=scalar_int(
                session,
                "SELECT count(DISTINCT aggregate_id) FROM audit_events "
                "WHERE tenant_id = :t AND event_type = 'approval.rejected'",
                args,
            ),
        )


def wait_for(check: str, *, tenant_id: uuid.UUID, timeout: float, sql: str, **params: Any) -> bool:
    """Poll one boolean SQL predicate until it holds or the deadline passes.

    Watching rather than assuming. The worker is a separate process reached only through
    PostgreSQL, so "did it happen" is a question about committed rows and nothing else.
    """
    deadline = time.monotonic() + timeout
    args = {"t": tenant_id, **params}
    while True:
        with kernel_session(tenant_id) as session:
            if session.execute(text(sql), args).scalar_one():
                return True
        if time.monotonic() >= deadline:
            print(f"    (timed out after {timeout:.0f}s waiting for {check})")
            return False
        time.sleep(0.5)


# ------------------------------------------------------------------ the buyer journey


#: Refusals from the checkout route that mean "not this basket, right now". Both are the
#: oversell guard: ``CONCURRENT_OPERATION`` says other live holds took the stock,
#: ``STALE_CHECKOUT`` that the item itself cannot cover the request. Neither is a fault to
#: report, and neither is retryable by asking again for the same thing.
NO_HOLD: Final[frozenset[str]] = frozenset({"CONCURRENT_OPERATION", "STALE_CHECKOUT"})


def open_checkout(api: Api, baskets: Sequence[Basket]) -> tuple[dict[str, Any], Basket]:
    """Basket, lines, checkout. Returns version 1's **approval card**, not a checkout.

    Every quantity is re-quoted by the merchant, and the checkout freezes the exact bytes
    that quote produced. Nothing here computes a total; the card's ``amount_minor`` is
    whatever the merchant said, which is the only figure an approval may bind to.

    ``baskets`` is a preference order rather than a single choice. Opening a checkout takes
    a five-minute inventory hold, so a tenant that several people have been clicking
    through accumulates live holds on the popular SKUs and the merchant starts refusing
    further ones. That refusal is the platform declining to oversell, so the answer is a
    different basket -- and the basket actually used is returned, because the caller that
    injects a price change needs to name a line this checkout really contains.
    """
    refusals: list[str] = []
    for basket in baskets:
        basket_id = str(api.post("/v1/carts")["cart_id"])
        for sku, quantity in basket:
            api.put(f"/v1/carts/{basket_id}/lines/{sku}", body={"quantity": quantity})
        try:
            return api.post(f"/v1/carts/{basket_id}/checkout"), basket
        except ApiError as exc:
            if exc.code not in NO_HOLD:
                raise
            refusals.append(f"{'+'.join(sku for sku, _ in basket)} {exc.code}")
            print(f"    (no stock hold for {refusals[-1]}; trying another basket)")
    raise SeedError(
        "every candidate basket was refused an inventory hold "
        f"({'; '.join(refusals)}). Live holds lapse five minutes after the checkout "
        "that took them, or --reset clears this tenant's outright."
    )


def approve(api: Api, card: Mapping[str, Any]) -> dict[str, Any]:
    """Echo the card's hash, amount and currency back. A card that moved is refused."""
    return api.post(
        f"/v1/checkouts/{card['checkout_id']}/versions/{card['version']}/approve",
        body={
            "content_hash": card["content_hash"],
            "amount_minor": card["amount_minor"],
            "currency": card["currency"],
        },
    )


def submit(api: Api, checkout_id: str, version: int) -> dict[str, Any]:
    """Kernel admission. **Always HTTP 200** -- a denial is the system working (ADR D15)."""
    return api.post(f"/v1/checkouts/{checkout_id}/versions/{version}/submit")


@dataclass(frozen=True, slots=True)
class Admitted:
    """An admitted attempt, and the refusals the kernel made on the way to it."""

    checkout_id: str
    version: int
    amount_minor: int
    currency: str
    attempt_id: uuid.UUID
    grant_id: uuid.UUID
    command_id: uuid.UUID
    refusals: tuple[tuple[int, int], ...]


def admit(api: Api, card: Mapping[str, Any], *, rounds: int = 4) -> Admitted:
    """Approve and submit, re-approving whatever version the kernel offers instead.

    A refusal is not a failure to recover from, it is the platform doing its job: another
    session injecting a price into the shared simulator mid-run produces exactly the same
    ``REAPPROVAL_REQUIRED`` the demonstration stages deliberately, and the correct response
    to it is the one an agent would make -- show the buyer the new card and ask again.
    Refusals are recorded so the caller can say how many there were.
    """
    refusals: list[tuple[int, int]] = []
    current: Mapping[str, Any] = card
    for _ in range(rounds):
        approve(api, current)
        decision = submit(api, str(current["checkout_id"]), int(current["version"]))
        if decision["allowed"]:
            return Admitted(
                checkout_id=str(current["checkout_id"]),
                version=int(current["version"]),
                amount_minor=int(current["amount_minor"]),
                currency=str(current["currency"]),
                attempt_id=uuid.UUID(str(decision["payment_attempt_id"])),
                grant_id=uuid.UUID(str(decision["grant_id"])),
                command_id=uuid.UUID(str(decision["command_id"])),
                refusals=tuple(refusals),
            )
        following = decision.get("approval_card")
        if not isinstance(following, dict):
            raise SeedError(
                f"submit refused {decision.get('code')} without offering a next version: "
                f"{json.dumps(decision)[:400]}"
            )
        refusals.append((int(current["version"]), int(current["amount_minor"])))
        current = following
    raise SeedError(f"still refused after {rounds} approve/submit rounds")


def await_provider_order(api: Api, checkout_id: str, *, timeout: float) -> str:
    """Wait for the worker to create the Razorpay test-mode order and record its id.

    The API never calls Razorpay; the worker does, holding the single-use grant. So this
    polls the handoff the browser would poll, and a missing order id means the worker has
    not got there yet rather than that anything is wrong.
    """
    deadline = time.monotonic() + timeout
    while True:
        handoff = api.get(f"/v1/checkouts/{checkout_id}/payment")
        provider_order_id = handoff.get("razorpay_order_id")
        if isinstance(provider_order_id, str) and provider_order_id:
            return provider_order_id
        if time.monotonic() >= deadline:
            raise SeedError(
                f"no Razorpay order on checkout {checkout_id} after {timeout:.0f}s; "
                f"attempt state is {handoff.get('state')}. Is the Action Executor running?"
            )
        time.sleep(0.5)


def apply_capture(admitted: Admitted, *, tenant_id: uuid.UUID, provider_order_id: str) -> uuid.UUID:
    """SEEDING SEAM 1 -- apply capture evidence the laptop will never be sent.

    Razorpay posts webhooks to a public URL; a laptop has none, so ``payment.captured``
    never arrives (``docs/DEMO.md`` troubleshooting 3). What is synthesised is the
    *delivery*. What happens to it is not: this is the identical call
    ``action_executor.handlers.apply_webhook`` makes after a signature has verified, with
    the same ``WEBHOOK`` source, through the same monotonic apply. The ``orders`` row, the
    state transition and the audit entry are the kernel's work, which is why the order that
    results is one ``/evidence`` may honestly account for.

    ``raw_digest`` is taken over the event bytes the evidence stands for, so the digest
    names something real rather than being a constant nobody could recompute.
    """
    import transaction_kernel as tk
    from commerce_domain import uuid7

    payment_id = f"pay_{secrets.token_hex(7)}"
    event_id = f"evt_seed_{secrets.token_hex(8)}"
    delivery = json.dumps(
        {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": payment_id,
                        "order_id": provider_order_id,
                        "amount": admitted.amount_minor,
                        "currency": admitted.currency,
                        "status": "captured",
                    }
                }
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    with kernel_session(tenant_id) as session:
        applied = tk.apply_provider_evidence(
            session,
            tenant_id=tenant_id,
            payment_attempt_id=admitted.attempt_id,
            evidence=tk.ProviderEvidence(
                source=tk.EvidenceSource.WEBHOOK,
                provider_payment_id=payment_id,
                provider_order_id=provider_order_id,
                amount_minor=admitted.amount_minor,
                currency=admitted.currency,
                status="captured",
                provider_status="captured",
                raw_digest=hashlib.sha256(delivery).hexdigest(),
                captured_at=_rfc3339_now(),
                event_id=event_id,
            ),
            correlation_id=uuid7(),
        )
    if applied.order_id is None:
        raise SeedError(
            f"capture evidence on attempt {admitted.attempt_id} produced no order "
            f"({applied.reason}); nothing was written"
        )
    return applied.order_id


def _rfc3339_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class Sale:
    """A confirmed order and the payment attempt underneath it.

    Carried together because a refund is admitted against the attempt, not the order, and
    looking the attempt up again from the order id would be a second answer to a question
    already answered.
    """

    order_id: uuid.UUID
    attempt_id: uuid.UUID
    amount_minor: int
    currency: str


def buy(api: Api, baskets: Sequence[Basket], *, timeout: float) -> Sale:
    """One whole purchase: basket, checkout, approval, admission, capture, order."""
    card, _ = open_checkout(api, baskets)
    admitted = admit(api, card)
    return capture(api, admitted, timeout=timeout)


def capture(api: Api, admitted: Admitted, *, timeout: float) -> Sale:
    """Wait for the worker's Razorpay order, then apply the capture evidence to it."""
    provider_order_id = await_provider_order(api, admitted.checkout_id, timeout=timeout)
    order_id = apply_capture(admitted, tenant_id=api.tenant_id, provider_order_id=provider_order_id)
    return Sale(
        order_id=order_id,
        attempt_id=admitted.attempt_id,
        amount_minor=admitted.amount_minor,
        currency=admitted.currency,
    )


# ------------------------------------------------------------------- the refusal shape


def unit_price_of(card: Mapping[str, Any], sku: str) -> int | None:
    """The unit price the merchant quoted for one SKU on version 1's card."""
    quote = card.get("quote")
    if not isinstance(quote, dict):
        return None
    for line in quote.get("lines", []):
        if isinstance(line, dict) and line.get("sku") == sku:
            return int(line["unit_price_minor"])
    return None


def raise_price(api: Api, sku: str, *, current_minor: int | None) -> dict[str, Any]:
    """Inject a price change, choosing a value the merchant will not refuse as a no-op.

    ``PRICE_SET`` to the value already in force answers 409, and on a shared simulator
    another session may have set the demonstration's own Rs 79.00 a moment ago. So the
    runbook's value is tried first and nearby values after it, and the caller reads the
    actual before/after out of the response rather than assuming either.
    """
    candidates = [REFUSAL_PRICE_MINOR]
    candidates.extend(REFUSAL_PRICE_MINOR + step for step in (37, 113, 251, 499))
    if current_minor is not None:
        candidates.append(current_minor * 2 + 7)
    for value in candidates:
        if value == current_minor:
            continue
        injected = api.inject(
            {"kind": "PRICE_SET", "sku": sku, "value": value, "note": "seed_demo_state step 5"}
        )
        if injected is not None:
            return injected
    raise SeedError(f"every candidate price for {sku} was already in force")


def seed_refused_then_paid(api: Api, *, timeout: float) -> tuple[Admitted, Sale, int]:
    """The demonstration: approve version 1, move the merchant, be refused, approve version 2.

    Returns the admitted version 2, its order, and the stale total version 1 carried. The
    gap between the two is the number ``/evidence`` reports as retained by refusing rather
    than honouring a stale approval, so it is read back from the rows and never assumed.
    """
    card, basket = open_checkout(api, REFUSAL_BASKETS)
    stale_total = int(card["amount_minor"])
    approve(api, card)

    # The line whose price moves is the first of the basket that was actually opened, not
    # of the one this function preferred: a fallback basket contains different SKUs, and
    # injecting into a line the checkout does not carry would move no total and produce no
    # refusal to demonstrate.
    sku = basket[0][0]
    injected = raise_price(api, sku, current_minor=unit_price_of(card, sku))
    deltas = injected.get("deltas") or [{}]
    before = deltas[0].get("before")
    after = deltas[0].get("after")
    print(f"    injected {sku} {before} -> {after} (label {injected.get('label')})")

    refused = submit(api, str(card["checkout_id"]), int(card["version"]))
    if refused["allowed"]:
        raise SeedError(
            "version 1 was admitted after a price injection; the merchant state the kernel "
            "revalidated against did not move, so there is no refusal to demonstrate"
        )
    if refused["code"] != "REAPPROVAL_REQUIRED":
        raise SeedError(f"expected REAPPROVAL_REQUIRED, got {refused['code']}")
    following = refused["approval_card"]

    admitted = admit(api, following)
    return admitted, capture(api, admitted, timeout=timeout), stale_total


# ------------------------------------------------------------------------- the refunds


def hold_command(tenant_id: uuid.UUID, *, command_type: str, refund_id: uuid.UUID) -> bool:
    """SEEDING SEAM 3 -- push one queued command past the demonstration.

    Two guards make the seam honest. It names the refund the command carries, so it can
    never hold work belonging to another session driving the same tenant. And it touches
    only a row still ``PENDING``: if the worker already leased it the update matches
    nothing and the caller is told, rather than a settled row being rewritten underneath a
    running handler.

    Used twice. Once to keep a refund genuinely ``REFUND_PENDING`` -- the command has not
    been sent, which is exactly what the row says. Once to stop the reconciliation behind a
    ``REFUND_UNKNOWN`` refund, which is bounded to six rounds and then escalates: it is
    chasing a payment that only ever existed as evidence, so letting it run to
    ``ESCALATED`` would replace a state the console needs with an artefact of the seed.
    """
    with kernel_session(tenant_id) as session:
        # Executed on the session's own connection rather than through the ORM: this is
        # plain DML whose only interesting result is `rowcount`, which is what the guard
        # above is read from.
        result = session.connection().execute(
            text(
                "UPDATE outbox_events SET available_at = now() + make_interval(days => :days) "
                "WHERE tenant_id = :t AND command_type = :kind AND status = 'PENDING' "
                "  AND payload ->> 'refund_id' = :refund"
            ),
            {
                "t": tenant_id,
                "kind": command_type,
                "days": HOLD_DAYS,
                "refund": str(refund_id),
            },
        )
        return result.rowcount > 0


def request_refund(api: Api, order_id: uuid.UUID, *, amount_minor: int, reason: str) -> uuid.UUID:
    """Ask for money back through the buyer's own route, under a fresh Execution Grant."""
    answer = api.post(
        f"/v1/orders/{order_id}/refunds",
        body={"amount_minor": amount_minor, "reason": reason},
    )
    decision = answer["decision"]
    refund = answer.get("refund")
    if not decision["allowed"] or not isinstance(refund, dict):
        raise SeedError(
            f"refund on order {order_id} was refused {decision['code']}: {decision['explanation']}"
        )
    return uuid.UUID(str(refund["refund_id"]))


def refund_status(tenant_id: uuid.UUID, refund_id: uuid.UUID) -> str:
    with kernel_session(tenant_id) as session:
        return str(
            session.execute(
                text("SELECT status FROM refunds WHERE tenant_id = :t AND id = :r"),
                {"t": tenant_id, "r": refund_id},
            ).scalar_one()
        )


REFUND_SETTLED: Final = (
    "SELECT count(*) > 0 FROM refunds WHERE tenant_id = :t AND id = :r AND status = :want"
)


def seed_failed_refund(api: Api, sale: Sale, *, timeout: float) -> str:
    """A refund the provider refuses. Nothing is staged: Razorpay is asked and says no.

    The capture behind these orders is evidence rather than a payment somebody made in
    Razorpay Checkout, so the payment id the refund names does not exist at the provider
    and the refusal is genuine. ``REFUND_FAILED`` means provider-confirmed absence, and it
    is the one of the three states that may be re-admitted under a fresh grant.
    """
    refund_id = request_refund(
        api, sale.order_id, amount_minor=REFUND_AMOUNTS["FAILED"], reason="seed_provider_refused"
    )
    wait_for(
        "the provider's refusal",
        tenant_id=api.tenant_id,
        timeout=timeout,
        sql=REFUND_SETTLED,
        r=refund_id,
        want="FAILED",
    )
    return refund_status(api.tenant_id, refund_id)


def seed_unknown_refund(api: Api, sale: Sale, *, timeout: float) -> str:
    """A refund whose answer was lost, produced by the scenario controller's own fault.

    ``REFUND_TIMEOUT`` is claimed by the worker *instead of* the provider call, so no
    request leaves and the recorded evidence says so. The refund becomes ``REFUND_UNKNOWN``
    -- a refund may or may not exist at the provider, and only reconciliation may decide --
    which is the state a console must never render as if it were ``REFUND_PENDING``.
    """
    api.post(
        "/v1/scenario/faults",
        body={
            "kind": "REFUND_TIMEOUT",
            "payment_attempt_id": str(sale.attempt_id),
            "once": True,
        },
        scenario=True,
    )
    refund_id = request_refund(
        api, sale.order_id, amount_minor=REFUND_AMOUNTS["UNKNOWN"], reason="seed_answer_lost"
    )
    wait_for(
        "the lost answer to be recorded",
        tenant_id=api.tenant_id,
        timeout=timeout,
        sql=REFUND_SETTLED,
        r=refund_id,
        want="UNKNOWN",
    )
    # The reconciliation the kernel enqueued now holds a question the provider cannot
    # answer. Held rather than allowed to spend its six rounds and escalate.
    hold_command(api.tenant_id, command_type="RECONCILE_REFUND", refund_id=refund_id)
    return refund_status(api.tenant_id, refund_id)


def seed_pending_refund(api: Api, sale: Sale, *, timeout: float, attempts: int = 4) -> str:
    """A refund admitted and not yet sent, held there by holding its command.

    The row is ``PENDING`` because nothing has been sent -- the grant is issued, the command
    is queued, the provider has not been asked. Winning the hold is a race against a worker
    that polls once a second, and losing it is detected rather than assumed.

    Losing is recoverable on the same order without needing another one, because of a rule
    that matters in its own right: a ``REFUND_FAILED`` refund may be re-admitted under a
    fresh grant, while a ``REFUND_UNKNOWN`` one may not -- a refund that may already exist
    at the provider is reconciliation's to resolve, and asking again would be the
    double-refund this whole vocabulary exists to prevent. So a lost race waits for the
    leased refund to settle and asks again only if it settled ``FAILED``.
    """
    for _ in range(attempts):
        refund_id = request_refund(
            api, sale.order_id, amount_minor=REFUND_AMOUNTS["PENDING"], reason="seed_not_yet_sent"
        )
        if hold_command(api.tenant_id, command_type="REFUND_EXECUTE", refund_id=refund_id):
            return refund_status(api.tenant_id, refund_id)
        print("    (the worker leased the refund before it could be held; waiting it out)")
        wait_for(
            "the leased refund to settle",
            tenant_id=api.tenant_id,
            timeout=timeout,
            sql="SELECT count(*) > 0 FROM refunds "
            "WHERE tenant_id = :t AND id = :r AND status <> 'PENDING'",
            r=refund_id,
        )
        settled = refund_status(api.tenant_id, refund_id)
        if settled != "FAILED":
            return settled
    return "not held"


# ------------------------------------------------------- the dead letter, cancel, reject


def expire_grant(tenant_id: uuid.UUID, grant_id: uuid.UUID) -> bool:
    """SEEDING SEAM 2 -- move one Execution Grant's clock past its expiry.

    A grant lives 300 seconds (ADR 0003 D13) and this script cannot wait five minutes to
    produce a dead letter. Only an ``ISSUED`` grant is touched: if the worker already
    consumed it, the guard matches nothing and the caller knows it lost the race instead of
    corrupting a grant that authorised a real provider call.

    Nothing after this is staged. The worker finds the grant unusable, refuses
    ``AUTHORITY_INSUFFICIENT``, records ``worker.grant_refused``, and the outbox buries the
    command. That refusal is correct and is the point of the exercise.
    """
    with kernel_session(tenant_id) as session:
        result = session.connection().execute(
            text(
                "UPDATE execution_grants SET expires_at = now() - interval '1 second' "
                "WHERE tenant_id = :t AND id = :g AND status = 'ISSUED'"
            ),
            {"t": tenant_id, "g": grant_id},
        )
        return result.rowcount == 1


def seed_dead_command(api: Api, *, timeout: float, attempts: int = 3) -> uuid.UUID | None:
    """Leave one buried command in the outbox, so the revive control has a subject."""
    for _ in range(attempts):
        card, _ = open_checkout(api, DEAD_LETTER_BASKETS)
        admitted = admit(api, card)
        if not expire_grant(api.tenant_id, admitted.grant_id):
            print("    (the worker consumed the grant first; opening another checkout)")
            continue
        buried = wait_for(
            "the worker to bury the command",
            tenant_id=api.tenant_id,
            timeout=timeout,
            sql="SELECT count(*) > 0 FROM outbox_events "
            "WHERE tenant_id = :t AND id = :c AND status = 'DEAD'",
            c=admitted.command_id,
        )
        if buried:
            return admitted.command_id
    return None


def seed_cancelled(api: Api) -> str:
    """A checkout the buyer walked away from. Refused once money may be moving.

    Cancellation answers a decision rather than a state -- ``allowed`` and a code, because
    once the checkout is ``AWAITING_PAYMENT`` the state table refuses and that refusal is
    an answer, not an error -- so the code is what is reported here. A refusal is raised:
    the checkout this opened a moment ago has nothing in flight, so a refusal would mean
    the cancel path is not doing what this line claims it demonstrates.
    """
    card, _ = open_checkout(api, CANCELLED_BASKETS)
    answer = api.post(
        f"/v1/checkouts/{card['checkout_id']}/cancel", body={"reason": "buyer_changed_mind"}
    )
    if not answer.get("allowed"):
        raise SeedError(
            f"cancelling a fresh checkout was refused {answer.get('code')}: "
            f"{answer.get('explanation')}"
        )
    return str(answer.get("code"))


def seed_rejected(api: Api) -> str:
    """A card the buyer declined, naming the exact bytes declined."""
    card, _ = open_checkout(api, REJECTED_BASKETS)
    answer = api.post(
        f"/v1/checkouts/{card['checkout_id']}/versions/{card['version']}/reject",
        body={"content_hash": card["content_hash"], "reason": "buyer_declined"},
    )
    return str(answer.get("state", "unknown"))


# ------------------------------------------------------------------------------- reset

#: Children before parents. Derived from the foreign keys in the schema and checked by
#: running it: a wrong order fails loudly on a constraint rather than half-clearing.
#: ``tenants`` and ``merchants`` are not here -- the reset rebuilds a tenant's state, it
#: does not delete the tenant ``seed_demo_tenant.py`` made.
RESET_ORDER: Final[tuple[str, ...]] = (
    "provider_requests",
    "reconciliation_runs",
    "execution_grants",
    "orders",
    "refunds",
    "payment_attempts",
    "approvals",
    "delegated_authorities",
    "checkout_versions",
    "checkouts",
    "carts",
    "policy_at_sale_receipts",
    "reservations",
    "outbox_events",
    "webhook_inbox",
    "scenario_faults",
    "scenario_runs",
    "idempotency_records",
    "audit_events",
    "api_sessions",
    "platform_operating_modes",
)


def reset_tenant(admin_url: str, tenant_id: uuid.UUID) -> Mapping[str, int]:
    """Delete this tenant's rows, in one transaction, as an administrative identity.

    Not the kernel role, and not by oversight. No platform role is granted DELETE on any
    table: the API, the worker and the kernel physically cannot erase a financial row, and
    the tenant-isolation suites depend on that. Clearing a demo tenant is therefore an act
    from outside the platform, and it looks like one.

    **Every statement filters on ``tenant_id``.** The administrative role is a superuser
    here, so it bypasses row-level security unconditionally; the predicate is the only
    thing standing between "reset the demo tenant" and "empty the database".
    """
    from sqlalchemy import create_engine

    deleted: dict[str, int] = {}
    engine = create_engine(admin_url, future=True)
    try:
        with engine.begin() as connection:
            for table in RESET_ORDER:
                result = connection.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id = :t"),  # noqa: S608 - fixed list
                    {"t": tenant_id},
                )
                if result.rowcount:
                    deleted[table] = int(result.rowcount)
    finally:
        engine.dispose()
    return deleted


# ------------------------------------------------------------------------ the whole run


def resolve_tenant(tenant_slug: str, merchant_slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Find the tenant and merchant before any session exists, as the kernel role.

    Resolved from the database rather than by minting a session first, because ``--reset``
    deletes ``api_sessions`` and a session minted before it would be gone by the time it
    was used.
    """
    from platform_db import session_scope, set_tenant

    with session_scope("KERNEL") as session:
        tenant_id = session.execute(
            text("SELECT id FROM tenants WHERE slug = :s"), {"s": tenant_slug}
        ).scalar_one_or_none()
        if tenant_id is None:
            raise SeedError(
                f"no tenant with slug {tenant_slug!r}. Run `make seed` "
                f"(scripts/seed_demo_tenant.py) first -- this script seeds state, not identity."
            )
        tenant = uuid.UUID(str(tenant_id))
        set_tenant(session, tenant)
        merchant_id = session.execute(
            text("SELECT id FROM merchants WHERE tenant_id = :t AND slug = :s"),
            {"t": tenant, "s": merchant_slug},
        ).scalar_one_or_none()
        if merchant_id is None:
            raise SeedError(f"tenant {tenant_slug!r} has no merchant with slug {merchant_slug!r}")
        return tenant, uuid.UUID(str(merchant_id))


def preflight(client: httpx.Client) -> None:
    """Refuse to start against a stack that cannot produce the state being asked for."""
    try:
        config = client.get("/v1/config").json()
    except httpx.HTTPError as exc:
        raise SeedError(
            f"the API at {client.base_url} is not answering ({exc}). Start it with `make demo`."
        ) from None
    if not config.get("scenario_routes_enabled"):
        raise SeedError(
            "the scenario routes are disabled, so the price injection step 5 turns on "
            "cannot run. Set SCENARIO_KEY and use PROFILE=development."
        )
    database = config.get("database", {})
    if not (database.get("kernel_role") and database.get("app_role")):
        raise SeedError(f"the API reports a database it cannot reach as both roles: {database}")


#: The three refund states, the seeder that reaches each, and the ``refunds.status`` that
#: proves it. Ordered so the two that cost a provider round trip run before the one that
#: races the worker for its command.
RefundSeeder = Callable[..., str]
REFUND_PLAN: Final[tuple[tuple[str, RefundSeeder], ...]] = (
    ("FAILED", seed_failed_refund),
    ("UNKNOWN", seed_unknown_refund),
    ("PENDING", seed_pending_refund),
)


def build(api: Api, result: SeedResult, *, orders: int, refusals: int, timeout: float) -> None:
    """Create only what the survey says is missing, in the order the surfaces need it."""
    before = result.before
    purchased: list[Sale] = []

    # --- plain confirmed orders --------------------------------------------------
    # The refusal shape is built after these so that `/evidence`, which defaults to the
    # newest refused approval when no checkout is named, opens on the one this run made.
    plain_held = max(before.confirmed_orders - before.refused_then_paid, 0)
    for index in range(max(orders - refusals - plain_held, 0)):
        # Rotated so consecutive orders differ, and the rest of the list follows as
        # fallbacks when the preferred basket cannot take a hold.
        start = index % len(ORDER_BASKETS)
        sale = buy(api, ORDER_BASKETS[start:] + ORDER_BASKETS[:start], timeout=timeout)
        purchased.append(sale)
        result.did(
            f"order {sale.order_id} confirmed at {sale.amount_minor} minor {sale.currency} "
            f"from WEBHOOK capture evidence"
        )

    # --- refunds, one order each so three states can coexist ----------------------
    # One payment attempt cannot hold three refunds in three different states, so each
    # needs its own confirmed order. Orders this run made are used first; if it made none,
    # because the tenant already held enough, its existing unrefunded orders are used.
    held = {
        "FAILED": before.refunds_failed,
        "UNKNOWN": before.refunds_unknown,
        "PENDING": before.refunds_pending,
    }
    if any(count < 1 for count in held.values()):
        available = purchased + unrefunded_sales(api.tenant_id, exclude=purchased)
        for status, make in REFUND_PLAN:
            if held[status] >= 1:
                continue
            if not available:
                result.missed(f"no confirmed order left to carry a REFUND_{status} refund")
                continue
            sale = available.pop(0)
            reached = make(api, sale, timeout=timeout)
            note = f"refund on order {sale.order_id} is {reached}"
            if reached == status:
                result.did(note)
            else:
                result.missed(f"{note}, wanted {status}")

    # --- the refusal that /evidence accounts for ----------------------------------
    # Built last among the purchases, because `/evidence` opens on the *newest* refused
    # approval when no checkout is named. Asking for more than one is how a tenant that
    # already carries somebody else's refusal gets a fresh one on top: the arithmetic on
    # the headline screen is then this run's, with amounts a reader can check against
    # docs/DEMO.md rather than whatever price a previous session left behind.
    for _ in range(max(refusals - before.refused_then_paid, 0)):
        admitted, sale, stale = seed_refused_then_paid(api, timeout=timeout)
        result.refusals.append(admitted.checkout_id)
        result.did(
            f"version 1 approved at {stale} minor and refused REAPPROVAL_REQUIRED; "
            f"version {admitted.version} approved at {admitted.amount_minor} and captured "
            f"as order {sale.order_id}"
        )

    # --- the operator's furniture -------------------------------------------------
    if before.dead_commands < 1:
        command_id = seed_dead_command(api, timeout=timeout)
        if command_id is None:
            result.missed("no dead outbox command: the worker consumed every grant in time")
        else:
            result.did(f"outbox command {command_id} buried DEAD on an expired grant")

    if before.cancelled_checkouts < 1:
        result.did(f"a checkout cancelled by the buyer ({seed_cancelled(api)})")
    if before.rejected_checkouts < 1:
        result.did(f"an approval card rejected by the buyer ({seed_rejected(api)})")


def unrefunded_sales(tenant_id: uuid.UUID, *, exclude: Sequence[Sale]) -> list[Sale]:
    """Confirmed orders whose payment attempt carries no refund yet, newest first."""
    with kernel_session(tenant_id) as session:
        rows = session.execute(
            text(
                "SELECT o.id, o.payment_attempt_id, o.total_minor, o.currency FROM orders o "
                "WHERE o.tenant_id = :t "
                "  AND NOT EXISTS (SELECT 1 FROM refunds r WHERE r.tenant_id = o.tenant_id "
                "                    AND r.payment_attempt_id = o.payment_attempt_id) "
                "ORDER BY o.created_at DESC"
            ),
            {"t": tenant_id},
        ).all()
    blocked = {sale.order_id for sale in exclude}
    sales = [
        Sale(
            order_id=uuid.UUID(str(row[0])),
            attempt_id=uuid.UUID(str(row[1])),
            amount_minor=int(row[2]),
            currency=str(row[3]),
        )
        for row in rows
    ]
    return [sale for sale in sales if sale.order_id not in blocked]


def read_retained_revenue(
    api: Api, merchant_id: uuid.UUID, *, checkout_id: str | None = None
) -> Mapping[str, Any] | None:
    """Ask ``/evidence``'s own endpoint what it will show, so the report is not a guess.

    Named checkout or not: without one the endpoint answers for the newest refused
    approval in the whole tenant, which is what the console's page does by default and is
    therefore worth reporting even when it is somebody else's checkout.
    """
    query = "" if checkout_id is None else f"?checkout_id={checkout_id}"
    try:
        return api.get(
            f"/v1/merchants/{merchant_id}/evidence/retained-revenue{query}", scenario=True
        )
    except SeedError:
        return None


def run(
    *,
    api_base: str,
    tenant_slug: str,
    merchant_slug: str,
    scenario_key: str,
    orders: int,
    refusals: int,
    timeout: float,
    do_reset: bool,
    admin_url: str,
    catalogue_reset: bool,
) -> SeedResult:
    tenant_id, merchant_id = resolve_tenant(tenant_slug, merchant_slug)

    deleted: Mapping[str, int] = {}
    if do_reset:
        print(f"Resetting {tenant_slug} as {redacted(admin_url)}")
        deleted = reset_tenant(admin_url, tenant_id)
        cleared = ", ".join(f"{name} {count}" for name, count in sorted(deleted.items()))
        print(f"  deleted {cleared or 'nothing: the tenant was already empty'}")

    with httpx.Client(base_url=api_base, timeout=30.0) as client:
        preflight(client)
        api = Api(client, scenario_key=scenario_key)
        api.mint(tenant_slug=tenant_slug, merchant_slug=merchant_slug)

        if catalogue_reset:
            # Prices, stock, listings and fees back to the fixture baseline, so the amounts
            # below are the ones docs/DEMO.md quotes and a re-run produces the same figures.
            api.inject({"kind": "CATALOGUE_RESET", "note": "seed_demo_state: before"})

        before = survey(tenant_id)
        result = SeedResult(
            tenant_id=tenant_id,
            tenant_slug=tenant_slug,
            merchant_id=merchant_id,
            merchant_slug=merchant_slug,
            before=before,
            after=before,
            reset=do_reset,
            deleted=deleted,
        )
        print()
        print("Building what is missing:")
        build(api, result, orders=orders, refusals=refusals, timeout=timeout)
        if not result.created_anything:
            print("  (nothing: the tenant already holds every state this script seeds)")

        if catalogue_reset:
            # Back to baseline again. The seeded checkouts are frozen at their own versions
            # and do not move, and step 5 of the runbook needs Rs 28.00 milk to raise to
            # Rs 79.00 -- an already-injected price is the single most likely way to waste
            # a recording.
            api.inject({"kind": "CATALOGUE_RESET", "note": "seed_demo_state: after"})

        result.after = survey(tenant_id)
        seeded = result.refusals[-1] if result.refusals else None
        result.retained_revenue = read_retained_revenue(api, merchant_id, checkout_id=seeded)
        result.retained_revenue_default = (
            result.retained_revenue if seeded is None else read_retained_revenue(api, merchant_id)
        )
        return result


# ------------------------------------------------------------------------------ output


def report(result: SeedResult, *, api_base: str, console_base: str) -> None:
    """Print what changed, then where to go and look at it."""
    print()
    print(f"Demo state in tenant {result.tenant_slug} ({result.tenant_id}):")
    print()
    print(f"  {'':<38}{'before':>8}{'after':>8}")
    for (label, before_count), (_, after_count) in zip(
        result.before.lines(), result.after.lines(), strict=True
    ):
        print(f"  {label:<38}{before_count:>8}{after_count:>8}")
    print()

    if result.shortfalls:
        print("Not reached, and the demo is missing it:")
        for line in result.shortfalls:
            print(f"  ! {line}")
        print()

    evidence = result.retained_revenue
    if evidence is None:
        print("/evidence has no refused approval to account for yet.")
        print()
    else:
        subject = (
            "the newest refused approval in the tenant"
            if not result.refusals
            else f"checkout {result.refusals[-1]}"
        )
        print(f"What /evidence accounts for on {subject}:")
        print(f"  stale approved   {evidence.get('stale_approved_minor')}")
        print(f"  corrected total  {evidence.get('corrected_total_minor')}")
        print(
            f"  captured         {evidence.get('captured_minor')} "
            f"(from {evidence.get('captured_from')})"
        )
        print(
            f"  difference       {evidence.get('difference_minor')} "
            f"to the {str(evidence.get('direction')).lower()}"
        )
        print(f"  net retained     {evidence.get('net_retained_minor')}")
        print(f"  controlled       {evidence.get('controlled_scenario')}")
        print()
        if evidence.get("captured_minor") is None:
            # A refusal nobody went on to pay for. Honest, and the page says so in terms
            # -- but it is the wrong screen to open a recording on, and the fix is one
            # flag rather than a puzzle.
            print(
                "  That refusal has no capture behind it, so the page states no "
                "difference at all.\n"
                "  Put a settled one on top with --refusals "
                f"{result.after.refused_then_paid + 1}."
            )
            print()

    # The page's own default is the newest refused approval in the whole tenant. On a
    # tenant several people are driving, that is whoever refused last, so say when it is
    # not the one this run built rather than leaving a recording to discover it.
    default = result.retained_revenue_default
    drifted = (
        default is not None
        and result.refusals
        and str(default.get("checkout_id")) != result.refusals[-1]
    )
    if drifted and default is not None:
        print(
            "/evidence opens on the newest refusal in the tenant, and that is currently "
            f"checkout {default.get('checkout_id')}, not this run's. Link the one you want:"
        )
        print(f"  {console_base}/evidence?checkout_id={result.refusals[-1]}")
        print()

    print("Look at it, rather than trusting these counts:")
    print(f"  {console_base}/operations   orders, refunds, the outbox and its revive control")
    print(f"  {console_base}/evidence     retained revenue and the audit chain")
    print(f"  {console_base}/inspector    one payment attempt's whole history")
    print(f"  {console_base}/catalogue    the merchant simulator's state")
    print(f"  {api_base}/docs   every endpoint the above reads")
    print()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Drive the demo tenant into a state worth showing, through the real paths.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("SEED_DATABASE_URL", DEFAULT_DATABASE_URL),
        help="Kernel-role connection URL. Overridable with SEED_DATABASE_URL.",
    )
    parser.add_argument(
        "--admin-database-url",
        default=os.environ.get("SEED_ADMIN_DATABASE_URL", DEFAULT_ADMIN_DATABASE_URL),
        help="--reset only. Needs DELETE, which no platform role has.",
    )
    parser.add_argument(
        "--api-base",
        default=os.environ.get("API_BASE", DEFAULT_API_BASE),
        help="The running API. Every buyer action goes through it.",
    )
    parser.add_argument(
        "--console-base",
        default=os.environ.get("CONSOLE_BASE", "http://localhost:3001"),
        help="Only used to print where to go and look.",
    )
    parser.add_argument(
        "--scenario-key",
        default=os.environ.get("SCENARIO_KEY", DEFAULT_SCENARIO_KEY),
        help="X-Scenario-Key. Matches scripts/run_demo.sh's default.",
    )
    parser.add_argument("--tenant-slug", default=DEFAULT_TENANT_SLUG)
    parser.add_argument("--merchant-slug", default=DEFAULT_MERCHANT_SLUG)
    parser.add_argument(
        "--orders",
        type=int,
        default=DEFAULT_ORDERS,
        help="Orders the tenant should end up with, the refused-then-paid ones included.",
    )
    parser.add_argument(
        "--refusals",
        type=int,
        default=DEFAULT_REFUSALS,
        help="How many of those are refused-then-re-approved. /evidence opens on the newest.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="Seconds to wait for the worker to reach a state before reporting a shortfall.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete this tenant's rows and rebuild from empty. Needs --admin-database-url.",
    )
    parser.add_argument(
        "--no-catalogue-reset",
        dest="catalogue_reset",
        action="store_false",
        help="Leave the merchant simulator's prices alone. Amounts then depend on live state.",
    )
    parser.set_defaults(catalogue_reset=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    url: str = args.database_url

    if database_name(url) == "commerce_test":
        print(
            "Refusing to seed commerce_test: it is the suite's database and its fixtures "
            "delete only what they created. Point --database-url at commerce_dev.",
            file=sys.stderr,
        )
        return 2
    if args.orders < 1:
        print("--orders must be at least 1.", file=sys.stderr)
        return 2
    if not 0 <= args.refusals <= args.orders:
        print("--refusals must be between 0 and --orders.", file=sys.stderr)
        return 2

    # Set before platform_db is imported anywhere, so the engine resolves the URL this run
    # was told to use and the printed URL cannot diverge from the used one.
    os.environ["DATABASE_URL_KERNEL"] = url
    print(f"Seeding state as the kernel role against {redacted(url)}")

    try:
        result = run(
            api_base=args.api_base.rstrip("/"),
            tenant_slug=args.tenant_slug,
            merchant_slug=args.merchant_slug,
            scenario_key=args.scenario_key,
            orders=args.orders,
            refusals=args.refusals,
            timeout=args.timeout,
            do_reset=args.reset,
            admin_url=args.admin_database_url,
            catalogue_reset=args.catalogue_reset,
        )
    except SeedError as exc:
        print(f"\nSeeding state failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - a seed failure must explain itself
        print(f"\nSeeding state failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(
            "\nMost likely causes, in order:\n"
            "  1. The API or the Action Executor is not running:  make demo\n"
            "  2. The tenant has not been seeded:                make seed\n"
            "  3. PostgreSQL is not running or is unmigrated:    make bootstrap",
            file=sys.stderr,
        )
        return 1

    report(result, api_base=args.api_base.rstrip("/"), console_base=args.console_base.rstrip("/"))
    return 0 if not result.shortfalls else 1


if __name__ == "__main__":
    raise SystemExit(main())
