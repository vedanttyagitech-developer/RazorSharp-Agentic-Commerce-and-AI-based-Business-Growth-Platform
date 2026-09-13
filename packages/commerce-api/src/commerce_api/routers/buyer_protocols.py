"""Demo buyer journeys over ACP and UCP; approval stays on the trusted buyer surface.

ACP uses the signed transport. UCP maps its line items and intent into the existing
transactional cart/checkout services. Neither adapter mints buyer consent or capture.
"""

from __future__ import annotations

import json
import secrets
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import httpx
from commerce_domain import ActorType
from commerce_protocols.acp.simulator import AcpBuyerSimulator
from commerce_protocols.core.evidence import EvidenceStage, open_interaction
from commerce_protocols.core.identity import AuthenticatedCaller, principal_for
from commerce_protocols.core.pins import PINS, Protocol
from commerce_protocols.ucp.checkout import CompletionContext, decide_completion, outcome_payload
from commerce_protocols.ucp.lifecycle import intent_for, line_items_from
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from ..deps import (
    AppSession,
    IdempotencyKey,
    KernelSession,
    RequestContext,
    SessionContext,
    merchant_registry,
    settings_of,
)
from ..errors import ProblemError
from ..idempotency import idempotent_mutation, request_fingerprint
from ..merchants import MerchantRegistry
from ..services import acp_transport, cart_service, checkout_service
from ..workload import admission

router = APIRouter(prefix="/v1/buyer-protocols", tags=["buyer-protocols"])
Registry = Annotated[MerchantRegistry, Depends(merchant_registry)]


def buyer(ctx: SessionContext, request: Request) -> RequestContext:
    if not settings_of(request).demo_routes_enabled:
        raise ProblemError(404, "Not Found", "This buyer protocol harness is demo-only.")
    if ctx.actor_type is not ActorType.BUYER or not ctx.buyer_ref:
        raise ProblemError(
            403,
            "Buyer session required",
            "Approval belongs to the buyer, not the operator console.",
        )
    return ctx


Buyer = Annotated[RequestContext, Depends(buyer)]


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str = Field(min_length=1, max_length=128)
    quantity: int = Field(ge=1, le=20)


class Basket(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[Item] = Field(min_length=1, max_length=20)


def unique_items(body: Basket) -> None:
    if len({i.sku for i in body.items}) != len(body.items):
        raise ProblemError(422, "Duplicate SKU", "Use a single quantity for each product.")


@router.post("/UCP/checkouts")
def ucp_checkout(
    body: Basket,
    ctx: Buyer,
    session: KernelSession,
    registry: Registry,
    key: IdempotencyKey,
    request: Request,
) -> dict[str, Any]:
    unique_items(body)
    caller = AuthenticatedCaller(
        protocol=Protocol.UCP,
        client_id=f"demo-ucp:{ctx.buyer_ref}",
        tenant_id=ctx.tenant_id,
        merchant_id=ctx.merchant_id,
        buyer_ref=ctx.buyer_ref,
        authenticated_by="trusted_demo_buyer_session",
        correlation_id=ctx.correlation_id,
    )
    protocol_ctx = replace(ctx, principal=principal_for(caller))
    payload = {"line_items": [{"item": {"id": i.sku}, "quantity": i.quantity} for i in body.items]}
    lines = line_items_from(payload)
    intent = intent_for(
        "create_checkout",
        caller,
        announced_version=PINS[Protocol.UCP].version,
        correlation_id=ctx.correlation_id,
        arguments=payload,
    )
    fingerprint = request_fingerprint(path_params={"protocol": "UCP"}, body=payload)
    with (
        admission(request, [(f"protocol-checkout:{ctx.tenant_id}", 20, 2)]),
        idempotent_mutation(session, ctx, key, "UCP_DEMO_CHECKOUT", fingerprint) as slot,
    ):
        interaction = open_interaction(
            pin=intent.pin,
            tenant_id=ctx.tenant_id,
            principal=protocol_ctx.principal,
            correlation_id=ctx.correlation_id,
        )
        interaction.record_received(
            session,
            endpoint="demo/UCP/checkouts",
            announced_version=intent.pin.version,
            body=payload,
            headers_seen={},
        )
        interaction.record(session, EvidenceStage.AUTHENTICATED, mechanism=caller.authenticated_by)
        cart = cart_service.create_cart(session, protocol_ctx, registry)
        for sku, quantity in lines.items():
            cart_service.set_line(
                session,
                protocol_ctx,
                registry,
                cart_id=uuid.UUID(cart["cart_id"]),
                sku=sku,
                quantity=quantity,
            )
        card = checkout_service.open_checkout(
            session, protocol_ctx, registry, cart_id=uuid.UUID(cart["cart_id"])
        )
        interaction.record(
            session,
            EvidenceStage.MAPPED,
            intent=intent.kind.value,
            checkout_id=card["checkout_id"],
        )
        result = {
            "protocol": "UCP",
            "card": card,
            "cart_id": cart["cart_id"],
            "interaction_id": str(interaction.interaction_id),
            "protocol_status": "requires_escalation",
            "continue_url": f"/platform/?buyerProtocol=UCP&checkout={card['checkout_id']}",
        }
        slot.store(result)
    return result


@router.post("/ACP/checkouts")
async def acp_checkout(
    body: Basket, ctx: Buyer, key: IdempotencyKey, request: Request
) -> dict[str, Any]:
    from ..app import create_app

    unique_items(body)
    # A scoped demo client, registered and signed on the server. No secret reaches JS.
    secret = secrets.token_urlsafe(48)
    client_id = f"buyer-demo:{uuid.uuid5(ctx.merchant_id, ctx.buyer_ref or 'buyer')}"
    audience = "https://buyer-protocol-demo.invalid/acp"
    settings = settings_of(request).model_copy(
        update={
            "acp_audience": audience,
            "acp_clients": json.dumps(
                [
                    {
                        "client_id": client_id,
                        "tenant_id": str(ctx.tenant_id),
                        "merchant_id": str(ctx.merchant_id),
                        "buyer_ref": ctx.buyer_ref,
                        "signing_secret": secret,
                    }
                ]
            ),
        }
    )
    signed = AcpBuyerSimulator(
        client_id=client_id,
        signing_secret=secret.encode(),
        audience=audience,
        seed=str(uuid.uuid4()),
    ).request(
        method="POST",
        path="/acp/checkout_sessions",
        now=datetime.now(UTC),
        idempotency_key=key,
        body={
            "items": [i.model_dump() for i in body.items],
            "buyer": {"reference": ctx.buyer_ref},
            "fulfillment": {"type": "delivery"},
        },
    )
    with admission(request, [(f"protocol-checkout:{ctx.tenant_id}", 20, 2)]):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(settings)),
            base_url="https://buyer-protocol-demo.invalid",
        ) as client:
            response = await client.request(
                signed.method, signed.path, headers=dict(signed.headers), content=signed.body
            )
        if response.status_code != 200:
            raise ProblemError(
                response.status_code,
                response.json().get("title", "ACP checkout refused"),
                response.json().get("detail", "ACP refused this request."),
            )
        wire = response.json()
        checkout_id = wire["session"]["checkout"]["checkout_id"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=request.app),
            base_url="https://buyer-protocol-demo.invalid",
        ) as client:
            view = await client.get(
                f"/v1/checkouts/{checkout_id}",
                headers={"Authorization": request.headers.get("authorization", "")},
            )
        if view.status_code != 200:
            raise ProblemError(
                view.status_code,
                "Checkout read failed",
                "Retry with the same request key to recover this checkout.",
            )
        return {
            "protocol": "ACP",
            "card": view.json()["approval_card"],
            "checkout_id": checkout_id,
            "cart_id": wire["session"]["id"],
            "protocol_response": wire,
        }


@router.get("/{protocol}/checkouts/{checkout_id}")
def status(
    protocol: Literal["ACP", "UCP"],
    checkout_id: uuid.UUID,
    ctx: Buyer,
    session: AppSession,
    registry: Registry,
) -> dict[str, Any]:
    view = checkout_service.read_checkout(session, ctx, checkout_id)
    if protocol == "ACP":
        projected = acp_transport.load_session(
            session, ctx, session_id=view["cart_id"], supplied_now=frozenset()
        )
        document = (
            acp_transport.session_document(session, ctx, registry, projected) if projected else None
        )
    else:
        outcome = decide_completion(
            CompletionContext(
                checkout_id=checkout_id,
                version=view["current_version"],
                negotiated_payment_action=False,
                asynchronous_work_in_flight=False,
                captured_order_id=view["order_id"],
            ),
            continue_url=f"/platform/?buyerProtocol=UCP&checkout={checkout_id}",
        )
        document = {
            "id": str(checkout_id),
            **outcome_payload(outcome),
            "order_id": view["order_id"],
        }
        if view["state"] in {"CANCELLED", "EXPIRED"}:
            document = {"id": str(checkout_id), "status": "canceled", "order_id": None}

    return {"protocol": protocol, "checkout": view, "protocol_response": document}
