"""The conversational surface: one turn in, one attributable record out.

Three routes, and what each one refuses to be:

``POST /v1/agent/turn`` and ``POST /v1/merchant/agent/turn`` run one turn of the buyer or
merchant copilot. They are **not money mutations**, so there is no ``Idempotency-Key`` and
no kernel transaction: the handler holds an app-role read session and physically cannot
write ``payment_attempts``, ``execution_grants`` or any other financial table. A turn that
wants to change state produces a *proposal* the trusted surface executes through the
mutation endpoints, each of which carries its own key and its own kernel admission.

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

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from ..deps import AppSession, SessionContext, merchant_registry
from ..merchants import MerchantRegistry
from ..services import agent_service
from ..services.agent_service import Copilot, Specialist, TurnResult, TurnRunner

router = APIRouter(tags=["agent"])

Registry = Annotated[MerchantRegistry, Depends(merchant_registry)]

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
    basket_id: uuid.UUID | None = None
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
    search page, a basket, a checkout, an order, a metrics block -- under a ``kind`` key,
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


def _turn_out(result: TurnResult) -> TurnOut:
    return TurnOut(
        reply=result.reply,
        language=result.language.value,
        specialist=result.specialist,
        routing_reason=result.routing_reason,
        principal_id=result.principal_id,
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
    )


def _run(
    request: Request,
    body: TurnRequest,
    ctx: SessionContext,
    session: AppSession,
    registry: MerchantRegistry,
    copilot: Copilot,
) -> TurnOut:
    agent_service.copilot_for(ctx, copilot)
    result = agent_service.run_turn(
        session,
        ctx,
        registry,
        copilot=copilot,
        message=body.message,
        locale=body.locale,
        basket_id=body.basket_id,
        checkout_id=body.checkout_id,
        order_id=body.order_id,
        runner=_runner(request),
    )
    return _turn_out(result)


@router.post(
    "/v1/agent/turn",
    response_model=TurnOut,
    summary="One turn of the buyer copilot",
)
def buyer_turn(
    body: TurnRequest,
    request: Request,
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
    return _run(request, body, ctx, session, registry, Copilot.BUYER)


@router.post(
    "/v1/merchant/agent/turn",
    response_model=TurnOut,
    summary="One turn of the merchant copilot",
)
def merchant_turn(
    body: TurnRequest,
    request: Request,
    ctx: SessionContext,
    session: AppSession,
    registry: Registry,
) -> TurnOut:
    """The merchant harness. An OPERATOR session only; a buyer session is 403.

    Tenant and merchant come from the session (specification 6.5), never from the
    message. The specialists here read counts and propose; nothing on this route can
    change a price, a stock figure, a fee or a refund rule.
    """
    return _run(request, body, ctx, session, registry, Copilot.MERCHANT)


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
    copilot = Copilot.MERCHANT if ctx.actor_type.value == "OPERATOR" else Copilot.BUYER
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
