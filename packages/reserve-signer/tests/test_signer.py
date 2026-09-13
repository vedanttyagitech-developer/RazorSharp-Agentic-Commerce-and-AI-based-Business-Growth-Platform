import base64
import time
import uuid

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from fastapi.testclient import TestClient
from jwcrypto.jwk import JWK
from jwcrypto.jws import JWS
from reserve_signer.app import build_app
from reserve_signer.kms import KmsSigner

VERSION = (
    "projects/example/locations/asia-south1/keyRings/reserve/"
    "cryptoKeys/authority/cryptoKeyVersions/1"
)
CALLER = "api@example.iam.gserviceaccount.com"


class Response:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class KmsTransport:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.calls = 0
        self.protection = "HSM"
        self.corrupt = False

    def get(self, url, **kwargs):
        return Response(
            {
                "algorithm": "EC_SIGN_P256_SHA256",
                "protectionLevel": self.protection,
                "pem": self.pem().decode(),
            }
        )

    def pem(self):
        return self.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    def post(self, url, json, **kwargs):
        self.calls += 1
        digest = base64.b64decode(json["digest"]["sha256"])
        der = self.key.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        if self.corrupt:
            der = der[:-1] + bytes([der[-1] ^ 1])
        return Response({"name": VERSION, "signature": base64.b64encode(der).decode()})


def setup():
    transport = KmsTransport()
    signer = KmsSigner(transport, VERSION, "key1", JWK.from_pem(transport.pem()).thumbprint())
    tenant, merchant = str(uuid.uuid4()), str(uuid.uuid4())
    body = {
        "tenant_id": tenant,
        "merchant_id": merchant,
        "buyer_ref": "buyer1",
        "max_amount_minor": 10000,
        "per_purchase_limit_minor": 1000,
    }
    policy = {
        "version": 1,
        "expires_at": int(time.time()) + 3600,
        "scopes": [
            {
                "caller": CALLER,
                "caller_subject": "123",
                "tenant_id": tenant,
                "merchant_id": merchant,
                "buyer_refs": ["buyer1"],
                "currency": "INR",
                "max_capacity_minor": 10000,
                "max_purchase_minor": 1000,
                "allow_until_revoked": True,
                "max_ttl_seconds": 86400,
            }
        ],
    }

    def identity(token):
        if token != "good":  # noqa: S105 - fake identity verifier
            raise ValueError("bad token")
        return {"email": CALLER, "email_verified": True, "sub": "123"}

    return transport, signer, body, policy, identity


def test_der_signature_is_raw_jws_verified_without_kms():
    transport, signer, body, policy, identity = setup()
    client = TestClient(build_app(signer, policy, identity))
    result = client.post("/v1/authorizations", json=body, headers={"Authorization": "Bearer good"})
    assert result.status_code == 200, result.text
    compact = result.json()["artifact_jws"]
    assert len(base64.urlsafe_b64decode(compact.split(".")[2] + "==")) == 64
    token = JWS()
    token.allowed_algs = ["ES256"]
    token.deserialize(compact)
    token.verify(JWK(**signer.jwk), alg="ES256")
    assert transport.calls == 1


@pytest.mark.parametrize(
    "change", ["token", "merchant", "buyer", "capacity", "currency", "claims", "expired"]
)
def test_unauthorized_requests_never_reach_kms(change):
    transport, signer, body, policy, identity = setup()
    token = "good"  # noqa: S105 - test token
    if change == "token":
        token = "bad"  # noqa: S105 - invalid test token
    elif change == "merchant":
        body["merchant_id"] = str(uuid.uuid4())
    elif change == "buyer":
        body["buyer_ref"] = "not-enrolled"
    elif change == "capacity":
        body["max_amount_minor"] = 10001
    elif change == "currency":
        body["currency"] = "USD"
    elif change == "claims":
        body["iss"] = "bank"
    else:
        policy["expires_at"] = 1
    client = TestClient(build_app(signer, policy, identity))
    assert client.post(
        "/v1/authorizations", json=body, headers={"Authorization": "Bearer " + token}
    ).status_code in (401, 403, 422, 503)
    assert transport.calls == 0


def test_corrupted_kms_signature_is_not_returned():
    transport, signer, body, policy, identity = setup()
    transport.corrupt = True
    client = TestClient(build_app(signer, policy, identity))
    assert (
        client.post(
            "/v1/authorizations", json=body, headers={"Authorization": "Bearer good"}
        ).status_code
        == 503
    )


def test_software_key_refused():
    transport = KmsTransport()
    transport.protection = "SOFTWARE"
    with pytest.raises(ValueError, match="HSM"):
        KmsSigner(transport, VERSION, "key1", JWK.from_pem(transport.pem()).thumbprint())
