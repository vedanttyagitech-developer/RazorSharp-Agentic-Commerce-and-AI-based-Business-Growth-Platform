"""Minting a pseudonymous session, so the demonstration has an authenticated caller.

There is no sign-up in this platform and no password anywhere in it. A buyer or an agent
asks for a session against a tenant slug and receives a bearer token; the row that token
resolves to is what tells every later request which tenant, which merchant and which
buyer it belongs to (specification invariant 14: tenant identity comes from the
authenticated server context, never from an argument).

**This route does not exist in the production profile.** It is the one endpoint that
hands out authority without proving anything, which is acceptable for a demonstration
against Razorpay test mode and is not acceptable anywhere else. The guard is a 404 rather
than a 403: in production the route genuinely is not there.

The buyer reference is pseudonymous by construction. Nothing here accepts an email, a
phone number or a name, and the reference the caller may supply is a short opaque label
so a demo script can rejoin its own basket after a reload.
"""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from typing import Annotated

from commerce_domain import uuid7
from fastapi import APIRouter, Depends, Request
from platform_db import ApiSession, Merchant, Tenant, set_tenant
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from transaction_kernel import ActorType

from ..deps import CAPABILITIES_BY_ACTOR, settings_of, unbound_app_session
from ..errors import ProblemError
from ..schemas import rfc3339
from ..security import hash_token, mint_token

router = APIRouter(prefix="/v1/demo", tags=["demo"])

#: A buyer reference is a label, not a person. Restricted so it cannot smuggle an email
#: address or a name into a column that ends up in audit rows and provider receipts.
_BUYER_REF_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


class DemoSessionRequest(BaseModel):
    """Ask for a session on one tenant.

    ``actor_type`` decides the capability set, and the difference is the whole point of
    the two registries: a BUYER session may approve, reject, cancel and request refunds;
    an AGENT session may discover, build a basket, construct a checkout and submit one
    the buyer has already approved, and may not consent to anything.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_slug: str = Field(min_length=1, max_length=64)
    #: Optional: which merchant within the tenant. Defaults to the tenant's only one.
    merchant_slug: str | None = Field(default=None, max_length=64)
    actor_type: ActorType = ActorType.BUYER
    #: Optional pseudonymous label so a reloaded demo can rejoin its own basket.
    buyer_ref: str | None = Field(default=None, max_length=64)


class DemoSessionOut(BaseModel):
    """The minted session. ``token`` is shown once and stored only as a SHA-256."""

    model_config = ConfigDict(extra="forbid")

    token: str
    session_id: str
    tenant_id: str
    merchant_id: str
    buyer_ref: str
    actor_type: ActorType
    capabilities: list[str]
    expires_at: str


@router.post(
    "/sessions",
    response_model=DemoSessionOut,
    status_code=201,
    summary="Mint a pseudonymous buyer or agent session",
)
def mint_session(
    body: DemoSessionRequest,
    request: Request,
    session: Annotated[Session, Depends(unbound_app_session)],
) -> DemoSessionOut:
    """Create an ``api_sessions`` row and return its bearer token exactly once.

    Runs on :func:`commerce_api.deps.unbound_app_session` -- an app-role transaction with
    **no tenant bound** -- because the tenant is what this request is discovering. The
    tenant is bound as soon as the slug resolves, before any row-level-security-protected
    table is read.

    Expiry is computed from the database clock plus ``SESSION_TTL_SECONDS``, so a skewed
    process clock cannot mint a session that outlives its policy.
    """
    settings = settings_of(request)
    if not settings.demo_routes_enabled:
        raise ProblemError(
            404,
            "Not Found",
            "Demo sessions are not available in this profile.",
        )
    if body.actor_type not in CAPABILITIES_BY_ACTOR:
        raise ProblemError(
            422,
            "Unsupported actor type",
            f"A demo session may be minted for {', '.join(sorted(CAPABILITIES_BY_ACTOR))}.",
            actor_type=body.actor_type.value,
        )

    tenant_id = session.execute(
        select(Tenant.id).where(Tenant.slug == body.tenant_slug)
    ).scalar_one_or_none()
    if tenant_id is None:
        raise ProblemError(
            404,
            "Tenant not found",
            "No tenant has that slug.",
            tenant_slug=body.tenant_slug,
        )

    # From here on the transaction is scoped; `merchants` is under row-level security and
    # would return nothing without this.
    set_tenant(session, tenant_id)

    merchant_query = select(Merchant.id).where(Merchant.tenant_id == tenant_id)
    if body.merchant_slug is not None:
        merchant_query = merchant_query.where(Merchant.slug == body.merchant_slug)
    merchant_id = session.execute(
        merchant_query.order_by(Merchant.created_at, Merchant.id).limit(1)
    ).scalar_one_or_none()
    if merchant_id is None:
        raise ProblemError(
            404,
            "Merchant not found",
            "That tenant has no such merchant.",
            tenant_slug=body.tenant_slug,
            merchant_slug=body.merchant_slug,
        )

    buyer_ref = _buyer_ref(body.buyer_ref)
    token = mint_token()
    now = session.execute(select(func.now())).scalar_one()
    capabilities = sorted(CAPABILITIES_BY_ACTOR[body.actor_type])
    row = ApiSession(
        id=uuid7(),
        tenant_id=tenant_id,
        merchant_id=merchant_id,
        token_hash=hash_token(token),
        buyer_ref=buyer_ref,
        actor_type=body.actor_type.value,
        capabilities=capabilities,
        expires_at=now + timedelta(seconds=settings.session_ttl_seconds),
    )
    session.add(row)
    # Flushed, not committed: the request-scoped dependency owns the transaction, and the
    # flush is only so the server defaults are readable while building the response.
    session.flush()

    return DemoSessionOut(
        token=token,
        session_id=str(row.id),
        tenant_id=str(tenant_id),
        merchant_id=str(merchant_id),
        buyer_ref=buyer_ref,
        actor_type=body.actor_type,
        capabilities=capabilities,
        expires_at=rfc3339(row.expires_at),
    )


def _buyer_ref(supplied: str | None) -> str:
    """Validate a caller-supplied reference, or mint one.

    A rejected reference is a 422 rather than a silent replacement: a demo script that
    thought it was rejoining a basket and quietly got a new identity would look like data
    loss.
    """
    if supplied is None:
        return f"buyer-{uuid.uuid4().hex[:12]}"
    if not _BUYER_REF_PATTERN.match(supplied):
        raise ProblemError(
            422,
            "Invalid buyer reference",
            "buyer_ref must be 1-64 characters of letters, digits, dot, underscore, "
            "colon or hyphen. It is a pseudonymous label, never a name, email or phone "
            "number.",
            field="buyer_ref",
        )
    return supplied
