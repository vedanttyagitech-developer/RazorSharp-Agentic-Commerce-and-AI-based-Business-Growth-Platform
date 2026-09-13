"""A buyer asked for help, and a person will answer.

This is the path a refund request takes, and it is deliberately not a path that refunds
anything. The distinction is the whole design:

* The buyer, or the support agent acting for them, may **open a case**. That writes a row
  naming the order, the reason and who raised it.
* Nobody on this path may compute what the buyer is owed. There is no amount here, no
  currency, no eligibility decision, and no call into the kernel's refund admission.
* What the buyer is owed is settled by a person on the merchant's side, reading the case.

Why it is built this way rather than as an automatic remedy: a model that can end a
conversation by moving money is a model somebody will talk into moving money. The agent's
useful work is the part before that — reading the Policy-at-Sale Receipt and telling the
buyer what the merchant actually promised, which it can already do through
``policy_search`` and cite by ``policy_id`` and ``policy_version``. Opening a case is the
only write it needs, and it is a write on a queue rather than on a ledger.

The service runs on the **app** role. That is not an oversight: ``support_cases`` is not a
financial table, the kernel is granted nothing on it, and a case that could only be opened
by a role holding payment credentials would be describing itself as something it is not.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from commerce_domain import ActorType, order_reference, uuid7
from platform_db import Order
from platform_db.schema_service import SupportCase
from sqlalchemy import Select, select, text
from sqlalchemy.orm import Session

from ..deps import RequestContext
from ..errors import ProblemError

__all__ = [
    "OPENED_BY",
    "SUPPORT_REASONS",
    "BACKWARD",
    "TRANSITIONS",
    "OpenedCase",
    "QueuedCase",
    "advance_case",
    "cases_for_order",
    "open_case",
    "queue_for_merchant",
    "read_case",
]

#: The reasons a buyer may give, exactly the keys the order screen already renders. Lower
#: case, and closed: a free-text reason would be a field nobody could route on, and a new
#: key is a decision about what the merchant's queue can be filtered by.
SUPPORT_REASONS: Final[frozenset[str]] = frozenset(
    {
        "buyer_requested",
        "item_not_delivered",
        "item_damaged",
        "wrong_item",
        "ordered_by_mistake",
    }
)

#: Who put the case on the queue. Recorded because "a model opened this on my behalf" is a
#: fact the person answering it should have, and because it is the difference between a
#: buyer's own words and a summary of them.
OPENED_BY: Final[frozenset[str]] = frozenset({"BUYER", "AGENT"})

#: How long the note may be. Long enough for a paragraph, short enough that the column is
#: not a document store.
MAX_NOTE = 1000


def _lock_order(session: Session, tenant_id: uuid.UUID, order_id: uuid.UUID) -> None:
    # The app role cannot lock financial orders with FOR UPDATE. A transaction-scoped
    # advisory lock serializes support creation and reopening without those privileges.
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
        {"scope": f"support:{tenant_id}:{order_id}"},
    )


@dataclass(frozen=True, slots=True)
class OpenedCase:
    """The case, as the buyer and the agent are both told about it.

    ``case_id`` is the whole answer on the agent's side. It is what the buyer quotes back,
    what the merchant's helpdesk opens, and the only thing the agent is entitled to return
    from an escalation -- there is deliberately no amount and no promise beside it.
    """

    case_id: uuid.UUID
    order_id: uuid.UUID
    reason_code: str
    status: str
    opened_by: str
    resolution_note: str = ""


def open_case(
    session: Session,
    ctx: RequestContext,
    *,
    order_id: uuid.UUID,
    reason_code: str,
    note: str = "",
    opened_by: str = "BUYER",
) -> OpenedCase:
    """Put one case on the merchant's queue.

    Refuses an unknown reason and an over-long note rather than storing either: the reason
    is what the merchant's side filters on, and a key nobody recognises is a case nobody
    routes.

    Returns the existing case when this buyer already has an open one on this order. A
    second press, a retried request or an agent asked twice must not fill a queue with the
    same complaint -- and answering with the original's id is more useful to the buyer than
    refusing, because the id is the thing they were going to be given anyway.
    """
    if reason_code not in SUPPORT_REASONS:
        raise ProblemError(
            422,
            "Unknown reason",
            "That is not a reason this store's support queue recognises.",
            reason_code=reason_code,
            allowed=sorted(SUPPORT_REASONS),
        )
    if opened_by not in OPENED_BY:
        raise ProblemError(
            500,
            "Unknown opener",
            "A case is opened by the buyer or by the agent acting for them.",
            opened_by=opened_by,
        )
    if len(note) > MAX_NOTE:
        raise ProblemError(
            422,
            "Note is too long",
            f"Keep it under {MAX_NOTE} characters.",
            length=len(note),
        )
    if ctx.buyer_ref is None:
        raise ProblemError(
            403,
            "Not a buyer session",
            "Only the buyer who placed an order may raise a case about it.",
        )

    # Whose queue this belongs on, read from the order rather than from the session. A
    # tenant may hold several merchants, and the buyer's session names one of them, which
    # is not necessarily the one that sold this order.
    merchant_id = session.execute(
        select(Order.merchant_id).where(Order.tenant_id == ctx.tenant_id, Order.id == order_id)
    ).scalar_one_or_none()
    if merchant_id is None:
        raise ProblemError(
            404,
            "Order not found",
            "No such order in this tenant.",
            order_id=str(order_id),
        )

    _lock_order(session, ctx.tenant_id, order_id)
    existing = session.execute(
        select(SupportCase).where(
            SupportCase.tenant_id == ctx.tenant_id,
            SupportCase.order_id == order_id,
            SupportCase.buyer_ref == ctx.buyer_ref,
            SupportCase.status.in_(("OPEN", "ACKNOWLEDGED")),
        )
    ).scalar_one_or_none()
    if existing is not None:
        return _view(existing)

    case = SupportCase(
        id=uuid7(),
        tenant_id=ctx.tenant_id,
        merchant_id=merchant_id,
        order_id=order_id,
        buyer_ref=ctx.buyer_ref,
        reason_code=reason_code,
        note=note,
        opened_by=opened_by,
        status="OPEN",
    )
    session.add(case)
    session.flush()
    return _view(case)


def cases_for_order(
    session: Session, ctx: RequestContext, *, order_id: uuid.UUID
) -> list[OpenedCase]:
    """Every case this buyer has raised on this order, oldest first.

    Scoped to the buyer as well as the tenant. Row-level security answers the tenant
    question; whose case it is remains an application check, because there is no
    ``buyer_ref`` in any policy predicate.
    """
    if ctx.buyer_ref is None:
        return []
    rows = session.execute(
        select(SupportCase)
        .where(
            SupportCase.tenant_id == ctx.tenant_id,
            SupportCase.order_id == order_id,
            SupportCase.buyer_ref == ctx.buyer_ref,
        )
        .order_by(SupportCase.created_at, SupportCase.id)
    ).scalars()
    return [_view(row) for row in rows]


def _view(case: SupportCase) -> OpenedCase:
    return OpenedCase(
        case_id=case.id,
        order_id=case.order_id,
        reason_code=case.reason_code,
        status=case.status,
        opened_by=case.opened_by,
        resolution_note=case.resolution_note,
    )


# --------------------------------------------------------------------- the merchant's side

#: What a case may become, and from where. A closed graph rather than a free assignment,
#: because the interesting question about a queue is not what state a row is in but whether
#: anybody can put it there twice.
#:
#: ``CLOSED`` is reachable from anywhere including ``OPEN``: a duplicate, a case the buyer
#: withdrew, or one about an order that turned out to be somebody else's is closed without
#: ever being worked. ``RESOLVED`` is not reachable from ``OPEN`` on purpose -- answering a
#: case you never picked up is possible in real life and is exactly the sequence that leaves
#: no record of who was dealing with it.
#:
#: **Every move a person makes here can be taken back, and none of them is an edit.**
#: This graph used to run one way only, which meant a queue where the cost of a misclick
#: was borne entirely by the buyer: a case picked up by the wrong person could not go back
#: to the queue, a case answered too early could only be closed, and a case closed by
#: mistake left somebody permanently unanswered with no way for anybody to notice. Nothing
#: about that was a safety property. It was a missing edge.
#:
#: So ``ACKNOWLEDGED`` returns to ``OPEN``, ``RESOLVED`` returns to ``ACKNOWLEDGED``, and
#: ``CLOSED`` reopens to ``ACKNOWLEDGED`` -- to the picked-up state rather than to the
#: queue, because whoever reopens a closed case is by that act dealing with it.
#:
#: A reversal is a recorded move and not an undo: it demands a reason (see
#: :func:`advance_case`), the note is appended rather than replaced, and the row keeps the
#: whole sequence. What is reversible is the *state*, never the record of how it got there.
BACKWARD: Final[dict[str, str]] = {
    "ACKNOWLEDGED": "OPEN",
    "RESOLVED": "ACKNOWLEDGED",
    "CLOSED": "ACKNOWLEDGED",
}

TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "OPEN": frozenset({"ACKNOWLEDGED", "CLOSED"}),
    "ACKNOWLEDGED": frozenset({"RESOLVED", "CLOSED", "OPEN"}),
    "RESOLVED": frozenset({"CLOSED", "ACKNOWLEDGED"}),
    "CLOSED": frozenset({"ACKNOWLEDGED"}),
}


@dataclass(frozen=True, slots=True)
class QueuedCase:
    """One case as the merchant's helpdesk sees it.

    Wider than :class:`OpenedCase` because the reader is different: the buyer already knows
    what they wrote, and the person answering does not. It carries the buyer's own words,
    the order's spoken reference, and who is dealing with it.

    It still carries **no amount**, and the omission is the same one as everywhere else on
    this path. What is still refundable is a question for the kernel at the moment it is
    asked, through ``GET /v1/orders/{order_id}/refundable``, which is the identical route
    the buyer's own screen uses. A figure copied onto a case is a figure that was true once.
    """

    case_id: uuid.UUID
    order_id: uuid.UUID
    order_reference: str
    merchant_id: uuid.UUID
    reason_code: str
    note: str
    status: str
    opened_by: str
    handled_by: str | None
    resolution_note: str
    created_at: datetime
    updated_at: datetime


def _visible_cases(ctx: RequestContext) -> Select[tuple[SupportCase]]:
    """Only operators may read across merchants; request filters can only narrow scope."""
    query = select(SupportCase).where(SupportCase.tenant_id == ctx.tenant_id)
    if ctx.principal.actor_type is not ActorType.OPERATOR:
        query = query.where(SupportCase.merchant_id == ctx.merchant_id)
    return query


def queue_for_merchant(
    session: Session,
    ctx: RequestContext,
    *,
    status: str | None = None,
    merchant_id: uuid.UUID | None = None,
    order_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[QueuedCase]:
    """The merchant's queue, oldest first, optionally narrowed to one status.

    Oldest first and not newest first, which is the opposite of most lists and is the point
    of a queue: the case that has been waiting longest is the one somebody is owed an answer
    on. A helpdesk sorted newest-first quietly abandons its tail.

    Tenant and session merchant scope are enforced before request filters. Only an
    operator may read across merchants in the same tenant; ``merchant_id`` and
    ``order_id`` can narrow that scope but cannot grant access.
    """
    query = _visible_cases(ctx)
    if status is not None:
        if status not in TRANSITIONS:
            raise ProblemError(
                422,
                "Unknown status",
                "That is not a state a support case can be in.",
                case_status=status,
                allowed=sorted(TRANSITIONS),
            )
        query = query.where(SupportCase.status == status)
    if merchant_id is not None:
        query = query.where(SupportCase.merchant_id == merchant_id)
    if order_id is not None:
        query = query.where(SupportCase.order_id == order_id)
    rows = session.execute(
        query.order_by(SupportCase.created_at, SupportCase.id).limit(limit).offset(offset)
    ).scalars()
    return [_queued(row) for row in rows]


def read_case(session: Session, ctx: RequestContext, *, case_id: uuid.UUID) -> QueuedCase:
    """One visible case, or the same 404 used for missing and other-merchant cases."""
    case = session.execute(
        _visible_cases(ctx).where(SupportCase.id == case_id)
    ).scalar_one_or_none()
    if case is None:
        raise ProblemError(
            404,
            "Case not found",
            "No such support case is visible to this session.",
            case_id=str(case_id),
        )
    return _queued(case)


def advance_case(
    session: Session,
    ctx: RequestContext,
    *,
    case_id: uuid.UUID,
    to_status: str,
    note: str = "",
) -> QueuedCase:
    """Move one case along, recording who did it.

    Refuses a transition the graph does not permit, and says which ones it would have
    allowed. That refusal is the one that matters: two people opening the same queue is
    normal, and without it the second press silently overwrites the first person's answer.

    ``handled_by`` is taken from the session and never from the request body. A field a
    caller can set is a field a caller can set to somebody else's name, and the whole value
    of this column is that it says who actually did it.
    """
    if to_status not in TRANSITIONS:
        raise ProblemError(
            422,
            "Unknown status",
            "That is not a state a support case can be in.",
            case_status=to_status,
            allowed=sorted(TRANSITIONS),
        )
    if len(note) > MAX_NOTE:
        raise ProblemError(
            422,
            "Note is too long",
            f"Keep it under {MAX_NOTE} characters.",
            length=len(note),
        )
    subject = session.execute(
        _visible_cases(ctx).where(SupportCase.id == case_id)
    ).scalar_one_or_none()
    if subject is None:
        raise ProblemError(
            404, "Case not found", "No such support case is visible to this session."
        )
    _lock_order(session, ctx.tenant_id, subject.order_id)
    case = session.execute(
        _visible_cases(ctx)
        .where(SupportCase.id == case_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if case is None:
        raise ProblemError(
            404,
            "Case not found",
            "No such support case is visible to this session.",
            case_id=str(case_id),
        )
    allowed = TRANSITIONS[case.status]
    if to_status not in allowed:
        raise ProblemError(
            409,
            "That is not a move this case can make",
            f"A case that is {case.status} cannot become {to_status}.",
            case_id=str(case_id),
            case_status=case.status,
            allowed=sorted(allowed),
        )
    # A reversal has to say why, and the why is kept beside what it reversed.
    #
    # Going back is the one move where the note is the whole record. A forward move is
    # legible from the state it produced -- ACKNOWLEDGED means somebody picked it up --
    # while a case that is ACKNOWLEDGED again looks exactly like one that was never
    # answered. Without a reason and without keeping the old one, reopening would erase
    # the fact that anybody had resolved it, which is a worse record than the one-way
    # graph it replaces.
    going_back = BACKWARD.get(case.status) == to_status
    if going_back and not note.strip():
        raise ProblemError(
            422,
            "Say why this is going back",
            "Taking a move back is allowed and is recorded. A reversal with no reason "
            "leaves the next person unable to tell a correction from a mistake.",
            case_id=str(case_id),
            case_status=case.status,
            to_status=to_status,
        )

    if to_status in {"OPEN", "ACKNOWLEDGED"}:
        existing = session.execute(
            select(SupportCase.id).where(
                SupportCase.tenant_id == ctx.tenant_id,
                SupportCase.order_id == case.order_id,
                SupportCase.buyer_ref == case.buyer_ref,
                SupportCase.id != case.id,
                SupportCase.status.in_(("OPEN", "ACKNOWLEDGED")),
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise ProblemError(
                409,
                "Active support case already exists",
                "Continue the existing active case before reopening this one.",
                active_case_id=str(existing),
            )
    case.status = to_status
    case.handled_by = _handler(ctx)
    if note:
        case.resolution_note = (
            f"{case.resolution_note}\n{_handler(ctx)} moved it back to {to_status}: {note}"
            if going_back and case.resolution_note
            else note
        )
    case.updated_at = datetime.now(UTC)
    session.flush()
    return _queued(case)


def _handler(ctx: RequestContext) -> str:
    """The operator's own name for the record, never a value from the request."""
    return ctx.principal.principal_id


def _queued(case: SupportCase) -> QueuedCase:
    return QueuedCase(
        case_id=case.id,
        order_id=case.order_id,
        # The order said out loud. A helpdesk that shows a raw UUID makes the person
        # answering read it back to a buyer who has never seen one. Derived from the id
        # rather than stored, so it cannot drift out of step with the order it names.
        order_reference=order_reference(case.order_id),
        merchant_id=case.merchant_id,
        reason_code=case.reason_code,
        note=case.note,
        status=case.status,
        opened_by=case.opened_by,
        handled_by=case.handled_by,
        resolution_note=case.resolution_note,
        created_at=case.created_at,
        updated_at=case.updated_at,
    )
