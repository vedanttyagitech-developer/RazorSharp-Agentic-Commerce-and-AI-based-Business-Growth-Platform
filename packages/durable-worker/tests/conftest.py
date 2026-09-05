"""Fixtures for the worker suite: a real database, a scripted provider, no network.

Two halves, and both are deliberate.

**The database is real and the roles are unprivileged.** ``commerce_test_worker`` and
``commerce_test_kernel`` are ``NOSUPERUSER NOBYPASSRLS``, asserted here rather than
assumed, because a superuser connection makes every row-level-security and grant
assertion in this suite pass for the wrong reason. The ``admitted`` fixture builds a
genuinely admissible checkout and runs the kernel's own admission, so the payment attempt
and the Execution Grant a test acts on are the ones production would have.

**The provider is scripted and the transport is counted.** :class:`FakeTransport` records
every request and refuses to be called more often than the script allows, which is how
this suite proves the claim that matters most: a second delivery of a create-order command
calls the transport zero times.

Import the helpers by plain name -- ``from conftest import FakeTransport, json_response``
-- as the other suites in this repository do; pytest's prepend import mode puts this
directory on ``sys.path``.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Final

import pytest
import transaction_kernel as tk
from commerce_domain import Money, canonical_hash, uuid7
from durable_work import CreateOrderCommand, enqueue_command
from durable_worker.settings import WorkerRuntime, WorkerSettings, build_runtime
from payment_adapters import HttpRequest, HttpResponse
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session
from transaction_kernel import receipts, reservations
from transaction_kernel.admission import AdmissionRequest, CurrentMerchantState, admit
from transaction_kernel.receipts import BuyerVisibleRef, MerchantPolicy, PolicyKind, ReceiptDraft

WORKER_URL: Final[str] = os.environ.get(
    "DATABASE_URL_TEST_WORKER",
    "postgresql+psycopg://commerce_test_worker:testpw@localhost:5432/commerce_test",
)
KERNEL_URL: Final[str] = os.environ.get(
    "DATABASE_URL_TEST_KERNEL",
    "postgresql+psycopg://commerce_test_kernel:testpw@localhost:5432/commerce_test",
)
ADMIN_URL: Final[str] = os.environ.get(
    "DATABASE_URL_TEST_ADMIN",
    "postgresql+psycopg://vedanttyagi@localhost:5432/commerce_test",
)

#: Credentials with the right shape and no meaning: the adapter's guard inspects the key
#: prefix and the secrets' length and distinctness, never their value.
TEST_KEY_ID: Final[str] = "rzp_test_dwkworkerkey"
TEST_KEY_SECRET: Final[str] = "dwk-test-api-secret"  # noqa: S105 - fake, prefix-checked only
TEST_WEBHOOK_SECRET: Final[str] = "dwk-test-webhook-sec"  # noqa: S105 - fake, see above

APPROVED_TOTAL: Final[Money] = Money(39500, "INR")

SET_TENANT: Final = text("SELECT set_config('app.tenant_id', :t, true)")

#: Teardown order: children before parents, so no foreign key is violated mid-clean.
_TENANT_TABLES: Final[tuple[str, ...]] = (
    "provider_requests",
    "reconciliation_runs",
    "orders",
    # execution_grants before refunds: a refund grant carries a foreign key to the
    # refunds row it authorises (ADR D10), so the reverse order fails on that constraint.
    "execution_grants",
    "refunds",
    "payment_attempts",
    "approvals",
    "reservations",
    "delegated_authorities",
    "checkout_versions",
    "policy_at_sale_receipts",
    "idempotency_records",
    "audit_events",
    "outbox_events",
    "webhook_inbox",
    "scenario_faults",
    "scenario_runs",
    "checkouts",
    "baskets",
    "api_sessions",
    "merchants",
)


# ------------------------------------------------------------------ scripted provider


class FakeTransport:
    """A :class:`payment_adapters.HttpTransport` whose every answer is written by a test.

    Satisfies the protocol structurally. Each scripted entry is either a response to
    return or an exception to raise, so a timeout is expressed as
    ``FakeTransport([TransportTimeoutError("...")])`` and costs no wall-clock time.

    Being called more often than the script allows is an ``AssertionError`` rather than a
    quiet default, because "the adapter sent exactly one request" is a claim this suite
    makes and a permissive fake would let it rot.
    """

    def __init__(self, script: list[HttpResponse | Exception] | None = None) -> None:
        self._script: list[HttpResponse | Exception] = list(script or [])
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if not self._script:
            raise AssertionError(
                f"transport called {len(self.requests)} times but only "
                f"{len(self.requests) - 1} answers were scripted; "
                f"the unscripted call was {request.method} {request.url}"
            )
        answer = self._script.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def extend(self, script: list[HttpResponse | Exception]) -> None:
        self._script.extend(script)


def json_response(status: int, payload: Any) -> HttpResponse:
    """A response whose body is JSON bytes, as a real provider would send."""
    return HttpResponse(status=status, body=json.dumps(payload).encode("utf-8"))


def order_entity(
    *,
    order_id: str = "order_DWKtest000001",
    amount: int = 39500,
    currency: str = "INR",
    receipt: str,
    status: str = "created",
) -> dict[str, Any]:
    """A Razorpay order entity, echoing what the adapter cross-checks."""
    return {
        "id": order_id,
        "entity": "order",
        "amount": amount,
        "currency": currency,
        "receipt": receipt,
        "status": status,
    }


def payment_entity(
    *,
    payment_id: str = "pay_DWKtest00001",
    order_id: str = "order_DWKtest000001",
    amount: int = 39500,
    currency: str = "INR",
    status: str = "captured",
    amount_refunded: int = 0,
    notes: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    entity: dict[str, Any] = {
        "id": payment_id,
        "entity": "payment",
        "order_id": order_id,
        "amount": amount,
        "currency": currency,
        "status": status,
        "amount_refunded": amount_refunded,
        "created_at": 1_767_225_600,
    }
    if notes is not None:
        entity["notes"] = dict(notes)
    return entity


def webhook_body(
    event: str,
    *,
    payment: Mapping[str, Any] | None = None,
    order: Mapping[str, Any] | None = None,
    refund: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """A Razorpay webhook body in the provider's documented envelope shape."""
    payload: dict[str, Any] = {}
    if payment is not None:
        payload["payment"] = {"entity": dict(payment)}
    if order is not None:
        payload["order"] = {"entity": dict(order)}
    if refund is not None:
        payload["refund"] = {"entity": dict(refund)}
    return {
        "entity": "event",
        "account_id": "acc_DWKTEST00000",
        "event": event,
        "contains": sorted(payload),
        "payload": payload,
        "created_at": 1_767_225_600,
    }


# ------------------------------------------------------------------------- engines


def _engine(url: str, *, pool_size: int = 2) -> Engine:
    # Small pools on purpose: the whole repository's suites share one PostgreSQL, and a
    # generous pool per package exhausts its connection slots long before any test is slow.
    engine = create_engine(url, future=True, pool_size=pool_size, max_overflow=pool_size)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"PostgreSQL not reachable: {exc}")
    return engine


def _assert_unprivileged(engine: Engine) -> None:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
    assert not row.rolsuper, "worker tests must not run as a superuser"
    assert not row.rolbypassrls, "worker tests must not run as a BYPASSRLS role"


@pytest.fixture(scope="session")
def dwk_admin_engine() -> Engine:
    """Owner connection. Application roles have no DELETE, so teardown needs this."""
    return _engine(ADMIN_URL, pool_size=2)


@pytest.fixture(scope="session")
def dwk_kernel_engine() -> Engine:
    engine = _engine(KERNEL_URL)
    _assert_unprivileged(engine)
    return engine


@pytest.fixture(scope="session")
def dwk_worker_engine() -> Engine:
    engine = _engine(WORKER_URL)
    _assert_unprivileged(engine)
    return engine


# ------------------------------------------------------------------------ settings


@pytest.fixture(scope="session")
def worker_settings() -> WorkerSettings:
    """The configuration every runtime in this suite is built from.

    Constructed from a dictionary, never from ``os.environ``: a developer's real ``.env``
    must not be able to reach a test, and no live key can reach this process.
    ``RECONCILIATION_BACKOFF_SECONDS`` is zero so a follow-up command is immediately
    available and a test can run the next round in the same tick.
    """
    return WorkerSettings(
        PROFILE="development",
        DATABASE_URL_WORKER=WORKER_URL,
        DATABASE_URL_KERNEL=KERNEL_URL,
        RAZORPAY_KEY_ID=TEST_KEY_ID,
        RAZORPAY_KEY_SECRET=TEST_KEY_SECRET,
        RAZORPAY_WEBHOOK_SECRET=TEST_WEBHOOK_SECRET,
        WORKER_ID="dwk-test-worker",
        WORKER_BATCH_SIZE=10,
        RECONCILIATION_BACKOFF_SECONDS=0,
        WORKER_HOUSEKEEPING_SECONDS=0,
    )


@pytest.fixture
def transport() -> FakeTransport:
    """An empty script. A test that expects a provider call must write one."""
    return FakeTransport()


@pytest.fixture
def runtime(
    worker_settings: WorkerSettings,
    transport: FakeTransport,
    dwk_kernel_engine: Engine,
    dwk_worker_engine: Engine,
) -> WorkerRuntime:
    """The worker as the process would assemble it, with a scripted provider.

    Depends on both engine fixtures so that the role assertions run before any test does,
    and so a database that is not reachable skips rather than failing obscurely inside a
    handler.
    """
    assert dwk_kernel_engine is not None and dwk_worker_engine is not None
    return build_runtime(worker_settings, transport=transport)


# -------------------------------------------------------------------- admitted work


@dataclass(frozen=True, slots=True)
class Admitted:
    """One checkout that has been through real kernel admission.

    Everything a handler needs is here, and every value came from the kernel rather than
    from the fixture's imagination: the attempt id and grant id are the admission's, and
    the receipt is the one the kernel minted for provider lookup.
    """

    tenant_id: uuid.UUID
    merchant_id: uuid.UUID
    checkout_id: uuid.UUID
    version: int
    content_hash: str
    attempt_id: uuid.UUID
    grant_id: uuid.UUID
    receipt: str
    amount: Money
    correlation_id: uuid.UUID

    @property
    def notes(self) -> dict[str, str]:
        """The notes a create-order command must carry, per ``CreateOrderCommand``."""
        return {
            "tenant_id": str(self.tenant_id),
            "checkout_id": str(self.checkout_id),
            "payment_attempt_id": str(self.attempt_id),
        }

    def create_order_command(self) -> CreateOrderCommand:
        """The command the API would have enqueued in the admission transaction."""
        return CreateOrderCommand(
            tenant_id=str(self.tenant_id),
            payment_attempt_id=str(self.attempt_id),
            grant_id=str(self.grant_id),
            checkout_id=str(self.checkout_id),
            checkout_version=self.version,
            content_hash=self.content_hash,
            amount_minor=self.amount.minor,
            currency=self.amount.currency,
            receipt=self.receipt,
            notes=self.notes,
            correlation_id=str(self.correlation_id),
        )


def _content(checkout_id: uuid.UUID, version: int, total: Money) -> dict[str, Any]:
    """The canonical checkout payload. Must match ``CurrentMerchantState.content_for_hash``."""
    return {
        "checkout_id": str(checkout_id),
        "version": version,
        "currency": total.currency,
        "total_minor": total.minor,
        "line_items": {"sku_milk": 2, "sku_bread": 1},
        "policy_version": "pol-v12",
    }


class StubMerchant:
    """A merchant whose current state reproduces exactly what was approved.

    The interesting merchant-change cases belong to the admission suite; here the point is
    that admission succeeds so the worker has a real grant to consume.
    """

    def __init__(self, checkout_id: uuid.UUID, total: Money = APPROVED_TOTAL) -> None:
        self._checkout_id = checkout_id
        self.total = total

    def revalidate(
        self, session: Session, *, checkout_id: uuid.UUID, version: int
    ) -> CurrentMerchantState:
        assert session is not None
        content = _content(checkout_id, version, self.total)
        return CurrentMerchantState(
            total=self.total,
            line_items=content["line_items"],
            all_available=True,
            policy_version=content["policy_version"],
        )


@pytest.fixture
def admitted(dwk_admin_engine: Engine, dwk_kernel_engine: Engine) -> Iterator[Admitted]:
    """A tenant, a merchant, an approved checkout version, and one admitted attempt."""
    tenant_id, merchant_id = uuid7(), uuid7()
    checkout_id, version = uuid7(), 1
    correlation_id = uuid7()
    content = _content(checkout_id, version, APPROVED_TOTAL)
    checkout = tk.CheckoutRef(checkout_id, version, canonical_hash(content))

    with dwk_admin_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, home_region) "
                "VALUES (:id, :s, :n, 'asia-south1')"
            ),
            {"id": tenant_id, "s": f"t-{tenant_id.hex}", "n": f"t-{tenant_id.hex}"},
        )
        conn.execute(SET_TENANT, {"t": str(tenant_id)})
        conn.execute(
            text(
                "INSERT INTO merchants (id, tenant_id, slug, name, currency) "
                "VALUES (:id, :t, :s, :n, 'INR')"
            ),
            {
                "id": merchant_id,
                "t": tenant_id,
                "s": f"m-{merchant_id.hex}",
                "n": "Demo Grocery Store",
            },
        )
        conn.execute(
            text(
                "INSERT INTO checkout_versions (id, tenant_id, merchant_id, checkout_id, "
                "version, content, content_hash, currency, total_minor, status, immutable) "
                "VALUES (:id, :t, :m, :c, :v, CAST(:content AS jsonb), :h, 'INR', :total, "
                ":status, true)"
            ),
            {
                "id": uuid7(),
                "t": tenant_id,
                "m": merchant_id,
                "c": checkout_id,
                "v": version,
                "content": json.dumps(content, sort_keys=True),
                "h": checkout.content_hash,
                "total": APPROVED_TOTAL.minor,
                "status": tk.CheckoutState.APPROVAL_REQUIRED.value,
            },
        )

    session = Session(dwk_kernel_engine, expire_on_commit=False)
    with session.begin():
        session.execute(SET_TENANT, {"t": str(tenant_id)})
        issued = receipts.issue_receipt(
            session,
            ReceiptDraft(
                tenant_id=tenant_id,
                merchant_id=merchant_id,
                checkout_id=checkout_id,
                checkout_version=version,
                checkout_hash=checkout.content_hash,
                policies=tuple(
                    MerchantPolicy(
                        kind=kind,
                        policy_id=f"pol-{kind.value.lower()}",
                        policy_version=12,
                        terms={"summary": f"{kind.value} terms"},
                    )
                    for kind in PolicyKind
                ),
                tax_policy_version=3,
                rounding_policy_version=1,
                buyer_visible_refs=(
                    BuyerVisibleRef(
                        label="Refund policy",
                        uri="https://demo.invalid/policies/refund",
                        text_hash=canonical_hash({"policy": "refund", "version": 12}),
                    ),
                ),
                correlation_id=correlation_id,
            ),
        )
        session.execute(
            text(
                "UPDATE checkout_versions SET policy_receipt_id = :rid, "
                "policy_receipt_hash = :rh, status = :s WHERE tenant_id = :t "
                "AND checkout_id = :c AND version = :v"
            ),
            {
                "rid": issued.receipt_id,
                "rh": issued.receipt_hash,
                "s": tk.CheckoutState.APPROVED.value,
                "t": tenant_id,
                "c": checkout_id,
                "v": version,
            },
        )
        reservations.reserve(
            session, checkout_id=checkout_id, checkout_version=version, ttl_seconds=900
        )

    with session.begin():
        session.execute(SET_TENANT, {"t": str(tenant_id)})
        decision = admit(
            session,
            AdmissionRequest(
                tenant_id=tenant_id,
                merchant_id=merchant_id,
                checkout=checkout,
                amount=APPROVED_TOTAL,
                operation=tk.Operation.PAYMENT_CREATE_ORDER,
                idempotency_key=f"idem-{uuid7().hex[:16]}",
                principal=tk.AgentPrincipal(
                    principal_id="buyer-dwk",
                    tenant_id=tenant_id,
                    actor_type=tk.ActorType.BUYER,
                    merchant_id=merchant_id,
                    capabilities=frozenset({"checkout.submit_approved"}),
                ),
                correlation_id=correlation_id,
                approval_id=uuid7(),
            ),
            StubMerchant(checkout_id),
        )
        assert decision.allowed, f"fixture must produce an admissible checkout: {decision}"
        assert decision.grant_id is not None and decision.payment_attempt_id is not None
        attempt = tk.read_attempt(
            session, tenant_id=tenant_id, payment_attempt_id=decision.payment_attempt_id
        )
        assert attempt is not None
        receipt = attempt.receipt
        # The API's submit route moves the version here once admission allows it; the
        # kernel's create-order recording expects EXECUTION_PENDING and would otherwise
        # leave the checkout where it stands. Doing it in the fixture keeps the worker
        # suite talking about the same state machine the demonstration walks through.
        tk.transition(
            session,
            tenant_id=tenant_id,
            checkout=checkout,
            target=tk.CheckoutState.EXECUTION_PENDING,
            reason="admitted",
            actor=tk.ActorType.BUYER,
            correlation_id=correlation_id,
            principal_id="buyer-dwk",
        )
    session.close()

    yield Admitted(
        tenant_id=tenant_id,
        merchant_id=merchant_id,
        checkout_id=checkout_id,
        version=version,
        content_hash=checkout.content_hash,
        attempt_id=decision.payment_attempt_id,
        grant_id=decision.grant_id,
        receipt=receipt,
        amount=APPROVED_TOTAL,
        correlation_id=correlation_id,
    )

    with dwk_admin_engine.begin() as conn:
        conn.execute(SET_TENANT, {"t": str(tenant_id)})
        for table in _TENANT_TABLES:
            # S608: `table` iterates the literal tuple above, never request data, and a
            # SQL identifier cannot be supplied as a bound parameter.
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant_id})  # noqa: S608
        conn.execute(SET_TENANT, {"t": None})
        conn.execute(text("DELETE FROM tenants WHERE id = :i"), {"i": tenant_id})


# ---------------------------------------------------------------------- utilities


@pytest.fixture
def kernel_session(dwk_kernel_engine: Engine) -> Iterator[Callable[[uuid.UUID], Session]]:
    """Open ad-hoc kernel transactions for arranging and asserting.

    Yields a factory rather than a session so a test can open a *fresh* transaction after
    the worker committed, which is the only way to observe what the worker actually made
    durable.
    """
    opened: list[Session] = []

    def factory(tenant_id: uuid.UUID) -> Session:
        session = Session(dwk_kernel_engine, expire_on_commit=False)
        session.begin()
        session.execute(SET_TENANT, {"t": str(tenant_id)})
        opened.append(session)
        return session

    yield factory
    for session in opened:
        try:
            session.rollback()
        finally:
            session.close()


def enqueue(session: Session, command: Any, *, link_grant: uuid.UUID | None = None) -> uuid.UUID:
    """Enqueue a command the way the API does, and link its grant when it carries one."""
    outbox = enqueue_command(session, command, idempotency_key=None)
    if link_grant is not None:
        tk.link_command(
            session,
            tenant_id=uuid.UUID(command.tenant_id),
            grant_id=link_grant,
            outbox_command_id=outbox.command_id,
            correlation_id=uuid.UUID(command.correlation_id),
        )
    return outbox.command_id


def attempt_of(session: Session, admitted: Admitted) -> tk.AttemptView:
    """The payment attempt as the database now holds it. Fails loudly if it vanished."""
    view = tk.read_attempt(
        session, tenant_id=admitted.tenant_id, payment_attempt_id=admitted.attempt_id
    )
    assert view is not None
    return view


def checkout_status(session: Session, admitted: Admitted) -> str:
    """The checkout version's status, which is what the buyer's screen reflects."""
    return str(
        session.execute(
            text(
                "SELECT status FROM checkout_versions "
                "WHERE tenant_id = :t AND checkout_id = :c AND version = :v"
            ),
            {"t": admitted.tenant_id, "c": admitted.checkout_id, "v": admitted.version},
        ).scalar_one()
    )


def invalidate_open_checkout(
    session: Session, admitted: Admitted, *, reason: str = "merchant_state_moved"
) -> None:
    """Move the open checkout to ``INVALIDATED_AWAITING_PAYMENT_RESULT`` and commit.

    Through the kernel rather than an ``UPDATE``, so the arrangement is the same
    transition the scenario controller's ``invalidate-open`` drives and a test cannot set
    up a state the state machine would refuse. The reservation is deliberately kept: a
    capture may still be in flight, and that is the whole reason this state exists.
    """
    tk.invalidate_open(
        session,
        tenant_id=admitted.tenant_id,
        checkout=tk.CheckoutRef(admitted.checkout_id, admitted.version, admitted.content_hash),
        reason=reason,
        correlation_id=admitted.correlation_id,
    )
    session.commit()


def refunds_of(session: Session, admitted: Admitted) -> list[Any]:
    """Every refund row on the attempt, oldest first."""
    return list(
        session.execute(
            text(
                "SELECT id, status, amount_minor, currency, reason_code, idem_key "
                "FROM refunds WHERE tenant_id = :t AND payment_attempt_id = :a "
                "ORDER BY created_at, id"
            ),
            {"t": admitted.tenant_id, "a": admitted.attempt_id},
        ).all()
    )


def outbox_commands(session: Session, tenant_id: uuid.UUID, command_type: str) -> list[uuid.UUID]:
    """The ids of every queued command of one type, oldest first."""
    return [
        row.id
        for row in session.execute(
            text(
                "SELECT id FROM outbox_events WHERE tenant_id = :t AND command_type = :c "
                "ORDER BY created_at, id"
            ),
            {"t": tenant_id, "c": command_type},
        ).all()
    ]


def grant_command_for_refund(
    session: Session, tenant_id: uuid.UUID, refund_id: uuid.UUID
) -> uuid.UUID | None:
    """The outbox command the refund's Execution Grant was linked to."""
    linked: uuid.UUID | None = session.execute(
        text(
            "SELECT outbox_command_id FROM execution_grants WHERE tenant_id = :t AND refund_id = :r"
        ),
        {"t": tenant_id, "r": refund_id},
    ).scalar_one()
    return linked


def outbox_types(session: Session, tenant_id: uuid.UUID) -> list[str]:
    """Every command type currently in the outbox, oldest first."""
    return [
        str(row.command_type)
        for row in session.execute(
            text("SELECT command_type FROM outbox_events WHERE tenant_id = :t ORDER BY created_at"),
            {"t": tenant_id},
        ).all()
    ]


def provider_requests(session: Session, admitted: Admitted) -> list[Any]:
    """Every recorded provider request for the attempt, oldest first."""
    return list(
        session.execute(
            text(
                "SELECT operation, method, url, http_status, outcome_code, grant_id, "
                "transport_error, provider_id FROM provider_requests "
                "WHERE tenant_id = :t AND payment_attempt_id = :a ORDER BY request_at, id"
            ),
            {"t": admitted.tenant_id, "a": admitted.attempt_id},
        ).all()
    )


def audit_types(session: Session, tenant_id: uuid.UUID, aggregate_id: uuid.UUID) -> list[str]:
    """Event types on one aggregate's hash chain, in sequence order."""
    return [
        str(row.event_type)
        for row in session.execute(
            text(
                "SELECT event_type FROM audit_events "
                "WHERE tenant_id = :t AND aggregate_id = :a ORDER BY seq"
            ),
            {"t": tenant_id, "a": aggregate_id},
        ).all()
    ]


def arm_fault(engine: Engine, *, tenant_id: uuid.UUID, kind: str, checkout_id: uuid.UUID) -> None:
    """Arm one scenario fault, in its own committed transaction.

    Takes an engine rather than a session because arming is an **app-role** write (the
    scenario controller lives in the API process) and this package holds no app engine;
    the owner connection stands in. The worker only ever updates the row, which is the
    grant it actually has.
    """
    with engine.begin() as conn:
        conn.execute(SET_TENANT, {"t": str(tenant_id)})
        conn.execute(
            text(
                "INSERT INTO scenario_faults (id, tenant_id, kind, checkout_id, armed) "
                "VALUES (:id, :t, :k, :c, true)"
            ),
            {"id": uuid7(), "t": tenant_id, "k": kind, "c": checkout_id},
        )


def store_webhook(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    body: Mapping[str, Any],
    event_id: str | None = "evt_DWKtest0001",
    signature_verified: bool = True,
) -> uuid.UUID:
    """Write one ``webhook_inbox`` row exactly as the receiver (ADR D7) will.

    The raw bytes are stored, not the parsed body, because the worker re-reads and
    re-parses them: what the signature covered is what gets applied.
    """
    inbox_id = uuid7()
    raw = json.dumps(body, sort_keys=True).encode("utf-8")
    session.execute(
        text(
            "INSERT INTO webhook_inbox (id, tenant_id, dedup_key, provider_event_id, "
            "event_type, body_digest, raw_body, headers_redacted, signature_verified) "
            "VALUES (:id, :t, :dedup, :evt, :type, :digest, :raw, CAST(:headers AS jsonb), :ok)"
        ),
        {
            "id": inbox_id,
            "t": tenant_id,
            "dedup": f"evt:{event_id or inbox_id}",
            "evt": event_id,
            "type": str(body.get("event", "")),
            "digest": canonical_hash({"raw": raw.decode("utf-8")}),
            "raw": raw,
            "headers": json.dumps({"x-razorpay-event-id": event_id}),
            "ok": signature_verified,
        },
    )
    return inbox_id
