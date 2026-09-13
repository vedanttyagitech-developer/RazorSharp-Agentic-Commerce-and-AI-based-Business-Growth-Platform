import json
import uuid

import httpx
import pytest
from sqlalchemy import text


@pytest.mark.db
@pytest.mark.asyncio
async def test_commit_failure_never_sends_success(api_app, demo_session, monkeypatch):
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    original = Session.commit

    def fail_kernel_commit(self):
        if "kernel" in self.get_bind().url.username:
            raise OperationalError("COMMIT", {}, Exception("injected commit failure"))
        return original(self)

    monkeypatch.setattr(Session, "commit", fail_kernel_commit)
    statuses = []

    async def wrapped(scope, receive, send):
        async def capture(message):
            if message["type"] == "http.response.start":
                statuses.append(message["status"])
            await send(message)

        await api_app(scope, receive, capture)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=wrapped, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/carts",
            headers={
                "Authorization": f"Bearer {demo_session.token}",
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
    assert response.status_code >= 500
    assert len(statuses) == 1 and statuses[0] >= 500


@pytest.mark.db
@pytest.mark.asyncio
async def test_response_follows_commit(api_app, demo_session, capi_admin_engine):
    observed = []

    async def wrapped(scope, receive, send):
        async def capture(message):
            if message["type"] == "http.response.body" and message.get("body"):
                body = json.loads(message["body"])
                if "cart_id" in body:
                    with capi_admin_engine.connect() as connection:
                        exists = connection.execute(
                            text("SELECT count(*) FROM carts WHERE id=:id"),
                            {"id": uuid.UUID(body["cart_id"])},
                        ).scalar_one()
                    observed.append((body["cart_id"], exists))
            await send(message)

        await api_app(scope, receive, capture)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=wrapped), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/carts",
            headers={
                "Authorization": f"Bearer {demo_session.token}",
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
    assert response.status_code == 201, response.text
    assert observed[0][1] == 1
    with capi_admin_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM carts WHERE id=:id"), {"id": uuid.UUID(observed[0][0])}
            ).scalar_one()
            == 1
        )
