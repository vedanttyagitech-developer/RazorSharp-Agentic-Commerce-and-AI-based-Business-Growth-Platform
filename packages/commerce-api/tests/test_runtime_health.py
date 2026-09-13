import httpx
import pytest
from commerce_api.services.runtime_health import probe


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "code", "body", "expected"),
    [
        ("worker", 200, {"status": "ok", "last_tick_seconds_ago": 1}, "healthy"),
        ("worker", 503, {"status": "stale", "last_tick_seconds_ago": 400}, "unavailable"),
        ("worker", 200, {"status": "ok"}, "unknown"),
        ("voice", 200, {"status": "ok", "speech_available": True}, "healthy"),
        ("voice", 200, {"status": "ok", "speech_available": False}, "degraded"),
        ("voice", 200, {}, "unavailable"),
        ("voice", 200, [], "unavailable"),
        ("voice", 302, {}, "unavailable"),
    ],
)
async def test_observed_health(name, code, body, expected):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _req: httpx.Response(code, json=body))
    ) as client:
        result = await probe(client, name, "http://internal/healthz")
    assert result["status"] == expected
    assert "internal" not in str(result)


@pytest.mark.asyncio
async def test_unreachable_and_unconfigured_are_not_healthy():
    def timeout(_req):
        raise httpx.ConnectTimeout("secret internal destination")

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        assert (await probe(client, "voice", "http://internal"))["status"] == "unavailable"
        assert (await probe(client, "worker", ""))["status"] == "unconfigured"


def test_runtime_health_requires_operator(client, operator_headers, monkeypatch):
    async def result():
        return {
            "api": {"status": "healthy"},
            "worker": {"status": "unavailable"},
            "voice": {"status": "degraded"},
        }

    monkeypatch.setattr("commerce_api.services.runtime_health.runtime_health", result)
    assert client.get("/v1/ops/runtime-health").status_code == 401
    response = client.get("/v1/ops/runtime-health", headers=operator_headers)
    assert response.status_code == 200
    assert response.json()["worker"]["status"] == "unavailable"
    assert response.headers["cache-control"] == "no-store"
