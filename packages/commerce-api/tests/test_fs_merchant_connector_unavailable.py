"""Failure scenario: the merchant catalogue/inventory/pricing connector cannot answer.

Specification 30's last row -- "Connector failure: no fabricated state; degrade/escalate"
-- and the matching line of the 23.3 degradation matrix, which says to pause quote and
revalidation and never invent stock, price, fee or fulfilment state.

**The money invariant already held before any of this was written.** Admission asks the
merchant state source for authoritative truth at step 8; when the source raises, the
exception propagates out of :func:`transaction_kernel.admit`, the transaction rolls back
and nothing is created. That is asserted here anyway, because it is the claim everything
else rests on. What was *not* honest was the degradation: an exception carrying no
:class:`~transaction_kernel.RecoveryCode` fell through
:func:`commerce_api.errors.status_for` to its "understood and declined" default and the
buyer was told **409 Conflict** with the Python class name in ``title`` -- a status that
tells every client in the chain there is a state conflict to resolve by re-approving,
when there is nothing to resolve and re-approving cannot help.

**Why the outage is a refusing state source rather than a cut socket.**
``test_fs_database_unavailable`` argues, correctly, that a mock which raises only proves
the code handles a raised exception. That argument does not transfer here. ADR 0003 D14
makes the merchant simulator authoritative *in this process* -- there is no socket
between the kernel and it, so "the connector is unavailable" has exactly one
representation: the state source declining to produce a :class:`CurrentMerchantState`.
That is the real interface the kernel depends on
(:class:`transaction_kernel.admission.MerchantStateSource`), and a deployment's HTTP
connector reaches the kernel through the same one contract. So the substitution here is
at the seam the design already named, not around it.

What a buyer meets, in order:

* search and the basket still work. Browsing reads the catalogue, not the state source,
  and 23.3 asks for browsing to keep rendering rather than for the storefront to go dark.
* the submit -- the money mutation -- is a **503** RFC 9457 problem carrying
  ``code: CONNECTOR_UNAVAILABLE``. Not a 200 with a decision in it: a decision means the
  kernel weighed the request, and here it never got the facts to weigh.
* the problem discloses nothing. A 5xx suppresses ``str(exc)``, which for this exception
  named the checkout id and the transaction it was not visible in.
* the code renders as a sentence in the buyer's language, from the same table every other
  refusal is rendered from, instead of arriving as ``RevalidationError``.

Then the connector comes back and one submit produces exactly one attempt, one grant and
one command -- the "and then forward" half, without which fail-closed is indistinguishable
from broken.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any, Final

import pytest
from agent_runtime.language import Language
from agent_runtime.rendering import recovery_text
from commerce_api.errors import PROBLEM_MEDIA_TYPE
from commerce_api.merchants import MerchantRegistry
from fastapi.testclient import TestClient
from merchant_sim.kernel_adapter import RevalidationError
from sqlalchemy import Engine, text
from transaction_kernel import RecoveryCode
from transaction_kernel.admission import CurrentMerchantState

from conftest import MintedSession

pytestmark = pytest.mark.db

MILK: Final[str] = "AMUL-DAIRY-001"
_SET_TENANT: Final = text("SELECT set_config('app.tenant_id', :tenant_id, true)")

#: Every row type a submit would create if it got as far as deciding anything.
_MONEY_TABLES: Final[tuple[str, ...]] = ("payment_attempts", "execution_grants", "outbox_events")


class _DeadConnector:
    """A merchant state source that cannot answer, which is the whole of the outage.

    It raises the same exception the real adapter raises when it cannot read the approved
    version back -- :class:`merchant_sim.kernel_adapter.RevalidationError` -- rather than a
    bespoke test exception, so what is proven is the behaviour of the class that ships.
    """

    def revalidate(
        self, session: object, *, checkout_id: uuid.UUID, version: int
    ) -> CurrentMerchantState:
        raise RevalidationError(
            f"the merchant connector did not answer for checkout {checkout_id} version {version}"
        )


def _hand_out_a_dead_connector(_registry: object, _merchant_id: uuid.UUID) -> _DeadConnector:
    """Stand in for :meth:`MerchantRegistry.state_source`: no merchant's source answers."""
    return _DeadConnector()


@pytest.fixture
def dead_connector(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The registry hands the kernel a source that cannot answer, until the test lets go.

    Patched on the class rather than on the instance because
    :class:`~commerce_api.merchants.MerchantRegistry` defines ``__slots__``; the effect is
    the same and ``monkeypatch`` undoes it, which is what the recovery test needs.
    """
    monkeypatch.setattr(MerchantRegistry, "state_source", _hand_out_a_dead_connector)
    yield
    monkeypatch.undo()


# ------------------------------------------------------------------------------ helpers


def _key() -> str:
    return f"k-{uuid.uuid4().hex}"


def _approved_checkout(client: TestClient) -> dict[str, Any]:
    """Basket, line, checkout, approval -- everything up to the money mutation."""
    basket = client.post("/v1/baskets", headers={"Idempotency-Key": _key()})
    assert basket.status_code == 201, basket.text
    basket_id = basket.json()["basket_id"]

    line = client.put(
        f"/v1/baskets/{basket_id}/lines/{MILK}",
        json={"quantity": 2},
        headers={"Idempotency-Key": _key()},
    )
    assert line.status_code == 200, line.text

    opened = client.post(f"/v1/baskets/{basket_id}/checkout", headers={"Idempotency-Key": _key()})
    assert opened.status_code == 201, opened.text
    card: dict[str, Any] = opened.json()

    approved = client.post(
        f"/v1/checkouts/{card['checkout_id']}/versions/{card['version']}/approve",
        json={
            "content_hash": card["content_hash"],
            "amount_minor": card["amount_minor"],
            "currency": card["currency"],
        },
        headers={"Idempotency-Key": _key()},
    )
    assert approved.status_code == 200, approved.text
    return card


def _submit(client: TestClient, card: dict[str, Any]) -> Any:
    return client.post(
        f"/v1/checkouts/{card['checkout_id']}/versions/{card['version']}/submit",
        headers={"Idempotency-Key": _key()},
    )


def _counts(engine: Engine, tenant_id: uuid.UUID) -> dict[str, int]:
    counted: dict[str, int] = {}
    with engine.begin() as conn:
        conn.execute(_SET_TENANT, {"tenant_id": str(tenant_id)})
        for table in _MONEY_TABLES:
            # S608: `table` iterates the literal tuple above, never request data, and a
            # SQL identifier cannot be supplied as a bound parameter.
            statement = text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t")  # noqa: S608
            counted[table] = int(conn.execute(statement, {"t": tenant_id}).scalar_one())
    return counted


# -------------------------------------------------------------------------------- tests


@pytest.mark.usefixtures("dead_connector")
def test_browsing_survives_the_connector_going_away(auth_client: TestClient) -> None:
    """Search and basket pricing keep working: 23.3 asks for stale, not dark.

    They read the catalogue directly, while only admission goes through the state source,
    so the split in the degradation matrix -- keep browsing, pause revalidation -- is a
    property of which collaborator each path uses rather than a policy anyone enforces.
    """
    found = auth_client.get("/v1/catalogue/search", params={"q": "doodh", "limit": 5})
    assert found.status_code == 200, found.text
    assert found.json()["hits"], "the catalogue is not behind the state source"

    card = _approved_checkout(auth_client)
    assert card["amount_minor"] > 0


@pytest.mark.usefixtures("dead_connector")
def test_a_dead_connector_is_a_503_naming_the_code(auth_client: TestClient) -> None:
    """The refusal says "the upstream is unavailable", not "you have a conflict".

    409 was the old answer, and it was the wrong sentence in three ways at once: it is a
    4xx, so it blamed the caller; it is the status the buyer surface treats as "re-approve
    to continue", so it offered a remedy that cannot work; and it carried ``str(exc)`` as
    the detail, which named the checkout id. A 503 with a structured code is the same
    refusal told truthfully.
    """
    card = _approved_checkout(auth_client)

    refused = _submit(auth_client, card)

    assert refused.status_code == 503, refused.text
    assert refused.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    body = refused.json()
    assert body["status"] == 503
    assert body["code"] == RecoveryCode.CONNECTOR_UNAVAILABLE.value
    # Not a decision. ADR 0003 D15's 200 is for a kernel that considered the request; this
    # one never received the facts to consider.
    assert "allowed" not in body
    assert "decision_id" not in body
    # A 5xx discloses nothing. The old 409 put ``str(exc)`` in ``detail``, and this
    # exception's message is written for an operator: it names the transaction the version
    # was not visible in. ``instance`` still carries the request path, which is the
    # caller's own URL and tells them nothing they did not send.
    assert "detail" not in body
    assert "did not answer" not in refused.text


@pytest.mark.usefixtures("dead_connector")
def test_the_code_reaches_the_buyer_as_language_not_as_a_class_name(
    auth_client: TestClient,
) -> None:
    """The point of the code: it renders, and every other refusal renders the same way.

    Before there was a code there was nothing to render from, so the only buyer-facing
    string this failure produced was the problem's ``title`` -- the internal Python class
    name ``RevalidationError``. This asserts the join: the value the API returned is the
    key ``agent_runtime.rendering`` looks up, in all three languages the module carries.
    """
    card = _approved_checkout(auth_client)
    body = _submit(auth_client, card).json()

    code = RecoveryCode(body["code"])
    for language in Language:
        sentence = recovery_text(code, language)
        assert sentence.strip()
        assert code.value not in sentence


@pytest.mark.usefixtures("dead_connector")
def test_the_outage_creates_nothing_and_retires_nothing(
    auth_client: TestClient,
    demo_session: MintedSession,
    capi_admin_engine: Engine,
) -> None:
    """No attempt, no grant, no command -- and version 1 is still the buyer's to spend.

    The second half matters as much as the first. A price change retires version N and
    offers N+1, because the merchant genuinely moved. An outage moved nothing, so
    invalidating the approved version would destroy consent over a fact nobody observed.
    """
    card = _approved_checkout(auth_client)
    before = _counts(capi_admin_engine, demo_session.tenant_id)

    assert _submit(auth_client, card).status_code == 503

    assert _counts(capi_admin_engine, demo_session.tenant_id) == before
    assert before == dict.fromkeys(_MONEY_TABLES, 0)

    view = auth_client.get(f"/v1/checkouts/{card['checkout_id']}")
    assert view.status_code == 200, view.text
    assert view.json()["current_version"] == card["version"]


def test_the_connector_returns_and_one_submit_makes_one_of_each(
    auth_client: TestClient,
    demo_session: MintedSession,
    capi_admin_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed, then forward. Without this, a wedged path would pass every test above.

    The retry carries a fresh ``Idempotency-Key``: the refused submit rolled its whole
    transaction back, so there is no stored result for the old key to replay, and reusing
    it would be testing the idempotency layer rather than the recovery.
    """
    real_state_source = MerchantRegistry.state_source
    monkeypatch.setattr(MerchantRegistry, "state_source", _hand_out_a_dead_connector)

    card = _approved_checkout(auth_client)
    assert _submit(auth_client, card).status_code == 503

    monkeypatch.setattr(MerchantRegistry, "state_source", real_state_source)

    admitted = _submit(auth_client, card)
    assert admitted.status_code == 200, admitted.text
    decision = admitted.json()
    assert decision["allowed"] is True, decision
    assert decision["code"] == RecoveryCode.OK.value

    assert _counts(capi_admin_engine, demo_session.tenant_id) == dict.fromkeys(_MONEY_TABLES, 1)
