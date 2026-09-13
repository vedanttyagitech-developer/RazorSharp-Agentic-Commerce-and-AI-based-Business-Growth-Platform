"""Remote mode must verify exact returned bounds and never use a local signing fallback."""

import json
import os
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from commerce_api.services import reserve_provider
from jwcrypto.jwk import JWK
from reserve_trust import AUDIENCE, ISSUER

pytestmark = pytest.mark.db


@pytest.mark.parametrize("changed_buyer", [False, True])
def test_remote_signing_is_pinned_and_bound_to_request(
    auth_client, demo_session, monkeypatch, tmp_path, changed_buyer
):
    expiry = datetime.now(UTC) + timedelta(days=7)
    issued = reserve_provider.issue_authorization(
        tenant_id=demo_session.tenant_id,
        merchant_id=demo_session.merchant_id,
        buyer_ref="different-buyer" if changed_buyer else demo_session.buyer_ref,
        max_amount_minor=500000,
        per_purchase_limit_minor=20000,
        allowed_skus=None,
        expires_at=expiry,
    )
    key = json.loads(os.environ["RESERVE_PROVIDER_VERIFICATION_JWKS"])["keys"][0]
    now = int(time.time())
    policy = {
        "version": 1,
        "issuer": ISSUER,
        "audience": AUDIENCE,
        "jwks_uri": "https://issuer.example/.well-known/reserve/jwks",
        "issued_at": now - 1,
        "expires_at": now + 600,
        "keys": {key["kid"]: {"sha256": JWK(**key).thumbprint(), "status": "active"}},
    }
    path = tmp_path / "trust.json"
    path.write_text(json.dumps(policy))
    monkeypatch.setenv("RESERVE_TRUST_CONFIG_PATH", str(path))
    monkeypatch.setenv("RESERVE_SIGNER_MODE", "remote")
    monkeypatch.setenv("RESERVE_SIGNER_URL", "https://signer.example")
    monkeypatch.delenv("RESERVE_PROVIDER_SIGNING_JWK")
    import google.oauth2.id_token

    monkeypatch.setattr(google.oauth2.id_token, "fetch_id_token", lambda *_args: "test-identity")
    calls = []

    def remote(request):
        calls.append(request)
        assert request.url == "https://signer.example/v1/authorizations"
        assert request.headers["Authorization"] == "Bearer test-identity"
        assert json.loads(request.content)["buyer_ref"] == demo_session.buyer_ref
        return httpx.Response(200, json={"artifact_jws": issued})

    original = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(remote), **kwargs)
    )
    result = auth_client.post(
        "/v1/reserve/authorities",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={
            "allowed_skus": None,
            "per_purchase_limit_minor": 20000,
            "capacity_minor": 500000,
            "expires_at": expiry.isoformat(),
        },
    )
    assert result.status_code == (503 if changed_buyer else 200), result.text
    assert len(calls) == 1


def test_discovery_does_not_publish_private_keys(auth_client):
    response = auth_client.get("/.well-known/reserve/jwks")
    assert response.status_code == 200
    assert all("d" not in key for key in response.json()["keys"])
