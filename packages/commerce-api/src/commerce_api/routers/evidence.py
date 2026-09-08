"""The action timeline, its SSE stream, the Money Action Proof Chain and step 11.

Step 10 of the demonstration: every claim the pitch makes has a reproducible evidence path
here. The proof chain is verified server-side and rendered link by link, so a judge can see
*which* link is missing rather than a bare "verified".

Prefix ``/v1`` because these paths hang off several nouns -- checkouts, audit streams and
merchants.

Two things about access, and both are deliberate
------------------------------------------------
**A session is required on every route, including the ones ADR 0003's catalogue marks
"scenario key".** Row-level security scopes every read to the tenant bound to the
transaction, and the only thing on this platform that establishes a tenant without being
told one is a bearer session (``deps.require_session``). Accepting a tenant from a header
or a query parameter would hand any caller holding the demo key the whole cluster, and
"the tenant comes from the authenticated session, never a request" is an invariant with no
exception worth making for an evidence viewer.

**The scenario key widens, it does not replace.** Within the session's tenant a valid
``X-Scenario-Key`` lifts the buyer-ownership restriction, which is what "session or
scenario key" is for: an operator inspecting a demo journey they did not personally check
out. Retained revenue additionally *requires* the key, because it is merchant evidence and
not a buyer-facing number.

A checkout that is neither owned nor unlocked by the key answers 404, never 403: a 403
confirms the identifier exists and turns this endpoint into an oracle for enumerating other
buyers' checkouts.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any, Final

import anyio
import transaction_kernel as tk
from commerce_protocols.core.evidence import AGGREGATE_TYPE as PROTOCOL_AGGREGATE
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from platform_db import set_tenant
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from sse_starlette.event import ServerSentEvent
from sse_starlette.sse import EventSourceResponse

from ..deps import AppSession, RequestContext, SessionContext, session_scope_for, settings_of
from ..errors import ProblemError
from ..schemas import rfc3339, uuid_str
from ..security import SCENARIO_KEY_HEADER, constant_time_equals
from ..services import proof_chain, timeline

router = APIRouter(prefix="/v1", tags=["evidence"])

#: How often the SSE stream re-reads the database, in seconds. Fast enough that "payment
#: opening" flips to "captured" while the buyer is still looking at the screen, slow
#: enough that a demo laptop is not running a query loop per viewer.
POLL_SECONDS: Final[float] = 1.0

#: Heartbeat interval. Comments, not events: an SSE comment keeps proxies and load
#: balancers from closing an idle connection without advancing any client's cursor.
HEARTBEAT_SECONDS: Final[int] = 15

#: Upper bound on one stream. A browser reconnects automatically with ``Last-Event-ID``,
#: and that reconnection is the path this design wants exercised, so the connection is
#: closed rather than held open indefinitely on a journey nobody is watching.
MAX_STREAM_SECONDS: Final[float] = 900.0

#: How long the stream keeps listening after the checkout reaches a terminal state. A
#: refund is an operation on the *attempt*, so events can still arrive after ``PAID``.
QUIET_SECONDS_AFTER_TERMINAL: Final[float] = 3.0

#: Audit aggregate types this API will verify. A closed set, because the value is a path
#: parameter and an open one would let a caller probe for stream names.
#:
#: ``protocol_interaction`` is named by its own constant rather than spelled here. It was
#: missing, and the absence was invisible from either side: two documents state that the
#: protocol layer's evidence is verifiable at this route -- ADR 0005 and the docstring of
#: :mod:`commerce_protocols.core.evidence` -- and a reviewer who followed either got a 404
#: saying the stream was not a verifiable type. The chain was real the whole time and the
#: allowlist simply never learned about it, which is the failure mode a written claim has
#: when nothing executes it.
VERIFIABLE_AGGREGATES: Final[frozenset[str]] = frozenset(
    {
        "checkout",
        "payment_attempt",
        "webhook_inbox",
        timeline.MERCHANT_AGGREGATE,
        PROTOCOL_AGGREGATE,
    }
)


# ------------------------------------------------------------------------ wire models


class _Out(BaseModel):
    """Strict responses, specification 24.1."""

    model_config = ConfigDict(extra="forbid")


class TimelineEntryOut(_Out):
    """One timeline row. The named fields are specification 26.1's list.

    ``id`` is the cursor, and it is the value a client hands back as ``Last-Event-ID``.
    """

    id: str
    occurred_at: str
    source: timeline.TimelineSource
    actor: str
    action: str
    summary: str
    correlation_id: str
    scenario_injection: bool
    checkout_version: int | None
    content_hash: str | None
    policy_version: str | None
    policy_receipt_hash: str | None
    freshness: dict[str, Any] | None
    approval_ref: str | None
    authority_epoch: int | None
    decision: dict[str, Any] | None
    grant: dict[str, Any] | None
    payment_attempt_id: str | None
    provider: dict[str, Any] | None
    reconciliation: dict[str, Any] | None
    refund: dict[str, Any] | None
    audit: dict[str, Any] | None
    details: dict[str, Any]

    @classmethod
    def of(cls, entry: timeline.TimelineEntry) -> TimelineEntryOut:
        return cls(
            id=entry.cursor,
            occurred_at=rfc3339(entry.occurred_at),
            source=entry.source,
            actor=entry.actor,
            action=entry.action,
            summary=entry.summary,
            correlation_id=str(entry.correlation_id),
            scenario_injection=entry.scenario_injection,
            checkout_version=entry.checkout_version,
            # Shorthand, not the full digest: specification 26.1 asks the timeline for a
            # version/hash shorthand and the proof chain for the whole thing.
            content_hash=entry.content_hash_short,
            policy_version=entry.policy_version,
            policy_receipt_hash=entry.policy_receipt_hash_short,
            freshness=None if entry.freshness is None else dict(entry.freshness),
            approval_ref=entry.approval_ref,
            authority_epoch=entry.authority_epoch,
            decision=None if entry.decision is None else dict(entry.decision),
            grant=None if entry.grant is None else dict(entry.grant),
            payment_attempt_id=uuid_str(entry.payment_attempt_id),
            provider=None if entry.provider is None else dict(entry.provider),
            reconciliation=None if entry.reconciliation is None else dict(entry.reconciliation),
            refund=None if entry.refund is None else dict(entry.refund),
            audit=None if entry.audit is None else dict(entry.audit),
            details=dict(entry.details),
        )


class TimelineOut(_Out):
    """The whole timeline for one checkout, oldest first."""

    checkout_id: str
    entries: list[TimelineEntryOut]
    cursor: str | None = Field(
        default=None,
        description="The newest entry's id. Pass it as Last-Event-ID to resume the stream.",
    )
    scenario_injections: int


class CheckOut(_Out):
    """One verifier check. ``applicable`` false means there was nothing yet to test."""

    name: str
    ok: bool
    applicable: bool
    detail: str


class VerdictOut(_Out):
    """The verifier's answer: how far the chain reached and whether it holds."""

    tier: proof_chain.ProofTier
    ok: bool
    checks: list[CheckOut]
    failed: list[str]


class ProofChainOut(_Out):
    """The ten links of specification 26.4, plus the verdict and the chain verifications."""

    checkout_id: str
    tenant_id: str
    merchant_id: str
    payment_attempt_id: str | None
    attempt_ids: list[str]
    links: dict[str, Any]
    verdict: VerdictOut
    audit_streams: dict[str, Any]


class AuditStreamVerificationOut(_Out):
    """``tk.verify_chain`` as a body: intact, or the first break and where it is."""

    aggregate_type: str
    aggregate_id: str
    intact: bool
    empty: bool
    length: int
    events_verified: int
    head_seq: int | None
    head_hash: str | None
    code: str
    first_break: dict[str, Any] | None


class RetainedRevenueOut(_Out):
    """Step 11. Every figure is a committed row; ``direction`` says who it protected."""

    checkout_id: str
    merchant_id: str
    currency: str
    stale_version: int | None
    stale_approved_minor: int | None
    stale_invalidated_at: str | None
    corrected_version: int | None
    corrected_total_minor: int | None
    captured_minor: int | None
    captured_from: str | None
    difference_minor: int | None
    direction: str
    refunded_minor: int
    net_retained_minor: int | None
    controlled_scenario: bool
    explanation: str


# ---------------------------------------------------------------------------- access


def scenario_key_ok(request: Request) -> bool:
    """True when a valid ``X-Scenario-Key`` accompanies this request.

    Non-raising, unlike ``deps.require_scenario_key``, because here the key *widens*
    access rather than gating a route: an absent key is the ordinary case and must fall
    through to the ownership check. The comparison is constant time, and a profile with
    the scenario apparatus disabled answers false whatever header arrives.
    """
    settings = settings_of(request)
    if not settings.scenario_routes_enabled:
        return False
    configured = settings.scenario_key
    if configured is None:
        return False
    return constant_time_equals(
        request.headers.get(SCENARIO_KEY_HEADER), configured.get_secret_value()
    )


Operator = Annotated[bool, Depends(scenario_key_ok)]


def visible_checkout(
    session: Session, ctx: RequestContext, checkout_id: uuid.UUID, *, operator: bool
) -> uuid.UUID:
    """Confirm this session may read ``checkout_id``'s evidence, or refuse with 404.

    Owned by this buyer, or unlocked by a valid scenario key within the same tenant.
    404 rather than 403 in both refusal cases -- see the module docstring.
    """
    head = timeline.head_of(session, tenant_id=ctx.tenant_id, checkout_id=checkout_id)
    if head is None or (head.buyer_ref != ctx.buyer_ref and not operator):
        raise ProblemError(
            404,
            "Checkout not found",
            "No checkout with that identifier is visible to this session.",
            checkout_id=str(checkout_id),
        )
    return head.id


def visible_attempt(
    session: Session, ctx: RequestContext, attempt_id: uuid.UUID, *, operator: bool
) -> tk.AttemptView:
    """A payment attempt is visible exactly when the checkout it belongs to is.

    Exported so the inspector router applies this rule rather than a copy of it: two
    authorization checks with one intent are two chances for them to diverge.
    """
    attempt = tk.read_attempt(session, tenant_id=ctx.tenant_id, payment_attempt_id=attempt_id)
    if attempt is None:
        raise ProblemError(
            404,
            "Payment attempt not found",
            "No payment attempt with that identifier is visible to this session.",
            payment_attempt_id=str(attempt_id),
        )
    visible_checkout(session, ctx, attempt.checkout_id, operator=operator)
    return attempt


def require_operator(operator: bool) -> None:
    """Merchant evidence is not buyer-facing. 404, so the route does not announce itself."""
    if not operator:
        raise ProblemError(404, "Not Found", "This endpoint requires the scenario operator key.")


# ---------------------------------------------------------------------------- routes


@router.get(
    "/checkouts/{checkout_id}/timeline",
    response_model=TimelineOut,
    summary="Action timeline",
)
def read_timeline(
    checkout_id: uuid.UUID,
    ctx: SessionContext,
    session: AppSession,
    operator: Operator,
) -> TimelineOut:
    """Specification 26.1's timeline for one checkout, merged and ordered.

    Read-only, on the app role, from committed rows only. Scenario injections are labelled
    rather than filtered out: hiding the demo apparatus would make an injected price change
    indistinguishable from an inventory failure, which is the one thing specification 31.3
    forbids.
    """
    visible_checkout(session, ctx, checkout_id, operator=operator)
    entries = timeline.collect(session, tenant_id=ctx.tenant_id, checkout_id=checkout_id)
    return TimelineOut(
        checkout_id=str(checkout_id),
        entries=[TimelineEntryOut.of(entry) for entry in entries],
        cursor=entries[-1].cursor if entries else None,
        scenario_injections=sum(1 for entry in entries if entry.scenario_injection),
    )


@router.get("/checkouts/{checkout_id}/events", summary="Action timeline as Server-Sent Events")
def stream_timeline(
    request: Request,
    checkout_id: uuid.UUID,
    ctx: SessionContext,
    operator: Operator,
    cursor: Annotated[
        str | None,
        Query(description="Resume position. The Last-Event-ID header takes precedence."),
    ] = None,
) -> EventSourceResponse:
    """The same timeline as a live stream, so the storefront never polls.

    Resumption is from the database (specification 24.2). ``Last-Event-ID`` -- or the
    ``cursor`` query parameter, for clients that cannot set the header -- names the last
    row the client saw; every poll re-reads the streams and emits only what sorts after it.
    Nothing about the resumption depends on this process having served the earlier half,
    which is the point: a pod restart mid-payment must not lose the buyer's view of it.

    This route deliberately does **not** depend on ``app_session``. That dependency owns a
    transaction for the life of the response, and a response that lives for fifteen minutes
    would pin a connection and an open snapshot for fifteen minutes. Ownership is checked
    once here in a short transaction, and each poll then opens its own, binds the tenant as
    its first statement, and closes.
    """
    settings = settings_of(request)
    with session_scope_for(settings.database_url_app) as session:
        set_tenant(session, ctx.tenant_id)
        visible_checkout(session, ctx, checkout_id, operator=operator)

    resume = request.headers.get("last-event-id") or cursor

    def _poll(after: str | None) -> tuple[tuple[timeline.TimelineEntry, ...], bool]:
        """One read, in its own short transaction. Runs on a worker thread; see below."""
        with session_scope_for(settings.database_url_app) as poll_session:
            set_tenant(poll_session, ctx.tenant_id)
            entries = timeline.read_after(
                poll_session, tenant_id=ctx.tenant_id, checkout_id=checkout_id, cursor=after
            )
            terminal = timeline.is_terminal(
                poll_session, tenant_id=ctx.tenant_id, checkout_id=checkout_id
            )
        return entries, terminal

    async def _events() -> AsyncIterator[ServerSentEvent]:
        position = resume
        elapsed = 0.0
        quiet = 0.0
        reason = "timeout"
        while elapsed < MAX_STREAM_SECONDS:
            if await request.is_disconnected():
                return
            # psycopg is synchronous. Each poll runs on a worker thread so one slow query
            # cannot stall every other connection sharing this event loop.
            entries, terminal = await anyio.to_thread.run_sync(_poll, position)
            for entry in entries:
                position = entry.cursor
                yield ServerSentEvent(
                    id=entry.cursor,
                    event="timeline",
                    data=TimelineEntryOut.of(entry).model_dump_json(),
                )
            if terminal and not entries:
                quiet += POLL_SECONDS
            else:
                quiet = 0.0
            if quiet >= QUIET_SECONDS_AFTER_TERMINAL:
                reason = "terminal"
                break
            await anyio.sleep(POLL_SECONDS)
            elapsed += POLL_SECONDS
        yield ServerSentEvent(event="complete", id=position, data=f'{{"reason":"{reason}"}}')

    return EventSourceResponse(
        _events(),
        ping=HEARTBEAT_SECONDS,
        ping_message_factory=lambda: ServerSentEvent(comment="keep-alive"),
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/checkouts/{checkout_id}/proof",
    response_model=ProofChainOut,
    summary="Money Action Proof Chain and verifier verdict",
)
def read_proof(
    checkout_id: uuid.UUID,
    ctx: SessionContext,
    session: AppSession,
    operator: Operator,
    payment_attempt_id: Annotated[
        uuid.UUID | None,
        Query(description="Which attempt to follow. Defaults to the newest."),
    ] = None,
    export: Annotated[
        str | None,
        Query(alias="format", description="Pass 'export' for a downloadable document."),
    ] = None,
) -> Any:
    """Specification 26.4's ten links, with the verdict recomputed rather than read.

    ``?format=export`` returns the same redacted document with a filename and
    ``Content-Disposition: attachment``, which is specification 26.2's "export redacted
    evidence for panel demonstration". The export differs only in its envelope: the links
    and the verdict are the same object, so a downloaded artifact and a screenshot of the
    inline response can never disagree.
    """
    visible_checkout(session, ctx, checkout_id, operator=operator)
    chain = proof_chain.build(
        session,
        tenant_id=ctx.tenant_id,
        checkout_id=checkout_id,
        payment_attempt_id=payment_attempt_id,
    )
    if chain is None:
        raise ProblemError(
            404, "Checkout not found", "No checkout with that identifier is visible."
        )
    if payment_attempt_id is not None and chain.payment_attempt_id != payment_attempt_id:
        raise ProblemError(
            404,
            "Payment attempt not found",
            "That attempt does not belong to this checkout.",
            payment_attempt_id=str(payment_attempt_id),
        )
    body = _proof_out(chain)
    if export != "export":
        return body
    document = {
        "document": "money_action_proof_chain",
        "specification": "26.4",
        "generated_at": rfc3339(datetime.now(UTC)),
        "redacted": True,
        "proof": body.model_dump(),
    }
    name = f"proof-{checkout_id}-{chain.payment_attempt_id or 'no-attempt'}.json"
    return JSONResponse(
        document,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
        },
    )


def _proof_out(chain: proof_chain.ProofChain) -> ProofChainOut:
    return ProofChainOut(
        checkout_id=str(chain.checkout_id),
        tenant_id=str(chain.tenant_id),
        merchant_id=str(chain.merchant_id),
        payment_attempt_id=uuid_str(chain.payment_attempt_id),
        attempt_ids=[str(value) for value in chain.attempt_ids],
        links=dict(chain.links),
        verdict=VerdictOut(
            tier=chain.verdict.tier,
            ok=chain.verdict.ok,
            checks=[
                CheckOut(
                    name=check.name,
                    ok=check.ok,
                    applicable=check.applicable,
                    detail=check.detail,
                )
                for check in chain.verdict.checks
            ],
            failed=[check.name for check in chain.verdict.failures],
        ),
        audit_streams=dict(chain.audit_streams),
    )


@router.get(
    "/audit/streams/{aggregate_type}/{aggregate_id}/verify",
    response_model=AuditStreamVerificationOut,
    summary="Verify one audit hash chain",
)
def verify_audit_stream(
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    ctx: SessionContext,
    session: AppSession,
    operator: Operator,
) -> AuditStreamVerificationOut:
    """Walk one aggregate's chain and report the first break, or that it is intact.

    The verification is ``tk.verify_chain``: it recomputes every hash from the stored
    columns and never trusts a stored hash to describe the row it sits on. This route adds
    access control and a wire shape, and no judgement of its own.

    A ``checkout`` or ``payment_attempt`` stream is checked for ownership first, so this
    endpoint cannot be used to discover that another buyer's checkout exists. The remaining
    types are tenant-scoped by row-level security and are operator-only.
    """
    if aggregate_type not in VERIFIABLE_AGGREGATES:
        raise ProblemError(
            404,
            "Unknown audit stream",
            f"{aggregate_type!r} is not a verifiable aggregate type.",
            aggregate_type=aggregate_type,
            known=sorted(VERIFIABLE_AGGREGATES),
        )
    if aggregate_type == "checkout":
        visible_checkout(session, ctx, aggregate_id, operator=operator)
    elif aggregate_type == "payment_attempt":
        visible_attempt(session, ctx, aggregate_id, operator=operator)
    else:
        require_operator(operator)
    summary = proof_chain.verify_stream(
        session,
        tenant_id=ctx.tenant_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
    )
    return AuditStreamVerificationOut.model_validate(dict(summary))


@router.get(
    "/merchants/{merchant_id}/evidence/retained-revenue",
    response_model=RetainedRevenueOut,
    summary="Step 11: revenue retained by refusing a stale approval",
)
def read_retained_revenue(
    merchant_id: uuid.UUID,
    ctx: SessionContext,
    session: AppSession,
    operator: Operator,
    checkout_id: Annotated[
        uuid.UUID | None,
        Query(description="The checkout to account for. Omit for the newest refused approval."),
    ] = None,
) -> RetainedRevenueOut:
    """What the platform retained by refusing version N, derived from committed rows.

    Operator-only: this is merchant evidence, not a buyer-facing number, so it needs the
    scenario key on top of a session. Every figure comes from a row -- the invalidated
    version's approved total, the corrected version's total, and the captured amount from
    the ``orders`` row the kernel wrote from verified capture evidence. Nothing here is
    estimated, and ``controlled_scenario`` is true because the price change was injected
    (specification 9.3: a synthetic result is labelled as one).
    """
    require_operator(operator)
    if checkout_id is None:
        checkout_id = proof_chain.latest_retained_revenue_checkout(
            session, tenant_id=ctx.tenant_id, merchant_id=merchant_id
        )
        if checkout_id is None:
            raise ProblemError(
                404,
                "No checkout to account for",
                "This merchant has no refused approval and no confirmed order yet.",
                merchant_id=str(merchant_id),
            )
    evidence = proof_chain.retained_revenue(
        session, tenant_id=ctx.tenant_id, merchant_id=merchant_id, checkout_id=checkout_id
    )
    if evidence is None:
        raise ProblemError(
            404,
            "Checkout not found",
            "No checkout with that identifier belongs to this merchant.",
            checkout_id=str(checkout_id),
            merchant_id=str(merchant_id),
        )
    return RetainedRevenueOut(
        checkout_id=str(evidence.checkout_id),
        merchant_id=str(evidence.merchant_id),
        currency=evidence.currency,
        stale_version=evidence.stale_version,
        stale_approved_minor=evidence.stale_approved_minor,
        stale_invalidated_at=None
        if evidence.stale_invalidated_at is None
        else rfc3339(evidence.stale_invalidated_at),
        corrected_version=evidence.corrected_version,
        corrected_total_minor=evidence.corrected_total_minor,
        captured_minor=evidence.captured_minor,
        captured_from=evidence.captured_from,
        difference_minor=evidence.difference_minor,
        direction=evidence.direction,
        refunded_minor=evidence.refunded_minor,
        net_retained_minor=evidence.net_retained_minor,
        controlled_scenario=evidence.controlled_scenario,
        explanation=evidence.explanation,
    )
