"""Browser session boundary for the directly mounted frontend.

Commerce requests enter the existing ASGI router, without an HTTP proxy or a second
per-route allowlist. Cookies select a fixed surface role; backend dependencies remain
responsible for capabilities, ownership and financial admission.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import httpx
from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .deps import RequestContext, require_session
from .errors import ProblemError

SURFACES = {
    "commerce": ("BUYER", "rs_buyer_token"),
    "merchant": ("MERCHANT", "rs_merchant_token"),
    "platform": ("OPERATOR", "rs_platform_session"),
}


class BrowserMount:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        settings = request.app.state.settings
        parts = scope["path"].split("/", 3)
        surface = parts[2] if len(parts) > 2 else ""
        path = parts[3] if len(parts) > 3 else ""

        async def refuse(status: int, detail: str) -> None:
            await JSONResponse(
                {"detail": detail}, status_code=status, headers={"Cache-Control": "no-store"}
            )(scope, receive, send)

        if not settings.demo_routes_enabled or os.environ.get("BROWSER_DEMO_ENABLED") != "true":
            await refuse(404, "Browser demo is disabled.")
            return
        # Deployment owns this origin. Do not trust arbitrary forwarded browser headers.
        origin = os.environ.get("BROWSER_ORIGIN", "http://localhost:8000").rstrip("/")
        origins = {origin} | {
            value.strip().rstrip("/")
            for value in os.environ.get("BROWSER_ADDITIONAL_ORIGINS", "").split(",")
            if value.strip()
        }
        if any(
            urlsplit(value).scheme != "https"
            and not (
                urlsplit(value).scheme == "http"
                and urlsplit(value).hostname in {"localhost", "127.0.0.1", "::1"}
            )
            for value in origins
        ):
            await refuse(503, "HTTPS is required outside local development.")
            return
        if request.method not in {"GET", "HEAD"} and request.headers.get("origin") not in origins:
            await refuse(403, "Same-origin browser request required.")
            return
        if surface == "voice" and path == "ticket":
            await self.voice(request, origin, receive, send)
            return
        if surface not in SURFACES or not path or any(p in {".", ".."} for p in path.split("/")):
            await refuse(404, "Unknown browser surface.")
            return
        role, cookie_name = SURFACES[surface]
        if surface == "platform" and os.environ.get("OPERATOR_DEMO_OPEN_ACCESS") != "true":
            await refuse(404, "Open operator demo is disabled.")
            return
        # Infrastructure and fixture control are never capabilities of this browser demo.
        group = path.split("/", 1)[0]
        if path != "session" and (
            group in {"demo", "scenario", "webhooks", "mcp"}
            or (group == "protocols" and surface != "platform")
            or path.startswith("reserve/simulator/")
            or (surface != "platform" and group in {"ops", "review"})
            or (
                surface == "platform"
                and (
                    group
                    not in {
                        "ops",
                        "review",
                        "inspector",
                        "audit",
                        "merchants",
                        "protocols",
                        "checkouts",
                        "refunds",
                        "config",
                    }
                    or request.method not in {"GET", "HEAD"}
                    and not (
                        path == "ops/safe-mode"
                        or (
                            request.method == "POST"
                            and path in {"protocols/probe", "protocols/enable-demo"}
                        )
                        or (
                            request.method == "POST"
                            and len(path.split("/")) == 4
                            and path.startswith("ops/outbox/")
                            and path.endswith("/revive")
                        )
                    )
                )
            )
        ):
            await refuse(403, "This operation is outside this demo surface.")
            return
        token = unquote(request.cookies[cookie_name]) if cookie_name in request.cookies else None
        added_cookies: list[bytes] = []
        secure = "; Secure" if origin.startswith("https://") else ""
        cookie_path = "/" if surface == "commerce" else f"/api/{surface}"

        async def authenticate(value: str) -> RequestContext:
            auth_scope = dict(scope)
            auth_scope["headers"] = [(b"authorization", f"Bearer {value}".encode())]
            return await run_in_threadpool(require_session, Request(auth_scope))

        ctx = None
        if token:
            try:
                ctx = await authenticate(token)
            except ProblemError as exc:
                if exc.status != 401:
                    await refuse(exc.status, exc.detail or "Session refused.")
                    return
                token = None
            else:
                if ctx.principal.actor_type.value != role:
                    await refuse(403, "Session role does not match this surface.")
                    return
        if not token:
            from commerce_domain.workload import WorkloadExceededError

            peer = request.client.host if request.client else "unknown"
            try:
                with request.app.state.workload.admit(
                    [
                        ("browser:mint:global", 120, 8),
                        (f"browser:mint:peer:{peer}", 20, 4),
                    ]
                ):
                    pass
            except WorkloadExceededError:
                await JSONResponse(
                    {"detail": "Demo is busy. Please wait a minute and try again."},
                    status_code=429,
                    headers={"Retry-After": "60", "Cache-Control": "no-store"},
                )(scope, receive, send)
                return
            headers = {}
            if role == "OPERATOR" and settings.scenario_key:
                headers["X-Scenario-Key"] = settings.scenario_key.get_secret_value()
            body = {
                "tenant_slug": os.environ.get("COMMERCE_TENANT_SLUG", "demo"),
                "actor_type": role,
            }
            if role == "BUYER" and request.cookies.get("rs_buyer_ref"):
                body["buyer_ref"] = unquote(request.cookies["rs_buyer_ref"])
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=request.app), base_url="http://internal"
            ) as client:
                minted = await client.post("/v1/demo/sessions", json=body, headers=headers)
            if minted.status_code != 201:
                await refuse(
                    429 if minted.status_code == 429 else 503,
                    "Demo session unavailable. Please try again later.",
                )
                return
            session = minted.json()
            token = session["token"]
            ctx = await authenticate(token)
            added_cookies.append(
                (
                    f"{cookie_name}={quote(token)}; HttpOnly; SameSite=Strict; "
                    f"Path={cookie_path}{secure}"
                ).encode()
            )
            if role == "BUYER":
                added_cookies.append(
                    (
                        f"rs_buyer_ref={quote(session['buyer_ref'])}; Max-Age=34560000; "
                        f"HttpOnly; SameSite=Strict; Path=/{secure}"
                    ).encode()
                )

        async def with_cookies(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = (
                    [(k, v) for k, v in message["headers"] if k.lower() != b"cache-control"]
                    + [(b"cache-control", b"no-store")]
                    + [(b"set-cookie", c) for c in added_cookies]
                )
            await send(message)

        assert ctx is not None
        if path == "session":
            if request.method != "POST":
                await refuse(405, "Use POST to initialize a session.")
                return
            await JSONResponse(
                {
                    "tenant_id": str(ctx.tenant_id),
                    "merchant_id": str(ctx.merchant_id),
                    "principal_id": ctx.principal.principal_id,
                },
                headers={"Cache-Control": "no-store"},
            )(scope, receive, with_cookies)
            return
        if (
            surface == "platform"
            and request.method == "POST"
            and path not in {"protocols/probe", "protocols/enable-demo"}
        ):
            try:
                body = await request.json()
            except ValueError:
                await refuse(400, "Invalid JSON body.")
                return
            if not isinstance(body, dict):
                await refuse(422, "Expected a mode change object.")
                return
            if path == "ops/safe-mode":
                if body.get("confirm") != "CHANGE TENANT MODE" or not isinstance(
                    body.get("enabled"), bool
                ):
                    await refuse(422, "Confirm the tenant mode change.")
                    return
                payload = json.dumps(
                    {
                        "enabled": body["enabled"],
                        "reason": "OPERATOR_DECLARED_INCIDENT"
                        if body["enabled"]
                        else "INCIDENT_RESOLVED",
                    }
                ).encode()
            else:
                command_id = path.split("/")[2]
                if body.get("confirm") != command_id:
                    await refuse(422, "Confirm the exact command identifier to revive.")
                    return
                payload = b"{}"
            delivered = False
            original_receive = receive

            async def rewritten_receive() -> Message:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": payload, "more_body": False}
                return await original_receive()

            receive = rewritten_receive
        else:
            payload = None
        downstream = dict(scope)
        downstream["path"] = "/v1/" + path
        downstream["raw_path"] = downstream["path"].encode()
        removed = {
            b"authorization",
            b"x-scenario-key",
            b"cookie",
            b"x-scenario-buyer-authorization",
        }
        if payload is not None:
            removed.add(b"content-length")
        downstream["headers"] = [(k, v) for k, v in scope["headers"] if k.lower() not in removed]
        downstream["headers"].append((b"authorization", f"Bearer {token}".encode()))
        if role in {"MERCHANT", "OPERATOR"} and settings.scenario_key:
            downstream["headers"].append(
                (b"x-scenario-key", settings.scenario_key.get_secret_value().encode())
            )
        # Direct ASGI dispatch preserves SSE chunks, disconnects and Last-Event-ID.
        await self.app(downstream, receive, with_cookies)

    async def voice(self, request: Request, origin: str, receive: Receive, send: Send) -> None:
        token = request.cookies.get("rs_buyer_token")
        if request.method != "POST" or not token:
            await JSONResponse({"detail": "Open the shop before starting voice."}, status_code=409)(
                request.scope, receive, send
            )
            return
        gateway = os.environ.get("VOICE_GATEWAY_URL", "http://127.0.0.1:8100")
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                response = await client.post(
                    gateway + "/v1/voice/tickets", headers={"Authorization": f"Bearer {token}"}
                )
            data = response.json()
            if response.is_success:
                socket_origin = os.environ.get("VOICE_PUBLIC_ORIGIN", origin)
                data["socket_url"] = (
                    socket_origin.replace("https://", "wss://").replace("http://", "ws://")
                    + "/v1/voice/stream"
                )
            result = JSONResponse(
                data, status_code=response.status_code, headers={"Cache-Control": "no-store"}
            )
        except httpx.HTTPError, ValueError:
            result = JSONResponse(
                {"detail": "Voice is unavailable. Typing still works."}, status_code=503
            )
        await result(request.scope, receive, send)


def install_browser_mount(app: FastAPI) -> None:
    directory = os.environ.get("FRONTEND_DIST")
    if not directory:
        return
    root = Path(directory).resolve()
    if not (root / "index.html").is_file():
        raise RuntimeError("FRONTEND_DIST must contain the built frontend")
    app.add_middleware(BrowserMount)
    app.mount("/", StaticFiles(directory=root, html=True), name="frontend")
