"""These tests exist because each of them corresponds to a way real money goes wrong.

A regression here is not a style problem: it is a buyer refunded twice, a checkout
fulfilled after it was invalidated, or an unknown outcome written off as a failure
because a browser tab closed. Every assertion below names the failure it prevents.
"""

import re
from collections import deque

import pytest
from platform_db.schema import CheckoutVersion, PaymentAttempt
from sqlalchemy import CheckConstraint
from transaction_kernel.states import (
    CHECKOUT_TRANSITIONS,
    NON_TERMINAL_CHECKOUT_STATES,
    NON_TERMINAL_PAYMENT_STATES,
    PAYMENT_TRANSITIONS,
    TERMINAL_CHECKOUT_STATES,
    TERMINAL_PAYMENT_STATES,
    UNCERTAIN_PAYMENT_STATES,
    CheckoutState,
    InvalidTransitionError,
    PaymentState,
    assert_transition,
    can_transition,
    is_terminal,
    monotonic_apply,
    reachable_states,
)

C = CheckoutState
P = PaymentState

#: States that only exist after the buyer has actually been debited.
MONEY_MOVED = frozenset(
    {
        P.CAPTURED,
        P.STALE_CAPTURE,
        P.REFUND_PENDING,
        P.PARTIALLY_REFUNDED,
        P.REFUNDED,
        P.REFUND_UNKNOWN,
        P.REFUND_FAILED,
    }
)

#: States that positively assert the buyer was not debited. RECONCILING and ESCALATED are
#: in neither set: they make no claim about the money, which is exactly their purpose.
NO_MONEY_MOVED = frozenset({P.CREATED, P.SUBMITTED, P.AUTHORIZED, P.FAILED, P.EXPIRED})


def _event_advance_bound() -> dict[PaymentState, frozenset[PaymentState]]:
    """Where an inbound event may legitimately advance an attempt to.

    Re-derived here from the public ``PAYMENT_TRANSITIONS`` table rather than imported
    from the module, so that the exhaustive test below checks ``monotonic_apply`` against
    an independent statement of the rule instead of restating the very helper it uses.

    The walk stops at RECONCILING rather than expanding through it. Without that stop the
    graph loops back on itself -- REFUND_UNKNOWN -> RECONCILING -> AUTHORIZED -- and
    nearly every state becomes reachable from nearly every other, which is what made the
    earlier version of this test unable to fail.
    """
    bound: dict[PaymentState, frozenset[PaymentState]] = {}
    for origin in PaymentState:
        seen: set[PaymentState] = set()
        queue = deque(PAYMENT_TRANSITIONS[origin])
        while queue:
            node = queue.popleft()
            if node in seen:
                continue
            seen.add(node)
            if node is not P.RECONCILING:
                queue.extend(PAYMENT_TRANSITIONS[node])
        bound[origin] = frozenset(seen)
    return bound


EVENT_ADVANCE_BOUND = _event_advance_bound()


class TestTableIntegrity:
    """Structural guards. These fail loudly when someone adds a state and forgets it."""

    @pytest.mark.parametrize(
        ("enum", "table"),
        [(CheckoutState, CHECKOUT_TRANSITIONS), (PaymentState, PAYMENT_TRANSITIONS)],
    )
    def test_every_state_has_an_entry(self, enum, table):
        # A missing key would raise KeyError at admission time, in production, under a
        # row lock, rather than here.
        assert set(table) == set(enum)

    @pytest.mark.parametrize(
        ("enum", "table"),
        [(CheckoutState, CHECKOUT_TRANSITIONS), (PaymentState, PAYMENT_TRANSITIONS)],
    )
    def test_every_target_belongs_to_the_same_machine(self, enum, table):
        for origin, targets in table.items():
            for target in targets:
                assert type(target) is enum, f"{origin} -> {target} crosses machines"

    @pytest.mark.parametrize(
        ("table", "terminal"),
        [
            (CHECKOUT_TRANSITIONS, TERMINAL_CHECKOUT_STATES),
            (PAYMENT_TRANSITIONS, TERMINAL_PAYMENT_STATES),
        ],
    )
    def test_declared_terminal_set_matches_the_table(self, table, terminal):
        # The terminal sets are hand-declared so that this comparison has teeth: a new
        # dead-end state that nobody classified fails here instead of silently swallowing
        # checkouts or attempts forever.
        derived = {state for state, targets in table.items() if not targets}
        assert derived == set(terminal)

    @pytest.mark.parametrize(
        ("enum", "terminal", "non_terminal"),
        [
            (CheckoutState, TERMINAL_CHECKOUT_STATES, NON_TERMINAL_CHECKOUT_STATES),
            (PaymentState, TERMINAL_PAYMENT_STATES, NON_TERMINAL_PAYMENT_STATES),
        ],
    )
    def test_terminal_and_non_terminal_partition_the_enum(self, enum, terminal, non_terminal):
        assert terminal | non_terminal == set(enum)
        assert not terminal & non_terminal

    @pytest.mark.parametrize(
        ("table", "terminal"),
        [
            (CHECKOUT_TRANSITIONS, TERMINAL_CHECKOUT_STATES),
            (PAYMENT_TRANSITIONS, TERMINAL_PAYMENT_STATES),
        ],
    )
    def test_every_state_is_terminal_or_has_an_exit(self, table, terminal):
        """The walk the brief asks for: no silent, unreachable dead end.

        A non-terminal state with no outgoing edge is a checkout or a payment that can
        be entered and never resolved -- inventory held forever, money in limbo, and no
        code path that can move it.
        """
        for state, targets in table.items():
            if state in terminal:
                assert not targets, f"{state} is declared terminal but has exits {targets}"
            else:
                assert targets, f"{state} is non-terminal but has no outgoing transition"

    @pytest.mark.parametrize(
        ("start", "enum"), [(C.DRAFT, CheckoutState), (P.CREATED, PaymentState)]
    )
    def test_every_state_is_reachable_from_the_entry_state(self, start, enum):
        # An orphan state is dead code that reviewers still have to reason about, and a
        # status value the database can hold that no transition can produce.
        assert reachable_states(start) | {start} == set(enum)

    @pytest.mark.parametrize(
        "table", [CHECKOUT_TRANSITIONS, PAYMENT_TRANSITIONS], ids=["checkout", "payment"]
    )
    def test_no_self_transitions(self, table):
        # Re-applying the state a row already holds is either a bug or a duplicate event.
        # Duplicates belong to monotonic_apply, not to assert_transition.
        for state, targets in table.items():
            assert state not in targets


class TestCheckoutInvariants:
    def test_happy_path_is_walkable(self):
        path = [
            C.DRAFT,
            C.QUOTED,
            C.RESERVED,
            C.APPROVAL_REQUIRED,
            C.APPROVED,
            C.EXECUTION_PENDING,
            C.AWAITING_PAYMENT,
            C.PAID,
        ]
        for current, target in zip(path, path[1:], strict=False):
            assert_transition(current, target)

    def test_invalidated_version_can_never_return_to_approved(self):
        """Invariant 1, next hop.

        Version N carries the exact bytes the buyer approved. Reviving it after a
        material change would charge the buyer for a cart they never saw.
        """
        assert not can_transition(C.INVALIDATED, C.APPROVED)
        with pytest.raises(InvalidTransitionError, match="INVALIDATED -> APPROVED"):
            assert_transition(C.INVALIDATED, C.APPROVED)

    def test_invalidated_version_can_never_reach_approved_by_any_route(self):
        """Invariant 1, whole lifecycle.

        Refusing the direct edge is not enough: a three-hop path back to APPROVED would
        be just as wrong and far harder to spot in review.
        """
        assert reachable_states(C.INVALIDATED) == frozenset()

    def test_invalidated_version_can_never_reach_paid_or_execution(self):
        forbidden = {C.APPROVED, C.EXECUTION_PENDING, C.AWAITING_PAYMENT, C.PAID}
        assert not reachable_states(C.INVALIDATED) & forbidden

    def test_awaiting_payment_cannot_be_invalidated_outright(self):
        """Specification 10.8.

        A payment surface is open; money may already be moving. Jumping straight to
        INVALIDATED would leave a late capture with no state that owes the buyer a
        refund. Every such intent must route through INVALIDATED_AWAITING_PAYMENT_RESULT.
        """
        for target in (C.INVALIDATED, C.CANCELLED, C.EXPIRED):
            assert not can_transition(C.AWAITING_PAYMENT, target)
        assert can_transition(C.AWAITING_PAYMENT, C.INVALIDATED_AWAITING_PAYMENT_RESULT)

    def test_invalidated_awaiting_result_can_never_reach_paid(self):
        """An invalidated version is never fulfilled, whatever the payment turns out to be.

        A late capture is refunded against the payment attempt; the checkout stays
        invalidated and any corrected purchase is version N+1.
        """
        assert C.PAID not in reachable_states(C.INVALIDATED_AWAITING_PAYMENT_RESULT)
        assert reachable_states(C.INVALIDATED_AWAITING_PAYMENT_RESULT) == frozenset({C.INVALIDATED})

    def test_unknown_payment_holds_the_reservation(self):
        """Specification 10.4: the hold is kept while an outcome is unknown.

        Cancelling or expiring the checkout would release stock that a possibly
        successful payment has already bought, and sell it to someone else.
        """
        assert not can_transition(C.PAYMENT_UNKNOWN, C.CANCELLED)
        assert not can_transition(C.PAYMENT_UNKNOWN, C.EXPIRED)

    def test_unknown_payment_cannot_be_invalidated_outright(self):
        assert not can_transition(C.PAYMENT_UNKNOWN, C.INVALIDATED)
        assert can_transition(C.PAYMENT_UNKNOWN, C.INVALIDATED_AWAITING_PAYMENT_RESULT)

    def test_create_order_timeout_is_unknown_not_failed(self):
        # Specification 10.6: the provider order may exist. Calling it failed permits a
        # second create and a second charge.
        assert can_transition(C.EXECUTION_PENDING, C.PAYMENT_UNKNOWN)

    def test_confirmed_failure_permits_a_policy_safe_retry(self):
        # Retry re-enters admission, which issues a new single-use grant. The edge back
        # to EXECUTION_PENDING is what makes that a modelled step rather than a hack.
        assert can_transition(C.PAYMENT_FAILED, C.EXECUTION_PENDING)

    def test_paid_is_terminal_for_the_checkout(self):
        # Refunds live on the payment attempt. A refunded order does not un-pay its
        # checkout version; the version records what the buyer agreed to buy.
        assert is_terminal(C.PAID)

    @pytest.mark.parametrize("terminal", sorted(TERMINAL_CHECKOUT_STATES))
    def test_terminal_checkout_states_reject_every_target(self, terminal):
        """Invariant 5."""
        assert is_terminal(terminal)
        for target in CheckoutState:
            assert not can_transition(terminal, target)
            with pytest.raises(InvalidTransitionError):
                assert_transition(terminal, target)

    def test_self_transition_is_refused(self):
        with pytest.raises(InvalidTransitionError, match="APPROVED -> APPROVED"):
            assert_transition(C.APPROVED, C.APPROVED)


class TestPaymentInvariants:
    def test_captured_never_regresses_to_authorized(self):
        """Invariant 2, transition table half.

        Razorpay sends payment.authorized and payment.captured; nothing guarantees the
        order of delivery. A regression here would un-capture a real payment and open
        the door to charging again.
        """
        assert not can_transition(P.CAPTURED, P.AUTHORIZED)
        with pytest.raises(InvalidTransitionError, match="CAPTURED -> AUTHORIZED"):
            assert_transition(P.CAPTURED, P.AUTHORIZED)

    def test_captured_never_regresses_to_failed_or_expired(self):
        assert not can_transition(P.CAPTURED, P.FAILED)
        assert not can_transition(P.CAPTURED, P.EXPIRED)

    def test_unknown_leaves_only_through_reconciling(self):
        """Invariant 3.

        The single exit is the guarantee. Any second exit is a route by which a timeout
        becomes a verdict.
        """
        assert PAYMENT_TRANSITIONS[P.UNKNOWN] == frozenset({P.RECONCILING})

    def test_there_is_no_unknown_to_failed_edge(self):
        # Named separately from the set-equality test above so that the failure message
        # says exactly what broke: a UI timeout was allowed to declare a payment failed.
        assert not can_transition(P.UNKNOWN, P.FAILED)
        with pytest.raises(InvalidTransitionError, match="UNKNOWN -> FAILED"):
            assert_transition(P.UNKNOWN, P.FAILED)

    def test_unknown_cannot_shortcut_to_captured_either(self):
        # Optimism is as unverified as pessimism. Both go through reconciliation.
        assert not can_transition(P.UNKNOWN, P.CAPTURED)

    def test_refund_unknown_leaves_only_through_reconciling(self):
        """Invariant 4, half one.

        A refund whose outcome is unknown may already exist. An edge to REFUND_PENDING
        would issue a second Execution Grant and refund the buyer twice.
        """
        assert PAYMENT_TRANSITIONS[P.REFUND_UNKNOWN] == frozenset({P.RECONCILING})
        assert not can_transition(P.REFUND_UNKNOWN, P.REFUND_PENDING)

    def test_refund_failed_may_retry_and_may_escalate(self):
        """Invariant 4, half two.

        REFUND_FAILED is provider-confirmed: no refund exists. A bounded retry through a
        fresh admission is safe here precisely because it is not safe from
        REFUND_UNKNOWN.
        """
        assert can_transition(P.REFUND_FAILED, P.REFUND_PENDING)
        assert can_transition(P.REFUND_FAILED, P.ESCALATED)

    def test_the_two_refund_failure_states_are_not_interchangeable(self):
        # The whole point of having both. If these ever became equal, one of them would
        # be permitting the other's forbidden action.
        assert PAYMENT_TRANSITIONS[P.REFUND_UNKNOWN] != PAYMENT_TRANSITIONS[P.REFUND_FAILED]

    def test_submitted_cannot_expire_without_reconciliation(self):
        # Once the buyer is on a payment surface, silence is UNKNOWN. Declaring the order
        # expired on a local timer is the same error as declaring it failed.
        assert not can_transition(P.SUBMITTED, P.EXPIRED)
        assert can_transition(P.SUBMITTED, P.UNKNOWN)

    def test_authorized_never_becomes_failed(self):
        # An authorization that exists is not erased by a refused capture. Releasing it
        # is an auto-refund, which is a state that owes the buyer something.
        assert not can_transition(P.AUTHORIZED, P.FAILED)
        assert can_transition(P.AUTHORIZED, P.AUTO_REFUND_PENDING)

    def test_stale_capture_only_refunds(self):
        """Specification 10.8: a capture bound to an invalidated checkout is refunded.

        Any other exit would be a route to fulfilling a version the merchant already
        withdrew.
        """
        assert PAYMENT_TRANSITIONS[P.STALE_CAPTURE] == frozenset({P.REFUND_PENDING})

    def test_auto_refund_can_time_out_and_can_fail(self):
        # Without these edges a timed-out automatic refund has nowhere legal to go, and
        # the only way forward is an unmodelled blind retry.
        assert can_transition(P.AUTO_REFUND_PENDING, P.REFUND_UNKNOWN)
        assert can_transition(P.AUTO_REFUND_PENDING, P.REFUND_FAILED)

    def test_repeated_partial_refunds_are_modelled(self):
        assert can_transition(P.PARTIALLY_REFUNDED, P.REFUND_PENDING)

    def test_escalated_is_frozen(self):
        """ESCALATED opens exactly one human review case and holds the attempt still.

        An automated edge out of it would let a retry loop or a replayed webhook thaw an
        attempt a human is still looking at.
        """
        assert is_terminal(P.ESCALATED)
        assert reachable_states(P.ESCALATED) == frozenset()

    @pytest.mark.parametrize("terminal", sorted(TERMINAL_PAYMENT_STATES))
    def test_terminal_payment_states_reject_every_target(self, terminal):
        """Invariant 5."""
        assert is_terminal(terminal)
        for target in PaymentState:
            assert not can_transition(terminal, target)
            with pytest.raises(InvalidTransitionError):
                assert_transition(terminal, target)

    def test_money_moved_states_are_unreachable_from_failed_or_expired(self):
        # If a terminal no-money state could reach a refund state, the ledger could claim
        # a refund against a payment that never happened.
        assert not reachable_states(P.FAILED)
        assert not reachable_states(P.EXPIRED)


class TestMonotonicApply:
    def test_out_of_order_authorized_after_capture_is_a_no_op(self):
        """Invariant 2, the headline case this function exists for.

        payment.authorized delivered after payment.captured must change nothing.
        """
        assert monotonic_apply(P.CAPTURED, P.AUTHORIZED) is P.CAPTURED

    def test_capture_after_authorized_advances(self):
        assert monotonic_apply(P.AUTHORIZED, P.CAPTURED) is P.CAPTURED

    def test_event_order_does_not_change_the_outcome(self):
        # The concrete duplicate-and-reorder scenario the webhook inbox faces.
        in_order = monotonic_apply(monotonic_apply(P.SUBMITTED, P.AUTHORIZED), P.CAPTURED)
        reversed_order = monotonic_apply(monotonic_apply(P.SUBMITTED, P.CAPTURED), P.AUTHORIZED)
        with_duplicate = monotonic_apply(reversed_order, P.AUTHORIZED)
        assert in_order is P.CAPTURED
        assert reversed_order is P.CAPTURED
        assert with_duplicate is P.CAPTURED

    def test_stale_failed_webhook_never_erases_a_capture(self):
        assert monotonic_apply(P.CAPTURED, P.FAILED) is P.CAPTURED

    @pytest.mark.parametrize("state", sorted(PaymentState))
    def test_redelivery_is_idempotent(self, state):
        assert monotonic_apply(state, state) is state

    @pytest.mark.parametrize("terminal", sorted(TERMINAL_PAYMENT_STATES))
    @pytest.mark.parametrize("incoming", sorted(PaymentState))
    def test_terminal_absorbs_every_event(self, terminal, incoming):
        """Invariant 5 on the webhook path.

        A refunded, failed, expired or escalated attempt is never revived by an inbound
        event. The raw event is still stored by the inbox; it is simply not applied.
        """
        assert monotonic_apply(terminal, incoming) is terminal

    @pytest.mark.parametrize(
        "incoming", [P.FAILED, P.CAPTURED, P.AUTHORIZED, P.EXPIRED, P.REFUNDED]
    )
    def test_unknown_resolves_only_into_reconciling(self, incoming):
        """Invariant 3 on the webhook path.

        No inbound event, however plausible, resolves an unknown outcome directly. The
        Reconciliation Service confirms against authoritative identifiers first.
        """
        assert monotonic_apply(P.UNKNOWN, incoming) is P.RECONCILING

    def test_unknown_is_not_disturbed_by_weaker_evidence(self):
        # A late CREATED or SUBMITTED event says nothing new and must not schedule work.
        assert monotonic_apply(P.UNKNOWN, P.CREATED) is P.UNKNOWN
        assert monotonic_apply(P.UNKNOWN, P.SUBMITTED) is P.UNKNOWN

    @pytest.mark.parametrize("incoming", [P.REFUNDED, P.REFUND_FAILED, P.PARTIALLY_REFUNDED])
    def test_refund_unknown_resolves_only_into_reconciling(self, incoming):
        """Invariant 4 on the webhook path."""
        assert monotonic_apply(P.REFUND_UNKNOWN, incoming) is P.RECONCILING

    def test_refund_unknown_never_slides_back_into_a_pending_refund(self):
        # This is the double-refund hazard stated as a webhook: an inbound refund.created
        # must not put the attempt back into a state a worker will act on.
        assert monotonic_apply(P.REFUND_UNKNOWN, P.REFUND_PENDING) is P.REFUND_UNKNOWN

    def test_reconciling_is_resolved_by_verified_evidence(self):
        assert monotonic_apply(P.RECONCILING, P.CAPTURED) is P.CAPTURED
        assert monotonic_apply(P.RECONCILING, P.FAILED) is P.FAILED

    def test_a_completed_refund_outranks_a_later_refund_failed_report(self):
        # A refund that demonstrably happened is not undone by a contradictory failure
        # report; treating it as failed would issue a second refund.
        assert monotonic_apply(P.REFUND_FAILED, P.REFUNDED) is P.REFUNDED
        assert monotonic_apply(P.PARTIALLY_REFUNDED, P.REFUND_FAILED) is P.PARTIALLY_REFUNDED

    def test_locally_decided_states_are_not_reachable_by_event(self):
        # AUTO_REFUND_PENDING is a decision the platform makes about an authorization, not
        # something a provider reports about a capture. Landing there from CAPTURED would
        # queue a refund of an authorization that was already captured.
        assert monotonic_apply(P.CAPTURED, P.AUTO_REFUND_PENDING) is P.CAPTURED

    def test_an_event_cannot_expire_an_attempt_that_was_never_verified(self):
        """The reconciliation frontier, stated as the case it protects.

        SUBMITTED can only reach EXPIRED by way of UNKNOWN and RECONCILING, so an event
        claiming expiry must not shortcut past the verification those states represent.
        """
        assert monotonic_apply(P.SUBMITTED, P.EXPIRED) is P.SUBMITTED
        assert monotonic_apply(P.CREATED, P.EXPIRED) is P.EXPIRED

    def test_the_inbox_agrees_with_the_transition_table_on_expiry(self):
        """The one place the two functions could plausibly have disagreed.

        ``can_transition`` refuses SUBMITTED -> EXPIRED so that no local timer declares
        an unverified attempt dead. If ``monotonic_apply`` allowed a webhook to do the
        same thing, the rule would hold only for code that happened to take the other
        path, which is the same as not holding at all.
        """
        assert not can_transition(P.SUBMITTED, P.EXPIRED)
        assert monotonic_apply(P.SUBMITTED, P.EXPIRED) is P.SUBMITTED

    @pytest.mark.parametrize("current", sorted(PaymentState))
    @pytest.mark.parametrize("incoming", sorted(PaymentState))
    def test_result_is_always_reachable_from_current(self, current, incoming):
        """Exhaustive over all 256 pairs.

        The inbox may skip states the provider skipped telling us about, but it must
        never park an attempt somewhere a single lawful advance could not have put it.

        The bound is deliberately the *event-advance* closure, not ``reachable_states``.
        Stated against the full closure this assertion could not fail for any pair --
        RECONCILING loops the graph back on itself, so almost everything is reachable
        from almost everything -- and it would have passed unchanged with the
        reachability guard deleted from ``monotonic_apply`` entirely.
        """
        result = monotonic_apply(current, incoming)
        assert result is current or result in EVENT_ADVANCE_BOUND[current], (
            f"{current} + {incoming} -> {result}, which no single lawful advance reaches"
        )

    @pytest.mark.parametrize("current", sorted(PaymentState))
    @pytest.mark.parametrize("incoming", sorted(PaymentState))
    def test_money_never_moves_backwards(self, current, incoming):
        """Exhaustive: once the buyer has been debited, no event walks that back.

        The result may become uncertain (RECONCILING) or frozen (ESCALATED), because
        those states make no claim either way. What it may never become is a state that
        positively asserts the debit never happened -- from any of those, the platform
        would go on to retry a payment the buyer has already made.
        """
        result = monotonic_apply(current, incoming)
        if current in MONEY_MOVED:
            assert result not in NO_MONEY_MOVED, (
                f"{current} + {incoming} -> {result} un-moved the money"
            )

    @pytest.mark.parametrize("current", sorted(frozenset(PaymentState) - UNCERTAIN_PAYMENT_STATES))
    @pytest.mark.parametrize("incoming", sorted(PaymentState))
    def test_applying_the_same_event_twice_settles(self, current, incoming):
        """A duplicate delivery after the first application changes nothing further.

        Uncertain states are excluded because they do **not** settle, not because
        something else settles them. The first delivery moves the attempt into
        RECONCILING and a second delivery of the same event then resolves it to the
        reported outcome, with no reconciliation in between. That gap is pinned by
        ``test_known_gap_a_second_event_resolves_uncertainty_without_reconciling``
        rather than left implicit in this exclusion.
        """
        once = monotonic_apply(current, incoming)
        assert monotonic_apply(once, incoming) is once

    @pytest.mark.parametrize("current", sorted(PaymentState))
    @pytest.mark.parametrize("incoming", sorted(PaymentState))
    def test_an_uncertain_state_never_leaves_except_to_reconciling(self, current, incoming):
        """Invariants 3 and 4, stated exhaustively rather than case by case.

        Note the scope: this holds for *one* application. It does not survive a second
        one -- see the known-gap test below.
        """
        if current in UNCERTAIN_PAYMENT_STATES:
            result = monotonic_apply(current, incoming)
            assert result in {current, P.RECONCILING}

    @pytest.mark.parametrize("incoming", [P.FAILED, P.CAPTURED, P.EXPIRED])
    def test_known_gap_a_second_event_resolves_uncertainty_without_reconciling(self, incoming):
        """KNOWN GAP, pinned so it cannot be lost. This is not desired behaviour.

        Invariants 3 and 4 hold for a single application only. Two deliveries walk an
        uncertain attempt to the reported outcome with no reconciliation in between,
        because RECONCILING itself is resolvable by an inbound event: the first delivery
        makes the state RECONCILING, and the second is then applied to it.

        Specification 6.4.1 says the Reconciliation Service *owns* UNKNOWN, RECONCILING
        and REFUND_UNKNOWN, and resolves them by querying the provider by authoritative
        identifier. The webhook path does not honour that for RECONCILING.

        The money consequence is on the refund side. REFUND_UNKNOWN reached by
        REFUND_FAILED twice lands on REFUND_FAILED, and REFUND_FAILED is exactly the
        state whose meaning is "no refund exists, a fresh grant may retry" -- which
        specification 10.5 forbids reaching from an unverified refund outcome.

        When this is fixed -- by making RECONCILING absorb inbound events the way an
        uncertain state does -- this test should be inverted, not deleted.
        """
        once = monotonic_apply(P.UNKNOWN, incoming)
        assert once is P.RECONCILING
        assert monotonic_apply(once, incoming) is incoming

        refund_once = monotonic_apply(P.REFUND_UNKNOWN, P.REFUND_FAILED)
        assert refund_once is P.RECONCILING
        assert monotonic_apply(refund_once, P.REFUND_FAILED) is P.REFUND_FAILED
        assert can_transition(P.REFUND_FAILED, P.REFUND_PENDING)


class TestCrossMachineConfusion:
    def test_the_two_machines_share_state_names_by_string(self):
        """Documents the hazard the type guard exists for.

        Both enums are StrEnum, so this comparison is True by plain string equality.
        Without the guard, a caller could ask whether a checkout may move to a payment
        state and get a confident, meaningless answer.
        """
        assert C.EXPIRED == P.EXPIRED

    def test_mixing_machines_raises_rather_than_answering(self):
        with pytest.raises(TypeError, match="different machines"):
            can_transition(C.AWAITING_PAYMENT, P.CAPTURED)  # type: ignore[call-overload]
        with pytest.raises(TypeError, match="different machines"):
            assert_transition(P.SUBMITTED, C.PAID)  # type: ignore[call-overload]

    def test_monotonic_apply_refuses_a_foreign_state_too(self):
        """The inbox path needs the same guard as the transition path.

        CheckoutState.EXPIRED compares and hashes equal to PaymentState.EXPIRED, so
        before the guard was added it was looked up in the payment rank and advance
        tables and answered: monotonic_apply(P.CAPTURED, C.EXPIRED) returned CAPTURED.
        A guard on two of the three public functions is not a guard.
        """
        with pytest.raises(TypeError, match="different machines"):
            monotonic_apply(P.CAPTURED, C.EXPIRED)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="different machines"):
            monotonic_apply(C.PAID, P.CAPTURED)  # type: ignore[arg-type]

    def test_is_terminal_distinguishes_the_machines(self):
        # EXPIRED is terminal in both, so pick a name that differs: PAID exists only on
        # the checkout, CAPTURED only on the payment.
        assert is_terminal(C.PAID)
        assert not is_terminal(P.CAPTURED)


def _check_constraint_values(table, constraint_name: str) -> frozenset[str]:
    """Pull the allowed values out of a ``status IN (...)`` CHECK constraint."""
    for constraint in table.constraints:
        if isinstance(constraint, CheckConstraint) and constraint.name == constraint_name:
            return frozenset(re.findall(r"'([A-Z_]+)'", str(constraint.sqltext)))
    raise AssertionError(f"{constraint_name} not found on {table.name}")


class TestSchemaAgreement:
    """The database and this module must not drift apart.

    A status the database permits but the machine does not know is a row no transition
    can move; a status the machine produces but the database rejects is a write that
    fails at commit, inside the admission transaction, after the locks were taken.
    """

    def test_checkout_status_constraint_matches_the_enum_exactly(self):
        allowed = _check_constraint_values(
            CheckoutVersion.__table__, "ck_checkout_versions_status_enum"
        )
        assert allowed == {state.value for state in CheckoutState}

    def test_every_persistable_payment_status_is_a_known_state(self):
        allowed = _check_constraint_values(
            PaymentAttempt.__table__, "ck_payment_attempts_status_enum"
        )
        unknown = allowed - {state.value for state in PaymentState}
        assert not unknown, f"database permits payment statuses the kernel cannot move: {unknown}"

    def test_the_in_flight_index_never_treats_a_terminal_state_as_in_flight(self):
        """The partial unique index in specification 10.6 allows one non-terminal attempt
        per checkout. If it listed a state this module calls terminal, that terminal
        attempt would block every future attempt on the checkout forever."""
        index = next(
            idx
            for idx in PaymentAttempt.__table__.indexes
            if idx.name == "uq_payment_attempts_one_non_terminal"
        )
        where = str(index.kwargs["postgresql_where"])
        in_flight = frozenset(re.findall(r"'([A-Z_]+)'", where))
        terminal = {state.value for state in TERMINAL_PAYMENT_STATES}
        assert not in_flight & terminal
        assert in_flight <= {state.value for state in NON_TERMINAL_PAYMENT_STATES}
