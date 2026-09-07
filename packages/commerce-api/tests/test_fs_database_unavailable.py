"""Failure scenario: PostgreSQL is unreachable for a period, then returns.

Specification 23.3 says the platform must "fail closed for approvals, admission, payment
and refund mutation" when Cloud SQL is unavailable, and section 30's governing rule is
that degradation has to be *visible* rather than silent. Both halves are asserted here,
and so is the half that is easy to forget: the process has to still be there afterwards.

**The outage is real, not simulated.** A mock connection that raises would prove that the
code handles a raised exception, which is not the question. The question is what a real
pooled connection does when the socket under it dies, so these tests run the API's
database traffic through a TCP forwarder on an ephemeral port and then stop forwarding.
The connections in SQLAlchemy's pool are genuine PostgreSQL sessions that get their
sockets closed underneath them, and the next request finds them broken exactly the way it
would if Cloud SQL had gone away. Recovery is equally real: the forwarder is started
again and the same ``Engine`` object -- asserted to be the same object, so this is not a
fresh pool wearing the old one's name -- serves traffic again. ``pool_pre_ping``
(``commerce_api.deps.engine_for``) is what makes that work, and it is claimed nowhere
else in the suite.

What a user sees during the outage, in the order a person would meet it:

* the storefront still loads, because ``GET /healthz`` answers from process state. A
  liveness probe wired to the database would have restarted this pod mid-payment.
* ``GET /v1/config`` says ``database.reachable: false`` and carries a ``degraded`` entry
  naming the component and the consequence. That is the visible-degradation rule: an
  operator reading the console does not have to infer an outage from a blank screen.
* a submit -- the money mutation -- comes back as an RFC 9457 problem with no decision in
  it. It is *not* a 200 denial, and that distinction is deliberate: a denial is the kernel
  having considered the request, and during an outage the kernel considered nothing.

What the money does: nothing. The checkout is at the version it was at before the outage,
with no payment attempt, no Execution Grant and no outbox command, and a submit after
recovery produces exactly one of each. Fail-closed, and then forward.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import uuid
from collections.abc import Iterator
from typing import Any, Final

import pytest
from commerce_api.app import create_app
from commerce_api.deps import engine_for
from commerce_api.settings import Settings
from fastapi.testclient import TestClient
from sqlalchemy import Engine, make_url, text

from conftest import (
    APP_URL,
    KERNEL_URL,
    TEST_KEY_ID,
    TEST_KEY_SECRET,
    TEST_SCENARIO_KEY,
    TEST_WEBHOOK_SECRET,
    MintedSession,
    SeededTenant,
)

pytestmark = pytest.mark.db

MILK: Final[str] = "AMUL-DAIRY-001"
_SET_TENANT: Final = text("SELECT set_config('app.tenant_id', :tenant_id, true)")

#: How long a forwarder thread waits on a socket before checking whether it was asked to
#: stop. Short, because the whole outage in these tests lasts a few hundred milliseconds.
_POLL_SECONDS: Final[float] = 0.25


# ------------------------------------------------------------------- the outage itself


class _Gate:
    """A TCP forwarder to PostgreSQL that can be cut and restored.

    Every connection it accepts is paired with an upstream connection to the real server
    and pumped in both directions by two threads. :meth:`cut` stops accepting and closes
    every socket it holds, which is what a client sees when a database goes away: existing
    connections break, and new ones are refused.

    Deliberately not a fixture with a single lifecycle -- a test needs to open it, cut it,
    and open it again within one process, which is the whole scenario.
    """

    def __init__(self, upstream_host: str, upstream_port: int) -> None:
        self._upstream = (upstream_host, upstream_port)
        self._listener: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._live: list[socket.socket] = []
        self._lock = threading.Lock()
        self._running = threading.Event()
        # Bound once, up front, so the port is stable across a cut and a restore: the
        # database URL is baked into the app's engine cache and cannot be rewritten later.
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", 0))
        self.port: int = probe.getsockname()[1]
        probe.close()

    def open(self) -> None:
        """Start accepting and forwarding. Idempotent."""
        if self._running.is_set():
            return
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", self.port))
        listener.listen(64)
        listener.settimeout(_POLL_SECONDS)
        self._listener = listener
        self._running.set()
        self._spawn(self._accept_loop)

    def cut(self) -> None:
        """Stop forwarding and break every connection currently open through this gate."""
        self._running.clear()
        listener, self._listener = self._listener, None
        if listener is not None:
            _close(listener)
        with self._lock:
            live, self._live = self._live, []
        for sock in live:
            _close(sock)
        for thread in self._threads:
            thread.join(timeout=5.0)
        self._threads = []

    def _accept_loop(self) -> None:
        while self._running.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                downstream, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                upstream = socket.create_connection(self._upstream, timeout=5.0)
            except OSError:  # pragma: no cover - the real server is up in these tests
                _close(downstream)
                continue
            with self._lock:
                self._live.extend((downstream, upstream))
            self._spawn(self._pump, downstream, upstream)
            self._spawn(self._pump, upstream, downstream)

    def _pump(self, source: socket.socket, sink: socket.socket) -> None:
        source.settimeout(_POLL_SECONDS)
        while self._running.is_set():
            try:
                chunk = source.recv(65536)
            except TimeoutError:
                continue
            except OSError:
                break
            if not chunk:
                break
            try:
                sink.sendall(chunk)
            except OSError:
                break
        _close(source)
        _close(sink)

    def _spawn(self, target: Any, *args: Any) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True)
        self._threads.append(thread)
        thread.start()


def _close(sock: socket.socket) -> None:
    """Break a socket both ways and let it go. Every failure here is uninteresting: the
    peer may already have gone, and a double close during a cut is normal."""
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        sock.close()


def _through_gate(url: str, port: int) -> str:
    """The same credentials and database, reached through the forwarder's port."""
    return make_url(url).set(host="127.0.0.1", port=port).render_as_string(hide_password=False)


# -------------------------------------------------------------------------- fixtures


@pytest.fixture
def gate() -> Iterator[_Gate]:
    """A forwarder to whatever host the test database actually lives on."""
    upstream = make_url(APP_URL)
    forwarder = _Gate(upstream.host or "127.0.0.1", upstream.port or 5432)
    forwarder.open()
    yield forwarder
    forwarder.cut()


@pytest.fixture
def gated_app(gate: _Gate) -> Iterator[tuple[TestClient, str, str]]:
    """An API whose app and kernel roles both reach PostgreSQL through the gate.

    Yields the client and the two gated URLs, because the tests assert on the engines
    those URLs resolve to. ``raise_server_exceptions=False`` so a failure during the
    outage arrives as the response a browser would get rather than as a raised exception.
    """
    app_url = _through_gate(APP_URL, gate.port)
    kernel_url = _through_gate(KERNEL_URL, gate.port)
    settings = Settings(
        PROFILE="development",
        DATABASE_URL_APP=app_url,
        DATABASE_URL_KERNEL=kernel_url,
        RAZORPAY_KEY_ID=TEST_KEY_ID,
        RAZORPAY_KEY_SECRET=TEST_KEY_SECRET,
        RAZORPAY_WEBHOOK_SECRET=TEST_WEBHOOK_SECRET,
        SCENARIO_KEY=TEST_SCENARIO_KEY,
        SESSION_TTL_SECONDS=3600,
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        yield client, app_url, kernel_url


def _key() -> str:
    return f"k-{uuid.uuid4().hex}"


def _mint(client: TestClient, tenant_slug: str) -> MintedSession:
    response = client.post(
        "/v1/demo/sessions", json={"tenant_slug": tenant_slug, "actor_type": "BUYER"}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return MintedSession(
        token=body["token"],
        session_id=uuid.UUID(body["session_id"]),
        tenant_id=uuid.UUID(body["tenant_id"]),
        merchant_id=uuid.UUID(body["merchant_id"]),
        buyer_ref=body["buyer_ref"],
        actor_type=body["actor_type"],
    )


def _approved_checkout(client: TestClient, auth: dict[str, str]) -> dict[str, Any]:
    """Cart, line, checkout, approval -- everything up to the money mutation."""
    cart = client.post("/v1/carts", headers={"Idempotency-Key": _key(), **auth})
    assert cart.status_code == 201, cart.text
    cart_id = cart.json()["cart_id"]

    line = client.put(
        f"/v1/carts/{cart_id}/lines/{MILK}",
        json={"quantity": 2},
        headers={"Idempotency-Key": _key(), **auth},
    )
    assert line.status_code == 200, line.text

    opened = client.post(
        f"/v1/carts/{cart_id}/checkout", headers={"Idempotency-Key": _key(), **auth}
    )
    assert opened.status_code == 201, opened.text
    card: dict[str, Any] = opened.json()

    approved = client.post(
        f"/v1/checkouts/{card['checkout_id']}/versions/{card['version']}/approve",
        json={
            "content_hash": card["content_hash"],
            "amount_minor": card["amount_minor"],
            "currency": card["currency"],
        },
        headers={"Idempotency-Key": _key(), **auth},
    )
    assert approved.status_code == 200, approved.text
    return card


def _counts(admin_engine: Engine, tenant_id: uuid.UUID) -> dict[str, int]:
    """Every row type a submit would create, counted under the owner role."""
    tables = ("payment_attempts", "execution_grants", "outbox_events")
    counted: dict[str, int] = {}
    with admin_engine.begin() as conn:
        conn.execute(_SET_TENANT, {"tenant_id": str(tenant_id)})
        for table in tables:
            # S608: `table` iterates the literal tuple above, and an identifier cannot be
            # supplied as a bound parameter.
            statement = text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t")  # noqa: S608
            counted[table] = int(conn.execute(statement, {"t": tenant_id}).scalar_one())
    return counted


# ----------------------------------------------------------------------------- tests


def test_liveness_survives_the_database_going_away(
    gated_app: tuple[TestClient, str, str], gate: _Gate
) -> None:
    """``/healthz`` keeps answering, so Kubernetes does not restart the pod mid-payment.

    This is the assertion behind the comment in ``commerce_api.routers.health``: a
    liveness probe that touches PostgreSQL converts a recoverable database blip into a
    killed process, and a killed process during a provider call is how an outcome becomes
    unknown with nobody left holding the context to reconcile it.
    """
    client, _, _ = gated_app
    assert client.get("/healthz").status_code == 200

    gate.cut()

    alive = client.get("/healthz")
    assert alive.status_code == 200
    assert alive.json()["status"] == "ok"


def test_the_outage_is_reported_rather_than_left_to_be_inferred(
    gated_app: tuple[TestClient, str, str], gate: _Gate
) -> None:
    """``GET /v1/config`` names the component and says what it costs the buyer.

    Silent degradation is a defect by this project's own rules, so the diagnostic has to
    survive the thing it diagnoses: ``_reachable`` catches broadly for exactly this
    request, and the endpoint answers 200 with the bad news rather than 500 with none.
    """
    client, _, _ = gated_app
    healthy = client.get("/v1/config")
    assert healthy.status_code == 200
    assert healthy.json()["database"]["reachable"] is True
    assert healthy.json()["degraded"] == []

    gate.cut()

    degraded = client.get("/v1/config")
    assert degraded.status_code == 200, degraded.text
    body = degraded.json()
    assert body["database"] == {"reachable": False, "app_role": False, "kernel_role": False}
    components = {entry["component"] for entry in body["degraded"]}
    assert "database" in components
    notice = next(e["notice"] for e in body["degraded"] if e["component"] == "database")
    assert "Checkout, approval and payment are unavailable" in notice


def test_safe_mode_is_reported_unknown_rather_than_asserted_false(
    gated_app: tuple[TestClient, str, str], gate: _Gate
) -> None:
    """An unreadable operating mode degrades loudly instead of claiming NORMAL quietly.

    ``_safe_mode`` returns ``(False, known=False)`` when the query fails, and the false
    would be indistinguishable from a real answer if it travelled alone. The second
    ``degraded`` entry is what makes it distinguishable, and it is the difference between
    "the kill switch is off" and "nobody can tell you whether the kill switch is off".
    """
    client, _, _ = gated_app
    gate.cut()

    body = client.get("/v1/config").json()
    assert body["safe_mode"] is False
    notice = next(e["notice"] for e in body["degraded"] if e["component"] == "safe_mode")
    assert "Treat that as unknown rather than as an assurance." in notice


def test_a_submit_during_the_outage_moves_no_money_and_is_not_a_denial(
    gated_app: tuple[TestClient, str, str],
    gate: _Gate,
    seeded_tenant: SeededTenant,
    capi_admin_engine: Engine,
) -> None:
    """The money mutation fails closed, and fails as a problem rather than as a decision.

    Two separate claims, and the second is the one a reviewer should press on. ADR 0003
    D15 makes a kernel denial a 200 carrying a structured decision, because a denial is
    the system working. An outage is not a denial: nothing was admitted, nothing was
    refused, and answering 200 with a decision-shaped body would tell a buyer that the
    platform considered their checkout when it never saw it. So this asserts a 5xx problem
    *and* the absence of a decision, and then asserts the database is untouched.
    """
    client, _, _ = gated_app
    minted = _mint(client, seeded_tenant.tenant_slug)
    auth = minted.auth_header
    card = _approved_checkout(client, auth)
    before = _counts(capi_admin_engine, seeded_tenant.tenant_id)
    assert before == {"payment_attempts": 0, "execution_grants": 0, "outbox_events": 0}

    gate.cut()

    response = client.post(
        f"/v1/checkouts/{card['checkout_id']}/versions/{card['version']}/submit",
        headers={"Idempotency-Key": _key(), **auth},
    )
    assert response.status_code >= 500, response.text
    assert "decision" not in response.text
    # The problem body discloses no connection string, exactly as for any other 500.
    assert "postgresql" not in response.text

    gate.open()
    assert _counts(capi_admin_engine, seeded_tenant.tenant_id) == before


def test_the_same_process_recovers_and_the_checkout_is_where_it_was_left(
    gated_app: tuple[TestClient, str, str],
    gate: _Gate,
    seeded_tenant: SeededTenant,
    capi_admin_engine: Engine,
) -> None:
    """After the database returns, one submit produces exactly one of each row.

    The engine identity assertion is the point: ``commerce_api.deps.engine_for`` is
    ``lru_cache``d per URL, so the object serving traffic after the outage is the object
    whose pooled connections were killed during it. Recovery therefore belongs to
    ``pool_pre_ping``, which discards a dead connection and dials a new one, rather than
    to a restart that these tests never perform.

    The state assertion is the money invariant. Nothing was lost -- the approved version 1
    is still approved and still holds its reservation, so the buyer does not start again
    -- and nothing was duplicated, because the submit that failed closed left no attempt
    behind for this one to collide with.
    """
    client, app_url, kernel_url = gated_app
    engines = (engine_for(app_url), engine_for(kernel_url))
    minted = _mint(client, seeded_tenant.tenant_slug)
    auth = minted.auth_header
    card = _approved_checkout(client, auth)

    gate.cut()
    failed = client.post(
        f"/v1/checkouts/{card['checkout_id']}/versions/{card['version']}/submit",
        headers={"Idempotency-Key": _key(), **auth},
    )
    assert failed.status_code >= 500

    gate.open()

    assert (engine_for(app_url), engine_for(kernel_url)) == engines
    assert client.get("/v1/config").json()["database"]["reachable"] is True

    # A different key: the failed submit never reached the idempotency table, so replaying
    # its key would prove nothing about whether the platform can still admit.
    submitted = client.post(
        f"/v1/checkouts/{card['checkout_id']}/versions/{card['version']}/submit",
        headers={"Idempotency-Key": _key(), **auth},
    )
    assert submitted.status_code == 200, submitted.text
    decision = submitted.json()
    assert decision["allowed"] is True, decision

    assert _counts(capi_admin_engine, seeded_tenant.tenant_id) == {
        "payment_attempts": 1,
        "execution_grants": 1,
        "outbox_events": 1,
    }
