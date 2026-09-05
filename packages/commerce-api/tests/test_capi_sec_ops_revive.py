"""Regression: an agent may not revive a dead-lettered command.

Found by the security review of 2026-09-05. ``POST /v1/ops/outbox/{id}/revive`` was gated
only by the router-level ``X-Scenario-Key`` dependency and did not consult the caller's
actor type at all -- unlike ``POST /v1/ops/safe-mode``, which refuses an ``AGENT`` with a
403. A ``DEAD`` outbox row is almost always a ``PAYMENT_CREATE_ORDER`` or ``REFUND_EXECUTE``
command, so reviving one re-drives a money operation under its existing grant. An agent
holding the demo scenario key could therefore reach a money control the platform otherwise
reserves for an operator, breaking the "an agent proposes but never moves money"
invariant.

The scenario key is the operator *apparatus*, not an *identity*; the fix restores parity
with the Safe Mode switch by refusing an ``AGENT`` actor before anything is touched. These
tests present the scenario key on the request (so the router gate is satisfied) and assert
the agent is still refused, while an operator with the same key passes the actor gate.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from fastapi.testclient import TestClient

from conftest import MintedSession, SeededTenant

MintClient = Callable[..., tuple[TestClient, MintedSession]]


def _operator_client(
    client: TestClient, seeded_tenant: SeededTenant, scenario_headers: dict[str, str]
) -> TestClient:
    """An OPERATOR session -- which can only be minted by a caller holding the key."""
    minted = client.post(
        "/v1/demo/sessions",
        json={"tenant_slug": seeded_tenant.tenant_slug, "actor_type": "OPERATOR"},
        headers=scenario_headers,
    )
    assert minted.status_code == 201, minted.text
    token = minted.json()["token"]
    return TestClient(client.app, headers={"Authorization": f"Bearer {token}"})


def test_an_agent_holding_the_scenario_key_may_not_revive(
    mint_client: MintClient, scenario_headers: dict[str, str]
) -> None:
    agent_client, _agent = mint_client(actor_type="AGENT")
    response = agent_client.post(f"/v1/ops/outbox/{uuid.uuid4()}/revive", headers=scenario_headers)
    # 403 from the actor guard, not a 401 from the key gate (the key is present) and not a
    # 404/409 from the revive handler (the agent must never reach it).
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["title"] == "Reviving a command is an operator control"
    assert body.get("actor_type") == "AGENT"


def test_an_operator_with_the_key_passes_the_actor_gate(
    client: TestClient, seeded_tenant: SeededTenant, scenario_headers: dict[str, str]
) -> None:
    """The fix must not over-block: an operator still reaches the revive handler.

    Reviving a random id has nothing to revive, so the handler answers a usage error --
    the point is only that the actor guard did not turn an operator away with a 403.
    """
    operator = _operator_client(client, seeded_tenant, scenario_headers)
    response = operator.post(f"/v1/ops/outbox/{uuid.uuid4()}/revive", headers=scenario_headers)
    assert response.status_code != 403, response.text
    if response.status_code == 200:
        # A 200 revive of a missing id reports the truth rather than pretending it worked.
        assert response.json()["code"] != "OK"
