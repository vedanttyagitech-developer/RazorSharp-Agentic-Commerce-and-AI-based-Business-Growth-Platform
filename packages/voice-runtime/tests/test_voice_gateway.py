"""The gateway's edges: tickets, origin, and the authority the socket does not carry.

These are security properties, so they are asserted on observable behaviour -- the status
code, the close code, what the response body does and does not contain -- rather than on
internal state.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from voice_runtime.clock import FakeClock
from voice_runtime.gateway import VoiceGateway, create_app
from voice_runtime.gateway.settings import GatewaySettings
from voice_runtime.stt.fakes import FakeSttFactory
from voice_runtime.testing import CAPABILITIES
from voice_runtime.testing import SAMPLE_SESSION_ID as SESSION
from voice_runtime.tts.synth import FakeSynthesizer
from voice_runtime.wire.origin import OriginPolicy
from voice_runtime.wire.tickets import TicketError, TicketIssuer

ORIGIN = "http://localhost:3000"


def build_gateway(
    *, capabilities: dict[str, Any] | None = None, status_code: int = 200
) -> VoiceGateway:
    payload = capabilities if capabilities is not None else CAPABILITIES

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/agent/capabilities":
            return httpx.Response(status_code, json=payload)
        return httpx.Response(200, json={"reply": "ok", "language": "en", "structured": None})

    return VoiceGateway(
        GatewaySettings(project="p", use_vertex=True, allowed_origins=(ORIGIN,)),
        clock=FakeClock(),
        http_client=httpx.AsyncClient(
            base_url="http://api.test", transport=httpx.MockTransport(handler)
        ),
        stt_factory=FakeSttFactory(),
        synthesizer=FakeSynthesizer(),
    )


# ---- minting ---------------------------------------------------------------------------


def test_a_ticket_needs_a_bearer() -> None:
    with TestClient(create_app(build_gateway())) as client:
        assert client.post("/v1/voice/tickets").status_code == 401
        assert (
            client.post("/v1/voice/tickets", headers={"Authorization": "Basic abc"}).status_code
            == 401
        )
        assert (
            client.post("/v1/voice/tickets", headers={"Authorization": "Bearer "}).status_code
            == 401
        )


def test_a_minted_ticket_never_contains_the_bearer() -> None:
    """The bearer stays server-side. The browser gets an opaque handle and nothing else."""
    with TestClient(create_app(build_gateway())) as client:
        response = client.post(
            "/v1/voice/tickets", headers={"Authorization": "Bearer super-secret-token"}
        )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"ticket", "expires_in_s", "session_id", "speech_available"}
    assert "super-secret-token" not in response.text
    assert body["session_id"] == SESSION
    assert body["ticket"] != "super-secret-token"


def test_a_merchant_session_cannot_mint_a_voice_ticket() -> None:
    gateway = build_gateway(capabilities=dict(CAPABILITIES, copilot="merchant"))
    with TestClient(create_app(gateway)) as client:
        response = client.post("/v1/voice/tickets", headers={"Authorization": "Bearer t"})
    assert response.status_code == 403


def test_a_revoked_session_cannot_mint_a_voice_ticket() -> None:
    gateway = build_gateway(status_code=401)
    with TestClient(create_app(gateway)) as client:
        response = client.post("/v1/voice/tickets", headers={"Authorization": "Bearer stale"})
    assert response.status_code == 403


# ---- the socket -------------------------------------------------------------------------


def test_a_socket_without_an_allowed_origin_is_refused() -> None:
    gateway = build_gateway()
    with TestClient(create_app(gateway)) as client:
        ticket = client.post("/v1/voice/tickets", headers={"Authorization": "Bearer t"}).json()[
            "ticket"
        ]
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                f"/v1/voice/stream?ticket={ticket}", headers={"Origin": "http://evil.test"}
            ):
                pass
    assert gateway.sockets_refused == 1
    assert gateway.sockets_opened == 0


def test_a_socket_with_no_ticket_is_refused() -> None:
    gateway = build_gateway()
    with TestClient(create_app(gateway)) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/v1/voice/stream", headers={"Origin": ORIGIN}):
                pass
    assert gateway.sockets_refused == 1


def test_a_ticket_opens_exactly_one_socket() -> None:
    """Replaying a ticket is indistinguishable from guessing one: both are refused."""
    gateway = build_gateway()
    with TestClient(create_app(gateway)) as client:
        ticket = client.post("/v1/voice/tickets", headers={"Authorization": "Bearer t"}).json()[
            "ticket"
        ]
        with client.websocket_connect(
            f"/v1/voice/stream?ticket={ticket}", headers={"Origin": ORIGIN}
        ) as socket:
            ready = socket.receive_json()
            assert ready["type"] == "session_ready"
            assert ready["voice_is_authority"] is False
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                f"/v1/voice/stream?ticket={ticket}", headers={"Origin": ORIGIN}
            ):
                pass
    assert gateway.sockets_opened == 1
    assert gateway.sockets_refused == 1


def test_metrics_surface_the_counters_that_matter() -> None:
    gateway = build_gateway()
    with TestClient(create_app(gateway)) as client:
        client.post("/v1/voice/tickets", headers={"Authorization": "Bearer t"})
        metrics = client.get("/v1/voice/metrics").json()
    assert metrics["tickets_issued"] == 1
    assert metrics["tickets_outstanding"] == 1
    assert metrics["sockets_opened"] == 0


def test_healthz_says_whether_speech_is_available() -> None:
    with TestClient(create_app(build_gateway())) as client:
        assert client.get("/healthz").json() == {"status": "ok", "speech_available": True}
    unconfigured = VoiceGateway(GatewaySettings(project=None, use_vertex=False))
    with TestClient(create_app(unconfigured)) as client:
        assert client.get("/healthz").json()["speech_available"] is False


# ---- the ticket store on its own -----------------------------------------------------


def test_a_ticket_is_single_use() -> None:
    clock = FakeClock()
    issuer = TicketIssuer(clock=clock)
    issued = issuer.issue(session_id="s", principal_id="p", bearer="b")
    assert issuer.redeem(issued.token).bearer == "b"
    with pytest.raises(TicketError) as caught:
        issuer.redeem(issued.token)
    assert caught.value.reason == "unknown", "a spent ticket looks exactly like a guessed one"


def test_a_ticket_expires() -> None:
    clock = FakeClock()
    issuer = TicketIssuer(clock=clock, ttl_s=60.0)
    issued = issuer.issue(session_id="s", principal_id="p", bearer="b")
    clock.advance(61.0)
    with pytest.raises(TicketError) as caught:
        issuer.redeem(issued.token)
    assert caught.value.reason == "expired"


def test_a_malformed_ticket_is_refused_without_touching_the_store() -> None:
    issuer = TicketIssuer(clock=FakeClock())
    issued = issuer.issue(session_id="s", principal_id="p", bearer="b")
    for bad in (None, "", "short", "../../etc/passwd", "!" * 43):
        with pytest.raises(TicketError) as caught:
            issuer.redeem(bad)
        assert caught.value.reason == "malformed"
    assert issuer.redeem(issued.token).bearer == "b", "the real ticket still works"


def test_a_ticket_from_another_tenant_is_refused() -> None:
    """The store enforces it -- but see the test below: production never asks it to."""
    issuer = TicketIssuer(clock=FakeClock())
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    issued = issuer.issue(session_id="s", principal_id="p", bearer="b", tenant_id=mine)
    with pytest.raises(TicketError) as caught:
        issuer.redeem(issued.token, tenant_id=theirs)
    assert caught.value.reason == "tenant_mismatch"


def test_the_tenant_binding_is_currently_inert_in_production() -> None:
    """An honest test for a control that does nothing, so nobody mistakes it for one.

    ``GET /v1/agent/capabilities`` does not return a tenant, so ``VoiceIdentity.tenant_id``
    is always ``None`` and the gateway redeems without one -- the mismatch branch above can
    never fire on the real path. This is not a hole: the trusted server enforces tenancy on
    every call the gateway makes with the buyer's bearer. But a test that exercises a
    control only through an argument production never passes is a test that reads as
    protection and is not. When the API grows the field, this test should fail and be
    deleted.
    """
    gateway = build_gateway()
    with TestClient(create_app(gateway)) as client:
        client.post("/v1/voice/tickets", headers={"Authorization": "Bearer t"})
    claims = next(iter(gateway.tickets._tickets.values()))  # noqa: SLF001 - asserting a gap
    assert claims.tenant_id is None, "if this now has a tenant, arm the check and delete me"


def test_the_bearer_is_redacted_from_a_claims_repr() -> None:
    """A bearer that reaches a log line or a traceback has already leaked."""
    issuer = TicketIssuer(clock=FakeClock())
    issued = issuer.issue(session_id="s", principal_id="p", bearer="super-secret-token")
    assert "super-secret-token" not in repr(issued.claims)
    assert "<redacted>" in repr(issued.claims)


def test_tickets_are_unguessable_and_never_repeat() -> None:
    issuer = TicketIssuer(clock=FakeClock())
    tokens = {issuer.issue(session_id="s", principal_id="p", bearer="b").token for _ in range(200)}
    assert len(tokens) == 200
    assert all(len(token) == 43 for token in tokens), "32 random bytes, base64url, unpadded"


# ---- origin policy ---------------------------------------------------------------------


def test_origin_policy_refuses_missing_null_and_unlisted() -> None:
    policy = OriginPolicy(["https://shop.example", "http://localhost:3000"])
    assert policy.check("https://shop.example").allowed
    assert policy.check("https://shop.example:443").allowed, "the default port is the same origin"
    assert not policy.check(None).allowed
    assert not policy.check("").allowed
    assert not policy.check("null").allowed
    assert not policy.check("https://shop.example.evil.test").allowed
    assert not policy.check("http://shop.example").allowed, "scheme is part of the origin"


def test_an_empty_allow_list_admits_nobody() -> None:
    policy = OriginPolicy([])
    assert not policy.check("https://shop.example").allowed
    assert not policy.check(None).allowed


# ---- minting from a browser, cross-origin ----------------------------------------------


def test_the_ticket_endpoint_answers_a_preflight_from_an_allowed_origin() -> None:
    """A browser that mints here directly is preflighted, and gets an answer.

    The storefront is not that browser -- it mints server-side, because the bearer this
    endpoint wants lives in an httpOnly cookie no page script can read. But a POST carrying
    an Authorization header from any origin triggers an OPTIONS first, and a gateway that
    answered nothing would fail in the one place a caller cannot see why: the browser would
    never send the real request, which looks exactly like the gateway being down.
    """
    with TestClient(create_app(build_gateway())) as client:
        response = client.options(
            "/v1/voice/tickets",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization",
            },
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert "authorization" in response.headers.get("access-control-allow-headers", "").lower()


def test_the_cors_allowlist_is_the_same_one_the_socket_checks() -> None:
    """A browser must not be able to reach one surface from an origin the other refuses."""
    gateway = build_gateway()
    with TestClient(create_app(gateway)) as client:
        refused = client.options(
            "/v1/voice/tickets",
            headers={
                "Origin": "http://evil.test",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization",
            },
        )
    assert refused.status_code == 400
    assert "access-control-allow-origin" not in refused.headers
    # And the same origin is refused on the handshake.
    assert not gateway.origins.check("http://evil.test").allowed
    assert gateway.origins.check(ORIGIN).allowed


def test_credentials_are_not_allowed_because_the_bearer_is_never_a_cookie() -> None:
    with TestClient(create_app(build_gateway())) as client:
        response = client.options(
            "/v1/voice/tickets",
            headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"},
        )
    assert "access-control-allow-credentials" not in response.headers


def test_old_google_configuration_cannot_reenable_removed_models() -> None:
    gateway = VoiceGateway(GatewaySettings(project="old-project", use_vertex=True))
    assert gateway.stt_factory() is None
    assert not gateway.speech_available
    with TestClient(create_app(gateway)) as client:
        assert client.get("/healthz").json()["speech_available"] is False


def test_ticket_budget_rejects_before_issuing_more_tickets():
    gateway = build_gateway()
    with TestClient(create_app(gateway)) as client:
        for _ in range(10):
            assert (
                client.post("/v1/voice/tickets", headers={"Authorization": "Bearer t"}).status_code
                == 200
            )
        response = client.post("/v1/voice/tickets", headers={"Authorization": "Bearer t"})
        assert response.status_code == 429
        assert response.headers["retry-after"] == "60"
        assert gateway.tickets.issued == 10


def test_parallel_voice_is_refused_and_disconnect_releases_capacity():
    gateway = build_gateway()
    with TestClient(create_app(gateway)) as client:

        def ticket():
            return client.post("/v1/voice/tickets", headers={"Authorization": "Bearer t"}).json()[
                "ticket"
            ]

        first, second = ticket(), ticket()
        with client.websocket_connect(
            f"/v1/voice/stream?ticket={first}", headers={"Origin": ORIGIN}
        ) as socket:
            assert socket.receive_json()["type"] == "session_ready"
            with pytest.raises(WebSocketDisconnect) as rejected:
                with client.websocket_connect(
                    f"/v1/voice/stream?ticket={second}", headers={"Origin": ORIGIN}
                ):
                    pass
            assert rejected.value.code == 1013
        with client.websocket_connect(
            f"/v1/voice/stream?ticket={ticket()}", headers={"Origin": ORIGIN}
        ) as socket:
            assert socket.receive_json()["type"] == "session_ready"
        assert gateway.sockets_opened == 2


def test_voice_budget_identity_comes_from_authenticated_capabilities():
    from voice_runtime.gateway.agent_client import identity_from_capabilities

    tenant = str(uuid.uuid4())
    identity = identity_from_capabilities(
        {**CAPABILITIES, "tenant_id": tenant, "buyer_ref": "buyer-one"}
    )
    assert str(identity.tenant_id) == tenant
    assert identity.buyer_ref == "buyer-one"


def test_voice_duration_limit_closes_socket_and_releases_slot(monkeypatch):
    from voice_runtime.gateway import app as gateway_module

    monkeypatch.setattr(gateway_module, "_MAX_SESSION_SECONDS", 0.05)
    gateway = build_gateway()
    with TestClient(create_app(gateway)) as client:
        for _ in range(2):
            ticket = client.post("/v1/voice/tickets", headers={"Authorization": "Bearer t"}).json()[
                "ticket"
            ]
            with client.websocket_connect(
                f"/v1/voice/stream?ticket={ticket}", headers={"Origin": ORIGIN}
            ) as socket:
                assert socket.receive_json()["type"] == "session_ready"
                with pytest.raises(WebSocketDisconnect) as closed:
                    while True:
                        socket.receive_json()
                assert closed.value.code == 1000
        assert gateway.sockets_opened == 2
