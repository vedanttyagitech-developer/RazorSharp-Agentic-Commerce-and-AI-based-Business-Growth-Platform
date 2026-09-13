"""Exercise the actual browser seam against real sessions and the existing API router."""

import pytest
from commerce_api.app import create_app
from commerce_api.deps import require_session
from fastapi import Depends
from fastapi.testclient import TestClient


@pytest.fixture
def mounted(monkeypatch, tmp_path, settings_for_tests, seeded_tenant):
    (tmp_path / "index.html").write_text("<h1>Mounted frontend</h1>")
    for page in ("shop", "merchant", "platform"):
        (tmp_path / page).mkdir()
        (tmp_path / page / "index.html").write_text(f"<h1>{page}</h1>")
    monkeypatch.setenv("FRONTEND_DIST", str(tmp_path))
    monkeypatch.setenv("BROWSER_DEMO_ENABLED", "true")
    monkeypatch.setenv("BROWSER_ORIGIN", "http://localhost")
    monkeypatch.setenv("OPERATOR_DEMO_OPEN_ACCESS", "true")
    monkeypatch.setenv("COMMERCE_TENANT_SLUG", seeded_tenant.tenant_slug)
    app = create_app(settings_for_tests)
    with TestClient(app, base_url="http://localhost") as client:
        yield client


@pytest.mark.parametrize("page", ["/", "/shop/", "/merchant/", "/platform/"])
def test_pages_are_served_by_api(mounted, page):
    assert mounted.get(page).status_code == 200


def test_open_session_and_real_operator_reads(mounted):
    result = mounted.post("/api/platform/session", json={}, headers={"Origin": "http://localhost"})
    assert result.status_code == 200, result.text
    assert "token" not in result.json()
    assert "HttpOnly" in result.headers["set-cookie"]
    for path in ("ops/safe-mode", "ops/outbox", "review/reconciliation", "review/queue"):
        assert mounted.get("/api/platform/" + path).status_code == 200


def test_new_backend_route_needs_no_bridge_rule(mounted):
    # Insert before the final static mount, as create_app does for API routers.
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/v1/new-feature", dependencies=[Depends(require_session)])
    def feature():
        return {"available": True}

    mounted.app.router.routes.insert(-1, router.routes[0])
    result = mounted.get("/api/commerce/new-feature")
    assert result.status_code == 200, result.text
    assert result.json() == {"available": True}


@pytest.mark.parametrize("surface", ["commerce", "merchant", "platform"])
def test_cross_origin_write_cannot_mint_or_mutate(mounted, surface):
    result = mounted.post(
        f"/api/{surface}/session", json={}, headers={"Origin": "https://evil.test"}
    )
    assert result.status_code == 403
    assert not result.headers.get("set-cookie")


@pytest.mark.parametrize(
    "path",
    ["ops/safe-mode", "review/queue", "scenario/reset", "reserve/simulator/id", "demo/sessions"],
)
def test_buyer_cannot_reach_operator_or_fixture_routes(mounted, path):
    assert mounted.get("/api/commerce/" + path).status_code == 403


def test_wrong_role_cookie_and_forged_authorization_are_not_operator_access(mounted):
    mounted.get("/api/commerce/carts/current")
    buyer = mounted.cookies.get("rs_buyer_token")
    mounted.cookies.set("rs_platform_session", buyer, path="/api/platform")
    result = mounted.get("/api/platform/ops/safe-mode", headers={"Authorization": "Bearer forged"})
    assert result.status_code == 403


def test_mode_confirmation_still_required(mounted):
    result = mounted.post(
        "/api/platform/ops/safe-mode",
        json={"enabled": True},
        headers={"Origin": "http://localhost"},
    )
    assert result.status_code == 422


def test_disabled_browser_cannot_mint(mounted, monkeypatch):
    monkeypatch.setenv("BROWSER_DEMO_ENABLED", "false")
    assert mounted.get("/api/commerce/carts/current").status_code == 404


def test_unknown_api_is_not_frontend_html(mounted):
    result = mounted.get("/api/commerce/does-not-exist")
    assert result.status_code == 404
    assert "Mounted frontend" not in result.text


def test_all_private_api_routes_declare_session_guard(api_app):
    from commerce_api.app import _api_routes
    from test_capi_operator_boundary import dependencies

    # These are protocol/public entry points with their own explicit verification,
    # not browser session routes. Keep this inventory at the backend boundary.
    public = {
        "/healthz",
        "/readyz",
        "/v1/demo/sessions",
        "/.well-known/reserve-pay/jwks.json",
        "/v1/reserve/verification-keys",
        "/v1/config",
        "/v1/protocols",
        "/v1/protocols/conformance",
        "/v1/mcp/sessions",
        "/v1/mcp/tools",
        "/v1/mcp/tools/call",
    }
    missing = []
    for route in _api_routes(api_app):
        if not route.path.startswith("/v1/"):
            continue
        if route.path in public or route.path.startswith(("/v1/webhooks/", "/.well-known/")):
            continue
        if require_session not in dependencies(route.dependant):
            missing.append(route.path)
    assert not missing, missing


def test_merchant_session_is_separate_and_keeps_keys_private(mounted, settings_for_tests):
    mounted.get("/api/commerce/carts/current")
    result = mounted.get("/api/merchant/merchant/actions?actor_type=OPERATOR")
    assert result.status_code == 200
    assert mounted.cookies.get("rs_buyer_token") != mounted.cookies.get("rs_merchant_token")
    secret = settings_for_tests.scenario_key.get_secret_value()
    assert secret not in result.text
    assert secret not in result.headers.get("set-cookie", "")

    identity = mounted.post(
        "/api/merchant/session", json={}, headers={"Origin": "http://localhost"}
    )
    assert identity.status_code == 200
    assert identity.json()["principal_id"]
    assert "token" not in identity.json()
    assert mounted.cookies.get("rs_merchant_token") not in identity.text


def test_stale_session_recovers_before_execution(mounted):
    mounted.cookies.set("rs_merchant_token", "expired", path="/api/merchant")
    result = mounted.get("/api/merchant/merchant/actions")
    assert result.status_code == 200
    assert "rs_merchant_token=" in result.headers.get("set-cookie", "")


def test_exact_body_and_idempotency_key_reach_backend_once(mounted):
    from fastapi import APIRouter, Request

    calls = []
    router = APIRouter()

    @router.post("/v1/transport-check", dependencies=[Depends(require_session)])
    async def check(request: Request):
        calls.append((await request.body(), request.headers.get("idempotency-key")))
        return {"ok": True}

    mounted.app.router.routes.insert(-1, router.routes[0])
    payload = b'{"amount_minor":100,"approval_hash":"exact"}'
    response = mounted.post(
        "/api/merchant/transport-check",
        content=payload,
        headers={
            "Origin": "http://localhost",
            "Idempotency-Key": "same-key",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200
    assert calls == [(payload, "same-key")]


def test_configured_https_controls_cookie_not_forged_forwarded_host(mounted, monkeypatch):
    monkeypatch.setenv("BROWSER_ORIGIN", "https://demo.example")
    response = mounted.post(
        "/api/platform/session",
        json={},
        headers={"Origin": "https://demo.example", "X-Forwarded-Host": "evil.test"},
    )
    assert response.status_code == 200
    assert "Secure" in response.headers["set-cookie"]
    assert (
        mounted.post(
            "/api/platform/session",
            json={},
            headers={"Origin": "https://evil.test", "X-Forwarded-Host": "evil.test"},
        ).status_code
        == 403
    )


def test_voice_cross_origin_is_refused_before_gateway(mounted):
    assert (
        mounted.post("/api/voice/ticket", headers={"Origin": "https://evil.test"}).status_code
        == 403
    )


def test_sse_chunks_are_delivered_before_stream_finishes(mounted):
    import anyio
    from fastapi import APIRouter, Request
    from starlette.responses import StreamingResponse

    mounted.get("/api/commerce/carts/current")
    cookie = mounted.cookies.get("rs_buyer_token")

    async def exercise():
        first_delivered = anyio.Event()
        router = APIRouter()

        @router.get("/v1/stream-check", dependencies=[Depends(require_session)])
        async def stream(request: Request):
            assert request.headers["last-event-id"] == "previous"

            async def chunks():
                yield b"id: next\nevent: timeline\ndata: {}\n\n"
                # A buffering bridge deadlocks here instead of delivering the first frame.
                with anyio.fail_after(2):
                    await first_delivered.wait()
                yield b'event: complete\ndata: {"reason":"terminal"}\n\n'

            return StreamingResponse(
                chunks(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
            )

        mounted.app.router.routes.insert(-1, router.routes[0])
        messages = []

        async def receive():
            await anyio.sleep_forever()

        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.body" and b"id: next" in message.get("body", b""):
                first_delivered.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/commerce/stream-check",
            "raw_path": b"/api/commerce/stream-check",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"cookie", f"rs_buyer_token={cookie}".encode()),
                (b"last-event-id", b"previous"),
            ],
            "server": ("localhost", 80),
            "client": ("test", 123),
        }
        await mounted.app(scope, receive, send)
        assert first_delivered.is_set()
        assert any(b"event: complete" in item.get("body", b"") for item in messages)
        assert (b"x-accel-buffering", b"no") in messages[0]["headers"]

    anyio.run(exercise)


def test_encoded_buyer_label_survives_session_refresh(mounted):
    mounted.cookies.set("rs_buyer_ref", "buyer%3Areturning", domain="localhost.local", path="/")
    first = mounted.post("/api/commerce/session", json={}, headers={"Origin": "http://localhost"})
    assert first.status_code == 200
    assert mounted.cookies.get("rs_buyer_ref") == "buyer%3Areturning"
    mounted.cookies.set("rs_buyer_token", "expired", domain="localhost.local", path="/")
    renewed = mounted.post("/api/commerce/session", json={}, headers={"Origin": "http://localhost"})
    assert renewed.status_code == 200


@pytest.mark.parametrize("path", ["ops/metrics", "protocols", "protocols/conformance", "refunds"])
def test_operator_evidence_reads_are_connected(mounted, path):
    result = mounted.get("/api/platform/" + path)
    assert result.status_code == 200, result.text
    assert result.headers.get("cache-control") == "no-store"


def test_revival_requires_exact_confirmation_and_keeps_existing_command(mounted, monkeypatch):
    from types import SimpleNamespace

    from commerce_api.routers import ops

    command = "00000000-0000-0000-0000-000000000123"
    seen = []

    def revive(_session, command_id):
        seen.append(str(command_id))
        return SimpleNamespace(
            code=SimpleNamespace(value="OK"), status=SimpleNamespace(value="PENDING"), retry_at=None
        )

    monkeypatch.setattr(ops.dw, "revive", revive)
    path = "/api/platform/ops/outbox/" + command + "/revive"
    headers = {"Origin": "http://localhost"}
    assert mounted.post(path, json={"confirm": "wrong"}, headers=headers).status_code == 422
    assert seen == []
    result = mounted.post(path, json={"confirm": command}, headers=headers)
    assert result.status_code == 200, result.text
    assert seen == [command]
    assert (
        mounted.post(
            "/api/merchant/ops/outbox/" + command + "/revive",
            json={"confirm": command},
            headers=headers,
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "path",
    [
        "scenario/injections",
        "reserve/simulator/example",
        "protocols/conformance",
        "inspector/payment-attempts/example",
    ],
)
def test_new_evidence_ui_does_not_open_arbitrary_platform_writes(mounted, path):
    assert (
        mounted.post(
            "/api/platform/" + path, json={}, headers={"Origin": "http://localhost"}
        ).status_code
        == 403
    )


def test_clearing_cookies_cannot_bypass_session_mint_budget(mounted):
    for _ in range(20):
        mounted.cookies.clear()
        response = mounted.post(
            "/api/commerce/session", json={}, headers={"Origin": "http://localhost"}
        )
        assert response.status_code == 200, response.text
    mounted.cookies.clear()
    response = mounted.post(
        "/api/commerce/session", json={}, headers={"Origin": "http://localhost"}
    )
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    assert "set-cookie" not in response.headers


def test_protocol_lab_fetches_real_ucp_profiles(mounted):
    result = mounted.post(
        "/api/platform/protocols/probe",
        json={"protocol": "UCP"},
        headers={"Origin": "http://localhost"},
    )
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "passed"
    assert len(result.json()["steps"]) == 2
    assert all(step["http_status"] == 200 for step in result.json()["steps"])


def test_protocol_lab_reports_missing_configuration(mounted):
    result = mounted.post(
        "/api/platform/protocols/probe",
        json={"protocol": "ACP"},
        headers={"Origin": "http://localhost"},
    )
    assert result.status_code == 200
    assert result.json()["status"] == "not_configured"


def test_protocol_lab_runs_mcp_and_never_returns_credentials(mounted):
    from pydantic import SecretStr

    mounted.app.state.settings = mounted.app.state.settings.model_copy(
        update={
            "mcp_resource": "https://protocol-test.invalid/mcp",
            "mcp_token_secret": SecretStr("test-protocol-probe-secret-not-for-production"),
        }
    )
    result = mounted.post(
        "/api/platform/protocols/probe",
        json={"protocol": "MCP", "query": "milk"},
        headers={"Origin": "http://localhost"},
    )
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "passed", result.text
    assert len(result.json()["steps"]) == 4
    assert "access_token" not in result.text
    assert "test-protocol-probe-secret" not in result.text


def test_protocol_lab_refuses_non_operator(auth_client):
    result = auth_client.post("/v1/protocols/probe", json={"protocol": "UCP"})
    assert result.status_code == 403


def test_protocol_lab_acp_exercises_real_auth_refusal(mounted):
    mounted.app.state.settings = mounted.app.state.settings.model_copy(
        update={
            "acp_audience": "https://acp-test.invalid",
            "acp_clients": "[]",
        }
    )
    response = mounted.post(
        "/api/platform/protocols/probe",
        json={"protocol": "ACP"},
        headers={"Origin": "http://localhost"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "passed", response.text
    assert response.json()["steps"][0]["http_status"] in (401, 403)


@pytest.mark.parametrize("protocol", ["ACP", "MCP", "UCP"])
def test_console_can_enable_and_test_protocol_demo(mounted, protocol):
    original = mounted.app.state.settings
    before = mounted.get("/api/platform/protocols/demo-status")
    assert before.status_code == 200, before.text
    assert before.json()["enabled"][protocol] is False
    response = mounted.post(
        "/api/platform/protocols/enable-demo",
        json={"protocol": protocol},
        headers={"Origin": "http://localhost"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is True
    assert mounted.get("/api/platform/protocols/demo-status").json()["enabled"][protocol] is True
    mounted.app.state.protocol_demo_apps.clear()
    assert mounted.get("/api/platform/protocols/demo-status").json()["enabled"][protocol] is False
    response = mounted.post(
        "/api/platform/protocols/enable-demo",
        json={"protocol": protocol},
        headers={"Origin": "http://localhost"},
    )
    assert response.status_code == 200
    assert mounted.app.state.settings is original
    assert "signing_secret" not in response.text
    assert "access_token" not in response.text
    response = mounted.post(
        "/api/platform/protocols/probe",
        json={"protocol": protocol},
        headers={"Origin": "http://localhost"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "passed", response.text


def test_enable_protocol_demo_requires_operator(auth_client):
    response = auth_client.post("/v1/protocols/enable-demo", json={"protocol": "MCP"})
    assert response.status_code == 403


def test_protocol_demo_status_requires_operator(auth_client):
    assert auth_client.get("/v1/protocols/demo-status").status_code == 403
