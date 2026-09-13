"""Bounded operator diagnostics; never changes the API's liveness probe."""

import asyncio
import math
import os

import httpx


async def probe(client: httpx.AsyncClient, name: str, url: str) -> dict[str, str]:
    if not url:
        return {"status": "unconfigured", "notice": "Health endpoint is not configured."}
    try:
        response = await client.get(url)
        body = response.json()
        if response.status_code != 200 or body.get("status") != "ok":
            return {"status": "unavailable", "notice": "Service reports an unhealthy state."}
        if name == "voice" and body.get("speech_available") is not True:
            return {"status": "degraded", "notice": "Gateway responds, but speech is unavailable."}
        if name == "worker":
            age = body.get("last_tick_seconds_ago")
            if (
                not isinstance(age, (int, float))
                or isinstance(age, bool)
                or not math.isfinite(age)
                or age < 0
            ):
                return {"status": "unknown", "notice": "No valid worker heartbeat evidence."}
        return {
            "status": "healthy",
            "notice": "Recent worker heartbeat."
            if name == "worker"
            else "Gateway responds and speech is available; no live audio turn tested.",
        }
    except httpx.HTTPError, ValueError, TypeError, AttributeError:
        return {"status": "unavailable", "notice": "Health check failed or timed out."}


async def runtime_health() -> dict[str, dict[str, str]]:
    # Configuration is operator-owned, never a browser-supplied URL. Do not return URLs.
    voice = os.environ.get("VOICE_GATEWAY_URL", "").rstrip("/")
    async with httpx.AsyncClient(timeout=2.0, follow_redirects=False, trust_env=False) as client:
        worker_result, voice_result = await asyncio.gather(
            probe(client, "worker", os.environ.get("WORKER_HEALTH_URL", "")),
            probe(client, "voice", f"{voice}/healthz" if voice else ""),
        )
    return {
        "api": {"status": "healthy", "notice": "Operator request served; liveness only."},
        "worker": worker_result,
        "voice": voice_result,
    }
