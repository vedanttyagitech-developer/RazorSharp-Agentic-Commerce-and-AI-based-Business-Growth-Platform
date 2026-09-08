"""The merchant's helpdesk: the queue a buyer's case lands on, and the answer to it.

This router exists because the buyer's order screen makes a promise that nothing was
keeping. It says that a disputed order goes to a person who decides what is owed, and it
says so beside a refund control that deliberately shows no field to type an amount into.
A case opened from that screen was inserted into ``support_cases`` -- with the owning
merchant resolved from the order, on purpose, and with an index built for a queue -- and
then read by nobody, because no merchant-facing route existed. The promise was true about
intent and false about machinery.

**No amount, anywhere on this router.** Not in a response, not in a request body, not as a
field somebody could fill in. What is still refundable is asked of the kernel at the moment
somebody needs it, through ``GET /v1/orders/{order_id}/refundable`` -- the identical route
the buyer's own screen uses, so the two cannot be shown different numbers. A figure copied
onto a case is a figure that was true once: a refund admitted in the meantime moves it, and
the person answering would be reading a promise the platform had already spent.

**And no refund from here.** Resolving a case records that a person dealt with it. The money
goes back through the refund route, the kernel's admission against its own ledger, and a
capability no operator session holds. That separation is the reason the escalation path is
worth having: a queue that could also pay out would be a second way to move money, reviewed
once, with none of the ledger's arithmetic behind it.

Access. Every route is behind ``X-Scenario-Key`` like the rest of the operator surface, and
each also names its capability: ``support.case.read`` to look, ``support.case.resolve`` to
answer. The two are separate because the model-driven Support Specialist holds the first
through ``SUPPORT_AGENT_CAPABILITIES`` and must never hold the second -- answering the case
is the human judgement the case was raised to obtain.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from ..deps import AppSession, SessionContext, require_scenario_key
from ..schemas import rfc3339
from ..services import support_service

router = APIRouter(
    prefix="/v1/support",
    tags=["support"],
    dependencies=[Depends(require_scenario_key)],
)

__all__ = ["router"]

#: How many cases one page returns. A queue page a person reads, not an export.
DEFAULT_LIMIT: Final[int] = 50
MAX_LIMIT: Final[int] = 200


class _Out(BaseModel):
    """Strict responses, specification 24.1."""

    model_config = ConfigDict(extra="forbid")


class SupportCaseOut(_Out):
    """One case as the person answering it sees it.

    Wider than the buyer's view of the same row, because the reader is different: the buyer
    already knows what they wrote and the person answering does not.

    ``order_reference`` rather than a raw id. A helpdesk that shows a UUID makes the person
    answering read it back to a buyer who has never seen one. It is derived from the order
    id rather than stored, so it cannot drift out of step with the order it names.

    ``opened_by`` says ``AGENT`` when the copilot raised it on the buyer's behalf. That is
    not a detail: a summary written by a model and a sentence typed by a person deserve
    different weight from whoever reads them, and only the row knows which this was.
    """

    case_id: str
    order_id: str
    order_reference: str
    merchant_id: str
    reason: str
    #: What the buyer said, in their words. Never parsed, and shown as written.
    note: str
    status: str
    opened_by: str
    #: Who on the merchant's side picked it up. Null until somebody does.
    handled_by: str | None
    #: What the person decided, in their words. Empty until they say.
    resolution_note: str
    created_at: str
    updated_at: str


class SupportQueueOut(_Out):
    """A page of the queue, oldest first.

    Oldest first is the opposite of most lists and is the whole point of a queue: the case
    that has waited longest is the one somebody is owed an answer on. A helpdesk sorted
    newest-first quietly abandons its own tail.
    """

    cases: list[SupportCaseOut]
    #: How many came back. Named rather than left to the caller to count, because a surface
    #: that says "50 open cases" from a page of 50 is stating a limit as a total.
    returned: int
    limit: int
    #: True when the page is full, so a reader knows the queue may be longer than this.
    may_have_more: bool


class AdvanceRequest(BaseModel):
    """Move one case along, with a note for the record.

    No amount and no currency, and there is nowhere on this router to put one. See the
    module docstring: what is owed is the kernel's arithmetic, and this is a queue.
    """

    model_config = ConfigDict(extra="forbid")

    #: The state to move to. The service refuses a transition the graph does not permit and
    #: names the ones it would have allowed.
    status: str = Field(min_length=1, max_length=16)
    note: str = Field(default="", max_length=1000)


@router.get("/cases", response_model=SupportQueueOut, summary="The merchant's support queue")
def read_queue(
    ctx: SessionContext,
    session: AppSession,
    status: Annotated[str | None, Query(max_length=16)] = None,
    merchant_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> SupportQueueOut:
    """What is waiting, oldest first, optionally narrowed to one status or one merchant.

    Tenant scope is row-level security, not a predicate written here. ``merchant_id``
    narrows further and is an application filter, because no policy names a merchant and a
    tenant may hold several -- the operator surface reads across them by design.
    """
    ctx.require("support.case.read")
    cases = support_service.queue_for_merchant(
        session, ctx, status=status, merchant_id=merchant_id, limit=limit
    )
    return SupportQueueOut(
        cases=[_case_out(case) for case in cases],
        returned=len(cases),
        limit=limit,
        may_have_more=len(cases) == limit,
    )


@router.get("/cases/{case_id}", response_model=SupportCaseOut, summary="One support case")
def read_case(case_id: uuid.UUID, ctx: SessionContext, session: AppSession) -> SupportCaseOut:
    ctx.require("support.case.read")
    return _case_out(support_service.read_case(session, ctx, case_id=case_id))


@router.post(
    "/cases/{case_id}/advance",
    response_model=SupportCaseOut,
    summary="Pick up, answer or close one case",
)
def advance_case(
    case_id: uuid.UUID,
    body: AdvanceRequest,
    ctx: SessionContext,
    session: AppSession,
) -> SupportCaseOut:
    """Move a case along and record who did it.

    Answers 409 when the move is not one this case can make, naming the ones it could. That
    refusal is the one that earns its keep: two people opening the same queue is ordinary,
    and without it the second press silently overwrites the first person's answer.

    ``handled_by`` comes from the session and is not a field on the request. A name a caller
    can set is a name a caller can set to somebody else's, and the entire value of that
    column is that it says who actually did it.
    """
    ctx.require("support.case.resolve")
    advanced = support_service.advance_case(
        session, ctx, case_id=case_id, to_status=body.status, note=body.note
    )
    return _case_out(advanced)


def _case_out(case: support_service.QueuedCase) -> SupportCaseOut:
    return SupportCaseOut(
        case_id=str(case.case_id),
        order_id=str(case.order_id),
        order_reference=case.order_reference,
        merchant_id=str(case.merchant_id),
        reason=case.reason_code,
        note=case.note,
        status=case.status,
        opened_by=case.opened_by,
        handled_by=case.handled_by,
        resolution_note=case.resolution_note,
        created_at=rfc3339(case.created_at),
        updated_at=rfc3339(case.updated_at),
    )
