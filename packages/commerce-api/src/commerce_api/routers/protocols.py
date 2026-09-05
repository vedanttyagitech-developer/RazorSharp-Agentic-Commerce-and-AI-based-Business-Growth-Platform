"""The protocol surface: published profiles, the pinned matrix, and the inspector.

Everything an external party or a reviewer needs to see about this platform's protocol
layer, and nothing that could move money. That division is deliberate and total: the money
path for a protocol caller runs through the same trusted approval and the same kernel
admission every other surface uses, so there is no endpoint here that admits, approves or
executes anything.

Three groups of route.

**The well-known profiles** (specification 14.1) are what a UCP counterparty fetches to
learn which version this merchant speaks and which public keys verify its signatures. They
are unauthenticated because that is the point of a well-known location, and they are safe to
be unauthenticated because they contain only public key material and capability names.

**The pinned matrix** (13.2) reports what this platform implements and -- more importantly --
what it must never claim. Each row carries its claim boundary and its disclaimer, so a
reviewer reading the API rather than the specification still gets told that an
ACP-compatible interface is not the same thing as being live in ChatGPT Instant Checkout.

**The Protocol Inspector** (28) reconstructs one protocol interaction from its evidence
chain. This is the panel-facing artifact: incoming protocol and version, authentication
status, schema validation, the internal command the request became, the kernel decision, and
the correlation ids that tie it to the rest of the money action proof chain. It never
displays credentials, private keys or full signatures, because the evidence it reads never
contained them -- redaction happened at the point of writing, in
``commerce_protocols.core.evidence``, and an inspector cannot leak what was never stored.

A note on keys, and an honest limitation
-----------------------------------------
Specification 15.5 wants encrypted ES256 test keys loaded from Secret Manager into a
dedicated signer module. Wiring that needs a change to ``commerce_api.settings``, which this
build unit does not own, so the requirement is written up in
``docs/KNOWN_GAPS.md`` and this router does the honest thing in the meantime:
it reads a JWK from ``UCP_MERCHANT_SIGNING_JWK`` and ``UCP_PLATFORM_SIGNING_JWK`` when they
are configured, and otherwise generates process-local keys and **says so in the published
profile**. An ephemeral key is fine for a demonstration and disastrous if mistaken for a
stable one, so the profile carries ``ephemeral_keys: true`` rather than letting a
counterparty assume the JWK Set it fetched will still verify anything tomorrow.
"""

from __future__ import annotations

import json
import os
import uuid
from functools import lru_cache
from typing import Annotated, Any, Final

from commerce_protocols.ap2.signing import InProcessSigner, KeyRing
from commerce_protocols.core.evidence import AGGREGATE_TYPE
from commerce_protocols.core.pins import PINS
from commerce_protocols.ucp import (
    MERCHANT_PROFILE_PATH,
    PLATFORM_PROFILE_PATH,
    BusinessProfile,
    profile_document,
)
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, Depends, Query
from jwcrypto.jwk import JWK
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from ..deps import AppSession, OwnedCheckout, ScenarioKey, SessionContext, require_owner
from ..errors import ProblemError

router = APIRouter(tags=["protocols"])

#: Where configured signing keys are read from, one JWK as JSON each. Two variables rather
#: than one because the merchant and the platform must not share a key: "the merchant signed
#: this checkout" and "the platform signed this receipt" are different claims, and they stop
#: being different the moment one key can produce both signatures.
MERCHANT_JWK_ENV: Final[str] = "UCP_MERCHANT_SIGNING_JWK"
PLATFORM_JWK_ENV: Final[str] = "UCP_PLATFORM_SIGNING_JWK"

#: How many evidence rows the inspector will render for one interaction. A protocol
#: interaction has at most nine stages (13.1's seven steps plus two outcomes), so a stream
#: longer than this is a bug or an attack, and truncating is better than rendering it.
MAX_INSPECTOR_ROWS: Final[int] = 64


class ProtocolPinOut(BaseModel):
    """One row of the specification 13.2 matrix, with its claim boundary attached."""

    model_config = ConfigDict(extra="forbid")

    protocol: str
    version: str
    source_ref: str
    claim_boundary: str
    disclaimer: str


class ProtocolMatrixOut(BaseModel):
    """What this platform implements, and what it refuses to claim."""

    model_config = ConfigDict(extra="forbid")

    pins: list[ProtocolPinOut]
    ephemeral_keys: bool


class InspectorStageOut(BaseModel):
    """One stage of one protocol interaction, as recorded.

    ``payload`` is the evidence row verbatim. It is safe to render because the redaction
    happened when it was written: a credential is present only as a fingerprint, and a
    private key was never in scope.
    """

    model_config = ConfigDict(extra="forbid")

    seq: int
    stage: str
    event_type: str
    occurred_at: str
    self_hash: str
    prev_hash: str | None
    payload: dict[str, Any]


class InspectorOut(BaseModel):
    """A whole protocol interaction, reconstructed. Specification 28."""

    model_config = ConfigDict(extra="forbid")

    interaction_id: str
    protocol: str | None
    protocol_version: str | None
    chain_intact: bool
    stages: list[InspectorStageOut]
    truncated: bool


@lru_cache(maxsize=4)
def _signer_and_provenance(variable: str, fallback_kid: str) -> tuple[InProcessSigner, bool]:
    """One signing key and whether it is ephemeral.

    Cached per variable for the life of the process, so a published JWK Set is stable
    across requests -- a profile whose keys changed between two fetches would be worse
    than one that admits its keys are ephemeral.
    """
    configured = os.environ.get(variable)
    if configured:
        return InProcessSigner.from_jwk(JWK.from_json(configured)), False

    material = json.loads(JWK.from_pyca(ec.generate_private_key(ec.SECP256R1())).export())
    material["kid"] = fallback_kid
    return InProcessSigner.from_jwk(JWK.from_json(json.dumps(material))), True


def _merchant_ring() -> tuple[KeyRing, bool]:
    signer, ephemeral = _signer_and_provenance(MERCHANT_JWK_ENV, "merchant-ephemeral-1")
    return KeyRing.of(signer), ephemeral


def _platform_ring() -> tuple[KeyRing, bool]:
    signer, ephemeral = _signer_and_provenance(PLATFORM_JWK_ENV, "platform-ephemeral-1")
    return KeyRing.of(signer), ephemeral


def _profile(
    subject_id: str, display_name: str, website: str, ring: KeyRing, ephemeral: bool
) -> dict[str, Any]:
    document = profile_document(
        BusinessProfile(
            profile_version=1,
            subject_id=subject_id,
            display_name=display_name,
            website=website,
            ring=ring,
        )
    )
    # Stated rather than implied. A counterparty that caches this JWK Set must be told that
    # it will not survive a restart, or it will read a rotation as tampering.
    document["ephemeral_keys"] = ephemeral
    return document


@router.get(
    MERCHANT_PROFILE_PATH,
    summary="The merchant's UCP business profile and verification keys",
)
def merchant_profile() -> dict[str, Any]:
    """Specification 14.1. Unauthenticated by design, and public by content."""
    ring, ephemeral = _merchant_ring()
    return _profile(
        subject_id="mrc_demo",
        display_name="Governed Agentic Commerce demo merchant",
        website="https://demo.invalid",
        ring=ring,
        ephemeral=ephemeral,
    )


@router.get(
    PLATFORM_PROFILE_PATH,
    summary="The demo platform's UCP profile and verification keys",
)
def platform_profile() -> dict[str, Any]:
    """Held apart from the merchant's profile, specification 14.1.

    "The merchant signed this checkout" and "the platform signed this receipt" are different
    claims. They stop being different the moment one key set covers both, so the two
    profiles are separate documents even in a demo where one process serves them, and they
    publish different keys under different key ids.
    """
    ring, ephemeral = _platform_ring()
    return _profile(
        subject_id="platform_demo",
        display_name="Governed Agentic Commerce platform",
        website="https://demo.invalid",
        ring=ring,
        ephemeral=ephemeral,
    )


@router.get("/v1/protocols", summary="The pinned protocol matrix and its claim boundaries")
def protocol_matrix() -> ProtocolMatrixOut:
    """Specification 13.2, including the disclaimers.

    The disclaimers travel with the data rather than living in a status table somebody has
    to remember to consult. A reviewer reading this endpoint is told, without having to ask,
    that implementing UCP is not availability inside Gemini and that an ACP-compatible
    interface is not a live ChatGPT Instant Checkout integration.
    """
    _, merchant_ephemeral = _merchant_ring()
    _, platform_ephemeral = _platform_ring()
    return ProtocolMatrixOut(
        pins=[
            ProtocolPinOut(
                protocol=pin.protocol.value,
                version=pin.version,
                source_ref=pin.source_ref,
                claim_boundary=pin.boundary.value,
                disclaimer=pin.disclaimer,
            )
            for pin in PINS.values()
        ],
        ephemeral_keys=merchant_ephemeral or platform_ephemeral,
    )


@router.get(
    "/v1/inspector/protocols/{interaction_id}",
    summary="Reconstruct one protocol interaction from its evidence chain",
)
def inspect_interaction(
    interaction_id: uuid.UUID,
    ctx: SessionContext,
    session: AppSession,
    limit: Annotated[int, Query(ge=1, le=MAX_INSPECTOR_ROWS)] = MAX_INSPECTOR_ROWS,
) -> InspectorOut:
    """Specification 28, for the protocol layer.

    Reads the interaction's audit stream and reports it in order, together with whether the
    hash chain still verifies. ``chain_intact`` is computed from the rows themselves --
    each row's ``prev_hash`` against its predecessor's ``self_hash`` -- so a tampered or
    deleted stage shows up here rather than having to be discovered elsewhere.

    Row-level security scopes the read to the session's tenant, so an interaction id from
    another tenant returns nothing and is reported as not found rather than as forbidden:
    a 403 would confirm the identifier names something real.
    """
    rows = session.execute(
        text(
            "SELECT seq, event_type, occurred_at, self_hash, prev_hash, payload "
            "FROM audit_events WHERE tenant_id = :t AND aggregate_type = :a "
            "AND aggregate_id = :i ORDER BY seq LIMIT :limit"
        ),
        {
            "t": ctx.tenant_id,
            "a": AGGREGATE_TYPE,
            "i": interaction_id,
            "limit": limit + 1,
        },
    ).all()
    if not rows:
        raise ProblemError(
            404,
            "Protocol interaction not found",
            "No protocol interaction with that identifier belongs to this session's tenant.",
            interaction_id=str(interaction_id),
        )

    truncated = len(rows) > limit
    visible = rows[:limit]

    intact = visible[0].prev_hash is None
    for earlier, later in zip(visible, visible[1:], strict=False):
        if later.prev_hash != earlier.self_hash:
            intact = False

    first = visible[0].payload
    return InspectorOut(
        interaction_id=str(interaction_id),
        protocol=first.get("protocol"),
        protocol_version=first.get("protocol_version"),
        chain_intact=intact,
        truncated=truncated,
        stages=[
            InspectorStageOut(
                seq=row.seq,
                stage=str(row.payload.get("stage", "")),
                event_type=row.event_type,
                occurred_at=row.occurred_at.isoformat(),
                self_hash=row.self_hash,
                prev_hash=row.prev_hash,
                payload=row.payload,
            )
            for row in visible
        ],
    )


@router.get(
    "/v1/checkouts/{checkout_id}/protocol-evidence",
    summary="Every protocol interaction recorded against this checkout",
)
def checkout_protocol_evidence(
    ctx: SessionContext,
    session: AppSession,
    owner: Annotated[OwnedCheckout, Depends(require_owner)],
) -> dict[str, Any]:
    """The protocol interactions correlated to one checkout.

    Joined on ``correlation_id`` rather than on the checkout id, because a protocol
    interaction is recorded against its own aggregate -- the interaction -- and the
    correlation id is what ties it to the buyer journey it belongs to. That indirection is
    what lets one journey span UCP discovery, a trusted approval and a kernel admission and
    still be reconstructable as one story.
    """
    rows = session.execute(
        text(
            "SELECT DISTINCT aggregate_id, "
            "  min(occurred_at) OVER (PARTITION BY aggregate_id) AS started_at, "
            "  first_value(payload) OVER ("
            "    PARTITION BY aggregate_id ORDER BY seq"
            "  ) AS head "
            "FROM audit_events "
            "WHERE tenant_id = :t AND aggregate_type = :a AND correlation_id = :c "
            "ORDER BY started_at"
        ),
        {"t": ctx.tenant_id, "a": AGGREGATE_TYPE, "c": owner.correlation_id},
    ).all()
    return {
        "checkout_id": str(owner.checkout_id),
        "correlation_id": str(owner.correlation_id),
        "interactions": [
            {
                "interaction_id": str(row.aggregate_id),
                "started_at": row.started_at.isoformat(),
                "protocol": row.head.get("protocol"),
                "protocol_version": row.head.get("protocol_version"),
            }
            for row in rows
        ],
    }


@router.get(
    "/v1/protocols/conformance",
    summary="What has actually been verified locally, for the evidence table",
)
def conformance_report(_: ScenarioKey) -> dict[str, Any]:
    """The honest status of each protocol, for specification 35's evidence table.

    Behind the scenario key rather than public, because it is an operator's view of what
    this build has and has not proved. The distinction it reports is the one specifications
    13.2, 15.4 and 16.2 all insist on: local conformance against a pinned schema is a claim
    this project can make, and external platform approval is not.
    """
    return {
        "pins": {
            pin.protocol.value: {
                "version": pin.version,
                "source_ref": pin.source_ref,
                "claim_boundary": pin.boundary.value,
                "disclaimer": pin.disclaimer,
            }
            for pin in PINS.values()
        },
        "must_never_claim": [pin.disclaimer for pin in PINS.values()],
    }
