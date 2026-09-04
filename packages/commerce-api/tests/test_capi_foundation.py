"""The foundation's promises, each one asserted rather than described.

Everything here is a rule the other four build units rely on and cannot re-check for
themselves: that a wrong-role connection cannot be configured, that a denial is a 200,
that an error is a problem detail, that a retry replays instead of re-executing, and that
the demo apparatus is genuinely absent in production.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Annotated, Any

import pytest
from commerce_api.app import create_app
from commerce_api.deps import (
    IdempotencyKey,
    KernelSession,
    SessionContext,
    require_scenario_key,
)
from commerce_api.errors import (
    PROBLEM_MEDIA_TYPE,
    STATUS_BY_RECOVERY_CODE,
    ProblemError,
    decision_response,
    problem,
    status_for,
)
from commerce_api.idempotency import IDEMPOTENT_REPLAYED_HEADER, idempotent_mutation
from commerce_api.settings import Settings
from commerce_domain import uuid7
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from payment_adapters import ConfigurationError
from pydantic import ValidationError
from transaction_kernel import CheckoutRef, KernelDecision, RecoveryCode
from transaction_kernel.checkouts import CheckoutStateError, CheckoutUsageError
from transaction_kernel.idempotency import IdempotencyKeyReuseError

from conftest import (
    APP_URL,
    KERNEL_URL,
    TEST_KEY_ID,
    TEST_KEY_SECRET,
    TEST_SCENARIO_KEY,
    TEST_WEBHOOK_SECRET,
    MintedSession,
)

_BASE_SETTINGS: dict[str, Any] = {
    "PROFILE": "development",
    "DATABASE_URL_APP": APP_URL,
    "DATABASE_URL_KERNEL": KERNEL_URL,
    "RAZORPAY_KEY_ID": TEST_KEY_ID,
    "RAZORPAY_KEY_SECRET": TEST_KEY_SECRET,
    "RAZORPAY_WEBHOOK_SECRET": TEST_WEBHOOK_SECRET,
}


def _settings(**overrides: Any) -> Settings:
    return Settings(**{**_BASE_SETTINGS, **overrides})


# ------------------------------------------------------------------------- settings


@pytest.mark.parametrize("missing", ["DATABASE_URL_APP", "DATABASE_URL_KERNEL"])
def test_settings_require_both_role_urls(missing: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither role URL has a default, and neither falls back to ``DATABASE_URL``.

    ``DATABASE_URL`` is supplied here precisely so the absence of a fallback is what the
    test observes: a process configured that way must refuse to start rather than run
    every query with whatever privileges that one URL happens to carry.

    The variable is cleared from the environment first because Settings reads it from
    there as well as from its arguments. Without this the test passes or fails according
    to whether the developer happened to source .env, which is a normal thing to do and
    must not change what a test proves.
    """
    monkeypatch.delenv(missing, raising=False)
    values = {key: value for key, value in _BASE_SETTINGS.items() if key != missing}
    values["DATABASE_URL"] = APP_URL
    with pytest.raises(ValidationError) as raised:
        Settings(**values)
    assert missing in str(raised.value)


def test_settings_refuse_a_live_key_outside_production() -> None:
    """Specification 11.5, enforced by the adapter's own guard and not re-implemented.

    The refusal surfaces as the adapter's own ``ConfigurationError`` rather than a
    pydantic ``ValidationError``, on purpose: re-raising it as a ``ValueError`` so
    pydantic could wrap it would bury the one message that says exactly which rule a live
    key breaks. Either way the process does not start.
    """
    with pytest.raises(ConfigurationError) as raised:
        _settings(PROFILE="demo", RAZORPAY_KEY_ID="rzp_live_notreallyalivekey")
    assert "live key" in str(raised.value)


def test_settings_accept_a_live_key_with_production_and_an_approval() -> None:
    """The positive control: the guard is asymmetric, not simply always-no."""
    settings = _settings(
        PROFILE="production",
        RAZORPAY_KEY_ID="rzp_live_notreallyalivekey",
        RAZORPAY_PRODUCTION_APPROVAL_REF="approval-2026-09-05",
    )
    assert settings.razorpay().is_test_mode is False
    assert settings.demo_routes_enabled is False
    assert settings.scenario_routes_enabled is False


def test_settings_refuse_web_concurrency_above_one() -> None:
    """ADR 0003 D14: the merchant simulator's state lives in this process."""
    with pytest.raises(ValidationError) as raised:
        _settings(WEB_CONCURRENCY=2)
    assert "D14" in str(raised.value)


def test_settings_repr_hides_every_secret() -> None:
    rendered = repr(_settings(SCENARIO_KEY=TEST_SCENARIO_KEY))
    for secret in (TEST_KEY_SECRET, TEST_WEBHOOK_SECRET, TEST_SCENARIO_KEY, APP_URL):
        assert secret not in rendered


# --------------------------------------------------------------------------- errors


def test_problem_is_shaped_as_rfc9457() -> None:
    response = problem(409, "Reservation expired", "The hold lapsed.", code="RESERVATION_EXPIRED")
    assert response.status_code == 409
    assert response.media_type == PROBLEM_MEDIA_TYPE
    body = _json(response)
    assert body["type"] == "about:blank"
    assert body["title"] == "Reservation expired"
    assert body["status"] == 409
    assert body["detail"] == "The hold lapsed."
    assert body["code"] == "RESERVATION_EXPIRED"


def test_unknown_route_answers_with_a_problem(client: TestClient) -> None:
    response = client.get("/v1/nothing-here")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    assert response.json()["status"] == 404


def test_the_recovery_code_table_is_total() -> None:
    """Every code has a status, so a new kernel code cannot silently become a 500."""
    assert set(STATUS_BY_RECOVERY_CODE) == set(RecoveryCode)


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (
            IdempotencyKeyReuseError(
                "k", "OP", stored_operation="OP", stored_request_hash="a", offered_request_hash="b"
            ),
            422,
        ),
        (CheckoutUsageError("no_transaction", "called wrongly"), 500),
        (CheckoutStateError("wrong_state", "not in that state"), 409),
    ],
)
def test_exception_status_mapping(exception: Exception, expected: int) -> None:
    assert status_for(exception) == expected


def test_a_five_hundred_discloses_nothing(api_app: FastAPI) -> None:
    """An unclassified failure is a bare 500 problem: no message, no traceback."""

    @api_app.get("/_test/boom")
    def _boom() -> None:
        raise RuntimeError("connection string postgresql://secret@host/db")

    with TestClient(api_app, raise_server_exceptions=False) as quiet:
        response = quiet.get("/_test/boom")
    assert response.status_code == 500
    assert "secret" not in response.text
    assert "postgresql" not in response.text
    assert response.json()["title"] == "Internal Server Error"


# ------------------------------------------------------- D15: a denial is a 200


def _denial() -> KernelDecision:
    return KernelDecision(
        decision_id=uuid7(),
        allowed=False,
        code=RecoveryCode.REAPPROVAL_REQUIRED,
        explanation="merchant_state_changed",
        checkout=CheckoutRef(uuid7(), 1, "a" * 64),
        next_version=2,
    )


def test_a_kernel_denial_is_two_hundred_with_the_decision() -> None:
    """ADR 0003 D15. The single most load-bearing behaviour in this contract."""
    response = decision_response(_denial())
    assert response.status_code == 200
    body = _json(response)
    assert body["allowed"] is False
    assert body["code"] == "REAPPROVAL_REQUIRED"
    assert body["next_version"] == 2
    assert body["checkout"]["version"] == 1


def test_decision_response_carries_sibling_keys() -> None:
    response = decision_response(_denial(), extra={"outcome": "REAPPROVAL_REQUIRED"})
    assert _json(response)["outcome"] == "REAPPROVAL_REQUIRED"


def test_decision_response_refuses_to_overwrite_the_decision() -> None:
    """A wrapper may add fields; it may not restate the kernel's answer differently."""
    with pytest.raises(ValueError, match="overwrite"):
        decision_response(_denial(), extra={"allowed": True})


# ---------------------------------------------------------------- profile guards


def test_demo_sessions_do_not_exist_in_production() -> None:
    production = _settings(
        PROFILE="production",
        RAZORPAY_KEY_ID="rzp_live_notreallyalivekey",
        RAZORPAY_PRODUCTION_APPROVAL_REF="approval-2026-09-05",
    )
    with TestClient(create_app(production)) as production_client:
        response = production_client.post("/v1/demo/sessions", json={"tenant_slug": "anything"})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)


def test_the_scenario_key_guard(api_app: FastAPI, client: TestClient) -> None:
    """Absent or wrong key is a 401 where the routes exist; correct key passes."""

    @api_app.get("/_test/scenario")
    def _guarded(key: Annotated[str, Depends(require_scenario_key)]) -> dict[str, str]:
        return {"key_seen": key}

    assert client.get("/_test/scenario").status_code == 401
    assert client.get("/_test/scenario", headers={"X-Scenario-Key": "wrong"}).status_code == 401
    ok = client.get("/_test/scenario", headers={"X-Scenario-Key": TEST_SCENARIO_KEY})
    assert ok.status_code == 200


def test_scenario_routes_are_absent_in_production() -> None:
    production = _settings(
        PROFILE="production",
        RAZORPAY_KEY_ID="rzp_live_notreallyalivekey",
        RAZORPAY_PRODUCTION_APPROVAL_REF="approval-2026-09-05",
        SCENARIO_KEY=TEST_SCENARIO_KEY,
    )
    app = create_app(production)

    @app.get("/_test/scenario")
    def _guarded(key: Annotated[str, Depends(require_scenario_key)]) -> dict[str, str]:
        return {"key_seen": key}

    with TestClient(app) as production_client:
        response = production_client.get(
            "/_test/scenario", headers={"X-Scenario-Key": TEST_SCENARIO_KEY}
        )
    assert response.status_code == 404, "a correct key must not reveal the route in production"


# -------------------------------------------------------------------------- health


def test_healthz_needs_no_database(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_config_reports_facts_and_leaks_no_secret(client: TestClient) -> None:
    response = client.get("/v1/config")
    assert response.status_code == 200
    body = response.json()
    assert body["profile"] == "development"
    assert body["razorpay_mode"] == "test"
    assert body["razorpay"]["test_mode"] is True
    assert body["razorpay"]["key_id_prefix"] == "rzp_test_"
    assert body["razorpay"]["webhook_secret_configured"] is True
    for secret in (TEST_KEY_SECRET, TEST_WEBHOOK_SECRET, TEST_SCENARIO_KEY, APP_URL, KERNEL_URL):
        assert secret not in response.text
    # The full key id is a credential's identifier; only its mode prefix is published.
    assert TEST_KEY_ID not in response.text


# ------------------------------------------------------------------ authentication


def test_a_missing_bearer_token_is_a_401(api_app: FastAPI, client: TestClient) -> None:
    """Refused before any database access, so this needs no PostgreSQL."""

    @api_app.get("/_test/whoami")
    def _whoami(ctx: SessionContext) -> dict[str, str]:
        return {"buyer_ref": ctx.buyer_ref}

    response = client.get("/_test/whoami")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)


@pytest.mark.db
def test_a_minted_session_identifies_its_tenant_and_buyer(
    api_app: FastAPI, auth_client: TestClient, demo_session: MintedSession
) -> None:
    @api_app.get("/_test/whoami")
    def _whoami(ctx: SessionContext) -> dict[str, str]:
        return {
            "tenant_id": str(ctx.tenant_id),
            "merchant_id": str(ctx.merchant_id),
            "buyer_ref": ctx.buyer_ref,
            "actor_type": ctx.actor_type.value,
        }

    body = auth_client.get("/_test/whoami").json()
    assert body["tenant_id"] == str(demo_session.tenant_id)
    assert body["merchant_id"] == str(demo_session.merchant_id)
    assert body["buyer_ref"] == demo_session.buyer_ref
    assert body["actor_type"] == "BUYER"


@pytest.mark.db
def test_an_unknown_bearer_token_is_a_401(api_app: FastAPI, client: TestClient) -> None:
    @api_app.get("/_test/whoami")
    def _whoami(ctx: SessionContext) -> dict[str, str]:
        return {"buyer_ref": ctx.buyer_ref}

    response = client.get("/_test/whoami", headers={"Authorization": "Bearer not-a-real-token"})
    assert response.status_code == 401


@pytest.mark.db
def test_an_agent_session_holds_no_approval_capability(
    client: TestClient, seeded_tenant: Any
) -> None:
    """Registry A and Registry B are separate. Consent is not delegable to the proposer."""
    minted = {
        actor: client.post(
            "/v1/demo/sessions",
            json={"tenant_slug": seeded_tenant.tenant_slug, "actor_type": actor},
        ).json()["capabilities"]
        for actor in ("BUYER", "AGENT")
    }
    assert "checkout.approve" in minted["BUYER"]
    assert "checkout.approve" not in minted["AGENT"]
    assert "checkout.reject" not in minted["AGENT"]
    assert "refund.request" not in minted["AGENT"]
    # An agent may still propose and submit what the buyer already approved.
    assert "checkout.submit_approved" in minted["AGENT"]


# -------------------------------------------------------------------- idempotency


@pytest.fixture
def mutation_probe(api_app: FastAPI) -> FastAPI:
    """A route that performs a guarded mutation and returns a body containing a fresh id.

    The id is the instrument: if a second request returns the *same* id, the stored
    response was replayed; if it returns a new one, the work ran twice.
    """

    @api_app.post("/_test/mutate")
    def _mutate(
        body: dict[str, Any],
        ctx: SessionContext,
        session: KernelSession,
        key: IdempotencyKey,
    ) -> dict[str, Any]:
        with idempotent_mutation(session, ctx, key, "TEST_MUTATION", body) as slot:
            response: dict[str, Any] = {"echo": body, "execution_id": str(uuid7())}
            slot.store(response)
        return response

    return api_app


@pytest.mark.db
def test_the_same_key_and_payload_replays_the_stored_response(
    mutation_probe: FastAPI,  # noqa: ARG001 - requested to register the probe route
    auth_client: TestClient,
) -> None:
    key = {"Idempotency-Key": f"k-{uuid.uuid4().hex}"}
    body = {"amount_minor": 39500, "currency": "INR"}

    first = auth_client.post("/_test/mutate", json=body, headers=key)
    assert first.status_code == 200
    assert IDEMPOTENT_REPLAYED_HEADER not in first.headers

    second = auth_client.post("/_test/mutate", json=body, headers=key)
    assert second.status_code == 200
    assert second.headers[IDEMPOTENT_REPLAYED_HEADER] == "true"
    assert second.json() == first.json(), "a replay returns the stored body, byte for byte"


@pytest.mark.db
def test_the_same_key_with_a_different_payload_is_422(
    mutation_probe: FastAPI,  # noqa: ARG001 - requested to register the probe route
    auth_client: TestClient,
) -> None:
    """ADR 0003 D9. Nothing executes, and nothing about the stored result is disclosed."""
    key = {"Idempotency-Key": f"k-{uuid.uuid4().hex}"}
    first = auth_client.post(
        "/_test/mutate", json={"amount_minor": 39500, "currency": "INR"}, headers=key
    )
    assert first.status_code == 200

    second = auth_client.post(
        "/_test/mutate", json={"amount_minor": 395000, "currency": "INR"}, headers=key
    )
    assert second.status_code == 422
    assert second.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    assert "39500" not in second.text or "395000" in second.text
    assert first.json()["execution_id"] not in second.text


@pytest.mark.db
def test_a_mutation_without_an_idempotency_key_is_400(
    mutation_probe: FastAPI,  # noqa: ARG001 - requested to register the probe route
    auth_client: TestClient,
) -> None:
    response = auth_client.post("/_test/mutate", json={"amount_minor": 1})
    assert response.status_code == 400
    assert response.json()["header"] == "Idempotency-Key"


@pytest.mark.db
def test_two_buyers_may_use_the_same_key(
    mutation_probe: FastAPI,  # noqa: ARG001 - requested to register the probe route
    mint_client: Callable[..., tuple[TestClient, MintedSession]],
) -> None:
    """Keys are scoped to the buyer that supplied them, so ``"1"`` is not a collision."""
    shared = {"Idempotency-Key": "1"}
    body = {"amount_minor": 100, "currency": "INR"}
    first_client, _ = mint_client(buyer_ref="buyer-one")
    second_client, _ = mint_client(buyer_ref="buyer-two")

    first = first_client.post("/_test/mutate", json=body, headers=shared)
    second = second_client.post("/_test/mutate", json=body, headers=shared)
    assert first.status_code == second.status_code == 200
    assert IDEMPOTENT_REPLAYED_HEADER not in second.headers
    assert first.json()["execution_id"] != second.json()["execution_id"]


# ---------------------------------------------------------------------- assembly


def test_every_router_is_included(client: TestClient) -> None:
    """The ROUTERS list is what the app serves; a stub contributes no paths yet."""
    paths = set(client.get("/openapi.json").json()["paths"])
    assert {"/healthz", "/v1/config", "/v1/demo/sessions"} <= paths


def test_the_openapi_document_is_three_one(client: TestClient) -> None:
    assert client.get("/openapi.json").json()["openapi"].startswith("3.1")


def test_problem_error_reaches_the_client(api_app: FastAPI, client: TestClient) -> None:
    @api_app.get("/_test/refuse")
    def _refuse() -> None:
        raise ProblemError(409, "Refused", "Because.", code="POLICY_EXCEPTION")

    response = client.get("/_test/refuse")
    assert response.status_code == 409
    body = response.json()
    assert body["title"] == "Refused"
    assert body["code"] == "POLICY_EXCEPTION"
    assert body["instance"] == "/_test/refuse"


def _json(response: Any) -> dict[str, Any]:
    """Decode a ``JSONResponse`` built outside the request cycle."""
    import json

    decoded: dict[str, Any] = json.loads(response.body)
    return decoded
