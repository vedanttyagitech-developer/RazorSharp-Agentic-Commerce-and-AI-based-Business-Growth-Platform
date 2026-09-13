"""The scenario controller: live injections for the demonstration.

ADR 0003 D11. Guarded by ``X-Scenario-Key`` and absent from the production profile.
Every injection writes a ``SCENARIO_INJECTION`` audit row and is never mixed with
organic data -- step 5's price change has to be visibly an injection, or step 7's
delta proves nothing.

**Owned by build unit F.**

Two credentials, and the difference matters
-------------------------------------------
The scenario key is declared once, on the router, so it is checked *before* anything else
on every path here -- FastAPI inserts router-level dependencies ahead of the endpoint's
own, which is why a production deployment answers 404 rather than 401 even to a request
carrying no bearer token at all. The routes genuinely do not exist there.

Every route *also* requires a session, because the key answers "may you operate this
apparatus" and not "on whose data". The tenant and merchant come from the bearer token,
exactly as they do for a buyer request (specification invariant 14), so an operator
console cannot reach into another tenant by naming one, and row-level security applies to
the controller in the same way it applies to everything else.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

import httpx
from fastapi import APIRouter, Depends, Request
from payment_adapters import EVENT_ID_HEADER, SIGNATURE_HEADER, verify_webhook_signature
from platform_db import Tenant
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ..deps import (
    AppSession,
    KernelSession,
    SessionContext,
    assert_owner,
    merchant_registry,
    require_operator,
    require_scenario_key,
    require_session,
    session_scope_for,
    settings_of,
)
from ..errors import ProblemError, decision_payload
from ..schemas import DecisionOut, rfc3339, uuid_str
from ..services import scenario_service as svc

router = APIRouter(
    prefix="/v1/scenario",
    tags=["scenario"],
    dependencies=[Depends(require_scenario_key), Depends(require_operator)],
)

__all__ = ["router"]

#: The receiver's path template (ADR D7). Named here so a replay can report
#: ``receiver_not_registered`` rather than handing an operator an opaque 404 that looks
#: like the stored event went missing.
WEBHOOK_ROUTE_TEMPLATE: Final[str] = "/webhooks/razorpay/{tenant_slug}"

#: Loopback delivery is in-process, so a slow receiver is a bug rather than a network
#: condition. Bounded anyway: this route must not be able to hang a demonstration.
LOOPBACK_TIMEOUT_SECONDS: Final[float] = 10.0

#: Only these travel with a replay. Everything else in ``headers_redacted`` is context;
#: these three are what the receiver actually verifies and dedupes on.
_REPLAYED_HEADERS: Final[frozenset[str]] = frozenset(
    {SIGNATURE_HEADER, EVENT_ID_HEADER, "content-type"}
)


# ------------------------------------------------------------------------- wire shapes


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _offer_terms(body: InjectionRequest) -> svc.OfferTerms | None:
    """Validate the offer against the kind before the controller is reached.

    Both directions matter. An ``OFFER_START`` with no offer would surface as a simulator
    error naming an internal field; an offer attached to a price change would be silently
    dropped, which during a live run reads as the controller having done something other
    than what the operator typed.
    """
    if body.kind is not svc.InjectionKind.OFFER_START:
        if body.offer is not None:
            raise ProblemError(
                422,
                "Offer terms are not accepted here",
                f"{body.kind.value} does not start an offer.",
                kind=body.kind.value,
            )
        return None
    offer = body.offer
    if offer is None:
        raise ProblemError(
            422,
            "Offer terms required",
            "OFFER_START must carry the offer it is starting.",
            kind=body.kind.value,
        )
    if (offer.percent_bp is None) == (offer.flat_minor is None):
        raise ProblemError(
            422,
            "An offer is a percentage or a flat amount",
            "Send exactly one of percent_bp and flat_minor.",
            kind=body.kind.value,
        )
    if offer.effective_to_epoch_ms <= offer.effective_from_epoch_ms:
        raise ProblemError(
            422,
            "An offer must end after it starts",
            "effective_to_epoch_ms must be greater than effective_from_epoch_ms.",
            kind=body.kind.value,
        )
    return svc.OfferTerms(
        offer_id=offer.offer_id,
        label=offer.label,
        percent_bp=offer.percent_bp,
        flat_minor=offer.flat_minor,
        effective_from_epoch_ms=offer.effective_from_epoch_ms,
        effective_to_epoch_ms=offer.effective_to_epoch_ms,
    )


class OfferBody(_Body):
    """The offer an ``OFFER_START`` puts in force.

    Exactly one of ``percent_bp`` and ``flat_minor``: an offer that is both is two offers,
    and one that is neither is not an offer. No currency field, for the same reason the
    injection has none -- it is read from the store.

    The window is what a buyer is told and what the Policy-at-Sale Receipt records. It does
    not gate the pricing: whether the store is running an offer is store state, advanced by
    this injection, because two quotes at one catalogue revision must produce one total.
    """

    offer_id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=120)
    percent_bp: int | None = Field(default=None, ge=1, le=10_000)
    flat_minor: int | None = Field(default=None, ge=1)
    effective_from_epoch_ms: int = Field(ge=0)
    effective_to_epoch_ms: int = Field(ge=0)


class InjectionRequest(_Body):
    """One merchant-state change.

    ``value`` is deliberately loose: stock units for ``STOCK_SET``, minor currency units
    for ``PRICE_SET``, a listing flag for ``AVAILABILITY_SET``. There is no ``currency``
    field, and that is not an omission -- the currency is read from the store, because a
    request that could name one could price a demo cart in the wrong unit and the total
    would look merely surprising rather than wrong.
    """

    kind: svc.InjectionKind
    sku: str | None = Field(default=None, max_length=64)
    value: bool | int | None = None
    note: str = Field(default="", max_length=200)
    #: Only for ``OFFER_START``. An offer is an identity, a label, a shape and a window,
    #: none of which fit in ``value``; refused on every other kind so a body that names an
    #: offer for a price change fails loudly rather than having it ignored.
    offer: OfferBody | None = None


class StateDeltaOut(_Body):
    """One before/after pair, in the primitives the canonical hasher accepts."""

    field: str
    before: bool | int
    after: bool | int


class InjectionOut(_Body):
    """The applied injection, its evidence, and the revision it produced.

    ``audit_payload`` is the row that was written, verbatim. It is echoed rather than
    summarised so the Inspector can show a judge the exact bytes that were chained,
    without a second round trip and without this endpoint's own idea of what mattered.
    """

    injection_id: str
    kind: str
    label: str
    sku: str | None
    note: str
    currency: str | None
    deltas: list[StateDeltaOut]
    revision_before: int
    revision_after: int
    injected_at: str
    audit_event_id: str
    scenario_run_id: str
    audit_payload: dict[str, Any]


class ReservationExpiryOut(_Body):
    """What the fast-forward did to one hold."""

    checkout_id: str
    version: int
    reservation_id: str
    status_before: str
    status_after: str | None
    expires_at: str | None
    code: str
    swept: int
    audit_event_id: str


class ReplayOut(_Body):
    """The redelivery, and the state it did not change."""

    inbox_id: str
    dedup_key: str
    provider_event_id: str | None
    event_type: str
    signature_reverified: bool
    delivered: bool
    delivery_status: int | None
    delivery_reason: str
    duplicate_confirmed: bool
    duplicate_count_before: int
    duplicate_count_after: int
    apply_status: str
    state_before: str | None
    state_after: str | None
    changed: bool | None
    audit_event_id: str


class DuplicateSubmitRequest(_Body):
    """Which version two tabs should race over."""

    checkout_id: uuid.UUID
    version: int = Field(ge=1)
    #: Optional, for reproducing a specific run. Admission re-verifies the binding.
    approval_id: uuid.UUID | None = None


class DuplicateSubmitOut(_Body):
    """Both answers, in submission order. One grant at most, by construction."""

    checkout_id: str
    version: int
    approval_id: str
    outcomes: list[DecisionOut]
    admitted_count: int
    attempt_ids: list[str]
    grant_ids: list[str]
    audit_event_id: str


class FaultRequest(_Body):
    """Arm one worker-side fault.

    ``once`` is explicit and accepts only ``true``: ``scenario_faults`` models a
    single-use fault (``armed`` plus ``consumed_at``) and has no column for a standing
    one. Silently ignoring ``once: false`` would leave an operator believing they had
    armed a fault that fires on every attempt.
    """

    kind: svc.FaultKind
    checkout_id: uuid.UUID | None = None
    payment_attempt_id: uuid.UUID | None = None
    once: bool = True


class FaultOut(_Body):
    """The armed fault, as the worker will find it."""

    fault_id: str
    kind: str
    checkout_id: str | None
    payment_attempt_id: str | None
    armed: bool
    once: bool
    created_at: str


class InvalidateOpenRequest(_Body):
    """Why this checkout is being taken out of play. A stable key, not a sentence."""

    reason: str = Field(default="SCENARIO_LATE_CAPTURE", min_length=1, max_length=64)


class InvalidateOpenOut(_Body):
    """The transition the kernel made, and the version it made it on."""

    checkout_id: str
    version: int
    content_hash: str
    from_state: str
    to_state: str
    reason: str
    audit_event_id: str


# ----------------------------------------------------------------------------- routes


@router.post(
    "/injections",
    response_model=InjectionOut,
    status_code=201,
    summary="Change merchant state, labelled as an injection",
)
def create_injection(
    body: InjectionRequest,
    request: Request,
    ctx: SessionContext,
    session: KernelSession,
) -> InjectionOut:
    """Step 5 of the demonstration, and the moment it turns on.

    Runs as the kernel role because it appends to ``audit_events``, which the app role
    cannot write -- evidence is kernel- and worker-written. The merchant-state change
    is persisted under the merchant database lock; the merchant rows, audit row
    and ``scenario_runs`` row commit with the request's single transaction,
    so there is no window in which the catalogue has moved and nothing says why.
    """
    outcome = svc.apply_injection(
        session,
        ctx,
        merchant_registry(request),
        kind=body.kind,
        sku=body.sku,
        value=body.value,
        note=body.note,
        offer=_offer_terms(body),
    )
    injection = outcome.injection
    return InjectionOut(
        injection_id=str(injection.injection_id),
        kind=injection.kind.value,
        label=injection.label,
        sku=injection.sku,
        note=injection.note,
        currency=injection.currency,
        deltas=[
            StateDeltaOut(field=delta.field, before=delta.before, after=delta.after)
            for delta in injection.deltas
        ],
        revision_before=injection.revision_before,
        revision_after=injection.revision_after,
        injected_at=rfc3339(injection.injected_at),
        audit_event_id=str(outcome.audit_event_id),
        scenario_run_id=str(outcome.scenario_run_id),
        audit_payload=dict(outcome.audit_payload),
    )


@router.post(
    "/reservations/{checkout_id}/{version}/expire",
    response_model=ReservationExpiryOut,
    summary="Expire a reservation on the database clock",
)
def expire_reservation(
    checkout_id: uuid.UUID,
    version: int,
    ctx: SessionContext,
    session: KernelSession,
) -> ReservationExpiryOut:
    """Make a hold lapse now, so the next admission denies ``RESERVATION_EXPIRED``.

    The deadline is moved, not the status. See ``scenario_service._FAST_FORWARD`` for why
    that distinction is the whole honesty of this lever.
    """
    outcome = svc.expire_reservation(session, ctx, checkout_id=checkout_id, version=version)
    return ReservationExpiryOut(
        checkout_id=str(outcome.checkout_id),
        version=outcome.version,
        reservation_id=str(outcome.reservation_id),
        status_before=outcome.status_before.value,
        status_after=None if outcome.status_after is None else outcome.status_after.value,
        expires_at=None if outcome.expires_at is None else rfc3339(outcome.expires_at),
        code=outcome.code.value,
        swept=outcome.swept,
        audit_event_id=str(outcome.audit_event_id),
    )


@router.post(
    "/webhooks/{inbox_id}/replay",
    response_model=ReplayOut,
    summary="Redeliver a stored webhook, byte for byte",
)
async def replay_webhook(
    inbox_id: uuid.UUID,
    request: Request,
    ctx: SessionContext,
    session: KernelSession,
) -> ReplayOut:
    """Re-post the exact stored bytes, with the original signature and event id.

    The point is the *dedupe*, and it is proved twice over. First the stored signature is
    re-verified against the stored ``raw_body``, which is what shows the inbox kept the
    bytes that were signed rather than a re-serialisation of them -- ``{"a":1,"b":2}`` and
    ``{"b":2,"a":1}`` mean the same thing and hash differently, so a receiver that stored
    parsed JSON could never prove this. Then the delivery is replayed into the receiver
    over loopback and the inbox claim is re-attempted, and it is PostgreSQL's unique index
    on ``(tenant_id, dedup_key)`` that refuses it.

    Declared ``async`` for one reason: the loopback is an in-process ASGI call, and
    awaiting it hands the event loop back so the receiver can actually run. The database
    work either side of that await is synchronous and short.
    """
    row = svc.load_inbox_row(session, ctx, inbox_id)
    raw_body = bytes(row.raw_body)
    headers = {str(key): str(value) for key, value in row.headers_redacted.items()}
    signature = headers.get(SIGNATURE_HEADER, "")
    secret = settings_of(request).razorpay().require_webhook_secret()
    signature_reverified = bool(signature) and verify_webhook_signature(raw_body, signature, secret)
    duplicate_count_before = row.duplicate_count

    delivered, status, reason = await _loopback(request, ctx, raw_body, headers)

    outcome = svc.record_replay(
        session,
        ctx,
        row,
        signature_reverified=signature_reverified,
        delivered=delivered,
        delivery_status=status,
        delivery_reason=reason,
        duplicate_count_before=duplicate_count_before,
    )
    return ReplayOut(
        inbox_id=str(outcome.inbox_id),
        dedup_key=outcome.dedup_key,
        provider_event_id=outcome.provider_event_id,
        event_type=outcome.event_type,
        signature_reverified=outcome.signature_reverified,
        delivered=outcome.delivered,
        delivery_status=outcome.delivery_status,
        delivery_reason=outcome.delivery_reason,
        duplicate_confirmed=outcome.duplicate_confirmed,
        duplicate_count_before=outcome.duplicate_count_before,
        duplicate_count_after=outcome.duplicate_count_after,
        apply_status=outcome.apply_status,
        state_before=outcome.state_before,
        state_after=outcome.state_after,
        changed=outcome.changed,
        audit_event_id=str(outcome.audit_event_id),
    )


async def _loopback(
    request: Request, ctx: SessionContext, raw_body: bytes, headers: dict[str, str]
) -> tuple[bool, int | None, str]:
    """Deliver the stored bytes to this process's own webhook receiver.

    Returns ``(delivered, status, reason)``. A receiver that is not mounted is reported
    as ``receiver_not_registered`` rather than as a 404, because those are different
    failures and only one of them is the operator's problem. A transport error is
    likewise reported rather than raised: the replay's evidence -- the re-verified
    signature and the refused claim -- is worth recording whether or not the receiver
    answered.
    """
    slug = _tenant_slug(request, ctx)
    if slug is None:  # pragma: no cover - the session proved this tenant exists
        return False, None, "tenant_slug_unresolved"
    registered = any(
        getattr(route, "path", None) == WEBHOOK_ROUTE_TEMPLATE for route in request.app.routes
    )
    if not registered:
        return False, None, "receiver_not_registered"

    forwarded = {key: value for key, value in headers.items() if key.lower() in _REPLAYED_HEADERS}
    forwarded.setdefault("content-type", "application/json")
    transport = httpx.ASGITransport(app=request.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://loopback", timeout=LOOPBACK_TIMEOUT_SECONDS
    ) as client:
        try:
            response = await client.post(
                f"/webhooks/razorpay/{slug}", content=raw_body, headers=forwarded
            )
        except httpx.HTTPError as exc:  # pragma: no cover - in-process transport
            return False, None, f"transport_error:{type(exc).__name__}"
    ok = 200 <= response.status_code < 300
    return ok, response.status_code, "delivered" if ok else "receiver_refused"


def _tenant_slug(request: Request, ctx: SessionContext) -> str | None:
    """The slug the receiver's route needs, read under the app role.

    ``tenants`` carries no row-level security -- resolving a slug is how a request
    discovers its tenant -- so this is a plain lookup by the id the session already
    proved, never by anything the caller supplied. It opens its own short transaction
    rather than borrowing the request's kernel session, because the kernel transaction is
    about to be read by the loopback delivery and must not be holding a statement open.
    """
    with session_scope_for(settings_of(request).database_url_app) as session:
        return session.execute(
            select(Tenant.slug).where(Tenant.id == ctx.tenant_id)
        ).scalar_one_or_none()


@router.post(
    "/duplicate-submit",
    response_model=DuplicateSubmitOut,
    summary="Fire two concurrent submits for one version",
)
def duplicate_submit(
    body: DuplicateSubmitRequest,
    request: Request,
    ctx: SessionContext,
    session: KernelSession,
) -> DuplicateSubmitOut:
    """The two-tabs story, run for real (ADR D9).

    Two transactions, two connections, one barrier, two different idempotency keys. The
    kernel resolves the race; both answers come back so the loser's
    ``CONCURRENT_OPERATION`` is visible beside the winner's Execution Grant.
    """
    # The operator controls the experiment, but cannot acquire buyer payment authority.
    buyer_authorization = request.headers.get("X-Scenario-Buyer-Authorization", "")
    scope = dict(request.scope)
    scope["headers"] = [
        (key, value) for key, value in request.scope["headers"] if key.lower() != b"authorization"
    ] + [(b"authorization", buyer_authorization.encode("latin-1"))]
    buyer = require_session(Request(scope))
    if (
        buyer.actor_type.value != "BUYER"
        or buyer.tenant_id != ctx.tenant_id
        or buyer.merchant_id != ctx.merchant_id
    ):
        raise ProblemError(
            403, "Buyer session required", "A buyer in the operator scope is required."
        )
    assert_owner(session, buyer, body.checkout_id)
    outcome = svc.duplicate_submit(
        session,
        buyer,
        merchant_registry(request),
        kernel_url=settings_of(request).database_url_kernel,
        checkout_id=body.checkout_id,
        version=body.version,
        approval_id=body.approval_id,
    )
    return DuplicateSubmitOut(
        checkout_id=str(outcome.checkout_id),
        version=outcome.version,
        approval_id=str(outcome.approval_id),
        outcomes=[decision_payload(decision) for decision in outcome.decisions],
        admitted_count=outcome.admitted_count,
        attempt_ids=[str(value) for value in outcome.attempt_ids],
        grant_ids=[str(value) for value in outcome.grant_ids],
        audit_event_id=str(outcome.audit_event_id),
    )


@router.post(
    "/faults",
    response_model=FaultOut,
    status_code=201,
    summary="Arm a single-use worker-side fault",
)
def arm_fault(body: FaultRequest, ctx: SessionContext, session: AppSession) -> FaultOut:
    """Arm a provider timeout the worker will hit on its next relevant command.

    Runs as the **app** role, alone among the routes in this module: ``scenario_faults``
    grants INSERT to the app and UPDATE to the kernel and the worker, because a fault is
    armed before the payment attempt it will hit exists.
    """
    if not body.once:
        raise ProblemError(
            422,
            "Only single-use faults are supported",
            "scenario_faults models one armed fault that is consumed once. A standing "
            "fault would need a column the schema does not have, and pretending "
            "otherwise would leave you expecting a fault on every attempt.",
            field="once",
        )
    fault = svc.arm_fault(
        session,
        ctx,
        kind=body.kind,
        checkout_id=body.checkout_id,
        payment_attempt_id=body.payment_attempt_id,
    )
    return FaultOut(
        fault_id=str(fault.id),
        kind=fault.kind,
        checkout_id=uuid_str(fault.checkout_id),
        payment_attempt_id=uuid_str(fault.payment_attempt_id),
        armed=fault.armed,
        once=True,
        created_at=rfc3339(fault.created_at),
    )


@router.post(
    "/checkouts/{checkout_id}/invalidate-open",
    response_model=InvalidateOpenOut,
    summary="Invalidate a checkout whose payment surface is still open",
)
def invalidate_open(
    checkout_id: uuid.UUID,
    body: InvalidateOpenRequest,
    ctx: SessionContext,
    session: KernelSession,
) -> InvalidateOpenOut:
    """Set up the late-capture story (specification 31.2).

    Afterwards a capture that arrives is ``STALE_CAPTURE``: no order is written, and
    exactly one automatic refund is admitted. The reservation is kept on purpose -- stock
    that was in fact paid for must not be resold before that refund lands.
    """
    outcome = svc.invalidate_open_checkout(
        session, ctx, checkout_id=checkout_id, reason=body.reason
    )
    return InvalidateOpenOut(
        checkout_id=str(outcome.checkout_id),
        version=outcome.version,
        content_hash=outcome.content_hash,
        from_state=outcome.from_state.value,
        to_state=outcome.to_state.value,
        reason=outcome.reason,
        audit_event_id=str(outcome.audit_event_id),
    )
