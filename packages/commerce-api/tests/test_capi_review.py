"""The three deterministic support services, over HTTP and against real PostgreSQL.

Every scenario here is built by driving the *real* paths: a cart and a checkout over
HTTP, an approval over HTTP, an admission over HTTP, and then the steps the Action Executor
performs through the kernel's own modules -- ``consume_grant``,
``record_create_order_result``, ``apply_provider_evidence``, ``record_refund_result``,
``escalate``, ``escalate_refund``. **No test inserts an ``orders`` or ``refunds`` row.**
Both exist only where the kernel put them, which is the whole reason these projections are
worth reading: a fixture that hand-wrote an order would prove that the code can render a
row somebody typed.

What is proven, in order:

* the routes do not exist in the production profile, refuse without the operator key, and
  refuse without a session -- and **every** route under ``/v1/review`` is a GET, so the
  read-only scope is a structural fact rather than a promise;
* a lost create-order response is a ``PAYMENT_UNKNOWN`` finding with **no verified
  provider state at all**, and the Resolution Service issues no plan for it;
* a clean capture produces an order, a verified provider statement and **no finding**, so
  the queue is not merely reporting everything it sees;
* a capture on an invalidated version is a ``STALE_CAPTURE`` finding, and the plan that
  would settle it cites the Policy-at-Sale Receipt's own refund policy id and version and
  quotes ``captured - reserved`` exactly;
* a refund whose provider answer was lost is its own ``REFUND_UNKNOWN`` finding on its own
  subject, and reserves its amount against the capture ledger;
* a provider that reports more refunded than this platform recorded is a finding, and the
  reverse -- a local ledger legitimately ahead of a stale snapshot -- is not;
* two detectors of one stuck payment yield exactly one case, and the queue says so;
* a case carries its redacted timeline, its proof-chain reference, its verified provider
  state at the moment of escalation, and the options the Resolution Service could and
  could not offer;
* the invariants of specification 6.4.2 are enforced by refusing to construct a plan that
  breaks them, not by describing them.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
import transaction_kernel as tk
from commerce_api.app import create_app
from commerce_api.services import human_review_service as review
from commerce_api.services import reconciliation_service as recon
from commerce_api.services import resolution_service as resolve
from commerce_api.settings import Settings
from commerce_domain import CheckoutRef, Money, RecoveryCode, sha256_hex, uuid7
from fastapi import FastAPI
from fastapi.testclient import TestClient
from platform_db import set_tenant
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session
from transaction_kernel import receipts
from transaction_kernel.payments import ProviderOrderOutcome
from transaction_kernel.refunds import RefundStatus

from conftest import APP_URL, KERNEL_URL, TEST_SCENARIO_KEY, SeededTenant, merchant_refund

pytestmark = pytest.mark.db

MILK = "AMUL-DAIRY-001"

PROD_KEY_ID = "rzp_live_reviewabsentkey"
PROD_KEY_SECRET = "review-absent-api-secret"  # noqa: S105 - fake, never signs anything
PROD_WEBHOOK_SECRET = "review-absent-webhook"  # noqa: S105 - fake, see above

#: Every path this file's router owns. The guard tests walk it, so a route added later
#: without the operator key fails a test rather than shipping open.
REVIEW_ROUTES: tuple[str, ...] = (
    "/v1/review/reconciliation",
    f"/v1/review/reconciliation/{uuid7()}",
    "/v1/review/queue",
    "/v1/review/queue/somekey",
)


# --------------------------------------------------------------------------- helpers


def _key() -> str:
    return f"k-{uuid.uuid4().hex}"


def _headers(**extra: str) -> dict[str, str]:
    return {"Idempotency-Key": _key(), **extra}


@dataclass(frozen=True, slots=True)
class Admitted:
    """An approved checkout with one admitted payment attempt and its unspent grant."""

    checkout_id: uuid.UUID
    version: int
    content_hash: str
    attempt_id: uuid.UUID
    grant_id: uuid.UUID
    amount: Money
    correlation_id: uuid.UUID

    @property
    def ref(self) -> CheckoutRef:
        return CheckoutRef(self.checkout_id, self.version, self.content_hash)


@pytest.fixture
def kernel(
    capi_kernel_engine: Engine,
    seeded_tenant: SeededTenant,  # noqa: ARG001 - ordering: torn down after this session
) -> Iterator[Session]:
    """A kernel-role session for the steps the Action Executor owns.

    Each helper opens its own transaction on it, because the kernel's guards require one
    and because a fixture holding a transaction open across a request would deadlock
    against the locks the API takes. The tenant fixture is depended on so that its
    teardown -- which deletes every row this session wrote -- runs after this one closes.
    """
    session = Session(capi_kernel_engine, expire_on_commit=False)
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def operator(auth_client: TestClient, scenario_headers: dict[str, str]) -> Callable[..., Any]:
    """Call a review route with both credentials: the session and the operator key."""

    def _call(path: str, **kwargs: Any) -> Any:
        headers = {**scenario_headers, **kwargs.pop("headers", {})}
        return auth_client.get(path, headers=headers, **kwargs)

    return _call


def _admit(auth_client: TestClient) -> Admitted:
    """Cart, checkout, approval and admission, all over HTTP.

    Nothing here is arranged in SQL. The attempt this returns is the one the kernel
    admitted for a version this buyer actually approved, so every projection built on it
    is describing the production path.
    """
    cart = auth_client.post("/v1/carts", headers=_headers())
    assert cart.status_code == 201, cart.text
    cart_id = cart.json()["cart_id"]

    line = auth_client.put(
        f"/v1/carts/{cart_id}/lines/{MILK}", json={"quantity": 2}, headers=_headers()
    )
    assert line.status_code == 200, line.text

    card = auth_client.post(f"/v1/carts/{cart_id}/checkout", headers=_headers())
    assert card.status_code == 201, card.text
    body = card.json()

    approved = auth_client.post(
        f"/v1/checkouts/{body['checkout_id']}/versions/{body['version']}/approve",
        json={
            "content_hash": body["content_hash"],
            "amount_minor": body["amount_minor"],
            "currency": body["currency"],
        },
        headers=_headers(),
    )
    assert approved.status_code == 200, approved.text

    submitted = auth_client.post(
        f"/v1/checkouts/{body['checkout_id']}/versions/{body['version']}/submit",
        headers=_headers(),
    )
    assert submitted.status_code == 200, submitted.text
    decision = submitted.json()
    assert decision["allowed"], decision

    return Admitted(
        checkout_id=uuid.UUID(body["checkout_id"]),
        version=int(body["version"]),
        content_hash=str(body["content_hash"]),
        attempt_id=uuid.UUID(decision["payment_attempt_id"]),
        grant_id=uuid.UUID(decision["grant_id"]),
        amount=Money(int(body["amount_minor"]), str(body["currency"])),
        correlation_id=uuid.UUID(decision["correlation_id"]),
    )


@pytest.fixture
def admitted(auth_client: TestClient) -> Admitted:
    """One approved, admitted checkout. Tests that need two call :func:`_admit` again."""
    return _admit(auth_client)


# ------------------------------------------------------- the Action Executor's own steps


def _spend_grant(kernel: Session, tenant_id: uuid.UUID, adm: Admitted) -> None:
    """What the worker does before its first provider call: consume the single-use grant."""
    with kernel.begin():
        set_tenant(kernel, tenant_id)
        tk.consume_grant(
            kernel,
            adm.grant_id,
            tk.GrantBinding(
                tenant_id=tenant_id,
                checkout=adm.ref,
                payment_attempt_id=adm.attempt_id,
                operation=tk.Operation.PAYMENT_CREATE_ORDER,
                amount=adm.amount,
            ),
        )


def _create_order(kernel: Session, tenant_id: uuid.UUID, adm: Admitted) -> str:
    """A create-order call that came back. The attempt becomes ``SUBMITTED``."""
    _spend_grant(kernel, tenant_id, adm)
    provider_order_id = f"order_{uuid.uuid4().hex[:14]}"
    with kernel.begin():
        set_tenant(kernel, tenant_id)
        tk.record_create_order_result(
            kernel,
            tenant_id=tenant_id,
            payment_attempt_id=adm.attempt_id,
            outcome=ProviderOrderOutcome(
                kind="ok",
                provider_order_id=provider_order_id,
                code=RecoveryCode.OK,
                reason="created",
            ),
            correlation_id=adm.correlation_id,
        )
    return provider_order_id


def _lost_create_order(kernel: Session, tenant_id: uuid.UUID, adm: Admitted) -> None:
    """A create-order call whose response was lost. The attempt becomes ``UNKNOWN``."""
    _spend_grant(kernel, tenant_id, adm)
    with kernel.begin():
        set_tenant(kernel, tenant_id)
        tk.record_create_order_result(
            kernel,
            tenant_id=tenant_id,
            payment_attempt_id=adm.attempt_id,
            outcome=ProviderOrderOutcome(
                kind="unknown",
                provider_order_id=None,
                code=RecoveryCode.PAYMENT_UNKNOWN,
                reason="transport_timeout",
            ),
            correlation_id=adm.correlation_id,
        )


def _apply_capture(
    kernel: Session,
    tenant_id: uuid.UUID,
    adm: Admitted,
    provider_order_id: str,
    *,
    amount_refunded_minor: int = 0,
) -> str:
    """Apply a server-side fetch that reports a capture. The kernel writes the order."""
    payment_id = f"pay_{uuid.uuid4().hex[:14]}"
    with kernel.begin():
        set_tenant(kernel, tenant_id)
        tk.apply_provider_evidence(
            kernel,
            tenant_id=tenant_id,
            payment_attempt_id=adm.attempt_id,
            evidence=tk.ProviderEvidence.from_mapping(
                {
                    "source": "PROVIDER_FETCH",
                    "provider_payment_id": payment_id,
                    "provider_order_id": provider_order_id,
                    "amount_minor": adm.amount.minor,
                    "currency": adm.amount.currency,
                    "status": "captured",
                    "provider_status": "captured",
                    "amount_refunded_minor": amount_refunded_minor,
                    "raw_digest": sha256_hex(payment_id.encode()),
                }
            ),
            correlation_id=adm.correlation_id,
        )
    return payment_id


def _invalidate(kernel: Session, tenant_id: uuid.UUID, adm: Admitted) -> None:
    """Merchant state moved while the payment surface was open (specification 10.8)."""
    with kernel.begin():
        set_tenant(kernel, tenant_id)
        tk.invalidate_open(
            kernel,
            tenant_id=tenant_id,
            checkout=adm.ref,
            reason="price_changed_under_open_payment",
            correlation_id=adm.correlation_id,
        )


def _escalate(
    kernel: Session, tenant_id: uuid.UUID, adm: Admitted, reason: str
) -> tk.payments.Escalation:
    with kernel.begin():
        set_tenant(kernel, tenant_id)
        return tk.escalate(
            kernel,
            tenant_id=tenant_id,
            payment_attempt_id=adm.attempt_id,
            reason=reason,
            correlation_id=adm.correlation_id,
        )


def _order_id(auth_client: TestClient, checkout_id: uuid.UUID) -> str:
    response = auth_client.get("/v1/orders")
    assert response.status_code == 200, response.text
    for order in response.json()["orders"]:
        if order["checkout_id"] == str(checkout_id):
            return str(order["order_id"])
    raise AssertionError(f"no order for checkout {checkout_id}: {response.text}")


def _request_refund(auth_client: TestClient, order_id: str, minor: int | None = None) -> str:
    body: dict[str, Any] = {"reason": "items_missing"}
    if minor is not None:
        body["amount_minor"] = minor
    response = merchant_refund(auth_client, order_id, body=body, headers=_headers())
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["decision"]["allowed"], payload
    return str(payload["refund"]["refund_id"])


def _lose_refund_result(
    kernel: Session, tenant_id: uuid.UUID, refund_id: str, correlation_id: uuid.UUID
) -> None:
    """The refund was sent and the answer never came back. ``REFUND_UNKNOWN``."""
    with kernel.begin():
        set_tenant(kernel, tenant_id)
        tk.refunds.record_refund_result(
            kernel,
            tenant_id=tenant_id,
            refund_id=uuid.UUID(refund_id),
            outcome="unknown",
            provider_refund_id=None,
            correlation_id=correlation_id,
        )


def _finding(payload: dict[str, Any], code: str) -> dict[str, Any]:
    matches = [item for item in payload["findings"] if item["code"] == code]
    assert len(matches) == 1, f"expected exactly one {code} finding, got {payload['findings']}"
    return matches[0]


def _attempt_of(page: dict[str, Any], attempt_id: uuid.UUID) -> dict[str, Any]:
    for item in page["attempts"]:
        if item["payment_attempt_id"] == str(attempt_id):
            return item
    raise AssertionError(f"attempt {attempt_id} absent from {page}")


# ------------------------------------------------------------------------ the guards


def test_review_routes_do_not_exist_in_the_production_profile() -> None:
    """404 on every path, with no session and no key.

    The routes are absent rather than locked, the same answer the scenario apparatus
    gives: a 401 would tell an unauthenticated caller that an operator queue exists here.
    """
    production = create_app(
        Settings(
            PROFILE="production",
            DATABASE_URL_APP=APP_URL,
            DATABASE_URL_KERNEL=KERNEL_URL,
            RAZORPAY_KEY_ID=PROD_KEY_ID,
            RAZORPAY_KEY_SECRET=PROD_KEY_SECRET,
            RAZORPAY_WEBHOOK_SECRET=PROD_WEBHOOK_SECRET,
            RAZORPAY_PRODUCTION_APPROVAL_REF="CHANGE-0000",
            SCENARIO_KEY=TEST_SCENARIO_KEY,
        )
    )
    with TestClient(production) as client:
        for path in REVIEW_ROUTES:
            response = client.get(path)
            assert response.status_code == 404, f"{path} -> {response.status_code}"
            assert response.headers["content-type"].startswith("application/problem+json")


def test_review_routes_refuse_a_valid_session_without_the_operator_key(
    auth_client: TestClient,
) -> None:
    """401 with a real buyer session and no key. Nothing here is buyer-facing."""
    for path in REVIEW_ROUTES:
        assert auth_client.get(path).status_code == 401, path


def test_review_routes_refuse_the_operator_key_without_a_session(
    client: TestClient, scenario_headers: dict[str, str]
) -> None:
    """401 with the key and no bearer token: the key says who may, the session says whose."""
    for path in REVIEW_ROUTES:
        assert client.get(path, headers=scenario_headers).status_code == 401, path


def test_every_review_route_is_a_read(api_app: FastAPI) -> None:
    """The read-only scope is structural, not a promise in a docstring.

    P0's human review is the queue and its evidence. A POST appearing under this prefix --
    an assign, a decision, a resolve -- fails here rather than shipping a control that
    implies a workflow the platform does not have.
    """
    for route in api_app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/v1/review"):
            continue
        methods = set(getattr(route, "methods", set()))
        assert methods <= {"GET", "HEAD"}, f"{path} exposes {sorted(methods)}"


# --------------------------------------------------------- a lost create-order response


def test_a_lost_create_order_is_payment_unknown_with_no_provider_statement(
    operator: Callable[..., Any],
    kernel: Session,
    seeded_tenant: SeededTenant,
    admitted: Admitted,
) -> None:
    """``UNKNOWN`` is reported as unknown, and nothing fills the provider's silence in."""
    _lost_create_order(kernel, seeded_tenant.tenant_id, admitted)

    page = operator("/v1/review/reconciliation", params={"unresolved_only": True}).json()
    attempt = _attempt_of(page, admitted.attempt_id)

    assert attempt["recorded_state"] == "UNKNOWN"
    # The provider never answered, so no field pretends it did.
    assert attempt["verified"]["present"] is False
    assert attempt["verified"]["status"] is None
    assert attempt["verified"]["amount_refunded_minor"] is None
    assert attempt["order_id"] is None
    assert attempt["attempts_used"] == 0
    assert attempt["attempts_remaining"] == recon.ATTEMPT_BOUND

    finding = _finding(attempt, "PAYMENT_UNKNOWN")
    assert finding["subject"] == "PAYMENT_ATTEMPT"
    assert finding["provider_state"] is None
    assert finding["exposure"]["minor"] == admitted.amount.minor
    assert page["finding_counts"]["PAYMENT_UNKNOWN"] == 1


def test_no_plan_is_issued_while_the_payment_outcome_is_unverified(
    operator: Callable[..., Any],
    kernel: Session,
    seeded_tenant: SeededTenant,
    admitted: Admitted,
) -> None:
    """Specification 6.4.2: an unknown payment state returns a code and issues no plan.

    The withheld list is the other half of the answer -- every outcome, and the reason
    none of them can be priced -- because an empty options list on its own tells a
    reviewer that nothing was considered.
    """
    _lost_create_order(kernel, seeded_tenant.tenant_id, admitted)

    detail = operator(f"/v1/review/reconciliation/{admitted.attempt_id}").json()
    assert len(detail["resolutions"]) == 1
    resolution = detail["resolutions"][0]

    assert resolution["code"] == "PAYMENT_UNKNOWN"
    assert resolution["plan_issued"] is False
    assert resolution["plan_id"] is None
    assert resolution["valid_until"] is None
    assert resolution["options"] == []
    assert resolution["recorded"] is False
    assert {item["outcome"] for item in resolution["withheld"]} == {
        outcome.value for outcome in resolve.Outcome
    }
    assert {item["reason"] for item in resolution["withheld"]} == {"PROVIDER_STATE_UNVERIFIED"}


# ------------------------------------------------------------------- a clean capture


def test_a_verified_capture_produces_an_order_and_no_finding(
    operator: Callable[..., Any],
    kernel: Session,
    seeded_tenant: SeededTenant,
    admitted: Admitted,
) -> None:
    """The service reports a divergence, not every attempt it can see."""
    provider_order_id = _create_order(kernel, seeded_tenant.tenant_id, admitted)
    payment_id = _apply_capture(kernel, seeded_tenant.tenant_id, admitted, provider_order_id)

    detail = operator(f"/v1/review/reconciliation/{admitted.attempt_id}").json()
    attempt = detail["attempt"]

    assert attempt["recorded_state"] == "CAPTURED"
    assert attempt["verified"]["present"] is True
    assert attempt["verified"]["source"] == "PROVIDER_FETCH"
    assert attempt["verified"]["status"] == "captured"
    assert attempt["verified"]["provider_payment_id"] == payment_id
    assert attempt["verified"]["provider_order_id"] == provider_order_id
    assert attempt["order_id"] is not None
    assert attempt["findings"] == []
    assert detail["resolutions"] == []

    unresolved = operator("/v1/review/reconciliation", params={"unresolved_only": True}).json()
    assert unresolved["attempts"] == []


def test_provider_evidence_is_joined_to_the_attempt_it_names(
    operator: Callable[..., Any],
    auth_client: TestClient,
    kernel: Session,
    seeded_tenant: SeededTenant,
    admitted: Admitted,
) -> None:
    """Evidence is joined by provider order id, and the response shows which one.

    One captured attempt and one whose create-order response was lost, live in the same
    tenant at the same moment. The unknown one stays unknown, and the captured one's
    statement names its own provider order -- the identifier the kernel cross-checked
    before applying it. A projection that joined on the tenant or on the checkout would
    hand one attempt's capture to the other, which is settling one checkout with another
    checkout's money, made in a reviewer's head instead of in the ledger.
    """
    provider_order_id = _create_order(kernel, seeded_tenant.tenant_id, admitted)
    _apply_capture(kernel, seeded_tenant.tenant_id, admitted, provider_order_id)

    other = _admit(auth_client)
    _lost_create_order(kernel, seeded_tenant.tenant_id, other)

    page = operator("/v1/review/reconciliation").json()
    captured = _attempt_of(page, admitted.attempt_id)
    unknown = _attempt_of(page, other.attempt_id)

    assert captured["verified"]["present"] is True
    # The statement names the attempt's own provider order, not merely "some order".
    assert captured["verified"]["provider_order_id"] == captured["provider_order_id"]
    assert unknown["verified"]["present"] is False
    assert unknown["provider_order_id"] is None
    assert [item["code"] for item in unknown["findings"]] == ["PAYMENT_UNKNOWN"]
    assert captured["findings"] == []


# ------------------------------------------------------------------- a stale capture


def test_a_stale_capture_yields_a_full_refund_citing_the_at_sale_policy(
    operator: Callable[..., Any],
    kernel: Session,
    seeded_tenant: SeededTenant,
    admitted: Admitted,
    capi_app_engine: Engine,
) -> None:
    """Money captured against an invalidated version is owed back, under the frozen rule.

    The amount is ``captured - reserved`` over integers the kernel wrote, and the citation
    is the receipt's own policy id and version -- read back here from the receipt itself,
    so the test would fail if the plan quoted the merchant's current policy instead.
    """
    provider_order_id = _create_order(kernel, seeded_tenant.tenant_id, admitted)
    _invalidate(kernel, seeded_tenant.tenant_id, admitted)
    _apply_capture(kernel, seeded_tenant.tenant_id, admitted, provider_order_id)

    detail = operator(f"/v1/review/reconciliation/{admitted.attempt_id}").json()
    attempt = detail["attempt"]

    assert attempt["recorded_state"] == "STALE_CAPTURE"
    # 10.8: no order is confirmed for a capture that can never be fulfilled.
    assert attempt["order_id"] is None
    finding = _finding(attempt, "STALE_CAPTURE")
    assert finding["exposure"]["minor"] == admitted.amount.minor

    resolution = next(
        item for item in detail["resolutions"] if item["finding_id"] == finding["finding_id"]
    )
    assert resolution["code"] == "RESOLUTION_PLAN_ISSUED"
    assert resolution["plan_issued"] is True
    assert resolution["plan_id"]
    assert resolution["valid_until"] is not None
    # No resolution_plans row exists in P0, and the response says so rather than implying
    # that something could be confirmed against this identifier.
    assert resolution["recorded"] is False
    assert resolution["captured_minor"] == admitted.amount.minor
    assert resolution["refunds_reserved_minor"] == 0
    assert resolution["refundable_minor"] == admitted.amount.minor

    assert len(resolution["options"]) == 1
    option = resolution["options"][0]
    assert option["outcome"] == "REFUND_FULL"
    assert option["amount"]["minor"] == admitted.amount.minor
    assert option["confirmation"] == "OPERATOR_APPROVAL"

    expected = _at_sale_refund_policy(capi_app_engine, seeded_tenant.tenant_id, admitted)
    assert option["policy_id"] == expected["policy_id"]
    assert option["policy_version"] == expected["policy_version"]

    withheld = {item["outcome"]: item["reason"] for item in resolution["withheld"]}
    assert withheld["ORDER_CANCEL"] == "MONEY_ALREADY_CAPTURED"
    assert withheld["STORE_CREDIT"] == "NOT_RECORDED_AT_SALE"
    assert withheld["REFUND_PARTIAL"] == "REQUIRES_A_REQUESTED_AMOUNT"


def test_a_plan_identifier_is_stable_while_its_inputs_are(
    operator: Callable[..., Any],
    kernel: Session,
    seeded_tenant: SeededTenant,
    admitted: Admitted,
) -> None:
    """Two evaluations of unchanged state name the same plan.

    That is what immutability can mean with no ``resolution_plans`` row to write: an id
    that still matches is a plan whose ledger and receipt have not moved underneath it.
    """
    provider_order_id = _create_order(kernel, seeded_tenant.tenant_id, admitted)
    _invalidate(kernel, seeded_tenant.tenant_id, admitted)
    _apply_capture(kernel, seeded_tenant.tenant_id, admitted, provider_order_id)

    first = operator(f"/v1/review/reconciliation/{admitted.attempt_id}").json()
    second = operator(f"/v1/review/reconciliation/{admitted.attempt_id}").json()
    assert first["resolutions"][0]["plan_id"] == second["resolutions"][0]["plan_id"]
    assert (
        first["attempt"]["findings"][0]["finding_id"]
        == (second["attempt"]["findings"][0]["finding_id"])
    )


def _at_sale_refund_policy(engine: Engine, tenant_id: uuid.UUID, adm: Admitted) -> dict[str, Any]:
    """The refund policy frozen on this sale, read from the receipt rather than assumed."""
    session = Session(engine, expire_on_commit=False)
    try:
        with session.begin():
            set_tenant(session, tenant_id)
            policy = receipts.policy_for_order(session, adm.ref)
            assert policy.ok, policy.reason
            recorded = policy.policy_for(receipts.PolicyKind.REFUND)
            return {
                "policy_id": recorded["policy_id"],
                "policy_version": recorded["policy_version"],
            }
    finally:
        session.close()


# ----------------------------------------------------------------- a lost refund answer


def test_a_lost_refund_answer_is_its_own_finding_on_its_own_subject(
    operator: Callable[..., Any],
    auth_client: TestClient,
    kernel: Session,
    seeded_tenant: SeededTenant,
    admitted: Admitted,
) -> None:
    """``REFUND_UNKNOWN`` belongs to the refund, and reserves its amount against the capture.

    A refund that may already exist at the provider keeps its money reserved: that is what
    stops a replacement refund from paying the buyer twice, and the projection reports the
    reservation rather than only the settled total.
    """
    provider_order_id = _create_order(kernel, seeded_tenant.tenant_id, admitted)
    _apply_capture(kernel, seeded_tenant.tenant_id, admitted, provider_order_id)
    order_id = _order_id(auth_client, admitted.checkout_id)
    part = admitted.amount.minor // 4
    refund_id = _request_refund(auth_client, order_id, part)
    _lose_refund_result(kernel, seeded_tenant.tenant_id, refund_id, admitted.correlation_id)

    detail = operator(f"/v1/review/reconciliation/{admitted.attempt_id}").json()
    attempt = detail["attempt"]

    assert attempt["recorded_state"] == "REFUND_UNKNOWN"
    assert attempt["refunds_reserved_minor"] == part
    assert attempt["refunds_settled_minor"] == 0
    refund_row = attempt["refunds"][0]
    assert refund_row["status"] == RefundStatus.UNKNOWN.value
    assert refund_row["reserves_money"] is True

    finding = _finding(attempt, "REFUND_UNKNOWN")
    assert finding["subject"] == "REFUND"
    assert finding["refund_id"] == refund_id
    assert finding["exposure"]["minor"] == part

    resolution = next(
        item for item in detail["resolutions"] if item["finding_id"] == finding["finding_id"]
    )
    assert resolution["code"] == "REFUND_REVIEW_REQUIRED"
    assert resolution["plan_id"] is None
    assert resolution["options"] == []
    # captured - reserved, over integers the kernel wrote, and nothing else.
    assert resolution["refundable_minor"] == admitted.amount.minor - part


def test_a_settled_refund_does_not_look_like_a_provider_gap(
    operator: Callable[..., Any],
    auth_client: TestClient,
    kernel: Session,
    seeded_tenant: SeededTenant,
    admitted: Admitted,
) -> None:
    """A local ledger legitimately ahead of a stale provider snapshot is not a finding.

    The provider's refunded total is recorded when evidence is applied and does not move
    afterwards, so a refund admitted later leaves the platform's figure higher. Reporting
    that as a divergence would raise a finding on every refund in flight.
    """
    provider_order_id = _create_order(kernel, seeded_tenant.tenant_id, admitted)
    _apply_capture(
        kernel, seeded_tenant.tenant_id, admitted, provider_order_id, amount_refunded_minor=0
    )
    order_id = _order_id(auth_client, admitted.checkout_id)
    _request_refund(auth_client, order_id, admitted.amount.minor // 4)

    attempt = operator(f"/v1/review/reconciliation/{admitted.attempt_id}").json()["attempt"]
    assert attempt["verified"]["amount_refunded_minor"] == 0
    assert attempt["refunds_reserved_minor"] > 0
    assert [item["code"] for item in attempt["findings"]] == []


def test_a_provider_refund_the_platform_never_recorded_is_reported(
    operator: Callable[..., Any],
    kernel: Session,
    seeded_tenant: SeededTenant,
    admitted: Admitted,
) -> None:
    """Money went back that nothing here initiated, so the difference is the finding."""
    returned = admitted.amount.minor // 5
    provider_order_id = _create_order(kernel, seeded_tenant.tenant_id, admitted)
    _apply_capture(
        kernel,
        seeded_tenant.tenant_id,
        admitted,
        provider_order_id,
        amount_refunded_minor=returned,
    )

    attempt = operator(f"/v1/review/reconciliation/{admitted.attempt_id}").json()["attempt"]
    assert attempt["verified"]["amount_refunded_minor"] == returned
    assert attempt["refunds_reserved_minor"] == 0
    finding = _finding(attempt, "PROVIDER_REFUND_UNRECORDED")
    assert finding["exposure"]["minor"] == returned


# ------------------------------------------------------------------ the review queue


def test_two_detectors_of_one_stuck_payment_open_exactly_one_case(
    operator: Callable[..., Any],
    kernel: Session,
    seeded_tenant: SeededTenant,
    admitted: Admitted,
) -> None:
    """Specification 6.4.3's exactly-once rule, asserted rather than trusted.

    A reviewer must not work the same evidence twice, so the second escalation under the
    same reason family finds the case already open and returns the same key. The queue
    reports how many events carry that key, which is what makes the guarantee visible
    instead of merely true.
    """
    _create_order(kernel, seeded_tenant.tenant_id, admitted)
    first = _escalate(kernel, seeded_tenant.tenant_id, admitted, "reconciliation_exhausted.test")
    second = _escalate(kernel, seeded_tenant.tenant_id, admitted, "reconciliation_exhausted.test")

    assert first.opened is True
    assert second.opened is False
    assert first.case_key == second.case_key

    body = operator("/v1/review/queue").json()
    assert len(body["cases"]) == 1
    case = body["cases"][0]
    assert case["case_key"] == first.case_key
    assert case["detections"] == 1
    assert case["state"] == "AWAITING_HUMAN"
    assert case["reason_code"] == "HUMAN_REVIEW_REQUIRED"
    assert case["reason_family"] == "reconciliation_exhausted.test"
    assert case["payment_attempt_id"] == str(admitted.attempt_id)
    assert case["monetary_exposure"]["minor"] == admitted.amount.minor
    # No verified provider statement exists, so this is the case that most needs a person.
    assert case["priority"] == "P1"
    assert case["audit_self_hash"]
    assert case["proof_chain"]["href"].startswith(f"/v1/checkouts/{admitted.checkout_id}/proof")
    assert body["priority_counts"]["P1"] == 1


def test_the_queue_states_that_a_reviewer_acts_elsewhere(
    operator: Callable[..., Any],
    kernel: Session,
    seeded_tenant: SeededTenant,
    admitted: Admitted,
) -> None:
    """The limit travels with the data: no case carries an action field of any kind."""
    _create_order(kernel, seeded_tenant.tenant_id, admitted)
    _escalate(kernel, seeded_tenant.tenant_id, admitted, "evidence_mismatch")

    body = operator("/v1/review/queue").json()
    assert body["scope"] == review.SCOPE_NOTE
    assert "No case is assigned, decided, annotated or resolved here" in body["scope"]
    forbidden = {"assigned_reviewer", "decision", "reviewer_reason", "approved_action", "actions"}
    for case in body["cases"]:
        assert forbidden.isdisjoint(case.keys()), case.keys()


def test_a_case_carries_its_timeline_proof_reference_and_withheld_options(
    operator: Callable[..., Any],
    kernel: Session,
    seeded_tenant: SeededTenant,
    admitted: Admitted,
) -> None:
    """Everything specification 6.4.3 asks a reviewer to be handed, and nothing invented."""
    _create_order(kernel, seeded_tenant.tenant_id, admitted)
    escalation = _escalate(
        kernel, seeded_tenant.tenant_id, admitted, "reconciliation_exhausted.test"
    )

    detail = operator(f"/v1/review/queue/{escalation.case_key}").json()
    assert detail["case"]["case_key"] == escalation.case_key
    assert detail["scope"] == review.SCOPE_NOTE

    actions = {entry["action"] for entry in detail["timeline"]}
    assert "human_review.opened" in actions
    assert "admission.allowed" in actions
    assert detail["timeline"], "a case with no timeline is evidence of nothing"

    # Nothing was ever verified with the provider, so the case says so rather than
    # showing a state somebody could read as a settlement.
    assert detail["verified_provider_state"]["present"] is False
    assert detail["refused_evidence"] is None

    assert detail["attempt"]["recorded_state"] == "ESCALATED"
    assert detail["attempt"]["reason_family"] == "reconciliation_exhausted.test"
    resolution = next(
        item for item in detail["resolutions"] if item["code"] == "HUMAN_REVIEW_REQUIRED"
    )
    assert resolution["plan_id"] is None
    assert {item["reason"] for item in resolution["withheld"]} == {"ESCALATED_TO_HUMAN"}


def test_a_refund_escalation_reaches_the_same_queue_from_the_other_stream(
    operator: Callable[..., Any],
    auth_client: TestClient,
    kernel: Session,
    seeded_tenant: SeededTenant,
    admitted: Admitted,
) -> None:
    """A refund's case is appended to the attempt's stream and must still be listed.

    The payment path writes its case onto the checkout's stream and the refund path onto
    the attempt's, because a refund is an operation on an attempt. A queue that read one
    stream would silently lose half the cases.
    """
    provider_order_id = _create_order(kernel, seeded_tenant.tenant_id, admitted)
    _apply_capture(kernel, seeded_tenant.tenant_id, admitted, provider_order_id)
    order_id = _order_id(auth_client, admitted.checkout_id)
    part = admitted.amount.minor // 4
    refund_id = _request_refund(auth_client, order_id, part)
    _lose_refund_result(kernel, seeded_tenant.tenant_id, refund_id, admitted.correlation_id)

    with kernel.begin():
        set_tenant(kernel, seeded_tenant.tenant_id)
        escalation = tk.refunds.escalate_refund(
            kernel,
            tenant_id=seeded_tenant.tenant_id,
            refund_id=uuid.UUID(refund_id),
            reason_family="refund_unresolved",
            correlation_id=admitted.correlation_id,
            attempts=recon.ATTEMPT_BOUND,
        )
    assert escalation.opened is True

    body = operator("/v1/review/queue").json()
    case = next(item for item in body["cases"] if item["case_key"] == escalation.case_key)
    assert case["audit_aggregate_type"] == "payment_attempt"
    assert case["refund_id"] == refund_id
    assert case["payment_attempt_id"] == str(admitted.attempt_id)
    # The refund path records its exposure as a Money, which the audit canonicaliser
    # expands; the payment path records two flat fields. Both must read the same way here.
    assert case["monetary_exposure"]["minor"] == part
    assert case["attempts_used"] == recon.ATTEMPT_BOUND
    # A verified capture exists for this attempt, so this case is not the P1 shape.
    assert case["priority"] == "P2"

    detail = operator(f"/v1/review/queue/{escalation.case_key}").json()
    assert detail["verified_provider_state"]["present"] is True
    assert detail["verified_provider_state"]["status"] == "captured"


def test_an_unknown_case_key_is_a_404(operator: Callable[..., Any]) -> None:
    """A case key is a hash, so a distinct refusal would let somebody probe for one."""
    response = operator("/v1/review/queue/not-a-real-case-key")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


def test_an_unknown_attempt_is_a_404(operator: Callable[..., Any]) -> None:
    response = operator(f"/v1/review/reconciliation/{uuid7()}")
    assert response.status_code == 404


# ------------------------------------------------------- the invariants, as guards


def _resolution(**overrides: Any) -> resolve.Resolution:
    """A minimal valid resolution, so each invariant test changes exactly one thing."""
    base: dict[str, Any] = {
        "finding_id": "f",
        "code": RecoveryCode.RESOLUTION_PLAN_ISSUED,
        "plan_id": "p",
        "options": (
            resolve.PlanOption(
                outcome=resolve.Outcome.REFUND_FULL,
                amount=Money(1000, "INR"),
                policy_kind=receipts.PolicyKind.REFUND,
                policy_id="demo/refund",
                policy_version=1,
                confirmation=resolve.Confirmation.OPERATOR_APPROVAL,
                basis="captured minus reserved",
            ),
        ),
        "withheld": (),
        "payment_attempt_id": uuid7(),
        "checkout_id": uuid7(),
        "refund_id": None,
        "policy_receipt_id": None,
        "policy_receipt_hash": None,
        "policy_binding": "OK",
        "captured_minor": 1000,
        "refunds_reserved_minor": 0,
        "refundable_minor": 1000,
        "currency": "INR",
        "evaluated_at": _fixed_moment(),
        "valid_until": _fixed_moment(),
        "explanation": "for the guard tests",
    }
    return resolve.Resolution(**{**base, **overrides})


def _fixed_moment() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def test_a_plan_may_never_offer_more_than_the_capture_supports() -> None:
    """The 6.4.2 ledger invariant, enforced by refusing to build the object.

    Raised rather than returned: a plan that offers more than ``captured - reserved`` is
    not something a caller should be able to receive, log and put in front of a reviewer.
    """
    with pytest.raises(resolve.ResolutionError, match="refundable"):
        _resolution(refunds_reserved_minor=600, refundable_minor=400)


def test_store_credit_is_never_offered_without_cash_beside_it() -> None:
    """Cash stays available whenever store credit is offered, so the guard says so."""
    credit = resolve.PlanOption(
        outcome=resolve.Outcome.STORE_CREDIT,
        amount=Money(1000, "INR"),
        policy_kind=receipts.PolicyKind.REFUND,
        policy_id="demo/refund",
        policy_version=1,
        confirmation=resolve.Confirmation.BUYER_APPROVAL,
        basis="captured minus reserved",
    )
    with pytest.raises(resolve.ResolutionError, match="store credit"):
        _resolution(options=(credit,))


def test_a_refusal_may_not_smuggle_a_plan() -> None:
    """A code that issues no plan carries no options and no plan id."""
    with pytest.raises(resolve.ResolutionError, match="issues no plan"):
        _resolution(code=RecoveryCode.PAYMENT_UNKNOWN)


def test_a_resolution_refuses_a_projection_about_another_attempt(
    kernel: Session,
    seeded_tenant: SeededTenant,
    admitted: Admitted,
    capi_app_engine: Engine,
) -> None:
    """Resolving one checkout against another's money is refused, not tolerated."""
    _lost_create_order(kernel, seeded_tenant.tenant_id, admitted)
    session = Session(capi_app_engine, expire_on_commit=False)
    try:
        with session.begin():
            set_tenant(session, seeded_tenant.tenant_id)
            projection = recon.project(
                session,
                tenant_id=seeded_tenant.tenant_id,
                payment_attempt_id=admitted.attempt_id,
            )
            assert projection is not None
            finding = projection.findings[0]
            stranger = recon.Finding(
                finding_id=finding.finding_id,
                code=finding.code,
                subject=finding.subject,
                payment_attempt_id=uuid7(),
                checkout_id=finding.checkout_id,
                checkout_version=finding.checkout_version,
                refund_id=None,
                recorded_state=finding.recorded_state,
                provider_state=None,
                reason_family=None,
                exposure=finding.exposure,
                detected_at=finding.detected_at,
                correlation_id=None,
                detail=finding.detail,
            )
            with pytest.raises(resolve.ResolutionError, match="does not describe"):
                resolve.evaluate(
                    session,
                    tenant_id=seeded_tenant.tenant_id,
                    finding=stranger,
                    projection=projection,
                )
    finally:
        session.close()


def test_the_survey_is_scoped_to_one_checkout_when_asked(
    operator: Callable[..., Any],
    kernel: Session,
    seeded_tenant: SeededTenant,
    admitted: Admitted,
) -> None:
    """The narrowing parameter narrows, and an unrelated checkout returns nothing."""
    _lost_create_order(kernel, seeded_tenant.tenant_id, admitted)

    mine = operator(
        "/v1/review/reconciliation", params={"checkout_id": str(admitted.checkout_id)}
    ).json()
    assert [item["payment_attempt_id"] for item in mine["attempts"]] == [str(admitted.attempt_id)]

    other = operator("/v1/review/reconciliation", params={"checkout_id": str(uuid7())}).json()
    assert other["attempts"] == []


def test_the_list_view_omits_the_rounds_the_detail_view_carries(
    operator: Callable[..., Any],
    kernel: Session,
    seeded_tenant: SeededTenant,
    admitted: Admitted,
) -> None:
    """One reconciliation round, recorded by the kernel, reaches the detail view.

    The list view leaves the rounds out on purpose: a survey of fifty attempts would carry
    hundreds of rows nobody asked for, and the count and the next scheduled attempt are
    what a survey is read for.
    """
    _lost_create_order(kernel, seeded_tenant.tenant_id, admitted)
    with kernel.begin():
        set_tenant(kernel, seeded_tenant.tenant_id)
        tk.record_reconciliation_run(
            kernel,
            tenant_id=seeded_tenant.tenant_id,
            payment_attempt_id=admitted.attempt_id,
            attempt_number=1,
            reason="payment_unknown",
            identifiers_queried={"receipt": "rcpt-test"},
            decision="order_lookup_unknown",
            resulting_transition=None,
            next_attempt_in_seconds=30,
            correlation_id=admitted.correlation_id,
        )

    page = operator("/v1/review/reconciliation").json()
    listed = _attempt_of(page, admitted.attempt_id)
    assert listed["runs"] == []
    assert listed["attempts_used"] == 1
    assert listed["attempts_remaining"] == recon.ATTEMPT_BOUND - 1
    assert listed["next_scheduled_attempt"] is not None

    detail = operator(f"/v1/review/reconciliation/{admitted.attempt_id}").json()["attempt"]
    assert len(detail["runs"]) == 1
    run = detail["runs"][0]
    assert run["attempt_number"] == 1
    assert run["decision"] == "order_lookup_unknown"
    assert run["identifiers_queried"] == {"receipt": "rcpt-test"}


def test_reconciliation_reads_write_nothing(
    operator: Callable[..., Any],
    kernel: Session,
    seeded_tenant: SeededTenant,
    admitted: Admitted,
    capi_app_engine: Engine,
) -> None:
    """Reading the queue changes no row anywhere.

    Asserted against the audit stream because that is where a write would have to leave a
    trace: this service produces findings, and a finding is a value rather than a row.
    """
    _create_order(kernel, seeded_tenant.tenant_id, admitted)
    escalation = _escalate(kernel, seeded_tenant.tenant_id, admitted, "evidence_mismatch")

    before = _row_counts(capi_app_engine, seeded_tenant.tenant_id)
    assert operator("/v1/review/reconciliation").status_code == 200
    assert operator(f"/v1/review/reconciliation/{admitted.attempt_id}").status_code == 200
    assert operator("/v1/review/queue").status_code == 200
    assert operator(f"/v1/review/queue/{escalation.case_key}").status_code == 200
    assert _row_counts(capi_app_engine, seeded_tenant.tenant_id) == before


def _row_counts(engine: Engine, tenant_id: uuid.UUID) -> dict[str, int]:
    tables = ("audit_events", "payment_attempts", "refunds", "orders", "reconciliation_runs")
    counts: dict[str, int] = {}
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        for table in tables:
            # S608: `table` iterates the literal tuple above, never request data.
            statement = text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t")  # noqa: S608
            counts[table] = int(conn.execute(statement, {"t": tenant_id}).scalar_one())
    return counts
