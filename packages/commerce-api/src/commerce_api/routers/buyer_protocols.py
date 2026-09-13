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
from commerce_protocols.ucp.checkout import (
    CompletionContext,
    Escalation,
    decide_completion,
    outcome_payload,
)
from commerce_protocols.ucp.lifecycle import intent_for, line_items_from
from commerce_protocols.ucp.messages import escalation_message
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

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
from ..idempotency import IdempotentReplay, idempotent_mutation, request_fingerprint
from ..merchants import MerchantRegistry
from ..services import acp_transport, cart_service, checkout_service
from ..workload import admission
from .acp import rate_limiter, serve_signed

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
    body: Basket, ctx: Buyer, key: IdempotencyKey, request: Request, registry: Registry
) -> dict[str, Any]:
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
        try:
            response = await run_in_threadpool(
                serve_signed, signed, settings, registry, rate_limiter(request)
            )
            wire = json.loads(bytes(response.body))
        except IdempotentReplay as replay:
            # The stored result is ACP wire data; preserve the buyer-facing envelope.
            wire = replay.response
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
        attempt = view.get("attempt") or {}
        payment_state = attempt.get("state")
        processing = (
            payment_state in {"AUTHORIZED", "CAPTURED", "UNKNOWN"}
            or (payment_state == "CREATED" and view["state"] == "EXECUTION_PENDING")
            or (payment_state == "SUBMITTED" and attempt.get("window_closed") is True)
        )
        outcome = decide_completion(
            CompletionContext(
                checkout_id=checkout_id,
                version=view["current_version"],
                negotiated_payment_action=False,
                asynchronous_work_in_flight=processing,
                captured_order_id=view["order_id"],
            ),
            continue_url=f"/platform/?buyerProtocol=UCP&checkout={checkout_id}",
        )
        if not view["order_id"] and payment_state in {"ESCALATED", "FAILED"}:
            review = payment_state == "ESCALATED"
            outcome = Escalation(
                continue_url=f"/platform/?buyerProtocol=UCP&checkout={checkout_id}",
                messages=(
                    escalation_message(
                        code="payment_requires_merchant_review"
                        if review
                        else "payment_requires_fresh_review",
                        content=(
                            "Payment outcome needs merchant review. Do not pay again."
                            if review
                            else (
                                "Payment failed. Refresh stock and review a fresh checkout "
                                "before paying."
                            )
                        ),
                        path="$.status",
                    ),
                ),
            )
        document = {
            "id": str(checkout_id),
            **outcome_payload(outcome),
            "order_id": view["order_id"],
        }
        if (
            not view["order_id"]
            and view["state"] in {"CANCELLED", "EXPIRED", "INVALIDATED"}
            and payment_state in {None, "FAILED", "EXPIRED"}
        ):
            document = {"id": str(checkout_id), "status": "canceled", "order_id": None}

    return {"protocol": protocol, "checkout": view, "protocol_response": document}


class CheckoutBasket(Basket):
    version: int = Field(ge=1)
    content_hash: str = Field(min_length=1, max_length=256)


@router.put("/{protocol}/checkouts/{checkout_id}")
def update_buyer_checkout(
    protocol: Literal["ACP", "UCP"],
    checkout_id: uuid.UUID,
    body: CheckoutBasket,
    ctx: Buyer,
    session: KernelSession,
    registry: Registry,
    key: IdempotencyKey,
    request: Request,
) -> dict[str, Any]:
    """Trusted buyer edit for either protocol: supersede, never reuse old consent.

    External ACP clients still cannot amend a frozen human approval. This adapter is
    authenticated as the buyer, and uses the same cart retirement rules as the shop.
    """
    unique_items(body)
    fingerprint = request_fingerprint(
        path_params={"protocol": protocol, "checkout_id": checkout_id}, body=body.model_dump()
    )
    with (
        admission(request, [(f"protocol-checkout:{ctx.tenant_id}", 20, 2)]),
        idempotent_mutation(session, ctx, key, "PROTOCOL_BUYER_UPDATE", fingerprint) as slot,
    ):
        view = checkout_service.read_checkout(session, ctx, checkout_id)
        cart = cart_service.lock_cart(session, ctx, uuid.UUID(view["cart_id"]))
        # Re-read after serializing cart writers: another tab may have superseded it.
        view = checkout_service.read_checkout(session, ctx, checkout_id)
        card = view["approval_card"]
        if not card or card["version"] != body.version or card["content_hash"] != body.content_hash:
            raise ProblemError(409, "Checkout changed", "Refresh and review the latest checkout.")
        quantities = {item.sku: item.quantity for item in body.items}
        for sku in sorted({line["sku"] for line in cart.lines} | quantities.keys()):
            cart_service.set_line(
                session, ctx, registry, cart_id=cart.id, sku=sku, quantity=quantities.get(sku, 0)
            )
        card = checkout_service.open_checkout(session, ctx, registry, cart_id=cart.id)
        result = {"protocol": protocol, "card": card, "cart_id": str(cart.id)}
        slot.store(result)
    return result
