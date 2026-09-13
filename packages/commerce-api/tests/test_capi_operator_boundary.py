"""Exercise every operator route through HTTP with genuine database-backed sessions."""

import re

import pytest
from commerce_api.app import _api_routes
from commerce_api.deps import require_operator, require_scenario_key

PREFIXES = ("/v1/scenario", "/v1/review", "/v1/ops")


def operator_routes(app):
    routes = [r for r in _api_routes(app) if r.path.startswith(PREFIXES)]
    assert routes, "Operator route census must not be empty"
    return routes


def dependencies(node):
    return {node.call} | {call for child in node.dependencies for call in dependencies(child)}


def test_every_operator_route_has_both_guards(api_app):
    routes = operator_routes(api_app)
    assert routes
    for route in routes:
        assert {require_operator, require_scenario_key} <= dependencies(route.dependant), route.path


@pytest.mark.parametrize("actor", ["BUYER", "MERCHANT", "AGENT"])
def test_nonoperators_with_valid_key_cannot_reach_any_operator_endpoint(
    api_app, mint_client, scenario_headers, actor
):
    client, _ = mint_client(actor_type=actor)
    with client:
        for route in operator_routes(api_app):
            path = re.sub(r"\{[^}]+\}", "11111111-1111-4111-8111-111111111111", route.path)
            for method in route.methods:
                response = client.request(
                    method,
                    path,
                    headers={**scenario_headers, "X-Actor-Type": "OPERATOR"},
                    json={} if method not in ("GET", "HEAD") else None,
                )
                assert response.status_code == 403, (actor, method, path, response.text)
                assert response.json()["actor_type"] == actor


def test_operator_still_requires_demo_key(client, operator_headers):
    headers = {"Authorization": operator_headers["Authorization"]}
    assert client.get("/v1/ops/safe-mode", headers=headers).status_code == 401


def test_denied_buyer_safe_mode_write_does_not_change_state(
    auth_client, client, scenario_headers, operator_headers
):
    before = client.get("/v1/ops/safe-mode", headers=operator_headers)
    assert before.status_code == 200
    denied = auth_client.post("/v1/ops/safe-mode", headers=scenario_headers, json={"enabled": True})
    assert denied.status_code == 403
    after = client.get("/v1/ops/safe-mode", headers=operator_headers)
    assert after.status_code == 200
    assert after.json() == before.json()
