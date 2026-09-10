"""Separate ES256 simulator credential issuer for trusted buyer authorization.

No NPCI/bank confirmation is claimed. The private key is provisioned once outside code;
the Kernel and debit worker require only its public verification set.
"""

import os
import time
import uuid
from datetime import datetime

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
