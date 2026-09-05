"""Failure scenario: the authority to act lapses while the command is already queued.

There is a window in this architecture that cannot be closed, only survived. The kernel
admits a checkout, mints one single-use Execution Grant, and enqueues the create-order
command in the same transaction; the worker picks that command up some time later. Two
things can happen in between, and they do *not* land in the same place:

* **the grant expires.** Grants are short-lived on purpose (specification 10.3), and a
  worker that was down, or behind, or merely unlucky arrives after the deadline. The
  worker refuses, sends nothing, and buries the command with ``AUTHORITY_INSUFFICIENT``.
  This is the behaviour the repository already exhibits -- there is a ``DEAD`` outbox row
  in the development database from exactly this cause, and it is correct. It is not a
  defect awaiting a fix, and these tests exist so that nobody "fixes" it into a retry.

* **Safe Mode is thrown.** An operator declares an incident, and the activation sweep
  withdraws this tenant's unused *delegated* grants. A human-present Standard Checkout is
  not one of them: its queued command runs to completion during the incident, because
  specification 34 requires Safe Mode to block delegated and Reserve Pay execution "while
  preserving human-present checkout and recovery". Stopping a buyer who has already
  approved a total would strand them holding a reservation and no order, over an incident
  that had nothing to do with them.

Where authority *is* withdrawn -- an expired grant, or one the sweep revoked -- three
things are asserted:

* **no money moves.** ``FakeTransport`` refuses any call it was not scripted for, so a
  provider request would fail the test rather than reach a network. The grant is not
  consumed and the payment attempt keeps whatever state it had.
* **the state is one a human can act on.** The outbox row is ``DEAD`` with its payload
  intact, so ``revive`` can return it to the queue after re-admission, and the audit chain
  carries ``worker.grant_refused`` naming the grant and the reason alongside
  ``outbox.dead_letter`` carrying the terminal code.
* **the degradation is visible.** ``AUTHORITY_INSUFFICIENT`` is not in ``RETRYABLE``, so
  the command is buried on the first attempt rather than after eight quiet retries. A
  scheduled retry would have hidden the stop behind a delay; burial surfaces it now.

The buyer is not left stranded either way: the approved checkout version and its
reservation survive, so recovery is a re-admission of the same bytes rather than a rebuild.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Final

import pytest
import transaction_kernel as tk
from durable_worker.loop import TenantRef, run_once
from durable_worker.settings import WorkerRuntime
from sqlalchemy import Engine, text
from transaction_kernel.grants import revoke_grant
from transaction_kernel.safe_mode import (
    ModeChangeReason,
    OperatingModeName,
    current_mode,
    enter_safe_mode,
)

from conftest import (
    Admitted,
    FakeTransport,
    attempt_of,
    audit_types,
    checkout_status,
    enqueue,
    json_response,
    order_entity,
    provider_requests,
)

pytestmark = pytest.mark.db

#: Every assertion here is about a command that must never be retried, so one tick is the
#: whole story: burial happens on the first failure, not on the eighth.
_ONE_TICK: Final[int] = 1


def _outbox_row(session: object, tenant_id: uuid.UUID) -> object:
    """The single outbox row for this tenant, with the columns a person would read."""
    return session.execute(  # type: ignore[attr-defined]
        text("SELECT id, status, attempts, payload FROM outbox_events WHERE tenant_id = :t"),
        {"t": tenant_id},
    ).one()


def _dead_letter(session: object, command_id: uuid.UUID) -> dict[str, object]:
    """The burial's audit payload, which is where the terminal code actually lives.

    ``outbox_events`` has no ``last_error_code`` column, and deliberately so: the queue
    row carries the work, and the reason the platform stopped carrying it belongs on the
    hash-chained audit stream where it cannot be quietly overwritten by the next attempt.
    """
    payload: dict[str, object] = session.execute(  # type: ignore[attr-defined]
        text(
            "SELECT payload FROM audit_events WHERE aggregate_id = :c "
            "AND event_type = 'outbox.dead_letter'"
        ),
        {"c": command_id},
    ).scalar_one()
    return payload


def _grant_status(session: object, admitted: Admitted) -> str:
    return str(
        session.execute(  # type: ignore[attr-defined]
            text("SELECT status FROM execution_grants WHERE id = :g"), {"g": admitted.grant_id}
        ).scalar_one()
    )


def _queue_the_create_order(admitted: Admitted, kernel_session) -> uuid.UUID:  # noqa: ANN001
    """Enqueue the command the API enqueues, linked to its grant, and commit.

    Linking matters: ``link_command`` is what binds this grant to this one command, and
    the refusal path under test is reached through that binding rather than through the
    payload alone.
    """
    session = kernel_session(admitted.tenant_id)
    command_id = enqueue(session, admitted.create_order_command(), link_grant=admitted.grant_id)
    session.commit()
    return command_id


# ------------------------------------------------------------- the grant simply expires


@pytest.mark.db
class TestGrantExpiresBeforeTheWorkerArrives:
    """The command outlives the authority that was issued with it."""

    def test_the_worker_refuses_and_sends_nothing_to_the_provider(
        self,
        runtime: WorkerRuntime,
        admitted: Admitted,
        transport: FakeTransport,
        dwk_admin_engine: Engine,
        kernel_session,  # noqa: ANN001
    ) -> None:
        """An expired grant buries the command. Zero provider requests, zero retries.

        The deadline is moved rather than the status, which is the honest way to age a
        grant: the row still says ``ISSUED``, exactly as a genuinely stale one would, and
        it is ``consume_grant``'s own expiry predicate -- evaluated by the database clock
        -- that refuses it. Setting ``status = 'EXPIRED'`` directly would test the
        worker's handling of a status this test wrote itself.
        """
        _queue_the_create_order(admitted, kernel_session)
        with dwk_admin_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE execution_grants SET expires_at = now() - interval '1 second' "
                    "WHERE id = :g"
                ),
                {"g": admitted.grant_id},
            )

        report = run_once(runtime, tenants=(TenantRef(admitted.tenant_id, "t"),))

        assert (report.leased, report.completed, report.failed) == (1, 0, _ONE_TICK)
        assert report.dead_letters == 1
        assert transport.requests == [], "an unusable grant must not reach Razorpay"

        after = kernel_session(admitted.tenant_id)
        assert provider_requests(after, admitted) == []
        assert _grant_status(after, admitted) != "CONSUMED"

    def test_the_row_is_buried_on_the_first_attempt_with_its_payload_kept(
        self,
        runtime: WorkerRuntime,
        admitted: Admitted,
        dwk_admin_engine: Engine,
        kernel_session,  # noqa: ANN001
    ) -> None:
        """``AUTHORITY_INSUFFICIENT`` is terminal, so this is visible now, not in an hour.

        The payload is asserted present because burial is not deletion: after the incident
        is understood, an operator re-admits the checkout, and the buried row is the
        evidence of what the platform was asked to do and declined to do.
        """
        command_id = _queue_the_create_order(admitted, kernel_session)
        with dwk_admin_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE execution_grants SET expires_at = now() - interval '1 second' "
                    "WHERE id = :g"
                ),
                {"g": admitted.grant_id},
            )

        run_once(runtime, tenants=(TenantRef(admitted.tenant_id, "t"),))

        after = kernel_session(admitted.tenant_id)
        row = _outbox_row(after, admitted.tenant_id)
        assert row.status == "DEAD"
        assert row.attempts == 1, "a terminal code must not consume eight attempts first"
        assert row.payload["payment_attempt_id"] == str(admitted.attempt_id)

        letter = _dead_letter(after, command_id)
        assert letter["terminal_code"] == tk.RecoveryCode.AUTHORITY_INSUFFICIENT.value
        assert letter["payload"] == row.payload

    def test_the_audit_chain_says_which_grant_was_refused_and_why(
        self,
        runtime: WorkerRuntime,
        admitted: Admitted,
        dwk_admin_engine: Engine,
        kernel_session,  # noqa: ANN001
    ) -> None:
        """Two rows, on two streams, and between them a person can reconstruct the stop.

        ``worker.grant_refused`` is written on the *checkout* stream, because that is
        where the Money Action Proof Chain for this purchase lives; ``outbox.dead_letter``
        is written on the *command* stream, because the burial is a fact about the work
        item. Asserting both is asserting that neither audience -- the buyer's timeline
        and the operator's queue -- has to learn about this from the other.
        """
        command_id = _queue_the_create_order(admitted, kernel_session)
        with dwk_admin_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE execution_grants SET expires_at = now() - interval '1 second' "
                    "WHERE id = :g"
                ),
                {"g": admitted.grant_id},
            )

        run_once(runtime, tenants=(TenantRef(admitted.tenant_id, "t"),))

        after = kernel_session(admitted.tenant_id)
        assert "worker.grant_refused" in audit_types(
            after, admitted.tenant_id, admitted.checkout_id
        )
        assert audit_types(after, admitted.tenant_id, command_id) == ["outbox.dead_letter"]

        refusal = after.execute(
            text(
                "SELECT payload FROM audit_events WHERE tenant_id = :t "
                "AND aggregate_id = :c AND event_type = 'worker.grant_refused'"
            ),
            {"t": admitted.tenant_id, "c": admitted.checkout_id},
        ).scalar_one()
        assert refusal["grant_id"] == str(admitted.grant_id)
        assert refusal["payment_attempt_id"] == str(admitted.attempt_id)
        assert refusal["reason"], "a refusal with no reason is a silent stop"


# ------------------------------------------------------- Safe Mode, thrown mid-flight


@pytest.fixture
def mode_history_cleanup(admitted: Admitted, dwk_admin_engine: Engine) -> Iterator[None]:
    """Remove this tenant's operating-mode records before the tenant itself goes.

    ``platform_operating_modes`` sits outside row-level security -- the platform-wide
    switch has no tenant -- but a *tenant-scoped* record still holds a foreign key onto
    ``tenants``, and the shared ``admitted`` fixture does not sweep it. Cleaning up here
    rather than widening that fixture's table list keeps this scenario's mess inside this
    scenario's file; depending on ``admitted`` is what puts this teardown *before* its,
    which is the order the foreign key requires.
    """
    yield
    with dwk_admin_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM platform_operating_modes WHERE tenant_id = :t"),
            {"t": admitted.tenant_id},
        )


@pytest.mark.db
@pytest.mark.usefixtures("mode_history_cleanup")
class TestSafeModeIsThrownWithWorkInTheOutbox:
    """The incident is declared after the command is queued and before the worker runs it.

    The outcome is the one specification 34 asks for, and it is not the intuitive one:
    **the queued human-present checkout still completes.** Safe Mode's activation sweep
    names ``RESERVE_DEBIT`` alone (:data:`~transaction_kernel.safe_mode
    .DELEGATED_GRANT_OPERATIONS`), so delegated and Reserve Pay execution stops while a
    Standard Checkout the buyer is sitting in front of runs to its end.

    That asymmetry is the design, not an oversight, and it is worth stating plainly
    because "Safe Mode" sounds like it should stop everything. Cancelling a checkout a
    person has already approved makes nobody safer: it strands them holding a reservation,
    no order and no explanation, while the incident that triggered the switch -- abnormal
    delegated activity, a key compromise, a reconciliation backlog -- had nothing to do
    with them. Safe Mode narrows *machine-initiated* money movement and leaves the
    human-present and buyer-protective paths open, which is also why refund grants are in
    :data:`~transaction_kernel.safe_mode.NEVER_SWEPT`.
    """

    def _activate(self, admitted: Admitted, kernel_session) -> None:  # noqa: ANN001
        """Declare a tenant-scoped incident, exactly as an operator would."""
        session = kernel_session(admitted.tenant_id)
        enter_safe_mode(
            session,
            tenant=admitted.tenant_id,
            reason=ModeChangeReason.OPERATOR_DECLARED_INCIDENT,
            actor="operator-fs",
        )
        session.commit()

    def test_the_incident_does_not_touch_a_queued_human_present_checkout(
        self,
        admitted: Admitted,
        kernel_session,  # noqa: ANN001
    ) -> None:
        """The sweep passes over a ``PAYMENT_CREATE_ORDER`` grant that already has a
        command against it.

        Asserted separately from the worker's behaviour because it is the load-bearing
        fact: if the sweep ever widened to cover this operation, the test below would
        still pass by burying the command, and the acceptance criterion would have been
        broken silently. This one fails loudly instead.
        """
        _queue_the_create_order(admitted, kernel_session)
        self._activate(admitted, kernel_session)

        after = kernel_session(admitted.tenant_id)
        assert _grant_status(after, admitted) == "ISSUED"
        assert current_mode(after, admitted.tenant_id) is OperatingModeName.SAFE_MODE

    def test_the_buyer_standing_at_the_payment_page_is_not_stranded(
        self,
        runtime: WorkerRuntime,
        admitted: Admitted,
        transport: FakeTransport,
        kernel_session,  # noqa: ANN001
    ) -> None:
        """The command completes during the incident: one order, one provider request.

        This is "preserving human-present checkout" (specification 34) tested against work
        that was already in the outbox when the switch was thrown, rather than against a
        checkout admitted afterwards. The distinction matters because the second is easy
        -- admission simply refuses -- and the first is where a half-implemented Safe Mode
        would leave a buyer with a held reservation and a command nobody will ever run.
        """
        _queue_the_create_order(admitted, kernel_session)
        self._activate(admitted, kernel_session)
        transport.extend(
            [
                json_response(
                    200,
                    order_entity(receipt=admitted.receipt, amount=admitted.amount.minor),
                )
            ]
        )

        report = run_once(runtime, tenants=(TenantRef(admitted.tenant_id, "t"),))

        assert (report.completed, report.failed, report.dead_letters) == (1, 0, 0)
        assert len(transport.requests) == 1
        after = kernel_session(admitted.tenant_id)
        assert _grant_status(after, admitted) == "CONSUMED"
        assert len(provider_requests(after, admitted)) == 1

    def test_a_grant_the_incident_withdrew_is_refused_and_never_sent(
        self,
        runtime: WorkerRuntime,
        admitted: Admitted,
        transport: FakeTransport,
        kernel_session,  # noqa: ANN001
    ) -> None:
        """The other half: a grant Safe Mode *did* revoke stops the worker dead.

        The revocation here is performed with the kernel's own
        :func:`~transaction_kernel.grants.revoke_grant` rather than by activating Safe
        Mode against a ``RESERVE_DEBIT`` attempt, and the reason is honesty about what is
        being proved. That the activation sweep revokes delegated grants is already proved
        in the kernel's own suite, against the real sweep. What is not proved anywhere
        else, and is proved here, is the *worker's* response to meeting a revoked grant on
        a command it has already leased: it refuses, sends nothing, and buries the row.
        Building a delegated attempt in this fixture to reach the same state would test
        the sweep a second time and the worker no better.
        """
        command_id = _queue_the_create_order(admitted, kernel_session)
        self._activate(admitted, kernel_session)
        arrange = kernel_session(admitted.tenant_id)
        revoke_grant(arrange, admitted.grant_id)
        arrange.commit()

        report = run_once(runtime, tenants=(TenantRef(admitted.tenant_id, "t"),))

        assert report.dead_letters == 1
        assert transport.requests == [], "a withdrawn authority must not reach Razorpay"
        after = kernel_session(admitted.tenant_id)
        assert provider_requests(after, admitted) == []
        assert _outbox_row(after, admitted.tenant_id).status == "DEAD"
        assert (
            _dead_letter(after, command_id)["terminal_code"]
            == tk.RecoveryCode.AUTHORITY_INSUFFICIENT.value
        )

    def test_the_buyer_keeps_their_approved_checkout_and_their_hold(
        self,
        runtime: WorkerRuntime,
        admitted: Admitted,
        kernel_session,  # noqa: ANN001
    ) -> None:
        """A withdrawn authority stops execution; it does not throw the basket away.

        The checkout version stays where admission left it, and the reservation stays
        ``CONSUMED`` -- admission converted the hold when it admitted, and the refusal does
        not release it. That is the direction that protects the buyer: released stock can
        be sold to somebody else while an operator is still deciding what to do about the
        incident, and this buyer would then find their approved basket unbuyable at the
        price they agreed to. Recovery is therefore a re-admission of the same approved
        bytes rather than a rebuild, which is what makes ``leave_safe_mode``'s refusal to
        resurrect a revoked grant affordable: the buyer has lost nothing but time.

        The payment attempt is asserted un-regressed for the same reason the kernel
        refuses to touch it: an incident is not evidence that a charge failed, and
        rewriting the attempt would destroy the record reconciliation reads.
        """
        _queue_the_create_order(admitted, kernel_session)
        before = attempt_of(kernel_session(admitted.tenant_id), admitted).status
        self._activate(admitted, kernel_session)
        arrange = kernel_session(admitted.tenant_id)
        revoke_grant(arrange, admitted.grant_id)
        arrange.commit()

        run_once(runtime, tenants=(TenantRef(admitted.tenant_id, "t"),))

        after = kernel_session(admitted.tenant_id)
        assert attempt_of(after, admitted).status == before
        assert checkout_status(after, admitted) == tk.CheckoutState.EXECUTION_PENDING.value
        reservation = after.execute(
            text(
                "SELECT status FROM reservations "
                "WHERE tenant_id = :t AND checkout_id = :c AND checkout_version = :v"
            ),
            {"t": admitted.tenant_id, "c": admitted.checkout_id, "v": admitted.version},
        ).scalar_one()
        assert reservation == "CONSUMED", (
            "stock the buyer approved must not be returned to the shelf mid-incident"
        )
