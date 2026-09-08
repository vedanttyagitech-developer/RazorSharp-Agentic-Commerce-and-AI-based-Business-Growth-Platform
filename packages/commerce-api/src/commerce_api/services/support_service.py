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
from typing import Final

from commerce_domain import uuid7
from platform_db import Order
from platform_db.schema_service import SupportCase
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import RequestContext
from ..errors import ProblemError

__all__ = [
    "OPENED_BY",
    "SUPPORT_REASONS",
    "OpenedCase",
    "cases_for_order",
    "open_case",
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
    )
