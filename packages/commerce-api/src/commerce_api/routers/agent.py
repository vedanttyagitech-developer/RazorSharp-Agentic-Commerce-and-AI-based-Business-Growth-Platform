"""The conversational surface: one turn in, one attributable record out.

``POST /v1/agent/turn`` runs one turn of the buyer copilot. It is **not a money mutation**,
so there is no ``Idempotency-Key`` and no kernel transaction: the handler holds an app-role
read session and physically cannot write ``payment_attempts``, ``execution_grants`` or any other
financial table. A turn that wants to change state produces a *proposal* the trusted surface
executes through the mutation endpoints, each of which carries its own key and its own kernel
admission.

``GET /v1/agent/capabilities`` shows what this session's agent principal may do, per
specialist, and which verbs are absent by construction. It is the explainability half of
the capability gate: a reviewer can read the answer and then watch a denial match it.

The rule the whole module turns on: **the principal comes from the bearer session and
from nothing in the body.** The request model forbids unknown fields, so a body cannot
even name a capability; a message that asks for one is a denial inside an HTTP 200, not a
4xx, because a refusal is the system working (ADR 0003 D15). Errors -- no session, wrong
harness, an unsupported locale -- are RFC 9457 problem details through ``errors.py``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from platform_db.schema_service import Cart
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ..deps import AppSession, SessionContext, merchant_registry, settings_of
from ..merchants import MerchantRegistry
from ..services import agent_service, scenario_service
from ..services.agent_service import (
    Copilot,
    ScenarioFaultClaimer,
    Specialist,
    TurnResult,
    TurnRunner,
)

router = APIRouter(tags=["agent"])

Registry = Annotated[MerchantRegistry, Depends(merchant_registry)]

#: Names the demo failures a turn consumed, so the Voice Gateway can act on the one it
#: owns. A header rather than a field in :class:`TurnOut`, and the reason is specification
#: 31.3 rather than taste: the response body is the buyer panel's contract with
#: ``extra="forbid"``, and putting demo apparatus into it would mix injected data with
#: organic data in the exact place the specification forbids. The header is absent on
#: every organic turn and absent entirely outside the demonstration profile. No buyer
#: surface ever read it, which was the point: only the Voice Gateway acts on it.
SCENARIO_FAULT_HEADER = "X-Scenario-Fault-Fired"

#: Longest message a turn accepts. A buyer's request is a sentence or three; a pasted
#: document is a prompt-injection vector, and capping it here is the cheapest defence.
MAX_MESSAGE_CHARS = 2000


class TurnRequest(BaseModel):
    """What the panel sends. Nothing here is identity and nothing here is authority.

    The three identifiers say what the buyer is *looking at*, which is a routing input:
    a checkout in context is a checkout conversation. Ownership of each is checked by
    the tool that reads it, against the session, so naming somebody else's checkout
    here yields a not-found tool result and never their data.

    ``extra="forbid"`` is the widening test's teeth: a ``capabilities`` key, or any
    other, is a 422 before a handler runs.
    """

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    #: ``en``, ``hi`` or ``hi-Latn`` (or the ``*-IN`` locale forms). Detected when absent.
    locale: str | None = Field(default=None, max_length=16)
    cart_event_id: uuid.UUID | None = None
    cart_id: uuid.UUID | None = None
    checkout_id: uuid.UUID | None = None
    order_id: uuid.UUID | None = None


class ToolCallOut(BaseModel):
    """One chip in the panel's activity strip: what ran, in a phrase, and whether it went."""

    model_config = ConfigDict(extra="forbid")

    name: str
    summary: str
    ok: bool
    reason_key: str | None = None
    denied: bool = False


class DenialOut(BaseModel):
    """A refusal, rendered as a first-class fact rather than an error."""

    model_config = ConfigDict(extra="forbid")

    capability: str
    reason_key: str
    tool: str | None = None


class TurnOut(BaseModel):
    """The contract the buyer and merchant panels render.

    ``structured`` carries the same JSON the REST read endpoints return -- a product, a
    search page, a cart, a checkout, an order, a metrics block -- under a ``kind`` key,
    plus an optional ``proposal`` for the trusted surface to execute. Every amount in
    ``reply`` was copied from a ``display`` field in ``structured``; none was computed.
    """

    model_config = ConfigDict(extra="forbid")

    reply: str
    language: str
    specialist: Specialist
    routing_reason: str
    principal_id: str
    tool_calls: list[ToolCallOut]
    denials: list[DenialOut]
    structured: dict[str, Any] | None
    #: Every minor-unit figure a tool returned this turn: the grounding ledger, on the wire.
    #:
    #: A reply is checked against this ledger *here* before it is sent. It travels because
    #: the Voice Gateway checks it again before speaking it, and the only ledger it could
    #: reach was whatever ``structured`` happened to hold -- the last tool result. That made
    #: the speech guard stricter than this service's own proof, so a turn that read two
    #: products spoke one price and refused the sentence naming the other. A consumer may be
    #: stricter than the model; it may not be stricter than the platform.
    grounded_amounts_minor: list[int] = []
    #: True when the platform wrote this reply rather than a model.
    #:
    #: The voice gateway is the only consumer and it needs it: its speech guard checks
    #: model sentences one at a time and passes server-authored text through whole. Without
    #: this the gateway inferred "server-authored" from a decision card this endpoint never
    #: returns, so the platform's own outage sentence was guarded as though a model had
    #: written it -- and in Hindi the guard refused it, leaving the buyer with text on
    #: screen and nothing spoken.
    server_authored: bool


class SpecialistCapabilitiesOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    specialist: Specialist
    principal_id: str
    capabilities: list[str]
    tools: list[str]


class CapabilitiesOut(BaseModel):
    """What this session's agent principal may do, and what no agent may ever do."""

    model_config = ConfigDict(extra="forbid")

    copilot: Copilot
    actor_type: str
    session_capabilities: list[str]
    agent_capabilities: list[str]
    specialists: list[SpecialistCapabilitiesOut]
    absent_by_construction: list[str]


def _runner(request: Request) -> TurnRunner | None:
    """The model-backed runner, if the process configured one; else the deterministic one.

    Read from ``app.state`` so a test can install a scripted runner and so the ADK
    adapter, which needs Vertex credentials, is attached by whoever starts the server
    rather than imported here. This module imports nothing from ``google.*``.
    """
    runner = getattr(request.app.state, "agent_runner", None)
    return runner if runner is not None else None


def _claimer(request: Request) -> ScenarioFaultClaimer | None:
    """The demo failure lever, or ``None`` in any profile that has no scenario controller.

    ``None`` is the gate, not a flag the service has to read correctly: with no claimer a
    turn never queries ``scenario_faults`` at all. The same setting already makes the
    arming routes return 404, so in production there is neither a way to arm one of these
    faults nor any code that would look for one.
    """
    settings = settings_of(request)
    if not settings.scenario_routes_enabled:
        return None
    return scenario_service.TurnFaultClaimer(kernel_url=settings.database_url_kernel)


def _turn_out(result: TurnResult) -> TurnOut:
    return TurnOut(
        reply=result.reply,
        language=result.language.value,
        specialist=result.specialist,
        routing_reason=result.routing_reason,
        principal_id=result.principal_id,
        server_authored=result.server_authored,
        tool_calls=[
            ToolCallOut(
                name=call.name,
                summary=call.summary,
                ok=call.ok,
                reason_key=call.reason_key,
                denied=call.denied,
            )
            for call in result.tool_calls
        ],
        denials=[
            DenialOut(capability=d.capability, reason_key=d.reason_key, tool=d.tool)
            for d in result.denials
        ],
        structured=result.structured,
        grounded_amounts_minor=list(result.grounded_amounts_minor),
    )


def _run(
    request: Request,
    response: Response,
    body: TurnRequest,
    ctx: SessionContext,
    session: AppSession,
    registry: MerchantRegistry,
    copilot: Copilot,
) -> TurnOut:
    agent_service.copilot_for(ctx, copilot)
    # Voice and typed turns without explicit view context read the same durable cart.
    # The session supplies identity; never accept a model-selected merchant or buyer.
    cart_id = body.cart_id
    if cart_id is None and copilot is Copilot.BUYER:
        cart_id = session.execute(
            select(Cart.id)
            .where(
                Cart.tenant_id == ctx.tenant_id,
                Cart.merchant_id == ctx.merchant_id,
                Cart.buyer_ref == ctx.buyer_ref,
                Cart.status == "OPEN",
            )
            .order_by(Cart.created_at.desc(), Cart.id.desc())
            .limit(1)
        ).scalar_one_or_none()
    result = agent_service.run_turn(
        session,
        ctx,
        registry,
        copilot=copilot,
        message=body.message,
        cart_event_id=body.cart_event_id,
        locale=body.locale,
        cart_id=cart_id,
        checkout_id=body.checkout_id,
        order_id=body.order_id,
        runner=_runner(request),
        scenario=_claimer(request),
    )
    if result.scenario_faults:
        response.headers[SCENARIO_FAULT_HEADER] = ",".join(sorted(result.scenario_faults))
    return _turn_out(result)


@router.post(
    "/v1/agent/turn",
    response_model=TurnOut,
    summary="One turn of the RazorAI",
)
def buyer_turn(
    body: TurnRequest,
    request: Request,
    response: Response,
    ctx: SessionContext,
    session: AppSession,
    registry: Registry,
) -> TurnOut:
    """Route the message to a buyer specialist, run it under the session's principal.

    Serves BUYER and AGENT sessions. Either way the agent principal is the session's
    capabilities intersected with the agent surface: a BUYER session's ``checkout.approve``
    does not survive the intersection, so asking the copilot to approve is refused even
    for the buyer who could approve on the trusted surface -- consent is not delegable to
    the thing that proposed the purchase.
    """
    return _run(request, response, body, ctx, session, registry, Copilot.BUYER)


@router.post(
    "/v1/merchant/agent/turn",
    response_model=TurnOut,
    summary="One turn of the Merchant Copilot",
)
def merchant_turn(
    body: TurnRequest,
    request: Request,
    response: Response,
    ctx: SessionContext,
    session: AppSession,
    registry: Registry,
) -> TurnOut:
    """Route the message to Operations, run it under the merchant session's principal.

    Serves MERCHANT sessions and no others; a buyer reaching here would be reading a
    shop's confirmed sales and its change queue. The agent principal is the session's
    capabilities intersected with the agent surface, which is where the merchant story
    lands: a MERCHANT session holds ``merchant.action.approve`` and it does not survive
    the intersection, so the specialist can draft a change and cannot approve the change
    it drafted. That is the same rule as the buyer's ``checkout.approve``, on the other
    side of the counter.
    """
    return _run(request, response, body, ctx, session, registry, Copilot.MERCHANT)


@router.get(
    "/v1/agent/capabilities",
    response_model=CapabilitiesOut,
    summary="What this session's agent principal may do",
)
def read_capabilities(ctx: SessionContext) -> CapabilitiesOut:
    """The binding, made visible.

    Computed by the same :func:`agent_service.bind` the turn uses, so what this answers
    and what a turn enforces cannot drift. ``absent_by_construction`` lists the consent
    verbs an agent can never hold, whichever session asks.
    """
    copilot = Copilot.BUYER
    binding = agent_service.bind(ctx, copilot)
    specialists = [
        SpecialistCapabilitiesOut(
            specialist=specialist,
            principal_id=principal.principal_id,
            capabilities=sorted(principal.capabilities),
            tools=sorted(
                name
                for name, spec in agent_service.TOOLS.items()
                if specialist in spec.specialists and principal.can(spec.capability)
            ),
        )
        for specialist, principal in binding.specialists.items()
    ]
    return CapabilitiesOut(
        copilot=copilot,
        actor_type=ctx.actor_type.value,
        session_capabilities=sorted(ctx.principal.capabilities),
        agent_capabilities=sorted(binding.harness.capabilities),
        specialists=specialists,
        absent_by_construction=sorted(set(agent_service.ABSENT_VERBS.values())),
    )
