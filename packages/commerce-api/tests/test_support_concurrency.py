from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from commerce_api.services import support_service
from test_capi_support_helpdesk import helpdesk, kernel, order_id  # noqa: F401

pytestmark = pytest.mark.db


def test_migration_preserves_duplicates_and_enforces_uniqueness(capi_admin_engine):
    import runpy
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    path = (
        Path(__file__).parents[2]
        / "platform-db/migrations/versions/fa912de43701_one_active_support_case.py"
    )
    migration = runpy.run_path(str(path))["upgrade"]
    with capi_admin_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(
                text("""CREATE TEMP TABLE support_cases (
                id uuid, tenant_id uuid, order_id uuid, buyer_ref text,
                status text, created_at timestamptz, updated_at timestamptz,
                resolution_note text
            ) ON COMMIT DROP""")
            )
            conn.execute(
                text("""INSERT INTO support_cases
                SELECT ('00000000-0000-0000-0000-' || lpad(n::text,12,'0'))::uuid,
                    '00000000-0000-0000-0000-000000000010'::uuid,
                    '00000000-0000-0000-0000-000000000020'::uuid,
                    'buyer', 'OPEN', now(), now(), 'original note'
                FROM generate_series(1,2) n""")
            )
            migration.__globals__["op"] = Operations(MigrationContext.configure(conn))
            migration()
            rows = conn.execute(
                text("SELECT status,resolution_note FROM support_cases ORDER BY id")
            ).all()
            assert rows[0] == ("OPEN", "original note")
            assert rows[1][0] == "CLOSED"
            assert "original note" in rows[1][1]
            assert "00000000-0000-0000-0000-000000000001" in rows[1][1]
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(text("UPDATE support_cases SET status='OPEN' WHERE status='CLOSED'"))
        finally:
            transaction.rollback()


def test_simultaneous_open_returns_one_case(auth_client, order_id, monkeypatch):  # noqa: F811
    barrier = Barrier(2)
    original = support_service._lock_order

    def lock(*args):
        barrier.wait(timeout=10)
        original(*args)

    monkeypatch.setattr(support_service, "_lock_order", lock)

    def send():
        return auth_client.post(
            f"/v1/orders/{order_id}/support-cases", json={"reason": "item_damaged"}
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: send(), range(2)))
    assert [r.status_code for r in results] == [201, 201]
    assert results[0].json()["case_id"] == results[1].json()["case_id"]
    monkeypatch.setattr(support_service, "_lock_order", original)
    assert send().json()["case_id"] == results[0].json()["case_id"]


def test_reopen_refuses_when_another_active_case_exists(
    auth_client,
    helpdesk,  # noqa: F811
    order_id,  # noqa: F811
    scenario_headers,  # noqa: F811
):
    def create():
        return auth_client.post(
            f"/v1/orders/{order_id}/support-cases", json={"reason": "item_damaged"}
        ).json()["case_id"]

    first = create()
    assert (
        helpdesk.post(
            f"/v1/support/cases/{first}/advance",
            headers=scenario_headers,
            json={"status": "CLOSED", "note": "Closed"},
        ).status_code
        == 200
    )
    second = create()
    refused = helpdesk.post(
        f"/v1/support/cases/{first}/advance",
        headers=scenario_headers,
        json={"status": "ACKNOWLEDGED", "note": "Reopen"},
    )
    assert refused.status_code == 409
    assert refused.json()["active_case_id"] == second
