"""A buyer's case reaches a person, and the person answers it. The whole loop, once.

This is the test the refund panel was owed. That screen tells the buyer, in words, that a
disputed order goes to somebody who decides what they are owed, and it deliberately offers
no field to type an amount into. Until this router existed the case was inserted into
``support_cases`` -- with the owning merchant resolved from the order, with an index built
for a queue -- and read by nobody. The promise was true about intent and false about
machinery, which is the kind of false that survives a demo.

So the first test here is the loop, end to end and over HTTP: a buyer opens a case, an
operator finds it on the queue, picks it up, answers it, and the buyer's own screen sees
the status move. Nothing is arranged in SQL except the two steps the Action Executor owns,
because an order that was inserted rather than admitted is not an order this queue would
ever be asked about.

The rest are the refusals, and they are the point of the design rather than its edges:

* two people opening the same queue must not silently overwrite each other, so a move the
  case cannot make is a 409 that names the moves it could;
* answering a case nobody picked up leaves no record of who was dealing with it, so
  ``OPEN`` cannot go straight to ``RESOLVED``;
* ``handled_by`` comes from the session, so a caller cannot put somebody else's name on
  their own decision;
* the Support Specialist's capability set may read the queue and may not answer it, because
  answering is the human judgement the case was raised to obtain;
* and no route here carries an amount in either direction -- what is still refundable is
  asked of the kernel, through the same route the buyer's screen uses.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
import transaction_kernel as tk
from commerce_domain import CheckoutRef, Money, RecoveryCode, sha256_hex
from fastapi.testclient import TestClient
from platform_db import set_tenant
from sqlalchemy import Engine
from sqlalchemy.orm import Session
from transaction_kernel.payments import ProviderOrderOutcome

from conftest import MintedSession, SeededTenant

pytestmark = pytest.mark.db

MILK = "AMUL-DAIRY-001"


def _key() -> str:
    return f"k-{uuid.uuid4().hex}"


def _headers(**extra: str) -> dict[str, str]:
    return {"Idempotency-Key": _key(), **extra}


@dataclass(frozen=True, slots=True)
class Admitted:
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
    session = Session(capi_kernel_engine, expire_on_commit=False)
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def helpdesk(
    client: TestClient,
    seeded_tenant: SeededTenant,
    demo_session: MintedSession,  # noqa: ARG001 - the buyer must exist before the operator
    scenario_headers: dict[str, str],
) -> TestClient:
    """An operator session: the merchant's side of the desk.

    Minted with no ``buyer_ref``. That is the difference that matters between this and the
    buyer's client, and it is what the queue read is scoped by -- the tenant, through
    row-level security, rather than one person's own orders.
    """
    minted = client.post(
        "/v1/demo/sessions",
        json={"tenant_slug": seeded_tenant.tenant_slug, "actor_type": "OPERATOR"},
        headers=scenario_headers,
    )
    assert minted.status_code == 201, minted.text
    token = minted.json()["token"]
    return TestClient(client.app, headers={"Authorization": f"Bearer {token}"})


def _admit(auth_client: TestClient) -> Admitted:
    """Cart, checkout, approval and admission, all over HTTP. Nothing arranged in SQL."""
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


def _confirmed_order(
    auth_client: TestClient, kernel: Session, tenant_id: uuid.UUID, adm: Admitted
) -> str:
    """The two steps the Action Executor owns, then the order the kernel wrote."""
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
                    "amount_refunded_minor": 0,
                    "raw_digest": sha256_hex(payment_id.encode()),
                }
            ),
            correlation_id=adm.correlation_id,
        )
    listed = auth_client.get("/v1/orders")
    assert listed.status_code == 200, listed.text
    for order in listed.json()["orders"]:
        if order["checkout_id"] == str(adm.checkout_id):
            return str(order["order_id"])
    raise AssertionError(f"no order for checkout {adm.checkout_id}: {listed.text}")


@pytest.fixture
def order_id(auth_client: TestClient, kernel: Session, seeded_tenant: SeededTenant) -> str:
    return _confirmed_order(auth_client, kernel, seeded_tenant.tenant_id, _admit(auth_client))


def _open_case(auth_client: TestClient, order: str, note: str = "") -> dict[str, Any]:
    raised = auth_client.post(
        f"/v1/orders/{order}/support-cases",
        json={"reason": "item_damaged", "note": note},
        headers=_headers(),
    )
    assert raised.status_code in (200, 201), raised.text
    return dict(raised.json())


# ------------------------------------------------------------------------------ the loop


def test_a_case_a_buyer_opens_reaches_a_person_who_answers_it(
    auth_client: TestClient,
    helpdesk: TestClient,
    scenario_headers: dict[str, str],
    order_id: str,
) -> None:
    """The promise on the refund panel, kept, in the order a real one happens."""
    opened = _open_case(auth_client, order_id, note="The seal was broken on both bottles.")
    case_id = opened["case_id"]

    queue = helpdesk.get("/v1/support/cases", params={"status": "OPEN"}, headers=scenario_headers)
    assert queue.status_code == 200, queue.text
    mine = [c for c in queue.json()["cases"] if c["case_id"] == case_id]
    assert mine, f"the case is not on the queue: {queue.text}"
    listed = mine[0]

    # What the person answering needs, and did not have before this router: the buyer's own
    # words, and a reference they can say out loud rather than a UUID they cannot.
    assert listed["note"] == "The seal was broken on both bottles."
    assert listed["order_reference"].startswith("RS-")
    assert listed["opened_by"] == "BUYER"
    assert listed["handled_by"] is None

    picked = helpdesk.post(
        f"/v1/support/cases/{case_id}/advance",
        json={"status": "ACKNOWLEDGED"},
        headers=scenario_headers,
    )
    assert picked.status_code == 200, picked.text
    assert picked.json()["status"] == "ACKNOWLEDGED"
    assert picked.json()["handled_by"], "nobody is recorded as dealing with it"

    answered = helpdesk.post(
        f"/v1/support/cases/{case_id}/advance",
        json={"status": "RESOLVED", "note": "Both bottles refunded and collection arranged."},
        headers=scenario_headers,
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["status"] == "RESOLVED"
    assert answered.json()["resolution_note"].startswith("Both bottles")

    # And the buyer's own screen sees it, on the route that screen already reads.
    theirs = auth_client.get(f"/v1/orders/{order_id}/support-cases")
    assert theirs.status_code == 200, theirs.text
    assert [c["status"] for c in theirs.json()["cases"] if c["case_id"] == case_id] == ["RESOLVED"]


def test_the_queue_is_oldest_first(
    auth_client: TestClient,
    helpdesk: TestClient,
    kernel: Session,
    seeded_tenant: SeededTenant,
    scenario_headers: dict[str, str],
    order_id: str,
) -> None:
    """The one ordering decision on this router, asserted rather than assumed.

    Oldest first is the opposite of most lists. A queue sorted newest-first abandons its own
    tail: the case that has waited longest is precisely the one somebody is owed an answer
    on, and it is the one that would fall off the end of the first page.
    """
    first = _open_case(auth_client, order_id, note="first")
    second_order = _confirmed_order(
        auth_client, kernel, seeded_tenant.tenant_id, _admit(auth_client)
    )
    second = _open_case(auth_client, second_order, note="second")

    queue = helpdesk.get("/v1/support/cases", headers=scenario_headers)
    assert queue.status_code == 200, queue.text
    ids = [c["case_id"] for c in queue.json()["cases"]]
    assert ids.index(first["case_id"]) < ids.index(second["case_id"])


# ------------------------------------------------------------------------- the refusals


def test_a_case_nobody_picked_up_cannot_be_answered(
    auth_client: TestClient,
    helpdesk: TestClient,
    scenario_headers: dict[str, str],
    order_id: str,
) -> None:
    """OPEN to RESOLVED is refused, and the refusal names what was possible instead.

    Answering a case you never picked up is entirely possible in real life, and it is the
    sequence that leaves no record of who was dealing with it. The graph refuses it so the
    record exists, not because the outcome would have been wrong.
    """
    case_id = _open_case(auth_client, order_id)["case_id"]
    refused = helpdesk.post(
        f"/v1/support/cases/{case_id}/advance",
        json={"status": "RESOLVED"},
        headers=scenario_headers,
    )
    assert refused.status_code == 409, refused.text
    body = refused.json()
    assert body["case_status"] == "OPEN"
    assert sorted(body["allowed"]) == ["ACKNOWLEDGED", "CLOSED"]


def test_a_case_closed_by_mistake_can_be_reopened_and_says_so(
    auth_client: TestClient,
    helpdesk: TestClient,
    scenario_headers: dict[str, str],
    order_id: str,
) -> None:
    """The cost of a misclick used to fall entirely on the buyer.

    ``CLOSED`` was terminal, so a case closed in error left somebody permanently
    unanswered and no surface could even show that it had happened. That was never a
    safety property; it was a missing edge. Reopening is now a move, and because it is a
    move it is recorded: it demands a reason and the reason is kept beside the answer it
    reversed rather than in place of it.
    """
    case_id = _open_case(auth_client, order_id)["case_id"]
    for status, note in (
        ("ACKNOWLEDGED", ""),
        ("RESOLVED", "both bottles refunded"),
        ("CLOSED", ""),
    ):
        moved = helpdesk.post(
            f"/v1/support/cases/{case_id}/advance",
            json={"status": status, "note": note},
            headers=scenario_headers,
        )
        assert moved.status_code == 200, moved.text

    reopened = helpdesk.post(
        f"/v1/support/cases/{case_id}/advance",
        json={"status": "ACKNOWLEDGED", "note": "closed the wrong row"},
        headers=scenario_headers,
    )
    assert reopened.status_code == 200, reopened.text

    now = helpdesk.get(f"/v1/support/cases/{case_id}", headers=scenario_headers).json()
    assert now["status"] == "ACKNOWLEDGED"
    assert "closed the wrong row" in now["resolution_note"]
    assert "both bottles refunded" in now["resolution_note"], (
        "reopening must not erase the answer it reversed; the trail is the record"
    )


def test_a_reversal_without_a_reason_is_refused(
    auth_client: TestClient,
    helpdesk: TestClient,
    scenario_headers: dict[str, str],
    order_id: str,
) -> None:
    """A reopened case is indistinguishable from one nobody ever answered.

    Which is why the note is not optional on the way back. Going forward, the state says
    what happened -- ACKNOWLEDGED means somebody picked it up. Going back, only the reason
    separates a correction from a mistake, and the next person to open the queue has
    nothing else to read.
    """
    case_id = _open_case(auth_client, order_id)["case_id"]
    helpdesk.post(
        f"/v1/support/cases/{case_id}/advance",
        json={"status": "ACKNOWLEDGED"},
        headers=scenario_headers,
    )

    bare = helpdesk.post(
        f"/v1/support/cases/{case_id}/advance",
        json={"status": "OPEN"},
        headers=scenario_headers,
    )
    assert bare.status_code == 422, bare.text

    still = helpdesk.get(f"/v1/support/cases/{case_id}", headers=scenario_headers)
    assert still.json()["status"] == "ACKNOWLEDGED", "a refused reversal must move nothing"

    with_reason = helpdesk.post(
        f"/v1/support/cases/{case_id}/advance",
        json={"status": "OPEN", "note": "not mine to answer"},
        headers=scenario_headers,
    )
    assert with_reason.status_code == 200, with_reason.text
    assert with_reason.json()["status"] == "OPEN"


def test_a_move_the_graph_forbids_is_still_refused(
    auth_client: TestClient,
    helpdesk: TestClient,
    scenario_headers: dict[str, str],
    order_id: str,
) -> None:
    """Reversibility widened the graph; it did not open it.

    Two people on one queue is ordinary, and the 409 is what stops the second press
    landing somewhere the first person's work did not leave the case.
    """
    case_id = _open_case(auth_client, order_id)["case_id"]
    refused = helpdesk.post(
        f"/v1/support/cases/{case_id}/advance",
        json={"status": "RESOLVED", "note": "answering without picking it up"},
        headers=scenario_headers,
    )
    assert refused.status_code == 409, refused.text
    assert "RESOLVED" not in refused.json()["allowed"]

    still = helpdesk.get(f"/v1/support/cases/{case_id}", headers=scenario_headers)
    assert still.json()["status"] == "OPEN"


def test_the_handler_is_the_session_and_not_the_request(
    auth_client: TestClient,
    helpdesk: TestClient,
    scenario_headers: dict[str, str],
    order_id: str,
) -> None:
    """A name a caller can set is a name a caller can set to somebody else's."""
    case_id = _open_case(auth_client, order_id)["case_id"]
    attempted = helpdesk.post(
        f"/v1/support/cases/{case_id}/advance",
        json={"status": "ACKNOWLEDGED", "handled_by": "somebody-else"},
        headers=scenario_headers,
    )
    # `extra="forbid"`: the field does not exist, so the request is rejected outright rather
    # than being accepted with the value quietly dropped. Silently ignoring it would read as
    # success to a caller who believed they had set it.
    assert attempted.status_code == 422, attempted.text

    picked = helpdesk.post(
        f"/v1/support/cases/{case_id}/advance",
        json={"status": "ACKNOWLEDGED"},
        headers=scenario_headers,
    )
    assert picked.status_code == 200, picked.text
    assert picked.json()["handled_by"] != "somebody-else"


def test_no_route_here_carries_an_amount(
    auth_client: TestClient,
    helpdesk: TestClient,
    scenario_headers: dict[str, str],
    order_id: str,
) -> None:
    """The omission that is a decision, asserted so a later field cannot arrive quietly.

    A figure stored on a case is a figure that was true once: a refund admitted in the
    meantime moves it, and the person answering would be reading a promise the platform had
    already spent. What is still refundable is asked of the kernel when somebody needs it,
    through the route the buyer's own screen uses.
    """
    case_id = _open_case(auth_client, order_id)["case_id"]
    one = helpdesk.get(f"/v1/support/cases/{case_id}", headers=scenario_headers).json()
    money_words = {"amount", "amount_minor", "currency", "refundable", "total", "minor"}
    assert money_words.isdisjoint(one), f"a money field appeared on a support case: {one}"

    with_amount = helpdesk.post(
        f"/v1/support/cases/{case_id}/advance",
        json={"status": "ACKNOWLEDGED", "amount_minor": 5000},
        headers=scenario_headers,
    )
    assert with_amount.status_code == 422, with_amount.text


def test_the_support_specialist_may_read_the_queue_and_may_not_answer_it(
    auth_client: TestClient,
    scenario_headers: dict[str, str],
    order_id: str,
) -> None:
    """The capability split, checked against the set the model actually holds.

    ``support.case.read`` is in ``SUPPORT_AGENT_CAPABILITIES`` and ``support.case.resolve``
    is deliberately not. If somebody later adds it there for symmetry, this fails -- which
    is the point, because answering the case is the human judgement it was raised to get.
    """
    from commerce_api.deps import OPERATOR_CAPABILITIES, SUPPORT_AGENT_CAPABILITIES

    assert "support.case.read" in SUPPORT_AGENT_CAPABILITIES
    assert "support.case.resolve" not in SUPPORT_AGENT_CAPABILITIES
    assert "support.case.resolve" in OPERATOR_CAPABILITIES

    case_id = _open_case(auth_client, order_id)["case_id"]
    # The buyer's own session holds neither, and is refused at the capability rather than
    # at the scenario key -- it sends the key here precisely so the gate under test is the
    # capability one.
    refused = auth_client.post(
        f"/v1/support/cases/{case_id}/advance",
        json={"status": "ACKNOWLEDGED"},
        headers=scenario_headers,
    )
    assert refused.status_code == 403, refused.text


def test_the_queue_is_closed_without_the_operator_key(
    auth_client: TestClient, order_id: str
) -> None:
    """No key, no queue. The refusal is 401 here and 404 elsewhere, and both are right.

    ``require_scenario_key`` answers 404 when the profile has no operator surface at all --
    the routes are declared not to exist, and a 401 would announce that they do. It answers
    401 when the surface exists and the key is absent or wrong, because then the endpoint
    is real and the operator has simply mistyped something, which is help rather than
    disclosure. A demo profile is the second case, so this asserts 401 rather than the 404
    a production profile would give.
    """
    _open_case(auth_client, order_id)
    assert auth_client.get("/v1/support/cases").status_code == 401


def test_an_unknown_case_is_a_404_and_not_an_error(
    helpdesk: TestClient, scenario_headers: dict[str, str]
) -> None:
    missing = helpdesk.get(f"/v1/support/cases/{uuid.uuid4()}", headers=scenario_headers)
    assert missing.status_code == 404, missing.text
