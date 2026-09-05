"""The short-lived, single-use trusted continuation, specification 14.3 step 3.

When a UCP Complete Checkout cannot be honoured headlessly, the platform answers
``requires_escalation`` and hands back a ``continue_url``. That URL is the whole handoff:
following it is how an authenticated buyer arrives at the trusted surface, reviews the same
checkout version and hash, and continues. So it has to carry, unforgeably, exactly which
checkout version the escalation was about -- and it has to stop working almost immediately
and after exactly one use.

Three properties, three separate mechanisms
--------------------------------------------
**Unforgeable** comes from an ES256 signature over the token's claims, verified with the
same pinned profile everything else in this package uses. A ``continue_url`` a caller could
mint would let an external party route a buyer to a trusted approval for a checkout of the
attacker's choosing, which is the one thing the trusted surface exists to prevent.

**Short-lived** comes from an ``exp`` claim checked against the database clock. Minutes, not
hours: the window is only as long as it takes a person to click a link they were just
handed, and every additional minute is time in which a leaked URL is still live.

**Single-use** comes from :func:`commerce_protocols.core.replay.claim_nonce`. This is the
property that cannot be done with a signature: a signed token is inherently replayable,
because verifying it does not change anything. Consumption has to be a write that can only
succeed once, and the kernel already owns a correct one -- an insert against a unique index,
where the loser blocks on the index rather than racing a read.

Why the token names the version and the hash
---------------------------------------------
Specification 14.3 step 4 says the buyer "reviews the same checkout version/hash". Carrying
both in the token is what makes "the same" checkable. If the token named only the checkout
id, then a merchant-state change between escalation and continuation would leave the buyer
approving a different total from the one the escalation was issued for -- and the whole
version/hash discipline the kernel enforces would have a hole in it exactly at the point
where a human is asked to consent.

What a continuation is not
---------------------------
It is not authority to move money, and consuming one authorises nothing. It gets a buyer to
a page. The approval that page records, and the kernel admission that follows, are the same
ones every other surface goes through -- which is the point of specification 13.1's
protocol-neutral core, and the reason this module produces a URL rather than a decision.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from urllib.parse import quote, urlencode

from commerce_domain import canonicalize, uuid7
from sqlalchemy.orm import Session

from ..ap2.signing import KeyRing, Signer, verify_compact
from ..core.errors import MandateRejected, SchemaRejected, StateRejected
from ..core.pins import Protocol
from ..core.replay import claim_nonce, database_now

__all__ = [
    "DEFAULT_CONTINUATION_TTL",
    "MAX_CONTINUATION_TTL",
    "ContinuationClaims",
    "consume_continuation",
    "issue_continuation",
]

#: How long a continuation stays valid. Long enough for a person to follow a link they were
#: just handed, and no longer.
DEFAULT_CONTINUATION_TTL: Final[timedelta] = timedelta(minutes=10)

#: A ceiling a caller cannot raise. A configurable TTL with no upper bound is a
#: configurable security property, and this one is not up for negotiation per-tenant.
MAX_CONTINUATION_TTL: Final[timedelta] = timedelta(minutes=15)

#: The ``typ`` on a continuation's protected header. A token minted for one purpose must not
#: verify for another, and this is what keeps a continuation from being presented as, say, a
#: receipt to a verifier that only checked the signature.
CONTINUATION_TYP: Final[str] = "ucp-continuation+jwt"


@dataclass(frozen=True, slots=True)
class ContinuationClaims:
    """What a continuation asserts. Every field is checked on consumption."""

    tenant_id: uuid.UUID
    checkout_id: uuid.UUID
    version: int
    content_hash: str
    #: The UCP checkout id the external party knows this order by, echoed so the trusted
    #: surface can show the buyer the same identifier their agent showed them.
    external_checkout_id: str
    nonce: str
    issued_at: datetime
    expires_at: datetime


def issue_continuation(
    *,
    tenant_id: uuid.UUID,
    checkout_id: uuid.UUID,
    version: int,
    content_hash: str,
    external_checkout_id: str,
    signer: Signer,
    base_url: str,
    now: datetime,
    ttl: timedelta = DEFAULT_CONTINUATION_TTL,
) -> tuple[str, ContinuationClaims]:
    """Mint a continuation and the URL that carries it.

    ``now`` is passed in rather than read here so the caller can supply the database
    transaction clock, which is the clock :func:`consume_continuation` will judge expiry
    against. A token stamped from a pod's clock and judged against the database's would
    expire early or late by the skew between them, and the failure would be intermittent.

    Returns the URL and the claims, so a caller can record in evidence exactly what it
    issued without having to parse back what it just built.
    """
    if ttl > MAX_CONTINUATION_TTL:
        raise ValueError(
            f"continuation ttl {ttl} exceeds the {MAX_CONTINUATION_TTL} ceiling; a "
            "trusted handoff that outlives the buyer's attention is a live URL nobody "
            "is watching"
        )
    if ttl <= timedelta(0):
        raise ValueError("continuation ttl must be positive")

    claims = ContinuationClaims(
        tenant_id=tenant_id,
        checkout_id=checkout_id,
        version=version,
        content_hash=content_hash,
        external_checkout_id=external_checkout_id,
        nonce=uuid7().hex,
        issued_at=now,
        expires_at=now + ttl,
    )
    token = signer.sign(
        {"typ": CONTINUATION_TYP},
        _encode(claims),
    )
    query = urlencode({"continuation": token}, quote_via=quote)
    return f"{base_url.rstrip('/')}/checkouts/{checkout_id}/continue?{query}", claims


def _encode(claims: ContinuationClaims) -> bytes:
    """The claims as canonical JSON bytes, for signing.

    Canonicalized for the same reason everything else in this package is: the bytes that
    get signed have to be a function of the content, so that a verifier reconstructing them
    reconstructs the same ones.
    """
    return canonicalize(
        {
            "tenant_id": str(claims.tenant_id),
            "checkout_id": str(claims.checkout_id),
            "version": claims.version,
            "content_hash": claims.content_hash,
            "external_checkout_id": claims.external_checkout_id,
            "nonce": claims.nonce,
            "iat": int(claims.issued_at.timestamp()),
            "exp": int(claims.expires_at.timestamp()),
        }
    )


def consume_continuation(
    session: Session,
    token: str,
    ring: KeyRing,
    *,
    tenant_id: uuid.UUID,
    current_version: int,
    current_content_hash: str,
) -> ContinuationClaims:
    """Verify a continuation, spend it, and confirm it still describes current state.

    The order matters and is the same principle the AP2 verifier follows: establish what the
    token *claims* before asking whether the claim is still true. Signature, then expiry,
    then single-use consumption, then the comparison against current state.

    Consumption happens *before* the state comparison, deliberately. A token presented
    against a superseded version is spent anyway, because it has been used -- allowing a
    retry after a state-mismatch refusal would leave a live URL in the hands of whoever
    triggered the mismatch, and the buyer's remedy is a fresh escalation carrying the new
    version rather than a second attempt at the old one.

    Nothing here authorises anything. It confirms this buyer arrived by a trusted route for
    this exact checkout version; the approval and the admission are still ahead.
    """
    payload = verify_compact(token, ring)
    claims = _decode(payload)

    if claims.tenant_id != tenant_id:
        # A continuation minted for another tenant. Refused as a state mismatch rather than
        # a signature failure, because the signature was fine -- and refused before the
        # nonce is claimed, since a cross-tenant token must not be able to burn a nonce in
        # this tenant's records.
        raise StateRejected(
            "continuation_belongs_to_another_tenant", checkout_id=str(claims.checkout_id)
        )

    now = database_now(session)
    if claims.expires_at <= now:
        raise MandateRejected(
            "continuation_expired",
            expired_at=claims.expires_at.isoformat(),
        )

    # Single use. A replayed continuation is refused here, by an insert that can only
    # succeed once, rather than by a read that could race another presentation of the same
    # token.
    claim_nonce(
        session,
        protocol=Protocol.UCP,
        client_id="continuation",
        nonce=claims.nonce,
        request_digest=claims.content_hash,
    )

    if claims.version != current_version or claims.content_hash != current_content_hash:
        raise StateRejected(
            "continuation_describes_a_superseded_version",
            continuation_version=claims.version,
            current_version=current_version,
        )
    return claims


def _decode(payload: dict[str, object]) -> ContinuationClaims:
    """Turn a verified payload into claims, refusing anything malformed.

    Verified bytes can still be the wrong shape -- a signature proves who wrote something,
    never that they wrote it correctly -- so every field is checked rather than trusted.
    """
    try:
        return ContinuationClaims(
            tenant_id=uuid.UUID(str(payload["tenant_id"])),
            checkout_id=uuid.UUID(str(payload["checkout_id"])),
            version=int(str(payload["version"])),
            content_hash=str(payload["content_hash"]),
            external_checkout_id=str(payload["external_checkout_id"]),
            nonce=str(payload["nonce"]),
            issued_at=datetime.fromtimestamp(int(str(payload["iat"])), tz=UTC),
            expires_at=datetime.fromtimestamp(int(str(payload["exp"])), tz=UTC),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SchemaRejected("continuation_claims_malformed") from exc
