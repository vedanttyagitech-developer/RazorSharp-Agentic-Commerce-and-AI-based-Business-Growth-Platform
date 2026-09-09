"""Sales facts use recorded order amounts and require merchant permission."""

import pytest
from fastapi.testclient import TestClient
from test_capi_support_helpdesk import (  # noqa: F401 - reuse confirmed-sale fixtures
    kernel,
    order_id,
)

from conftest import SeededTenant

pytestmark = pytest.mark.db


def test_sales_value_is_the_recorded_order_amount(
    client: TestClient,
    auth_client: TestClient,
    seeded_tenant: SeededTenant,
    scenario_headers: dict[str, str],
    order_id: str,  # noqa: F811 - imported pytest fixture is injected by name
) -> None:
    order = auth_client.get(f"/v1/orders/{order_id}").json()
    session = client.post(
        "/v1/demo/sessions",
        headers=scenario_headers,
        json={"tenant_slug": seeded_tenant.tenant_slug, "actor_type": "MERCHANT"},
    )
    assert session.status_code == 201
    headers = {**scenario_headers, "Authorization": f"Bearer {session.json()['token']}"}
    response = client.get("/v1/merchant/insights?days=7", headers=headers)
    assert response.status_code == 200, response.text
    rows = response.json()["totals"]
    currency = next(row for row in rows if row["currency"] == order["currency"])
    assert currency["orders"] == 1
    assert currency["sales_minor"] == order["amount_minor"]
    assert "before refunds" in response.json()["definition"]


def test_buyer_cannot_read_merchant_sales_even_with_scenario_header(
    auth_client: TestClient,
    scenario_headers: dict[str, str],
) -> None:
    response = auth_client.get("/v1/merchant/insights", headers=scenario_headers)
    assert response.status_code == 403
