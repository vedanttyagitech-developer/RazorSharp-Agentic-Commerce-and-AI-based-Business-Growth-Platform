"""Public demo budgets reject work before model invocation without adding a login."""

import pytest
from commerce_domain.workload import WorkloadGate

pytestmark = pytest.mark.db


def test_session_creation_is_bounded(client, seeded_tenant, scenario_headers):
    client.app.state.workload = WorkloadGate()
    for _ in range(30):
        response = client.post(
            "/v1/demo/sessions",
            json={
                "tenant_slug": seeded_tenant.tenant_slug,
                "actor_type": "BUYER",
            },
            headers=scenario_headers,
        )
        assert response.status_code == 201, response.text
    response = client.post(
        "/v1/demo/sessions",
        json={
            "tenant_slug": seeded_tenant.tenant_slug,
            "actor_type": "BUYER",
        },
        headers=scenario_headers,
    )
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"


def test_agent_tenant_capacity_refuses_before_model(client, demo_session, monkeypatch):
    from commerce_api.services import agent_service

    def forbidden(*_args, **_kwargs):
        pytest.fail("overloaded request reached agent execution")

    monkeypatch.setattr(agent_service, "run_turn", forbidden)
    with client.app.state.workload.admit([(f"agent:tenant:{demo_session.tenant_id}", 60, 1)]):
        # Fill all four tenant slots using nested admissions.
        with client.app.state.workload.admit([(f"agent:tenant:{demo_session.tenant_id}", 60, 4)]):
            with client.app.state.workload.admit(
                [(f"agent:tenant:{demo_session.tenant_id}", 60, 4)]
            ):
                with client.app.state.workload.admit(
                    [(f"agent:tenant:{demo_session.tenant_id}", 60, 4)]
                ):
                    response = client.post(
                        "/v1/agent/turn",
                        json={"message": "show milk"},
                        headers=demo_session.auth_header,
                    )
    assert response.status_code == 429, response.text
    assert response.headers["retry-after"] == "60"
