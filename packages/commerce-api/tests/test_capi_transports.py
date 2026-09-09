"""The ACP and MCP transports over HTTP: what they let through, and what they refuse.

The protocol layer's own suite proves the gates work. This file proves the *transports* --
that the gates are actually in the path, that a caller reaching them becomes a principal
narrower than the session it came from, and that the refusals are the ones the architecture
promises rather than the ones the code happens to produce.

The refusals are the interesting half, so they are tested by name:

* an unconfigured deployment has no transport at all, and says 404 rather than 401;
* a token minted from a buyer session carries no consent scope, because the ceiling took
  it, and there is no tool that could use one anyway;
* a session whose grant is narrower cannot see the tools it may not call, and is refused
  by name when it calls one regardless;
* an access token opens exactly one session;
* a token minted for another resource does not verify here;
* an ACP client whose registry entry omits ``checkout.submit_approved`` completes nothing;
* an ACP completion without an approval recorded on the trusted surface is refused, and
  the refusal is about authority rather than about state.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import pytest
from commerce_api.app import create_app
from commerce_api.settings import Settings
from commerce_domain import sha256_b64url
from commerce_protocols.acp import sign, signing_string
from commerce_protocols.core import PINS, PROTOCOL_CAPABILITIES, Protocol
from commerce_protocols.mcp import (
    FORBIDDEN_ARGUMENT_NAMES,
    FORBIDDEN_TOOL_NAMES,
    TOOLS,
    ToolName,
    public_name_offends,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from conftest import (
    APP_URL,
    KERNEL_URL,
    TEST_KEY_ID,
    TEST_KEY_SECRET,
    TEST_SCENARIO_KEY,
    TEST_WEBHOOK_SECRET,
    MintedSession,
    SeededTenant,
)

pytestmark = pytest.mark.db

#: This deployment's RFC 8707 resource indicator for the tests.
RESOURCE: Final[str] = "https://tests.invalid/mcp"

#: The ACP audience these tests sign for. A signature minted for anything else must not
#: verify here, which is what ``test_a_signature_for_another_audience_is_refused`` checks.
AUDIENCE: Final[str] = "https://tests.invalid/acp"

MCP_SECRET: Final[str] = "mcp-transport-test-token-secret"  # noqa: S105 - a test HMAC key
ACP_SECRET: Final[str] = "acp-transport-test-signing-secret"  # noqa: S105 - a test HMAC key

#: The buyer both surfaces act for. Fixed so the trusted surface can approve what a
#: protocol caller constructed, which is the whole shape of the demonstration: the agent
#: proposes and the human consents.
BUYER_REF: Final[str] = "acp-buyer-1"

ACP_VERSION: Final[str] = PINS[Protocol.ACP].version
MCP_VERSION: Final[str] = PINS[Protocol.MCP].version


# ------------------------------------------------------------------------------ fixtures


def _settings(seeded: SeededTenant, *, granted: list[str] | None = None) -> Settings:
    """Settings with both transports configured against one seeded tenant."""
    client = {
        "client_id": "acp-test-client",
        "tenant_id": str(seeded.tenant_id),
        "merchant_id": str(seeded.merchant_id),
        "signing_secret": ACP_SECRET,
        "buyer_ref": BUYER_REF,
    }
    if granted is not None:
        client["granted"] = granted
    return Settings(
        PROFILE="development",
        DATABASE_URL_APP=APP_URL,
        DATABASE_URL_KERNEL=KERNEL_URL,
        RAZORPAY_KEY_ID=TEST_KEY_ID,
        RAZORPAY_KEY_SECRET=TEST_KEY_SECRET,
        RAZORPAY_WEBHOOK_SECRET=TEST_WEBHOOK_SECRET,
        SCENARIO_KEY=TEST_SCENARIO_KEY,
        SESSION_TTL_SECONDS=3600,
        MCP_RESOURCE=RESOURCE,
        MCP_TOKEN_SECRET=MCP_SECRET,
        ACP_AUDIENCE=AUDIENCE,
        ACP_CLIENTS=json.dumps([client]),
    )


@pytest.fixture
def transport_app(seeded_tenant: SeededTenant) -> FastAPI:
    """An app with both transports configured. Built per test, like every other app here."""
    return create_app(_settings(seeded_tenant))


@pytest.fixture
def transport_client(transport_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(transport_app) as test_client:
        yield test_client


@pytest.fixture
def mint_on(
    transport_client: TestClient, seeded_tenant: SeededTenant
) -> Callable[..., tuple[TestClient, MintedSession]]:
    """Mint a demo session on the transport-enabled app.

    The conftest's ``mint_client`` mints against the app built from ``settings_for_tests``,
    which deliberately configures no transport, so these tests need their own.
    """

    def _mint(
        actor_type: str = "BUYER", buyer_ref: str = BUYER_REF
    ) -> tuple[TestClient, MintedSession]:
        headers = {"X-Scenario-Key": TEST_SCENARIO_KEY} if actor_type == "OPERATOR" else {}
        response = transport_client.post(
            "/v1/demo/sessions",
            json={
                "tenant_slug": seeded_tenant.tenant_slug,
                "actor_type": actor_type,
                "buyer_ref": buyer_ref,
            },
            headers=headers,
        )
        assert response.status_code == 201, response.text
        payload = response.json()
        minted = MintedSession(
            token=payload["token"],
            session_id=uuid.UUID(payload["session_id"]),
            tenant_id=uuid.UUID(payload["tenant_id"]),
            merchant_id=uuid.UUID(payload["merchant_id"]),
            buyer_ref=payload["buyer_ref"],
            actor_type=payload["actor_type"],
        )
        return TestClient(transport_client.app, headers=minted.auth_header), minted

    return _mint


# --------------------------------------------------------------------------- MCP helpers


@dataclass(frozen=True, slots=True)
class McpClient:
    """A driver for one established MCP session."""

    http: TestClient
    session_id: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Mcp-Session-Id": self.session_id}

    def tools(self) -> list[dict[str, Any]]:
        response = self.http.get("/v1/mcp/tools", headers=self.headers)
        assert response.status_code == 200, response.text
        return list(response.json()["tools"])

    def call(self, tool: str, **arguments: Any) -> Any:
        return self.http.post(
            "/v1/mcp/tools/call",
            headers={**self.headers, "Idempotency-Key": uuid.uuid4().hex},
            json={
                "tool": tool,
                "arguments": arguments,
                "nonce": uuid.uuid4().hex,
                "requested_at": datetime.now(tz=UTC).isoformat(),
            },
        )

    def content(self, tool: str, **arguments: Any) -> dict[str, Any]:
        response = self.call(tool, **arguments)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["isError"] is False, body
        return dict(body["content"])


def _token(http: TestClient) -> dict[str, Any]:
    response = http.post("/v1/mcp/token")
    assert response.status_code == 200, response.text
    return dict(response.json())


def _open(transport_client: TestClient, access_token: str) -> Any:
    return transport_client.post(
        "/v1/mcp/sessions",
        headers={
            "Authorization": f"Bearer {access_token}",
            "MCP-Protocol-Version": MCP_VERSION,
        },
    )


def _session(transport_client: TestClient, authed: TestClient) -> McpClient:
    """The whole handshake: exchange a platform session, open an MCP one."""
    granted = _token(authed)
    opened = _open(transport_client, granted["access_token"])
    assert opened.status_code == 201, opened.text
    return McpClient(http=transport_client, session_id=opened.json()["session_id"])


class TestAProtocolCallerIsConfinedToItsOwnBuyer:
    """The narrowing that survives every capability a protocol caller could hold.

    The tenant half of this is structural and tested elsewhere: row-level security scopes
    every read to the transaction's bound tenant, that tenant comes from the credential,
    and ``transaction_kernel.audit`` refuses the interaction's first evidence row if the
    transaction is bound to a different one. What is worth testing *here* is the half that
    is specific to these transports -- that a protocol session inherits its
    ``buyer_ref`` from the credential and cannot reach past it, even holding every
    capability the ceiling allows.
    """

    def test_one_buyers_session_cannot_see_another_buyers_checkout(
        self,
        transport_client: TestClient,
        mint_on: Callable[..., tuple[TestClient, MintedSession]],
    ) -> None:
        """404, not 403. A 403 would confirm the checkout exists, which turns this into an
        oracle for enumerating other buyers' checkouts."""
        first, _ = mint_on(buyer_ref="buyer-one")
        second, _ = mint_on(buyer_ref="buyer-two")
        sku = _first_sku(first)

        owner = _session(transport_client, first)
        cart_id = str(owner.content(ToolName.BASKET_CREATE.value)["basket_id"])
        owner.content(ToolName.BASKET_UPDATE.value, basket_id=cart_id, sku=sku, quantity=1)
        reserved = owner.content(ToolName.RESERVATION_REQUEST.value, basket_id=cart_id)

        intruder = _session(transport_client, second)
        refused = intruder.call(
            ToolName.CHECKOUT_SUBMIT_FOR_APPROVAL.value,
            checkout_id=str(reserved["checkout_id"]),
            version=1,
        )
        assert refused.status_code == 404, refused.text
        assert "not found" in refused.text.lower()

    def test_one_buyers_session_cannot_amend_another_buyers_basket(
        self,
        transport_client: TestClient,
        mint_on: Callable[..., tuple[TestClient, MintedSession]],
    ) -> None:
        first, _ = mint_on(buyer_ref="buyer-three")
        second, _ = mint_on(buyer_ref="buyer-four")
        sku = _first_sku(first)

        owner = _session(transport_client, first)
        cart_id = str(owner.content(ToolName.BASKET_CREATE.value)["basket_id"])

        intruder = _session(transport_client, second)
        refused = intruder.call(
            ToolName.BASKET_UPDATE.value, basket_id=cart_id, sku=sku, quantity=99
        )
        assert refused.status_code == 404, refused.text


# --------------------------------------------------------------------------- ACP helpers


def _acp(
    http: TestClient,
    *,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    secret: str = ACP_SECRET,
    audience: str = AUDIENCE,
    idempotency_key: str | None = None,
    omit_idempotency_key: bool = False,
    client_id: str = "acp-test-client",
) -> Any:
    """Send one correctly signed ACP request, or a deliberately wrong one.

    The signature is computed over the exact bytes sent, which is why the body is
    serialised once here and handed to both the signer and the client. A test that let
    ``requests`` re-serialise the body would be signing something the server never sees.
    """
    raw = b"" if body is None else json.dumps(body).encode("utf-8")
    timestamp = datetime.now(tz=UTC).isoformat()
    request_id = uuid.uuid4().hex
    key = "" if omit_idempotency_key or method == "GET" else idempotency_key or uuid.uuid4().hex
    content_type = "application/json" if raw else ""
    material = signing_string(
        method=method,
        path=path,
        query="",
        audience=audience,
        api_version=ACP_VERSION,
        timestamp=timestamp,
        request_id=request_id,
        idempotency_key=key,
        content_type=content_type,
        body=raw,
    )
    headers = {
        "Signature": sign(secret.encode("utf-8"), material),
        "Signature-Client": client_id,
        "Signature-Algorithm": "HMAC-SHA256",
        "Timestamp": timestamp,
        "Request-Id": request_id,
        "API-Version": ACP_VERSION,
    }
    if key:
        headers["Idempotency-Key"] = key
    if content_type:
        headers["Content-Type"] = content_type
    return http.request(method, path, content=raw, headers=headers)


def _first_sku(authed: TestClient) -> str:
    """Any available SKU from the seeded merchant's live catalogue."""
    response = authed.get("/v1/catalogue/search", params={"q": "milk", "limit": 5})
    assert response.status_code == 200, response.text
    available = [hit for hit in response.json()["hits"] if hit["is_available"]]
    assert available, "the seeded catalogue has nothing available to buy"
    return str(available[0]["sku"])


def _approve(authed: TestClient, checkout_id: str, version: int, card: dict[str, Any]) -> None:
    """The human's decision, on the trusted surface, against the exact bytes shown.

    Every protocol test that reaches admission goes through here, and that is the point:
    no ACP or MCP call in this file records an approval, because none of them can.
    """
    response = authed.post(
        f"/v1/checkouts/{checkout_id}/versions/{version}/approve",
        headers={"Idempotency-Key": uuid.uuid4().hex},
        json={
            "content_hash": card["content_hash"],
            "amount_minor": card["amount_minor"],
            "currency": card["currency"],
        },
    )
    assert response.status_code == 200, response.text


# ------------------------------------------------------------------- the surfaces exist


class TestAnUnconfiguredDeploymentHasNoTransport:
    """ADR 0003 D11's rule, applied to two more surfaces.

    A deployment that has issued no ACP credentials and named no MCP resource indicator
    does not have those surfaces. The distinction between 404 and 401 is the whole
    assertion: a 401 tells an anonymous caller that the endpoint is there and merely shut,
    which is a fact about this deployment it is not entitled to.
    """

    def test_the_mcp_token_endpoint_does_not_exist(self, client: TestClient) -> None:
        assert client.post("/v1/mcp/token").status_code == 404

    def test_the_mcp_resource_metadata_does_not_exist(self, client: TestClient) -> None:
        assert client.get("/.well-known/oauth-protected-resource").status_code == 404

    def test_the_acp_surface_does_not_exist(self, client: TestClient) -> None:
        response = client.post("/acp/checkout_sessions", json={})
        assert response.status_code == 404

    def test_configuring_half_of_acp_stops_the_process(self, seeded_tenant: SeededTenant) -> None:
        """An audience with no registered client answers to nobody, and the reverse has
        nothing to bind its credentials to. Refused at construction, not at first request."""
        with pytest.raises(ValueError, match="ACP_AUDIENCE and ACP_CLIENTS"):
            Settings(
                PROFILE="development",
                DATABASE_URL_APP=APP_URL,
                DATABASE_URL_KERNEL=KERNEL_URL,
                RAZORPAY_KEY_ID=TEST_KEY_ID,
                RAZORPAY_KEY_SECRET=TEST_KEY_SECRET,
                RAZORPAY_WEBHOOK_SECRET=TEST_WEBHOOK_SECRET,
                ACP_AUDIENCE=AUDIENCE,
            )

    def test_an_mcp_resource_without_a_token_secret_stops_the_process(self) -> None:
        """The surface mints signed bearer credentials, so it cannot be configured without
        the key that signs them. There is deliberately no generated fallback."""
        with pytest.raises(ValueError, match="MCP_TOKEN_SECRET"):
            Settings(
                PROFILE="development",
                DATABASE_URL_APP=APP_URL,
                DATABASE_URL_KERNEL=KERNEL_URL,
                RAZORPAY_KEY_ID=TEST_KEY_ID,
                RAZORPAY_KEY_SECRET=TEST_KEY_SECRET,
                RAZORPAY_WEBHOOK_SECRET=TEST_WEBHOOK_SECRET,
                MCP_RESOURCE=RESOURCE,
            )


# --------------------------------------------------------------------- the MCP credential


class TestTheAccessTokenIsNarrowerThanTheSessionThatMintedIt:
    def test_a_buyer_session_yields_no_consent_scope(
        self, mint_on: Callable[..., tuple[TestClient, MintedSession]]
    ) -> None:
        """A BUYER session holds ``checkout.approve``. The token minted from it does not.

        Not because the token endpoint filters it by name, but because the scopes are the
        session's capabilities intersected with ``PROTOCOL_CAPABILITIES``, which has never
        contained a consent capability.
        """
        authed, _ = mint_on("BUYER")
        granted = _token(authed)
        scopes = set(str(granted["scope"]).split())
        assert "mcp:checkout.approve" not in scopes
        assert "mcp:refund.request" not in scopes
        assert scopes == {f"mcp:{name}" for name in PROTOCOL_CAPABILITIES}

    def test_an_operator_session_yields_only_what_it_holds(
        self, mint_on: Callable[..., tuple[TestClient, MintedSession]]
    ) -> None:
        """An OPERATOR session may read a catalogue and an order and build nothing.

        This is the narrowing that later refuses a tool call, and it is worth seeing here
        as a set difference rather than only as an HTTP status.
        """
        authed, _ = mint_on("OPERATOR")
        scopes = set(str(_token(authed)["scope"]).split())
        assert scopes == {"mcp:catalogue.read", "mcp:order.read"}

    def test_a_token_opens_exactly_one_session(
        self,
        transport_client: TestClient,
        mint_on: Callable[..., tuple[TestClient, MintedSession]],
    ) -> None:
        """The ``jti`` is claimed through the platform's replay guard.

        A credential that can open unlimited sessions is one whose theft leaves no trace.
        Single use turns the thief's second attempt into a refusal that lands in the
        evidence chain.
        """
        authed, _ = mint_on()
        granted = _token(authed)
        assert _open(transport_client, granted["access_token"]).status_code == 201
        second = _open(transport_client, granted["access_token"])
        assert second.status_code >= 400
        assert "nonce" in second.text.lower() or "replay" in second.text.lower()

    def test_a_token_minted_for_another_resource_does_not_verify(
        self,
        seeded_tenant: SeededTenant,
        transport_client: TestClient,
        mint_on: Callable[..., tuple[TestClient, MintedSession]],
    ) -> None:
        """RFC 8707, and the failure specification 29.7 names as a required test.

        A token that is perfectly valid for some other resource server is presented here.
        Every claim is well formed and the signature verifies -- the two deployments share
        a key in this test precisely so that nothing but the audience can be doing the
        refusing.
        """
        from commerce_api.deps import RequestContext
        from commerce_api.services.mcp_transport import issue_access_token
        from commerce_domain import ActorType, AgentPrincipal

        authed, minted = mint_on()
        principal = AgentPrincipal(
            principal_id=f"session:{minted.session_id}",
            tenant_id=minted.tenant_id,
            actor_type=ActorType.BUYER,
            merchant_id=minted.merchant_id,
            buyer_ref=minted.buyer_ref,
            capabilities=PROTOCOL_CAPABILITIES,
        )
        elsewhere = issue_access_token(
            secret=MCP_SECRET.encode("utf-8"),
            resource="https://someone-elses.invalid/mcp",
            issuer="https://someone-elses.invalid",
            ctx=RequestContext(
                tenant_id=minted.tenant_id,
                merchant_id=minted.merchant_id,
                buyer_ref=minted.buyer_ref,
                principal=principal,
                correlation_id=uuid.uuid4(),
                session_id=minted.session_id,
                expires_at=datetime.now(tz=UTC),
            ),
            granted=PROTOCOL_CAPABILITIES,
            now=datetime.now(tz=UTC),
        )
        refused = _open(transport_client, elsewhere.token)
        assert refused.status_code >= 400, refused.text
        assert "not_verified" in refused.text

    def test_an_unsigned_token_does_not_verify(self, transport_client: TestClient) -> None:
        forged = _open(transport_client, "not.a.token")
        assert forged.status_code >= 400
        assert "mcp_access_token_not_verified" in forged.text


# ------------------------------------------------------------------------- the tool list


class TestTheToolListIsTheSessionsAllowlist:
    def test_a_full_session_sees_every_registered_tool_and_no_consent_tool(
        self,
        transport_client: TestClient,
        mint_on: Callable[..., tuple[TestClient, MintedSession]],
    ) -> None:
        authed, _ = mint_on()
        listed = {tool["name"] for tool in _session(transport_client, authed).tools()}
        assert listed == {name.value for name in ToolName}
        # Segment-wise, through the package's own screen. A substring test would flag
        # ``checkout.submit_approved``, and that tool is the point: submitting a version a
        # human already approved is the one thing this architecture exists to make safe.
        assert not [name for name in listed if public_name_offends(name)]
        assert not listed & FORBIDDEN_TOOL_NAMES

    def test_a_narrower_session_is_not_told_what_it_may_not_call(
        self,
        transport_client: TestClient,
        mint_on: Callable[..., tuple[TestClient, MintedSession]],
    ) -> None:
        """A tool the session may not call is absent, not marked forbidden.

        A list that enumerates what a caller may *not* have is a map of the platform for
        anybody who obtains a low-scoped token, and a model that cannot see a tool does not
        spend a turn discovering it is denied.
        """
        authed, _ = mint_on("OPERATOR")
        listed = {tool["name"] for tool in _session(transport_client, authed).tools()}
        readable = {
            name.value
            for name, spec in TOOLS.items()
            if spec.capability in {"catalogue.read", "order.read"}
        }
        assert listed == readable
        assert ToolName.BASKET_CREATE.value not in listed
        assert ToolName.CHECKOUT_SUBMIT_APPROVED.value not in listed


class TestACallerWithoutTheCapabilityIsRefused:
    """The more interesting half of the surface.

    Two different refusals, and they are different on purpose. A tool outside the session's
    fixed allowlist is refused by the allowlist; a tool that does not exist is refused by
    the registry. Neither reaches a service, and neither says anything about the checkout
    or the cart a wider session would have been able to touch.
    """

    def test_a_tool_outside_the_allowlist_is_refused_by_name(
        self,
        transport_client: TestClient,
        mint_on: Callable[..., tuple[TestClient, MintedSession]],
    ) -> None:
        authed, _ = mint_on("OPERATOR")
        session = _session(transport_client, authed)
        refused = session.call(ToolName.BASKET_CREATE.value)
        assert refused.status_code == 403, refused.text
        assert "tool_not_in_session_allowlist" in refused.text

    def test_a_tool_that_does_not_exist_stops_at_the_registry(
        self,
        transport_client: TestClient,
        mint_on: Callable[..., tuple[TestClient, MintedSession]],
    ) -> None:
        authed, _ = mint_on()
        session = _session(transport_client, authed)
        refused = session.call("checkout.approve")
        assert refused.status_code == 422, refused.text
        assert "tool_not_in_registry" in refused.text

    def test_a_submit_from_a_session_that_may_not_submit_is_refused(
        self,
        transport_client: TestClient,
        mint_on: Callable[..., tuple[TestClient, MintedSession]],
    ) -> None:
        """The one tool that reaches money, refused before it reaches anything.

        An operator session holds no ``checkout.submit_approved``, so the tool is not in
        its allowlist and the refusal happens at the gate -- not inside admission, and not
        after a lock has been taken.
        """
        authed, _ = mint_on("OPERATOR")
        session = _session(transport_client, authed)
        refused = session.call(
            ToolName.CHECKOUT_SUBMIT_APPROVED.value,
            checkout_id=str(uuid.uuid4()),
            version=1,
            content_hash="A" * 32,
        )
        assert refused.status_code == 403
        assert "tool_not_in_session_allowlist" in refused.text


# ------------------------------------------------------------------------ the MCP journey


class TestTheGovernedJourneyOverMcp:
    def test_discovery_construction_approval_and_admission(
        self,
        transport_client: TestClient,
        mint_on: Callable[..., tuple[TestClient, MintedSession]],
    ) -> None:
        """The whole demonstration, driven by tool calls except for the one human step.

        Search, build, freeze, *the buyer approves on the trusted surface*, submit. The
        approval is the only step that does not happen over MCP, and it cannot: the
        capability is not in the ceiling and the tool is not in the registry.
        """
        authed, _ = mint_on()
        session = _session(transport_client, authed)

        found = session.content(ToolName.CATALOGUE_SEARCH.value, query="milk", limit=3)
        assert found["hits"], found
        sku = str(found["hits"][0]["sku"])

        cart = session.content(ToolName.BASKET_CREATE.value)
        cart_id = str(cart["basket_id"])
        priced = session.content(
            ToolName.BASKET_UPDATE.value, basket_id=cart_id, sku=sku, quantity=2
        )
        assert priced["total_minor"] > 0
        assert isinstance(priced["total_minor"], int)

        reserved = session.content(ToolName.RESERVATION_REQUEST.value, basket_id=cart_id)
        assert reserved["approved"] is False
        checkout_id = str(reserved["checkout_id"])

        asked = session.content(
            ToolName.CHECKOUT_SUBMIT_FOR_APPROVAL.value, checkout_id=checkout_id, version=1
        )
        assert asked["approved"] is False
        assert asked["awaiting"] == "buyer_decision_on_trusted_surface"

        # The human step. Nothing above could have done this.
        _approve(
            authed,
            checkout_id,
            1,
            {
                "content_hash": reserved["content_hash"],
                "amount_minor": reserved["total_minor"],
                "currency": reserved["currency"],
            },
        )

        decision = session.content(
            ToolName.CHECKOUT_SUBMIT_APPROVED.value,
            checkout_id=checkout_id,
            version=1,
            content_hash=str(reserved["content_hash"]),
        )
        assert decision["allowed"] is True, decision
        assert decision["code"] == "OK"
        assert decision["grant_id"]

    def test_a_submit_without_an_approval_is_refused_and_stays_a_refusal(
        self,
        transport_client: TestClient,
        mint_on: Callable[..., tuple[TestClient, MintedSession]],
    ) -> None:
        """An MCP caller cannot approve, so a submit it has not been given is refused.

        409 rather than 200-with-a-denial because no admission ran: the platform declined
        to ask the kernel about a version carrying no live approval. The distinction is
        ADR 0003 D15's -- a *kernel* denial is a 200; this never reached the kernel.
        """
        authed, _ = mint_on()
        session = _session(transport_client, authed)
        sku = _first_sku(authed)
        cart_id = str(session.content(ToolName.BASKET_CREATE.value)["basket_id"])
        session.content(ToolName.BASKET_UPDATE.value, basket_id=cart_id, sku=sku, quantity=1)
        reserved = session.content(ToolName.RESERVATION_REQUEST.value, basket_id=cart_id)

        refused = session.call(
            ToolName.CHECKOUT_SUBMIT_APPROVED.value,
            checkout_id=str(reserved["checkout_id"]),
            version=1,
            content_hash=str(reserved["content_hash"]),
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "REAPPROVAL_REQUIRED"

    def test_a_stale_content_hash_is_refused_before_a_lock_is_taken(
        self,
        transport_client: TestClient,
        mint_on: Callable[..., tuple[TestClient, MintedSession]],
    ) -> None:
        """Echoing a hash the version does not carry means the caller has not re-read it."""
        authed, _ = mint_on()
        session = _session(transport_client, authed)
        sku = _first_sku(authed)
        cart_id = str(session.content(ToolName.BASKET_CREATE.value)["basket_id"])
        session.content(ToolName.BASKET_UPDATE.value, basket_id=cart_id, sku=sku, quantity=1)
        reserved = session.content(ToolName.RESERVATION_REQUEST.value, basket_id=cart_id)
        _approve(
            authed,
            str(reserved["checkout_id"]),
            1,
            {
                "content_hash": reserved["content_hash"],
                "amount_minor": reserved["total_minor"],
                "currency": reserved["currency"],
            },
        )
        refused = session.call(
            ToolName.CHECKOUT_SUBMIT_APPROVED.value,
            checkout_id=str(reserved["checkout_id"]),
            version=1,
            content_hash=sha256_b64url(b"not the bytes anybody approved"),
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "STALE_CHECKOUT"

    def test_a_proposal_names_no_amount_and_says_what_it_left_behind(
        self,
        transport_client: TestClient,
        mint_on: Callable[..., tuple[TestClient, MintedSession]],
    ) -> None:
        """Specification 29.4. A proposal produces a request for a human and nothing else,
        and the result says so rather than reporting a case this platform does not create."""
        authed, _ = mint_on()
        session = _session(transport_client, authed)
        result = session.content(
            ToolName.REFUND_PROPOSE.value,
            order_id=str(uuid.uuid4()),
            reason="the milk arrived warm",
        )
        assert result["changed_anything"] is False
        # No field of the result is a figure. The tool could not have received one either:
        # the registry refuses an argument named for money, which is asserted at import in
        # ``commerce_protocols.mcp.tools``.
        assert not set(result) & FORBIDDEN_ARGUMENT_NAMES
        # ``bool`` is excluded because it is an ``int`` in Python and the flags above are
        # the whole point of the result. What must not be here is a number.
        assert not [
            value
            for value in result.values()
            if isinstance(value, int) and not isinstance(value, bool)
        ]
        interaction = result["recorded_as"]
        recorded = authed.get(f"/v1/inspector/protocols/{interaction}")
        assert recorded.status_code == 200, recorded.text
        stages = [row["stage"] for row in recorded.json()["stages"]]
        assert "RECEIVED" in stages and "ANSWERED" in stages
        assert recorded.json()["chain_intact"] is True

    def test_a_card_sent_to_this_surface_never_reaches_the_chain(
        self,
        transport_client: TestClient,
        mint_on: Callable[..., tuple[TestClient, MintedSession]],
        seeded_tenant: SeededTenant,
        capi_admin_engine: Engine,
    ) -> None:
        """An instrument the caller should never send, and the platform never stores.

        Nothing here takes card data. `_complete` reads `content_hash` from the body and
        nothing else, admission decides money from the approval recorded on the trusted
        surface, and payment finishes at the provider's own gateway (specification 2.4).
        An ACP caller sending a PAN is a misconfigured integration or a probe.

        Both are worth recording as *having happened*, and neither is worth keeping. The
        RECEIVED row is written before any body-level check -- deliberately, so a stream
        opens with what arrived -- so before screening a refused body was stored as
        faithfully as an accepted one, and came back in cleartext from the Protocol
        Inspector to any session in the tenant.

        The permanence is the reason this is a test and not a lint rule: every row's hash
        covers its predecessor, so a card that reaches `audit_events` cannot be removed
        afterwards without breaking the stream from that point to the head. There is no
        cleanup for getting this wrong; there is only not getting it wrong.

        Read from the table rather than through the inspector, because the claim is about
        what was **committed**. An endpoint could be fixed while the row still held the
        card.
        """
        pan = "4111111111111111"
        cvc = "737"
        authed, _ = mint_on()

        def streams() -> set[str]:
            """Every protocol evidence stream this tenant holds, right now."""
            with capi_admin_engine.begin() as conn:
                conn.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(seeded_tenant.tenant_id)},
                )
                return {
                    str(one)
                    for one in conn.execute(
                        text(
                            "SELECT DISTINCT aggregate_id FROM audit_events "
                            "WHERE tenant_id = :tenant "
                            "AND aggregate_type = 'protocol_interaction'"
                        ),
                        {"tenant": seeded_tenant.tenant_id},
                    ).scalars()
                }

        # Only this completion's own stream is examined. Reading every protocol row in the
        # tenant makes the assertion agree with whatever a neighbour left behind, and this
        # test did exactly that once: it passed alone and failed in a full run against a
        # row an earlier reverted experiment had written. A leak test that can be tripped
        # by somebody else's data is a leak test nobody will trust the second time.
        before = streams()
        sku = _first_sku(authed)
        created = _acp(
            transport_client,
            method="POST",
            path="/acp/checkout_sessions",
            body={
                "items": [{"sku": sku, "quantity": 1}],
                "buyer": {"reference": BUYER_REF},
                "fulfillment": {"type": "delivery"},
            },
        )
        session = created.json()["session"]
        _acp(
            transport_client,
            method="POST",
            path=f"/acp/checkout_sessions/{session['id']}/complete",
            body={
                "checkout_version": 1,
                "content_hash": session["checkout"]["content_hash"],
                "total": {
                    "amount_minor": session["totals"]["total_minor"],
                    "currency": session["currency"],
                },
                "payment": {"type": "card", "number": pan, "cvc": cvc, "token": "tok_secret"},
            },
        )

        mine = sorted(streams() - before)
        assert mine, "the completion opened no evidence stream, which is its own defect"

        with capi_admin_engine.begin() as conn:
            conn.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(seeded_tenant.tenant_id)},
            )
            rows = (
                conn.execute(
                    text(
                        "SELECT payload FROM audit_events WHERE tenant_id = :tenant "
                        "AND aggregate_type = 'protocol_interaction' "
                        "AND aggregate_id = ANY(:ids)"
                    ),
                    {"tenant": seeded_tenant.tenant_id, "ids": mine},
                )
                .scalars()
                .all()
            )

        assert rows, "the stream opened and recorded nothing"
        chain = json.dumps([dict(row) for row in rows])

        assert pan not in chain
        assert cvc not in chain
        assert "tok_secret" not in chain
        # Not a prefix either. Eight characters is what `fingerprint`'s preview would have
        # kept, and eight digits of this card is half of it.
        for length in range(4, len(pan)):
            assert pan[:length] not in chain, (
                f"the first {length} digits of the card are in the audit chain"
            )
        # The evidence that an instrument arrived survives, which is the half worth keeping.
        assert '"withheld": true' in chain.lower()

    def test_a_refusal_is_still_in_the_evidence_chain(
        self,
        transport_client: TestClient,
        mint_on: Callable[..., tuple[TestClient, MintedSession]],
        seeded_tenant: SeededTenant,
        capi_admin_engine: Engine,
    ) -> None:
        """Specification 13.3: a reviewer must be able to see which check refused.

        The refusal rolls back everything it touched except its own evidence, which is
        committed on the way out. Without that, a refused request would be a request that
        never happened as far as the audit trail is concerned -- and "which check refused
        this" would be answerable only by re-running it.

        Read straight from ``audit_events`` rather than through an endpoint, because the
        claim is about what was *committed*, and an endpoint that read from the same
        rolled-back transaction would agree with the bug.
        """
        authed, _ = mint_on()
        session = _session(transport_client, authed)
        assert session.call("checkout.approve").status_code == 422

        with capi_admin_engine.begin() as conn:
            conn.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(seeded_tenant.tenant_id)},
            )
            rejected = conn.execute(
                text(
                    "SELECT payload FROM audit_events "
                    "WHERE tenant_id = :tenant AND event_type = 'protocol.rejected'"
                ),
                {"tenant": seeded_tenant.tenant_id},
            ).all()
        assert rejected, "the refusal left no evidence row"
        reasons = {row.payload["reason"] for row in rejected}
        assert "tool_not_in_registry" in reasons
        assert {row.payload["failed_at"] for row in rejected} == {"VALIDATED"}


# --------------------------------------------------------------------------- ACP


class TestTheAcpSurface:
    def test_a_session_reaches_a_frozen_version_and_asks_for_a_human(
        self,
        transport_client: TestClient,
        mint_on: Callable[..., tuple[TestClient, MintedSession]],
    ) -> None:
        """One create carrying items, buyer and fulfilment freezes version 1.

        The response says ``READY_FOR_PAYMENT`` and carries a message saying, in words an
        external platform can render, that a human on a surface it cannot reach has to
        agree before anything is paid.
        """
        authed, _ = mint_on()
        sku = _first_sku(authed)
        created = _acp(
            transport_client,
            method="POST",
            path="/acp/checkout_sessions",
            body={
                "items": [{"sku": sku, "quantity": 1}],
                "buyer": {"reference": BUYER_REF},
                "fulfillment": {"type": "delivery"},
            },
        )
        assert created.status_code == 200, created.text
        session = created.json()["session"]
        assert session["status"] == "READY_FOR_PAYMENT"
        assert session["checkout"]["version"] == 1
        assert session["checkout"]["content_hash"]
        assert session["approval"] is None
        assert session["messages"][0]["code"] == "awaiting_buyer_approval"

    def test_a_completion_without_a_recorded_approval_is_refused(
        self,
        transport_client: TestClient,
        mint_on: Callable[..., tuple[TestClient, MintedSession]],
    ) -> None:
        """The one refusal this whole surface exists for.

        An ACP completion carries payment credentials the external platform collected from
        its own user. Their presence is not this platform's buyer having agreed to
        anything, and the refusal is about authority rather than about state.
        """
        authed, _ = mint_on()
        sku = _first_sku(authed)
        created = _acp(
            transport_client,
            method="POST",
            path="/acp/checkout_sessions",
            body={
                "items": [{"sku": sku, "quantity": 1}],
                "buyer": {"reference": BUYER_REF},
                "fulfillment": {"type": "delivery"},
            },
        )
        session = created.json()["session"]
        refused = _acp(
            transport_client,
            method="POST",
            path=f"/acp/checkout_sessions/{session['id']}/complete",
            body={
                "checkout_version": 1,
                "content_hash": session["checkout"]["content_hash"],
                "total": {
                    "amount_minor": session["totals"]["total_minor"],
                    "currency": session["currency"],
                },
                "payment": {"token": "tok_from_the_external_platform"},
            },
        )
        assert refused.status_code == 403, refused.text
        assert "acp_completion_without_recorded_approval" in refused.text

    def test_a_completion_after_a_human_approval_reaches_admission(
        self,
        transport_client: TestClient,
        mint_on: Callable[..., tuple[TestClient, MintedSession]],
    ) -> None:
        """The same request, after the buyer has decided on the trusted surface.

        HTTP 200 with the kernel's decision beside the session, allowed or denied
        (ADR 0003 D15). Here it is allowed, and the grant it names is the one the worker
        will spend.
        """
        authed, _ = mint_on()
        sku = _first_sku(authed)
        created = _acp(
            transport_client,
            method="POST",
            path="/acp/checkout_sessions",
            body={
                "items": [{"sku": sku, "quantity": 2}],
                "buyer": {"reference": BUYER_REF},
                "fulfillment": {"type": "delivery"},
            },
        )
        session = created.json()["session"]
        _approve(
            authed,
            session["checkout"]["checkout_id"],
            1,
            {
                "content_hash": session["checkout"]["content_hash"],
                "amount_minor": session["totals"]["total_minor"],
                "currency": session["currency"],
            },
        )
        completed = _acp(
            transport_client,
            method="POST",
            path=f"/acp/checkout_sessions/{session['id']}/complete",
            body={
                "checkout_version": 1,
                "content_hash": session["checkout"]["content_hash"],
                "total": {
                    "amount_minor": session["totals"]["total_minor"],
                    "currency": session["currency"],
                },
            },
        )
        assert completed.status_code == 200, completed.text
        body = completed.json()
        decision = body["decision"]
        assert decision["allowed"] is True, decision
        assert decision["grant_id"]
        # The session the answer carries is the session as it is *now*, not as it was
        # before admission ran. Reporting ``READY_FOR_PAYMENT`` here would invite the
        # external buyer to complete a session whose payment is already in flight.
        assert body["session"]["status"] == "IN_PROGRESS"
        assert body["session"]["payment"]["state"] == "CREATED"
        assert body["session"]["payment"]["terminal"] is False
        assert body["session"]["messages"][0]["code"] == "payment_in_flight"

    def test_a_signature_that_does_not_verify_is_refused(
        self, transport_client: TestClient
    ) -> None:
        refused = _acp(
            transport_client,
            method="POST",
            path="/acp/checkout_sessions",
            body={"items": []},
            secret="the wrong secret",  # noqa: S106 - the point of the test
        )
        assert refused.status_code == 403, refused.text
        assert "acp_signature_did_not_verify" in refused.text

    def test_a_signature_for_another_audience_is_refused(
        self, transport_client: TestClient
    ) -> None:
        """Audience binding, over the signed bytes.

        The client signs correctly, with its own secret, over a different audience string.
        This deployment builds its signing string with *its* audience and gets a different
        HMAC, so the signature does not verify -- which is what makes a credential minted
        for the sandbox inert against production.
        """
        refused = _acp(
            transport_client,
            method="POST",
            path="/acp/checkout_sessions",
            body={"items": []},
            audience="https://somewhere-else.invalid/acp",
        )
        assert refused.status_code == 403, refused.text
        assert "acp_signature_did_not_verify" in refused.text

    def test_an_unregistered_client_is_refused(self, transport_client: TestClient) -> None:
        refused = _acp(
            transport_client,
            method="POST",
            path="/acp/checkout_sessions",
            body={"items": []},
            client_id="a-client-nobody-issued",
        )
        assert refused.status_code == 403, refused.text

    def test_a_replayed_request_id_is_refused(
        self,
        transport_client: TestClient,
        mint_on: Callable[..., tuple[TestClient, MintedSession]],
    ) -> None:
        """One nonce, one request. The second presentation is refused outright rather than
        replayed: nothing was done under it, so there is no result to return."""
        authed, _ = mint_on()
        sku = _first_sku(authed)
        body = {
            "items": [{"sku": sku, "quantity": 1}],
            "buyer": {"reference": BUYER_REF},
            "fulfillment": {"type": "delivery"},
        }
        raw = json.dumps(body).encode("utf-8")
        timestamp = datetime.now(tz=UTC).isoformat()
        request_id = uuid.uuid4().hex
        key = uuid.uuid4().hex
        material = signing_string(
            method="POST",
            path="/acp/checkout_sessions",
            query="",
            audience=AUDIENCE,
            api_version=ACP_VERSION,
            timestamp=timestamp,
            request_id=request_id,
            idempotency_key=key,
            content_type="application/json",
            body=raw,
        )
        headers = {
            "Signature": sign(ACP_SECRET.encode("utf-8"), material),
            "Signature-Client": "acp-test-client",
            "Timestamp": timestamp,
            "Request-Id": request_id,
            "Idempotency-Key": key,
            "API-Version": ACP_VERSION,
            "Content-Type": "application/json",
        }
        first = transport_client.post("/acp/checkout_sessions", content=raw, headers=headers)
        assert first.status_code == 200, first.text
        second = transport_client.post("/acp/checkout_sessions", content=raw, headers=headers)
        assert second.status_code == 422, second.text

    def test_a_mutation_without_an_idempotency_key_is_refused(
        self, transport_client: TestClient
    ) -> None:
        refused = _acp(
            transport_client,
            method="POST",
            path="/acp/checkout_sessions",
            body={"items": []},
            omit_idempotency_key=True,
        )
        assert refused.status_code == 422, refused.text
        assert "acp_idempotency_key_required_on_mutation" in refused.text


class TestAnAcpClientCannotExceedItsRegistryEntry:
    def test_a_registry_entry_naming_a_consent_capability_grants_nothing(
        self, seeded_tenant: SeededTenant
    ) -> None:
        """A misconfiguration nobody has noticed yet grants precisely nothing.

        Dropped silently and totally, at configuration time as well as inside
        ``principal_for``, so an operator comparing what they wrote against what the client
        holds is comparing two sets that were narrowed the same way.
        """
        settings = _settings(
            seeded_tenant, granted=["checkout.approve", "refund.request", "catalogue.read"]
        )
        entry = settings.acp_registry().clients["acp-test-client"]
        assert entry.granted == frozenset({"catalogue.read"})

    def test_a_client_without_submit_approved_completes_nothing(
        self,
        seeded_tenant: SeededTenant,
        mint_on: Callable[..., tuple[TestClient, MintedSession]],
        transport_client: TestClient,
    ) -> None:
        """The capability check, on the surface that most needs it.

        This client may discover, build and freeze. It may not hand a version to
        admission, and the refusal happens in ``submit_checkout`` -- the same line the
        trusted surface would hit -- rather than in anything the transport wrote.
        """
        authed, _ = mint_on()
        sku = _first_sku(authed)
        narrowed = create_app(
            _settings(
                seeded_tenant,
                granted=["catalogue.read", "basket.write", "checkout.create", "order.read"],
            )
        )
        with TestClient(narrowed) as http:
            created = _acp(
                http,
                method="POST",
                path="/acp/checkout_sessions",
                body={
                    "items": [{"sku": sku, "quantity": 1}],
                    "buyer": {"reference": BUYER_REF},
                    "fulfillment": {"type": "delivery"},
                },
            )
            assert created.status_code == 200, created.text
            session = created.json()["session"]
            _approve(
                authed,
                session["checkout"]["checkout_id"],
                1,
                {
                    "content_hash": session["checkout"]["content_hash"],
                    "amount_minor": session["totals"]["total_minor"],
                    "currency": session["currency"],
                },
            )
            refused = _acp(
                http,
                method="POST",
                path=f"/acp/checkout_sessions/{session['id']}/complete",
                body={
                    "checkout_version": 1,
                    "content_hash": session["checkout"]["content_hash"],
                    "total": {
                        "amount_minor": session["totals"]["total_minor"],
                        "currency": session["currency"],
                    },
                },
            )
        assert refused.status_code == 403, refused.text
        assert "checkout.submit_approved" in refused.text
