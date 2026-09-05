"""The protocol evidence rule, specification 13.3.

The requirement, stated plainly: after the fact, a reviewer must be able to reconstruct
what an external party asked for and what this platform did about it. Not a log line saying
a request was rejected -- the request, the version it announced, which check refused it, and
the answer that went back.

Every protocol interaction therefore opens an evidence stream and appends one record per
stage of the specification 13.1 pipeline. The stream is written through
:func:`transaction_kernel.audit.append`, which is not a logging call: it maintains a
gapless, hash-chained sequence per aggregate, where each row's ``self_hash`` covers the
previous row's. Deleting a stage, reordering two, or editing an amount after the fact
breaks the chain from that point to the head, and the platform already ships a verifier
that says so at ``/v1/audit/streams/{type}/{id}/verify``.

Riding on the audit chain rather than on the ``protocol_messages`` table sketched in
specification 25.4 is a deliberate decision, recorded in ADR 0005. Section 25.4 names six
protocol tables and gives no columns for any of them; nothing in this repository creates
them. Inventing six schemas would have meant tamper-evidence I would have had to build,
a tenant-isolation policy I would have had to get right, and a role grant set I would have
had to argue for -- all of it duplicating a mechanism the kernel already has and already
proves. The audit chain is the stronger substrate, and using it keeps this package free of
any schema change at all.

What is never written here
--------------------------
Specification 28 is explicit that the inspector must never display credentials, private
keys, full payment signatures or unnecessary PII, and evidence is what the inspector reads.
So the rule is applied at the point of writing rather than at the point of display: a
secret that never enters the chain cannot leak from it, and the chain is immutable, so a
mistake here could not be redacted afterwards even if somebody wanted to.

Concretely, an artifact that carries or proves a secret is recorded as
:func:`fingerprint` -- its algorithm, its length and a SHA-256 digest. That is enough for a
reviewer to confirm two records name the same bytes, and to confirm a stored fixture
matches what arrived, without the bytes themselves being in the row. Request *bodies* are
kept verbatim, because the body is the ask and redacting it would defeat the entire rule;
credentials travel in headers and in signature material, not in the ask.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from commerce_domain import sha256_b64url, uuid7
from sqlalchemy.orm import Session
from transaction_kernel import ActorType, AgentPrincipal, KernelDecision
from transaction_kernel import audit as kernel_audit

from .pins import ProtocolPin

__all__ = [
    "AGGREGATE_TYPE",
    "MAX_FINGERPRINT_PREVIEW",
    "EvidenceStage",
    "ProtocolInteraction",
    "fingerprint",
    "open_interaction",
]

#: The audit aggregate every protocol evidence stream is written under. One constant so the
#: inspector, the verifier endpoint and this module cannot drift apart.
AGGREGATE_TYPE: Final[str] = "protocol_interaction"

#: How much of an artifact may appear in cleartext beside its digest. Eight characters is
#: enough to correlate two records by eye in an inspector and far too little to reconstruct
#: a signature from.
MAX_FINGERPRINT_PREVIEW: Final[int] = 8


class EvidenceStage(StrEnum):
    """One stage of the specification 13.1 pipeline.

    The values are the seven steps, plus the two outcomes. A stream that reaches
    ``REJECTED`` names the stage that refused it in its payload, so "where did this fail"
    is answerable from the row rather than by re-running the request.
    """

    #: Step 0. The bytes arrived. Written before anything has been trusted about them.
    RECEIVED = "RECEIVED"
    #: Step 1. The caller is who it claims to be, by a named mechanism.
    AUTHENTICATED = "AUTHENTICATED"
    #: Step 2. The pinned version and the schema both accept the payload.
    VALIDATED = "VALIDATED"
    #: Step 3. Signatures, timestamps, nonce, audience and replay constraints pass.
    VERIFIED = "VERIFIED"
    #: Step 4. The request became a typed internal intent.
    MAPPED = "MAPPED"
    #: Step 5. A VerifiedAuthorityProof was produced. Absent when no financial authority
    #: was presented, which is the normal case for discovery and construction.
    AUTHORITY_PROVEN = "AUTHORITY_PROVEN"
    #: Step 6. The kernel answered. Allowed or denied; both are recorded identically.
    DECIDED = "DECIDED"
    #: Step 7. The protocol-shaped answer that went back to the caller.
    ANSWERED = "ANSWERED"
    #: A refusal at any earlier stage. Terminal.
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """A secret-bearing artifact, recorded without recording the secret."""

    algorithm: str
    length: int
    digest: str
    preview: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "length": self.length,
            "digest": self.digest,
            "preview": self.preview,
        }


def fingerprint(artifact: str | bytes, *, algorithm: str = "SHA-256") -> Fingerprint:
    """Record that an artifact was seen, and which one, without recording its content.

    Used for signatures, bearer tokens, API keys and any compact serialization whose bytes
    prove something. The digest lets a reviewer confirm a stored fixture is byte-identical
    to what arrived; the length catches truncation; the preview lets two rows be correlated
    by eye. None of the three reconstructs the artifact.
    """
    raw = artifact.encode("utf-8") if isinstance(artifact, str) else artifact
    text = artifact if isinstance(artifact, str) else raw.decode("utf-8", errors="replace")
    return Fingerprint(
        algorithm=algorithm,
        length=len(raw),
        digest=sha256_b64url(raw),
        preview=text[:MAX_FINGERPRINT_PREVIEW],
    )


@dataclass(frozen=True, slots=True)
class ProtocolInteraction:
    """One external request's evidence stream, from arrival to answer.

    Created by :func:`open_interaction` and carried through the adapter. Every ``record``
    call appends to the same hash chain, so the stages of one request are provably the
    stages of *that* request rather than a set of rows that happen to share a correlation
    id.

    Nothing here commits. Appends land in the caller's transaction, which means evidence
    for a rejected request commits exactly when the rejection does, and evidence for an
    admitted one commits with the admission. A crash cannot leave a decision without its
    evidence or evidence without its decision.
    """

    interaction_id: uuid.UUID
    pin: ProtocolPin
    tenant_id: uuid.UUID
    principal: AgentPrincipal
    correlation_id: uuid.UUID

    def record(
        self,
        session: Session,
        stage: EvidenceStage,
        **payload: Any,
    ) -> None:
        """Append one stage to this interaction's chain.

        The pin travels on every row rather than only on the first. A reviewer reading a
        single row in isolation -- which is what happens in an inspector, in a log search,
        in a screenshot pasted into a review -- must be able to see which protocol version
        the platform believed it was speaking, without having to find the head of the
        stream to learn it.
        """
        kernel_audit.append(
            session,
            tenant=self.tenant_id,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=self.interaction_id,
            event_type=f"protocol.{stage.value.lower()}",
            actor_type=ActorType.PROTOCOL,
            principal_id=self.principal.principal_id,
            payload={
                "protocol": self.pin.protocol.value,
                "protocol_version": self.pin.version,
                "source_ref": self.pin.source_ref,
                "stage": stage.value,
                **payload,
            },
            correlation_id=self.correlation_id,
        )

    def record_received(
        self,
        session: Session,
        *,
        endpoint: str,
        announced_version: str | None,
        body: Mapping[str, Any] | None,
        headers_seen: Mapping[str, str],
        credential: Fingerprint | None = None,
    ) -> None:
        """Step 0, and the row that makes reconstruction possible.

        ``body`` is kept verbatim: it is what the external party asked for, and a redacted
        ask is not evidence of anything. ``headers_seen`` must already be reduced to
        non-secret headers by the adapter -- a signature or a bearer token belongs in
        ``credential`` as a fingerprint, never here.

        ``announced_version`` is recorded even when it is wrong, and especially then: a
        caller repeatedly announcing an unpinned version is the signal that somebody's
        integration is pointed at the wrong contract, and it is only visible if the
        rejected value is kept.
        """
        self.record(
            session,
            EvidenceStage.RECEIVED,
            endpoint=endpoint,
            announced_version=announced_version,
            request=dict(body) if body is not None else None,
            headers=dict(headers_seen),
            credential=None if credential is None else credential.as_payload(),
        )

    def record_decision(self, session: Session, decision: KernelDecision) -> None:
        """Step 6. The kernel's answer, allowed or denied, recorded identically.

        A denial is written with the same weight as an approval because a platform that
        only evidences what it permitted cannot explain what it refused, and refusing well
        is most of what this architecture does. The deltas are kept because they are the
        substance of a ``REAPPROVAL_REQUIRED``: they say precisely what moved between the
        buyer's approval and the merchant's current truth.
        """
        self.record(
            session,
            EvidenceStage.DECIDED,
            decision_id=str(decision.decision_id),
            allowed=decision.allowed,
            code=decision.code.value,
            explanation=decision.explanation,
            grant_id=None if decision.grant_id is None else str(decision.grant_id),
            payment_attempt_id=(
                None if decision.payment_attempt_id is None else str(decision.payment_attempt_id)
            ),
            next_version=decision.next_version,
            deltas=[
                {"field": d.field_path, "approved": d.approved, "current": d.current}
                for d in decision.deltas
            ],
        )

    def record_rejection(
        self,
        session: Session,
        *,
        stage: EvidenceStage,
        reason: str,
        code: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """A refusal, naming the stage that refused it.

        ``stage`` is the stage that *failed*, not ``REJECTED`` -- the row's own stage is
        ``REJECTED`` and ``failed_at`` says where. Keeping both means a reviewer can filter
        a stream for every rejection and still sort them by which check did the refusing,
        which is the question actually asked when an integration starts failing.
        """
        self.record(
            session,
            EvidenceStage.REJECTED,
            failed_at=stage.value,
            reason=reason,
            code=code,
            details=dict(details) if details else {},
        )


def open_interaction(
    *,
    pin: ProtocolPin,
    tenant_id: uuid.UUID,
    principal: AgentPrincipal,
    correlation_id: uuid.UUID,
    interaction_id: uuid.UUID | None = None,
) -> ProtocolInteraction:
    """Begin an evidence stream for one external request.

    The identifier is a UUIDv7 so that streams sort by arrival time, which is what an
    inspector wants and what makes a keyset scan over recent protocol traffic cheap.

    Nothing is written here. An interaction that is opened and never recorded leaves no
    trace, which is correct: the first row is written when the bytes arrive, and a request
    that never arrived should not appear to have.
    """
    return ProtocolInteraction(
        interaction_id=interaction_id or uuid7(),
        pin=pin,
        tenant_id=tenant_id,
        principal=principal,
        correlation_id=correlation_id,
    )
