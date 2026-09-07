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
from merchant_sim.kernel_adapter import RevalidationError
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


def test_every_recovery_code_has_a_status_and_a_sentence() -> None:
    """A code the API can answer with must map to a status **and** reach every surface.

    A code is the join between four modules that never import each other: the kernel
    declares it, this service turns it into an HTTP status, the agent runtime renders it as
    text and the voice runtime renders it as speech. Any one of them can be updated alone,
    and when one is not, nothing fails -- the code simply arrives somewhere with no entry
    waiting for it. That is how a dead merchant connector came to be reported as **409
    Conflict** with the Python class name ``RevalidationError`` in a ``title`` field: the
    failure had no code at all, so ``status_for`` fell through to its "understood and
    declined" default and there was nothing for any renderer to look up.

    Every one of those packages already proves its own table total over the enum, and all
    of those tests stay -- a package has to be able to check itself without importing a
    sibling. What none of them can see is the join, and the join is where the defect lives.
    This test is the one place that sees it, and it lives in ``commerce-api`` because this
    is the process that puts a code on the wire: if a code is answerable here, it has to be
    answerable everywhere it lands.

    **Adding a surface means adding it to this list.** The list is the claim; a surface
    missing from it is a surface this test silently exempts. The voice runtime was missing
    from the first version of this test and a code added under it went out with no spoken
    sentence, which is the same defect one layer over.

    The failure names the offending codes and the surface that lacks them, because a code
    is added by one person and rendered by another, and "which one, where" is the entire
    content of the message.
    """
    # These imports are deliberately local. This is the only test in the repository that
    # reaches across every package that consumes a RecoveryCode, and confining the reach to
    # one function is what keeps it a stated exception rather than a habit.
    from agent_runtime.language import Language
    from agent_runtime.rendering import RECOVERY_TEXT
    from voice_runtime.tts.templates import Locale, render_decision

    def _spoken(code: RecoveryCode, locale: Locale) -> str:
        """The voice sentence, or "" when there is no template for the code.

        ``explanation`` is a key no reason override uses, so the code's own template is the
        one consulted; a decision whose reason had its own sentence would prove nothing
        about the code. A missing template surfaces as ``LookupError`` from the module's
        own table, and is reported as an absence rather than raised out of the test.

        ``OK`` is the one code that has to be probed as an allowed decision, because
        ``KernelDecision`` refuses to be denied and carry it -- and an allowed decision must
        name the grant it issued.
        """
        allowed = code is RecoveryCode.OK
        probe = KernelDecision(
            decision_id=uuid7(),
            allowed=allowed,
            code=code,
            explanation="recovery_code_coverage_probe",
            checkout=CheckoutRef(uuid7(), 1, "a" * 64),
            grant_id=uuid7() if allowed else None,
            payment_attempt_id=uuid7() if allowed else None,
        )
        try:
            return render_decision(probe, locale=locale).text
        except LookupError:
            return ""

    unmapped = sorted(code.value for code in RecoveryCode if code not in STATUS_BY_RECOVERY_CODE)
    unwritten = sorted(
        f"{code.value} ({language.value})"
        for code in RecoveryCode
        for language in Language
        if not (RECOVERY_TEXT.get(code) or {}).get(language, "").strip()
    )
    unspoken = sorted(
        f"{code.value} ({locale.value})"
        for code in RecoveryCode
        for locale in Locale
        if not _spoken(code, locale).strip()
    )

    assert not unmapped and not unwritten and not unspoken, (
        "a RecoveryCode the API can produce is not answerable end to end.\n"
        f"    no HTTP status in commerce_api.errors.STATUS_BY_RECOVERY_CODE: {unmapped}\n"
        f"    no written sentence in agent_runtime.rendering.messages: {unwritten}\n"
        f"    no spoken sentence in voice_runtime.tts.templates: {unspoken}"
    )


def test_an_unavailable_dependency_is_a_503_not_a_conflict() -> None:
    """``CONNECTOR_UNAVAILABLE`` is a 5xx, and the reason it is not a 4xx is the point.

    A 4xx says the caller sent something wrong and can send something better. When a
    merchant connector stops answering the caller sent nothing wrong, and 409 in
    particular is the status the buyer surface reads as "re-approve to continue" -- a
    remedy that cannot work, offered for a state that never changed.
    """
    assert STATUS_BY_RECOVERY_CODE[RecoveryCode.CONNECTOR_UNAVAILABLE] == 503
    assert status_for(RevalidationError("the connector did not answer")) == 503


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


def test_config_reports_deterministic_reasoning_without_vertex(client: TestClient) -> None:
    """The default process reached no model, so the mode reads deterministic-only.

    The suite runs without Vertex, so the app under test attached no bridge. The point of
    publishing this is that a process which fell back still answers every turn and looks
    agentic; ``bridged: false`` is the fact that says otherwise without reading a log. An
    empty ``specialists`` list, not a missing key, is how "no specialist is model-backed"
    is distinguished from "the question was never answered". ``model_reached`` is ``null``
    for the same reason: with no bridge attached there is no object to have proven
    anything, so this is not the ``false`` a genuine outage would report.
    """
    body = client.get("/v1/config").json()
    assert body["reasoning"] == {"bridged": False, "specialists": [], "model_reached": None}


def test_config_reports_the_bridged_specialists_when_a_bridge_is_attached(
    api_app: FastAPI, client: TestClient
) -> None:
    """A process that reached a model publishes which specialists it answers.

    ``app._attach_specialist_runner`` records the same tuple it logs; this endpoint serves
    it. The mode is set directly here rather than by standing up Vertex -- the attachment
    path has its own tests -- because what is under test is that ``/v1/config`` reports
    whatever the process decided, mode and no more (no model id, no profile detail).
    ``model_reached`` stays ``null`` in this test: it is set on the bridge OBJECT by a real
    turn, and this test never builds one -- it only sets the tuple ``/v1/config`` serves,
    which ``agent_runner`` on the test app is not.
    """
    api_app.state.reasoning_specialists = ("shopping",)
    try:
        body = client.get("/v1/config").json()
    finally:
        api_app.state.reasoning_specialists = ()
    assert body["reasoning"] == {
        "bridged": True,
        "specialists": ["shopping"],
        "model_reached": None,
    }


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


def test_minting_a_session_commits_before_it_answers() -> None:
    """The token in a 201 must name a row that is already committed.

    A FastAPI ``yield`` dependency runs its cleanup *after* the response has been sent, so
    with the default scope this endpoint returned a bearer token whose ``api_sessions`` row
    was still inside an open transaction. A caller that used the token at once -- which is
    exactly what a test fixture and the storefront proxy both do -- read through a different
    connection, found nothing, and got 401 "The bearer token is not recognised". The commit
    then landed and the identical request succeeded milliseconds later.

    That is the intermittent 401 a peer session chased across three signatures, one of them
    the same token refused and then accepted with nothing about it changed. Not expiry, not
    eviction, not a race between two mints: a response that overtook its own transaction.

    This asserts the declaration rather than the timing, and the distinction is worth stating
    plainly. The race needs a real server and a client fast enough to beat a commit; under
    ``TestClient`` the dependency's cleanup has already run by the time the response object
    reaches the test, so the bug is invisible there and a behavioural test would pass either
    way. What this catches is the regression that actually threatens -- someone removing the
    scope while tidying an annotation -- and it names the reason so they do not.
    """
    import typing

    from commerce_api.routers import demo
    from fastapi.params import Depends as DependsParam

    # ``get_type_hints(..., include_extras=True)`` rather than ``__annotations__``: the raw
    # attribute holds the unresolved annotation, so the Depends marker inside Annotated is
    # not reachable through it. Getting that wrong made this test fail against a fix that
    # was already correctly applied.
    hints = typing.get_type_hints(demo.mint_session, include_extras=True)
    found = [
        meta.scope
        for hint in hints.values()
        for meta in getattr(hint, "__metadata__", ())
        if isinstance(meta, DependsParam) and meta.scope is not None
    ]
    assert "function" in found, (
        "mint_session must take its session with Depends(..., scope='function') so the "
        "transaction commits before the token is sent; without it the 201 can name an "
        "uncommitted row and the caller's next request is a 401."
    )
