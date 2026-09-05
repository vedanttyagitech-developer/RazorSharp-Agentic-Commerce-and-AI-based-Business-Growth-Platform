"""The protocol surface over HTTP: profiles, the pinned matrix, and the inspector.

The assertions worth stating up front, because they are the ones a reviewer would want to
check by hand:

The published profiles contain **no private key material**, asserted over the serialised
response bytes rather than over its structure. A structural check would pass a document that
carried a private component under an unexpected member name, and publishing a signing key to
a well-known URL is not a mistake anybody recovers from by rotating.

The merchant and the platform publish **different keys**. "The merchant signed this
checkout" and "the platform signed this receipt" are different claims, and they stop being
different the moment one key can produce both signatures. This was a real bug in the first
version of the router, caught by fetching both documents and comparing them.

The matrix carries its **disclaimers**, so a reviewer reading the API rather than the
specification is still told that implementing UCP is not availability inside Gemini, and
that an ACP-compatible interface is not a live ChatGPT Instant Checkout integration.

And there is **no endpoint here that moves money**. The last test in the file asserts that
over the app's own route table, because the property is an absence and an absence is exactly
what a behavioural test cannot demonstrate.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from conftest import TEST_SCENARIO_KEY, MintedSession, SeededTenant

WELL_KNOWN_MERCHANT = "/.well-known/ucp/business-profile"
WELL_KNOWN_PLATFORM = "/.well-known/ucp/platform-profile"


class TestPublishedProfiles:
    def test_the_merchant_profile_is_public_and_names_the_pinned_version(
        self, client: TestClient
    ) -> None:
        """Unauthenticated by design: a well-known location nobody can fetch is not one."""
        response = client.get(WELL_KNOWN_MERCHANT)
        assert response.status_code == 200, response.text

        body = response.json()
        assert body["protocol"] == "UCP"
        assert body["protocol_version"] == "2026-08-25"
        assert body["profile_version"] >= 1
        assert body["jwks"]["keys"], "a profile with no keys verifies nothing"

    def test_no_profile_ever_publishes_private_key_material(self, client: TestClient) -> None:
        """Asserted over the raw bytes. The one failure that cannot be undone by rotating."""
        for path in (WELL_KNOWN_MERCHANT, WELL_KNOWN_PLATFORM):
            raw = client.get(path).text
            assert '"d"' not in raw, f"{path} leaked a private component"
            assert "PRIVATE" not in raw.upper()
            for key in json.loads(raw)["jwks"]["keys"]:
                assert set(key) <= {"kty", "crv", "x", "y", "kid", "alg", "use"}

    def test_the_merchant_and_the_platform_publish_different_keys(self, client: TestClient) -> None:
        """Two claims that must stay distinguishable, so two key sets.

        A shared key would make "the merchant authorised this checkout" and "the platform
        issued this receipt" the same statement, and the AP2 verification sequence's step 3
        -- resolve the correct key by kid -- would be resolving nothing.
        """
        merchant = {k["kid"] for k in client.get(WELL_KNOWN_MERCHANT).json()["jwks"]["keys"]}
        platform = {k["kid"] for k in client.get(WELL_KNOWN_PLATFORM).json()["jwks"]["keys"]}

        assert merchant, "the merchant profile published no key"
        assert platform, "the platform profile published no key"
        assert merchant.isdisjoint(platform)

    def test_a_profile_served_twice_publishes_the_same_keys(self, client: TestClient) -> None:
        """A JWK Set that changed between two fetches would read as tampering."""
        first = client.get(WELL_KNOWN_MERCHANT).json()["jwks"]
        second = client.get(WELL_KNOWN_MERCHANT).json()["jwks"]
        assert first == second

    def test_an_ephemeral_key_says_so_rather_than_letting_a_peer_assume_otherwise(
        self, client: TestClient
    ) -> None:
        """Honesty about a demo limitation, in the document a counterparty actually reads.

        With no key configured the process mints one, which is fine for a demonstration and
        disastrous if a peer caches it expecting it to survive a restart.
        """
        body = client.get(WELL_KNOWN_MERCHANT).json()
        assert body["ephemeral_keys"] is True

    def test_the_profile_advertises_escalation_and_never_headless_completion(
        self, client: TestClient
    ) -> None:
        """A profile is a promise. Specification 14.3 says we cannot complete headlessly."""
        capabilities = client.get(WELL_KNOWN_MERCHANT).json()["capabilities"]
        assert "checkout.complete.requires_escalation" in capabilities
        assert "checkout.complete" not in capabilities
        assert not any("payment.execute" in c for c in capabilities)
        assert not any("refund.execute" in c for c in capabilities)


class TestProtocolMatrix:
    def test_every_pinned_protocol_is_reported_with_its_claim_boundary(
        self, client: TestClient
    ) -> None:
        response = client.get("/v1/protocols")
        assert response.status_code == 200, response.text

        pins = {row["protocol"]: row for row in response.json()["pins"]}
        assert set(pins) == {"UCP", "AP2", "ACP", "MCP"}
        assert pins["AP2"]["source_ref"] == "b4587ac1d055888a73b4b21750973cffba961793"
        assert pins["UCP"]["version"] == "2026-08-25"
        assert pins["ACP"]["version"] == "2026-04-17"

    def test_the_disclaimers_travel_with_the_data(self, client: TestClient) -> None:
        """Specification 13.2 and 16.2, in the API rather than only in a status table."""
        pins = {row["protocol"]: row for row in client.get("/v1/protocols").json()["pins"]}
        assert "Gemini" in pins["UCP"]["disclaimer"]
        assert "ChatGPT Instant Checkout" in pins["ACP"]["disclaimer"]
        assert "ES256" in pins["AP2"]["disclaimer"]

    def test_acp_and_mcp_are_not_claimed_as_locally_conformant(self, client: TestClient) -> None:
        """Compatible interface is a weaker claim than local conformance, and stays weaker."""
        pins = {row["protocol"]: row for row in client.get("/v1/protocols").json()["pins"]}
        assert pins["ACP"]["claim_boundary"] == "COMPATIBLE_INTERFACE"
        assert pins["MCP"]["claim_boundary"] == "COMPATIBLE_INTERFACE"

    def test_the_conformance_report_is_behind_the_scenario_key(self, client: TestClient) -> None:
        """An operator's view of what this build has proved, not a public statement."""
        assert client.get("/v1/protocols/conformance").status_code == 401

        response = client.get(
            "/v1/protocols/conformance", headers={"X-Scenario-Key": TEST_SCENARIO_KEY}
        )
        assert response.status_code == 200, response.text
        assert len(response.json()["must_never_claim"]) == 4


@pytest.mark.db
class TestProtocolInspector:
    def test_an_unknown_interaction_is_a_404_not_a_403(self, auth_client: TestClient) -> None:
        """A 403 would confirm the identifier names something real in another tenant."""
        response = auth_client.get(f"/v1/inspector/protocols/{uuid.uuid4()}")
        assert response.status_code == 404, response.text

    def test_the_inspector_requires_a_session(self, client: TestClient) -> None:
        response = client.get(f"/v1/inspector/protocols/{uuid.uuid4()}")
        assert response.status_code == 401, response.text

    def test_a_recorded_interaction_is_reconstructed_in_order_with_its_chain_verified(
        self,
        auth_client: TestClient,
        demo_session: MintedSession,
        seeded_tenant: SeededTenant,
        capi_kernel_engine: object,
    ) -> None:
        """Specification 28 and 13.3: reconstruct what was asked and what was done.

        The evidence is written directly through the protocol core -- the same call path a
        live adapter uses -- so what the inspector renders is the real record rather than a
        fixture shaped to look like one.
        """
        from commerce_protocols.core import (
            PINS,
            EvidenceStage,
            Protocol,
            fingerprint,
            open_interaction,
            principal_for,
        )
        from commerce_protocols.core.identity import AuthenticatedCaller
        from platform_db import set_tenant
        from sqlalchemy import Engine
        from sqlalchemy.orm import Session

        assert isinstance(capi_kernel_engine, Engine)
        caller = AuthenticatedCaller(
            protocol=Protocol.ACP,
            client_id="external-buyer-1",
            tenant_id=seeded_tenant.tenant_id,
            merchant_id=seeded_tenant.merchant_id,
            authenticated_by="http-message-signature",
            correlation_id=demo_session.session_id,
        )
        interaction = open_interaction(
            pin=PINS[Protocol.ACP],
            tenant_id=seeded_tenant.tenant_id,
            principal=principal_for(caller),
            correlation_id=demo_session.session_id,
        )

        session = Session(capi_kernel_engine, expire_on_commit=False)
        with session.begin():
            set_tenant(session, seeded_tenant.tenant_id)
            interaction.record_received(
                session,
                endpoint="POST /v1/acp/checkout_sessions",
                announced_version="2026-04-17",
                body={"items": [{"id": "TEA-BEV-001", "quantity": 2}]},
                headers_seen={"content-type": "application/json"},
                credential=fingerprint("Signature keyId=external-buyer-1,signature=abc"),
            )
            interaction.record(session, EvidenceStage.AUTHENTICATED)
            interaction.record(session, EvidenceStage.VALIDATED)
        session.close()

        response = auth_client.get(f"/v1/inspector/protocols/{interaction.interaction_id}")
        assert response.status_code == 200, response.text

        body = response.json()
        assert body["protocol"] == "ACP"
        assert body["protocol_version"] == "2026-04-17"
        assert body["chain_intact"] is True
        assert [stage["stage"] for stage in body["stages"]] == [
            "RECEIVED",
            "AUTHENTICATED",
            "VALIDATED",
        ]

        rendered = json.dumps(body)
        assert "signature=abc" not in rendered, (
            "the inspector must never render a credential; the evidence chain stores a "
            "fingerprint, so there is nothing here to leak"
        )
        assert body["stages"][0]["payload"]["request"]["items"][0]["id"] == "TEA-BEV-001"


class TestNoMoneyPathOnTheProtocolSurface:
    def test_no_protocol_route_admits_approves_or_executes_anything(self, api_app: object) -> None:
        """An absence, asserted over the route table rather than over behaviour.

        A protocol caller's money path runs through the same trusted approval and the same
        kernel admission as every other surface. If a route ever appeared here that could
        short-circuit that, this test is what would notice.

        The walk is recursive because this FastAPI version wraps each included router in an
        ``_IncludedRouter`` node that exposes its contents as ``original_router`` rather
        than flattening them into ``app.routes``. Iterating the top level alone finds no
        ``APIRoute`` at all, so the guard below asserts the walk actually reached the route
        table -- a test whose subject is an absence must never be allowed to pass because it
        looked at nothing.
        """
        from fastapi import FastAPI
        from fastapi.routing import APIRoute

        assert isinstance(api_app, FastAPI)

        def walk(node: object) -> list[APIRoute]:
            found: list[APIRoute] = []
            for route in getattr(node, "routes", None) or []:
                if isinstance(route, APIRoute):
                    found.append(route)
                else:
                    nested = getattr(route, "original_router", None)
                    if nested is not None:
                        found.extend(walk(nested))
            return found

        every_route = walk(api_app.router)
        assert len(every_route) > 30, (
            f"only {len(every_route)} routes were reached; the walk is not finding them, "
            "so the assertions below would pass without checking anything"
        )

        protocol_routes = [route for route in every_route if "protocols" in (route.tags or [])]
        assert protocol_routes, "the protocols router was not registered"

        for route in protocol_routes:
            assert route.methods <= {"GET", "HEAD"}, (
                f"{route.path} accepts {route.methods}; the protocol surface is read-only "
                "and every mutation belongs to the trusted surface or the kernel"
            )
            for forbidden in ("approve", "pay", "charge", "refund", "revoke", "execute"):
                assert forbidden not in route.path.lower(), (
                    f"{route.path} names {forbidden!r}; specification 17.3's rule is that "
                    "such an endpoint must not exist rather than be guarded"
                )
