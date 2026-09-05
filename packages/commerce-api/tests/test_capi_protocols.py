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
from commerce_api.app import create_app
from commerce_api.settings import Settings
from commerce_protocols.ap2.signing import KeyRing, verify_compact
from commerce_protocols.core.errors import SignatureRejected
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

    def test_a_profile_published_by_a_new_process_publishes_the_same_keys(
        self, settings_for_tests: Settings
    ) -> None:
        """The restart property, at the smallest scale that can express it.

        Two apps built from the same configuration are two processes as far as the signing
        key is concerned: nothing is carried over between them but the environment. Under
        the old ``ec.generate_private_key`` fallback this assertion failed -- each app
        minted its own key, so the JWK Set moved and evidence signed by the first was
        unverifiable by the second. It is the unit-level statement of the same fact
        ``TestSignedEvidenceSurvivesARestart`` proves against a real signature.
        """
        first = TestClient(create_app(settings_for_tests))
        second = TestClient(create_app(settings_for_tests))
        assert (
            first.get(WELL_KNOWN_MERCHANT).json()["jwks"]
            == (second.get(WELL_KNOWN_MERCHANT).json()["jwks"])
        )

    def test_a_deployment_with_no_configured_key_publishes_no_profile_at_all(
        self, settings_for_tests: Settings
    ) -> None:
        """404, not an empty JWK Set and not a minted one. ADR 0003 D11.

        A deployment that holds no signing key signs nothing, so it has no profile. The
        two answers this rejects are both worse: an empty key set describes a surface that
        is present and broken, and a minted key set describes one that works until the
        next restart and then silently stops attributing its own past signatures.
        """
        unconfigured = settings_for_tests.model_copy(
            update={"ucp_merchant_signing_jwk": None, "ucp_platform_signing_jwk": None}
        )
        client = TestClient(create_app(unconfigured))

        for path in (WELL_KNOWN_MERCHANT, WELL_KNOWN_PLATFORM):
            assert client.get(path).status_code == 404, path
        assert client.get("/v1/protocols").json()["signing_keys_configured"] is False

    def test_the_profile_advertises_escalation_and_never_headless_completion(
        self, client: TestClient
    ) -> None:
        """A profile is a promise. Specification 14.3 says we cannot complete headlessly."""
        capabilities = client.get(WELL_KNOWN_MERCHANT).json()["capabilities"]
        assert "checkout.complete.requires_escalation" in capabilities
        assert "checkout.complete" not in capabilities
        assert not any("payment.execute" in c for c in capabilities)
        assert not any("refund.execute" in c for c in capabilities)


class TestSignedEvidenceSurvivesARestart:
    """The property the Protocol Inspector's whole claim rests on.

    "This interaction can be reconstructed and verified afterwards" is false if
    *afterwards* is bounded by the process's lifetime. A restart is the ordinary case --
    a rollout, a crash loop, a pod rescheduled onto another node -- so evidence that stops
    verifying across one is not evidence at all, and worse than useless: a signature that
    cannot be attributed is indistinguishable from a forged one by anybody holding it.

    A "restart" here is a second ``Settings`` built from the same environment mapping,
    which is exactly what the next process would do. Nothing is carried over in memory.
    """

    def test_a_signature_made_before_a_restart_still_verifies_after_one(
        self, settings_for_tests: Settings
    ) -> None:
        """Sign with the process that is about to die; verify with the one that replaces it.

        ``verify_compact`` is the same verifier every protocol path funnels through --
        the pinned-``ES256``, resolve-by-``kid``, one-element-``allowed_algs`` one. Nothing
        is relaxed to make this pass; the only thing that changed is where the key came
        from.
        """
        environment = _environment(settings_for_tests)

        before = Settings(**environment).ucp_signers()
        assert before is not None
        evidence = before.merchant.sign({"typ": "test+jws"}, b'{"claim":"the merchant signed"}')

        # The restart. A fresh Settings, a fresh signer, a fresh ring -- sharing nothing
        # with the objects above except the two configured JWKs.
        after = Settings(**environment).ucp_signers()
        assert after is not None

        payload = verify_compact(evidence, KeyRing.of(after.merchant))
        assert payload == {"claim": "the merchant signed"}

    def test_the_key_id_is_stable_across_a_restart_so_the_ring_can_resolve_it(
        self, settings_for_tests: Settings
    ) -> None:
        """Attribution needs the ``kid`` to survive, not only the key bytes.

        A ring resolves by ``kid`` before it checks anything, so a restart that kept the
        key material but renamed it would refuse old evidence at ``unknown_kid`` -- which
        reads, to an operator, exactly like a signature over the wrong key.
        """
        environment = _environment(settings_for_tests)
        before = Settings(**environment).ucp_signers()
        after = Settings(**environment).ucp_signers()
        assert before is not None and after is not None

        assert before.merchant.kid == after.merchant.kid
        assert before.platform.kid == after.platform.kid
        assert before.merchant.public_jwk() == after.merchant.public_jwk()

    def test_a_restart_that_swapped_the_key_refuses_the_old_evidence_rather_than_accepting_it(
        self, settings_for_tests: Settings
    ) -> None:
        """The negative control, without which the two tests above prove nothing.

        If ``verify_compact`` accepted anything put in front of it, the assertions above
        would pass against a key that had in fact changed. So: same ``kid``, different key
        material, and the verifier must still refuse. This is what a genuinely rotated-out
        key looks like, and refusing it loudly is correct -- the failure mode being fixed
        is refusing *unrotated* evidence, not accepting rotated evidence.
        """
        environment = _environment(settings_for_tests)
        signers = Settings(**environment).ucp_signers()
        assert signers is not None
        evidence = signers.merchant.sign({"typ": "test+jws"}, b'{"claim":"the merchant signed"}')

        impostor = json.loads(environment["UCP_PLATFORM_SIGNING_JWK"])
        impostor["kid"] = signers.merchant.kid
        swapped = Settings(
            **{**environment, "UCP_MERCHANT_SIGNING_JWK": json.dumps(impostor)}
        ).ucp_signers()
        assert swapped is not None

        with pytest.raises(SignatureRejected):
            verify_compact(evidence, KeyRing.of(swapped.merchant))


def _environment(settings: Settings) -> dict[str, str]:
    """The configuration a restarted process would read, as a plain mapping.

    Built from the test settings rather than typed out again, so these tests cannot drift
    away from the keys the app fixture actually serves.
    """
    assert settings.ucp_merchant_signing_jwk is not None
    assert settings.ucp_platform_signing_jwk is not None
    return {
        "PROFILE": settings.profile.value,
        "DATABASE_URL_APP": settings.database_url_app,
        "DATABASE_URL_KERNEL": settings.database_url_kernel,
        "RAZORPAY_KEY_ID": settings.razorpay_key_id,
        "RAZORPAY_KEY_SECRET": settings.razorpay_key_secret.get_secret_value(),
        "RAZORPAY_WEBHOOK_SECRET": settings.razorpay_webhook_secret.get_secret_value(),
        "UCP_MERCHANT_SIGNING_JWK": settings.ucp_merchant_signing_jwk.get_secret_value(),
        "UCP_PLATFORM_SIGNING_JWK": settings.ucp_platform_signing_jwk.get_secret_value(),
    }


class TestSigningKeyConfiguration:
    """Startup refusals. Every one of these would otherwise be a runtime surprise."""

    def test_one_key_without_the_other_is_refused_at_startup(
        self, settings_for_tests: Settings
    ) -> None:
        """Configured together or not at all, matching ACP_AUDIENCE and ACP_CLIENTS."""
        environment = _environment(settings_for_tests)
        del environment["UCP_PLATFORM_SIGNING_JWK"]

        with pytest.raises(ValueError, match="UCP_MERCHANT_SIGNING_JWK and"):
            Settings(**environment)

    def test_the_merchant_and_the_platform_may_not_share_a_key_id(
        self, settings_for_tests: Settings
    ) -> None:
        """One kid covering both roles collapses two different claims into one.

        Refused here because it can only be refused here: ``KeyRing.of`` catches a
        collision inside one ring, and these are two separate rings.
        """
        environment = _environment(settings_for_tests)
        platform = json.loads(environment["UCP_PLATFORM_SIGNING_JWK"])
        platform["kid"] = json.loads(environment["UCP_MERCHANT_SIGNING_JWK"])["kid"]
        environment["UCP_PLATFORM_SIGNING_JWK"] = json.dumps(platform)

        with pytest.raises(ValueError, match="both carry kid"):
            Settings(**environment)

    def test_a_public_key_is_refused_because_it_can_sign_nothing(
        self, settings_for_tests: Settings
    ) -> None:
        """And the message names the variable, because a deployment configures two."""
        environment = _environment(settings_for_tests)
        public_half = json.loads(environment["UCP_MERCHANT_SIGNING_JWK"])
        del public_half["d"]
        environment["UCP_MERCHANT_SIGNING_JWK"] = json.dumps(public_half)

        with pytest.raises(ValueError, match="UCP_MERCHANT_SIGNING_JWK is not a usable"):
            Settings(**environment)

    def test_a_malformed_jwk_stops_the_process_rather_than_the_first_fetch(
        self, settings_for_tests: Settings
    ) -> None:
        """A key that will not load must not be discovered by a counterparty."""
        environment = _environment(settings_for_tests)
        environment["UCP_MERCHANT_SIGNING_JWK"] = "not json at all"

        with pytest.raises(ValueError, match="UCP_MERCHANT_SIGNING_JWK is not a well-formed"):
            Settings(**environment)

    def test_a_refusal_never_quotes_the_key_material_it_was_handed(
        self, settings_for_tests: Settings
    ) -> None:
        """A startup error lands in a log aggregator. A private component must not.

        The underlying jwcrypto exception is chained rather than formatted into the
        message for exactly this reason, and chaining is easy to undo by accident, so the
        property is asserted rather than trusted.
        """
        environment = _environment(settings_for_tests)
        secret = json.loads(environment["UCP_MERCHANT_SIGNING_JWK"])["d"]
        environment["UCP_MERCHANT_SIGNING_JWK"] = json.dumps({"kty": "oct", "d": secret})

        with pytest.raises(ValueError) as raised:
            Settings(**environment)
        assert secret not in str(raised.value)


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


class TestTheCapabilityCeilingStaysInStepWithRegistryB:
    """The one assertion neither package can make alone.

    ``commerce_api.deps`` is where Registry A and Registry B are actually defined, and
    ``commerce_protocols.core.identity`` holds the ceiling that keeps an external caller out
    of Registry B. The protocol package cannot import ``commerce_api`` to derive the second
    from the first -- commerce-api depends on commerce-protocols, so the import would be a
    cycle and ADR 0003 D2 fixes the direction.

    So the two lists are maintained separately and this file, which can see both, asserts
    they have not drifted. Without it, a Registry B capability added to ``deps`` becomes one
    the protocol ceiling has never heard of, and the failure is silent in both packages'
    suites.
    """

    def test_the_protocol_ceiling_is_exactly_the_agent_registry(self) -> None:
        """An external AI buyer is an agent that arrived over a protocol.

        Equality rather than a subset: a capability an agent may hold and a protocol caller
        may not would need a reason, and there is currently no such reason. If one is ever
        found, this test is where the argument gets written down.
        """
        from commerce_api.deps import AGENT_CAPABILITIES
        from commerce_protocols.core import PROTOCOL_CAPABILITIES

        assert PROTOCOL_CAPABILITIES == AGENT_CAPABILITIES

    def test_every_buyer_only_capability_is_named_in_the_consent_set(self) -> None:
        """The drift guard proper.

        Anything a buyer holds and an agent does not is Registry B: consent, or bound to the
        buyer's own session. ``payment.verify`` is the second kind and was found this way --
        it is not consent, but a protocol caller holding it could present a client return for
        somebody else's checkout, and the ceiling's guard had never been told about it.
        """
        from commerce_api.deps import AGENT_CAPABILITIES, BUYER_CAPABILITIES
        from commerce_protocols.core import CONSENT_CAPABILITIES

        registry_b = BUYER_CAPABILITIES - AGENT_CAPABILITIES
        unguarded = registry_b - CONSENT_CAPABILITIES
        assert not unguarded, (
            f"{sorted(unguarded)} are buyer-only in commerce_api.deps but absent from "
            "commerce_protocols.core.CONSENT_CAPABILITIES, so assert_never_consents would "
            "not catch a principal holding one"
        )

    def test_no_operator_capability_is_reachable_by_a_protocol_caller(self) -> None:
        """Registry C is the merchant's own surface and is not delegable outward either.

        Nothing in the operator set should be grantable to an external party, and the
        intersection being empty is the cheap way to keep noticing that as the merchant
        console grows.
        """
        from commerce_api.deps import OPERATOR_CAPABILITIES
        from commerce_protocols.core import PROTOCOL_CAPABILITIES

        # The two registries legitimately share plain reads; an operator and an external
        # buyer may both look at a catalogue and at an order they are entitled to see.
        # Anything else in the overlap would be merchant authority reaching outward.
        shared_reads = frozenset({"catalogue.read", "order.read"})
        overlap = OPERATOR_CAPABILITIES & PROTOCOL_CAPABILITIES
        assert overlap <= shared_reads, (
            f"{sorted(overlap - shared_reads)} is an operator capability an external "
            "protocol caller could hold"
        )
