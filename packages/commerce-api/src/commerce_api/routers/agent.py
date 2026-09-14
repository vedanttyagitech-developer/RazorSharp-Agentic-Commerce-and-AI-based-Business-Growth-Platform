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
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from commerce_domain.ids import uuid7
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
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
    presentation: bool = False
    grounding_only: bool = False
    # Untrusted conversational context, never identity, facts or action authority.
    project_questions: list[Annotated[str, Field(min_length=1, max_length=2000)]] = Field(
        default_factory=list, max_length=8
    )
    tour_step: Literal["merchant", "shopping", "console"] = "merchant"
    #: ``en``, ``hi`` or ``hi-Latn`` (or the ``*-IN`` locale forms). Detected when absent.
    locale: str | None = Field(default=None, max_length=16)
    cart_event_id: uuid.UUID | None = None
    cart_id: uuid.UUID | None = None
    checkout_id: uuid.UUID | None = None
    order_id: uuid.UUID | None = None
    #: Logical instruction identity. Resent on transport retry so the retry replays
    #: the same identity; omitted for a new instruction. Validated as a UUID and
    #: echoed back; it never carries identity or authority, which stay server-side.
    operation_id: uuid.UUID | None = None
    #: Stable conversation identity across typed and spoken turns. Minted
    #: server-side when omitted and echoed back. An index into the shared
    #: conversation record, never a credential and never a permission.
    conversation_id: uuid.UUID | None = None


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
    #: The logical instruction identity, minted server-side when the request omitted
    #: it. A retry resends it; see ``TurnRequest.operation_id``.
    operation_id: str
    #: The conversation this turn joined, minted server-side when omitted.
    conversation_id: str
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

    tenant_id: str
    buyer_ref: str | None
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


def _main_agent(request: Request) -> Any | None:
    """The shared Razor AI main agent, if attached; else None (deterministic only).

    Like :func:`_runner`: read from ``app.state`` so tests install scripted doubles.
    """
    return getattr(request.app.state, "main_agent", None)


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
        operation_id=str(result.operation_id),
        conversation_id=str(result.conversation_id),
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
    from ..workload import admission

    user = ctx.buyer_ref or str(ctx.merchant_id)
    with admission(
        request,
        [
            ("agent:global", 120, 8),
            (f"agent:tenant:{ctx.tenant_id}", 60, 4),
            (f"agent:user:{ctx.tenant_id}:{user}", 20, 1),
        ],
    ):
        if (
            body.presentation
            and copilot in {Copilot.BUYER, Copilot.MERCHANT}
            and body.checkout_id is None
            and (copilot is Copilot.MERCHANT or body.grounding_only or _main_agent(request) is None)
        ):
            from ..services.project_guide import answer

            questions = [question[:2000] for question in body.project_questions]
            guide = answer(body.message, body.tour_step, questions)
            if guide is None and body.grounding_only:
                guide = {
                    "reply": (
                        "This is a commerce operation, not a project explanation. "
                        "Use the authorized shopping flow."
                    ),
                    "step": body.tour_step,
                    "sources": [],
                }
            if guide is not None:
                guide["speech_text"] = guide["reply"]
                binding = agent_service.bind(ctx, copilot)
                return TurnOut(
                    reply=guide["reply"],
                    language="en",
                    specialist=Specialist.OPERATIONS
                    if copilot is Copilot.MERCHANT
                    else Specialist.SHOPPING,
                    routing_reason="project_knowledge",
                    principal_id=binding.principal_for(
                        Specialist.OPERATIONS
                        if copilot is Copilot.MERCHANT
                        else Specialist.SHOPPING
                    ).principal_id,
                    operation_id=str(
                        body.operation_id if body.operation_id is not None else uuid7()
                    ),
                    conversation_id=str(
                        body.conversation_id if body.conversation_id is not None else uuid7()
                    ),
                    tool_calls=[
                        ToolCallOut(
                            name="project_knowledge",
                            summary="Read source-linked project knowledge",
                            ok=True,
                        )
                    ],
                    denials=[],
                    structured={"kind": "project_guide", **guide},
                    server_authored=not guide.get("generated", False),
                )
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
            operation_id=body.operation_id,
            conversation_id=body.conversation_id,
            main_agent=_main_agent(request),
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
    if ctx.actor_type.value == "OPERATOR":
        return CapabilitiesOut(
            tenant_id=str(ctx.tenant_id),
            buyer_ref=None,
            copilot=Copilot.CONSOLE,
            actor_type="OPERATOR",
            session_capabilities=[],
            agent_capabilities=[],
            specialists=[
                SpecialistCapabilitiesOut(
                    specialist=Specialist.OPERATIONS,
                    principal_id=f"session:{ctx.session_id}/console/operations",
                    capabilities=[],
                    tools=[],
                )
            ],
            absent_by_construction=sorted(set(agent_service.ABSENT_VERBS.values())),
        )
    copilot = Copilot.MERCHANT if ctx.actor_type.value == "MERCHANT" else Copilot.BUYER
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
        tenant_id=str(ctx.tenant_id),
        buyer_ref=ctx.buyer_ref,
        copilot=copilot,
        actor_type=ctx.actor_type.value,
        session_capabilities=sorted(ctx.principal.capabilities),
        agent_capabilities=sorted(binding.harness.capabilities),
        specialists=specialists,
        absent_by_construction=sorted(set(agent_service.ABSENT_VERBS.values())),
    )


@router.post("/v1/ops/agent/turn")
def console_turn(
    body: TurnRequest, request: Request, ctx: SessionContext, session: AppSession
) -> dict[str, Any]:
    """Operator-only, read-only conversation; demo session comes from the existing cookie."""
    from ..deps import require_operator
    from ..services.console_agent import CONSOLE_TOOLS, ConsoleTools
    from ..services.conversation import TurnRecord, shared_service
    from ..services.razor_main import MainAgentError
    from ..workload import admission

    require_operator(ctx)
    conversation_id = body.conversation_id or uuid.uuid5(ctx.session_id, "console")
    service = shared_service()
    service.claim(
        conversation_id,
        tenant_id=ctx.tenant_id,
        owner=f"{ctx.session_id}:OPERATOR:{ctx.merchant_id}:console",
    )
    view = service.view(conversation_id, tenant_id=ctx.tenant_id, actor_type="OPERATOR")
    with admission(
        request,
        [
            ("agent:global", 120, 8),
            (f"agent:tenant:{ctx.tenant_id}", 60, 4),
            (f"agent:operator:{ctx.session_id}", 20, 1),
        ],
    ):
        model = _main_agent(request)
        if model is None:
            reply = (
                "The reasoning service is unavailable. "
                "Console evidence remains available in its panels."
            )
        else:
            try:
                result = model.run(
                    message=body.message,
                    language="en",
                    tools=ConsoleTools(session, ctx),
                    tool_names=CONSOLE_TOOLS,
                    history=service.history_text(view),
                    context={"surface": "console"},
                )
                reply = result.reply
            except MainAgentError:
                reply = (
                    "I could not complete this explanation. "
                    "Please inspect the console evidence panels."
                )
    operation_id = body.operation_id or uuid7()
    service.record_turn(
        conversation_id,
        tenant_id=ctx.tenant_id,
        actor_type="OPERATOR",
        turn=TurnRecord(
            turn_id=uuid7(),
            operation_id=operation_id,
            message=body.message,
            reply=reply,
            language="en",
            surface="console",
        ),
    )
    return {
        "reply": reply,
        "conversation_id": str(conversation_id),
        "operation_id": str(operation_id),
        "language": "en",
        "server_authored": model is None,
        "structured": {"kind": "project_guide", "reply": reply, "step": "console"},
    }


@router.post("/v1/voice/turn-stream")
async def voice_turn_stream(
    body: TurnRequest, request: Request, ctx: SessionContext
) -> StreamingResponse:
    """Stream guarded narration while the existing authenticated turn completes once."""
    import asyncio
    import json

    import httpx
    from fastapi.responses import StreamingResponse

    from ..errors import ProblemError
    from ..services.speech_stream import speech_sink

    paths = {
        "BUYER": "/v1/agent/turn",
        "MERCHANT": "/v1/merchant/agent/turn",
        "OPERATOR": "/v1/ops/agent/turn",
    }
    path = paths.get(ctx.actor_type.value)
    if path is None:
        raise ProblemError(403, "Voice session required", "Unsupported voice actor.")
    authorization = request.headers.get("authorization", "")

    async def events() -> AsyncIterator[str]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=128)
        loop = asyncio.get_running_loop()
        connected = True

        def enqueue(event: dict[str, Any]) -> None:
            if connected and not queue.full():
                queue.put_nowait(event)

        async def run() -> None:
            def publish(event: dict[str, Any]) -> None:
                loop.call_soon_threadsafe(enqueue, event)

            token = speech_sink.set(publish)
            try:
                # Existing app and endpoint own transactions, auth and workload limits.
                # No new app, alternate runner, or retry is constructed here.
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=request.app), base_url="http://internal"
                ) as client:
                    response = await client.post(
                        path,
                        json=body.model_dump(mode="json"),
                        headers={"Authorization": authorization},
                    )
                event = {
                    "type": "result",
                    "status": response.status_code,
                    "body": response.json(),
                    "scenario_faults": response.headers.get(SCENARIO_FAULT_HEADER, ""),
                }
            except Exception:
                event = {"type": "error", "detail": "Turn outcome unavailable; do not replay."}
            finally:
                speech_sink.reset(token)
            if connected:
                await queue.put(event)

        task = asyncio.create_task(run())
        # Retain the task after transport disconnect: stopping audio cannot roll back
        # or replay work already submitted to the existing application endpoint.
        active = getattr(request.app.state, "speech_stream_tasks", None)
        if active is None:
            active = request.app.state.speech_stream_tasks = set()
        active.add(task)
        task.add_done_callback(active.discard)
        try:
            while True:
                event = await queue.get()
                yield json.dumps(event, ensure_ascii=False) + "\n"
                if event["type"] in {"result", "error"}:
                    break
        finally:
            connected = False

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
