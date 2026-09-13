"""Signed simulator authority: adversarial binding and execution tests, real PostgreSQL."""

import json
import os
import uuid

import pytest
from commerce_domain import Money
from platform_db import set_tenant
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_capi_approve_and_pay import _card
from test_capi_reserve import execute, pay, permission
from transaction_kernel.authority import AuthorityError, AuthorityKind, grant_authority
from transaction_kernel.reserve_proofs import ReserveProofError, persist, verify_artifact

pytestmark = pytest.mark.db


def artifact(engine, authority):
    with engine.begin() as c:
        return c.execute(
            text("SELECT artifact_jws FROM verified_authority_proofs WHERE authority_id=:a"),
            {"a": authority["authority_id"]},
        ).scalar_one()


def test_signed_merchant_wide_finite_expiry(auth_client, capi_admin_engine):
    response = auth_client.post(
        "/v1/reserve/authorities",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"allowed_skus": None, "per_purchase_limit_minor": 20000, "capacity_minor": 500000},
    )
    assert response.status_code == 200, response.text
    claims = verify_artifact(artifact(capi_admin_engine, response.json()))
    assert claims["allowed_skus"] is None and claims["expires_at"] == response.json()["expires_at"]
    assert claims["max_amount_minor"] == 500000 and claims["per_purchase_limit_minor"] == 20000
    assert claims["currency"] == "INR" and claims["provider_reference"].startswith("sim_reserve_")


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_amount_minor", 999999),
        ("per_purchase_limit_minor", 100),
        ("buyer_ref", "forged-buyer"),
        ("allowed_skus", None),
    ],
)
def test_db_bounds_tampering_refused(auth_client, capi_admin_engine, field, value):
    auth = permission(auth_client)
    card = _card(auth_client)
    with capi_admin_engine.begin() as c:
        # Names are closed test parameters, never user input.
        c.execute(
            text(f"UPDATE delegated_authorities SET {field}=:v WHERE id=:a"),  # noqa: S608
            {"v": value, "a": auth["authority_id"]},
        )
    result = pay(auth_client, card, auth)
    assert result.status_code == 404 or not result.json()["allowed"], result.text
    with capi_admin_engine.begin() as c:
        assert (
            c.execute(
                text("SELECT consumed_amount_minor FROM delegated_authorities WHERE id=:a"),
                {"a": auth["authority_id"]},
            ).scalar_one()
            == 0
        )


def test_key_revocation_before_worker_sends(
    auth_client, demo_session, capi_admin_engine, monkeypatch
):
    auth = permission(auth_client)
    result = pay(auth_client, _card(auth_client), auth).json()
    assert result["allowed"], result
    ring = json.loads(os.environ["RESERVE_PROVIDER_VERIFICATION_JWKS"])
    ring["revoked_kids"] = [ring["keys"][0]["kid"]]
    monkeypatch.setenv("RESERVE_PROVIDER_VERIFICATION_JWKS", json.dumps(ring))
    execute(capi_admin_engine, demo_session.tenant_id, result["attempt_id"])
    state = auth_client.get(f"/v1/reserve/payments/{result['attempt_id']}").json()
    assert state["allocation"] == "RELEASED" and state["order_id"] is None, state


def test_missing_signer_fails_closed(auth_client, capi_admin_engine, demo_session, monkeypatch):
    monkeypatch.delenv("RESERVE_PROVIDER_SIGNING_JWK")
    r = auth_client.post(
        "/v1/reserve/authorities",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"allowed_skus": None, "per_purchase_limit_minor": 20000, "capacity_minor": 500000},
    )
    assert r.status_code == 503, r.text
    with capi_admin_engine.begin() as c:
        assert (
            c.execute(
                text("SELECT count(*) FROM delegated_authorities WHERE tenant_id=:t"),
                {"t": demo_session.tenant_id},
            ).scalar_one()
            == 0
        )


def test_unsigned_kernel_grant_refused(capi_kernel_engine, demo_session):
    with Session(capi_kernel_engine) as s, s.begin():
        set_tenant(s, demo_session.tenant_id)
        with pytest.raises(AuthorityError, match="signed authorization"):
            grant_authority(
                s,
                tenant_id=demo_session.tenant_id,
                merchant_id=demo_session.merchant_id,
                buyer_ref=demo_session.buyer_ref,
                kind=AuthorityKind.RESERVE,
                max_amount=Money(500000, "INR"),
                until_revoked=True,
            )


def test_proof_is_immutable_for_kernel(
    auth_client, capi_kernel_engine, capi_admin_engine, demo_session
):
    auth = permission(auth_client)
    for statement in (
        "UPDATE verified_authority_proofs SET payload_sha256=payload_sha256",
        "DELETE FROM verified_authority_proofs",
    ):
        with pytest.raises(DBAPIError), Session(capi_kernel_engine) as s, s.begin():
            set_tenant(s, demo_session.tenant_id)
            s.execute(text(statement + " WHERE authority_id=:a"), {"a": auth["authority_id"]})
    assert verify_artifact(artifact(capi_admin_engine, auth))


def test_replay_and_swapped_binding_fail(
    auth_client, capi_kernel_engine, capi_admin_engine, demo_session
):
    auth = permission(auth_client)
    proof = artifact(capi_admin_engine, auth)
    claims = verify_artifact(proof)
    with (
        pytest.raises(ReserveProofError, match="bounds_mismatch"),
        Session(capi_kernel_engine) as s,
        s.begin(),
    ):
        set_tenant(s, demo_session.tenant_id)
        persist(s, proof, {"authority_id": str(uuid.uuid4())})
    with pytest.raises(DBAPIError), Session(capi_kernel_engine) as s, s.begin():
        set_tenant(s, demo_session.tenant_id)
        persist(s, proof, {"authority_id": claims["authority_id"]})


def test_unknown_capacity_stays_held_when_key_revoked(
    auth_client, demo_session, capi_admin_engine, scenario_headers, monkeypatch
):
    auth = permission(auth_client)
    result = pay(auth_client, _card(auth_client), auth).json()
    assert result["allowed"]
    attempt = result["attempt_id"]
    r = auth_client.post(
        f"/v1/reserve/simulator/{attempt}",
        headers={**scenario_headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"outcome": "unknown"},
    )
    assert r.status_code == 200
    execute(capi_admin_engine, demo_session.tenant_id, attempt)
    ring = json.loads(os.environ["RESERVE_PROVIDER_VERIFICATION_JWKS"])
    ring["revoked_kids"] = [ring["keys"][0]["kid"]]
    monkeypatch.setenv("RESERVE_PROVIDER_VERIFICATION_JWKS", json.dumps(ring))
    execute(capi_admin_engine, demo_session.tenant_id, attempt)
    state = auth_client.get(f"/v1/reserve/payments/{attempt}").json()
    assert state["allocation"] == "HELD" and state["order_id"] is None, state
    r = auth_client.post(
        f"/v1/reserve/simulator/{attempt}",
        headers={**scenario_headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"outcome": "captured"},
    )
    assert r.status_code == 200
    execute(capi_admin_engine, demo_session.tenant_id, attempt)
    state = auth_client.get(f"/v1/reserve/payments/{attempt}").json()
    assert state["allocation"] == "SPENT", state


def test_duplicate_nonce_with_fresh_jti_refused(
    auth_client, capi_kernel_engine, capi_admin_engine, demo_session
):
    from commerce_domain import canonicalize
    from jwcrypto.jwk import JWK
    from jwcrypto.jws import JWS
    from transaction_kernel.reserve_proofs import TYPE

    auth = permission(auth_client)
    claims = verify_artifact(artifact(capi_admin_engine, auth))
    claims["jti"] = str(uuid.uuid4())
    claims["authority_id"] = str(uuid.uuid4())
    key = JWK.from_json(os.environ["RESERVE_PROVIDER_SIGNING_JWK"])
    token = JWS(canonicalize(claims))
    token.add_signature(key, protected={"alg": "ES256", "kid": key["kid"], "typ": TYPE})
    with pytest.raises(DBAPIError), Session(capi_kernel_engine) as s, s.begin():
        set_tenant(s, demo_session.tenant_id)
        persist(s, token.serialize(compact=True), {"authority_id": claims["authority_id"]})


def test_proof_permissions_and_tenant_isolation(auth_client, capi_admin_engine, capi_kernel_engine):
    auth = permission(auth_client)
    with capi_admin_engine.begin() as c:
        for role in ("commerce_test_app", "commerce_test_worker", "commerce_test_kernel"):
            for privilege in ("UPDATE", "DELETE", "INSERT"):
                permitted = c.execute(
                    text("SELECT has_table_privilege(:r,'verified_authority_proofs',:p)"),
                    {"r": role, "p": privilege},
                ).scalar_one()
                assert permitted == (role == "commerce_test_kernel" and privilege == "INSERT")
    with Session(capi_kernel_engine) as s, s.begin():
        set_tenant(s, uuid.uuid4())
        assert (
            s.execute(
                text("SELECT count(*) FROM verified_authority_proofs WHERE authority_id=:a"),
                {"a": auth["authority_id"]},
            ).scalar_one()
            == 0
        )


def test_bounds_changed_after_admission_are_not_sent(auth_client, demo_session, capi_admin_engine):
    auth = permission(auth_client)
    result = pay(auth_client, _card(auth_client), auth).json()
    assert result["allowed"]
    with capi_admin_engine.begin() as c:
        c.execute(
            text(
                "UPDATE delegated_authorities SET max_amount_minor=max_amount_minor+1 WHERE id=:a"
            ),
            {"a": auth["authority_id"]},
        )
    execute(capi_admin_engine, demo_session.tenant_id, result["attempt_id"])
    state = auth_client.get(f"/v1/reserve/payments/{result['attempt_id']}").json()
    assert state["allocation"] == "RELEASED" and state["order_id"] is None, state
    with capi_admin_engine.begin() as c:
        assert (
            c.execute(
                text("SELECT count(*) FROM provider_requests WHERE payment_attempt_id=:p"),
                {"p": result["attempt_id"]},
            ).scalar_one()
            == 0
        )


def test_signed_bounds_tamper_has_exact_reason_and_no_allocation(
    auth_client, capi_admin_engine, capi_kernel_engine, demo_session
):
    from transaction_kernel.authority import AuthorityReason, admit_debit, check_authority

    auth = permission(auth_client)
    aid = uuid.UUID(auth["authority_id"])
    from test_capi_approve_and_pay import MILK

    args = {
        "expected_epoch": auth["epoch"],
        "amount": Money(100, "INR"),
        "merchant_id": demo_session.merchant_id,
        "buyer_ref": demo_session.buyer_ref,
        "product_skus": frozenset({MILK}),
    }
    with Session(capi_kernel_engine) as session, session.begin():
        set_tenant(session, demo_session.tenant_id)
        assert check_authority(session, aid, **args).code.value == "OK"
    with capi_admin_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE delegated_authorities SET max_amount_minor=max_amount_minor+1 WHERE id=:a"
            ),
            {"a": aid},
        )
    with Session(capi_kernel_engine) as session, session.begin():
        set_tenant(session, demo_session.tenant_id)
        decision = admit_debit(session, aid, **args)
        assert decision.reason is AuthorityReason.AUTHORITY_PROOF_INVALID
    with capi_admin_engine.begin() as conn:
        assert (
            conn.execute(
                text("SELECT consumed_amount_minor FROM delegated_authorities WHERE id=:a"),
                {"a": aid},
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                text("SELECT count(*) FROM payment_attempts WHERE tenant_id=:t"),
                {"t": demo_session.tenant_id},
            ).scalar_one()
            == 0
        )


def test_unique_migration_refuses_existing_duplicates_without_rewriting(
    auth_client, capi_admin_engine, demo_session
):
    import runpy
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from commerce_api.services import reserve_provider

    permission(auth_client)
    artifact_jws = reserve_provider.issue_authorization(
        tenant_id=demo_session.tenant_id,
        merchant_id=demo_session.merchant_id,
        buyer_ref=demo_session.buyer_ref,
        max_amount_minor=500000,
        per_purchase_limit_minor=50000,
        allowed_skus=None,
    )
    migration = (
        Path(__file__).resolve().parents[2] / "platform-db/migrations/versions/"
        "e8b421cc901a_one_live_reserve_permission.py"
    )
    upgrade = runpy.run_path(str(migration))["upgrade"]
    with capi_admin_engine.connect() as connection:
        transaction = connection.begin()
        try:
            # Recreate historical data only inside a transaction rolled back below.
            connection.execute(text("DROP INDEX uq_reserve_live_buyer_merchant"))
            with Session(bind=connection) as session:
                session.begin()
                set_tenant(session, demo_session.tenant_id)
                grant_authority(
                    session,
                    tenant_id=demo_session.tenant_id,
                    merchant_id=demo_session.merchant_id,
                    buyer_ref=demo_session.buyer_ref,
                    kind=AuthorityKind.RESERVE,
                    max_amount=Money(500000, "INR"),
                    per_purchase_limit=Money(50000, "INR"),
                    until_revoked=True,
                    signed_artifact=artifact_jws,
                )
                with Operations.context(MigrationContext.configure(connection)):
                    with pytest.raises(RuntimeError, match="multiple live Reserve permissions"):
                        upgrade()
                assert (
                    connection.execute(
                        text(
                            "SELECT count(*) FROM delegated_authorities "
                            "WHERE tenant_id=:t AND status='ACTIVE'"
                        ),
                        {"t": demo_session.tenant_id},
                    ).scalar_one()
                    == 2
                )
        finally:
            if transaction.is_active:
                transaction.rollback()


@pytest.mark.parametrize("days", [-1, 91])
def test_creation_rejects_invalid_expiry(auth_client, days):
    import uuid
    from datetime import UTC, datetime, timedelta

    response = auth_client.post(
        "/v1/reserve/authorities",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={
            "per_purchase_limit_minor": 10000,
            "capacity_minor": 20000,
            "expires_at": (datetime.now(UTC) + timedelta(days=days)).isoformat(),
        },
    )
    assert response.status_code == 422


def test_selected_expiry_is_signed_and_idempotent(auth_client, capi_admin_engine):
    import uuid
    from datetime import UTC, datetime, timedelta

    expiry = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    headers = {"Idempotency-Key": str(uuid.uuid4())}
    body = {"per_purchase_limit_minor": 10000, "capacity_minor": 20000, "expires_at": expiry}
    response = auth_client.post("/v1/reserve/authorities", json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["expires_at"] == expiry
    assert verify_artifact(artifact(capi_admin_engine, response.json()))["expires_at"] == expiry
    assert (
        auth_client.post("/v1/reserve/authorities", json=body, headers=headers).json()
        == response.json()
    )
