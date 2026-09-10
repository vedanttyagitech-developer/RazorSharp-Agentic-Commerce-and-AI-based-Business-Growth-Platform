"""Checkout and payment state machines, specification 10.4, 10.5, 10.7 and 10.8.

Pure transition tables. Nothing here reads a clock, touches a database, calls a provider
or consults a model: given the same pair of states, every pod on every day returns the
same answer. That determinism is the point. The kernel's admission transaction decides
*whether* a move is permitted by policy and by locked rows; this module decides whether
the move is *representable at all*, and it is the last line of defence when a webhook,
a retry or a race proposes something the lifecycle does not allow.

Two functions serve two different callers and must not be confused:

``assert_transition`` is for a deliberate, single, locked step taken by the kernel or a
worker. It refuses anything that is not a declared edge.

``monotonic_apply`` is for the webhook inbox, where events are duplicated, delayed and
delivered out of order. It joins the state we hold with the state an event reports and
returns the winner, never a regression. It may skip intermediate states, because the
provider genuinely skipped them in what it told us, but it can never park an attempt in
a state the machine could not have reached.

The invariants encoded below are the ones that cost real money when they break:

1. Version N, once ``INVALIDATED``, never returns to ``APPROVED``. A corrected purchase
   is version N+1 with a new hash and a new approval.
2. ``CAPTURED`` never regresses to ``AUTHORIZED``. An ``authorized`` webhook overtaken by
   a ``captured`` webhook is a no-op, not a rewind.
3. ``UNKNOWN`` leaves only through ``RECONCILING``. There is no ``UNKNOWN -> FAILED``
   edge in either direction of this module, because a request timing out is not evidence
   that the buyer's money stayed put.
4. ``REFUND_UNKNOWN`` leaves only through ``RECONCILING`` for the same reason, while
   ``REFUND_FAILED`` -- a provider-confirmed failure, no refund exists -- may retry. The
   two are not interchangeable; conflating them is how a buyer gets refunded twice.
5. Terminal states reject every outgoing transition, in both functions.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from enum import StrEnum
from typing import Final, overload

from commerce_domain import DomainError

__all__ = [
    "CHECKOUT_TRANSITIONS",
    "NON_TERMINAL_CHECKOUT_STATES",
    "NON_TERMINAL_PAYMENT_STATES",
    "PAYMENT_TRANSITIONS",
    "TERMINAL_CHECKOUT_STATES",
    "TERMINAL_PAYMENT_STATES",
    "UNCERTAIN_PAYMENT_STATES",
    "CheckoutState",
    "InvalidTransitionError",
    "PaymentState",
    "assert_transition",
    "can_transition",
    "is_terminal",
    "monotonic_apply",
    "reachable_states",
]


# --------------------------------------------------------------------------- states


class CheckoutState(StrEnum):
    """Lifecycle of one immutable checkout version, specification 10.4.

    These are the states of *a version*, not of a shopping session. A material change
    does not move a version backwards; it ends this version and starts the next one.
    """

    DRAFT = "DRAFT"
    QUOTED = "QUOTED"
    RESERVED = "RESERVED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVED = "APPROVED"
    EXECUTION_PENDING = "EXECUTION_PENDING"
    AWAITING_PAYMENT = "AWAITING_PAYMENT"
    PAID = "PAID"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    PAYMENT_UNKNOWN = "PAYMENT_UNKNOWN"
    INVALIDATED = "INVALIDATED"
    INVALIDATED_AWAITING_PAYMENT_RESULT = "INVALIDATED_AWAITING_PAYMENT_RESULT"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class PaymentState(StrEnum):
    """Lifecycle of one payment attempt and of the refunds against it, specification 10.5.

    Refund states live on the attempt rather than in a separate machine because a refund
    is only ever meaningful against a specific capture; separating them would allow a
    refund whose captured amount is not in view.
    """

    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    AUTHORIZED = "AUTHORIZED"
    CAPTURED = "CAPTURED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"
    ESCALATED = "ESCALATED"
    STALE_CAPTURE = "STALE_CAPTURE"

    REFUND_PENDING = "REFUND_PENDING"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    REFUNDED = "REFUNDED"
    REFUND_UNKNOWN = "REFUND_UNKNOWN"
    REFUND_FAILED = "REFUND_FAILED"
    AUTO_REFUND_PENDING = "AUTO_REFUND_PENDING"


class InvalidTransitionError(DomainError):
    """A transition that the lifecycle does not permit was attempted.

    This is an internal consistency failure, not a business outcome, and it is
    deliberately *not* carried to a buyer as a ``RecoveryCode``. The kernel decides which
    code its decision returns from the reason it attempted the move -- a refused move to
    ``APPROVED`` on an invalidated version is ``REAPPROVAL_REQUIRED``, a refused move
    lost to a concurrent winner is ``CONCURRENT_OPERATION`` -- because the correct code
    depends on why, and this module only knows what.
    """

    def __init__(self, current: StrEnum, target: StrEnum) -> None:
        self.current = current
        self.target = target
        machine = type(current).__name__
        super().__init__(f"{machine}: {current.value} -> {target.value} is not a legal transition")


# ---------------------------------------------------------------- checkout transitions

_C = CheckoutState

CHECKOUT_TRANSITIONS: Final[Mapping[CheckoutState, frozenset[CheckoutState]]] = {
    _C.DRAFT: frozenset({_C.QUOTED, _C.CANCELLED, _C.EXPIRED}),
    # A re-quote is a new version, not a self-loop: the buyer approved specific bytes and
    # a changed quote must be re-approved against a new hash.
    _C.QUOTED: frozenset(
        {_C.APPROVAL_REQUIRED, _C.RESERVED, _C.INVALIDATED, _C.CANCELLED, _C.EXPIRED}
    ),
    _C.RESERVED: frozenset({_C.APPROVAL_REQUIRED, _C.INVALIDATED, _C.CANCELLED, _C.EXPIRED}),
    _C.APPROVAL_REQUIRED: frozenset({_C.APPROVED, _C.INVALIDATED, _C.CANCELLED, _C.EXPIRED}),
    _C.APPROVED: frozenset({_C.EXECUTION_PENDING, _C.INVALIDATED, _C.CANCELLED, _C.EXPIRED}),
    # EXECUTION_PENDING means a grant is issued and the create-order command is in the
    # outbox, but no provider order is confirmed yet. Plain INVALIDATED and CANCELLED are
    # reachable here because the grant can still be revoked before consumption; the row
    # lock, not this table, decides who wins that race against the worker.
    # A create-order that times out is PAYMENT_UNKNOWN, never PAYMENT_FAILED: the order
    # may exist, and specification 10.6 requires a lookup by stable receipt before any
    # second create.
    _C.EXECUTION_PENDING: frozenset(
        {
            _C.AWAITING_PAYMENT,
            _C.PAYMENT_FAILED,
            _C.PAYMENT_UNKNOWN,
            _C.INVALIDATED,
            _C.CANCELLED,
            _C.EXPIRED,
        }
    ),
    # Once a payment surface is open, the checkout can no longer be invalidated,
    # cancelled or expired outright: money may already be in flight. Every one of those
    # intents routes through INVALIDATED_AWAITING_PAYMENT_RESULT so that a late capture
    # is refunded rather than orphaned (specification 10.8).
    _C.AWAITING_PAYMENT: frozenset(
        {
            _C.PAID,
            _C.PAYMENT_FAILED,
            _C.PAYMENT_UNKNOWN,
            _C.INVALIDATED_AWAITING_PAYMENT_RESULT,
        }
    ),
    # A confirmed failure releases the reservation, and a policy-safe retry re-enters
    # admission, which issues a *new* single-use grant. It never reuses the old one.
    _C.PAYMENT_FAILED: frozenset({_C.EXECUTION_PENDING, _C.INVALIDATED, _C.CANCELLED, _C.EXPIRED}),
    # PAYMENT_UNKNOWN deliberately has no edge to CANCELLED or EXPIRED. The reservation
    # is held while an outcome is unknown (specification 10.4); releasing it would let a
    # second buyer take stock that a possibly-successful payment already bought.
    # It resolves only from verified provider evidence carried by the payment attempt's
    # own RECONCILING step -- see PaymentState, where the UNKNOWN rule is enforced.
    _C.PAYMENT_UNKNOWN: frozenset(
        {_C.PAID, _C.PAYMENT_FAILED, _C.INVALIDATED_AWAITING_PAYMENT_RESULT}
    ),
    # There is no edge to PAID. An invalidated version is never fulfilled, whatever the
    # payment result turns out to be; a late capture becomes a stale capture and is
    # refunded against the attempt, and any corrected purchase is version N+1.
    _C.INVALIDATED_AWAITING_PAYMENT_RESULT: frozenset({_C.INVALIDATED}),
    _C.PAID: frozenset(),
    _C.INVALIDATED: frozenset(),
    _C.CANCELLED: frozenset(),
    _C.EXPIRED: frozenset(),
}

#: Declared by hand rather than derived, so that adding a state with no outgoing edges
#: and forgetting to classify it fails the table-integrity test instead of silently
#: becoming a dead end that quietly swallows checkouts.
TERMINAL_CHECKOUT_STATES: Final[frozenset[CheckoutState]] = frozenset(
    {_C.PAID, _C.INVALIDATED, _C.CANCELLED, _C.EXPIRED}
)

NON_TERMINAL_CHECKOUT_STATES: Final[frozenset[CheckoutState]] = (
    frozenset(CheckoutState) - TERMINAL_CHECKOUT_STATES
)


# ----------------------------------------------------------------- payment transitions

_P = PaymentState

PAYMENT_TRANSITIONS: Final[Mapping[PaymentState, frozenset[PaymentState]]] = {
    # A create-order call that loses its response is UNKNOWN, not FAILED: the provider
    # order may exist and must be looked up by stable receipt (specification 10.6).
    _P.CREATED: frozenset({_P.SUBMITTED, _P.FAILED, _P.EXPIRED, _P.UNKNOWN}),
    # Deliberately no SUBMITTED -> EXPIRED edge. Once the buyer has been sent to a
    # payment surface, a silent absence of news is UNKNOWN. Calling it "expired" on a
    # local timer is the same mistake as calling it "failed".
    _P.SUBMITTED: frozenset({_P.AUTHORIZED, _P.CAPTURED, _P.FAILED, _P.UNKNOWN}),
    # No AUTHORIZED -> FAILED edge: an authorization that exists does not become a
    # non-event because a later capture call was refused. If the bound checkout was
    # invalidated, specification 10.8 says do not capture -- release it through
    # AUTO_REFUND_PENDING instead.
    _P.AUTHORIZED: frozenset({_P.CAPTURED, _P.AUTO_REFUND_PENDING, _P.UNKNOWN}),
    # No path back to AUTHORIZED, FAILED or EXPIRED. Money moved; the only questions
    # left are whether it is refunded and whether the capture was stale.
    _P.CAPTURED: frozenset({_P.REFUND_PENDING, _P.STALE_CAPTURE}),
    _P.FAILED: frozenset(),
    _P.EXPIRED: frozenset(),
    # Invariant 3. The single outgoing edge is the whole point: an unknown outcome is
    # owned by the Reconciliation Service and is resolved by querying the provider by
    # authoritative identifier, never by a timeout, a UI, or an operator's assumption.
    _P.UNKNOWN: frozenset({_P.RECONCILING}),
    # Reconciliation is shared by payment uncertainty and refund uncertainty, so it
    # resolves into both branches. STALE_CAPTURE is reachable directly so that a capture
    # discovered against an invalidated checkout never spends a moment reading CAPTURED,
    # which is the read a fulfilment job would act on.
    _P.RECONCILING: frozenset(
        {
            _P.AUTHORIZED,
            _P.CAPTURED,
            _P.FAILED,
            _P.EXPIRED,
            _P.ESCALATED,
            _P.STALE_CAPTURE,
            _P.REFUNDED,
            _P.PARTIALLY_REFUNDED,
            _P.REFUND_FAILED,
        }
    ),
    # Terminal on purpose. ESCALATED freezes the attempt and opens exactly one human
    # review case; if any automated edge left this state, a retry loop or a replayed
    # webhook could unfreeze it. A reviewer acting through the operator path resumes the
    # work as a new attempt carrying recorded operator authority, not as an edge here.
    _P.ESCALATED: frozenset(),
    # A stale capture is refunded in full. It is never fulfilled and never "un-staled".
    _P.STALE_CAPTURE: frozenset({_P.REFUND_PENDING}),
    # An automatic refund call has the same three outcomes as any other refund call. The
    # spec diagram shows only the happy one; omitting the other two here would leave a
    # timed-out auto-refund with nowhere legal to go, which is how it gets retried blind.
    _P.AUTO_REFUND_PENDING: frozenset({_P.REFUNDED, _P.REFUND_UNKNOWN, _P.REFUND_FAILED}),
    _P.REFUND_PENDING: frozenset(
        {_P.PARTIALLY_REFUNDED, _P.REFUNDED, _P.REFUND_UNKNOWN, _P.REFUND_FAILED}
    ),
    # A further partial refund is a fresh kernel admission and a fresh grant; the amount
    # ceiling is enforced by the Resolution Service, not by this table.
    _P.PARTIALLY_REFUNDED: frozenset({_P.REFUND_PENDING}),
    _P.REFUNDED: frozenset(),
    # Invariant 4, half one. The provider outcome is genuinely unknown and a refund may
    # already exist, so there is no edge to REFUND_PENDING: issuing another grant here is
    # exactly how a buyer is refunded twice.
    _P.REFUND_UNKNOWN: frozenset({_P.RECONCILING}),
    # Invariant 4, half two. The provider confirmed no refund exists, so a bounded retry
    # through a fresh admission is safe, and exhausting the retries escalates.
    _P.REFUND_FAILED: frozenset({_P.REFUND_PENDING, _P.ESCALATED}),
}

TERMINAL_PAYMENT_STATES: Final[frozenset[PaymentState]] = frozenset(
    {_P.FAILED, _P.EXPIRED, _P.ESCALATED, _P.REFUNDED}
)

NON_TERMINAL_PAYMENT_STATES: Final[frozenset[PaymentState]] = (
    frozenset(PaymentState) - TERMINAL_PAYMENT_STATES
)

#: States where the provider's answer is genuinely not known. Their only lawful exit is
#: reconciliation against authoritative identifiers. Everything else is a guess.
UNCERTAIN_PAYMENT_STATES: Final[frozenset[PaymentState]] = frozenset(
    {_P.UNKNOWN, _P.REFUND_UNKNOWN}
)


# ------------------------------------------------------------------- monotonic ranking

#: How far an attempt has progressed along the axis that matters: how much is known and
#: how far the money has moved. ``monotonic_apply`` never returns a state that ranks
#: below the one already held, which is what makes an out-of-order webhook harmless.
#:
#: Two placements carry the weight:
#: * every state in which money has demonstrably moved outranks every state in which it
#:   has not, so CAPTURED beats a late AUTHORIZED and a completed refund beats a later
#:   REFUND_FAILED report;
#: * ESCALATED ranks highest, so a frozen attempt is never thawed by an inbound event.
_RANK: Final[Mapping[PaymentState, int]] = {
    _P.CREATED: 0,
    _P.SUBMITTED: 1,
    _P.UNKNOWN: 2,
    _P.RECONCILING: 3,
    _P.EXPIRED: 4,
    _P.FAILED: 5,
    _P.AUTHORIZED: 6,
    _P.CAPTURED: 7,
    _P.STALE_CAPTURE: 8,
    _P.AUTO_REFUND_PENDING: 9,
    _P.REFUND_PENDING: 10,
    _P.REFUND_UNKNOWN: 11,
    _P.REFUND_FAILED: 12,
    _P.PARTIALLY_REFUNDED: 13,
    _P.REFUNDED: 14,
    _P.ESCALATED: 15,
}


def _transitive_closure[S: StrEnum](
    table: Mapping[S, frozenset[S]],
    frontier: frozenset[S] = frozenset(),
) -> dict[S, frozenset[S]]:
    """Every state reachable from each state in one or more steps.

    A state in ``frontier`` is included as a destination but not expanded through, so the
    walk stops there. Computed once at import; the tables never change at runtime.
    """
    closure: dict[S, frozenset[S]] = {}
    for origin in table:
        seen: set[S] = set()
        queue: deque[S] = deque(table[origin])
        while queue:
            node = queue.popleft()
            if node in seen:
                continue
            seen.add(node)
            if node not in frontier:
                queue.extend(table[node])
        closure[origin] = frozenset(seen)
    return closure


_CHECKOUT_CLOSURE: Final[Mapping[CheckoutState, frozenset[CheckoutState]]] = _transitive_closure(
    CHECKOUT_TRANSITIONS
)
_PAYMENT_CLOSURE: Final[Mapping[PaymentState, frozenset[PaymentState]]] = _transitive_closure(
    PAYMENT_TRANSITIONS
)

#: Where an inbound provider event is permitted to advance an attempt to.
#:
#: This is the transition closure with ``RECONCILING`` as a frontier: an event may carry
#: the attempt *into* reconciliation, but the walk never continues *through* it. The
#: reason is that reconciliation is a deliberate act -- the platform queries the provider
#: by authoritative identifier and records what came back -- and an inbound webhook is not
#: entitled to imply that it happened.
#:
#: Without this stop, almost every state would be reachable from almost every other one,
#: because ``REFUND_UNKNOWN -> RECONCILING -> AUTHORIZED`` loops the graph back on itself.
#: A webhook could then land an attempt in ``AUTO_REFUND_PENDING`` -- a decision the
#: platform makes about an authorization, never something a provider reports -- or expire
#: a ``SUBMITTED`` attempt that has not been verified. Both are states no legal single
#: step could have produced from where the attempt actually was.
_EVENT_ADVANCE: Final[Mapping[PaymentState, frozenset[PaymentState]]] = _transitive_closure(
    PAYMENT_TRANSITIONS, frozenset({PaymentState.RECONCILING})
)


# --------------------------------------------------------------------------- public API


def is_terminal(state: CheckoutState | PaymentState) -> bool:
    """True when no transition may leave ``state``.

    Guarantees that a terminal state is a genuine dead end in both directions of this
    module: ``can_transition`` refuses every target from it, and ``monotonic_apply``
    returns it unchanged for every inbound event.
    """
    if isinstance(state, CheckoutState):
        return state in TERMINAL_CHECKOUT_STATES
    return state in TERMINAL_PAYMENT_STATES


def _successors(
    state: CheckoutState | PaymentState,
) -> frozenset[CheckoutState] | frozenset[PaymentState]:
    if isinstance(state, CheckoutState):
        return CHECKOUT_TRANSITIONS[state]
    return PAYMENT_TRANSITIONS[state]


def _require_same_machine(current: StrEnum, target: StrEnum) -> None:
    """Refuse a checkout state compared against a payment state.

    Both machines are ``StrEnum``, so ``CheckoutState.EXPIRED == PaymentState.EXPIRED``
    is ``True`` by plain string equality. Silently answering such a pair would let a
    caller mix the two lifecycles and get a plausible-looking wrong answer, so the
    mismatch is raised rather than resolved.
    """
    if type(current) is not type(target):
        raise TypeError(
            "cannot compare states from different machines: "
            f"{type(current).__name__}.{current.value} and {type(target).__name__}.{target.value}"
        )


@overload
def can_transition(current: CheckoutState, target: CheckoutState) -> bool: ...


@overload
def can_transition(current: PaymentState, target: PaymentState) -> bool: ...


def can_transition(
    current: CheckoutState | PaymentState, target: CheckoutState | PaymentState
) -> bool:
    """True when ``current -> target`` is a declared edge of that machine.

    Refuses self-transitions: re-applying a state the row already holds is either a bug
    or a duplicate event, and the caller must handle it explicitly -- webhook redelivery
    goes through ``monotonic_apply``, which is idempotent by design.

    Raises ``TypeError`` if the two states belong to different machines.
    """
    _require_same_machine(current, target)
    return target in _successors(current)


@overload
def assert_transition(current: CheckoutState, target: CheckoutState) -> None: ...


@overload
def assert_transition(current: PaymentState, target: PaymentState) -> None: ...


def assert_transition(
    current: CheckoutState | PaymentState, target: CheckoutState | PaymentState
) -> None:
    """Permit a single deliberate step, or raise ``InvalidTransitionError``.

    This is the guard for a locked, intentional move made by the kernel or a worker. It
    refuses every non-edge, including every move out of a terminal state and every
    self-transition. It does not decide policy, freshness or authority -- only shape.

    Raises ``TypeError`` if the two states belong to different machines.
    """
    _require_same_machine(current, target)
    if target not in _successors(current):
        raise InvalidTransitionError(current, target)


@overload
def reachable_states(state: CheckoutState) -> frozenset[CheckoutState]: ...


@overload
def reachable_states(state: PaymentState) -> frozenset[PaymentState]: ...


def reachable_states(
    state: CheckoutState | PaymentState,
) -> frozenset[CheckoutState] | frozenset[PaymentState]:
    """Every state reachable from ``state`` in one or more transitions.

    Exposed so that a caller can assert a whole-lifecycle property -- that an invalidated
    version can never become approved by any route, however long -- rather than only
    checking the next hop. An empty result means ``state`` is terminal.
    """
    if isinstance(state, CheckoutState):
        return _CHECKOUT_CLOSURE[state]
    return _PAYMENT_CLOSURE[state]


def monotonic_apply(current: PaymentState, incoming: PaymentState) -> PaymentState:
    """Join the state we hold with the state an event reports; return the winner.

    This is the function the webhook inbox calls after HMAC verification and dedup, where
    events arrive duplicated, late and out of order. It guarantees:

    * **Idempotence.** ``monotonic_apply(s, s) is s``, so a redelivered event changes
      nothing.
    * **No regression.** The result never ranks below ``current`` on the money axis. An
      ``authorized`` event arriving after a capture returns ``CAPTURED``; a stale
      ``failed`` event arriving after a capture returns ``CAPTURED``.
    * **Terminal absorption.** A terminal ``current`` is returned unchanged for every
      inbound state, so no event revives a failed, expired, refunded or escalated
      attempt. Contradictory evidence is not discarded -- the inbox has already stored
      the raw event -- it is simply not applied here, and reconciliation adjudicates it.
    * **Uncertainty is never resolved by an event.** When ``current`` is ``UNKNOWN`` or
      ``REFUND_UNKNOWN``, any stronger inbound evidence yields ``RECONCILING`` rather
      than the reported state. This is the one intentional rank decrease, and it is what
      keeps invariants 3 and 4 true: the Reconciliation Service confirms the outcome
      against authoritative identifiers before the attempt claims it. In particular an
      inbound ``failed`` can never turn an ``UNKNOWN`` into a ``FAILED``, and an inbound
      refund result can never authorize a second refund attempt.
    * **Reachability.** The result is always ``current`` itself or a state reachable from
      ``current`` through the transition table without passing through ``RECONCILING``.
      Intermediate states may be skipped, because the provider may genuinely have skipped
      telling us about them, but an event may never imply that a reconciliation ran, so
      the inbox can never park an attempt in a state that only a verified provider query
      could have justified.

    It refuses no *event*: a webhook is not a request, and rejecting one would only mean
    losing evidence. It does refuse a state belonging to the other machine, which is a
    programming error rather than an event, and raises ``TypeError`` for it.
    """
    # Both machines are StrEnum, so CheckoutState.EXPIRED equals PaymentState.EXPIRED and
    # hashes alike. Without this guard a CheckoutState is silently looked up in the
    # payment rank and advance tables and answered: monotonic_apply(CAPTURED, C.EXPIRED)
    # returned CAPTURED, a confident answer to a meaningless question. can_transition and
    # assert_transition already refuse such a pair; the inbox path must refuse it too.
    if type(current) is not PaymentState or type(incoming) is not PaymentState:
        raise TypeError(
            "monotonic_apply takes payment states only; states from different machines "
            f"were given: {type(current).__name__}.{current} and "
            f"{type(incoming).__name__}.{incoming}"
        )

    if current is incoming:
        return current

    # Invariant 5. Terminal absorbs everything; a frozen or finished attempt is not
    # reopened by an inbound event, only by a fresh, authorized operation.
    if current in TERMINAL_PAYMENT_STATES:
        return current

    # Invariants 3 and 4. Evidence that something happened moves an uncertain attempt
    # into reconciliation, never straight into the reported outcome.
    if current in UNCERTAIN_PAYMENT_STATES:
        if incoming is PaymentState.RECONCILING or _RANK[incoming] > _RANK[current]:
            return PaymentState.RECONCILING
        return current

    if _RANK[incoming] > _RANK[current] and incoming in _EVENT_ADVANCE[current]:
        return incoming
    return current
