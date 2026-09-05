"""A deterministic external AI buyer, specification 16.1.

Nothing else in this repository can exercise the ACP surface honestly. The gate in
:mod:`commerce_protocols.acp.auth` refuses anything that is not correctly signed, so a test
that wants to prove a *positive* -- that a well-formed request is admitted, mapped and
answered -- has to be able to mint one. And a test that wants to prove a negative has to be
able to mint one that is wrong in exactly one respect, which is harder: a hand-assembled bad
request is usually wrong in three ways and proves only that the first check fired.

So the simulator signs correctly by default and misbehaves on request, one named way at a
time. Every :class:`Misbehaviour` produces a request that is otherwise perfect, which is
what makes a test asserting a specific refusal reason meaningful rather than coincidental.
``OVERSIZED_BODY`` and ``WRONG_CONTENT_TYPE`` are signed *honestly* over the offending body,
so a passing test proves the transport limit refused it and not the signature; likewise
``WRONG_API_VERSION`` announces an unpinned version and signs that version, so what the test
observes is the pin doing its job.

Determinism
-----------
The simulator reads no clock and draws no randomness. The instant comes from the caller --
which in a test is the database clock, the same clock
:func:`~commerce_protocols.core.replay.assert_fresh` judges against -- and nonces and
idempotency keys come from a counter seeded by a string. Two runs of the same script produce
byte-identical requests, so a fixture can be recorded from this and compared later
(specification 13.3's raw-request fixture), and a failing replay test fails for the reason
it names rather than because two `uuid4`s happened to differ.

The counter is the one piece of mutable state here, and it is what ``REPLAYED_NONCE`` reuses:
the misbehaviour is not "send a random nonce twice" but "send the previous request's nonce
again", which is what a captured-and-replayed request actually looks like.

What the simulator is not
-------------------------
It is not a conformance oracle. It speaks this surface's dialect, including the completion
echo that :mod:`commerce_protocols.acp.sessions` requires and ACP does not, so a request it
produces proves that this adapter is self-consistent -- not that an ACP client written
against the published schema would be accepted unchanged. Specification 13.2 pins ACP at
``COMPATIBLE_INTERFACE`` for that reason, and :mod:`commerce_protocols.acp.claims` is what
stops the difference from being lost in a summary.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final
from uuid import UUID

from commerce_domain import sha256_b64url

from ..core import PINS, PROTOCOL_CAPABILITIES, Protocol
from .auth import (
    ACCEPTED_CONTENT_TYPE,
    API_VERSION_HEADER,
    AUTHORIZATION_HEADER,
    IDEMPOTENCY_KEY_HEADER,
    MAX_BODY_BYTES,
    REQUEST_ID_HEADER,
    SIGNATURE_ALGORITHM,
    SIGNATURE_ALGORITHM_HEADER,
    SIGNATURE_CLIENT_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    AcpClient,
    AcpRequest,
    RateLimit,
    sign,
    signing_string,
)
from .sessions import BASE_PATH

__all__ = [
    "FOREIGN_SIGNING_SECRET",
    "AcpBuyerSimulator",
    "Credential",
    "Misbehaviour",
]

#: A key this platform has never issued. Every negative signature test needs one, and having
#: it here rather than in the test file means all of them are wrong in the same way.
FOREIGN_SIGNING_SECRET: Final[bytes] = b"a-secret-this-platform-has-never-issued"

#: How far outside the five-minute window the clock misbehaviours land. One minute of margin
#: past the ceiling, so a test cannot pass or fail on rounding.
_OUTSIDE_WINDOW: Final[timedelta] = timedelta(minutes=6)

#: An unpinned version, announced and signed, for the version-rejection case.
_UNPINNED_VERSION: Final[str] = "2019-01-01"


class Credential(StrEnum):
    """Which of specification 16.3's two mechanisms this request presents."""

    SIGNATURE = "SIGNATURE"
    API_KEY = "API_KEY"


class Misbehaviour(StrEnum):
    """One way to be wrong, holding everything else right.

    Each member names the single check it is built to trip. A member that could trip two
    would make the test using it prove less than it appears to.
    """

    #: Sign and send the same well-formed request.
    NONE = "NONE"
    #: Sign one body, send a different one. Trips the body digest inside the signature.
    TAMPERED_BODY = "TAMPERED_BODY"
    #: Sign correctly, then alter one character of the signature.
    TAMPERED_SIGNATURE = "TAMPERED_SIGNATURE"
    #: Sign with a key this platform has not issued.
    WRONG_SIGNING_KEY = "WRONG_SIGNING_KEY"
    #: Sign for a different audience. Valid bytes, valid key, wrong deployment.
    WRONG_AUDIENCE = "WRONG_AUDIENCE"
    #: Timestamp six minutes in the past, signed as such.
    STALE_TIMESTAMP = "STALE_TIMESTAMP"
    #: Timestamp six minutes in the future, signed as such.
    FUTURE_TIMESTAMP = "FUTURE_TIMESTAMP"
    #: Reuse the previous request's nonce, signed correctly with it.
    REPLAYED_NONCE = "REPLAYED_NONCE"
    #: Announce and sign a version the matrix does not pin.
    WRONG_API_VERSION = "WRONG_API_VERSION"
    #: A correctly signed body larger than the transport ceiling.
    OVERSIZED_BODY = "OVERSIZED_BODY"
    #: A correctly signed body sent as ``text/plain``.
    WRONG_CONTENT_TYPE = "WRONG_CONTENT_TYPE"
    #: Omit the idempotency key on a mutation, signing its absence honestly.
    MISSING_IDEMPOTENCY_KEY = "MISSING_IDEMPOTENCY_KEY"
    #: Send neither credential.
    NO_CREDENTIAL = "NO_CREDENTIAL"
    #: Send a signature and a bearer token at once.
    BOTH_CREDENTIALS = "BOTH_CREDENTIALS"
    #: Announce an algorithm this surface does not implement.
    WRONG_SIGNATURE_ALGORITHM = "WRONG_SIGNATURE_ALGORITHM"
    #: Well-formed JSON that is not an object.
    NON_OBJECT_BODY = "NON_OBJECT_BODY"


def _canonical(body: Mapping[str, Any]) -> bytes:
    """Serialise a body the same way twice, forever.

    Sorted keys and no whitespace, so the digest inside a signature depends on the content
    and not on the dictionary's insertion order. This is not the platform's JCS
    canonicalization -- that is for hashes the kernel compares -- it is only the simulator
    agreeing with itself about what it sent.
    """
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(slots=True)
class AcpBuyerSimulator:
    """An external AI buyer that signs correctly, and lies only when told to.

    ``issued`` is public and mutable on purpose. A test that wants two runs to produce
    identical bytes resets it; a test that wants the replay case reads ``last_nonce``. Both
    are the simulator's whole state, and hiding them behind properties would only make the
    determinism harder to verify.
    """

    client_id: str
    signing_secret: bytes
    audience: str
    api_key: str | None = None
    api_version: str = PINS[Protocol.ACP].version
    seed: str = "acp-sim"
    issued: int = 0
    last_nonce: str = ""
    granted: frozenset[str] = PROTOCOL_CAPABILITIES
    client_rate: RateLimit | None = field(default=None)

    # ------------------------------------------------------------------ registration

    def registration(
        self, *, tenant_id: UUID, merchant_id: UUID, buyer_ref: str | None = None
    ) -> AcpClient:
        """The registry entry that makes this simulator's credentials verify.

        Built from the simulator rather than written out beside it, so a test cannot drift
        into registering one audience and signing for another and then spend an afternoon
        discovering that its "wrong audience" case was passing for the wrong reason.
        """
        return AcpClient(
            client_id=self.client_id,
            tenant_id=tenant_id,
            merchant_id=merchant_id,
            audience=self.audience,
            signing_secret=self.signing_secret,
            api_key_digest=(
                None if self.api_key is None else sha256_b64url(self.api_key.encode("utf-8"))
            ),
            buyer_ref=buyer_ref,
            granted=self.granted,
            client_rate=self.client_rate,
        )

    # ----------------------------------------------------------------- request minting

    def request(
        self,
        *,
        method: str,
        path: str,
        now: datetime,
        body: Mapping[str, Any] | None = None,
        query: str = "",
        idempotency_key: str | None = None,
        credential: Credential = Credential.SIGNATURE,
        misbehave: Misbehaviour = Misbehaviour.NONE,
    ) -> AcpRequest:
        """Mint one request. ``now`` is the caller's clock; the simulator reads none."""
        ordinal = self.issued
        self.issued += 1

        nonce = (
            self.last_nonce if misbehave is Misbehaviour.REPLAYED_NONCE else self._nonce(ordinal)
        )
        self.last_nonce = nonce

        key = self._idempotency_key(method, ordinal, idempotency_key, misbehave)
        stamp = self._timestamp(now, misbehave)
        version = (
            _UNPINNED_VERSION if misbehave is Misbehaviour.WRONG_API_VERSION else self.api_version
        )
        content_type = (
            "text/plain" if misbehave is Misbehaviour.WRONG_CONTENT_TYPE else ACCEPTED_CONTENT_TYPE
        )
        signed_body, sent_body = self._bodies(body, misbehave)

        material = signing_string(
            method=method,
            path=path,
            query=query,
            audience=(
                f"{self.audience}:other"
                if misbehave is Misbehaviour.WRONG_AUDIENCE
                else self.audience
            ),
            api_version=version,
            timestamp=stamp,
            request_id=nonce,
            idempotency_key=key or "",
            content_type=content_type if sent_body else "",
            body=signed_body,
        )
        secret = (
            FOREIGN_SIGNING_SECRET
            if misbehave is Misbehaviour.WRONG_SIGNING_KEY
            else self.signing_secret
        )
        signature = _tamper(sign(secret, material), misbehave)

        headers: dict[str, str] = {
            API_VERSION_HEADER: version,
            TIMESTAMP_HEADER: stamp,
            REQUEST_ID_HEADER: nonce,
            SIGNATURE_CLIENT_HEADER: self.client_id,
        }
        if sent_body:
            headers["Content-Type"] = content_type
        if key is not None:
            headers[IDEMPOTENCY_KEY_HEADER] = key
        headers.update(self._credential_headers(signature, credential, misbehave))
        return AcpRequest.build(
            method=method, path=path, headers=headers, body=sent_body, query=query
        )

    # --------------------------------------------------------------- lifecycle helpers

    def create_session(
        self,
        now: datetime,
        *,
        body: Mapping[str, Any] | None = None,
        misbehave: Misbehaviour = Misbehaviour.NONE,
        idempotency_key: str | None = None,
        credential: Credential = Credential.SIGNATURE,
    ) -> AcpRequest:
        return self.request(
            method="POST",
            path=BASE_PATH,
            now=now,
            body=body
            if body is not None
            else {"items": [{"sku": "MILK-DAIRY-001", "quantity": 2}]},
            misbehave=misbehave,
            idempotency_key=idempotency_key,
            credential=credential,
        )

    def update_session(
        self,
        now: datetime,
        session_id: str,
        *,
        body: Mapping[str, Any],
        misbehave: Misbehaviour = Misbehaviour.NONE,
        idempotency_key: str | None = None,
    ) -> AcpRequest:
        return self.request(
            method="POST",
            path=f"{BASE_PATH}/{session_id}",
            now=now,
            body=body,
            misbehave=misbehave,
            idempotency_key=idempotency_key,
        )

    def retrieve_session(
        self,
        now: datetime,
        session_id: str,
        *,
        misbehave: Misbehaviour = Misbehaviour.NONE,
    ) -> AcpRequest:
        return self.request(
            method="GET", path=f"{BASE_PATH}/{session_id}", now=now, misbehave=misbehave
        )

    def complete_session(
        self,
        now: datetime,
        session_id: str,
        *,
        checkout_version: int,
        content_hash: str,
        amount_minor: int,
        currency: str = "INR",
        misbehave: Misbehaviour = Misbehaviour.NONE,
        idempotency_key: str | None = None,
    ) -> AcpRequest:
        return self.request(
            method="POST",
            path=f"{BASE_PATH}/{session_id}/complete",
            now=now,
            body={
                "checkout_version": checkout_version,
                "content_hash": content_hash,
                "total": {"amount_minor": amount_minor, "currency": currency},
            },
            misbehave=misbehave,
            idempotency_key=idempotency_key,
        )

    def cancel_session(
        self,
        now: datetime,
        session_id: str,
        *,
        misbehave: Misbehaviour = Misbehaviour.NONE,
        idempotency_key: str | None = None,
    ) -> AcpRequest:
        return self.request(
            method="POST",
            path=f"{BASE_PATH}/{session_id}/cancel",
            now=now,
            body={"reason": "buyer_changed_mind"},
            misbehave=misbehave,
            idempotency_key=idempotency_key,
        )

    # ------------------------------------------------------------------------ internals

    def _nonce(self, ordinal: int) -> str:
        return f"{self.seed}-nonce-{ordinal:06d}"

    def _idempotency_key(
        self, method: str, ordinal: int, presented: str | None, misbehave: Misbehaviour
    ) -> str | None:
        """Mint one for a mutation, omit one for a read, obey an explicit choice.

        A read carrying an idempotency key would be harmless and meaningless, and sending
        one anyway would quietly weaken the mutation test: if every request has a key, the
        check that mutations require one never has to be right.
        """
        if misbehave is Misbehaviour.MISSING_IDEMPOTENCY_KEY:
            return None
        if presented is not None:
            return presented
        return None if method.upper() == "GET" else f"{self.seed}-idem-{ordinal:06d}"

    def _timestamp(self, now: datetime, misbehave: Misbehaviour) -> str:
        if misbehave is Misbehaviour.STALE_TIMESTAMP:
            return (now - _OUTSIDE_WINDOW).isoformat()
        if misbehave is Misbehaviour.FUTURE_TIMESTAMP:
            return (now + _OUTSIDE_WINDOW).isoformat()
        return now.isoformat()

    def _bodies(
        self, body: Mapping[str, Any] | None, misbehave: Misbehaviour
    ) -> tuple[bytes, bytes]:
        """The bytes that were signed, and the bytes that are sent. Usually the same.

        ``OVERSIZED_BODY`` and ``NON_OBJECT_BODY`` sign what they send, because the checks
        they target run before and after signature verification respectively and a mismatch
        would let the signature refuse them first.
        """
        if misbehave is Misbehaviour.OVERSIZED_BODY:
            padded = _canonical({"padding": "x" * (MAX_BODY_BYTES + 1024)})
            return padded, padded
        if misbehave is Misbehaviour.NON_OBJECT_BODY:
            raw = b'["not", "an", "object"]'
            return raw, raw
        signed = _canonical(body) if body is not None else b""
        if misbehave is Misbehaviour.TAMPERED_BODY:
            # One field changed after signing, which is what an interception looks like:
            # a larger total on a request whose signature was minted for a smaller one.
            tampered = dict(body or {})
            tampered["total"] = {"amount_minor": 999_999, "currency": "INR"}
            return signed, _canonical(tampered)
        return signed, signed

    def _credential_headers(
        self, signature: str, credential: Credential, misbehave: Misbehaviour
    ) -> dict[str, str]:
        if misbehave is Misbehaviour.NO_CREDENTIAL:
            return {}
        bearer = {AUTHORIZATION_HEADER: f"Bearer {self.api_key}"} if self.api_key else {}
        signed = {
            SIGNATURE_HEADER: signature,
            SIGNATURE_ALGORITHM_HEADER: (
                "HMAC-SHA1"
                if misbehave is Misbehaviour.WRONG_SIGNATURE_ALGORITHM
                else SIGNATURE_ALGORITHM
            ),
        }
        if misbehave is Misbehaviour.BOTH_CREDENTIALS:
            return {**signed, **bearer}
        return bearer if credential is Credential.API_KEY else signed


def _tamper(signature: str, misbehave: Misbehaviour) -> str:
    """Flip one character, deterministically, without producing a shorter signature.

    Length is preserved because a truncated signature would be refused by
    ``hmac.compare_digest`` on its length alone, and a test proving that would not be
    proving that the HMAC disagreed.
    """
    if misbehave is not Misbehaviour.TAMPERED_SIGNATURE or not signature:
        return signature
    head = "B" if signature[0] == "A" else "A"
    return head + signature[1:]
