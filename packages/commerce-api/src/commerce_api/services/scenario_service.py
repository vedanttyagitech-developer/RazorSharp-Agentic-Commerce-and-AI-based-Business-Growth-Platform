"""The demonstration's levers, composed over the kernel and the merchant simulator.

ADR 0003 D11 and specification 31.3. These are the controls that let a panel see the
system's failure paths on purpose rather than on luck: a price that moves between
approval and submission, a reservation that lapses, a webhook redelivered byte for byte,
two tabs submitting the same version at once, a provider call that times out, a capture
that arrives after the checkout was invalidated.

Three rules hold across every function here, and they are what separate demo apparatus
from a lie.

**Every injection is labelled.** Merchant-state changes go through
:class:`merchant_sim.ScenarioController`, which cannot produce an unlabelled change --
:class:`~merchant_sim.injection.ScenarioInjection` refuses any label but
``SCENARIO_INJECTION``. The other levers write an audit event whose ``event_type`` begins
``SCENARIO_`` and whose payload carries the same label, plus a ``scenario_runs`` row.
During a live demonstration the difference between "we injected this" and "our inventory
service broke" is the whole credibility of the system, so it is structural rather than a
convention somebody has to remember.

**Nothing here invents financial state.** Reservation expiry moves a *deadline* and then
asks :func:`transaction_kernel.reservations.release` to make the transition; the
late-capture lever is :func:`transaction_kernel.invalidate_open`; the duplicate-submit
lever is two real :func:`transaction_kernel.admit` calls in two real transactions. The
one raw statement in this module is the ``expires_at`` fast-forward, and its reasoning is
written where it is defined.

**The tenant comes from the session.** The scenario key says *may you operate this
apparatus*; it does not say *on whose data*. Every function takes a
:class:`~commerce_api.deps.RequestContext` and works inside that tenant, under row-level
security, exactly as a buyer request would.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

import transaction_kernel as tk
from commerce_domain import Money, uuid7
from merchant_sim import (
    SCENARIO_LABEL,
    ScenarioController,
    ScenarioError,
    ScenarioInjection,
    UnknownSkuError,
)
from platform_db import (
    Approval,
    ScenarioFault,
    ScenarioRun,
    WebhookInboxRow,
    claim_scenario_fault,
    set_tenant,
)
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from transaction_kernel import (
    ActorType,
    AdmissionRequest,
    CheckoutRef,
    CheckoutState,
    KernelDecision,
    MerchantStateSource,
    Operation,
    RecoveryCode,
)
from transaction_kernel.reservations import ReleaseCause, ReservationStatus

from ..deps import RequestContext, session_scope_for
from ..errors import ProblemError
from ..merchants import MerchantRegistry

__all__ = [
    "BARRIER_TIMEOUT_SECONDS",
    "INJECTION_EVENT_TYPE",
    "SCENARIO_EVENT_PREFIX",
    "DuplicateSubmitOutcome",
    "FaultKind",
    "InjectionKind",
    "InjectionOutcome",
    "InvalidationOutcome",
    "ReplayOutcome",
    "ReservationExpiry",
    "TurnFaultClaimer",
    "apply_injection",
    "arm_fault",
    "duplicate_submit",
    "expire_reservation",
    "invalidate_open_checkout",
    "load_inbox_row",
    "record_replay",
]

#: Every audit event this module writes starts here, so a timeline can strip demo
#: apparatus out of an organic journey with one prefix test rather than a list of names.
SCENARIO_EVENT_PREFIX: Final[str] = "SCENARIO_"

#: The event type specification 31.3 names. Only :func:`apply_injection` writes it, so
#: "how many merchant-state injections happened" is one query rather than a scan with a
#: filter somebody has to keep in step with this module.
INJECTION_EVENT_TYPE: Final[str] = SCENARIO_LABEL

#: The money action the duplicate-submit lever races. Two tabs both submitting an
#: approved checkout is a create-order race, not a delegated debit.
DUPLICATE_SUBMIT_OPERATION: Final[Operation] = Operation.PAYMENT_CREATE_ORDER

#: Long enough that a cold connection pool does not turn the demonstration into a
#: failure, short enough that a wedged thread cannot hold the request open.
BARRIER_TIMEOUT_SECONDS: Final[float] = 10.0


# --------------------------------------------------------------------------- vocabulary


class InjectionKind(StrEnum):
    """What a merchant-state injection changes, as the wire names it.

    A superset of :class:`merchant_sim.InjectionKind` by exactly one member,
    ``SELL_OUT``. The simulator models a sell-out as ``STOCK_SET`` to zero -- correctly,
    because that is what changed -- but "take the last unit" is the instruction an
    operator actually gives, and making them type ``STOCK_SET`` with ``value: 0`` during
    a live run is an invitation to type ``1`` by accident. The audit records the
    ``STOCK_SET`` the simulator performed, so this convenience never rewrites history.
    """

    STOCK_SET = "STOCK_SET"
    STOCK_DECREMENT = "STOCK_DECREMENT"
    SELL_OUT = "SELL_OUT"
    PRICE_SET = "PRICE_SET"
    AVAILABILITY_SET = "AVAILABILITY_SET"
    DELIVERY_FEE_SET = "DELIVERY_FEE_SET"
    FREE_DELIVERY_THRESHOLD_SET = "FREE_DELIVERY_THRESHOLD_SET"
    CATALOGUE_RESET = "CATALOGUE_RESET"


#: Kinds that name one SKU, checked before the controller is reached so the refusal names
#: the missing field rather than surfacing as a simulator error.
SKU_SCOPED: Final[frozenset[InjectionKind]] = frozenset(
    {
        InjectionKind.STOCK_SET,
        InjectionKind.STOCK_DECREMENT,
        InjectionKind.SELL_OUT,
        InjectionKind.PRICE_SET,
        InjectionKind.AVAILABILITY_SET,
    }
)

#: Kinds that are complete instructions on their own. Accepting a ``value`` for one of
#: these would let the value be silently ignored, which during a live run reads as the
#: controller having done something other than what the operator typed.
VALUE_FORBIDDEN: Final[frozenset[InjectionKind]] = frozenset(
    {InjectionKind.SELL_OUT, InjectionKind.CATALOGUE_RESET}
)


class FaultKind(StrEnum):
    """Every failure the controller can arm, worker-side and platform-side.

    The three timeouts came first and are all *provider* faults: the worker consults
    ``scenario_faults`` immediately before a Razorpay call and, when a row is armed,
    makes no call at all. A timeout rather than a failure because the timeout is the
    interesting case -- a failure is a known outcome, a timeout is an unknown one, and
    unknown outcomes are what the reconciliation path exists to resolve (specification
    10.7).

    The two that follow are a different animal and it would be dishonest to file them
    beside the timeouts without saying so. Specification 30 requires a deterministic
    answer when the *reasoning* layer or the *speech* layer fails, and neither of those
    is a Razorpay call, neither is reached by the Action Executor, and neither has a
    checkout or a payment attempt to be scoped to. They share this table anyway, and only
    this table, because what ``scenario_faults`` actually models is "one armed, single-use
    demonstration intention, scoped to a tenant" -- ``armed`` plus ``consumed_at`` plus
    the claim in :func:`platform_db.claim_scenario_fault`. A second store would have had
    to re-derive that single-use guarantee and would have got it subtly wrong. What the
    new kinds do *not* share is the consumer: ``LLM_FAILURE`` is claimed by the API
    inside an agent turn, and ``TTS_FAILURE`` is claimed by the API and then handed to
    the voice gateway, which has no database at all.

    Both are armed tenant-wide -- see :func:`arm_fault`, which refuses either identifier
    for them -- and both are demo-profile only, because a reasoning fault that could be
    armed against production would be a denial-of-service control on the buyer's
    conversation rather than a demonstration.
    """

    #: The three worker-side provider timeouts. Every name here must match a member of
    #: ``action_executor.faults.FaultKind`` exactly, because the worker claims by string:
    #: a kind this side can arm and that side cannot claim is a lever wired to nothing,
    #: and a kind that side claims and this side cannot arm is a fault nobody can reach.
    #: Both existed until today. ``PAYMENT_FETCH_TIMEOUT`` was armable here, claimed
    #: nowhere, and answered ``armed: true`` for a fault that could never fire; the
    #: reconciliation timeout the worker really does claim -- the one the bounded-attempts
    #: escalation of ADR D13 is demonstrated with -- had no way to be armed at all. The
    #: names agree now and ``test_the_two_fault_vocabularies_agree`` keeps them agreeing.
    CREATE_ORDER_TIMEOUT = "CREATE_ORDER_TIMEOUT"
    RECONCILE_FETCH_TIMEOUT = "RECONCILE_FETCH_TIMEOUT"
    REFUND_TIMEOUT = "REFUND_TIMEOUT"

    #: The model call inside an agent turn never happens. The turn answers from the
    #: platform's own records instead and says so (specification 30, "LLM failure").
    LLM_FAILURE = "LLM_FAILURE"

    #: Speech synthesis raises for one phrase of the next spoken reply. The text the
    #: buyer reads is unaffected, which is the whole point of the modality fallback
    #: (specification 30, "STT/TTS failure").
    TTS_FAILURE = "TTS_FAILURE"


#: The faults an agent turn consults, in the order they are claimed. Ordered rather than
#: a set so that when an operator arms both, the audit reads in the same order every time.
TURN_FAULTS: Final[tuple[FaultKind, ...]] = (FaultKind.LLM_FAILURE, FaultKind.TTS_FAILURE)

#: Kinds whose consumer scopes tenant-wide, so a row carrying a checkout or a payment
#: attempt could never match and would sit armed forever while the operator waited.
TENANT_SCOPED_FAULTS: Final[frozenset[FaultKind]] = frozenset(TURN_FAULTS)


# ----------------------------------------------------------------------------- results


@dataclass(frozen=True, slots=True)
class InjectionOutcome:
    """One applied merchant-state change and the evidence it left behind."""

    injection: ScenarioInjection
    audit_event_id: uuid.UUID
    scenario_run_id: uuid.UUID
    audit_payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ReservationExpiry:
    """The result of fast-forwarding one reservation to its deadline."""

    checkout_id: uuid.UUID
    version: int
    reservation_id: uuid.UUID
    status_before: ReservationStatus
    status_after: ReservationStatus | None
    expires_at: datetime | None
    code: RecoveryCode
    swept: int
    audit_event_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class ReplayOutcome:
    """What a redelivered webhook did and -- more usefully -- what it did not do."""

    inbox_id: uuid.UUID
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
    audit_event_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class DuplicateSubmitOutcome:
    """Two genuine admissions of one version, and what the kernel did about it."""

    checkout_id: uuid.UUID
    version: int
    approval_id: uuid.UUID
    decisions: tuple[KernelDecision, ...]
    admitted_count: int
    attempt_ids: tuple[uuid.UUID, ...]
    grant_ids: tuple[uuid.UUID, ...]
    audit_event_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class InvalidationOutcome:
    """A checkout taken out of play while its payment surface is still open."""

    checkout_id: uuid.UUID
    version: int
    content_hash: str
    from_state: CheckoutState
    to_state: CheckoutState
    reason: str
    audit_event_id: uuid.UUID


# ------------------------------------------------------------------ shared bookkeeping


def _audit(
    session: Session,
    ctx: RequestContext,
    *,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    event_type: str,
    payload: Mapping[str, Any],
) -> uuid.UUID:
    """Append one labelled scenario event to an aggregate's hash chain.

    ``actor_type`` is ``OPERATOR`` because the credential that reached this code is the
    scenario key, which is an operator credential; the session identity travels in
    ``principal_id`` so the row still says *which* console did it. Recording the buyer or
    agent whose bearer token happened to accompany the request would attribute an
    operator's demo action to the person whose journey it interrupts.
    """
    event = tk.append(
        session,
        tenant=ctx.tenant_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        actor_type=ActorType.OPERATOR,
        principal_id=ctx.principal.principal_id,
        payload=payload,
        correlation_id=ctx.correlation_id,
    )
    return event.event_id


def _record_run(
    session: Session,
    ctx: RequestContext,
    *,
    injection_id: uuid.UUID,
    kind: str,
    payload: Mapping[str, Any],
    audit_event_id: uuid.UUID | None,
) -> uuid.UUID:
    """Write the ``scenario_runs`` row that marks this change as apparatus.

    Two records rather than one, because they answer different questions under different
    rules: the audit event is evidence and can never be edited, while a scenario run is
    the demo metadata a timeline joins against in order to *label* an event as injected.
    """
    row = ScenarioRun(
        id=uuid7(),
        tenant_id=ctx.tenant_id,
        merchant_id=ctx.merchant_id,
        injection_id=injection_id,
        kind=kind,
        payload=dict(payload),
        audit_event_id=audit_event_id,
    )
    session.add(row)
    session.flush()
    return row.id


def _unprocessable(detail: str, **extensions: Any) -> ProblemError:
    return ProblemError(422, "Injection not applicable", detail, **extensions)


def _no_such_checkout(checkout_id: uuid.UUID) -> ProblemError:
    """404, never 403: the scenario key is an operator credential, not an oracle for
    discovering which checkout identifiers exist in somebody else's tenant."""
    return ProblemError(
        404,
        "Checkout not found",
        "No checkout with that identifier is visible to this tenant.",
        checkout_id=str(checkout_id),
    )


# -------------------------------------------------------------- merchant-state changes


def _integer(value: bool | int | None, kind: InjectionKind) -> int:
    """The integer a kind needs, refusing a boolean that would coerce to 0 or 1.

    ``bool`` is a subclass of ``int``, so ``{"value": true}`` on a ``PRICE_SET`` would
    otherwise set the price to one paisa without complaint.
    """
    if value is None or isinstance(value, bool):
        raise _unprocessable(
            f"{kind.value} requires an integer value.", kind=kind.value, field="value"
        )
    return value


def _boolean(value: bool | int | None, kind: InjectionKind) -> bool:
    if not isinstance(value, bool):
        raise _unprocessable(
            f"{kind.value} requires a boolean value.", kind=kind.value, field="value"
        )
    return value


def _checked_sku(sku: str | None, kind: InjectionKind) -> str:
    if kind in SKU_SCOPED:
        if not sku:
            raise _unprocessable(f"{kind.value} must name the SKU it changes.", kind=kind.value)
        return sku
    if sku:
        raise _unprocessable(
            f"{kind.value} is store-wide and must not name a SKU.", kind=kind.value, sku=sku
        )
    return ""


def _apply(
    scenario: ScenarioController,
    *,
    kind: InjectionKind,
    sku: str,
    value: bool | int | None,
    note: str,
    fee_currency: str,
    price_currency: str,
) -> ScenarioInjection:
    """Dispatch one wire instruction onto the controller.

    Currency is never a request parameter. A body that could name one could price a demo
    basket in the wrong unit, and the resulting total would look merely surprising rather
    than wrong -- so both currencies are read from the store and handed in here.
    """
    if kind is InjectionKind.CATALOGUE_RESET:
        return scenario.reset(note=note)
    if kind is InjectionKind.DELIVERY_FEE_SET:
        return scenario.set_delivery_fee(Money(_integer(value, kind), fee_currency), note=note)
    if kind is InjectionKind.FREE_DELIVERY_THRESHOLD_SET:
        return scenario.set_free_delivery_threshold(
            Money(_integer(value, kind), fee_currency), note=note
        )
    if kind is InjectionKind.SELL_OUT:
        return scenario.sell_out(sku, note=note)
    if kind is InjectionKind.STOCK_SET:
        return scenario.set_stock(sku, _integer(value, kind), note=note)
    if kind is InjectionKind.STOCK_DECREMENT:
        # No value means one unit: the competing-buyer story, and the common instruction.
        units = 1 if value is None else _integer(value, kind)
        return scenario.decrement_stock(sku, units, note=note)
    if kind is InjectionKind.AVAILABILITY_SET:
        return scenario.set_availability(sku, _boolean(value, kind), note=note)
    return scenario.set_price(sku, Money(_integer(value, kind), price_currency), note=note)


def apply_injection(
    session: Session,
    ctx: RequestContext,
    registry: MerchantRegistry,
    *,
    kind: InjectionKind,
    sku: str | None = None,
    value: bool | int | None = None,
    note: str = "",
) -> InjectionOutcome:
    """Step 5: change merchant state while a buyer is mid-checkout.

    The change is applied under the registry lock, so two injections cannot interleave on
    one store, and the catalogue revision advances by exactly one -- which is what makes
    every quote taken before it provably stale and the kernel's later refusal legible.

    Guarantees: exactly one ``SCENARIO_INJECTION`` audit row per applied injection, whose
    payload is byte-identical to the controller's own
    :meth:`~merchant_sim.injection.ScenarioInjection.to_audit_payload`; exactly one
    ``scenario_runs`` row cross-linking it; and no write of any kind when the simulator
    refuses, because :meth:`~merchant_sim.store.MerchantStore.mutate` validates before it
    writes and the whole request shares one transaction.

    Refuses a no-op -- a price already at that value, a SKU already delisted -- with 409.
    An injection that changed nothing would still advance the revision, and a revision
    bump with no cause is exactly the unexplained staleness this apparatus exists to
    prevent.
    """
    if kind in VALUE_FORBIDDEN and value is not None:
        raise _unprocessable(
            f"{kind.value} takes no value; supplying one would be silently ignored.",
            kind=kind.value,
            field="value",
        )
    checked_sku = _checked_sku(sku, kind)
    store = registry.store(ctx.merchant_id)

    with registry.mutating(ctx.merchant_id) as scenario:
        try:
            price_currency = (
                store.get_product(checked_sku).unit_price.currency
                if kind is InjectionKind.PRICE_SET
                else store.fee_policy.currency
            )
            injection = _apply(
                scenario,
                kind=kind,
                sku=checked_sku,
                value=value,
                note=note,
                fee_currency=store.fee_policy.currency,
                price_currency=price_currency,
            )
        except UnknownSkuError as exc:
            raise ProblemError(
                404, "Unknown SKU", str(exc), sku=sku, merchant_id=str(ctx.merchant_id)
            ) from exc
        except ScenarioError as exc:
            # The simulator protecting the demonstration from a state nobody could
            # explain is not a server fault.
            raise ProblemError(
                409, "Injection refused", str(exc), kind=kind.value, sku=sku
            ) from exc

    payload = injection.to_audit_payload()
    audit_event_id = _audit(
        session,
        ctx,
        aggregate_type="merchant",
        aggregate_id=ctx.merchant_id,
        event_type=INJECTION_EVENT_TYPE,
        payload=payload,
    )
    run_id = _record_run(
        session,
        ctx,
        injection_id=injection.injection_id,
        kind=injection.kind.value,
        payload=payload,
        audit_event_id=audit_event_id,
    )
    return InjectionOutcome(
        injection=injection,
        audit_event_id=audit_event_id,
        scenario_run_id=run_id,
        audit_payload=payload,
    )


# ------------------------------------------------------------------- reservation clock

#: The one raw statement in this module, and the reason it is here.
#:
#: A reservation's TTL is 300 seconds (ADR D13) and its expiry is judged by PostgreSQL,
#: never by a process clock: ``check_validity`` and ``release`` both compare against
#: ``now()``. That is right, and it is also why a demonstration cannot show an expiry
#: without waiting the hold out -- the kernel deliberately offers no "expire this now",
#: because such a call would let a pod running fast retire a hold a buyer still
#: legitimately holds.
#:
#: So this moves the *deadline*, not the state. Afterwards PostgreSQL genuinely agrees
#: the hold has lapsed, and the transition is made by
#: :func:`transaction_kernel.reservations.release`, which is the only code that writes
#: ``reservations.status``. Fast-forwarding a clock is the honest shape of this lever;
#: forcing a row to ``EXPIRED`` would assert something the rest of the system could not
#: verify, and the next admission would disagree with it.
_FAST_FORWARD = text(
    """
    UPDATE reservations
       SET expires_at = now()
     WHERE tenant_id = :tenant
       AND checkout_id = :checkout
       AND checkout_version = :version
       AND status = 'ACTIVE'
    """
)

_CURRENT_RESERVATION = text(
    """
    SELECT id, status, expires_at
      FROM reservations
     WHERE tenant_id = :tenant
       AND checkout_id = :checkout
       AND checkout_version = :version
    """
)


def expire_reservation(
    session: Session, ctx: RequestContext, *, checkout_id: uuid.UUID, version: int
) -> ReservationExpiry:
    """Retire one hold on the database clock, so a later admission denies for real.

    Three steps, in this order and no other: move the deadline to ``now()``; ask
    :func:`transaction_kernel.reservations.release` to make the ``ACTIVE -> EXPIRED``
    transition, which it does only once PostgreSQL agrees the hold has lapsed; then
    sweep, so no other lapsed hold in this tenant keeps reading ``ACTIVE`` to an
    operator. The sweep is hygiene and never a precondition -- admission refuses a lapsed
    hold whether or not its status column has caught up.

    A hold that is already ``CONSUMED``, ``RELEASED`` or ``EXPIRED`` is not an error. The
    outcome carries the code the kernel returned beside the status the row actually
    holds, which is what an operator needs in order to know which of the two happened.
    """
    if tk.read_head(session, tenant_id=ctx.tenant_id, checkout_id=checkout_id) is None:
        raise _no_such_checkout(checkout_id)

    before = session.execute(
        _CURRENT_RESERVATION,
        {"tenant": ctx.tenant_id, "checkout": checkout_id, "version": version},
    ).one_or_none()
    if before is None:
        raise ProblemError(
            404,
            "Reservation not found",
            "That checkout version has never held a reservation.",
            checkout_id=str(checkout_id),
            version=version,
        )
    status_before = ReservationStatus(before.status)

    session.execute(
        _FAST_FORWARD, {"tenant": ctx.tenant_id, "checkout": checkout_id, "version": version}
    )
    outcome = tk.reservations.release(
        session, checkout_id=checkout_id, checkout_version=version, cause=ReleaseCause.EXPIRED
    )
    swept = tk.reservations.sweep_expired(session)

    view = outcome.reservation
    status_after = None if view is None else view.status
    audit_event_id = _audit(
        session,
        ctx,
        aggregate_type="checkout",
        aggregate_id=checkout_id,
        event_type=f"{SCENARIO_EVENT_PREFIX}RESERVATION_EXPIRY",
        payload={
            "label": SCENARIO_LABEL,
            "checkout_id": str(checkout_id),
            "version": version,
            "reservation_id": str(before.id),
            "status_before": status_before.value,
            "status_after": "" if status_after is None else status_after.value,
            "code": outcome.code.value,
            "swept": swept,
        },
    )
    _record_run(
        session,
        ctx,
        injection_id=before.id,
        kind=f"{SCENARIO_EVENT_PREFIX}RESERVATION_EXPIRY",
        payload={"checkout_id": str(checkout_id), "version": version, "code": outcome.code.value},
        audit_event_id=audit_event_id,
    )
    return ReservationExpiry(
        checkout_id=checkout_id,
        version=version,
        reservation_id=before.id,
        status_before=status_before,
        status_after=status_after,
        expires_at=None if view is None else view.expires_at,
        code=outcome.code,
        swept=swept,
        audit_event_id=audit_event_id,
    )


# ----------------------------------------------------------------------- webhook replay

_DUPLICATE_COUNT = text(
    "SELECT duplicate_count FROM webhook_inbox WHERE tenant_id = :tenant AND id = :inbox_id"
)

#: Exactly the claim the receiver makes (ADR D7), re-made against the stored row. Zero
#: rows back is the proof the demonstration is after: PostgreSQL, not application logic,
#: is what stops a redelivered capture from being applied a second time.
_CLAIM_AGAIN = text(
    """
    INSERT INTO webhook_inbox (
        id, tenant_id, dedup_key, provider_event_id, event_type, body_digest, raw_body,
        headers_redacted, signature_verified, payment_id, order_id, refund_id, apply_status
    )
    SELECT :new_id, tenant_id, dedup_key, provider_event_id, event_type, body_digest,
           raw_body, headers_redacted, signature_verified, payment_id, order_id,
           refund_id, 'RECEIVED'
      FROM webhook_inbox
     WHERE tenant_id = :tenant AND id = :inbox_id
    ON CONFLICT (tenant_id, dedup_key) DO NOTHING
    RETURNING id
    """
)

_BUMP_DUPLICATE = text(
    """
    UPDATE webhook_inbox
       SET duplicate_count = duplicate_count + 1
     WHERE tenant_id = :tenant AND id = :inbox_id
    RETURNING duplicate_count
    """
)


def load_inbox_row(session: Session, ctx: RequestContext, inbox_id: uuid.UUID) -> WebhookInboxRow:
    """The stored delivery, or a 404. Row-level security scopes it to this tenant."""
    row = session.execute(
        select(WebhookInboxRow).where(
            WebhookInboxRow.tenant_id == ctx.tenant_id, WebhookInboxRow.id == inbox_id
        )
    ).scalar_one_or_none()
    if row is None:
        raise ProblemError(
            404,
            "Webhook delivery not found",
            "No stored webhook with that identifier belongs to this tenant.",
            inbox_id=str(inbox_id),
        )
    return row


def record_replay(
    session: Session,
    ctx: RequestContext,
    row: WebhookInboxRow,
    *,
    signature_reverified: bool,
    delivered: bool,
    delivery_status: int | None,
    delivery_reason: str,
    duplicate_count_before: int,
) -> ReplayOutcome:
    """Finish a replay: prove the dedupe, count the redelivery once, audit it.

    ``duplicate_count`` is incremented here only when the loopback delivery did not
    already increment it. A receiver that counts its own duplicates and a controller that
    counted them again would report two redeliveries for one replay, and the number an
    operator reads off the Inspector has to be the number of times the event arrived.

    Runs as the kernel role: the app role holds SELECT and nothing else on
    ``webhook_inbox`` (``platform_db.roles.WRITE_GRANTS``), because the inbox is the
    record of what a provider actually said.
    """
    claimed = session.execute(
        _CLAIM_AGAIN, {"new_id": uuid7(), "tenant": ctx.tenant_id, "inbox_id": row.id}
    ).one_or_none()
    duplicate_confirmed = claimed is None

    after: int = session.execute(
        _DUPLICATE_COUNT, {"tenant": ctx.tenant_id, "inbox_id": row.id}
    ).scalar_one()
    if after == duplicate_count_before:
        after = session.execute(
            _BUMP_DUPLICATE, {"tenant": ctx.tenant_id, "inbox_id": row.id}
        ).scalar_one()

    audit_event_id = _audit(
        session,
        ctx,
        aggregate_type="webhook_inbox",
        aggregate_id=row.id,
        event_type=f"{SCENARIO_EVENT_PREFIX}WEBHOOK_REPLAY",
        payload={
            "label": SCENARIO_LABEL,
            "inbox_id": str(row.id),
            "dedup_key": row.dedup_key,
            "provider_event_id": row.provider_event_id or "",
            "event_type": row.event_type,
            "signature_reverified": signature_reverified,
            "delivered": delivered,
            "delivery_status": 0 if delivery_status is None else delivery_status,
            "delivery_reason": delivery_reason,
            "duplicate_confirmed": duplicate_confirmed,
            "duplicate_count_before": duplicate_count_before,
            "duplicate_count_after": after,
        },
    )
    _record_run(
        session,
        ctx,
        injection_id=row.id,
        kind=f"{SCENARIO_EVENT_PREFIX}WEBHOOK_REPLAY",
        payload={"inbox_id": str(row.id), "dedup_key": row.dedup_key},
        audit_event_id=audit_event_id,
    )
    return ReplayOutcome(
        inbox_id=row.id,
        dedup_key=row.dedup_key,
        provider_event_id=row.provider_event_id,
        event_type=row.event_type,
        signature_reverified=signature_reverified,
        delivered=delivered,
        delivery_status=delivery_status,
        delivery_reason=delivery_reason,
        duplicate_confirmed=duplicate_confirmed,
        duplicate_count_before=duplicate_count_before,
        duplicate_count_after=after,
        apply_status=row.apply_status,
        state_before=row.state_before,
        state_after=row.state_after,
        changed=row.changed,
        audit_event_id=audit_event_id,
    )


# --------------------------------------------------------------------- duplicate submit


def _resolve_approval(
    session: Session,
    ctx: RequestContext,
    checkout_id: uuid.UUID,
    version: int,
    supplied: uuid.UUID | None,
) -> uuid.UUID:
    """The recorded approval this race will spend, found rather than taken on trust.

    A caller may name one, which is how a demo reproduces a specific run; otherwise the
    ``RECORDED`` approval for the version is read. Either way admission re-verifies the
    binding through ``consume_recorded`` (ADR D4a), so nothing here is load-bearing for
    correctness. It is load-bearing for the message an operator sees when they aim this
    lever at a version nobody has approved.
    """
    query = select(Approval.id).where(
        Approval.tenant_id == ctx.tenant_id,
        Approval.checkout_id == checkout_id,
        Approval.checkout_version == version,
    )
    query = (
        query.where(Approval.id == supplied)
        if supplied is not None
        else query.where(Approval.status == "RECORDED")
    )
    found = session.execute(query.limit(1)).scalar_one_or_none()
    if found is None:
        raise ProblemError(
            409,
            "No approval to submit",
            "That checkout version has no recorded buyer approval, so there is nothing "
            "for two tabs to race over. Approve it first.",
            checkout_id=str(checkout_id),
            version=version,
        )
    return found


def _race(
    requests: tuple[AdmissionRequest, ...],
    *,
    kernel_url: str,
    tenant_id: uuid.UUID,
    merchant_state: MerchantStateSource,
) -> tuple[KernelDecision, ...]:
    """Run both admissions at once, and return their decisions in submission order.

    The barrier is what makes this a race rather than a sequence. Without it the first
    thread routinely commits before the second one opens its transaction, and the
    demonstration shows a duplicate being replayed instead of genuine contention being
    resolved by the database.
    """
    barrier = threading.Barrier(len(requests), timeout=BARRIER_TIMEOUT_SECONDS)

    def run(request: AdmissionRequest) -> KernelDecision:
        with session_scope_for(kernel_url) as session:
            set_tenant(session, tenant_id)
            barrier.wait()
            return tk.admit(session, request, merchant_state)

    with ThreadPoolExecutor(max_workers=len(requests)) as pool:
        return tuple(pool.map(run, requests))


def duplicate_submit(
    session: Session,
    ctx: RequestContext,
    registry: MerchantRegistry,
    *,
    kernel_url: str,
    checkout_id: uuid.UUID,
    version: int,
    approval_id: uuid.UUID | None = None,
) -> DuplicateSubmitOutcome:
    """Two tabs, one approved version, one payment. ADR 0003 D9.

    Both submissions are real: two separate PostgreSQL transactions on two separate
    connections, released together by a barrier so they contend rather than queue, each
    carrying its own idempotency key. A shared key would replay the first response and
    prove nothing -- the interesting case is two honest clients that have never heard of
    each other.

    The kernel decides. One admission consumes the reservation, issues one Execution
    Grant and creates one payment attempt; the other is refused ``CONCURRENT_OPERATION``
    by the partial unique index on non-terminal attempts (ADR D4b). Both answers come
    back, because the two-tabs story is only convincing when the loser's refusal is
    visible beside the winner's grant.

    Reads and audits on the caller's session; the two admissions never touch it. An
    admission sharing the request's transaction could not contend with anything.
    """
    ctx.require("checkout.submit_approved")
    head = tk.read_head(session, tenant_id=ctx.tenant_id, checkout_id=checkout_id)
    if head is None:
        raise _no_such_checkout(checkout_id)

    versions = tk.read_versions(session, tenant_id=ctx.tenant_id, checkout_id=checkout_id)
    target = next((view for view in versions if view.version == version), None)
    if target is None:
        raise ProblemError(
            404,
            "Checkout version not found",
            "That checkout has no such version.",
            checkout_id=str(checkout_id),
            version=version,
        )

    resolved = _resolve_approval(session, ctx, checkout_id, version, approval_id)
    requests = tuple(
        AdmissionRequest(
            tenant_id=ctx.tenant_id,
            merchant_id=head.merchant_id,
            checkout=target.ref,
            amount=target.total,
            operation=DUPLICATE_SUBMIT_OPERATION,
            idempotency_key=f"scenario-dup-{checkout_id.hex[:12]}-{version}-{index}",
            principal=ctx.principal,
            correlation_id=ctx.correlation_id,
            approval_id=resolved,
        )
        for index in (1, 2)
    )
    decisions = _race(
        requests,
        kernel_url=kernel_url,
        tenant_id=ctx.tenant_id,
        merchant_state=registry.state_source(head.merchant_id),
    )

    admitted = tuple(decision for decision in decisions if decision.allowed)
    audit_event_id = _audit(
        session,
        ctx,
        aggregate_type="checkout",
        aggregate_id=checkout_id,
        event_type=f"{SCENARIO_EVENT_PREFIX}DUPLICATE_SUBMIT",
        payload={
            "label": SCENARIO_LABEL,
            "checkout_id": str(checkout_id),
            "version": version,
            "approval_id": str(resolved),
            "codes": [decision.code.value for decision in decisions],
            "decision_ids": [str(decision.decision_id) for decision in decisions],
            "admitted_count": len(admitted),
        },
    )
    _record_run(
        session,
        ctx,
        injection_id=checkout_id,
        kind=f"{SCENARIO_EVENT_PREFIX}DUPLICATE_SUBMIT",
        payload={"checkout_id": str(checkout_id), "version": version},
        audit_event_id=audit_event_id,
    )
    return DuplicateSubmitOutcome(
        checkout_id=checkout_id,
        version=version,
        approval_id=resolved,
        decisions=decisions,
        admitted_count=len(admitted),
        attempt_ids=tuple(
            decision.payment_attempt_id
            for decision in decisions
            if decision.payment_attempt_id is not None
        ),
        grant_ids=tuple(
            decision.grant_id for decision in decisions if decision.grant_id is not None
        ),
        audit_event_id=audit_event_id,
    )


# ------------------------------------------------------------------------ worker faults


def arm_fault(
    session: Session,
    ctx: RequestContext,
    *,
    kind: FaultKind,
    checkout_id: uuid.UUID | None = None,
    payment_attempt_id: uuid.UUID | None = None,
) -> ScenarioFault:
    """Arm one single-use worker-side fault.

    Runs as the **app** role, the only role with INSERT on ``scenario_faults``
    (``platform_db.roles.WRITE_GRANTS``). That is not an oversight in the grant set: a
    fault is armed before the payment attempt it will hit exists, so it cannot belong to
    a kernel transaction that has anything to do with money, and the kernel keeps only
    UPDATE so it can fill in the attempt the fault was consumed against.

    The consequence is stated rather than hidden: no audit event is written here, because
    the app role cannot append to ``audit_events`` by design -- evidence is kernel- and
    worker-written. The ``scenario_runs`` row is the label, and the worker writes the
    audit event when the fault actually fires, which is the moment worth recording. An
    armed fault that is never reached changed nothing.
    """
    if checkout_id is not None and payment_attempt_id is not None:
        raise _unprocessable(
            "Name a checkout or a payment attempt, not both. A fault armed against a "
            "checkout is waiting for the attempt that does not exist yet.",
            kind=kind.value,
        )
    if kind in TENANT_SCOPED_FAULTS and (checkout_id is not None or payment_attempt_id is not None):
        raise _unprocessable(
            f"{kind.value} is armed for the tenant and cannot name a checkout or a "
            "payment attempt. Its consumer is an agent turn, which has neither, so a "
            "row scoped to one would stay armed forever while you waited for it.",
            kind=kind.value,
        )
    fault = ScenarioFault(
        id=uuid7(),
        tenant_id=ctx.tenant_id,
        kind=kind.value,
        checkout_id=checkout_id,
        payment_attempt_id=payment_attempt_id,
        armed=True,
    )
    session.add(fault)
    session.flush()
    _record_run(
        session,
        ctx,
        injection_id=fault.id,
        kind=f"{SCENARIO_EVENT_PREFIX}FAULT_ARMED",
        payload={
            "fault_id": str(fault.id),
            "kind": kind.value,
            "checkout_id": "" if checkout_id is None else str(checkout_id),
            "payment_attempt_id": "" if payment_attempt_id is None else str(payment_attempt_id),
        },
        audit_event_id=None,
    )
    return fault


@dataclass(frozen=True, slots=True)
class TurnFaultClaimer:
    """Consumes the turn-scoped faults for one agent turn, on the kernel role.

    Handed to :func:`commerce_api.services.agent_service.run_turn` by the router, and
    handed as ``None`` whenever ``scenario_routes_enabled`` is false. That is the second
    of the three production gates and the only one that protects the *consuming* side:
    the arming routes already do not exist in production, and with ``None`` in place of
    this object the turn does not merely decline to fire a fault, it never asks. There is
    no query, no branch and nothing an operator could reach.

    It opens its own short transaction rather than borrowing the request's session for
    two reasons that are both grants rather than style. The turn runs as the **app** role,
    which holds INSERT on ``scenario_faults`` and neither UPDATE on it nor any privilege
    on ``audit_events``; claiming a fault is an UPDATE and recording that it fired is an
    append to the hash chain, so both need the kernel. And they need to be *the same*
    transaction: a fault disarmed without its audit event would be an injection that left
    no trace, and an audit event without the disarm would be a trace of something that
    can still happen again.
    """

    kernel_url: str

    def claim_for_turn(self, ctx: RequestContext, *, context: Mapping[str, str]) -> frozenset[str]:
        """Return the kinds that fired, as their wire names. Empty is the normal answer.

        Both kinds are consulted on every turn, on whichever surface the turn arrived
        from. That is the same contract the worker-side faults have -- an armed
        ``CREATE_ORDER_TIMEOUT`` is taken by the next create-order command, not by the one
        the operator was picturing -- and it is the honest one: the row says a failure is
        armed for this tenant, so the next turn on this tenant is the one that gets it. An
        operator arms immediately before the turn they mean to disturb.
        """
        fired: set[str] = set()
        with session_scope_for(self.kernel_url) as kernel:
            set_tenant(kernel, ctx.tenant_id)
            for kind in TURN_FAULTS:
                claimed = claim_scenario_fault(kernel, tenant_id=ctx.tenant_id, kind=kind.value)
                if claimed is None:
                    continue
                event_type = f"{SCENARIO_EVENT_PREFIX}{kind.value}_FIRED"
                payload = {"fault_id": str(claimed.fault_id), "kind": kind.value, **context}
                audit_event_id = _audit(
                    kernel,
                    ctx,
                    aggregate_type="merchant",
                    aggregate_id=ctx.merchant_id,
                    event_type=event_type,
                    payload=payload,
                )
                _record_run(
                    kernel,
                    ctx,
                    injection_id=claimed.fault_id,
                    kind=event_type,
                    payload=payload,
                    audit_event_id=audit_event_id,
                )
                fired.add(kind.value)
        return frozenset(fired)


# ------------------------------------------------------------------------- late capture


def invalidate_open_checkout(
    session: Session, ctx: RequestContext, *, checkout_id: uuid.UUID, reason: str
) -> InvalidationOutcome:
    """Take a checkout out of play while Razorpay Checkout is still open on the screen.

    The secondary scenario's first move (specification 31.2). Everything that makes it
    interesting happens afterwards and elsewhere: a capture arriving now is classified
    ``STALE_CAPTURE``, no order is written, and exactly one automatic refund is admitted.
    All of that belongs to the kernel, through
    :func:`transaction_kernel.apply_provider_evidence` and
    :func:`transaction_kernel.admit_stale_capture_refund`. This lever only creates the
    condition.

    The reservation is deliberately kept. A capture may still land, and stock that was in
    fact paid for must not be resold before that late capture has been refunded.
    """
    if tk.read_head(session, tenant_id=ctx.tenant_id, checkout_id=checkout_id) is None:
        raise _no_such_checkout(checkout_id)
    current = tk.checkouts.current_version(
        session, tenant_id=ctx.tenant_id, checkout_id=checkout_id
    )
    if current is None:  # pragma: no cover - a head always has at least version 1
        raise _no_such_checkout(checkout_id)

    result = tk.invalidate_open(
        session,
        tenant_id=ctx.tenant_id,
        checkout=CheckoutRef(checkout_id, current.version, current.content_hash),
        reason=reason,
        correlation_id=ctx.correlation_id,
        actor=ActorType.OPERATOR,
        principal_id=ctx.principal.principal_id,
    )
    audit_event_id = _audit(
        session,
        ctx,
        aggregate_type="checkout",
        aggregate_id=checkout_id,
        event_type=f"{SCENARIO_EVENT_PREFIX}INVALIDATE_OPEN",
        payload={
            "label": SCENARIO_LABEL,
            "checkout_id": str(checkout_id),
            "version": current.version,
            "content_hash": current.content_hash,
            "from": result.from_state.value,
            "to": result.to_state.value,
            "reason": reason,
        },
    )
    _record_run(
        session,
        ctx,
        injection_id=checkout_id,
        kind=f"{SCENARIO_EVENT_PREFIX}INVALIDATE_OPEN",
        payload={"checkout_id": str(checkout_id), "version": current.version, "reason": reason},
        audit_event_id=audit_event_id,
    )
    return InvalidationOutcome(
        checkout_id=checkout_id,
        version=current.version,
        content_hash=current.content_hash,
        from_state=result.from_state,
        to_state=result.to_state,
        reason=reason,
        audit_event_id=audit_event_id,
    )
