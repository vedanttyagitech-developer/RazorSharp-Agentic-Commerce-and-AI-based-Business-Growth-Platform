"""Helpers for the adversarial suites.

These suites attack the invariants the README claims, so the helpers here exist to build
*legitimate* state as cheaply as possible: every denial a test observes must be caused by
the attack under test and never by setup the fixture forgot. Anything that manufactures
an illegitimate state says so in its own name (``forge_``, ``tamper_``).

Lives beside ``admission_support`` and reuses it rather than restating the canonical
content shape, because two definitions of "what was approved" would let a test pass by
agreeing with itself.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from admission_support import APPROVED_TOTAL, SET_TENANT, Fixture, _content
from commerce_domain import AgentPrincipal, CheckoutRef, Money, PolicyKind, canonical_hash, uuid7
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session
from transaction_kernel import approvals, receipts, reservations
from transaction_kernel.receipts import BuyerVisibleRef, ReceiptDraft, SaleTerm
from transaction_kernel.states import CheckoutState

__all__ = [
    "Outcome",
    "add_approved_version",
    "count",
    "race",
    "scalar",
]


# --------------------------------------------------------------------- concurrency


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one racer produced: a value, or the exception it died of, never both.

    Kept as data rather than asserted inside the worker thread because a failed
    ``assert`` in a thread is swallowed by ``Thread.run`` and the suite goes green while
    proving nothing. Every racer therefore reports, and the main thread judges.
    """

    index: int
    value: Any = None
    error: BaseException | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def race[T](
    engine: Engine,
    tenant_id: uuid.UUID,
    body: Callable[[Session, int], T],
    *,
    workers: int,
    timeout: float = 60.0,
) -> list[Outcome]:
    """Run ``body`` in ``workers`` real threads on ``workers`` real database sessions.

    Every thread opens its own :class:`~sqlalchemy.orm.Session` on ``engine``, waits on a
    barrier so the transactions genuinely overlap, binds the tenant inside its own
    transaction, and runs ``body``. Contention is therefore PostgreSQL's, not a mock's:
    the locks, the unique indexes and the isolation level are all real.

    A racer that raises has its exception captured rather than printed, so a test can
    assert on *how* the loser lost. Results come back ordered by worker index so a
    failure message can name which one did what.

    The barrier timeout is deliberately generous: a starved thread that never reaches the
    barrier must fail the test loudly, not silently reduce the level of contention the
    test believed it was applying.
    """
    if workers < 2:
        raise ValueError("a race needs at least two workers")
    results: list[Outcome | None] = [None] * workers
    barrier = threading.Barrier(workers, timeout=timeout)

    def run(index: int) -> None:
        session = Session(engine, expire_on_commit=False)
        try:
            barrier.wait()
            with session.begin():
                session.execute(SET_TENANT, {"t": str(tenant_id)})
                results[index] = Outcome(index=index, value=body(session, index))
        except BaseException as exc:  # noqa: BLE001 - the point is to report, not to hide
            results[index] = Outcome(index=index, error=exc)
        finally:
            session.close()

    threads = [threading.Thread(target=run, args=(i,), name=f"racer-{i}") for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=timeout)
    stuck = [thread.name for thread in threads if thread.is_alive()]
    if stuck:
        raise AssertionError(f"racers did not finish within {timeout}s: {stuck}")
    missing = [i for i, outcome in enumerate(results) if outcome is None]
    if missing:
        raise AssertionError(f"racers produced no outcome at all: {missing}")
    return [outcome for outcome in results if outcome is not None]


# ------------------------------------------------------------------------- reading


def scalar(engine: Engine, tenant_id: uuid.UUID, sql: str, **params: Any) -> Any:
    """One scalar read in its own transaction, with the tenant bound.

    Reads run on their own connection so that what a test observes is *committed* state,
    not something still sitting in a writer's uncommitted transaction.
    """
    session = Session(engine, expire_on_commit=False)
    try:
        with session.begin():
            session.execute(SET_TENANT, {"t": str(tenant_id)})
            return session.execute(text(sql), params).scalar()
    finally:
        session.close()


def count(engine: Engine, tenant_id: uuid.UUID, sql: str, **params: Any) -> int:
    return int(scalar(engine, tenant_id, sql, **params) or 0)


# ------------------------------------------------------------------- state building


def add_approved_version(
    admin_engine: Engine,
    kernel_engine: Engine,
    fixture: Fixture,
    *,
    version: int,
    total: Money = APPROVED_TOTAL,
    reserve: bool = True,
    ttl_seconds: int = 900,
) -> CheckoutRef:
    """Create one further genuinely admissible version of the fixture's checkout.

    Mirrors the ``admissible`` fixture exactly -- version row, Policy-at-Sale Receipt
    written through :mod:`transaction_kernel.receipts`, an ACTIVE reservation, and the
    buyer's approval -- so that a test which then admits this version is exercising the
    admission path and not a half-built row.

    ``reserve=False`` omits the hold, which is the shape a test needs when it wants
    admission to fail at step 7 rather than earlier.

    Teardown is the fixture's: every row created here carries the fixture's tenant, and
    the ``admissible`` fixture deletes by tenant.
    """
    content = _content(fixture.checkout.checkout_id, version, total)
    ref = CheckoutRef(fixture.checkout.checkout_id, version, canonical_hash(content))

    with admin_engine.begin() as conn:
        conn.execute(SET_TENANT, {"t": str(fixture.tenant_id)})
        conn.execute(
            text(
                "INSERT INTO checkout_versions (id, tenant_id, merchant_id, checkout_id, "
                "version, content, content_hash, currency, total_minor, status, immutable) "
                "VALUES (:id, :t, :m, :c, :v, CAST(:content AS jsonb), :h, :cur, :total, "
                ":status, true)"
            ),
            {
                "id": uuid7(),
                "t": fixture.tenant_id,
                "m": fixture.merchant_id,
                "c": fixture.checkout.checkout_id,
                "v": version,
                "content": __import__("json").dumps(content, sort_keys=True),
                "h": ref.content_hash,
                "cur": total.currency,
                "total": total.minor,
                "status": CheckoutState.APPROVAL_REQUIRED.value,
            },
        )

    session = Session(kernel_engine, expire_on_commit=False)
    try:
        with session.begin():
            session.execute(SET_TENANT, {"t": str(fixture.tenant_id)})
            issued = receipts.issue_receipt(
                session,
                ReceiptDraft(
                    tenant_id=fixture.tenant_id,
                    merchant_id=fixture.merchant_id,
                    checkout_id=ref.checkout_id,
                    checkout_version=version,
                    checkout_hash=ref.content_hash,
                    policies=tuple(
                        SaleTerm(
                            kind=kind,
                            policy_id=f"pol-{kind.value.lower()}",
                            policy_version=12,
                            terms={"summary": f"{kind.value} terms"},
                        )
                        for kind in PolicyKind
                    ),
                    tax_policy_version=3,
                    rounding_policy_version=1,
                    buyer_visible_refs=(
                        BuyerVisibleRef(
                            label="Refund policy",
                            uri="https://demo.invalid/policies/refund",
                            text_hash=canonical_hash({"policy": "refund", "version": 12}),
                        ),
                    ),
                    correlation_id=uuid7(),
                ),
            )
            session.execute(
                text(
                    "UPDATE checkout_versions SET policy_receipt_id = :rid, "
                    "policy_receipt_hash = :rh WHERE tenant_id = :t AND checkout_id = :c "
                    "AND version = :v"
                ),
                {
                    "rid": issued.receipt_id,
                    "rh": issued.receipt_hash,
                    "t": fixture.tenant_id,
                    "c": ref.checkout_id,
                    "v": version,
                },
            )
            if reserve:
                reservations.reserve(
                    session,
                    checkout_id=ref.checkout_id,
                    checkout_version=version,
                    ttl_seconds=ttl_seconds,
                )
            # The buyer's approval, written through the real function. Admission reads the
            # approvals row it is handed, so a version whose status was moved by an UPDATE
            # with no row behind it is not admissible and should not pretend to be.
            approvals.record_approval(
                session,
                tenant_id=fixture.tenant_id,
                checkout=ref,
                amount=total,
                principal=fixture.principal,
                correlation_id=fixture.correlation_id,
            )
    finally:
        session.close()
    return ref


def recorded_approval_id(engine: Engine, fixture: Fixture, checkout: CheckoutRef) -> uuid.UUID:
    """The id of the live approval on this version.

    Tests that mean to be admitted have to name the buyer's real decision, because the
    kernel now reads it. Looked up rather than threaded through every helper's return type,
    so a test reads as "the approval on this version" instead of carrying an id it never
    mentions again.
    """
    with engine.begin() as conn:
        conn.execute(SET_TENANT, {"t": str(fixture.tenant_id)})
        found = conn.execute(
            text(
                "SELECT id FROM approvals WHERE tenant_id = :t AND checkout_id = :c "
                "AND checkout_version = :v AND status = 'RECORDED'"
            ),
            {"t": fixture.tenant_id, "c": checkout.checkout_id, "v": checkout.version},
        ).scalar_one()
    return uuid.UUID(str(found))


def principal_with(fixture: Fixture, capabilities: Sequence[str]) -> AgentPrincipal:
    """The fixture's principal carrying exactly ``capabilities``."""
    return AgentPrincipal(
        principal_id=fixture.principal.principal_id,
        tenant_id=fixture.tenant_id,
        actor_type=fixture.principal.actor_type,
        merchant_id=fixture.merchant_id,
        capabilities=frozenset(capabilities),
    )
