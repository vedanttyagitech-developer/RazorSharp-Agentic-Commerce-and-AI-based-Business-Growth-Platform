"""A merchant proposes a change, agrees to it, and the shop changes. Over HTTP, once.

The loop first, because everything else here is a refusal and refusals only mean something
once the permitted path is known to work: draft a price change, edit it, put it to an
approver, approve it by naming the digest, execute it, and read the new price out of the
catalogue the buyer's storefront reads.

Then the four refusals that are the design rather than its edges.

**An approval names a document, not a row.** Approving with a stale digest is refused, and
the refusal reports both, because "this changed" is only useful if the reader can see what
it changed from. That is what stops an approval attaching to an edit its approver never saw.

**An edit cannot reach an approved action.** The graph has no path back to DRAFT, so there
is no state in which an approved action and an edited action are the same action.

**The world is checked as well as the document.** An action approved against one catalogue
revision, executed after the shop has moved, is STALE rather than performed against a world
nobody agreed to.

**A model may propose and may not approve.** The two capabilities are separate strings, and
an AGENT session holds neither.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from conftest import SeededTenant

pytestmark = pytest.mark.db

MILK = "AMUL-DAIRY-001"


@pytest.fixture
def merchant(
    client: TestClient, seeded_tenant: SeededTenant, scenario_headers: dict[str, str]
) -> TestClient:
    """A merchant session: the person who runs the shop, with no buyer scope at all."""
    minted = client.post(
        "/v1/demo/sessions",
        json={"tenant_slug": seeded_tenant.tenant_slug, "actor_type": "MERCHANT"},
        headers=scenario_headers,
    )
    assert minted.status_code == 201, minted.text
    assert minted.json()["buyer_ref"] is None
    return TestClient(client.app, headers={"Authorization": f"Bearer {minted.json()['token']}"})


def _propose(merchant: TestClient, headers: dict[str, str], **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": "PRICE_CHANGE",
        "target": MILK,
        "proposal": {"unit_price_minor": 2800, "reason": "supplier increase"},
    }
    body.update(overrides)
    drafted = merchant.post("/v1/merchant/actions", json=body, headers=headers)
    assert drafted.status_code == 201, drafted.text
    return dict(drafted.json())


# ------------------------------------------------------------------------------ the loop


def test_a_merchant_proposes_approves_and_the_shop_changes(
    merchant: TestClient, scenario_headers: dict[str, str]
) -> None:
    """The permitted path, end to end, with the new price read back from the catalogue."""
    drafted = _propose(merchant, scenario_headers)
    assert drafted["state"] == "DRAFT"
    assert drafted["approved_by"] is None
    assert drafted["proposed_by"].startswith("session:")
    action_id = drafted["action_id"]

    edited = merchant.put(
        f"/v1/merchant/actions/{action_id}",
        json={"proposal": {"unit_price_minor": 2750, "reason": "supplier increase, revised"}},
        headers=scenario_headers,
    )
    assert edited.status_code == 200, edited.text
    # The digest moved with the proposal, which is the whole reason it is stored.
    assert edited.json()["content_hash"] != drafted["content_hash"]

    submitted = merchant.post(f"/v1/merchant/actions/{action_id}/submit", headers=scenario_headers)
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["state"] == "AWAITING_APPROVAL"

    approved = merchant.post(
        f"/v1/merchant/actions/{action_id}/approve",
        json={"content_hash": edited.json()["content_hash"]},
        headers=scenario_headers,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "APPROVED"
    assert approved.json()["approved_by"], "nobody is recorded as having agreed"

    done = merchant.post(f"/v1/merchant/actions/{action_id}/execute", headers=scenario_headers)
    assert done.status_code == 200, done.text
    assert done.json()["ok"], done.text
    assert done.json()["action"]["state"] == "SUCCEEDED"

    # And the shop really moved: read the price back out of the catalogue. The merchant
    # session holds `catalogue.read`, and this is the same route the storefront reads, so
    # what is asserted is what a buyer would now be shown.
    listed = merchant.get("/v1/catalogue/products", params={"limit": 100})
    assert listed.status_code == 200, listed.text
    prices = {p["sku"]: p["unit_price_minor"] for p in listed.json()["products"]}
    assert prices[MILK] == 2750


# ------------------------------------------------------------------------- the refusals


def test_approving_a_digest_that_is_no_longer_current_is_refused(
    merchant: TestClient, scenario_headers: dict[str, str]
) -> None:
    """The refusal the digest exists for.

    Somebody reads the proposal, somebody edits it, and the first person presses approve on
    a screen showing the old figure. Approving by id alone would agree to whatever the row
    says at the moment the request lands, which is not what they read.
    """
    drafted = _propose(merchant, scenario_headers)
    action_id = drafted["action_id"]
    stale_hash = drafted["content_hash"]

    merchant.put(
        f"/v1/merchant/actions/{action_id}",
        json={"proposal": {"unit_price_minor": 9900, "reason": "much bigger increase"}},
        headers=scenario_headers,
    )
    merchant.post(f"/v1/merchant/actions/{action_id}/submit", headers=scenario_headers)

    refused = merchant.post(
        f"/v1/merchant/actions/{action_id}/approve",
        json={"content_hash": stale_hash},
        headers=scenario_headers,
    )
    assert refused.status_code == 409, refused.text
    body = refused.json()
    # Both digests, so the reader can see what moved rather than being told to retry.
    assert body["approved_hash"] == stale_hash
    assert body["current_hash"] != stale_hash

    still = merchant.get(f"/v1/merchant/actions/{action_id}", headers=scenario_headers)
    assert still.json()["state"] == "AWAITING_APPROVAL"
    assert still.json()["approved_by"] is None


def test_an_approved_action_cannot_be_edited(
    merchant: TestClient, scenario_headers: dict[str, str]
) -> None:
    """No path back to draft, so an approval can never come to describe something else."""
    drafted = _propose(merchant, scenario_headers)
    action_id = drafted["action_id"]
    merchant.post(f"/v1/merchant/actions/{action_id}/submit", headers=scenario_headers)
    merchant.post(
        f"/v1/merchant/actions/{action_id}/approve",
        json={"content_hash": drafted["content_hash"]},
        headers=scenario_headers,
    )

    refused = merchant.put(
        f"/v1/merchant/actions/{action_id}",
        json={"proposal": {"unit_price_minor": 1}},
        headers=scenario_headers,
    )
    assert refused.status_code == 409, refused.text
    assert "draft" in refused.json()["title"].lower()


def test_an_action_whose_shop_moved_is_stale_rather_than_performed(
    merchant: TestClient, client: TestClient, scenario_headers: dict[str, str]
) -> None:
    """Approved against one world, executed in another.

    The catalogue revision is inside the hashed document precisely so this is detectable.
    Without it an approval would mean "somebody agreed to this change" rather than "somebody
    agreed to this change to *that* shelf".
    """
    drafted = _propose(merchant, scenario_headers)
    action_id = drafted["action_id"]
    merchant.post(f"/v1/merchant/actions/{action_id}/submit", headers=scenario_headers)
    merchant.post(
        f"/v1/merchant/actions/{action_id}/approve",
        json={"content_hash": drafted["content_hash"]},
        headers=scenario_headers,
    )

    # Somebody else moves the shop underneath, through the operator's own lever.
    moved = client.post(
        "/v1/scenario/injections",
        json={"kind": "STOCK_SET", "sku": MILK, "value": 5},
        headers={**scenario_headers, "Authorization": merchant.headers["Authorization"]},
    )
    assert moved.status_code == 201, moved.text

    executed = merchant.post(f"/v1/merchant/actions/{action_id}/execute", headers=scenario_headers)
    assert executed.status_code == 200, executed.text
    assert not executed.json()["ok"]
    assert executed.json()["reason"] == "catalogue_moved"
    assert executed.json()["action"]["state"] == "STALE"
    assert "revision" in executed.json()["action"]["outcome_note"]


def test_a_change_that_changes_nothing_is_recorded_as_failed_not_retried(
    merchant: TestClient, scenario_headers: dict[str, str]
) -> None:
    """The shop refuses its own no-op, and that refusal is an answer.

    An injection that alters nothing would still advance the catalogue revision and make
    every open quote stale for no reason. So the store says no, and the action records what
    it said rather than looping.
    """
    listed = merchant.get("/v1/catalogue/products", params={"limit": 100})
    current = {p["sku"]: p["unit_price_minor"] for p in listed.json()["products"]}[MILK]

    drafted = _propose(
        merchant, scenario_headers, proposal={"unit_price_minor": current, "reason": "no change"}
    )
    action_id = drafted["action_id"]
    merchant.post(f"/v1/merchant/actions/{action_id}/submit", headers=scenario_headers)
    merchant.post(
        f"/v1/merchant/actions/{action_id}/approve",
        json={"content_hash": drafted["content_hash"]},
        headers=scenario_headers,
    )
    executed = merchant.post(f"/v1/merchant/actions/{action_id}/execute", headers=scenario_headers)
    assert executed.status_code == 200, executed.text
    assert not executed.json()["ok"]
    assert executed.json()["action"]["state"] == "FAILED"
    assert executed.json()["action"]["outcome_note"]


def test_a_proposal_a_person_cannot_read_is_refused(
    merchant: TestClient, scenario_headers: dict[str, str]
) -> None:
    """A float, by name. The approval of a document nobody can render is worth less."""
    refused = merchant.post(
        "/v1/merchant/actions",
        json={"kind": "PRICE_CHANGE", "target": MILK, "proposal": {"unit_price": 28.5}},
        headers=scenario_headers,
    )
    assert refused.status_code == 422, refused.text
    assert "unit_price" in refused.json()["detail"]


def test_the_second_execution_is_refused_rather_than_repeated(
    merchant: TestClient, scenario_headers: dict[str, str]
) -> None:
    """The action's own state is the single-use guarantee.

    There is no merchant idempotency key, and none is needed: an action leaves APPROVED
    once, and nothing in the graph returns to it. Pressing again asks a question the row has
    already answered.
    """
    drafted = _propose(merchant, scenario_headers, proposal={"unit_price_minor": 3100})
    action_id = drafted["action_id"]
    merchant.post(f"/v1/merchant/actions/{action_id}/submit", headers=scenario_headers)
    merchant.post(
        f"/v1/merchant/actions/{action_id}/approve",
        json={"content_hash": drafted["content_hash"]},
        headers=scenario_headers,
    )
    first = merchant.post(f"/v1/merchant/actions/{action_id}/execute", headers=scenario_headers)
    assert first.json()["ok"], first.text

    again = merchant.post(f"/v1/merchant/actions/{action_id}/execute", headers=scenario_headers)
    assert again.status_code == 200, again.text
    assert not again.json()["ok"]
    assert again.json()["reason"] == "not_a_permitted_move"
    assert again.json()["action"]["state"] == "SUCCEEDED"
    assert again.json()["allowed"] == []


# ----------------------------------------------------------------------- who may do what


def test_the_copilot_may_not_reach_this_surface(
    client: TestClient, seeded_tenant: SeededTenant, scenario_headers: dict[str, str]
) -> None:
    """A model holds neither capability, and the two are separate for that reason.

    The copilot that drafts a change would hold ``merchant.action.propose`` if it were ever
    given one. It would never hold ``merchant.action.approve``, because approving is the
    human judgement the proposal is put up for.
    """
    minted = client.post(
        "/v1/demo/sessions",
        json={"tenant_slug": seeded_tenant.tenant_slug, "actor_type": "AGENT"},
    )
    assert minted.status_code == 201, minted.text
    agent = TestClient(client.app, headers={"Authorization": f"Bearer {minted.json()['token']}"})

    listing = agent.get("/v1/merchant/actions", headers=scenario_headers)
    assert listing.status_code == 403, listing.text

    proposing = agent.post(
        "/v1/merchant/actions",
        json={"kind": "PRICE_CHANGE", "target": MILK, "proposal": {"unit_price_minor": 1}},
        headers=scenario_headers,
    )
    assert proposing.status_code == 403, proposing.text


def test_the_surface_is_closed_without_the_operator_key(merchant: TestClient) -> None:
    assert merchant.get("/v1/merchant/actions").status_code == 401


def test_a_buyer_cannot_see_another_merchants_actions(
    merchant: TestClient,
    client: TestClient,
    seeded_tenant: SeededTenant,
    scenario_headers: dict[str, str],
) -> None:
    """The buyer holds neither capability. The 403 is the capability, not the key."""
    _propose(merchant, scenario_headers)
    minted = client.post(
        "/v1/demo/sessions",
        json={"tenant_slug": seeded_tenant.tenant_slug, "actor_type": "BUYER"},
    )
    buyer = TestClient(client.app, headers={"Authorization": f"Bearer {minted.json()['token']}"})
    refused = buyer.get("/v1/merchant/actions", headers=scenario_headers)
    assert refused.status_code == 403, refused.text
