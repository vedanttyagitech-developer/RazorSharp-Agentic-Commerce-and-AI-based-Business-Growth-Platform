"""The conversational surface's promises, each asserted over the real database.

What is proven here, and why each one is a rule rather than a feature:

* a turn answers with a reply, a specialist, a deterministic routing reason and a
  tool-call log whose entries name real reads that ran under this session;
* a request cannot widen its own capabilities: a body naming one is 422 before a handler
  runs, a bound specialist never holds a capability the session lacks, and a message
  asking for consent is a denial in a 200 with no tool run;
* a buyer session cannot reach the merchant harness, and an operator session can;
* a turn writes nothing financial -- asserted both on the source of the two modules and
  on row counts in every financial table before and after a turn;
* a malformed body is a 422 problem detail, and no session is a 401.

The roles are ``NOSUPERUSER NOBYPASSRLS``; a superuser would make the row-count
assertion vacuous.
"""

from __future__ import annotations

import ast
import inspect
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from agent_runtime.language import Language
from commerce_api.deps import RequestContext, session_scope_for
from commerce_api.routers import agent as agent_router
from commerce_api.security import hash_token, mint_token
from commerce_api.services import agent_service
from commerce_api.services.agent_service import (
    ABSENT_VERBS,
    AGENT_SURFACE,
    SPECIALIST_ALLOWLIST,
    Copilot,
    Route,
    Specialist,
    ToolExecutor,
    TurnInput,
    TurnOutcome,
    bind,
    route,
)
from commerce_domain import uuid7
from fastapi import FastAPI
from fastapi.testclient import TestClient
from platform_db import FINANCIAL_TABLES
from sqlalchemy import Engine, text
from transaction_kernel import ActorType, AgentPrincipal

from conftest import MintedSession, SeededTenant

pytestmark = pytest.mark.db

MILK = "AMUL-DAIRY-001"
_SET_TENANT = text("SELECT set_config('app.tenant_id', :tenant_id, true)")


# --------------------------------------------------------------------------- helpers


def _key() -> str:
    return f"k-{uuid.uuid4().hex}"


def _problem(response: Any) -> dict[str, Any]:
    assert response.headers["content-type"].startswith("application/problem+json"), response.text
    body: dict[str, Any] = response.json()
    return body


@pytest.fixture
def operator(
    capi_admin_engine: Engine,
    seeded_tenant: SeededTenant,
    api_app: FastAPI,
) -> Iterator[tuple[TestClient, MintedSession]]:
    """An OPERATOR session holding the merchant Registry A capabilities.

    The demo route mints BUYER and AGENT sessions only, so the merchant session is
    written directly as the database owner -- the same way ``seeded_tenant`` writes the
    tenant. Its capability list is exactly the merchant agent set; ``bind`` must not add
    to it.
    """
    token = mint_token()
    session_id = uuid7()
    with capi_admin_engine.begin() as conn:
        conn.execute(_SET_TENANT, {"tenant_id": str(seeded_tenant.tenant_id)})
        conn.execute(
            text(
                "INSERT INTO api_sessions "
                "(id, tenant_id, merchant_id, token_hash, buyer_ref, actor_type, "
                "capabilities, expires_at) VALUES "
                "(:id, :tenant, :merchant, :hash, :ref, 'OPERATOR', "
                "CAST(:caps AS jsonb), now() + CAST(:ttl AS interval))"
            ),
            {
                "id": session_id,
                "tenant": seeded_tenant.tenant_id,
                "merchant": seeded_tenant.merchant_id,
                "hash": hash_token(token),
                "ref": "operator-demo",
                "caps": '["merchant.catalogue_health.read","merchant.inventory_anomalies.read",'
                '"merchant.checkout_metrics.read","merchant.growth_proposal.create"]',
                "ttl": str(timedelta(hours=1)),
            },
        )
    minted = MintedSession(
        token=token,
        session_id=session_id,
        tenant_id=seeded_tenant.tenant_id,
        merchant_id=seeded_tenant.merchant_id,
        buyer_ref="operator-demo",
        actor_type="OPERATOR",
    )
    with TestClient(api_app, headers=minted.auth_header) as client:
        yield client, minted


def _financial_counts(engine: Engine, tenant_id: uuid.UUID) -> dict[str, int]:
    counts: dict[str, int] = {}
    with engine.begin() as conn:
        conn.execute(_SET_TENANT, {"tenant_id": str(tenant_id)})
        for table in (*FINANCIAL_TABLES, "audit_events", "outbox_events"):
            # S608: `table` iterates a literal tuple from platform_db, never request data.
            statement = text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t")  # noqa: S608
            counts[table] = int(conn.execute(statement, {"t": tenant_id}).scalar_one())
    return counts


# ------------------------------------------------------------------ a turn, answered


def test_turn_returns_reply_specialist_and_tool_log(auth_client: TestClient) -> None:
    """Step 1 through the copilot: the search ran, the reply is grounded in its hits."""
    response = auth_client.post("/v1/agent/turn", json={"message": "doodh"})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["specialist"] == "shopping"
    assert body["routing_reason"] == "default_shopping"
    assert body["language"] == "en"
    assert body["denials"] == []
    assert body["principal_id"].startswith("session:")
    assert body["principal_id"].endswith("/razorai/shopping")

    calls = body["tool_calls"]
    assert [call["name"] for call in calls] == ["catalog.search"]
    assert calls[0]["ok"] is True
    assert calls[0]["summary"].startswith("searched catalogue: doodh")

    structured = body["structured"]
    assert structured["kind"] == "products"
    assert structured["hits"], structured
    # Every figure in the sentence is a display string copied from a hit, never computed.
    for hit in structured["hits"]:
        assert hit["unit_price"]["display"] in body["reply"]
        assert hit["display_name"] in body["reply"]


def test_turn_proposes_a_basket_line_only_for_a_sku_a_tool_returned(
    auth_client: TestClient,
) -> None:
    """A write is a proposal for the trusted surface, and it names a grounded SKU.

    The message names a SKU, so the product read is the grounding step; the proposal
    that follows carries that SKU and the parsed quantity, capped at the basket service's
    own limit, and executes nowhere on this route.
    """
    response = auth_client.post(
        "/v1/agent/turn", json={"message": f"add 2 {MILK} please", "locale": "en"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["specialist"] == "shopping"
    assert [call["name"] for call in body["tool_calls"]] == ["catalog.get_product"]
    proposal = body["structured"]["proposal"]
    assert proposal == {
        "action": "basket.update",
        "sku": MILK,
        "quantity": 2,
        "basket_id": None,
        "executes_on": "trusted_surface",
        "display": {"quantity": 2, "name": body["structured"]["product"]["display_name"]},
    }


def test_turn_routes_by_what_the_buyer_is_looking_at(auth_client: TestClient) -> None:
    """Structured intent outranks the lexicon; an unknown checkout is a not-found tool
    result inside a 200, never somebody else's checkout and never a 404 turn."""
    checkout_id = str(uuid7())
    response = auth_client.post(
        "/v1/agent/turn",
        json={"message": "how does this look", "checkout_id": checkout_id, "locale": "en"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["specialist"] == "checkout"
    assert body["routing_reason"] == "checkout_in_context"
    (call,) = body["tool_calls"]
    assert call["name"] == "checkout.read"
    assert call["ok"] is False
    assert call["reason_key"] == "not_found"
    assert body["structured"] is None


def test_language_is_the_harness_decision(auth_client: TestClient) -> None:
    """An explicit locale wins over detection; an unsupported one is refused, not defaulted."""
    hindi = auth_client.post("/v1/agent/turn", json={"message": "milk", "locale": "hi-IN"})
    assert hindi.status_code == 200, hindi.text
    assert hindi.json()["language"] == "hi"

    refused = auth_client.post("/v1/agent/turn", json={"message": "milk", "locale": "mr-IN"})
    assert refused.status_code == 422
    assert _problem(refused)["field"] == "locale"


# ------------------------------------------------------- a request cannot widen itself


def test_body_naming_a_capability_is_refused_before_any_handler(auth_client: TestClient) -> None:
    """The request model forbids unknown fields, so authority cannot even be asked for."""
    response = auth_client.post(
        "/v1/agent/turn",
        json={"message": "milk", "capabilities": ["checkout.approve"]},
    )
    assert response.status_code == 422
    problem = _problem(response)
    assert problem["title"] == "Request validation failed"
    assert any(err["loc"][-1] == "capabilities" for err in problem["errors"])


def test_asking_for_consent_is_a_denial_in_a_200_and_no_tool_runs(
    mint_client: Callable[..., tuple[TestClient, MintedSession]],
) -> None:
    """An AGENT session lacks ``checkout.approve``; asking is refused and nothing ran.

    A 200 rather than a 4xx because the refusal is the platform working (ADR 0003
    D15). The tool log carries the denial as its only entry, so the panel can show it as
    a first-class chip, and ``ok`` on that entry is False.
    """
    agent, _ = mint_client(actor_type="AGENT")
    response = agent.post(
        "/v1/agent/turn", json={"message": "approve and pay for my checkout", "locale": "en"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["specialist"] == "checkout"
    assert body["denials"] == [
        {"capability": "checkout.approve", "reason_key": "not_on_agent_surface", "tool": None}
    ]
    assert all(call["denied"] and not call["ok"] for call in body["tool_calls"])
    assert body["structured"] is None
    assert body["reply"]


def test_a_buyer_session_cannot_delegate_its_own_consent(auth_client: TestClient) -> None:
    """The BUYER session *holds* ``checkout.approve``; the agent bound from it does not."""
    capabilities = auth_client.get("/v1/agent/capabilities")
    assert capabilities.status_code == 200, capabilities.text
    body = capabilities.json()
    assert "checkout.approve" in body["session_capabilities"]
    assert "checkout.approve" not in body["agent_capabilities"]
    assert "checkout.approve" in body["absent_by_construction"]
    for specialist in body["specialists"]:
        assert set(specialist["capabilities"]) <= set(body["session_capabilities"])
        assert set(specialist["capabilities"]) <= AGENT_SURFACE

    turn = auth_client.post("/v1/agent/turn", json={"message": "approve it", "locale": "en"})
    assert turn.status_code == 200
    assert turn.json()["denials"][0]["capability"] == "checkout.approve"


def test_binding_never_widens_and_widening_raises() -> None:
    """Every bound specialist is a subset of the session, and the kernel type refuses
    a superset -- the intersection is computed and then checked, not merely computed."""
    session = uuid7()
    principal = AgentPrincipal(
        principal_id=f"session:{session}",
        tenant_id=uuid7(),
        actor_type=ActorType.AGENT,
        capabilities=frozenset({"catalogue.read"}),
    )
    ctx = RequestContext(
        tenant_id=principal.tenant_id,
        merchant_id=uuid7(),
        buyer_ref="b",
        principal=principal,
        correlation_id=uuid7(),
        session_id=session,
        expires_at=datetime.now(tz=UTC),
    )
    binding = bind(ctx, Copilot.BUYER)
    assert binding.harness.capabilities == frozenset({"catalogue.read"})
    for specialist, bound in binding.specialists.items():
        assert bound.capabilities <= principal.capabilities
        assert bound.capabilities <= SPECIALIST_ALLOWLIST[specialist]
        assert bound.delegation_chain == (principal.principal_id, binding.harness.principal_id)
    with pytest.raises(ValueError, match="would gain capabilities"):
        binding.harness.subset_for("rogue", frozenset({"catalogue.read", "checkout.approve"}))


def test_absent_verbs_are_outside_the_agent_surface() -> None:
    """The structural claim: no capability a consent verb needs is one an agent can hold,
    and no tool in the table requires one."""
    for capability in ABSENT_VERBS.values():
        assert capability not in AGENT_SURFACE
        assert all(spec.capability != capability for spec in agent_service.TOOLS.values())
    assert {"checkout.approve", "checkout.reject", "authority.revoke"} <= set(ABSENT_VERBS.values())


def test_tool_executor_gates_unknown_tools_and_missing_capabilities(
    api_app: FastAPI, demo_session: MintedSession
) -> None:
    """The executor is the only path to a service, and each gate answers with a result.

    Built directly, with a principal that holds nothing, so the assertion is about the
    gate and not about which session happened to be minted.
    """
    empty = AgentPrincipal(
        principal_id="session:test",
        tenant_id=demo_session.tenant_id,
        actor_type=ActorType.AGENT,
        capabilities=frozenset(),
    )
    ctx = RequestContext(
        tenant_id=demo_session.tenant_id,
        merchant_id=demo_session.merchant_id,
        buyer_ref=demo_session.buyer_ref,
        principal=empty,
        correlation_id=uuid7(),
        session_id=demo_session.session_id,
        expires_at=datetime.now(tz=UTC),
    )
    with session_scope_for(api_app.state.settings.database_url_app) as session:
        tools = ToolExecutor(
            session=session,
            ctx=ctx,
            registry=api_app.state.merchants,
            principal=empty,
            specialist=Specialist.SHOPPING,
            language=Language.EN,
            ledger=agent_service.TurnLedger(),
        )
        unknown = tools.call("checkout.approve")
        assert unknown.reason_key == "tool_not_registered" and not unknown.ok
        wrong_specialist = tools.call("order.track", order_id=uuid7())
        assert wrong_specialist.reason_key == "tool_not_registered"
        denied = tools.call("catalog.search", query="milk")
        assert denied.denied and denied.reason_key == "capability_missing"
        assert tools.ledger.denials[0].capability == "catalogue.read"
        assert tools.ledger.admitted == 0


# ------------------------------------------------------------- the merchant harness


def test_buyer_session_cannot_call_the_merchant_endpoint(auth_client: TestClient) -> None:
    response = auth_client.post("/v1/merchant/agent/turn", json={"message": "how are sales"})
    assert response.status_code == 403
    problem = _problem(response)
    assert problem["title"] == "Merchant session required"
    assert problem["actor_type"] == "BUYER"


def test_agent_session_cannot_call_the_merchant_endpoint(
    mint_client: Callable[..., tuple[TestClient, MintedSession]],
) -> None:
    agent, _ = mint_client(actor_type="AGENT")
    response = agent.post("/v1/merchant/agent/turn", json={"message": "how are sales"})
    assert response.status_code == 403


def test_operator_session_runs_the_growth_specialist(
    operator: tuple[TestClient, MintedSession],
) -> None:
    """Merchant reads are counts from committed rows and are labelled synthetic."""
    client, _ = operator
    response = client.post("/v1/merchant/agent/turn", json={"message": "how are checkouts"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["specialist"] == "growth"
    assert body["routing_reason"] == "default_growth"
    assert [call["name"] for call in body["tool_calls"]] == ["merchant.checkout_metrics.read"]
    assert body["structured"]["kind"] == "checkout_metrics"
    assert body["structured"]["synthetic"] is True
    assert body["structured"]["sample_size"] == 0

    inventory = client.post("/v1/merchant/agent/turn", json={"message": "any stock anomalies?"})
    assert inventory.status_code == 200, inventory.text
    assert inventory.json()["structured"]["kind"] == "inventory_anomalies"

    denied = client.post("/v1/agent/turn", json={"message": "milk"})
    assert denied.status_code == 403
    assert _problem(denied)["title"] == "Buyer session required"


def test_operator_capabilities_are_the_merchant_surface(
    operator: tuple[TestClient, MintedSession],
) -> None:
    client, _ = operator
    body = client.get("/v1/agent/capabilities").json()
    assert body["copilot"] == "merchant"
    assert body["actor_type"] == "OPERATOR"
    assert set(body["agent_capabilities"]) == agent_service.MERCHANT_AGENT_CAPABILITIES
    assert {s["specialist"] for s in body["specialists"]} == {"growth", "case"}
    case = next(s for s in body["specialists"] if s["specialist"] == "case")
    assert case["capabilities"] == [] and case["tools"] == []


# ------------------------------------------------------------ nothing financial written


def test_agent_modules_write_no_financial_table() -> None:
    """Static half: the two modules hold no mutation and name no financial table.

    ``KernelSession`` is the only way a route in this service reaches the kernel role,
    ``idempotent_mutation`` the only way it records a mutation, and SQLAlchemy's
    ``insert``/``update``/``delete``/``text`` the only ways to write without the ORM;
    none is imported. The session methods that would flush a row are absent too.
    """
    for module in (agent_service, agent_router):
        source = inspect.getsource(module)
        imported = {
            alias.asname or alias.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom | ast.Import)
            for alias in node.names
        }
        for name in ("insert", "update", "delete", "text", "KernelSession", "idempotent_mutation"):
            assert name not in imported, f"{module.__name__} imports {name}"
        for forbidden in (
            "kernel_session",
            "session.add(",
            ".flush(",
            ".commit(",
            ".execute(text(",
            "INSERT ",
            "UPDATE ",
            "DELETE ",
            "IdempotencyKey",
        ):
            assert forbidden not in source, f"{module.__name__} contains {forbidden!r}"
        # Every statement handed to the session is a SELECT chain: walk each
        # ``.execute(...)`` argument down its ``.where(...)``/``.group_by(...)`` calls
        # and require ``select(`` at the root.
        for node in ast.walk(ast.parse(source)):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"
            ):
                continue
            statement = node.args[0]
            while isinstance(statement, ast.Call) and isinstance(statement.func, ast.Attribute):
                statement = statement.func.value
            assert isinstance(statement, ast.Call), ast.dump(node)
            assert isinstance(statement.func, ast.Name) and statement.func.id == "select", (
                f"{module.__name__} executes something other than a select: {ast.dump(node)}"
            )
    # The router's only database dependency is the app-role read session.
    assert "AppSession" in inspect.getsource(agent_router)


def test_a_turn_leaves_every_financial_table_untouched(
    auth_client: TestClient,
    capi_admin_engine: Engine,
    demo_session: MintedSession,
    mint_client: Callable[..., tuple[TestClient, MintedSession]],
) -> None:
    """Dynamic half: row counts across every financial table, plus the audit log and the
    outbox, are identical before and after turns that search, propose and get refused."""
    before = _financial_counts(capi_admin_engine, demo_session.tenant_id)
    agent, _ = mint_client(actor_type="AGENT")

    for client, message in (
        (auth_client, "doodh"),
        (auth_client, f"add 3 {MILK}"),
        (agent, "approve and pay now"),
        (auth_client, "refund my order"),
    ):
        response = client.post("/v1/agent/turn", json={"message": message})
        assert response.status_code == 200, response.text

    assert _financial_counts(capi_admin_engine, demo_session.tenant_id) == before


def test_support_turn_without_an_order_asks_for_one_and_names_no_amount(
    auth_client: TestClient,
) -> None:
    """Support over nothing verified says so; the reply carries no figure at all."""
    response = auth_client.post(
        "/v1/agent/turn", json={"message": "I want a refund", "locale": "en"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["specialist"] == "support"
    assert body["routing_reason"] == "support_cue:refund"
    assert body["tool_calls"] == []
    assert not any(ch.isdigit() for ch in body["reply"])


# -------------------------------------------------------------------- malformed, unauth


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"message": ""},
        {"message": "x" * (agent_router.MAX_MESSAGE_CHARS + 1)},
        {"message": "milk", "checkout_id": "not-a-uuid"},
        {"message": 42},
    ],
)
def test_malformed_body_is_422_problem(auth_client: TestClient, body: dict[str, Any]) -> None:
    response = auth_client.post("/v1/agent/turn", json=body)
    assert response.status_code == 422, response.text
    assert _problem(response)["title"] == "Request validation failed"


def test_turn_needs_a_session(client: TestClient) -> None:
    response = client.post("/v1/agent/turn", json={"message": "milk"})
    assert response.status_code == 401
    assert client.get("/v1/agent/capabilities").status_code == 401
    assert client.post("/v1/merchant/agent/turn", json={"message": "x"}).status_code == 401


# --------------------------------------------------------------- the runner seam


def test_a_configured_runner_is_used_but_cannot_bypass_the_executor(
    api_app: FastAPI, auth_client: TestClient
) -> None:
    """A model-backed runner installed on ``app.state`` answers the turn, and the tool
    log the response carries is the executor's ledger, not the runner's claim."""

    class Scripted:
        def run(self, turn: TurnInput, chosen: Route, tools: ToolExecutor) -> TurnOutcome:
            assert chosen.specialist is Specialist.SHOPPING
            result = tools.call("catalog.search", query=turn.message, limit=2)
            assert result.ok
            tools.call("order.track", order_id=uuid7())  # not this specialist's tool
            return TurnOutcome(reply="scripted", structured={"kind": "products", **result.payload})

    api_app.state.agent_runner = Scripted()
    try:
        response = auth_client.post("/v1/agent/turn", json={"message": "milk", "locale": "en"})
    finally:
        api_app.state.agent_runner = None
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reply"] == "scripted"
    assert [(c["name"], c["ok"], c["reason_key"]) for c in body["tool_calls"]] == [
        ("catalog.search", True, None),
        ("order.track", False, "tool_not_registered"),
    ]


def test_route_is_deterministic_and_model_free() -> None:
    """The router is a pure function of the message and the identifiers in context."""
    en = Language.EN
    assert route(TurnInput(Copilot.BUYER, "doodh", en)) == Route(
        Specialist.SHOPPING, "default_shopping"
    )
    assert route(TurnInput(Copilot.BUYER, "where is my order", en)).specialist is Specialist.SUPPORT
    assert route(TurnInput(Copilot.BUYER, "pay now", en)).reason == "checkout_cue:pay"
    assert (
        route(TurnInput(Copilot.BUYER, "pay now", en, order_id=uuid7())).reason
        == "order_in_context"
    )
    assert (
        route(TurnInput(Copilot.MERCHANT, "show the review queue", en)).specialist
        is Specialist.CASE
    )
    assert route(TurnInput(Copilot.MERCHANT, "how are sales", en)).specialist is Specialist.GROWTH
    source = inspect.getsource(agent_service)
    assert "import google" not in source and "from google" not in source


class TestSpokenQuantities:
    """A buyer who says "two" asked for two.

    The digit pattern that predates this found nothing in "add two milk", so every spoken
    request for more than one silently became a request for exactly one. Silently is the
    problem: the buyer hears a confirmation naming the right product and has no reason to
    re-count until the order arrives.
    """

    def test_digits_are_read(self) -> None:
        assert agent_service.quantity_in("add 2 AMUL-DAIRY-002") == 2
        assert agent_service.quantity_in("add 12 AMUL-DAIRY-001") == 12

    def test_number_words_are_read_in_english_hindi_and_hinglish(self) -> None:
        assert agent_service.quantity_in("add two AMUL-DAIRY-002 please") == 2
        assert agent_service.quantity_in("do AMUL-DAIRY-001 daal do") == 2
        assert agent_service.quantity_in("teen packet doodh chahiye") == 3
        assert agent_service.quantity_in("तीन दूध चाहिए") == 3
        assert agent_service.quantity_in("paanch AMUL-DAIRY-001") == 5

    def test_an_article_is_a_quantity_of_one(self) -> None:
        """ "Add a milk" has a quantity, and it is one, rather than no quantity at all."""
        assert agent_service.quantity_in("add a milk") == 1
        assert agent_service.quantity_in("add an apple") == 1

    def test_a_sku_s_own_digits_are_never_read_as_a_quantity(self) -> None:
        """The three digits ending every SKU must not be mistaken for how many were asked for."""
        assert agent_service.quantity_in("add AMUL-DAIRY-024 to my basket") == 1
        assert agent_service.quantity_in("AMUL-DAIRY-002") == 1

    def test_a_digit_wins_over_a_word(self) -> None:
        """Both present is far likelier to be "2 of the three-pack" than a contradiction."""
        assert agent_service.quantity_in("add 2 of the three pack") == 2

    def test_no_quantity_at_all_is_one(self) -> None:
        assert agent_service.quantity_in("add milk") == 1
