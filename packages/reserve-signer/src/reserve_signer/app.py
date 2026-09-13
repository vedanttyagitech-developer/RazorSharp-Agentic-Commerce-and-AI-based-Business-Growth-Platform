"""Authenticated simulator issuer; caller input never supplies JWS headers or arbitrary claims."""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from reserve_trust import AUDIENCE, ISSUER, JWKS_PATH, public_ring

from .kms import KmsSigner


class Authorization(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tenant_id: str
    merchant_id: str
    buyer_ref: str = Field(min_length=1, max_length=128)
    max_amount_minor: int = Field(gt=0)
    per_purchase_limit_minor: int = Field(gt=0)
    currency: str = "INR"
    allowed_skus: list[str] | None = Field(default=None, min_length=1, max_length=100)
    expires_at: str | None = None

    @model_validator(mode="after")
    def bounds(self) -> Authorization:
        uuid.UUID(self.tenant_id)
        uuid.UUID(self.merchant_id)
        if self.per_purchase_limit_minor > self.max_amount_minor:
            raise ValueError("purchase limit exceeds capacity")
        if self.allowed_skus is not None and (
            len(set(self.allowed_skus)) != len(self.allowed_skus)
            or any(not sku.strip() or len(sku) > 128 for sku in self.allowed_skus)
        ):
            raise ValueError("invalid product scope")
        if self.expires_at is not None:
            expiry = datetime.fromisoformat(self.expires_at)
            if expiry.tzinfo is None or expiry.timestamp() <= time.time():
                raise ValueError("expiry must be a future timezone-aware timestamp")
        return self


def authorize(body: Authorization, caller: str, policy: dict[str, Any]) -> None:
    # This file is provisioned by the signer operator, never sent by the API.
    if (
        policy.get("version") != 1
        or type(policy.get("expires_at")) is not int
        or policy["expires_at"] <= time.time()
    ):
        raise HTTPException(503, "Signer authorization policy expired")
    scope = next(
        (
            s
            for s in policy["scopes"]
            if s["caller"] == caller
            and s["tenant_id"] == body.tenant_id
            and s["merchant_id"] == body.merchant_id
        ),
        None,
    )
    if scope is None:
        raise HTTPException(403, "Caller cannot authorize this merchant")
    if body.buyer_ref not in scope.get("buyer_refs", []):
        raise HTTPException(403, "Buyer is not enrolled with this simulator issuer")
    if (
        body.currency != scope["currency"]
        or body.max_amount_minor > scope["max_capacity_minor"]
        or body.per_purchase_limit_minor > scope["max_purchase_minor"]
    ):
        raise HTTPException(403, "Authorization exceeds signer bounds")
    skus = scope.get("allowed_skus")
    if skus is not None and (body.allowed_skus is None or not set(body.allowed_skus) <= set(skus)):
        raise HTTPException(403, "Product scope exceeds signer bounds")
    if body.expires_at is None:
        if scope.get("allow_until_revoked") is not True:
            raise HTTPException(403, "Finite authorization expiry required")
    elif (
        datetime.fromisoformat(body.expires_at).timestamp() > time.time() + scope["max_ttl_seconds"]
    ):
        raise HTTPException(403, "Authorization lifetime exceeds signer bounds")


def claims_for(body: Authorization) -> dict[str, Any]:
    aid = uuid.uuid4()
    return {
        **body.model_dump(),
        "authority_id": str(aid),
        "version": 1,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "issued_at": int(time.time()),
        "jti": str(uuid.uuid4()),
        "nonce": str(uuid.uuid4()),
        "initial_epoch": 0,
        "provider_reference": f"sim_reserve_{aid.hex}",
        "allowed_skus": None if body.allowed_skus is None else sorted(body.allowed_skus),
        "expires_at": None
        if body.expires_at is None
        else datetime.fromisoformat(body.expires_at).astimezone(UTC).isoformat(),
    }


def create_app() -> FastAPI:
    import google.auth
    from google.auth.transport.requests import AuthorizedSession, Request
    from google.oauth2.id_token import verify_oauth2_token

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    signer = KmsSigner(
        AuthorizedSession(credentials),  # type: ignore[no-untyped-call]
        os.environ["RESERVE_KMS_KEY_VERSION"],
        os.environ["RESERVE_KMS_KID"],
        os.environ["RESERVE_KMS_PUBLIC_SHA256"],
    )
    audience = os.environ["RESERVE_SIGNER_AUDIENCE"]
    policy = json.loads(os.environ["RESERVE_SIGNER_POLICY"])

    def verify_identity(token: str) -> dict[str, Any]:
        identity: dict[str, Any] = verify_oauth2_token(token, Request(), audience=audience)  # type: ignore[no-untyped-call]
        return identity

    return build_app(signer, policy, verify_identity)


def build_app(signer: Any, policy: dict[str, Any], verify_identity: Any) -> FastAPI:
    app = FastAPI(title="Isolated Reserve simulator issuer", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok", "issuer_kind": "SIMULATOR"}

    @app.get(JWKS_PATH)
    def keys() -> dict[str, Any]:
        return public_ring({"keys": [signer.jwk]})

    @app.post("/v1/authorizations")
    def issue(body: Authorization, authorization: str = Header(default="")) -> dict[str, str]:
        try:
            scheme, token = authorization.split(" ", 1)
            if scheme.lower() != "bearer":
                raise ValueError("bearer required")
            identity = verify_identity(token)
            caller = identity.get("email")
            if (
                identity.get("email_verified") is not True
                or not isinstance(caller, str)
                or not caller.endswith(".gserviceaccount.com")
            ):
                raise ValueError("verified service identity required")
            if not any(
                s["caller"] == caller and s["caller_subject"] == identity.get("sub")
                for s in policy["scopes"]
            ):
                raise ValueError("verified service identity required")
        except Exception:
            raise HTTPException(401, "Service identity verification failed") from None
        authorize(body, caller, policy)
        try:
            return {"artifact_jws": signer.sign(claims_for(body))}
        except Exception:
            raise HTTPException(503, "Simulator signing unavailable") from None

    return app
