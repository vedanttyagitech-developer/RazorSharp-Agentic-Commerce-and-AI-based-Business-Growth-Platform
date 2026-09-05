"""The front door of the ACP surface, specification 16.3.

This is the only public, unauthenticated-by-default endpoint family in the platform: an
external AI buyer, run by somebody else, on hardware we do not control, pointed at a URL
that moves money three hops later. Everything downstream -- the intent mapping, the session
lifecycle, the kernel -- is written on the assumption that whoever got past this module is
who they say they are. So this module has to be right, and it has to be right in a way a
reviewer can check by reading it.

What a caller must prove
------------------------
One of two credentials, never both. A protocol API key, which proves possession of a
long-lived secret issued to one client for one audience; or an HTTP message signature over
a canonical signing string, which additionally proves that *this* request -- this method,
this path, these bytes -- was authorised by the holder of that secret. The signature is the
stronger of the two and is what the simulator uses; the key path exists because
specification 16.3 offers it and because a read-only integration should not be forced to
implement canonicalization to list a catalogue.

Presenting both is refused rather than resolved. Two credentials are two answers to "who is
this", and picking one on the caller's behalf is a decision the caller did not make -- which
is precisely the shape of every credential-confusion bug worth having.

The canonical signing string
----------------------------
An HMAC only proves something about the bytes it covers, so the interesting question is
never the algorithm, it is what got fed to it. The string built by :func:`signing_string`
covers the method, the path, the query, the audience, the announced API version, the
timestamp, the request id, the idempotency key, the content type and a digest of the body.
Change any one of them and the signature stops verifying, which is the property the whole
module rests on.

Every field is length-prefixed before being joined. Without that, an attacker who controls
two adjacent fields can move the boundary between them -- a path ending in a newline and a
query beginning with one produce the same joined bytes as a different path and a different
query -- and the signature over the forgery is genuinely valid. Length-prefixing makes the
encoding injective, so exactly one tuple of fields produces any given string. The domain
prefix on the front does the same job across protocols: bytes signed for this adapter can
never be replayed as bytes signed for another.

The audience is not read from the request. It is supplied by the verifier from its own
configuration, and a caller has no way to influence it. This is what makes audience binding
mean anything: a signature minted for the sandbox audience presented to production does not
verify, because production builds its signing string with the production audience and gets a
different HMAC. Taking the audience from a header would reduce the check to "the caller
agrees with itself", which is not a check.

Covering the audience in the signed bytes is only half of audience binding, and the missing
half is easy to lose because the first half looks so much like the whole thing. The signing
string stops a *captured* signature from being replayed at another deployment. It says
nothing about whether the client that minted it was ever issued credentials for the
deployment it is now talking to: a client holding its own secret can sign over any audience
string it likes, including one it was never registered for. So both mechanisms check the
registration as well -- ``AcpClient.audience`` must equal the configured audience -- and the
check sits after the credential has verified, where it can only be reached by somebody who
already proved possession and therefore discloses nothing to anybody else.

Freshness, replay and the clock
-------------------------------
Neither is implemented here. :func:`~commerce_protocols.core.replay.assert_fresh` and
:func:`~commerce_protocols.core.replay.claim_nonce` are the platform's single implementation
of both, they judge against the database clock rather than this process's, and the nonce
store is a real unique index rather than a dictionary with a race in it. A second
implementation living at the ACP edge would be a second thing to get right and a second
thing to keep in step; there is exactly one, and this module calls it.

Order of operations
-------------------
Fixed, and the order is the design:

1. transport limits -- body size and content type -- before any cryptography, so a caller
   cannot make this process hash a gigabyte to learn that it will be refused;
2. credential verification, before anything that could disclose configuration. Every
   refusal after this point names something about how this platform is set up -- which
   versions it pins, how fast it will answer -- and an anonymous caller is entitled to none
   of it;
3. evidence opens, because only now is there a verified tenant to write it under. A refusal
   before this point is deliberately not written into any tenant's audit chain: there is no
   tenant it belongs to, and letting an unauthenticated caller append rows to one it merely
   named would turn the evidence rule into a write primitive;
4. the body is parsed as JSON only after the signature over its bytes has verified, exactly
   as ADR 0003 D7 requires of the Razorpay webhook receiver, and for the same reason -- a
   JSON parser is a large attack surface and unverified bytes should never reach one. It is
   then checked against what the audit chain can canonicalize, because a body that cannot
   be evidenced cannot be honoured; see :func:`_unrecordable`;
5. version pin, rate limit, freshness, nonce, then the mutation rules. Cheap and stateless
   before expensive and stateful, so a refused caller never reaches the *nonce store* --
   the one write here that persists past the request. Note what this ordering does not buy:
   the ``RECEIVED`` evidence row is written at step 3, before the limiter runs, so a
   throttled caller still appends two rows to its tenant's chain per attempt. That is the
   price of specification 13.3's rule that what arrived must be recorded whether or not it
   was honoured, and it is stated here rather than left for somebody to discover from a
   chain that grew faster than the traffic that was admitted.

Rate limiting in one process
----------------------------
The token buckets here are in-process dictionaries, which would be dishonest in a
horizontally scaled service and is not here: ADR 0003 D14 fixes this platform at one API
process and the settings object refuses ``WEB_CONCURRENCY > 1``, so the in-process bucket is
the whole system's bucket rather than one shard of it. The buckets are driven by the
database clock passed in, not by ``time.monotonic()``, so a test can advance time exactly
and the limiter behaves identically on a laptop that has been asleep. Refill arithmetic is
integer throughout; this module contains no floating-point operation of any kind, which is
the same rule the money types obey and for a related reason -- a limiter that drifts is a
limiter nobody can reason about.

Rate limiting happens *after* authentication, which is a real trade-off rather than an
oversight. Limiting on an unverified client id would let anyone exhaust a legitimate
client's bucket by putting its name in a header, so the buckets are keyed by verified
identity only. The cost is that an anonymous flood is bounded by the transport limits and
the cost of one HMAC rather than by a bucket, which is the cheaper of the two failures.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Final, NoReturn

from commerce_domain import b64url, sha256_b64url, uuid7
from sqlalchemy.orm import Session
from transaction_kernel import RecoveryCode
from transaction_kernel.idempotency import MAX_KEY_LENGTH

from ..core import (
    PINS,
    PROTOCOL_CAPABILITIES,
    AuthenticatedCaller,
    AuthenticationRejected,
    EvidenceStage,
    Protocol,
    ProtocolInteraction,
    ProtocolRejection,
    ReplayRejected,
    SchemaRejected,
    SignatureRejected,
    UnsupportedVersionError,
    VersionRejected,
    assert_fresh,
    assert_never_consents,
    claim_nonce,
    database_now,
    fingerprint,
    open_interaction,
    principal_for,
    require_pin,
)
from ..core.evidence import Fingerprint
from ..core.replay import DEFAULT_MAX_REQUEST_AGE

__all__ = [
    "ACCEPTED_CONTENT_TYPE",
    "API_VERSION_HEADER",
    "AUTHORIZATION_HEADER",
    "IDEMPOTENCY_KEY_HEADER",
    "MAX_BODY_BYTES",
    "MAX_IDEMPOTENCY_KEY_LENGTH",
    "REQUEST_ID_HEADER",
    "SAFE_HEADERS",
    "SIGNATURE_ALGORITHM",
    "SIGNATURE_ALGORITHM_HEADER",
    "SIGNATURE_CLIENT_HEADER",
    "SIGNATURE_HEADER",
    "SIGNING_DOMAIN",
    "TIMESTAMP_HEADER",
    "AcpClient",
    "AcpRequest",
    "AdmittedRequest",
    "ClientRegistry",
    "PayloadRejected",
    "RateLimit",
    "RateLimited",
    "TokenBucketLimiter",
    "admit",
    "sign",
    "signing_string",
]

# --------------------------------------------------------------------------- headers

#: The bearer credential for the API-key mechanism. ``Bearer`` scheme, one token.
AUTHORIZATION_HEADER: Final[str] = "Authorization"
#: Base64url HMAC over :func:`signing_string`.
SIGNATURE_HEADER: Final[str] = "Signature"
#: Which registered client minted the signature. The ``keyid`` of RFC 9421, carried in its
#: own header so that the canonical string never has to be parsed to find out whose secret
#: to verify it with.
SIGNATURE_CLIENT_HEADER: Final[str] = "Signature-Client"
#: Announced signature algorithm. Never used to dispatch -- see :data:`SIGNING_DOMAIN`.
SIGNATURE_ALGORITHM_HEADER: Final[str] = "Signature-Algorithm"
#: RFC 3339 instant the request was signed at. Judged against the database clock.
TIMESTAMP_HEADER: Final[str] = "Timestamp"
#: The nonce. Named ``Request-Id`` because that is what an ACP client already sends.
REQUEST_ID_HEADER: Final[str] = "Request-Id"
#: Required on every mutation (16.3), and covered by the signature.
IDEMPOTENCY_KEY_HEADER: Final[str] = "Idempotency-Key"
#: The pinned contract the caller believes it is speaking (13.2).
API_VERSION_HEADER: Final[str] = "API-Version"
CONTENT_TYPE_HEADER: Final[str] = "Content-Type"

#: Headers that may be copied verbatim into evidence. Everything absent from this set is
#: either a credential or unexamined, and specification 28 forbids the first from appearing
#: anywhere the inspector can read. An allowlist rather than a denylist, because a denylist
#: is one forgotten header away from writing a bearer token into an immutable chain.
SAFE_HEADERS: Final[frozenset[str]] = frozenset(
    {
        API_VERSION_HEADER.lower(),
        CONTENT_TYPE_HEADER.lower(),
        TIMESTAMP_HEADER.lower(),
        REQUEST_ID_HEADER.lower(),
        IDEMPOTENCY_KEY_HEADER.lower(),
        SIGNATURE_CLIENT_HEADER.lower(),
        SIGNATURE_ALGORITHM_HEADER.lower(),
    }
)

# ------------------------------------------------------------------------ constants

#: Domain separation. Prefixed to every signing string so bytes signed for this adapter
#: cannot be replayed as bytes signed for another, and so the algorithm is pinned by the
#: material itself rather than by a header a caller controls.
SIGNING_DOMAIN: Final[str] = "ACP-HMAC-SHA256"

#: The one algorithm this surface accepts. A single-entry allowlist is not a limitation to
#: be lifted later; it is the reason algorithm confusion is not reachable from here.
SIGNATURE_ALGORITHM: Final[str] = "HMAC-SHA256"

#: Specification 16.3's body-size limit. An ACP checkout session carries a buyer, a
#: fulfilment address and a line-item list; 64 KiB is roughly two orders of magnitude more
#: than the largest honest one and small enough that refusing costs nothing. Deliberately
#: tighter than ADR 0003 D13's 256 KiB webhook ceiling, because a webhook comes from
#: Razorpay and this comes from anyone.
MAX_BODY_BYTES: Final[int] = 64 * 1024

#: The only media type this surface parses.
ACCEPTED_CONTENT_TYPE: Final[str] = "application/json"

#: How deeply an ACP body may nest. A checkout session is a buyer, an address and a list of
#: line items; sixteen levels is generous for that and shallow enough that walking the body
#: to validate it cannot itself be the denial of service.
MAX_BODY_DEPTH: Final[int] = 16

#: Room for the scoping prefix the session layer adds before the key reaches the kernel's
#: 128-character column, so an over-long key is refused by name here rather than surfacing
#: as an opaque driver error inside a money transaction.
MAX_IDEMPOTENCY_KEY_LENGTH: Final[int] = MAX_KEY_LENGTH - 48

#: Compared against when the presented client id is unknown, so that an unregistered client
#: and a wrong secret cost the same amount of work. See :func:`_verify_credential`.
_DECOY_SECRET: Final[bytes] = b"acp-unregistered-client-decoy-secret"


# --------------------------------------------------------------------------- refusals


class PayloadRejected(ProtocolRejection):
    """The request's transport envelope is outside what this surface accepts.

    Body too large, content type not JSON, a header presented twice with different casing.
    Held apart from :class:`~commerce_protocols.core.SchemaRejected` because these are the
    refusals that happen *before* anything has been authenticated or parsed, and an
    operator reading a burst of them is looking at a misconfigured client or a probe rather
    than at a content disagreement.
    """

    code = RecoveryCode.POLICY_EXCEPTION


class RateLimited(ProtocolRejection):
    """The client or its tenant has spent its budget for now.

    ``POLICY_EXCEPTION`` because the closed :class:`~transaction_kernel.RecoveryCode` enum
    has no rate-limit member and inventing one would mean editing a contract shared by
    every deterministic service in the platform to describe a condition at one edge. The
    details carry ``retry_after_seconds``, which is what a caller actually needs.
    """

    code = RecoveryCode.POLICY_EXCEPTION


# ---------------------------------------------------------------------------- request


@dataclass(frozen=True, slots=True)
class AcpRequest:
    """One inbound HTTP request, reduced to the parts that are signed.

    ``body`` is bytes and stays bytes until the signature over them verifies. Holding a
    parsed object here would make it possible, and eventually normal, to verify a signature
    over a re-serialisation of the body rather than over the body -- which is a signature
    over something the caller never sent.

    Header names are lowercased on construction. Two headers differing only in case are
    refused rather than merged: HTTP says they are the same header, so a request carrying
    both is either a confused client or somebody probing for a proxy that disagrees with
    this process about which one wins.
    """

    method: str
    path: str
    headers: Mapping[str, str]
    body: bytes
    query: str = ""

    @staticmethod
    def build(
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
        query: str = "",
    ) -> AcpRequest:
        lowered: dict[str, str] = {}
        for name, value in headers.items():
            key = name.lower()
            if key in lowered:
                raise PayloadRejected("acp_duplicate_header", header=key)
            lowered[key] = value
        return AcpRequest(
            method=method.upper(),
            path=path,
            headers=MappingProxyType(lowered),
            body=body,
            query=query,
        )

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


@dataclass(frozen=True, slots=True)
class AdmittedRequest:
    """A request that survived every check in specification 16.3.

    Carries the parsed body rather than the bytes, because by the time one of these exists
    the bytes have been verified, digested into evidence and are no longer the interesting
    artifact. ``request_digest`` keeps the link back to them: it is the value the nonce was
    claimed against, so a reviewer can prove which bytes this admission was granted over.

    ``interaction`` is open and already carries the ``RECEIVED`` and ``AUTHENTICATED`` rows.
    The caller continues appending to it -- ``MAPPED``, ``DECIDED``, ``ANSWERED`` -- so the
    whole life of one external request is one hash chain.
    """

    caller: AuthenticatedCaller
    interaction: ProtocolInteraction
    body: Mapping[str, Any]
    request_digest: str
    nonce: str
    timestamp: datetime
    idempotency_key: str | None
    received_at: datetime


# ------------------------------------------------------------------------- registry


@dataclass(frozen=True, slots=True)
class AcpClient:
    """One external AI buyer this platform has issued credentials to.

    ``signing_secret`` is held in the clear because verifying an HMAC requires the secret;
    ``api_key_digest`` is a SHA-256 of the key because verifying a bearer token does not.
    The asymmetry is deliberate and worth keeping: a database dump that leaks this registry
    hands an attacker the ability to forge signatures, which nothing can prevent, but not
    the ability to present the API key, which storing the digest does prevent.

    ``audience`` is what a signature from this client must have been minted for, and what
    an API key from it is scoped to. It lives on the client rather than being global
    because one deployment answers for a sandbox audience and a live one, and a credential
    minted for the first must be inert against the second. Both credential paths compare it
    against the verifier's configured audience after the credential itself has verified,
    because the signing string alone cannot do this job: a client can always sign over an
    audience string it was never registered for.

    ``granted`` defaults to the whole protocol ceiling, which is safe precisely because the
    ceiling is a ceiling: :data:`~commerce_protocols.core.PROTOCOL_CAPABILITIES` already
    excludes every consent capability, so the permissive default cannot grant one. Narrowing
    it is the configuration act, and whatever a tenant configures is intersected with the
    ceiling again before it reaches the kernel.
    """

    client_id: str
    tenant_id: uuid.UUID
    merchant_id: uuid.UUID
    audience: str
    signing_secret: bytes
    api_key_digest: str | None = None
    buyer_ref: str | None = None
    granted: frozenset[str] = PROTOCOL_CAPABILITIES
    client_rate: RateLimit | None = None


@dataclass(frozen=True, slots=True)
class ClientRegistry:
    """The clients this surface will answer to, and nothing else.

    A mapping wrapped in a type rather than a bare dict, so the lookup that refuses an
    unknown client is one function every caller shares. There is no fallback, no wildcard
    and no "unknown clients get read-only": an ACP request from a client this platform has
    not issued credentials to is refused, full stop.
    """

    clients: Mapping[str, AcpClient]

    @staticmethod
    def of(*clients: AcpClient) -> ClientRegistry:
        return ClientRegistry(MappingProxyType({c.client_id: c for c in clients}))

    def get(self, client_id: str | None) -> AcpClient | None:
        return None if client_id is None else self.clients.get(client_id)


# ---------------------------------------------------------------------- rate limits


@dataclass(frozen=True, slots=True)
class RateLimit:
    """A token bucket's shape: how many at once, and how fast they come back.

    Both integers, and the refill is expressed per second rather than as an interval so
    that the arithmetic in :class:`TokenBucketLimiter` stays in whole microseconds. A
    limiter whose refill is a float is a limiter that behaves differently on two machines,
    which makes every rate-limit test flaky in the way that gets tests deleted.
    """

    capacity: int
    refill_per_second: int

    def __post_init__(self) -> None:
        if self.capacity < 1 or self.refill_per_second < 1:
            raise ValueError("a rate limit must permit at least one request and refill it")


#: What one external client may spend. Twenty at once covers an AI buyer building a basket
#: line by line; two per second is far more than a conversational agent needs and far less
#: than a loop.
DEFAULT_CLIENT_RATE: Final[RateLimit] = RateLimit(capacity=20, refill_per_second=2)

#: The ceiling for every client of one tenant together, so one merchant's noisy integration
#: cannot consume the single process ADR 0003 D14 gives us on behalf of all of them.
DEFAULT_TENANT_RATE: Final[RateLimit] = RateLimit(capacity=60, refill_per_second=6)

_MICROSECOND: Final[timedelta] = timedelta(microseconds=1)
_MICROSECONDS_PER_SECOND: Final[int] = 1_000_000


@dataclass(slots=True)
class _Bucket:
    tokens: int
    filled_at: datetime


@dataclass(slots=True)
class TokenBucketLimiter:
    """Per-client and per-tenant token buckets, driven by a clock the caller supplies.

    Both buckets must admit before either is charged. Charging the client bucket and then
    discovering the tenant bucket is empty would spend a token on a request that never
    happened, and a client that is being refused for its neighbour's traffic would slowly
    lose its own budget as well.

    Time only ever moves forward here. The database clock is monotonic within a
    transaction and across committed transactions, but two overlapping transactions can
    read timestamps out of order, and an earlier reading must not be allowed to withdraw
    tokens that a later one already granted.
    """

    tenant_rate: RateLimit = DEFAULT_TENANT_RATE
    _buckets: dict[str, _Bucket] = field(default_factory=dict)

    def take(self, now: datetime, *, client: AcpClient) -> None:
        """Spend one token from the client's bucket and its tenant's, or refuse both."""
        client_rate = client.client_rate or DEFAULT_CLIENT_RATE
        keys = (
            (f"client:{client.tenant_id}:{client.client_id}", client_rate),
            (f"tenant:{client.tenant_id}", self.tenant_rate),
        )
        refilled = [(key, rate, self._refill(key, rate, now)) for key, rate in keys]
        empty = [(key, rate, bucket) for key, rate, bucket in refilled if bucket.tokens < 1]
        if empty:
            key, rate, _ = empty[0]
            raise RateLimited(
                "acp_rate_limit_exhausted",
                scope=key.split(":", 1)[0],
                capacity=rate.capacity,
                refill_per_second=rate.refill_per_second,
                retry_after_seconds=1,
            )
        for _, _, bucket in refilled:
            bucket.tokens -= 1

    def _refill(self, key: str, rate: RateLimit, now: datetime) -> _Bucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=rate.capacity, filled_at=now)
            self._buckets[key] = bucket
            return bucket
        elapsed = (now - bucket.filled_at) // _MICROSECOND
        if elapsed <= 0:
            return bucket
        gained = elapsed * rate.refill_per_second // _MICROSECONDS_PER_SECOND
        if gained <= 0:
            return bucket
        bucket.tokens = min(rate.capacity, bucket.tokens + gained)
        # Advance the clock only by the whole tokens actually granted. Setting it to ``now``
        # would discard the fractional remainder on every call, so a client polling faster
        # than the refill rate would never accrue anything at all.
        bucket.filled_at += timedelta(
            microseconds=gained * _MICROSECONDS_PER_SECOND // rate.refill_per_second
        )
        return bucket


# --------------------------------------------------------------------- signing string

#: The signed fields, in order. Adding one is a breaking change for every client, which is
#: why the list is here rather than assembled from whatever headers happened to arrive.
_SIGNED_FIELDS: Final[tuple[str, ...]] = (
    "domain",
    "method",
    "path",
    "query",
    "audience",
    "api-version",
    "timestamp",
    "request-id",
    "idempotency-key",
    "content-type",
    "body-sha256",
)


def _frame(label: str, value: str) -> bytes:
    """One field, length-prefixed so the joined string decodes to exactly one tuple."""
    raw = value.encode("utf-8")
    return label.encode("ascii") + b"=" + str(len(raw)).encode("ascii") + b":" + raw


def signing_string(
    *,
    method: str,
    path: str,
    query: str,
    audience: str,
    api_version: str,
    timestamp: str,
    request_id: str,
    idempotency_key: str,
    content_type: str,
    body: bytes,
) -> bytes:
    """The exact bytes an ACP client HMACs, and the exact bytes this surface verifies.

    A digest of the body rather than the body itself, so that signing is constant-size and
    a large upload is hashed once instead of twice. The digest is the platform's own
    ``sha256_b64url``, which is what every other content hash in this codebase uses; a
    second encoding of the same digest would be a second thing to get wrong.

    ``idempotency_key`` is the empty string when absent, and the field is signed either
    way. Signing an absent header as "not present" rather than omitting the field is what
    stops an attacker stripping the header from a captured mutation and having the
    signature still verify.
    """
    values = (
        SIGNING_DOMAIN,
        method.upper(),
        path,
        query,
        audience,
        api_version,
        timestamp,
        request_id,
        idempotency_key,
        content_type,
        sha256_b64url(body),
    )
    return b"\n".join(
        _frame(label, value) for label, value in zip(_SIGNED_FIELDS, values, strict=True)
    )


def sign(secret: bytes, material: bytes) -> str:
    """Base64url HMAC-SHA256, unpadded, matching the platform's other digest encodings."""
    return b64url(hmac.new(secret, material, hashlib.sha256).digest())


# --------------------------------------------------------------------------- the gate


def admit(
    session: Session,
    request: AcpRequest,
    *,
    registry: ClientRegistry,
    audience: str,
    limiter: TokenBucketLimiter,
    requires_idempotency_key: bool,
    max_request_age: timedelta = DEFAULT_MAX_REQUEST_AGE,
) -> AdmittedRequest:
    """Run specification 16.3 end to end, or refuse with a structured rejection.

    ``audience`` comes from this deployment's configuration and never from the request; see
    the module docstring. ``requires_idempotency_key`` is the endpoint's own answer to
    "does honouring this change anything", supplied by the router rather than inferred
    here, because an adapter that guesses which of its endpoints mutate will eventually
    guess wrong in the safe-looking direction.

    Every refusal raises. Nothing here returns a code the caller could ignore before
    proceeding to move money, which is the same reasoning
    :func:`~transaction_kernel.audit.append` applies to evidence.
    """
    _check_transport(request)
    client, mechanism, credential = _verify_credential(
        request, registry=registry, audience=audience
    )

    caller = AuthenticatedCaller(
        protocol=Protocol.ACP,
        client_id=client.client_id,
        tenant_id=client.tenant_id,
        merchant_id=client.merchant_id,
        authenticated_by=mechanism,
        correlation_id=uuid7(),
        buyer_ref=client.buyer_ref,
        granted=client.granted,
    )
    # The ceiling has already been intersected away by ``principal_for``, so this cannot
    # fire; it is called anyway, here, because this is the point where an external caller
    # stops being bytes and becomes a principal the rest of the platform will act on, and
    # the cost of being wrong about it once is an outside party consenting on a human's
    # behalf. ``core.identity`` asks every adapter to make the assertion at exactly this
    # point rather than to trust that the intersection above did its job.
    principal = principal_for(caller)
    assert_never_consents(principal)

    pin = PINS[Protocol.ACP]
    interaction = open_interaction(
        pin=pin,
        tenant_id=client.tenant_id,
        principal=principal,
        correlation_id=caller.correlation_id,
    )
    # The ``RECEIVED`` row goes down before the body's own refusal, so that a stream always
    # opens with what arrived. A body the chain cannot canonicalize is recorded as absent
    # rather than repaired: an edited body is not evidence of anything, and the rejection
    # row that follows names the offending path and the digest of the bytes.
    outcome = _readable_body(request.body)
    interaction.record_received(
        session,
        endpoint=f"{request.method} {request.path}",
        announced_version=request.header(API_VERSION_HEADER),
        body=None if isinstance(outcome, SchemaRejected) else outcome,
        headers_seen={k: v for k, v in request.headers.items() if k in SAFE_HEADERS},
        credential=credential,
    )
    if isinstance(outcome, SchemaRejected):
        _refuse(session, interaction, EvidenceStage.VALIDATED, outcome)

    now, signed_at = _run_checks(
        session,
        request,
        interaction=interaction,
        client=client,
        limiter=limiter,
        requires_idempotency_key=requires_idempotency_key,
        max_request_age=max_request_age,
    )
    # The claim boundary rides on the row that says this caller was let in, which is the
    # one place a reviewer is guaranteed to look. Specification 16.2 is a rule about what
    # this project may assert, and an assertion made in an immutable chain is the strongest
    # kind: every admitted ACP request is on the record as COMPATIBLE_INTERFACE.
    interaction.record(
        session,
        EvidenceStage.AUTHENTICATED,
        client_id=client.client_id,
        mechanism=mechanism,
        audience=audience,
        claim_boundary=pin.boundary.value,
    )
    return AdmittedRequest(
        caller=caller,
        interaction=interaction,
        body=outcome,
        request_digest=sha256_b64url(request.body),
        nonce=request.header(REQUEST_ID_HEADER) or "",
        timestamp=signed_at,
        idempotency_key=request.header(IDEMPOTENCY_KEY_HEADER),
        received_at=now,
    )


def _run_checks(
    session: Session,
    request: AcpRequest,
    *,
    interaction: ProtocolInteraction,
    client: AcpClient,
    limiter: TokenBucketLimiter,
    requires_idempotency_key: bool,
    max_request_age: timedelta,
) -> tuple[datetime, datetime]:
    """Everything after authentication, each failure evidenced at the stage that refused it.

    Returns the database clock this request was judged against and the instant the caller
    signed at, so :func:`admit` can put both on the admission rather than re-deriving
    either -- a second read of the clock or a second parse of the timestamp would be a
    second chance for the two to disagree.

    Split out from :func:`admit` so that the mapping from a check to the
    :class:`~commerce_protocols.core.EvidenceStage` it belongs to is a single readable
    table rather than several scattered ``except`` clauses.
    """
    try:
        require_pin(Protocol.ACP, request.header(API_VERSION_HEADER))
    except UnsupportedVersionError as exc:
        _refuse(
            session,
            interaction,
            EvidenceStage.VALIDATED,
            VersionRejected(
                "acp_api_version_not_pinned",
                announced=exc.announced,
                supported=exc.supported,
            ),
        )

    # One clock read for the whole request. PostgreSQL fixes ``now()`` per transaction, so
    # the limiter and the freshness window judge against the same instant by construction
    # rather than by hoping two reads land close together.
    now = database_now(session)
    try:
        limiter.take(now, client=client)
    except RateLimited as exc:
        _refuse(session, interaction, EvidenceStage.AUTHENTICATED, exc)

    try:
        signed_at = _parse_timestamp(request.header(TIMESTAMP_HEADER))
        assert_fresh(session, timestamp=signed_at, max_age=max_request_age)
    except ReplayRejected as exc:
        _refuse(session, interaction, EvidenceStage.VERIFIED, exc)

    try:
        claim_nonce(
            session,
            protocol=Protocol.ACP,
            client_id=client.client_id,
            nonce=request.header(REQUEST_ID_HEADER) or "",
            request_digest=sha256_b64url(request.body),
        )
    except ReplayRejected as exc:
        _refuse(session, interaction, EvidenceStage.VERIFIED, exc)

    if requires_idempotency_key:
        _check_idempotency_key(session, interaction, request.header(IDEMPOTENCY_KEY_HEADER))
    return now, signed_at


def _check_idempotency_key(
    session: Session, interaction: ProtocolInteraction, key: str | None
) -> None:
    """Specification 16.3's last rule: a mutation without a key is not attempted.

    Refused rather than defaulted to the nonce. They are different guarantees and conflating
    them breaks the honest retry: a proxy retrying a timed-out mutation must send the *same*
    idempotency key with a *fresh* nonce, and a caller whose key was silently taken from its
    nonce would create a second checkout every time it retried.
    """
    if not key:
        _refuse(
            session,
            interaction,
            EvidenceStage.VALIDATED,
            SchemaRejected("acp_idempotency_key_required_on_mutation"),
        )
    elif len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        _refuse(
            session,
            interaction,
            EvidenceStage.VALIDATED,
            SchemaRejected(
                "acp_idempotency_key_too_long",
                length=len(key),
                maximum=MAX_IDEMPOTENCY_KEY_LENGTH,
            ),
        )


def _refuse(
    session: Session,
    interaction: ProtocolInteraction,
    stage: EvidenceStage,
    rejection: ProtocolRejection,
) -> NoReturn:
    """Evidence the refusal, then raise it.

    One function rather than a pair of statements at each check, because the pair can be
    written in the wrong order. A refusal that raises before it is recorded leaves an
    evidence stream that stops mid-pipeline with no row saying why, and specification 13.3
    asks for exactly the opposite: a reviewer must be able to see which check refused,
    without re-running the request.

    The append lands in the caller's transaction and commits with the refusal. A caller
    that rolls back keeps neither, which is correct -- there is no state to explain.
    """
    interaction.record_rejection(
        session,
        stage=stage,
        reason=rejection.reason,
        code=rejection.code.value,
        details=rejection.details,
    )
    raise rejection


# ------------------------------------------------------------------ individual checks


def _check_transport(request: AcpRequest) -> None:
    """Size and media type, before a single byte is hashed or parsed.

    A body over the limit is refused on its declared length rather than after being read
    into memory, which only works because whatever handed us this object already bounded
    it; an HTTP layer in front of this must apply the same ceiling to the stream. That is a
    real requirement on the router and it is stated here rather than assumed.
    """
    if len(request.body) > MAX_BODY_BYTES:
        raise PayloadRejected(
            "acp_body_too_large", length=len(request.body), maximum=MAX_BODY_BYTES
        )
    if not request.body:
        return
    declared = (request.header(CONTENT_TYPE_HEADER) or "").split(";", 1)[0].strip().lower()
    if declared != ACCEPTED_CONTENT_TYPE:
        raise PayloadRejected("acp_content_type_not_json", content_type=declared or None)


def _verify_credential(
    request: AcpRequest, *, registry: ClientRegistry, audience: str
) -> tuple[AcpClient, str, Fingerprint]:
    """Establish who the caller is. Returns the client, the mechanism and a fingerprint.

    An unknown client id and a wrong secret are answered identically, and the unknown case
    still performs a full HMAC against a decoy secret before refusing. Skipping the work
    would make the registry enumerable by timing: an attacker who can tell "no such client"
    from "wrong secret" learns which client ids are real, and client ids appear in URLs, in
    support tickets and in screenshots.
    """
    bearer = request.header(AUTHORIZATION_HEADER)
    signature = request.header(SIGNATURE_HEADER)
    if bearer and signature:
        raise AuthenticationRejected("acp_credential_ambiguous")
    if signature:
        return _verify_signature(request, registry=registry, audience=audience)
    if bearer:
        return _verify_api_key(bearer, request=request, registry=registry, audience=audience)
    raise AuthenticationRejected("acp_credential_absent")


def _verify_signature(
    request: AcpRequest, *, registry: ClientRegistry, audience: str
) -> tuple[AcpClient, str, Fingerprint]:
    presented = request.header(SIGNATURE_HEADER) or ""
    announced = request.header(SIGNATURE_ALGORITHM_HEADER)
    if announced is not None and announced != SIGNATURE_ALGORITHM:
        # Refused rather than ignored. Nothing here dispatches on the header -- the domain
        # prefix pins the algorithm inside the signed bytes -- but a client announcing an
        # algorithm this surface does not implement has a mistaken belief about what its
        # signature means, and letting it succeed anyway would confirm the belief.
        raise SignatureRejected("acp_signature_algorithm_unsupported", announced=announced)

    client = registry.get(request.header(SIGNATURE_CLIENT_HEADER))
    material = signing_string(
        method=request.method,
        path=request.path,
        query=request.query,
        audience=audience,
        api_version=request.header(API_VERSION_HEADER) or "",
        timestamp=request.header(TIMESTAMP_HEADER) or "",
        request_id=request.header(REQUEST_ID_HEADER) or "",
        idempotency_key=request.header(IDEMPOTENCY_KEY_HEADER) or "",
        content_type=request.header(CONTENT_TYPE_HEADER) or "",
        body=request.body,
    )
    expected = sign(client.signing_secret if client is not None else _DECOY_SECRET, material)
    if not hmac.compare_digest(expected, presented) or client is None:
        raise SignatureRejected("acp_signature_did_not_verify")
    if client.audience != audience:
        # The HMAC proved the signer meant *this* audience; the registration decides whether
        # it was ever entitled to it. Without this line a client issued for the sandbox
        # would be admitted here simply by signing over the live audience string, and the
        # audience field in the signing string would be binding the caller only to its own
        # intentions. Reached only by a caller that already proved possession of the secret,
        # so naming the reason tells nobody anything they did not already know.
        raise SignatureRejected("acp_signature_audience_not_registered")
    return client, "http_message_signature", fingerprint(presented)


def _verify_api_key(
    bearer: str, *, request: AcpRequest, registry: ClientRegistry, audience: str
) -> tuple[AcpClient, str, Fingerprint]:
    """The weaker mechanism: possession of a long-lived secret, and nothing about the bytes.

    Because a key says nothing about this particular request, the audience cannot be bound
    by the credential and is bound by the registration instead -- a key issued for the
    sandbox audience is inert against the live one. Freshness and the nonce store still
    apply, so a captured request is not replayable; what a stolen key does buy an attacker,
    and a stolen signature does not, is the ability to mint *new* requests. That is the
    trade specification 16.3 offers, and it is why the simulator signs.
    """
    scheme, _, token = bearer.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthenticationRejected("acp_authorization_scheme_unsupported")
    digest = sha256_b64url(token.encode("utf-8"))
    client = registry.get(request.header(SIGNATURE_CLIENT_HEADER))
    expected = client.api_key_digest if client is not None and client.api_key_digest else ""
    if not hmac.compare_digest(expected, digest) or client is None:
        raise AuthenticationRejected("acp_api_key_did_not_verify")
    if client.audience != audience:
        raise AuthenticationRejected("acp_api_key_audience_mismatch")
    # The digest, not the token: :func:`fingerprint` keeps an eight-character preview, and
    # eight characters of a live bearer token in an immutable chain is eight too many.
    return client, "protocol_api_key", fingerprint(digest)


def _readable_body(body: bytes) -> Mapping[str, Any] | SchemaRejected:
    """Parse a verified body, or return the refusal it earned.

    Returning the rejection rather than raising it is what lets :func:`admit` write the
    ``RECEIVED`` row first. A refusal has to be evidenced under the interaction it belongs
    to, and the interaction has to exist before the refusal is raised.

    A JSON array, a bare string and a number are all valid JSON and none of them is an ACP
    request, so they are refused by the same path as malformed bytes. An empty body parses
    to an empty mapping, which is what a ``GET`` sends.
    """
    if not body:
        return MappingProxyType({})
    try:
        value = json.loads(body)
    except UnicodeDecodeError, json.JSONDecodeError:
        return SchemaRejected(
            "acp_body_is_not_a_json_object", length=len(body), digest=sha256_b64url(body)
        )
    except RecursionError:
        # Belt and braces. This interpreter's scanner survives the deepest nesting that
        # fits inside :data:`MAX_BODY_BYTES`, so nothing a caller can send reaches this
        # line today -- but whether the parser recurses is an implementation detail that
        # has changed between CPython releases, and an external party must not be able to
        # turn a well-formed refusal into a crash by sending punctuation. The depth
        # ceiling below is the check that actually does the work; see :func:`_unrecordable`.
        return SchemaRejected("acp_body_nested_too_deeply", length=len(body))
    if not isinstance(value, dict):
        return SchemaRejected(
            "acp_body_is_not_a_json_object", length=len(body), digest=sha256_b64url(body)
        )
    defect = _unrecordable(value)
    return defect if defect is not None else value


def _unrecordable(value: Any, *, path: str = "$", depth: int = 0) -> SchemaRejected | None:
    """Refuse a body this platform could parse but could not evidence.

    Specification 13.3 requires the request be recorded verbatim, and
    :func:`transaction_kernel.audit.append` refuses to canonicalize a float, a datetime,
    raw bytes or a non-string key, because a value with no stable JSON form could never be
    re-verified. That constraint propagates outward: a request that cannot be written into
    the chain cannot be honoured, since honouring it would mean acting without the evidence
    the rule demands.

    So the check happens at the door and produces a structured refusal naming the path that
    offended. Discovering it inside ``audit.append`` instead turns a caller's ordinary
    mistake into this platform's 5xx, and lets an external party crash a request by writing
    ``39500.0`` where an integer belongs -- which is exactly what happened the first time
    this adapter was pointed at a real audit chain.

    That the same rule keeps floats away from amounts is not a coincidence. Both come from
    one refusal to let an inexact number near money, stated once in the kernel and inherited
    here rather than restated.
    """
    if depth > MAX_BODY_DEPTH:
        return SchemaRejected("acp_body_nested_too_deeply", at=path, maximum=MAX_BODY_DEPTH)
    if value is None or isinstance(value, bool | int | str):
        return None
    if isinstance(value, float):
        return SchemaRejected("acp_body_carries_a_non_integer_number", at=path)
    if isinstance(value, list):
        for index, item in enumerate(value):
            defect = _unrecordable(item, path=f"{path}[{index}]", depth=depth + 1)
            if defect is not None:
                return defect
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            defect = _unrecordable(item, path=f"{path}.{key}", depth=depth + 1)
            if defect is not None:
                return defect
        return None
    return SchemaRejected(  # pragma: no cover - json.loads produces nothing else
        "acp_body_value_type_unsupported", at=path, presented_type=type(value).__name__
    )


def _parse_timestamp(raw: str | None) -> datetime:
    """RFC 3339 into an aware datetime, refusing anything else as a replay-guard failure.

    ``ReplayRejected`` rather than a schema refusal because the timestamp exists for exactly
    one purpose -- the freshness window -- and a timestamp the window cannot evaluate must
    be treated as a request that failed the window, not as a cosmetic formatting problem.
    """
    if not raw:
        raise ReplayRejected("acp_timestamp_absent")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise ReplayRejected("acp_timestamp_unparsable") from None
    if parsed.tzinfo is None:
        raise ReplayRejected("acp_timestamp_is_naive")
    return parsed
