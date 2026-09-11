"""Collection reads over checkouts, orders and refunds: scope-aware, paginated, counted.

**Owned by build unit D.** Built for the merchant console's operations page, which until
now had nothing to list: there were no order or refund collection endpoints.

Two scopes, decided from the request and never from anything the caller asserts:

* **own** -- a buyer session lists its own checkouts, and the orders and refunds made
  from them, and nothing else. ``checkouts.buyer_ref`` is the ownership fact here exactly
  as it is for every
  single-row read (:func:`commerce_api.deps.assert_owner`), so an order and the checkout
  it was confirmed from can never disagree about who may see it.
* **tenant** -- a session accompanied by a valid scenario key, the P0 stand-in for the
  merchant operator surface (ADR 0003 D11), lists every row in its tenant. Row-level
  security still confines it to that tenant, and the tenant predicate is written out
  as well so the intent is readable at the call site.

Pagination is keyset on ``(created_at, id)`` descending. An offset would shift under an
operator paging while new orders land; a keyset page never repeats or skips a row. The
cursor is that pair encoded and nothing more. It carries no authority: a cursor minted
under one scope and presented under another still meets the second scope's predicate,
because the predicate is applied on every page from the session, not from the cursor.

Counts are taken across the whole scope rather than the page, for the reason the outbox
view gives: a rising ``REFUND_UNKNOWN`` count is the operational signal, and a page is
a window onto it.

Every amount is the integer the row holds. Nothing here adds, rounds or converts money;
``refunded_minor`` is a database ``SUM`` over settled rows, which is the one arithmetic
this module asks for and the database performs it in integers.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time
from datetime import date as date_type
from typing import Any, Final

import transaction_kernel as tk
from commerce_domain import ActorType, CheckoutRef, Money, order_reference
from commerce_domain.ids import parse_order_reference
from platform_db.schema import CheckoutVersion, PaymentAttempt, Refund
from platform_db.schema_service import Checkout, Order
from sqlalchemy import BigInteger, case, false, func, select, tuple_
from sqlalchemy.orm import InstrumentedAttribute, Session, aliased
from sqlalchemy.sql import ColumnElement, Select
from transaction_kernel.receipts import database_now_ms, return_offer_at_sale
from transaction_kernel.refunds import STALE_CAPTURE_REASON, RefundStatus
from transaction_kernel.states import (
    NON_TERMINAL_CHECKOUT_STATES,
    TERMINAL_CHECKOUT_STATES,
)

from ..deps import RequestContext
from ..errors import ProblemError
from ..schemas import (
    CheckoutsPageOut,
    CheckoutSummaryOut,
    ListScope,
    MoneyOut,
    OrdersPageOut,
    OrderState,
    OrderSummaryOut,
    RefundListItemOut,
    RefundsPageOut,
    rfc3339,
    uuid_str,
)
from .payment_service import _capture_evidence

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "Cursor",
    "list_checkouts",
    "list_orders",
    "list_refunds",
    "scope_of",
]

DEFAULT_PAGE_SIZE: Final[int] = 25
#: One hundred, not one thousand: a console renders a page, and a client that wants the
#: whole tenant walks cursors. Bounding the page bounds the join work per request.
MAX_PAGE_SIZE: Final[int] = 100

_CURSOR_SEPARATOR: Final[str] = "|"


# ----------------------------------------------------------------------------- cursor


@dataclass(frozen=True, slots=True)
class Cursor:
    """The last row of the previous page, as the pair the ordering is keyed on.

    Encoded URL-safe base64 with the padding stripped, so it survives a query string
    untouched. It is opaque by convention rather than by encryption: there is nothing
    in it a client could not already read off the page it came from.
    """

    created_at: datetime
    row_id: uuid.UUID

    def encode(self) -> str:
        raw = f"{rfc3339(self.created_at)}{_CURSOR_SEPARATOR}{self.row_id}".encode()
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, token: str) -> Cursor:
        """Parse a cursor, or refuse with 400.

        400 rather than an empty page: a client that mangled its cursor and received an
        empty page would conclude it had reached the end, and silently stop short.
        """
        try:
            padded = token + "=" * (-len(token) % 4)
            raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
            moment, separator, ident = raw.partition(_CURSOR_SEPARATOR)
            if separator != _CURSOR_SEPARATOR:
                raise ValueError("missing separator")
            created_at = datetime.fromisoformat(moment)
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            return cls(created_at=created_at, row_id=uuid.UUID(ident))
        except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
            raise ProblemError(
                400,
                "Invalid cursor",
                "The cursor is not one this endpoint issued. Start again without one.",
            ) from exc


def scope_of(ctx: RequestContext, *, operator: bool) -> ListScope:
    """The widest scope this request is entitled to. Never widened by anything else.

    The scenario key is described above, and in ``ListScope`` itself, as "the P0 stand-in
    for the merchant operator surface": a placeholder held open because the merchant actor
    did not exist yet. It exists now, so a MERCHANT session reaches that scope on its own
    identity instead of by carrying the platform's demo key. The placeholder is being
    replaced, not the scope widened -- and a merchant reading their own orders no longer
    needs a credential that would also mint them an operator.
    """
    if operator or ctx.principal.actor_type is ActorType.MERCHANT:
        return ListScope.TENANT
    return ListScope.OWN


# ------------------------------------------------------- finding an order by its number


#: Milliseconds in a day. The bound of the id range one reference's date can name.
_DAY_MS: Final[int] = 24 * 60 * 60 * 1000


def _id_range_for(day: date_type) -> tuple[uuid.UUID, uuid.UUID]:
    """The half-open ``orders.id`` range whose UUIDv7 timestamps fall on ``day``, in UTC.

    Bounded on the **id** and not on ``created_at`` on purpose. ``order_reference`` reads
    the day out of the id's own 48-bit timestamp, so the id is the column the reference
    actually names; filtering on ``created_at`` would ask a second clock whether it agreed
    with the first, and near midnight it sometimes would not.

    A UUIDv7's first six bytes are the big-endian millisecond timestamp, so PostgreSQL's
    byte-wise ``uuid`` comparison is time order, and this is a range scan on the primary
    key rather than a table read.
    """
    start_ms = int(datetime.combine(day, time.min, tzinfo=UTC).timestamp() * 1000)
    low = uuid.UUID(bytes=start_ms.to_bytes(6, "big") + bytes(10))
    high = uuid.UUID(bytes=(start_ms + _DAY_MS).to_bytes(6, "big") + bytes(10))
    return low, high


def find_order_by_reference(
    session: Session, ctx: RequestContext, scope: ListScope, reference: str
) -> uuid.UUID | None:
    """The order a buyer's own order number names, if they own one that matches.

    THE REFERENCE IS NOT AN IDENTIFIER AND THIS DOES NOT TREAT IT AS ONE. Its tail is
    thirty-five bits of the id's last sixty-four, so it is a strong hint about which order
    is meant and never a proof. What happens here is a *comparison*: the day narrows the
    candidates to one buyer's orders from one UTC date, and each candidate is rendered with
    ``order_reference`` and matched exactly. That keeps ``order_reference`` the only
    definition of what an order is called -- there is no second implementation here to
    drift away from it -- and it means a near-miss finds nothing rather than the closest row.

    Scope is applied in the query, before anything is compared. Knowing somebody's order
    number is not a capability, and a resolver that found the row first and checked
    ownership afterwards would be one refactor away from being one.

    Raises ``ReferenceFormatError`` when the text is not a reference at all, so a caller can
    tell "you mistyped it" from "there is no such order". Those are different answers and
    collapsing them tells a buyer their order does not exist.
    """
    parsed = parse_order_reference(reference)
    low, high = _id_range_for(parsed.date)
    query = (
        select(Order.id)
        .join(Checkout, Checkout.id == Order.checkout_id)
        .where(Order.tenant_id == ctx.tenant_id, Order.id >= low, Order.id < high)
    )
    if scope is ListScope.OWN:
        query = query.where(Checkout.buyer_ref == ctx.buyer_ref)
    for (candidate,) in session.execute(query).all():
        if order_reference(candidate) == parsed.canonical:
            return uuid.UUID(str(candidate))
    return None


# ----------------------------------------------------------------------------- orders


def _age_seconds(
    created_at: ColumnElement[Any] | InstrumentedAttribute[Any],
) -> ColumnElement[Any]:
    """Whole seconds since ``created_at``, by the database clock, never negative.

    The database clock rather than the process clock: the same clock stamped the row,
    so an API pod with a skewed clock cannot report an order as younger than it is.
    """
    return _seconds_between(created_at, func.now())


def _seconds_between(
    start: ColumnElement[Any] | InstrumentedAttribute[Any],
    end: ColumnElement[Any] | InstrumentedAttribute[Any],
) -> ColumnElement[Any]:
    """Whole seconds from ``start`` to ``end``, by the database, never negative.

    Both ends must be database-stamped for the answer to mean anything, which is why this
    takes columns rather than values: a caller that had already read a timestamp into
    Python and subtracted it there would be reporting the difference between two clocks
    as if it were a duration.
    """
    elapsed = func.extract("epoch", end - start)
    # ``GREATEST`` is not null-propagating in PostgreSQL: it ignores nulls among its
    # arguments, so ``GREATEST(0, NULL)`` is ``0``. Left alone, an end this could not
    # read would be reported as a span of zero seconds -- an instant sale -- which is a
    # flattering lie where the honest answer is that nothing was measured.
    return case((elapsed.is_(None), None), else_=func.greatest(0, func.cast(elapsed, BigInteger)))


def _orders_query(ctx: RequestContext, scope: ListScope, status: OrderState | None) -> Select[Any]:
    # The version this checkout opened at, which is where the transaction starts: the
    # quote frozen, the hash minted, the stock held. A second alias because the join
    # below already binds ``CheckoutVersion`` to the *approved* version, and on a checkout
    # the buyer reopened those are different rows.
    opened = aliased(CheckoutVersion, name="opened")
    settled = (
        select(
            Refund.tenant_id.label("tenant_id"),
            Refund.payment_attempt_id.label("payment_attempt_id"),
            func.coalesce(
                func.sum(Refund.amount_minor).filter(Refund.status == RefundStatus.PROCESSED.value),
                0,
            ).label("refunded_minor"),
            func.count().label("refund_count"),
        )
        .group_by(Refund.tenant_id, Refund.payment_attempt_id)
        .subquery("settled")
    )
    query = (
        select(
            Order.id,
            Order.checkout_id,
            Order.checkout_version,
            Order.payment_attempt_id,
            Order.policy_receipt_hash,
            Order.status,
            Order.total_minor,
            Order.currency,
            Order.capture_evidence,
            Order.created_at,
            # The approved version's hash, so a listed row can be resolved through the same
            # verified binding a single-order read uses. Joined rather than taken from
            # `orders.policy_receipt_hash`: that names the receipt, and what decides whether
            # returns were offered is the receipt bound to the version, re-checked.
            CheckoutVersion.content_hash,
            PaymentAttempt.provider_order_id,
            PaymentAttempt.provider_payment_id,
            func.coalesce(settled.c.refunded_minor, 0).label("refunded_minor"),
            func.coalesce(settled.c.refund_count, 0).label("refund_count"),
            _age_seconds(Order.created_at).label("age_seconds"),
            # How long the sale took, computed where both timestamps were stamped. The
            # detail read in ``refund_service`` computes the same figure in raw SQL, and
            # ``test_capi_order_duration`` holds the two to the same answer.
            _seconds_between(opened.created_at, Order.created_at).label("duration_seconds"),
        )
        .join(
            Checkout, (Checkout.tenant_id == Order.tenant_id) & (Checkout.id == Order.checkout_id)
        )
        .join(
            CheckoutVersion,
            (CheckoutVersion.tenant_id == Order.tenant_id)
            & (CheckoutVersion.checkout_id == Order.checkout_id)
            & (CheckoutVersion.version == Order.checkout_version),
        )
        .join(
            PaymentAttempt,
            (PaymentAttempt.tenant_id == Order.tenant_id)
            & (PaymentAttempt.id == Order.payment_attempt_id),
        )
        # Outer, so an order whose opening version cannot be read still lists. An inner
        # join would drop the row entirely, which is a measurement deleting its subject.
        .outerjoin(
            opened,
            (opened.tenant_id == Order.tenant_id)
            & (opened.checkout_id == Order.checkout_id)
            & (opened.version == 1),
        )
        .outerjoin(
            settled,
            (settled.c.tenant_id == Order.tenant_id)
            & (settled.c.payment_attempt_id == Order.payment_attempt_id),
        )
        .where(Order.tenant_id == ctx.tenant_id)
    )
    if scope is ListScope.OWN:
        query = query.where(Checkout.buyer_ref == ctx.buyer_ref)
    if status is not None:
        query = query.where(Order.status == status.value)
    return query


def _order_counts(session: Session, ctx: RequestContext, scope: ListScope) -> dict[str, int]:
    query = (
        select(Order.status, func.count().label("total"))
        .join(
            Checkout, (Checkout.tenant_id == Order.tenant_id) & (Checkout.id == Order.checkout_id)
        )
        .where(Order.tenant_id == ctx.tenant_id)
    )
    if scope is ListScope.OWN:
        query = query.where(Checkout.buyer_ref == ctx.buyer_ref)
    counted = {
        str(row.status): int(row.total) for row in session.execute(query.group_by(Order.status))
    }
    # Every state present, so a zero reads as "none" rather than as "not measured".
    return {member.value: counted.get(member.value, 0) for member in OrderState}


def _order_summary(session: Session, row: Any, *, now_ms: int) -> OrderSummaryOut:
    amount = Money(int(row.total_minor), str(row.currency))
    # What this sale was sold under, never what the shop offers today. One resolution per
    # row, through the same verified binding the order screen uses: a page of orders sold
    # while returns were offered still says so after the merchant withdraws them, which is
    # the receipt's whole claim and would be quietly untrue if the list read live policy.
    returns = return_offer_at_sale(
        session,
        CheckoutRef(row.checkout_id, int(row.checkout_version), str(row.content_hash)),
        now_ms=now_ms,
    )
    return OrderSummaryOut(
        order_id=str(row.id),
        reference=order_reference(row.id),
        checkout_id=str(row.checkout_id),
        version=int(row.checkout_version),
        payment_attempt_id=str(row.payment_attempt_id),
        policy_receipt_hash=str(row.policy_receipt_hash),
        state=OrderState(row.status),
        amount_minor=amount.minor,
        currency=amount.currency,
        amount=MoneyOut.of(amount),
        capture_evidence=_capture_evidence(row.capture_evidence),
        razorpay_order_id=row.provider_order_id,
        razorpay_payment_id=row.provider_payment_id,
        refunded_minor=int(row.refunded_minor),
        refund_count=int(row.refund_count),
        created_at=rfc3339(row.created_at),
        age_seconds=int(row.age_seconds),
        duration_seconds=(None if row.duration_seconds is None else int(row.duration_seconds)),
        return_offered=returns.offered,
        return_closes_at=(
            None
            if returns.closes_at_ms is None
            else rfc3339(datetime.fromtimestamp(returns.closes_at_ms / 1000, tz=UTC))
        ),
    )


def list_orders(
    session: Session,
    ctx: RequestContext,
    *,
    operator: bool,
    status: OrderState | None,
    limit: int,
    cursor: str | None,
    reference: str | None = None,
) -> OrdersPageOut:
    """One page of orders, newest first, in the widest scope this request may see.

    ``reference`` narrows to the single order a buyer's own order number names -- the
    ``RS-260909-XW5G26M`` on their screen, which until now nothing could look up. It is a
    filter on this page rather than a route of its own so that a caller gets the shape it
    already handles, and so scope, counts and the cursor keep meaning exactly what they
    mean without it. See :func:`find_order_by_reference` for why a reference is compared
    and never trusted.
    """
    scope = scope_of(ctx, operator=operator)
    query = _orders_query(ctx, scope, status)
    if reference is not None:
        # Resolved before the page is built, and to at most one row. `false()` rather than
        # an early return so the page keeps its real `counts` and `scope`: "you have orders,
        # none of them is that one" and "you have no orders" are different answers.
        found = find_order_by_reference(session, ctx, scope, reference)
        query = query.where(Order.id == found) if found is not None else query.where(false())
    if cursor is not None:
        after = Cursor.decode(cursor)
        query = query.where(tuple_(Order.created_at, Order.id) < (after.created_at, after.row_id))
    # One more than asked, to learn whether a next page exists without a second query.
    rows = session.execute(
        query.order_by(Order.created_at.desc(), Order.id.desc()).limit(limit + 1)
    ).all()
    page, more = rows[:limit], len(rows) > limit
    last = page[-1] if page and more else None
    # One clock for the whole page, read once. Two rows of the same list deciding a
    # deadline against two different instants is a list that can contradict itself.
    now_ms = database_now_ms(session)
    return OrdersPageOut(
        orders=[_order_summary(session, row, now_ms=now_ms) for row in page],
        next_cursor=None if last is None else Cursor(last.created_at, last.id).encode(),
        limit=limit,
        scope=scope,
        counts=_order_counts(session, ctx, scope),
    )


# ---------------------------------------------------------------------------- refunds


def _wire_state() -> ColumnElement[str]:
    """``refund_service.refund_state_of``, as the database evaluates it per row.

    The same rule in SQL so the ``state`` filter and the counts are exact rather than
    approximate: ``PROCESSED`` for less than the capture is ``PARTIALLY_REFUNDED``, and
    collapsing that into ``REFUNDED`` would tell an operator a partial refund settled the
    whole payment. The capture amount is the ``orders`` row's, joined by attempt.
    """
    processed = Refund.status == RefundStatus.PROCESSED.value
    partial = processed & Order.total_minor.is_not(None) & (Refund.amount_minor < Order.total_minor)
    return case(
        (partial, tk.PaymentState.PARTIALLY_REFUNDED.value),
        (processed, tk.PaymentState.REFUNDED.value),
        (Refund.status == RefundStatus.PENDING.value, tk.PaymentState.REFUND_PENDING.value),
        (Refund.status == RefundStatus.FAILED.value, tk.PaymentState.REFUND_FAILED.value),
        (Refund.status == RefundStatus.UNKNOWN.value, tk.PaymentState.REFUND_UNKNOWN.value),
        (Refund.status == RefundStatus.RECONCILING.value, tk.PaymentState.RECONCILING.value),
        (Refund.status == RefundStatus.ESCALATED.value, tk.PaymentState.ESCALATED.value),
        else_=Refund.status,
    )


#: The states a refund row can present as. Counts report every one of these, so an
#: operator sees a zero next to ``REFUND_UNKNOWN`` rather than nothing at all.
REFUND_WIRE_STATES: Final[tuple[tk.PaymentState, ...]] = (
    tk.PaymentState.REFUND_PENDING,
    tk.PaymentState.REFUND_UNKNOWN,
    tk.PaymentState.REFUND_FAILED,
    tk.PaymentState.RECONCILING,
    tk.PaymentState.ESCALATED,
    tk.PaymentState.PARTIALLY_REFUNDED,
    tk.PaymentState.REFUNDED,
)


def _refunds_base(ctx: RequestContext, scope: ListScope) -> Select[Any]:
    query = (
        select(Refund, Order.id.label("order_id"), Order.total_minor.label("captured_minor"))
        .join(
            Checkout, (Checkout.tenant_id == Refund.tenant_id) & (Checkout.id == Refund.checkout_id)
        )
        .outerjoin(
            Order,
            (Order.tenant_id == Refund.tenant_id)
            & (Order.payment_attempt_id == Refund.payment_attempt_id),
        )
        .where(Refund.tenant_id == ctx.tenant_id)
    )
    if scope is ListScope.OWN:
        query = query.where(Checkout.buyer_ref == ctx.buyer_ref)
    return query


def _refund_counts(session: Session, ctx: RequestContext, scope: ListScope) -> dict[str, int]:
    wire = _wire_state()
    query = (
        select(wire.label("wire_state"), func.count().label("total"))
        .select_from(Refund)
        .join(
            Checkout, (Checkout.tenant_id == Refund.tenant_id) & (Checkout.id == Refund.checkout_id)
        )
        .outerjoin(
            Order,
            (Order.tenant_id == Refund.tenant_id)
            & (Order.payment_attempt_id == Refund.payment_attempt_id),
        )
        .where(Refund.tenant_id == ctx.tenant_id)
    )
    if scope is ListScope.OWN:
        query = query.where(Checkout.buyer_ref == ctx.buyer_ref)
    counted = {str(row.wire_state): int(row.total) for row in session.execute(query.group_by(wire))}
    return {member.value: counted.get(member.value, 0) for member in REFUND_WIRE_STATES}


def _refund_item(row: Any, *, age_seconds: int) -> RefundListItemOut:
    refund: Refund = row.Refund
    amount = Money(int(refund.amount_minor), str(refund.currency))
    captured = (
        None if row.captured_minor is None else Money(int(row.captured_minor), amount.currency)
    )
    status = RefundStatus(refund.status)
    if status is RefundStatus.PROCESSED and captured is not None and amount < captured:
        state = tk.PaymentState.PARTIALLY_REFUNDED
    else:
        state = _REFUND_STATE[status]
    return RefundListItemOut(
        refund_id=str(refund.id),
        order_id=uuid_str(row.order_id),
        order_reference=None if row.order_id is None else order_reference(row.order_id),
        checkout_id=str(refund.checkout_id),
        payment_attempt_id=str(refund.payment_attempt_id),
        amount_minor=amount.minor,
        currency=amount.currency,
        amount=MoneyOut.of(amount),
        captured_minor=None if captured is None else captured.minor,
        state=state,
        row_status=status.value,
        reason=str(refund.reason_code),
        automatic=refund.reason_code == STALE_CAPTURE_REASON or bool(refund.provider_originated),
        provider_refund_id=refund.provider_refund_id,
        created_at=rfc3339(refund.created_at),
        updated_at=rfc3339(refund.updated_at),
        age_seconds=age_seconds,
    )


#: ``refund_service._REFUND_STATE``, restated here rather than imported so this module
#: depends on the kernel's vocabulary and not on a sibling's private name.
_REFUND_STATE: Final[dict[RefundStatus, tk.PaymentState]] = {
    RefundStatus.PENDING: tk.PaymentState.REFUND_PENDING,
    RefundStatus.FAILED: tk.PaymentState.REFUND_FAILED,
    RefundStatus.UNKNOWN: tk.PaymentState.REFUND_UNKNOWN,
    RefundStatus.RECONCILING: tk.PaymentState.RECONCILING,
    RefundStatus.ESCALATED: tk.PaymentState.ESCALATED,
    RefundStatus.PROCESSED: tk.PaymentState.REFUNDED,
}


def list_refunds(
    session: Session,
    ctx: RequestContext,
    *,
    operator: bool,
    state: tk.PaymentState | None,
    limit: int,
    cursor: str | None,
) -> RefundsPageOut:
    """One page of refunds, newest first, in the widest scope this request may see.

    ``state`` filters on the wire state, which is what an operator reasons in; the row's
    own ``status`` is reported alongside as ``row_status`` for anyone reconciling against
    the database directly.
    """
    scope = scope_of(ctx, operator=operator)
    if state is not None and state not in REFUND_WIRE_STATES:
        raise ProblemError(
            422,
            "Not a refund state",
            "Filter by one of the states a refund can be in.",
            state=state.value,
            allowed=[member.value for member in REFUND_WIRE_STATES],
        )
    age = _age_seconds(Refund.created_at).label("age_seconds")
    query = _refunds_base(ctx, scope).add_columns(age)
    if state is not None:
        query = query.where(_wire_state() == state.value)
    if cursor is not None:
        after = Cursor.decode(cursor)
        query = query.where(tuple_(Refund.created_at, Refund.id) < (after.created_at, after.row_id))
    rows = session.execute(
        query.order_by(Refund.created_at.desc(), Refund.id.desc()).limit(limit + 1)
    ).all()
    page, more = rows[:limit], len(rows) > limit
    last = page[-1].Refund if page and more else None
    return RefundsPageOut(
        refunds=[_refund_item(row, age_seconds=int(row.age_seconds)) for row in page],
        next_cursor=None if last is None else Cursor(last.created_at, last.id).encode(),
        limit=limit,
        scope=scope,
        counts=_refund_counts(session, ctx, scope),
    )


# -------------------------------------------------------------------------- checkouts


def _checkout_state(current: Any) -> ColumnElement[str]:
    """The state of a checkout, from its current version, falling back to the head.

    ``checkout_versions.status`` is the truth and ``checkouts.status`` is a denormalised
    copy of it kept for cheap reads -- the head table says so itself. Reading the version
    means the filter, the counts and the row can never disagree with the checkout screen,
    which reads the same row.

    The fallback exists because the join to the current version is an outer one, for the
    reason the orders query gives: an inner join would drop a head whose version cannot
    be read, and a buyer's own list quietly losing a row is worse than a row missing an
    amount.
    """
    return func.coalesce(current.status, Checkout.status)


def _checkouts_query(
    ctx: RequestContext,
    scope: ListScope,
    state: tk.CheckoutState | None,
    *,
    live: bool,
) -> Select[Any]:
    current = aliased(CheckoutVersion, name="current")
    state_of = _checkout_state(current)
    query = (
        select(
            Checkout.id,
            Checkout.cart_id,
            Checkout.created_at,
            Checkout.updated_at,
            state_of.label("state"),
            func.coalesce(current.version, Checkout.current_version).label("version"),
            current.total_minor,
            current.currency,
            current.content_hash,
            _age_seconds(Checkout.created_at).label("age_seconds"),
        )
        .outerjoin(
            current,
            (current.tenant_id == Checkout.tenant_id)
            & (current.checkout_id == Checkout.id)
            & (current.version == Checkout.current_version),
        )
        .where(Checkout.tenant_id == ctx.tenant_id)
    )
    if scope is ListScope.OWN:
        # ``ix_checkouts_tenant_buyer`` is on exactly this pair.
        query = query.where(Checkout.buyer_ref == ctx.buyer_ref)
    if live:
        query = query.where(
            state_of.in_([member.value for member in sorted(NON_TERMINAL_CHECKOUT_STATES)])
        )
    if state is not None:
        query = query.where(state_of == state.value)
    return query


def _checkout_counts(session: Session, ctx: RequestContext, scope: ListScope) -> dict[str, int]:
    """Every state's total across the whole scope, unfiltered by ``state`` or ``live``.

    Across the scope rather than the page, and across every state rather than the ones
    asked for, so a caller that requested only live checkouts is still told that terminal
    ones exist. A count that moved with the filter would make "none of these" and "none
    at all" the same answer.
    """
    current = aliased(CheckoutVersion, name="counted")
    state_of = _checkout_state(current)
    query = (
        select(state_of.label("state"), func.count().label("total"))
        .select_from(Checkout)
        .outerjoin(
            current,
            (current.tenant_id == Checkout.tenant_id)
            & (current.checkout_id == Checkout.id)
            & (current.version == Checkout.current_version),
        )
        .where(Checkout.tenant_id == ctx.tenant_id)
    )
    if scope is ListScope.OWN:
        query = query.where(Checkout.buyer_ref == ctx.buyer_ref)
    counted = {str(row.state): int(row.total) for row in session.execute(query.group_by(state_of))}
    return {member.value: counted.get(member.value, 0) for member in tk.CheckoutState}


def _checkout_summary(row: Any) -> CheckoutSummaryOut:
    state = tk.CheckoutState(row.state)
    amount = None if row.total_minor is None else Money(int(row.total_minor), str(row.currency))
    return CheckoutSummaryOut(
        checkout_id=str(row.id),
        cart_id=str(row.cart_id),
        state=state,
        version=int(row.version),
        # From the kernel's own set, resolved here so the client never holds a copy of it.
        live=state not in TERMINAL_CHECKOUT_STATES,
        amount_minor=None if amount is None else amount.minor,
        currency=None if amount is None else amount.currency,
        amount=None if amount is None else MoneyOut.of(amount),
        content_hash=None if row.content_hash is None else str(row.content_hash),
        created_at=rfc3339(row.created_at),
        updated_at=rfc3339(row.updated_at),
        age_seconds=int(row.age_seconds),
    )


def list_checkouts(
    session: Session,
    ctx: RequestContext,
    *,
    operator: bool,
    state: tk.CheckoutState | None,
    live: bool,
    limit: int,
    cursor: str | None,
) -> CheckoutsPageOut:
    """One page of checkouts, newest first, in the widest scope this request may see.

    This is how a surface that has lost its place finds it again. A checkout is reachable
    by its identifier and by the cart it closed, and a client that kept neither -- a
    reload, a second device, a voice session that never had a browser -- had no way back
    to a checkout it had already opened. ``live=true`` is that way back: the buyer's own
    unfinished checkouts, decided by the kernel's ``NON_TERMINAL_CHECKOUT_STATES`` rather
    than by the caller's idea of which states are finished.
    """
    scope = scope_of(ctx, operator=operator)
    if live and state is not None and state in TERMINAL_CHECKOUT_STATES:
        # Refused rather than answered with an empty page. The two filters contradict each
        # other, and an empty page would read as "you have none of those" -- which is a
        # statement about the buyer's checkouts, not about the request being impossible.
        raise ProblemError(
            422,
            "Contradictory filters",
            f"{state.value} is a state a checkout has finished in, so it cannot also be "
            "live. Ask for one or the other.",
            state=state.value,
            live=live,
        )
    query = _checkouts_query(ctx, scope, state, live=live)
    if cursor is not None:
        after = Cursor.decode(cursor)
        query = query.where(
            tuple_(Checkout.created_at, Checkout.id) < (after.created_at, after.row_id)
        )
    rows = session.execute(
        query.order_by(Checkout.created_at.desc(), Checkout.id.desc()).limit(limit + 1)
    ).all()
    page, more = rows[:limit], len(rows) > limit
    last = page[-1] if page and more else None
    return CheckoutsPageOut(
        checkouts=[_checkout_summary(row) for row in page],
        next_cursor=None if last is None else Cursor(last.created_at, last.id).encode(),
        limit=limit,
        scope=scope,
        counts=_checkout_counts(session, ctx, scope),
    )
