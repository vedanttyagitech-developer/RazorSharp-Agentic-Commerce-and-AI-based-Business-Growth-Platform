"""The ACP surface, specification 16, exercised by an external AI buyer that lies on demand.

Almost every assertion here is a negative one, because the positives are cheap: a correctly
signed request being admitted proves that the happy path is wired up, and nothing more. What
this file has to establish is that each of specification 16.3's checks is load-bearing --
that removing any one of them would let something through -- and the only honest way to do
that is to send a request which is wrong in exactly one respect and watch the right check
refuse it.

That is what :class:`~commerce_protocols.acp.simulator.Misbehaviour` is for, and it is why
several of its members sign *honestly* over the offending request. An oversized body carries
a valid signature, so a passing size test proves the transport limit fired rather than the
HMAC. An unpinned API version is announced and signed, so the version test proves the pinned
matrix fired. A test that cannot distinguish which check refused it is a test that would
still pass with the check it names deleted.

Three assertions are worth reading twice:

* a replayed nonce is refused **and not answered with a stored response**. The nonce store
  rides on the kernel's idempotency records, and idempotency's whole purpose is to answer a
  repeat with what happened last time. For a nonce that behaviour is a vulnerability -- it
  would hand an attacker the original request's result -- so ``core.replay`` collapses every
  prior sighting into a refusal, and the test asserts both the ``POLICY_EXCEPTION`` code and
  the absence of any response payload;
* a refusal never echoes the credential it refused, in the exception or in the immutable
  evidence chain. A verifier that repeats back what it rejected is a verifier an attacker
  can interrogate, and an audit row is forever;
* an ACP completion is not consent. The external platform collects payment credentials from
  its user and ACP's ``complete`` carries them; this platform refuses the completion anyway
  unless it holds its own recorded approval, against the exact version and hash, taken on a
  surface the external caller cannot reach.

Database-backed classes are marked ``db``: freshness is judged against PostgreSQL's clock
and the nonce store is a real unique index, and neither means anything against a fake. The
pure classes -- signing string, routing, claim boundary, rate limiter, simulator determinism
-- need nothing and are marked accordingly, which is to say not at all.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

import pytest
from commerce_domain import Money, RecoveryCode, sha256_b64url, uuid7
from commerce_protocols.acp import (
    AcpBuyerSimulator,
    AcpClient,
    AcpRequest,
    AcpSession,
    AcpSessionStatus,
    AdmittedRequest,
    ClientRegistry,
    Credential,
    Misbehaviour,
    OverclaimError,
    PayloadRejected,
    RateLimit,
    RateLimited,
    TokenBucketLimiter,
    admit,
    approval_handoff,
    assert_claim_permitted,
    describe_surface,
    idempotency_key_for,
    map_request,
    parse_amount,
    route,
    sign,
    signing_string,
)
from commerce_protocols.acp.auth import MAX_BODY_BYTES, MAX_IDEMPOTENCY_KEY_LENGTH
from commerce_protocols.acp.claims import CLAIM_FOR_BOUNDARY, FORBIDDEN_CLAIM, PERMITTED_CLAIM
from commerce_protocols.acp.sessions import BASE_PATH, MUTATION_OPERATION, AcpOperation
from commerce_protocols.core import (
    CONSENT_CAPABILITIES,
    PINS,
    AuthenticationRejected,
    ClaimBoundary,
    IntentKind,
    MandateRejected,
    Protocol,
    ProtocolPin,
    ProtocolRejection,
    ReplayRejected,
    SchemaRejected,
    SignatureRejected,
    StateRejected,
    VersionRejected,
    database_now,
    principal_for,
)
from sqlalchemy import text
from sqlalchemy.orm import Session
from transaction_kernel.idempotency import (
    IdempotencyKeyReuseError,
    IdempotentReplayError,
    idempotent,
)

#: This deployment's audience. Never read from a request; see ``acp.auth``.
AUDIENCE = "https://acp.demo.invalid/v1"

#: The simulator's shared secret. A constant so a recorded fixture stays reproducible.
SIGNING_SECRET = b"acp-simulator-shared-signing-secret"

API_KEY = "acpk_live_0123456789abcdef0123456789abcdef"

TOTAL = Money(39500, "INR")

READY_BODY: dict[str, Any] = {
    "items": [{"sku": "MILK-DAIRY-001", "quantity": 2}],
    "buyer": {"reference": "buyer-7"},
    "fulfillment": {"pincode": "560001"},
}


# ------------------------------------------------------------------------- fixtures


@pytest.fixture
def acp_simulator() -> AcpBuyerSimulator:
    """One external AI buyer, seeded so two runs produce the same bytes."""
    return AcpBuyerSimulator(
        client_id="acp-client-alpha",
        signing_secret=SIGNING_SECRET,
        audience=AUDIENCE,
        api_key=API_KEY,
        seed="alpha",
    )


@pytest.fixture
def acp_registry(
    acp_simulator: AcpBuyerSimulator, cp_tenant: tuple[uuid.UUID, uuid.UUID]
) -> ClientRegistry:
    tenant_id, merchant_id = cp_tenant
    return ClientRegistry.of(
        acp_simulator.registration(
            tenant_id=tenant_id, merchant_id=merchant_id, buyer_ref="buyer-7"
        )
    )


@pytest.fixture
def acp_limiter() -> TokenBucketLimiter:
    return TokenBucketLimiter()


@pytest.fixture
def acp_now(cp_session: Session) -> datetime:
    """The database clock, which is the only clock the freshness window trusts."""
    return database_now(cp_session)


def gate(
    session: Session,
    request: AcpRequest,
    *,
    registry: ClientRegistry,
    limiter: TokenBucketLimiter,
    audience: str = AUDIENCE,
) -> AdmittedRequest:
    """Route, then admit. The router owns the answer to "does this endpoint mutate"."""
    resolved = route(request.method, request.path)
    return admit(
        session,
        request,
        registry=registry,
        audience=audience,
        limiter=limiter,
        requires_idempotency_key=resolved.requires_idempotency_key,
    )


def signed_raw(
    simulator: AcpBuyerSimulator,
    now: datetime,
    *,
    method: str,
    path: str,
    raw: bytes,
) -> AcpRequest:
    """One request over exactly these bytes, signed honestly.

    Every :class:`Misbehaviour` is a *named* way for a client to be wrong; the two cases
    below are about the shape of arbitrary bytes rather than about a client misbehaving, so
    they are assembled here instead of widening the simulator's vocabulary to describe them.
    """
    stamp = now.isoformat()
    nonce = f"raw-{sha256_b64url(raw)[:16]}"
    key = "raw-idem-1"
    material = signing_string(
        method=method,
        path=path,
        query="",
        audience=AUDIENCE,
        api_version=PINS[Protocol.ACP].version,
        timestamp=stamp,
        request_id=nonce,
        idempotency_key=key,
        content_type="application/json",
        body=raw,
    )
    return AcpRequest.build(
        method=method,
        path=path,
        body=raw,
        headers={
            "API-Version": PINS[Protocol.ACP].version,
            "Content-Type": "application/json",
            "Timestamp": stamp,
            "Request-Id": nonce,
            "Idempotency-Key": key,
            "Signature-Client": simulator.client_id,
            "Signature-Algorithm": "HMAC-SHA256",
            "Signature": sign(simulator.signing_secret, material),
        },
    )


def audit_payloads(session: Session, tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            "SELECT payload FROM audit_events WHERE tenant_id = :t "
            "AND aggregate_type = 'protocol_interaction' ORDER BY occurred_at, seq"
        ),
        {"t": tenant_id},
    ).all()
    return [row.payload for row in rows]


# ------------------------------------------------------------------- signing string


class TestTheCanonicalSigningString:
    """What the HMAC covers, and why the encoding of it has to be injective."""

    def _material(self, **overrides: Any) -> bytes:
        fields: dict[str, Any] = {
            "method": "POST",
            "path": BASE_PATH,
            "query": "",
            "audience": AUDIENCE,
            "api_version": PINS[Protocol.ACP].version,
            "timestamp": "2026-04-17T10:00:00+00:00",
            "request_id": "nonce-1",
            "idempotency_key": "idem-1",
            "content_type": "application/json",
            "body": b'{"a":1}',
        }
        fields.update(overrides)
        return signing_string(**fields)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("method", "GET"),
            ("path", f"{BASE_PATH}/other"),
            ("query", "limit=1"),
            ("audience", "https://elsewhere.invalid/v1"),
            ("api_version", "2019-01-01"),
            ("timestamp", "2026-04-17T10:00:01+00:00"),
            ("request_id", "nonce-2"),
            ("idempotency_key", "idem-2"),
            ("content_type", "text/plain"),
            ("body", b'{"a":2}'),
        ],
    )
    def test_changing_any_covered_field_changes_the_signature(self, field: str, value: Any) -> None:
        """Every field named in the module docstring is actually covered, one by one.

        Parametrised rather than written as one assertion over a dict, because a single
        combined test would pass if nine of the ten were covered.
        """
        baseline = sign(SIGNING_SECRET, self._material())
        altered = sign(SIGNING_SECRET, self._material(**{field: value}))
        assert baseline != altered, f"{field} is not covered by the signature"

    def test_a_field_boundary_cannot_be_moved_by_a_newline(self) -> None:
        """The reason every field is length-prefixed rather than newline-joined.

        Without the prefix, a path ending in a newline followed by an empty query produces
        the same joined bytes as a shorter path followed by a query -- and a signature over
        one is a valid signature over the other. The forgery below is what an attacker who
        controls a path segment would reach for first.
        """
        honest = self._material(path=f"{BASE_PATH}/abc", query="limit=1")
        forged = self._material(path=f"{BASE_PATH}/abc\nquery=5:limit=1", query="")
        assert honest != forged

    def test_the_domain_prefix_separates_this_surface_from_every_other(self) -> None:
        assert self._material().startswith(b"domain=15:ACP-HMAC-SHA256")

    def test_an_absent_idempotency_key_is_signed_as_absent_rather_than_omitted(self) -> None:
        """Stripping the header from a captured mutation must invalidate the signature."""
        with_key = self._material(idempotency_key="idem-1")
        without = self._material(idempotency_key="")
        assert with_key != without


# ------------------------------------------------------------------ claim boundary


class TestTheExternalApprovalBoundary:
    """Specification 16.2, as a gate rather than as a promise in a README."""

    def test_the_sentence_specification_16_2_permits_is_permitted(self) -> None:
        assert assert_claim_permitted(PERMITTED_CLAIM) == PERMITTED_CLAIM

    def test_the_sentence_specification_16_2_forbids_is_refused_verbatim(self) -> None:
        """It trips two rules at once; the refusal names whichever fired, and both are lines."""
        with pytest.raises(OverclaimError) as caught:
            assert_claim_permitted(FORBIDDEN_CLAIM)
        assert caught.value.rule in {"external_distribution", "external_product_surface"}
        assert FORBIDDEN_CLAIM in str(caught.value)

    @pytest.mark.parametrize(
        "overclaim",
        [
            "We are live in ChatGPT.",
            "The store is live on Chat-GPT today.",
            "Our merchant is live and taking agentic orders.",
            "This integration was approved by OpenAI.",
            "OpenAI certification complete.",
            "ACP certified.",
            "Available in ChatGPT for all shoppers.",
        ],
    )
    def test_a_paraphrase_of_the_forbidden_claim_is_refused_too(self, overclaim: str) -> None:
        """Punctuation and spacing are normalised away, so a rewrite does not escape.

        The gate would be theatre if it only caught the specification's exact wording; the
        sentence somebody actually writes is never the sentence in the specification.
        """
        with pytest.raises(OverclaimError):
            assert_claim_permitted(overclaim)

    def test_the_surface_reports_the_pinned_boundary_and_its_disclaimer(self) -> None:
        status = describe_surface()
        assert status.boundary is ClaimBoundary.COMPATIBLE_INTERFACE
        assert status.version == "2026-04-17"
        assert status.claim == CLAIM_FOR_BOUNDARY[ClaimBoundary.COMPATIBLE_INTERFACE]
        assert "OpenAI" in status.disclaimer
        assert status.as_payload()["claim_boundary"] == "COMPATIBLE_INTERFACE"

    def test_the_reported_claim_does_not_assert_a_schema_validation_that_never_happened(
        self,
    ) -> None:
        """The sentence and the boundary label have to agree, and the sentence is what sticks.

        Specification 16.2's permitted sentence says "validated locally against the pinned
        public schema", which is the definition of ``LOCAL_CONFORMANCE``. ACP is pinned at
        ``COMPATIBLE_INTERFACE`` and there is no ACP schema artifact in this repository to
        validate anything against, so reporting that sentence would be a status object whose
        two halves contradict each other -- and nobody quotes the label.
        """
        status = describe_surface()
        assert status.claim != PERMITTED_CLAIM
        assert "validated locally against the pinned public schema" not in status.claim
        assert "ACP-compatible interface" in status.claim

    def test_every_boundary_has_a_sentence_and_none_of_them_overclaims(self) -> None:
        """The table is exhaustive, so a pin change cannot land on a missing key at runtime."""
        assert set(CLAIM_FOR_BOUNDARY) == set(ClaimBoundary)
        for boundary, sentence in CLAIM_FOR_BOUNDARY.items():
            assert assert_claim_permitted(sentence) == sentence
            assert (
                describe_surface(
                    ProtocolPin(
                        protocol=Protocol.ACP,
                        version="2026-04-17",
                        source_ref="test",
                        boundary=boundary,
                        disclaimer="d",
                    )
                ).claim
                == sentence
            )

    def test_the_disclaimer_quotes_the_sentence_it_refutes(self) -> None:
        """Which is why a refutation is data and not run through the claim gate.

        The pin's disclaimer necessarily contains the forbidden phrase in order to deny it.
        Passing it through :func:`assert_claim_permitted` would mean the only way to state
        the boundary is to not state it, so the gate guards claims and the disclaimer is
        published beside them.
        """
        disclaimer = describe_surface().disclaimer
        assert "Instant Checkout" in disclaimer
        with pytest.raises(OverclaimError):
            assert_claim_permitted(disclaimer)

    def test_the_boundary_is_weaker_than_the_protocols_with_recorded_fixtures(self) -> None:
        """ACP is ``COMPATIBLE_INTERFACE``; UCP and AP2 are ``LOCAL_CONFORMANCE``.

        The difference is the whole reason this adapter is allowed its completion echo, and
        a summary that flattened the four protocols into one column would lose it.
        """
        assert PINS[Protocol.ACP].boundary is ClaimBoundary.COMPATIBLE_INTERFACE
        assert PINS[Protocol.UCP].boundary is ClaimBoundary.LOCAL_CONFORMANCE


# ------------------------------------------------------------------------- routing


class TestRouting:
    @pytest.mark.parametrize(
        ("method", "path", "operation", "needs_key"),
        [
            ("POST", BASE_PATH, AcpOperation.CREATE_SESSION, True),
            ("POST", f"{BASE_PATH}/cs_1", AcpOperation.UPDATE_SESSION, True),
            ("GET", f"{BASE_PATH}/cs_1", AcpOperation.RETRIEVE_SESSION, False),
            ("POST", f"{BASE_PATH}/cs_1/complete", AcpOperation.COMPLETE_SESSION, True),
            ("POST", f"{BASE_PATH}/cs_1/cancel", AcpOperation.CANCEL_SESSION, True),
        ],
    )
    def test_each_endpoint_resolves_to_one_operation(
        self, method: str, path: str, operation: AcpOperation, needs_key: bool
    ) -> None:
        resolved = route(method, path)
        assert resolved.operation is operation
        assert resolved.requires_idempotency_key is needs_key

    @pytest.mark.parametrize(
        "path",
        [
            f"{BASE_PATH}/",
            f"{BASE_PATH}//cs_1",
            f"{BASE_PATH}/cs_1/../cs_2",
            "/acp//checkout_sessions",
        ],
    )
    def test_a_non_canonical_path_is_refused_rather_than_normalised(self, path: str) -> None:
        """Normalising here would let the signed bytes and the acted-on resource differ."""
        with pytest.raises(SchemaRejected) as caught:
            route("POST", path)
        assert caught.value.reason in {"acp_path_not_canonical", "acp_session_id_malformed"}

    def test_a_session_id_carrying_path_syntax_is_refused(self) -> None:
        with pytest.raises(SchemaRejected):
            route("POST", f"{BASE_PATH}/cs%2F1/complete")

    def test_an_unknown_endpoint_is_refused(self) -> None:
        with pytest.raises(SchemaRejected) as caught:
            route("POST", "/acp/payments")
        assert caught.value.reason == "acp_unknown_endpoint"

    def test_a_completion_over_the_wrong_method_is_refused(self) -> None:
        with pytest.raises(SchemaRejected) as caught:
            route("GET", f"{BASE_PATH}/cs_1/complete")
        assert caught.value.reason == "acp_method_not_allowed"

    def test_only_reads_are_exempt_from_the_idempotency_key(self) -> None:
        """The one rule of 16.3 that a router can get wrong in the safe-looking direction."""
        assert not route("GET", f"{BASE_PATH}/cs_1").requires_idempotency_key
        for path in (BASE_PATH, f"{BASE_PATH}/cs_1", f"{BASE_PATH}/cs_1/complete"):
            assert route("POST", path).requires_idempotency_key


# --------------------------------------------------------------------- determinism


class TestTheSimulatorIsDeterministic:
    def test_two_runs_from_the_same_seed_and_clock_produce_identical_bytes(self) -> None:
        """Otherwise a recorded fixture (13.3) would differ from the request that made it."""
        now = datetime.fromisoformat("2026-04-17T10:00:00+05:30")

        def once() -> tuple[str, bytes, Mapping[str, str]]:
            sim = AcpBuyerSimulator(
                client_id="acp-client-alpha",
                signing_secret=SIGNING_SECRET,
                audience=AUDIENCE,
                seed="alpha",
            )
            request = sim.create_session(now, body=READY_BODY)
            return request.path, request.body, request.headers

        assert once() == once()

    def test_a_replayed_nonce_reuses_the_previous_request_id(self) -> None:
        now = datetime.fromisoformat("2026-04-17T10:00:00+05:30")
        sim = AcpBuyerSimulator(
            client_id="c", signing_secret=SIGNING_SECRET, audience=AUDIENCE, seed="alpha"
        )
        first = sim.create_session(now, body=READY_BODY)
        replay = sim.create_session(now, body=READY_BODY, misbehave=Misbehaviour.REPLAYED_NONCE)
        assert replay.header("Request-Id") == first.header("Request-Id")

    def test_an_oversized_body_is_signed_honestly_so_the_size_check_is_what_refuses(
        self,
    ) -> None:
        """The property that makes the size test mean something. See the module docstring."""
        now = datetime.fromisoformat("2026-04-17T10:00:00+05:30")
        sim = AcpBuyerSimulator(
            client_id="c", signing_secret=SIGNING_SECRET, audience=AUDIENCE, seed="alpha"
        )
        request = sim.create_session(now, misbehave=Misbehaviour.OVERSIZED_BODY)
        material = signing_string(
            method=request.method,
            path=request.path,
            query="",
            audience=AUDIENCE,
            api_version=PINS[Protocol.ACP].version,
            timestamp=request.header("Timestamp") or "",
            request_id=request.header("Request-Id") or "",
            idempotency_key=request.header("Idempotency-Key") or "",
            content_type=request.header("Content-Type") or "",
            body=request.body,
        )
        assert len(request.body) > MAX_BODY_BYTES
        assert sign(SIGNING_SECRET, material) == request.header("Signature")


# ---------------------------------------------------------------------- rate limits


class TestTheTokenBucket:
    def _client(self, capacity: int, refill: int) -> AcpClient:
        return AcpClient(
            client_id="c",
            tenant_id=uuid7(),
            merchant_id=uuid7(),
            audience=AUDIENCE,
            signing_secret=SIGNING_SECRET,
            client_rate=RateLimit(capacity=capacity, refill_per_second=refill),
        )

    def test_the_n_plus_first_request_in_a_burst_is_refused(self) -> None:
        limiter = TokenBucketLimiter()
        client = self._client(capacity=3, refill=1)
        now = datetime.fromisoformat("2026-04-17T10:00:00+00:00")
        for _ in range(3):
            limiter.take(now, client=client)
        with pytest.raises(RateLimited) as caught:
            limiter.take(now, client=client)
        assert caught.value.code is RecoveryCode.POLICY_EXCEPTION
        assert caught.value.details["scope"] == "client"

    def test_tokens_return_at_the_configured_rate_and_no_faster(self) -> None:
        limiter = TokenBucketLimiter()
        client = self._client(capacity=2, refill=2)
        now = datetime.fromisoformat("2026-04-17T10:00:00+00:00")
        limiter.take(now, client=client)
        limiter.take(now, client=client)
        with pytest.raises(RateLimited):
            limiter.take(now, client=client)
        # Half a second at two per second is exactly one token.
        limiter.take(now + timedelta(milliseconds=500), client=client)
        with pytest.raises(RateLimited):
            limiter.take(now + timedelta(milliseconds=500), client=client)

    def test_polling_faster_than_the_refill_still_accrues(self) -> None:
        """The reason the bucket advances its own clock by whole tokens only.

        Setting ``filled_at`` to ``now`` on every call would discard the remainder each
        time, so a client polling every hundred milliseconds against a one-per-second
        refill would never gain a token at all -- a starvation bug that only appears under
        the traffic pattern an eager AI buyer actually produces.
        """
        limiter = TokenBucketLimiter()
        client = self._client(capacity=1, refill=1)
        now = datetime.fromisoformat("2026-04-17T10:00:00+00:00")
        limiter.take(now, client=client)
        for tick in range(1, 10):
            with pytest.raises(RateLimited):
                limiter.take(now + timedelta(milliseconds=100 * tick), client=client)
        limiter.take(now + timedelta(milliseconds=1000), client=client)

    def test_a_tenant_ceiling_refuses_a_client_that_still_has_room(self) -> None:
        limiter = TokenBucketLimiter(tenant_rate=RateLimit(capacity=2, refill_per_second=1))
        client = self._client(capacity=50, refill=1)
        now = datetime.fromisoformat("2026-04-17T10:00:00+00:00")
        limiter.take(now, client=client)
        limiter.take(now, client=client)
        with pytest.raises(RateLimited) as caught:
            limiter.take(now, client=client)
        assert caught.value.details["scope"] == "tenant"

    def test_a_request_refused_by_one_bucket_does_not_spend_the_other(self) -> None:
        """Otherwise a client refused for its neighbour's traffic loses its own budget too."""
        limiter = TokenBucketLimiter(tenant_rate=RateLimit(capacity=1, refill_per_second=1))
        client = self._client(capacity=5, refill=1)
        now = datetime.fromisoformat("2026-04-17T10:00:00+00:00")
        limiter.take(now, client=client)
        for _ in range(3):
            with pytest.raises(RateLimited):
                limiter.take(now, client=client)
        # Four refusals later the client bucket must still hold its remaining four tokens,
        # which a full second of tenant refill is enough to prove.
        for tick in range(1, 5):
            limiter.take(now + timedelta(seconds=tick), client=client)


# ------------------------------------------------------------------------ admission


class TestAdmission:
    """Specification 16.3 end to end, against PostgreSQL's clock and its unique index."""

    pytestmark = pytest.mark.db

    def test_a_correctly_signed_request_is_admitted(
        self,
        cp_session: Session,
        cp_tenant: tuple[uuid.UUID, uuid.UUID],
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        tenant_id, merchant_id = cp_tenant
        admitted = gate(
            cp_session,
            acp_simulator.create_session(acp_now, body=READY_BODY),
            registry=acp_registry,
            limiter=acp_limiter,
        )
        assert admitted.caller.protocol is Protocol.ACP
        assert admitted.caller.tenant_id == tenant_id
        assert admitted.caller.merchant_id == merchant_id
        assert admitted.caller.authenticated_by == "http_message_signature"
        assert admitted.idempotency_key == "alpha-idem-000000"
        assert admitted.body["buyer"] == {"reference": "buyer-7"}

    def test_the_admitted_principal_can_never_consent(
        self,
        cp_session: Session,
        cp_tenant: tuple[uuid.UUID, uuid.UUID],
        acp_simulator: AcpBuyerSimulator,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        """A misconfigured registry entry is narrowed, not obeyed.

        This is the one failure the whole architecture exists to make impossible, so the
        client below is deliberately configured with ``checkout.approve`` -- the mistake a
        human would make once -- and the capability is dropped at every use rather than
        refused loudly and then forgotten about.
        """
        tenant_id, merchant_id = cp_tenant
        misconfigured = acp_simulator.registration(tenant_id=tenant_id, merchant_id=merchant_id)
        registry = ClientRegistry.of(
            AcpClient(
                client_id=misconfigured.client_id,
                tenant_id=tenant_id,
                merchant_id=merchant_id,
                audience=AUDIENCE,
                signing_secret=SIGNING_SECRET,
                granted=frozenset({"catalogue.read", "checkout.approve", "refund.request"}),
            )
        )
        admitted = gate(
            cp_session,
            acp_simulator.create_session(acp_now, body=READY_BODY),
            registry=registry,
            limiter=acp_limiter,
        )
        principal = principal_for(admitted.caller)
        assert principal.capabilities & CONSENT_CAPABILITIES == frozenset()
        assert principal.capabilities == frozenset({"catalogue.read"})

    def test_the_gate_asserts_the_consent_ceiling_rather_than_trusting_the_intersection(
        self,
        cp_session: Session,
        cp_tenant: tuple[uuid.UUID, uuid.UUID],
        acp_simulator: AcpBuyerSimulator,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``core.identity`` asks every adapter to re-assert this where it acts, and this is it.

        The intersection in ``principal_for`` makes the assertion unreachable in ordinary
        operation, which is exactly why an adapter that skips it looks correct forever and
        then does not. The intersection is stubbed out below so the belt-and-braces check is
        the only thing standing between a misconfigured registry entry and a principal that
        can approve a purchase on a human's behalf; a gate that never called it would admit
        this request.
        """
        import commerce_protocols.acp.auth as auth_module

        tenant_id, merchant_id = cp_tenant
        registry = ClientRegistry.of(
            acp_simulator.registration(tenant_id=tenant_id, merchant_id=merchant_id)
        )

        def widened(caller: Any) -> Any:
            narrowed = principal_for(caller)
            return narrowed.__class__(
                **{
                    **{f: getattr(narrowed, f) for f in narrowed.__dataclass_fields__},
                    "capabilities": narrowed.capabilities | {"checkout.approve"},
                }
            )

        monkeypatch.setattr(auth_module, "principal_for", widened)
        with pytest.raises(AuthenticationRejected) as caught:
            gate(
                cp_session,
                acp_simulator.create_session(acp_now, body=READY_BODY),
                registry=registry,
                limiter=acp_limiter,
            )
        assert caught.value.reason == "protocol_principal_holds_consent_capability"

    def test_a_tampered_body_does_not_verify(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        """One field raised to 9,999.99 after signing, which is what interception looks like."""
        request = acp_simulator.create_session(
            acp_now, body=READY_BODY, misbehave=Misbehaviour.TAMPERED_BODY
        )
        assert b"999999" in request.body
        with pytest.raises(SignatureRejected) as caught:
            gate(cp_session, request, registry=acp_registry, limiter=acp_limiter)
        assert caught.value.reason == "acp_signature_did_not_verify"
        assert caught.value.code is RecoveryCode.AUTHORITY_INSUFFICIENT

    def test_a_tampered_signature_does_not_verify(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        request = acp_simulator.create_session(
            acp_now, body=READY_BODY, misbehave=Misbehaviour.TAMPERED_SIGNATURE
        )
        with pytest.raises(SignatureRejected):
            gate(cp_session, request, registry=acp_registry, limiter=acp_limiter)

    def test_a_signature_from_a_key_this_platform_never_issued_is_refused(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        request = acp_simulator.create_session(
            acp_now, body=READY_BODY, misbehave=Misbehaviour.WRONG_SIGNING_KEY
        )
        with pytest.raises(SignatureRejected):
            gate(cp_session, request, registry=acp_registry, limiter=acp_limiter)

    def test_a_signature_minted_for_another_audience_does_not_verify_here(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        request = acp_simulator.create_session(
            acp_now, body=READY_BODY, misbehave=Misbehaviour.WRONG_AUDIENCE
        )
        with pytest.raises(SignatureRejected):
            gate(cp_session, request, registry=acp_registry, limiter=acp_limiter)

    def test_the_same_bytes_verify_for_one_audience_and_not_another(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        """Audience binding, stated as the property rather than as a misbehaviour.

        The same request object is presented to two verifiers differing only in their
        configured audience. If the audience were read from the request instead, both would
        admit it -- which is the bug this asserts the absence of.
        """
        request = acp_simulator.create_session(acp_now, body=READY_BODY)
        with pytest.raises(SignatureRejected):
            admit(
                cp_session,
                request,
                registry=acp_registry,
                audience="https://acp.other-deployment.invalid/v1",
                limiter=acp_limiter,
                requires_idempotency_key=True,
            )
        assert gate(cp_session, request, registry=acp_registry, limiter=acp_limiter)

    def test_a_client_registered_for_another_audience_cannot_sign_its_way_into_this_one(
        self,
        cp_session: Session,
        cp_tenant: tuple[uuid.UUID, uuid.UUID],
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        """The signing string binds the audience to the signer's intent, not to its grant.

        ``test_the_same_bytes_verify_for_one_audience_and_not_another`` proves a *captured*
        signature does not travel between deployments, and it is easy to read that as the
        whole of audience binding. It is not. A client holding its own secret can mint a
        fresh signature over any audience string at all, including one it was never issued
        credentials for -- so the sandbox-only registration below signs honestly for the
        live audience, and only the registration check can refuse it.

        Without that check this test admits a sandbox client to production, which is the
        exact failure the audience field is supposed to make impossible.
        """
        tenant_id, merchant_id = cp_tenant
        live = "https://acp.live.invalid/v1"
        sandbox_registration = AcpBuyerSimulator(
            client_id="acp-sandbox-only",
            signing_secret=SIGNING_SECRET,
            audience="https://acp.sandbox.invalid/v1",
            seed="sandbox",
        ).registration(tenant_id=tenant_id, merchant_id=merchant_id)
        signing_for_live = AcpBuyerSimulator(
            client_id="acp-sandbox-only",
            signing_secret=SIGNING_SECRET,
            audience=live,
            seed="sandbox",
        )
        with pytest.raises(SignatureRejected) as caught:
            admit(
                cp_session,
                signing_for_live.create_session(acp_now, body=READY_BODY),
                registry=ClientRegistry.of(sandbox_registration),
                audience=live,
                limiter=acp_limiter,
                requires_idempotency_key=True,
            )
        assert caught.value.reason == "acp_signature_audience_not_registered"

    def test_the_audience_check_is_reached_only_after_the_secret_is_proved(
        self,
        cp_session: Session,
        cp_tenant: tuple[uuid.UUID, uuid.UUID],
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        """Otherwise the registration check would be a free oracle over the registry.

        Somebody who does not hold the secret must learn nothing about which audiences a
        client id is registered for, so a wrong signature and a wrong registered audience
        must not be distinguishable -- the first must refuse before the second is consulted.
        """
        tenant_id, merchant_id = cp_tenant
        live = "https://acp.live.invalid/v1"
        sandbox_registration = AcpBuyerSimulator(
            client_id="acp-sandbox-only",
            signing_secret=SIGNING_SECRET,
            audience="https://acp.sandbox.invalid/v1",
            seed="sandbox",
        ).registration(tenant_id=tenant_id, merchant_id=merchant_id)
        forger = AcpBuyerSimulator(
            client_id="acp-sandbox-only",
            signing_secret=b"not-the-secret-this-client-was-issued",
            audience=live,
            seed="sandbox",
        )
        with pytest.raises(SignatureRejected) as caught:
            admit(
                cp_session,
                forger.create_session(acp_now, body=READY_BODY),
                registry=ClientRegistry.of(sandbox_registration),
                audience=live,
                limiter=acp_limiter,
                requires_idempotency_key=True,
            )
        assert caught.value.reason == "acp_signature_did_not_verify"

    def test_an_unknown_client_is_refused_exactly_like_a_bad_signature(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        """A registry that can be enumerated by refusal text is a registry being harvested."""
        empty = ClientRegistry.of()
        with pytest.raises(SignatureRejected) as unknown:
            gate(
                cp_session,
                acp_simulator.create_session(acp_now, body=READY_BODY),
                registry=empty,
                limiter=acp_limiter,
            )
        assert unknown.value.reason == "acp_signature_did_not_verify"
        assert unknown.value.details == {}

    def test_a_stale_timestamp_is_refused(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        with pytest.raises(ReplayRejected) as caught:
            gate(
                cp_session,
                acp_simulator.create_session(
                    acp_now, body=READY_BODY, misbehave=Misbehaviour.STALE_TIMESTAMP
                ),
                registry=acp_registry,
                limiter=acp_limiter,
            )
        assert caught.value.reason == "request_outside_freshness_window"
        assert caught.value.details["max_age_seconds"] == 300

    def test_a_future_timestamp_is_refused(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        """A request dated an hour ahead would outlive its nonce inside its own window."""
        with pytest.raises(ReplayRejected) as caught:
            gate(
                cp_session,
                acp_simulator.create_session(
                    acp_now, body=READY_BODY, misbehave=Misbehaviour.FUTURE_TIMESTAMP
                ),
                registry=acp_registry,
                limiter=acp_limiter,
            )
        assert caught.value.reason == "request_timestamp_in_the_future"

    def test_a_timestamp_without_a_zone_is_refused_rather_than_assumed_utc(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        naive = acp_now.replace(tzinfo=None)
        request = acp_simulator.create_session(naive, body=READY_BODY)
        with pytest.raises(ReplayRejected) as caught:
            gate(cp_session, request, registry=acp_registry, limiter=acp_limiter)
        assert caught.value.reason == "acp_timestamp_is_naive"

    def test_a_replayed_nonce_is_refused_not_answered(
        self,
        cp_session: Session,
        cp_tenant: tuple[uuid.UUID, uuid.UUID],
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        """The nonce store rides on idempotency records and must not behave like one.

        Idempotency answers a repeat with the stored response, which is right for a retry
        and catastrophic for a nonce: it would hand whoever captured the request the result
        of the original. So the code is ``POLICY_EXCEPTION`` rather than
        ``DUPLICATE_OPERATION``, no response travels back, and the refusal is recorded as a
        rejection rather than as a replayed success.
        """
        tenant_id, _ = cp_tenant
        first = acp_simulator.create_session(acp_now, body=READY_BODY)
        gate(cp_session, first, registry=acp_registry, limiter=acp_limiter)

        replay = acp_simulator.create_session(
            acp_now, body=READY_BODY, misbehave=Misbehaviour.REPLAYED_NONCE
        )
        assert replay.header("Request-Id") == first.header("Request-Id")
        with pytest.raises(ReplayRejected) as caught:
            gate(cp_session, replay, registry=acp_registry, limiter=acp_limiter)

        assert caught.value.reason == "nonce_already_presented"
        assert caught.value.code is RecoveryCode.POLICY_EXCEPTION
        assert caught.value.code is not RecoveryCode.DUPLICATE_OPERATION
        assert not hasattr(caught.value, "response"), (
            "a replayed nonce must never carry the original request's answer"
        )
        rejections = [p for p in audit_payloads(cp_session, tenant_id) if p["stage"] == "REJECTED"]
        assert [p["reason"] for p in rejections] == ["nonce_already_presented"]
        assert rejections[0]["failed_at"] == "VERIFIED"

    def test_a_replay_carrying_different_bytes_is_still_refused(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        """A reused nonce over changed content is somebody probing, not a retrying proxy."""
        gate(
            cp_session,
            acp_simulator.create_session(acp_now, body=READY_BODY),
            registry=acp_registry,
            limiter=acp_limiter,
        )
        different = acp_simulator.create_session(
            acp_now,
            body={"items": [{"sku": "BREAD-BAKERY-002", "quantity": 1}]},
            misbehave=Misbehaviour.REPLAYED_NONCE,
        )
        with pytest.raises(ReplayRejected):
            gate(cp_session, different, registry=acp_registry, limiter=acp_limiter)

    def test_a_fresh_nonce_after_a_replay_is_admitted(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        """The replay guard must refuse the request, not blacklist the client."""
        gate(
            cp_session,
            acp_simulator.create_session(acp_now, body=READY_BODY),
            registry=acp_registry,
            limiter=acp_limiter,
        )
        with pytest.raises(ReplayRejected):
            gate(
                cp_session,
                acp_simulator.create_session(
                    acp_now, body=READY_BODY, misbehave=Misbehaviour.REPLAYED_NONCE
                ),
                registry=acp_registry,
                limiter=acp_limiter,
            )
        assert gate(
            cp_session,
            acp_simulator.create_session(acp_now, body=READY_BODY),
            registry=acp_registry,
            limiter=acp_limiter,
        )

    def test_an_unpinned_api_version_is_refused(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        with pytest.raises(VersionRejected) as caught:
            gate(
                cp_session,
                acp_simulator.create_session(
                    acp_now, body=READY_BODY, misbehave=Misbehaviour.WRONG_API_VERSION
                ),
                registry=acp_registry,
                limiter=acp_limiter,
            )
        assert caught.value.details == {"announced": "2019-01-01", "supported": "2026-04-17"}

    def test_the_rejected_version_is_kept_in_evidence(
        self,
        cp_session: Session,
        cp_tenant: tuple[uuid.UUID, uuid.UUID],
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        """A client repeatedly announcing the wrong version is only visible if it is kept."""
        tenant_id, _ = cp_tenant
        with pytest.raises(VersionRejected):
            gate(
                cp_session,
                acp_simulator.create_session(
                    acp_now, body=READY_BODY, misbehave=Misbehaviour.WRONG_API_VERSION
                ),
                registry=acp_registry,
                limiter=acp_limiter,
            )
        received = [p for p in audit_payloads(cp_session, tenant_id) if p["stage"] == "RECEIVED"]
        assert received[0]["announced_version"] == "2019-01-01"
        assert received[0]["protocol_version"] == "2026-04-17"

    def test_an_oversized_body_is_refused(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        with pytest.raises(PayloadRejected) as caught:
            gate(
                cp_session,
                acp_simulator.create_session(acp_now, misbehave=Misbehaviour.OVERSIZED_BODY),
                registry=acp_registry,
                limiter=acp_limiter,
            )
        assert caught.value.reason == "acp_body_too_large"
        assert caught.value.details["maximum"] == MAX_BODY_BYTES

    def test_a_content_type_that_is_not_json_is_refused(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        with pytest.raises(PayloadRejected) as caught:
            gate(
                cp_session,
                acp_simulator.create_session(
                    acp_now, body=READY_BODY, misbehave=Misbehaviour.WRONG_CONTENT_TYPE
                ),
                registry=acp_registry,
                limiter=acp_limiter,
            )
        assert caught.value.reason == "acp_content_type_not_json"

    def test_json_that_is_not_an_object_is_refused_after_the_signature_verifies(
        self,
        cp_session: Session,
        cp_tenant: tuple[uuid.UUID, uuid.UUID],
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        """Bytes reach the parser only once the HMAC over them has verified (ADR 0003 D7)."""
        tenant_id, _ = cp_tenant
        with pytest.raises(SchemaRejected) as caught:
            gate(
                cp_session,
                acp_simulator.create_session(acp_now, misbehave=Misbehaviour.NON_OBJECT_BODY),
                registry=acp_registry,
                limiter=acp_limiter,
            )
        assert caught.value.reason == "acp_body_is_not_a_json_object"
        stages = [p["stage"] for p in audit_payloads(cp_session, tenant_id)]
        assert stages == ["RECEIVED", "REJECTED"], (
            "the request was authenticated first, so it must be evidenced before refusal"
        )

    def test_a_body_the_audit_chain_could_not_record_is_refused_by_name(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        """The refusal names the path, so a client can fix its serialiser in one attempt."""
        raw = b'{"items": [{"quantity": 1.5}]}'
        with pytest.raises(SchemaRejected) as caught:
            gate(
                cp_session,
                signed_raw(acp_simulator, acp_now, method="POST", path=BASE_PATH, raw=raw),
                registry=acp_registry,
                limiter=acp_limiter,
            )
        assert caught.value.reason == "acp_body_carries_a_non_integer_number"
        assert caught.value.details["at"] == "$.items[0].quantity"

    @pytest.mark.parametrize("levels", [40, 10_000])
    def test_a_body_nested_past_the_ceiling_is_refused_without_walking_it(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
        levels: int,
    ) -> None:
        """Six bytes per nesting level fits ten thousand levels inside the size ceiling.

        The second case is the adversarial one: it is well within every limit specification
        16.3 names, and a validator that recursed to the bottom before deciding would run
        out of stack proving that the body is unacceptable. The depth check is at the top of
        the walk rather than at the end of it, so refusing costs seventeen frames whatever
        the caller sent.
        """
        raw = b'{"a":' * levels + b"1" + b"}" * levels
        assert len(raw) < MAX_BODY_BYTES
        with pytest.raises(SchemaRejected) as caught:
            gate(
                cp_session,
                signed_raw(acp_simulator, acp_now, method="POST", path=BASE_PATH, raw=raw),
                registry=acp_registry,
                limiter=acp_limiter,
            )
        assert caught.value.reason == "acp_body_nested_too_deeply"

    def test_a_mutation_without_an_idempotency_key_is_refused(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        with pytest.raises(SchemaRejected) as caught:
            gate(
                cp_session,
                acp_simulator.create_session(
                    acp_now, body=READY_BODY, misbehave=Misbehaviour.MISSING_IDEMPOTENCY_KEY
                ),
                registry=acp_registry,
                limiter=acp_limiter,
            )
        assert caught.value.reason == "acp_idempotency_key_required_on_mutation"

    def test_an_over_long_idempotency_key_is_refused_by_name(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        """Better here than as an opaque driver error inside a money transaction."""
        with pytest.raises(SchemaRejected) as caught:
            gate(
                cp_session,
                acp_simulator.create_session(
                    acp_now, body=READY_BODY, idempotency_key="k" * (MAX_IDEMPOTENCY_KEY_LENGTH + 1)
                ),
                registry=acp_registry,
                limiter=acp_limiter,
            )
        assert caught.value.reason == "acp_idempotency_key_too_long"

    def test_a_read_needs_no_idempotency_key(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        admitted = gate(
            cp_session,
            acp_simulator.retrieve_session(acp_now, "cs_1"),
            registry=acp_registry,
            limiter=acp_limiter,
        )
        assert admitted.idempotency_key is None

    def test_no_credential_at_all_is_refused(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        with pytest.raises(AuthenticationRejected) as caught:
            gate(
                cp_session,
                acp_simulator.create_session(
                    acp_now, body=READY_BODY, misbehave=Misbehaviour.NO_CREDENTIAL
                ),
                registry=acp_registry,
                limiter=acp_limiter,
            )
        assert caught.value.reason == "acp_credential_absent"

    def test_two_credentials_at_once_are_refused_rather_than_resolved(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        """Two answers to "who is this" is the shape of every credential-confusion bug."""
        with pytest.raises(AuthenticationRejected) as caught:
            gate(
                cp_session,
                acp_simulator.create_session(
                    acp_now, body=READY_BODY, misbehave=Misbehaviour.BOTH_CREDENTIALS
                ),
                registry=acp_registry,
                limiter=acp_limiter,
            )
        assert caught.value.reason == "acp_credential_ambiguous"

    def test_an_unimplemented_signature_algorithm_is_refused(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        with pytest.raises(SignatureRejected) as caught:
            gate(
                cp_session,
                acp_simulator.create_session(
                    acp_now, body=READY_BODY, misbehave=Misbehaviour.WRONG_SIGNATURE_ALGORITHM
                ),
                registry=acp_registry,
                limiter=acp_limiter,
            )
        assert caught.value.reason == "acp_signature_algorithm_unsupported"

    def test_the_api_key_mechanism_admits_a_read(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        admitted = gate(
            cp_session,
            acp_simulator.request(
                method="GET",
                path=f"{BASE_PATH}/cs_1",
                now=acp_now,
                credential=Credential.API_KEY,
            ),
            registry=acp_registry,
            limiter=acp_limiter,
        )
        assert admitted.caller.authenticated_by == "protocol_api_key"

    def test_a_wrong_api_key_is_refused(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        acp_simulator.api_key = "acpk_live_ffffffffffffffffffffffffffffffff"
        with pytest.raises(AuthenticationRejected) as caught:
            gate(
                cp_session,
                acp_simulator.request(
                    method="GET",
                    path=f"{BASE_PATH}/cs_1",
                    now=acp_now,
                    credential=Credential.API_KEY,
                ),
                registry=acp_registry,
                limiter=acp_limiter,
            )
        assert caught.value.reason == "acp_api_key_did_not_verify"

    def test_an_api_key_issued_for_another_audience_is_inert_here(
        self,
        cp_session: Session,
        cp_tenant: tuple[uuid.UUID, uuid.UUID],
        acp_simulator: AcpBuyerSimulator,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        """A key cannot bind an audience cryptographically, so its registration does."""
        tenant_id, merchant_id = cp_tenant
        sandbox = AcpClient(
            client_id=acp_simulator.client_id,
            tenant_id=tenant_id,
            merchant_id=merchant_id,
            audience="https://acp.sandbox.invalid/v1",
            signing_secret=SIGNING_SECRET,
            api_key_digest=sha256_b64url(API_KEY.encode()),
        )
        with pytest.raises(AuthenticationRejected) as caught:
            gate(
                cp_session,
                acp_simulator.request(
                    method="GET",
                    path=f"{BASE_PATH}/cs_1",
                    now=acp_now,
                    credential=Credential.API_KEY,
                ),
                registry=ClientRegistry.of(sandbox),
                limiter=acp_limiter,
            )
        assert caught.value.reason == "acp_api_key_audience_mismatch"

    def test_the_gate_refuses_the_request_after_the_burst_is_spent(
        self,
        cp_session: Session,
        cp_tenant: tuple[uuid.UUID, uuid.UUID],
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        """Two admitted, the third refused, with everything else about it correct."""
        tenant_id, merchant_id = cp_tenant
        simulator = AcpBuyerSimulator(
            client_id="acp-client-throttled",
            signing_secret=SIGNING_SECRET,
            audience=AUDIENCE,
            seed="throttled",
            client_rate=RateLimit(capacity=2, refill_per_second=1),
        )
        registry = ClientRegistry.of(
            simulator.registration(tenant_id=tenant_id, merchant_id=merchant_id)
        )
        for _ in range(2):
            gate(
                cp_session,
                simulator.create_session(acp_now, body=READY_BODY),
                registry=registry,
                limiter=acp_limiter,
            )
        with pytest.raises(RateLimited) as caught:
            gate(
                cp_session,
                simulator.create_session(acp_now, body=READY_BODY),
                registry=registry,
                limiter=acp_limiter,
            )
        assert caught.value.details["retry_after_seconds"] == 1


# ------------------------------------------------------------- credential hygiene


class TestCredentialHygiene:
    """Nothing that failed to verify may travel back, or into an immutable chain."""

    pytestmark = pytest.mark.db

    def test_a_rejection_never_echoes_the_signature_it_refused(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        request = acp_simulator.create_session(
            acp_now, body=READY_BODY, misbehave=Misbehaviour.TAMPERED_SIGNATURE
        )
        presented = request.header("Signature") or ""
        assert presented
        with pytest.raises(ProtocolRejection) as caught:
            gate(cp_session, request, registry=acp_registry, limiter=acp_limiter)
        rendered = f"{caught.value!s} {caught.value!r} {caught.value.details!r}"
        assert presented not in rendered

    def test_a_rejection_never_echoes_the_api_key_it_refused(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        acp_simulator.api_key = "acpk_live_deadbeefdeadbeefdeadbeefdeadbeef"
        with pytest.raises(AuthenticationRejected) as caught:
            gate(
                cp_session,
                acp_simulator.request(
                    method="GET",
                    path=f"{BASE_PATH}/cs_1",
                    now=acp_now,
                    credential=Credential.API_KEY,
                ),
                registry=acp_registry,
                limiter=acp_limiter,
            )
        rendered = f"{caught.value!s} {caught.value!r} {caught.value.details!r}"
        assert "deadbeef" not in rendered

    def test_evidence_fingerprints_an_accepted_signature_rather_than_keeping_it(
        self,
        cp_session: Session,
        cp_tenant: tuple[uuid.UUID, uuid.UUID],
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        """Specification 28, applied at the point of writing because a chain cannot be redacted."""
        tenant_id, _ = cp_tenant
        request = acp_simulator.create_session(acp_now, body=READY_BODY)
        presented = request.header("Signature") or ""
        gate(cp_session, request, registry=acp_registry, limiter=acp_limiter)

        rows = audit_payloads(cp_session, tenant_id)
        serialised = json.dumps(rows)
        assert presented not in serialised
        received = next(p for p in rows if p["stage"] == "RECEIVED")
        assert received["credential"]["digest"] == sha256_b64url(presented.encode())
        assert len(received["credential"]["preview"]) == 8
        assert "signature" not in received["headers"]
        assert "authorization" not in received["headers"]

    def test_evidence_never_holds_even_a_preview_of_an_api_key(
        self,
        cp_session: Session,
        cp_tenant: tuple[uuid.UUID, uuid.UUID],
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        """A signature is spent; a bearer token is not, so not even eight characters of it."""
        tenant_id, _ = cp_tenant
        gate(
            cp_session,
            acp_simulator.request(
                method="GET",
                path=f"{BASE_PATH}/cs_1",
                now=acp_now,
                credential=Credential.API_KEY,
            ),
            registry=acp_registry,
            limiter=acp_limiter,
        )
        serialised = json.dumps(audit_payloads(cp_session, tenant_id))
        assert API_KEY not in serialised
        assert API_KEY[:8] not in serialised

    def test_a_refusal_before_authentication_writes_into_no_tenants_chain(
        self,
        cp_session: Session,
        cp_tenant: tuple[uuid.UUID, uuid.UUID],
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        """Otherwise the evidence rule would be an unauthenticated write primitive."""
        tenant_id, _ = cp_tenant
        with pytest.raises(SignatureRejected):
            gate(
                cp_session,
                acp_simulator.create_session(
                    acp_now, body=READY_BODY, misbehave=Misbehaviour.WRONG_SIGNING_KEY
                ),
                registry=acp_registry,
                limiter=acp_limiter,
            )
        assert audit_payloads(cp_session, tenant_id) == []

    def test_an_admitted_request_records_the_claim_boundary(
        self,
        cp_session: Session,
        cp_tenant: tuple[uuid.UUID, uuid.UUID],
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        """Specification 16.2 asserted where it cannot later be softened: the audit chain."""
        tenant_id, _ = cp_tenant
        gate(
            cp_session,
            acp_simulator.create_session(acp_now, body=READY_BODY),
            registry=acp_registry,
            limiter=acp_limiter,
        )
        authenticated = next(
            p for p in audit_payloads(cp_session, tenant_id) if p["stage"] == "AUTHENTICATED"
        )
        assert authenticated["claim_boundary"] == "COMPATIBLE_INTERFACE"
        assert authenticated["mechanism"] == "http_message_signature"


# ------------------------------------------------------------------ session mapping


@pytest.fixture
def ready_session() -> AcpSession:
    """A session frozen into version 3 and approved by a human on the trusted surface."""
    return AcpSession(
        session_id="cs_ready",
        status=AcpSessionStatus.READY_FOR_PAYMENT,
        supplied=frozenset({"items", "buyer", "fulfillment"}),
        checkout_id=uuid7(),
        checkout_version=3,
        content_hash="Zm9vYmFyLWhhc2g",
        amount=TOTAL,
        approved_version=3,
        approval_id=uuid7(),
        authority_epoch=7,
    )


class TestSessionMapping:
    """Specification 16.1: an ACP session becomes one intent, keeping every invariant."""

    pytestmark = pytest.mark.db

    def _admit(
        self,
        session: Session,
        request: AcpRequest,
        registry: ClientRegistry,
        limiter: TokenBucketLimiter,
    ) -> tuple[AdmittedRequest, AcpOperation]:
        resolved = route(request.method, request.path)
        admitted = admit(
            session,
            request,
            registry=registry,
            audience=AUDIENCE,
            limiter=limiter,
            requires_idempotency_key=resolved.requires_idempotency_key,
        )
        return admitted, resolved.operation

    def test_a_session_with_only_items_is_basket_construction(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        admitted, operation = self._admit(
            cp_session,
            acp_simulator.create_session(acp_now, body={"items": [{"sku": "X", "quantity": 1}]}),
            acp_registry,
            acp_limiter,
        )
        intent = map_request(cp_session, admitted=admitted, operation=operation, authority_epoch=0)
        assert intent.kind is IntentKind.BUILD_BASKET
        assert not intent.moves_money
        assert intent.raw_reference == str(admitted.interaction.interaction_id)

    def test_the_update_that_completes_a_session_freezes_a_checkout(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        """One ACP request, two internal steps; the intent names the further one."""
        partial = AcpSession(
            session_id="cs_1",
            status=AcpSessionStatus.NOT_READY_FOR_PAYMENT,
            supplied=frozenset({"items", "buyer"}),
        )
        admitted, operation = self._admit(
            cp_session,
            acp_simulator.update_session(
                acp_now, "cs_1", body={"fulfillment": {"pincode": "560001"}}
            ),
            acp_registry,
            acp_limiter,
        )
        intent = map_request(
            cp_session,
            admitted=admitted,
            operation=operation,
            acp_session=partial,
            authority_epoch=0,
        )
        assert intent.kind is IntentKind.CREATE_CHECKOUT
        assert intent.external_id == "cs_1"

    def test_a_named_but_empty_field_does_not_make_a_session_ready(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        """``"items": []`` would otherwise freeze a checkout with nothing in it to approve."""
        partial = AcpSession(
            session_id="cs_1",
            status=AcpSessionStatus.NOT_READY_FOR_PAYMENT,
            supplied=frozenset({"buyer", "fulfillment"}),
        )
        admitted, operation = self._admit(
            cp_session,
            acp_simulator.update_session(acp_now, "cs_1", body={"items": []}),
            acp_registry,
            acp_limiter,
        )
        intent = map_request(
            cp_session,
            admitted=admitted,
            operation=operation,
            acp_session=partial,
            authority_epoch=0,
        )
        assert intent.kind is IntentKind.BUILD_BASKET

    def test_a_completion_maps_to_the_one_intent_that_can_reach_admission(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
        ready_session: AcpSession,
    ) -> None:
        admitted, operation = self._admit(
            cp_session,
            acp_simulator.complete_session(
                acp_now,
                ready_session.session_id,
                checkout_version=3,
                content_hash="Zm9vYmFyLWhhc2g",
                amount_minor=TOTAL.minor,
            ),
            acp_registry,
            acp_limiter,
        )
        intent = map_request(
            cp_session,
            admitted=admitted,
            operation=operation,
            acp_session=ready_session,
            authority_epoch=7,
        )
        assert intent.kind is IntentKind.SUBMIT_APPROVED
        assert intent.moves_money
        assert intent.amount == TOTAL
        assert intent.checkout_version == 3
        assert intent.content_hash == "Zm9vYmFyLWhhc2g"

    def test_a_completion_without_a_recorded_approval_is_refused(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
        ready_session: AcpSession,
    ) -> None:
        """ACP's ``complete`` carries the external platform's payment data. It is not consent.

        The session below is otherwise perfect: ready for payment, the right version, the
        right hash, the right amount, the right epoch. It has no approval recorded on this
        platform's trusted surface, and that alone is disqualifying.
        """
        unapproved = AcpSession(
            session_id=ready_session.session_id,
            status=ready_session.status,
            supplied=ready_session.supplied,
            checkout_id=ready_session.checkout_id,
            checkout_version=3,
            content_hash=ready_session.content_hash,
            amount=TOTAL,
            authority_epoch=7,
        )
        admitted, operation = self._admit(
            cp_session,
            acp_simulator.complete_session(
                acp_now,
                unapproved.session_id,
                checkout_version=3,
                content_hash="Zm9vYmFyLWhhc2g",
                amount_minor=TOTAL.minor,
            ),
            acp_registry,
            acp_limiter,
        )
        with pytest.raises(MandateRejected) as caught:
            map_request(
                cp_session,
                admitted=admitted,
                operation=operation,
                acp_session=unapproved,
                authority_epoch=7,
            )
        assert caught.value.reason == "acp_completion_without_recorded_approval"
        assert caught.value.code is RecoveryCode.AUTHORITY_INSUFFICIENT

    @pytest.mark.parametrize(
        ("version", "content_hash"),
        [(2, "Zm9vYmFyLWhhc2g"), (3, "c3RhbGUtaGFzaA")],
    )
    def test_a_completion_naming_superseded_state_is_stale_not_forbidden(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
        ready_session: AcpSession,
        version: int,
        content_hash: str,
    ) -> None:
        """``STALE_CHECKOUT`` tells every layer above that re-reading is the remedy."""
        admitted, operation = self._admit(
            cp_session,
            acp_simulator.complete_session(
                acp_now,
                ready_session.session_id,
                checkout_version=version,
                content_hash=content_hash,
                amount_minor=TOTAL.minor,
            ),
            acp_registry,
            acp_limiter,
        )
        with pytest.raises(StateRejected) as caught:
            map_request(
                cp_session,
                admitted=admitted,
                operation=operation,
                acp_session=ready_session,
                authority_epoch=7,
            )
        assert caught.value.reason == "acp_checkout_version_superseded"
        assert caught.value.code is RecoveryCode.STALE_CHECKOUT

    def test_a_completion_that_names_no_version_at_all_is_refused(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
        ready_session: AcpSession,
    ) -> None:
        """This surface's one deliberate narrowing of ACP; see the module it lives in."""
        admitted, operation = self._admit(
            cp_session,
            acp_simulator.request(
                method="POST",
                path=f"{BASE_PATH}/{ready_session.session_id}/complete",
                now=acp_now,
                body={"payment_data": {"token": "tok_external"}},
            ),
            acp_registry,
            acp_limiter,
        )
        with pytest.raises(SchemaRejected) as caught:
            map_request(
                cp_session,
                admitted=admitted,
                operation=operation,
                acp_session=ready_session,
                authority_epoch=7,
            )
        assert caught.value.reason == "acp_completion_must_echo_version_and_hash"

    def test_a_completion_under_a_revoked_authority_epoch_is_refused(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
        ready_session: AcpSession,
    ) -> None:
        admitted, operation = self._admit(
            cp_session,
            acp_simulator.complete_session(
                acp_now,
                ready_session.session_id,
                checkout_version=3,
                content_hash="Zm9vYmFyLWhhc2g",
                amount_minor=TOTAL.minor,
            ),
            acp_registry,
            acp_limiter,
        )
        with pytest.raises(MandateRejected) as caught:
            map_request(
                cp_session,
                admitted=admitted,
                operation=operation,
                acp_session=ready_session,
                authority_epoch=8,
            )
        assert caught.value.reason == "acp_authority_epoch_superseded"

    def test_an_expired_approval_is_refused_against_the_database_clock(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
        ready_session: AcpSession,
    ) -> None:
        """A skewed pod must not be able to extend consent past the window a buyer saw."""
        expired = AcpSession(
            session_id=ready_session.session_id,
            status=ready_session.status,
            supplied=ready_session.supplied,
            checkout_id=ready_session.checkout_id,
            checkout_version=3,
            content_hash=ready_session.content_hash,
            amount=TOTAL,
            approved_version=3,
            approval_id=ready_session.approval_id,
            approval_expires_at=acp_now - timedelta(seconds=1),
            authority_epoch=7,
        )
        admitted, operation = self._admit(
            cp_session,
            acp_simulator.complete_session(
                acp_now,
                expired.session_id,
                checkout_version=3,
                content_hash="Zm9vYmFyLWhhc2g",
                amount_minor=TOTAL.minor,
            ),
            acp_registry,
            acp_limiter,
        )
        with pytest.raises(MandateRejected) as caught:
            map_request(
                cp_session,
                admitted=admitted,
                operation=operation,
                acp_session=expired,
                authority_epoch=7,
            )
        assert caught.value.reason == "acp_approval_expired"

    def test_a_completion_for_a_different_amount_is_refused(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
        ready_session: AcpSession,
    ) -> None:
        admitted, operation = self._admit(
            cp_session,
            acp_simulator.complete_session(
                acp_now,
                ready_session.session_id,
                checkout_version=3,
                content_hash="Zm9vYmFyLWhhc2g",
                amount_minor=TOTAL.minor + 1,
            ),
            acp_registry,
            acp_limiter,
        )
        with pytest.raises(MandateRejected) as caught:
            map_request(
                cp_session,
                admitted=admitted,
                operation=operation,
                acp_session=ready_session,
                authority_epoch=7,
            )
        assert caught.value.reason == "acp_amount_does_not_match_approved_total"

    def test_an_amount_that_arrived_as_a_float_never_reaches_the_mapping(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
        ready_session: AcpSession,
    ) -> None:
        """``json.loads`` turns ``39500.0`` into a float, and the gate refuses it at the door.

        Two rules meet here and agree. The audit chain cannot canonicalize a float, so a
        body carrying one cannot be evidenced and therefore cannot be honoured; and no float
        may touch an amount anywhere in this platform. The refusal is structured, which
        matters more than it sounds: before this check existed the same request raised
        ``AuditContentError`` from inside ``audit.append``, turning an external party's
        ordinary mistake into a 5xx it could reproduce at will.
        """
        with pytest.raises(SchemaRejected) as caught:
            self._admit(
                cp_session,
                acp_simulator.request(
                    method="POST",
                    path=f"{BASE_PATH}/{ready_session.session_id}/complete",
                    now=acp_now,
                    body={
                        "checkout_version": 3,
                        "content_hash": "Zm9vYmFyLWhhc2g",
                        "total": {"amount_minor": 39500.0, "currency": "INR"},
                    },
                ),
                acp_registry,
                acp_limiter,
            )
        assert caught.value.reason == "acp_body_carries_a_non_integer_number"
        assert caught.value.details["at"] == "$.total.amount_minor"

    def test_a_session_with_no_approved_total_cannot_be_completed_at_all(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
        ready_session: AcpSession,
    ) -> None:
        """The amount comparison must not be skippable by the state it compares against.

        A projection carrying a recorded approval but no total is this platform not knowing
        what the human agreed to. The one wrong way out is to accept the number in the
        request body -- which would make an external party's field the amount on the only
        intent that ``moves_money``, and would do it silently, on the path where an
        approval already exists and every other check has passed.
        """
        amountless = AcpSession(
            session_id=ready_session.session_id,
            status=ready_session.status,
            supplied=ready_session.supplied,
            checkout_id=ready_session.checkout_id,
            checkout_version=3,
            content_hash=ready_session.content_hash,
            amount=None,
            approved_version=3,
            approval_id=ready_session.approval_id,
            authority_epoch=7,
        )
        admitted, operation = self._admit(
            cp_session,
            acp_simulator.complete_session(
                acp_now,
                amountless.session_id,
                checkout_version=3,
                content_hash="Zm9vYmFyLWhhc2g",
                amount_minor=99_999_999,
            ),
            acp_registry,
            acp_limiter,
        )
        with pytest.raises(MandateRejected) as caught:
            map_request(
                cp_session,
                admitted=admitted,
                operation=operation,
                acp_session=amountless,
                authority_epoch=7,
            )
        assert caught.value.reason == "acp_approved_total_unknown"

    def test_the_amount_that_travels_onward_is_the_approved_one_not_the_presented_one(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
        ready_session: AcpSession,
    ) -> None:
        """Provenance, asserted where equality would look like enough.

        The two are equal by the time the intent is built, so no assertion on the value can
        tell them apart. The identity check can: the amount on a money-moving intent must
        be the object taken from what a human approved, so that a later edit loosening the
        comparison cannot quietly promote a request field into an amount.
        """
        admitted, operation = self._admit(
            cp_session,
            acp_simulator.complete_session(
                acp_now,
                ready_session.session_id,
                checkout_version=3,
                content_hash="Zm9vYmFyLWhhc2g",
                amount_minor=TOTAL.minor,
            ),
            acp_registry,
            acp_limiter,
        )
        intent = map_request(
            cp_session,
            admitted=admitted,
            operation=operation,
            acp_session=ready_session,
            authority_epoch=7,
        )
        assert intent.amount is ready_session.amount

    def test_the_revocation_check_cannot_be_defeated_by_omitting_the_epoch(self) -> None:
        """A security check whose default is "permit" reads like a check and is not one.

        ``authority_epoch`` and ``AcpSession.authority_epoch`` once both defaulted to zero,
        so a caller that simply forgot the argument got a revocation check that compared two
        defaults and agreed with itself. It is required now, which turns the omission into a
        type error at every call site instead of a runtime pass.
        """
        import inspect

        parameter = inspect.signature(map_request).parameters["authority_epoch"]
        assert parameter.default is inspect.Parameter.empty
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY

    def test_the_amount_parser_refuses_a_float_on_its_own_account(self) -> None:
        """Defence in depth: the gate is not the only caller ``parse_amount`` may ever have."""
        with pytest.raises(SchemaRejected) as caught:
            parse_amount({"total": {"amount_minor": 39500.0, "currency": "INR"}})
        assert caught.value.reason == "acp_amount_is_not_integer_minor_units"
        assert caught.value.details["presented_type"] == "float"

    def test_a_boolean_is_not_one_paisa(self) -> None:
        """``bool`` subclasses ``int``, so it has to be excluded by name."""
        with pytest.raises(SchemaRejected):
            parse_amount({"total": {"amount_minor": True, "currency": "INR"}})

    @pytest.mark.parametrize("status", [AcpSessionStatus.IN_PROGRESS, AcpSessionStatus.COMPLETED])
    def test_a_session_whose_payment_has_started_cannot_be_amended(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
        status: AcpSessionStatus,
    ) -> None:
        """The payment-state invariant: nothing changes under a payment in flight."""
        in_flight = AcpSession(session_id="cs_1", status=status)
        admitted, operation = self._admit(
            cp_session,
            acp_simulator.update_session(acp_now, "cs_1", body={"items": [{"sku": "X"}]}),
            acp_registry,
            acp_limiter,
        )
        with pytest.raises(StateRejected) as caught:
            map_request(
                cp_session,
                admitted=admitted,
                operation=operation,
                acp_session=in_flight,
                authority_epoch=7,
            )
        assert caught.value.reason == "acp_session_state_forbids_operation"
        assert caught.value.details["status"] == status.value

    def test_a_completed_session_cannot_be_completed_again(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
        ready_session: AcpSession,
    ) -> None:
        done = AcpSession(
            session_id=ready_session.session_id,
            status=AcpSessionStatus.COMPLETED,
            checkout_version=3,
            content_hash=ready_session.content_hash,
            approved_version=3,
            approval_id=ready_session.approval_id,
            amount=TOTAL,
            authority_epoch=7,
        )
        admitted, operation = self._admit(
            cp_session,
            acp_simulator.complete_session(
                acp_now,
                done.session_id,
                checkout_version=3,
                content_hash="Zm9vYmFyLWhhc2g",
                amount_minor=TOTAL.minor,
            ),
            acp_registry,
            acp_limiter,
        )
        with pytest.raises(StateRejected):
            map_request(
                cp_session,
                admitted=admitted,
                operation=operation,
                acp_session=done,
                authority_epoch=7,
            )

    def test_a_cancellation_is_a_proposal_and_never_an_outcome(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
        ready_session: AcpSession,
    ) -> None:
        admitted, operation = self._admit(
            cp_session,
            acp_simulator.cancel_session(acp_now, ready_session.session_id),
            acp_registry,
            acp_limiter,
        )
        intent = map_request(
            cp_session,
            admitted=admitted,
            operation=operation,
            acp_session=ready_session,
            authority_epoch=7,
        )
        assert intent.kind is IntentKind.PROPOSE_CANCELLATION
        assert intent.is_proposal
        assert not intent.moves_money
        assert intent.amount is None, "a proposal names no amount (specification 29.4)"

    def test_a_retrieval_is_a_read_to_every_layer_below(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
        ready_session: AcpSession,
    ) -> None:
        admitted, operation = self._admit(
            cp_session,
            acp_simulator.retrieve_session(acp_now, ready_session.session_id),
            acp_registry,
            acp_limiter,
        )
        intent = map_request(
            cp_session,
            admitted=admitted,
            operation=operation,
            acp_session=ready_session,
            authority_epoch=7,
        )
        assert intent.is_read_only

    def test_an_unknown_session_is_reported_as_stale_rather_than_as_a_secret(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        admitted, operation = self._admit(
            cp_session,
            acp_simulator.retrieve_session(acp_now, "cs_nope"),
            acp_registry,
            acp_limiter,
        )
        with pytest.raises(StateRejected) as caught:
            map_request(cp_session, admitted=admitted, operation=operation, authority_epoch=0)
        assert caught.value.reason == "acp_session_not_found"
        assert caught.value.details == {}

    def test_no_acp_request_can_map_to_an_approval(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
        ready_session: AcpSession,
    ) -> None:
        """Exhaustive over the routing table, because the absence is the guarantee.

        Every endpoint this surface exposes is driven with a request the caller controls
        entirely, and none of them produces ``REQUEST_APPROVAL``. The external party cannot
        ask a human on the human's behalf, and cannot ask this platform to pretend one did.
        """
        basket_only = AcpSession(
            session_id="cs_partial", status=AcpSessionStatus.NOT_READY_FOR_PAYMENT
        )
        cases: list[tuple[AcpRequest, AcpSession | None]] = [
            (acp_simulator.create_session(acp_now, body=READY_BODY), None),
            (
                acp_simulator.update_session(
                    acp_now, "cs_partial", body={"items": [{"sku": "X", "quantity": 1}]}
                ),
                basket_only,
            ),
            (
                acp_simulator.update_session(acp_now, ready_session.session_id, body=READY_BODY),
                ready_session,
            ),
            (acp_simulator.retrieve_session(acp_now, ready_session.session_id), ready_session),
            (acp_simulator.cancel_session(acp_now, ready_session.session_id), ready_session),
            (
                acp_simulator.complete_session(
                    acp_now,
                    ready_session.session_id,
                    checkout_version=3,
                    content_hash="Zm9vYmFyLWhhc2g",
                    amount_minor=TOTAL.minor,
                ),
                ready_session,
            ),
        ]
        kinds = set()
        for request, state in cases:
            admitted, operation = self._admit(cp_session, request, acp_registry, acp_limiter)
            intent = map_request(
                cp_session,
                admitted=admitted,
                operation=operation,
                acp_session=state,
                authority_epoch=7,
            )
            kinds.add(intent.kind)
        assert IntentKind.REQUEST_APPROVAL not in kinds
        assert kinds == {
            IntentKind.BUILD_BASKET,
            IntentKind.CREATE_CHECKOUT,
            IntentKind.TRACK_ORDER,
            IntentKind.PROPOSE_CANCELLATION,
            IntentKind.SUBMIT_APPROVED,
        }

    def test_the_approval_handoff_asks_a_human_and_moves_no_money(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
        ready_session: AcpSession,
    ) -> None:
        """Raised by the adapter, never by the caller. See ``sessions.approval_handoff``."""
        admitted, _ = self._admit(
            cp_session,
            acp_simulator.update_session(acp_now, ready_session.session_id, body=READY_BODY),
            acp_registry,
            acp_limiter,
        )
        intent = approval_handoff(admitted, ready_session)
        assert intent.kind is IntentKind.REQUEST_APPROVAL
        assert intent.is_proposal
        assert not intent.moves_money


# -------------------------------------------------------------------- idempotency


class TestIdempotencyOnMutations:
    """Specification 16.3's last rule, against the kernel's real unique index."""

    pytestmark = pytest.mark.db

    def _claim(self, session: Session, key: str, body: Mapping[str, Any]) -> dict[str, Any]:
        with idempotent(session, key, MUTATION_OPERATION, dict(body)) as slot:
            response = {"session_id": "cs_1", "status": "READY_FOR_PAYMENT"}
            slot.store(response)
            return response

    def test_the_same_key_and_the_same_request_replays_without_re_executing(
        self, cp_session: Session
    ) -> None:
        key = idempotency_key_for("acp-client-alpha", AcpOperation.CREATE_SESSION, "abc")
        first = self._claim(cp_session, key, READY_BODY)
        with pytest.raises(IdempotentReplayError) as caught:
            self._claim(cp_session, key, READY_BODY)
        assert caught.value.response == first
        assert caught.value.code is RecoveryCode.DUPLICATE_OPERATION

    def test_the_same_key_with_a_changed_amount_is_refused_and_executes_nothing(
        self, cp_session: Session
    ) -> None:
        """The row that matters most: neither re-executing nor answering with the old result."""
        key = idempotency_key_for("acp-client-alpha", AcpOperation.COMPLETE_SESSION, "abc")
        self._claim(cp_session, key, {"total": {"amount_minor": 39500, "currency": "INR"}})
        with pytest.raises(IdempotencyKeyReuseError) as caught:
            self._claim(cp_session, key, {"total": {"amount_minor": 395000, "currency": "INR"}})
        assert caught.value.code is RecoveryCode.POLICY_EXCEPTION

    def test_two_clients_presenting_the_same_key_do_not_collide(self, cp_session: Session) -> None:
        """One integration must not be able to burn another's key by guessing it."""
        mine = idempotency_key_for("acp-client-alpha", AcpOperation.CREATE_SESSION, "shared")
        theirs = idempotency_key_for("acp-client-beta", AcpOperation.CREATE_SESSION, "shared")
        assert mine != theirs
        self._claim(cp_session, mine, READY_BODY)
        self._claim(cp_session, theirs, READY_BODY)

    def test_the_same_key_on_two_operations_is_two_operations(self, cp_session: Session) -> None:
        update = idempotency_key_for("acp-client-alpha", AcpOperation.UPDATE_SESSION, "k")
        complete = idempotency_key_for("acp-client-alpha", AcpOperation.COMPLETE_SESSION, "k")
        assert update != complete
        self._claim(cp_session, update, READY_BODY)
        self._claim(cp_session, complete, READY_BODY)

    def test_an_honest_retry_carries_the_same_key_and_a_fresh_nonce(
        self,
        cp_session: Session,
        acp_simulator: AcpBuyerSimulator,
        acp_registry: ClientRegistry,
        acp_limiter: TokenBucketLimiter,
        acp_now: datetime,
    ) -> None:
        """The reason the two must never be conflated.

        A proxy retrying a timed-out mutation reuses its idempotency key so the operation
        does not happen twice, and mints a fresh nonce so the request is not a replay. If
        the key were silently derived from the nonce, every retry would create a second
        checkout; if the nonce were derived from the key, every retry would be refused.
        """
        first = acp_simulator.create_session(acp_now, body=READY_BODY, idempotency_key="retry-1")
        retry = acp_simulator.create_session(acp_now, body=READY_BODY, idempotency_key="retry-1")
        assert first.header("Request-Id") != retry.header("Request-Id")

        one = gate(cp_session, first, registry=acp_registry, limiter=acp_limiter)
        two = gate(cp_session, retry, registry=acp_registry, limiter=acp_limiter)
        assert one.idempotency_key == two.idempotency_key == "retry-1"

        key = idempotency_key_for(one.caller.client_id, AcpOperation.CREATE_SESSION, "retry-1")
        self._claim(cp_session, key, READY_BODY)
        with pytest.raises(IdempotentReplayError):
            self._claim(cp_session, key, READY_BODY)


# ------------------------------------------------------------------- house-keeping


def test_the_simulator_reads_no_clock_and_draws_no_randomness() -> None:
    """The determinism claim, checked at the source rather than inferred from one run.

    ``test_two_runs_from_the_same_seed_and_clock_produce_identical_bytes`` would still pass
    on a fast enough machine if the simulator called ``datetime.now()`` once per request, so
    the property is also asserted the only way that cannot be got lucky with.
    """
    import commerce_protocols.acp.simulator as module

    source = module.__file__
    assert source is not None
    with open(source, encoding="utf-8") as handle:
        body = handle.read()
    forbidden = ("datetime.now(", "time.time(", "time.monotonic(", "import random", "uuid4(")
    for call in forbidden:
        assert call not in body, f"the simulator must not call {call}"
