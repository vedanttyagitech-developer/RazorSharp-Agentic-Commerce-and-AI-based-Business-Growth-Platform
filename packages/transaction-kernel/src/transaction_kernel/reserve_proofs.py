"""Cryptographic Reserve authorization evidence; no signing key belongs in the Kernel.

A simulator signature attests a trusted demo authorization, never a bank funds block.
Original signed bounds are immutable. Revocation and allocation remain live DB state.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from commerce_domain import canonicalize, sha256_hex, uuid7
from jwcrypto.jwk import JWK
from jwcrypto.jws import JWS
from platform_db import require_tenant
from sqlalchemy import text
from sqlalchemy.orm import Session

ISSUER = "razorsharp-reserve-provider-simulator"
AUDIENCE = "razorsharp-reserve"
TYPE = "reserve-authority+jws"


class ReserveProofError(ValueError):
    pass


def verify_artifact(artifact: str, *, jwks: str | None = None) -> dict[str, Any]:
    """Verify with the deployment's public trust set, never a key carried by a token."""
    try:
        if not isinstance(artifact, str) or len(artifact) > 32768 or artifact.count(".") != 2:
            raise ReserveProofError("compact_authority_signature_required")
        ring = json.loads(
            jwks if jwks is not None else os.environ["RESERVE_PROVIDER_VERIFICATION_JWKS"]
        )
        token = JWS()
        token.allowed_algs = ["ES256"]
        token.deserialize(artifact)
        header = token.jose_header
        if (
            set(header) != {"alg", "kid", "typ"}
            or header.get("alg") != "ES256"
            or header.get("typ") != TYPE
        ):
            raise ReserveProofError("unsupported_authority_signature_profile")
        kid = header.get("kid")
        if not isinstance(kid, str) or kid in ring.get("revoked_kids", []):
            raise ReserveProofError("unknown_or_revoked_authority_key")
        keys = [k for k in ring["keys"] if k.get("kid") == kid]
        if len(keys) != 1:
            raise ReserveProofError("unknown_or_ambiguous_authority_key")
        key = keys[0]
        if key.get("kty") != "EC" or key.get("crv") != "P-256" or "d" in key:
            raise ReserveProofError("verification_key_must_be_public_p256")
        token.verify(JWK(**key), alg="ES256")
        claims = json.loads(token.payload)
        if not isinstance(claims, dict):
            raise ReserveProofError("authority_claims_not_object")
        if token.payload != canonicalize(claims):
            raise ReserveProofError("authority_payload_not_canonical")
        expected = {
            "version",
            "iss",
            "aud",
            "authority_id",
            "tenant_id",
            "merchant_id",
            "buyer_ref",
            "currency",
            "max_amount_minor",
            "per_purchase_limit_minor",
            "allowed_skus",
            "expires_at",
            "issued_at",
            "jti",
            "nonce",
            "provider_reference",
            "initial_epoch",
        }
        if claims.get("version") == 2:
            expected.add("buyer_consent_sha256")
            consent = claims.get("buyer_consent_sha256")
            if (
                not isinstance(consent, str)
                or len(consent) != 64
                or any(c not in "0123456789abcdef" for c in consent)
            ):
                raise ReserveProofError("authority_consent_digest_invalid")
        if set(claims) != expected or claims["iss"] != ISSUER or claims["aud"] != AUDIENCE:
            raise ReserveProofError("authority_issuer_audience_or_claims_invalid")
        if (
            type(claims["version"]) is not int
            or claims["version"] not in (1, 2)
            or type(claims["initial_epoch"]) is not int
            or claims["initial_epoch"] != 0
        ):
            raise ReserveProofError("authority_version_or_epoch_invalid")
        if type(claims["issued_at"]) is not int or claims["issued_at"] > time.time() + 30:
            raise ReserveProofError("authority_issue_time_invalid")
        for name in ("authority_id", "tenant_id", "merchant_id", "jti", "nonce"):
            uuid.UUID(claims[name])
        if not isinstance(claims["buyer_ref"], str) or not claims["buyer_ref"]:
            raise ReserveProofError("authority_buyer_missing")
        if not isinstance(claims["provider_reference"], str) or not claims[
            "provider_reference"
        ].startswith("sim_reserve_"):
            raise ReserveProofError("simulator_reference_required")
        for name in ("max_amount_minor", "per_purchase_limit_minor"):
            value = claims[name]
            if value is not None and (type(value) is not int or value <= 0):
                raise ReserveProofError("authority_amount_invalid")
        if claims["max_amount_minor"] is None:
            raise ReserveProofError("authority_capacity_required")
        if (
            claims["per_purchase_limit_minor"] is not None
            and claims["per_purchase_limit_minor"] > claims["max_amount_minor"]
        ):
            raise ReserveProofError("authority_limit_exceeds_capacity")
        if (
            not isinstance(claims["currency"], str)
            or len(claims["currency"]) != 3
            or not claims["currency"].isascii()
            or not claims["currency"].isalpha()
            or not claims["currency"].isupper()
        ):
            raise ReserveProofError("authority_currency_invalid")
        if claims["expires_at"] is not None:
            expiry = datetime.fromisoformat(claims["expires_at"])
            if expiry.tzinfo is None or expiry.astimezone(UTC).isoformat() != claims["expires_at"]:
                raise ReserveProofError("authority_expiry_invalid")
        skus = claims["allowed_skus"]
        if skus is not None and (
            not isinstance(skus, list)
            or not skus
            or any(not isinstance(s, str) or not s for s in skus)
            or skus != sorted(set(skus))
        ):
            raise ReserveProofError("authority_scope_invalid")
        return claims
    except ReserveProofError:
        raise
    except Exception as exc:
        raise ReserveProofError("authority_signature_unverifiable") from exc


def bound_claims(
    *,
    authority_id: uuid.UUID,
    tenant_id: uuid.UUID,
    merchant_id: uuid.UUID,
    buyer_ref: str,
    currency: str,
    max_amount_minor: int,
    per_purchase_limit_minor: int | None,
    allowed_skus: set[str] | frozenset[str] | list[str] | None,
    expires_at: datetime | None,
) -> dict[str, Any]:
    return {
        "authority_id": str(authority_id),
        "tenant_id": str(tenant_id),
        "merchant_id": str(merchant_id),
        "buyer_ref": buyer_ref,
        "currency": currency,
        "max_amount_minor": max_amount_minor,
        "per_purchase_limit_minor": per_purchase_limit_minor,
        "allowed_skus": None if allowed_skus is None else sorted(allowed_skus),
        "expires_at": None if expires_at is None else expires_at.astimezone(UTC).isoformat(),
    }


def persist(session: Session, artifact: str, expected: dict[str, Any]) -> uuid.UUID:
    claims = verify_artifact(artifact)
    if any(claims[k] != v for k, v in expected.items()):
        raise ReserveProofError("authority_signed_bounds_mismatch")
    if str(require_tenant(session)) != claims["tenant_id"]:
        raise ReserveProofError("authority_tenant_mismatch")
    identifier = uuid7()
    session.execute(
        text(
            "INSERT INTO verified_authority_proofs "
            "(id,tenant_id,authority_id,issuer,jti,nonce,artifact_jws,"
            "payload_sha256,provider_reference) "
            "VALUES (:id,:t,:a,:iss,:jti,:nonce,:artifact,:hash,:ref)"
        ),
        {
            "id": identifier,
            "t": uuid.UUID(claims["tenant_id"]),
            "a": uuid.UUID(claims["authority_id"]),
            "iss": claims["iss"],
            "jti": uuid.UUID(claims["jti"]),
            "nonce": uuid.UUID(claims["nonce"]),
            "artifact": artifact,
            "hash": sha256_hex(canonicalize(claims)),
            "ref": claims["provider_reference"],
        },
    )
    return identifier


def verify_authority(session: Session, authority_id: uuid.UUID) -> bool:
    """Re-verify signed bounds against the locked authority at admission and execution."""
    tenant = require_tenant(session)
    row = session.execute(
        text(
            "SELECT a.*, p.artifact_jws, p.payload_sha256 FROM delegated_authorities a "
            "LEFT JOIN verified_authority_proofs p ON p.id=a.reserve_proof_id "
            "AND p.tenant_id=a.tenant_id "
            "AND p.authority_id=a.id WHERE a.tenant_id=:t AND a.id=:a FOR UPDATE OF a"
        ),
        {"t": tenant, "a": authority_id},
    ).one_or_none()
    if row is None or not row.artifact_jws:
        return False
    try:
        claims = verify_artifact(row.artifact_jws)
        expected = bound_claims(
            authority_id=row.id,
            tenant_id=row.tenant_id,
            merchant_id=row.merchant_id,
            buyer_ref=row.buyer_ref,
            currency=row.currency,
            max_amount_minor=row.max_amount_minor,
            per_purchase_limit_minor=row.per_purchase_limit_minor,
            allowed_skus=row.allowed_skus,
            expires_at=row.expires_at,
        )
        return (
            row.kind == "RESERVE"
            and all(claims[k] == v for k, v in expected.items())
            and row.payload_sha256 == sha256_hex(canonicalize(claims))
        )
    except ReserveProofError:
        return False
