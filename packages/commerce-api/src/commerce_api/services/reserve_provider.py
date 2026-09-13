"""Separate ES256 simulator credential issuer for trusted buyer authorization.

No NPCI/bank confirmation is claimed. The private key is provisioned once outside code;
the Kernel and debit worker require only its public verification set.
"""

import os
import time
import uuid
from datetime import datetime
from typing import Any

from commerce_domain import canonicalize, uuid7
from commerce_protocols.ap2.signing import InProcessSigner
from jwcrypto.jwk import JWK
from transaction_kernel.reserve_proofs import AUDIENCE, ISSUER, TYPE, bound_claims, verify_artifact


def issue_authorization(
    *,
    tenant_id: uuid.UUID,
    merchant_id: uuid.UUID,
    buyer_ref: str,
    max_amount_minor: int,
    per_purchase_limit_minor: int,
    allowed_skus: set[str] | frozenset[str] | list[str] | None,
    expires_at: datetime | None = None,
    buyer_consent_sha256: str | None = None,
) -> str:
    mode = os.environ.get("RESERVE_SIGNER_MODE", "local")
    if mode == "remote":
        return _remote_authorization(
            tenant_id=tenant_id,
            merchant_id=merchant_id,
            buyer_ref=buyer_ref,
            max_amount_minor=max_amount_minor,
            per_purchase_limit_minor=per_purchase_limit_minor,
            allowed_skus=allowed_skus,
            expires_at=expires_at,
            buyer_consent_sha256=buyer_consent_sha256,
        )
    if mode != "local":
        raise ValueError("Unsupported Reserve signer mode")
    authority_id = uuid7()
    key = JWK.from_json(os.environ["RESERVE_PROVIDER_SIGNING_JWK"])
    signer = InProcessSigner.from_jwk(key)
    claims = {
        **bound_claims(
            authority_id=authority_id,
            tenant_id=tenant_id,
            merchant_id=merchant_id,
            buyer_ref=buyer_ref,
            currency="INR",
            max_amount_minor=max_amount_minor,
            per_purchase_limit_minor=per_purchase_limit_minor,
            allowed_skus=allowed_skus,
            expires_at=expires_at,
        ),
        "version": 1,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "issued_at": int(time.time()),
        "jti": str(uuid7()),
        "nonce": str(uuid7()),
        "initial_epoch": 0,
        "provider_reference": f"sim_reserve_{authority_id.hex}",
    }
    if buyer_consent_sha256 is not None:
        claims["version"] = 2
        claims["buyer_consent_sha256"] = buyer_consent_sha256
    artifact = signer.sign({"typ": TYPE}, canonicalize(claims))
    # Catch a misconfigured public/private pair before persisting an active permission.
    verify_artifact(artifact)
    return artifact


def _remote_authorization(**values: Any) -> str:
    from datetime import UTC
    from urllib.parse import urlsplit

    import httpx
    from google.auth.transport.requests import Request
    from google.oauth2.id_token import fetch_id_token
    from reserve_trust import load_policy

    if os.environ.get("RESERVE_PROVIDER_SIGNING_JWK"):
        raise ValueError("Remote signing requires removing the API private key")
    # Fail before any request; a bad trust configuration must never trigger enrollment.
    load_policy(os.environ["RESERVE_TRUST_CONFIG_PATH"])
    url = os.environ["RESERVE_SIGNER_URL"].rstrip("/")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Signer must be an operator-configured HTTPS origin")
    if values.pop("buyer_consent_sha256") is not None:
        raise ValueError("Remote simulator does not attest verified buyer consent")
    values["tenant_id"] = str(values["tenant_id"])
    values["merchant_id"] = str(values["merchant_id"])
    values["currency"] = "INR"
    values["allowed_skus"] = (
        None if values["allowed_skus"] is None else sorted(values["allowed_skus"])
    )
    values["expires_at"] = (
        None if values["expires_at"] is None else values["expires_at"].astimezone(UTC).isoformat()
    )
    try:
        identity = fetch_id_token(Request(), url)  # type: ignore[no-untyped-call]
        # No automatic retry after an uncertain signing response. No private-key fallback.
        with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as client:
            response = client.post(
                url + "/v1/authorizations",
                json=values,
                headers={
                    "Authorization": "Bearer " + identity,
                    "X-Serverless-Authorization": "Bearer " + identity,
                },
            )
            response.raise_for_status()
            artifact = response.json()["artifact_jws"]
        if not isinstance(artifact, str):
            raise ValueError("Signer did not return a compact JWS")
        claims = verify_artifact(artifact)
        if any(claims.get(name) != value for name, value in values.items()):
            raise ValueError("Signer returned different authorization bounds")
        return artifact
    except Exception as exc:
        raise ValueError("Remote Reserve authorization unavailable") from exc
