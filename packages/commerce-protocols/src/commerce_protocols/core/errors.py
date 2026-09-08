"""What a protocol adapter refuses with, and why refusal is a first-class outcome.

Two different things get called "an error" at a protocol boundary and conflating them is
the mistake this module exists to prevent.

A **rejection** is the adapter working. The signature did not verify, the version is not
pinned, the nonce was replayed, the mandate names an amount the checkout does not carry.
Nothing is broken; an external party asked for something it may not have, and the honest
answer is a structured refusal that says which check failed. These carry a
:class:`~commerce_domain.RecoveryCode` so the rest of the platform -- the problem
handler in ``commerce_api.errors``, the inspector, the audit trail -- treats them the same
way it treats every other deterministic refusal.

A **fault** is this platform working badly: a key that will not load, a stored artifact
that will not parse, an adapter handed a request its own router should have filtered.
Those are bugs, they map to 5xx, and they must never be dressed up as the caller's fault.

Everything here is a rejection. Faults are left to raise as whatever they naturally are,
because inventing a wrapper for them would only make a 500 look considered.

The reason strings are stable keys, not sentences. Specification 6.7's rule for kernel
decisions applies just as hard at a protocol edge: the code carries the decision and the
prose carries only the explanation, so a caller may branch on ``reason`` and a renderer
may translate it without either being able to change what happened.
"""

from __future__ import annotations

from typing import Any

from commerce_domain import RecoveryCode

__all__ = [
    "AuthenticationRejected",
    "CorrelationRejected",
    "MandateRejected",
    "ProtocolRejection",
    "ReplayRejected",
    "SchemaRejected",
    "SignatureRejected",
    "StateRejected",
    "VersionRejected",
]


class ProtocolRejection(Exception):  # noqa: N818 - a refusal is an outcome, not a failure
    """An external request refused at the protocol boundary.

    ``reason`` is a stable snake_case key naming the check that failed. ``details`` carries
    machine-readable context for the inspector and the audit record.

    Note what is deliberately *not* here: the raw artifact that failed. A rejection travels
    back to the caller and into logs, and echoing an unverified signature or a full mandate
    into either is how a verifier becomes an oracle. The evidence record keeps the artifact
    (:mod:`commerce_protocols.core.evidence`); the rejection keeps only the verdict.
    """

    #: Overridden per subclass. The kernel's vocabulary, so a protocol refusal and a kernel
    #: denial are the same kind of thing to everything downstream.
    code: RecoveryCode = RecoveryCode.POLICY_EXCEPTION

    def __init__(self, reason: str, **details: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details

    def __str__(self) -> str:
        if not self.details:
            return self.reason
        rendered = ", ".join(f"{k}={v!r}" for k, v in sorted(self.details.items()))
        return f"{self.reason} ({rendered})"


class AuthenticationRejected(ProtocolRejection):
    """Step 1 of specification 13.1: the caller is not who it claims to be.

    ``AUTHORITY_INSUFFICIENT`` rather than a bespoke code, because from the platform's
    point of view an unauthenticated caller and an authenticated one holding no capability
    are the same fact: nothing here authorises what was asked.
    """

    code = RecoveryCode.AUTHORITY_INSUFFICIENT


class VersionRejected(ProtocolRejection):
    """Step 2: a version this platform has no fixtures for (13.2)."""

    code = RecoveryCode.POLICY_EXCEPTION


class SchemaRejected(ProtocolRejection):
    """Step 2: the payload does not validate against the pinned schema."""

    code = RecoveryCode.POLICY_EXCEPTION


class SignatureRejected(ProtocolRejection):
    """Step 3: a signature, algorithm, key or canonicalization check failed.

    Covers the whole cryptographic surface deliberately. A caller learns *that* the
    artifact did not verify and which stage refused it; it does not learn whether the
    algorithm was wrong, the key unknown or the bytes tampered with, because a verifier
    that distinguishes those is a verifier an attacker can interrogate.
    """

    code = RecoveryCode.AUTHORITY_INSUFFICIENT


class ReplayRejected(ProtocolRejection):
    """Step 3: a nonce, request id or timestamp window says this arrived before.

    ``DUPLICATE_OPERATION`` is wrong here and the difference matters. That code means "you
    already did this and here is the result", which is a *success* the caller may show a
    buyer. A replayed nonce is not a repeat of a completed operation; it is a request the
    platform refuses to consider at all, and nothing was done under it.
    """

    code = RecoveryCode.POLICY_EXCEPTION


class MandateRejected(ProtocolRejection):
    """Step 5: the mandate verified cryptographically but does not authorise this.

    Expired, wrong audience, wrong checkout, wrong amount, superseded version, revoked
    epoch. The signature was good and the authority is still absent -- which is exactly the
    distinction specification 15.3 draws when it says no mandate reaches the kernel as
    authority until *all* applicable checks pass.
    """

    code = RecoveryCode.AUTHORITY_INSUFFICIENT


class CorrelationRejected(ProtocolRejection):
    """Step 5: two artifacts that must name each other do not.

    Specifically AP2's payment-mandate-to-checkout-mandate binding (15.2, 15.4). Held apart
    from :class:`MandateRejected` because a correlation failure means the caller presented
    a well-formed mandate for a *different* transaction, and that is worth being able to
    find in an audit trail without reading the details.
    """

    code = RecoveryCode.AUTHORITY_INSUFFICIENT


class StateRejected(ProtocolRejection):
    """Step 7: the platform's state moved and the request no longer fits it.

    ``STALE_CHECKOUT`` is the honest code: the request was legitimate when it was built and
    something has changed since. The caller's remedy is to re-read and try again, which is
    what a stale-state code tells every layer above.
    """

    code = RecoveryCode.STALE_CHECKOUT
