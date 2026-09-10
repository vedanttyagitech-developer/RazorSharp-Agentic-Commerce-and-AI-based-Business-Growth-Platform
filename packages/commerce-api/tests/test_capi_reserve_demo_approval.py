"""Demo confirmation replaces device enrollment; signatures remain mandatory."""

import importlib.util
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.db
TERMS = {"per_purchase_limit_minor": 20000, "capacity_minor": 500000}


def test_demo_approval_retry_and_independent_proof(auth_client):
    headers = {"Idempotency-Key": str(uuid.uuid4())}
    response = auth_client.post("/v1/reserve/authorities", headers=headers, json=TERMS)
    assert response.status_code == 200, response.text
    again = auth_client.post("/v1/reserve/authorities", headers=headers, json=TERMS)
    assert again.json() == response.json()
    identifier = response.json()["authority_id"]
    document = auth_client.get(f"/v1/reserve/authorities/{identifier}/proof").json()
    keys = auth_client.get("/v1/reserve/verification-keys").json()
    assert document["consent_evidence"] is None
    script = Path(__file__).resolve().parents[3] / "scripts/verify_reserve_authorization.py"
    spec = importlib.util.spec_from_file_location("reserve_verifier", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.verify(document, keys)["signature"] == "VALID"
    import base64
    import json

    parts = document["artifact_jws"].split(".")
    claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
    claims["max_amount_minor"] += 1
    parts[1] = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    from jwcrypto.jws import InvalidJWSSignature

    with pytest.raises(InvalidJWSSignature):
        module.verify({**document, "artifact_jws": ".".join(parts)}, keys)


@pytest.mark.parametrize("path", ["/authorities/options", "/passkeys/register"])
def test_retired_device_endpoints_are_unavailable(auth_client, path):
    result = auth_client.post("/v1/reserve" + path, json=TERMS)
    assert result.status_code in (404, 405)


def test_old_device_fields_are_rejected(auth_client):
    result = auth_client.post(
        "/v1/reserve/authorities",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={**TERMS, "ceremony_id": str(uuid.uuid4()), "credential": {}},
    )
    assert result.status_code == 422
