"""A merchant's change, from proposal to the moment it happens.

The lifecycle lives in :mod:`merchant_controller`, which owns the graph and the hash and
knows nothing about databases. This module is the half that persists it and carries it out,
and it is deliberately thin: every decision about what is allowed is asked of the Controller
rather than re-implemented here, because a second copy of a state machine is a state machine
that will disagree with the first one.

The approval is the point
-------------------------
An approver names the digest they read. Not the row -- the digest. ``approve`` refuses if
the caller's hash is not the current one, which means an action edited between the screen
rendering and the button being pressed cannot be approved by somebody who never saw the
edit. That is the same guarantee the buyer's checkout gives, arrived at the same way, and it
is the reason the hash exists at all.

Execution re-checks it, and checks the world too. Between approval and execution the
catalogue can move: an action approved against revision 41 is stale at 42, because the shelf
it described is not the shelf that exists. The Controller has a state for that and this
module puts it there rather than executing anyway.

Two roles, and which one runs where is not arbitrary. Proposing, editing, submitting,
approving, rejecting and cancelling move no money and run as the app role. Executing runs as
the kernel role, because carrying the change out writes a merchant audit event in the same
transaction as the state change, and that append is the kernel's. The grants say the same
thing: the app role may insert and update, the kernel role may only update.

What this module will not do
----------------------------
It issues no Execution Grant, admits no payment and touches no financial table. A
merchant-initiated refund crosses a narrow financial boundary with independently checked
permissions, which is a different request rather than a wider version of one of these.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from merchant_controller import (
    MerchantActionError,
    MerchantActionKind,
    MerchantActionResult,
    MerchantActionState,
    action_hash,
    build_action_content,
    may_move,
)
from platform_db.schema_service import MerchantAction as MerchantActionRow
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import RequestContext
from ..errors import ProblemError
from ..merchants import MerchantRegistry
from . import merchant_policy_service, scenario_service

__all__ = [
    "KIND_TO_INJECTION",
    "ProposedAction",
    "approve_action",
    "cancel_action",
    "edit_action",
    "execute_action",
    "list_actions",
    "propose_action",
    "read_action",
    "reject_action",
    "submit_action",
]

#: How a merchant action becomes a change the simulator can perform.
#:
#: The Controller's kinds are what a merchant asks for; the injection kinds are what the
#: store understands. They are deliberately not the same enum -- one is a merchant-facing
#: vocabulary and the other is the simulator's -- and this mapping is where the two meet, so
#: a kind added on either side without a partner is a ``KeyError`` here rather than an
#: action that can be approved and never executed.
#:
#: ``POLICY_PUBLISH`` is absent from this mapping and is not unbuilt. It writes a policy
#: version rather than injecting merchant state, so it has its own branch in
#: :func:`execute_action`; there is no injection kind for it because there is no injection.
KIND_TO_INJECTION: Final[dict[MerchantActionKind, scenario_service.InjectionKind]] = {
    MerchantActionKind.PRICE_CHANGE: scenario_service.InjectionKind.PRICE_SET,
    MerchantActionKind.STOCK_ADJUSTMENT: scenario_service.InjectionKind.STOCK_SET,
    MerchantActionKind.LISTING_CHANGE: scenario_service.InjectionKind.AVAILABILITY_SET,
    MerchantActionKind.OFFER_START: scenario_service.InjectionKind.OFFER_START,
    MerchantActionKind.OFFER_END: scenario_service.InjectionKind.OFFER_END,
}

#: The proposal field each kind carries its new value in. One field, named per kind, so the
#: executor never has to guess which key means "the number".
VALUE_FIELD: Final[dict[MerchantActionKind, str]] = {
    MerchantActionKind.PRICE_CHANGE: "unit_price_minor",
    MerchantActionKind.STOCK_ADJUSTMENT: "units",
    MerchantActionKind.LISTING_CHANGE: "listed",
}


@dataclass(frozen=True, slots=True)
class ProposedAction:
    """One action as every surface reads it."""

    action_id: uuid.UUID
    merchant_id: uuid.UUID
    kind: MerchantActionKind
    target: str
    proposal: Mapping[str, Any]
    expected_revision: int
    state: MerchantActionState
    content_hash: str
    proposed_by: str
    approved_by: str | None
    outcome_note: str
    #: What the change moved, as ``[{"field", "before", "after"}]``. Empty until it has
    #: succeeded, because until then it has moved nothing.
    applied: tuple[Mapping[str, Any], ...]
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------- proposing


#: Fields an executor reads back as whole numbers, by the kind that carries them.
#: ``build_action_content`` validates a proposal for *shape* on purpose -- "what a price
#: change may contain is the business module's question" -- and this is that module.
_INTEGER_FIELDS: Final[Mapping[MerchantActionKind, tuple[str, ...]]] = {
    MerchantActionKind.OFFER_START: (
        "percent_bp",
        "flat_minor",
        "effective_from_epoch_ms",
        "effective_to_epoch_ms",
    ),
}


def _refuse_ill_typed_proposal(kind: MerchantActionKind, proposal: Mapping[str, Any]) -> None:
    """Refuse a proposal whose numbers are not numbers, while the merchant is looking.

    ``_scalar`` accepts strings, because a proposal is a flat document a person reads, and
    most of its fields are prose. But ``_offer_terms`` reads four of them back with
    ``int()`` at execute time. A proposal carrying ``effective_from_epoch_ms: "soon"``
    therefore drafted, hashed, submitted and got a human's approval -- and then raised
    ``ValueError`` inside execute, which surfaced as a 500 and left the action sitting in
    APPROVED with no reason recorded and no way forward.

    A merchant who mistypes a date should be told so on the form, not after somebody has
    approved it. ``bool`` is refused before ``int`` because ``isinstance(True, int)`` is
    true and ``True`` is not a timestamp.
    """
    for field in _INTEGER_FIELDS.get(kind, ()):
        if field not in proposal:
            continue
        value = proposal[field]
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProblemError(
                422,
                "That field has to be a whole number",
                f"{field!r} is read back as an integer when this action is executed, so a "
                "value that is not one would fail after somebody had already approved it.",
                kind=kind.value,
                field=field,
            )


def propose_action(
    session: Session,
    ctx: RequestContext,
    registry: MerchantRegistry,
    *,
    kind: MerchantActionKind,
    target: str,
    proposal: Mapping[str, Any],
) -> ProposedAction:
    """Draft one change. It performs nothing and is not yet asking anybody for anything.

    ``expected_revision`` is read from the store rather than accepted from the caller. A
    revision a caller could supply is a revision a caller could supply from ten minutes ago,
    and the whole use of the field is that it records the world the proposal was written
    against.
    """
    ctx.require("merchant.action.propose")
    _refuse_ill_typed_proposal(kind, proposal)
    revision = registry.store(ctx.merchant_id).revision
    try:
        content = build_action_content(
            tenant_id=ctx.tenant_id,
            merchant_id=ctx.merchant_id,
            kind=kind,
            target=target,
            proposal=proposal,
            expected_revision=revision,
        )
    except MerchantActionError as cause:
        raise ProblemError(
            422, "That is not a proposal this shop can act on", str(cause)
        ) from cause

    row = MerchantActionRow(
        id=uuid.uuid4(),
        tenant_id=ctx.tenant_id,
        merchant_id=ctx.merchant_id,
        kind=kind.value,
        target=target,
        proposal=dict(content["proposal"]),
        expected_revision=revision,
        state=MerchantActionState.DRAFT.value,
        content_hash=action_hash(content),
        proposed_by=ctx.principal.principal_id,
    )
    session.add(row)
    session.flush()
    return _view(row)


def edit_action(
    session: Session,
    ctx: RequestContext,
    registry: MerchantRegistry,
    *,
    action_id: uuid.UUID,
    proposal: Mapping[str, Any],
) -> ProposedAction:
    """Change a draft, which produces a new digest and therefore a new proposal.

    Only from ``DRAFT``. That is not a convenience: an edit that could reach an approved
    action would leave an approval naming a document nobody read, which is the one failure
    the hash exists to prevent. The graph refuses it and so does this.

    The revision is re-read as well as the proposal. An edit made ten minutes later is a
    proposal about the shop as it is now, and carrying the old revision forward would let an
    approver agree to a change described against a world that had already moved.
    """
    ctx.require("merchant.action.propose")
    row = _locked(session, ctx, action_id)
    if row.state != MerchantActionState.DRAFT.value:
        raise ProblemError(
            409,
            "Only a draft can be edited",
            "This action has already been put to somebody. Cancel it and propose again.",
            action_id=str(action_id),
            state=row.state,
        )
    revision = registry.store(ctx.merchant_id).revision
    try:
        content = build_action_content(
            tenant_id=ctx.tenant_id,
            merchant_id=ctx.merchant_id,
            kind=MerchantActionKind(row.kind),
            target=row.target,
            proposal=proposal,
            expected_revision=revision,
        )
    except MerchantActionError as cause:
        raise ProblemError(
            422, "That is not a proposal this shop can act on", str(cause)
        ) from cause
    row.proposal = dict(content["proposal"])
    row.expected_revision = revision
    row.content_hash = action_hash(content)
    row.updated_at = datetime.now(UTC)
    session.flush()
    return _view(row)


# ------------------------------------------------------------------------ moving it along


def submit_action(session: Session, ctx: RequestContext, *, action_id: uuid.UUID) -> ProposedAction:
    """Put a draft to whoever approves. Nothing about the proposal changes."""
    ctx.require("merchant.action.propose")
    return _move(session, ctx, action_id, MerchantActionState.AWAITING_APPROVAL)


def approve_action(
    session: Session, ctx: RequestContext, *, action_id: uuid.UUID, content_hash: str
) -> ProposedAction:
    """Agree to one exact document, named by its digest.

    The hash is a required argument and not a convenience. An approver who names the digest
    is saying "I agree to *this*", and an action edited between the screen rendering and the
    button being pressed will not match -- so an approval can never attach to a document its
    approver did not see.

    ``approved_by`` is the session's own principal. A model proposing is recorded in
    ``proposed_by`` and is never copied here: a model stays an AGENT principal for its whole
    life and is never written down as the person who agreed to what it suggested.
    """
    ctx.require("merchant.action.approve")
    row = _locked(session, ctx, action_id)
    if content_hash != row.content_hash:
        raise ProblemError(
            409,
            "That is not the proposal in front of you any more",
            "This action was edited after the version you read. Look at it again before "
            "approving: an approval names the exact document, not the request.",
            action_id=str(action_id),
            approved_hash=content_hash,
            current_hash=row.content_hash,
        )
    # Set before the move, not after, because the two go to the database together. The
    # table's own constraint says an action past approval names its approver, and writing
    # the state first made that a violation rather than a guarantee -- which is the
    # constraint doing exactly what it was added for, on the first run.
    row.approved_by = ctx.principal.principal_id
    return _move(session, ctx, action_id, MerchantActionState.APPROVED, row=row)


def reject_action(
    session: Session, ctx: RequestContext, *, action_id: uuid.UUID, note: str = ""
) -> ProposedAction:
    ctx.require("merchant.action.approve")
    return _move(session, ctx, action_id, MerchantActionState.REJECTED, note=note)


def cancel_action(
    session: Session, ctx: RequestContext, *, action_id: uuid.UUID, note: str = ""
) -> ProposedAction:
    """Withdraw it. Reachable from every live state before the change has been sent."""
    ctx.require("merchant.action.propose")
    return _move(session, ctx, action_id, MerchantActionState.CANCELLED, note=note)


# ------------------------------------------------------------------------- carrying it out


def execute_action(
    session: Session,
    ctx: RequestContext,
    registry: MerchantRegistry,
    *,
    action_id: uuid.UUID,
) -> tuple[ProposedAction, MerchantActionResult]:
    """Perform an approved change, or say precisely why it was not performed.

    Three checks before anything happens, and each has its own answer.

    The digest must still describe the row. If it does not, the row moved after somebody
    approved it and the approval refers to a document that no longer exists.

    The catalogue revision must still be the one approved against. If it is not, the shelf
    the approver read about is not the shelf that exists, and the action is ``STALE`` rather
    than performed against a world nobody agreed to.

    And the store must accept the change. Nine of the simulator's twelve levers refuse their
    own no-op on purpose -- a change that changes nothing would still advance the revision
    and make every open quote stale for no reason -- so a refusal here is ordinary and is
    recorded as ``FAILED`` with what the store said, never retried into a loop.
    """
    ctx.require("merchant.action.approve")
    row = _locked(session, ctx, action_id)
    state = MerchantActionState(row.state)

    if not may_move(state, MerchantActionState.EXECUTING):
        return _view(row), _refusal(row, "not_a_permitted_move", state)

    rebuilt = build_action_content(
        tenant_id=row.tenant_id,
        merchant_id=row.merchant_id,
        kind=MerchantActionKind(row.kind),
        target=row.target,
        proposal=row.proposal,
        expected_revision=row.expected_revision,
    )
    if action_hash(rebuilt) != row.content_hash:
        _set(row, MerchantActionState.STALE, "the proposal was edited after it was approved")
        return _view(row), _refusal(row, "content_changed_after_approval", state)

    current_revision = registry.store(row.merchant_id).revision
    if current_revision != row.expected_revision:
        _set(
            row,
            MerchantActionState.STALE,
            f"approved against catalogue revision {row.expected_revision}; "
            f"the shop is now at {current_revision}",
        )
        return _view(row), _refusal(row, "catalogue_moved", state)

    _set(row, MerchantActionState.EXECUTING, "")
    session.flush()

    kind = MerchantActionKind(row.kind)
    if kind is MerchantActionKind.POLICY_PUBLISH:
        return _publish(session, ctx, row)

    try:
        outcome = scenario_service.apply_injection(
            session,
            ctx,
            registry,
            kind=KIND_TO_INJECTION[kind],
            sku=row.target if kind in VALUE_FIELD else None,
            value=row.proposal.get(VALUE_FIELD[kind]) if kind in VALUE_FIELD else None,
            note=f"merchant action {row.id}",
            offer=_offer_terms(row) if kind is MerchantActionKind.OFFER_START else None,
        )
    except ProblemError as refused:
        # The store said no, and that is an answer rather than a fault. Recorded as FAILED
        # with what it said, so the merchant reads the reason instead of a retry.
        _set(row, MerchantActionState.FAILED, refused.detail or refused.title)
        session.flush()
        return _view(row), _refusal(
            row, "the_shop_refused_the_change", MerchantActionState.EXECUTING
        )

    # What actually moved, kept rather than returned and forgotten. The simulator has
    # always computed this pair and the service has always dropped it, which left the
    # record saying what a price became and never what it was -- and made a revert
    # impossible to offer, since restoring a value requires knowing it.
    row.applied = [
        {"field": delta.field, "before": delta.before, "after": delta.after}
        for delta in outcome.injection.deltas
    ]
    _set(
        row,
        MerchantActionState.SUCCEEDED,
        f"catalogue revision {outcome.injection.revision_after}",
    )
    session.flush()
    return _view(row), MerchantActionResult(
        action_id=row.id,
        state=MerchantActionState.SUCCEEDED,
        content_hash=row.content_hash,
        ok=True,
        reason="ok",
    )


def _publish(
    session: Session, ctx: RequestContext, row: MerchantActionRow
) -> tuple[ProposedAction, MerchantActionResult]:
    """Carry out an approved policy change by publishing a new version.

    A different write from every other kind here, and the difference is the point. The other
    kinds change what the shop *is* -- a price, a stock level -- and go through the merchant
    state source. This changes what the shop *promises*, which is not state the kernel
    revalidates but terms the kernel freezes onto each sale. So it writes a row rather than
    injecting a change, and nothing about the catalogue moves.

    Which also means the revision check above was the wrong question for this kind and the
    right one anyway: a policy change does not depend on the shelf, but an action approved
    against a shop that has since moved is one whose approver was reading an older world,
    and refusing it costs a re-approval rather than a wrong promise.

    The new version does not touch orders already sold. That is the whole reason this exists:
    their receipts carry the terms they were sold under, and this row is a new version beside
    the old one rather than an edit to it.
    """
    family = merchant_policy_service.kind_of(row.target)
    # Read before publishing, because after it the previous terms are a row nobody on this
    # path looks up again. Same shape as a catalogue delta so one reader serves both, and
    # the whole family rather than a field: a published version replaces a family entire,
    # so a revert that restored one key would be restoring something nobody published.
    was = dict(
        merchant_policy_service.current_policy(
            session, tenant_id=ctx.tenant_id, merchant_id=ctx.merchant_id
        ).terms.get(family, {})
    )
    published = merchant_policy_service.publish_family(
        session,
        ctx,
        kind=family,
        terms=row.proposal,
        action_id=row.id,
    )
    row.applied = [{"field": family, "before": was, "after": dict(row.proposal)}]
    _set(
        row,
        MerchantActionState.SUCCEEDED,
        f"published as policy version {published.version}; "
        "orders already sold keep the terms they were sold under",
    )
    session.flush()
    return _view(row), MerchantActionResult(
        action_id=row.id,
        state=MerchantActionState.SUCCEEDED,
        content_hash=row.content_hash,
        ok=True,
        reason="ok",
    )


# ------------------------------------------------------------------------------- reading


def list_actions(
    session: Session,
    ctx: RequestContext,
    *,
    state: str | None = None,
    limit: int = 50,
) -> list[ProposedAction]:
    """This merchant's own worklist, newest first.

    Newest first, unlike the support queue, and the difference is real rather than a taste.
    A support case is somebody waiting for an answer, so the oldest is the one owed. An
    action list is a merchant's record of what they have been doing, and the thing they want
    on top is what they just did.

    Scoped to the session's own merchant as well as the tenant. Row-level security answers
    the tenant question; a tenant may hold several merchants and no policy names one.
    """
    ctx.require("merchant.action.propose")
    query = select(MerchantActionRow).where(
        MerchantActionRow.tenant_id == ctx.tenant_id,
        MerchantActionRow.merchant_id == ctx.merchant_id,
    )
    if state is not None:
        if state not in {s.value for s in MerchantActionState}:
            raise ProblemError(
                422,
                "Unknown state",
                "That is not a state a merchant action can be in.",
                requested=state,
                allowed=sorted(s.value for s in MerchantActionState),
            )
        query = query.where(MerchantActionRow.state == state)
    rows = session.execute(
        query.order_by(MerchantActionRow.created_at.desc(), MerchantActionRow.id).limit(limit)
    ).scalars()
    return [_view(row) for row in rows]


def read_action(session: Session, ctx: RequestContext, *, action_id: uuid.UUID) -> ProposedAction:
    ctx.require("merchant.action.propose")
    return _view(_load(session, ctx, action_id))


# ------------------------------------------------------------------------------ internals


def _load(session: Session, ctx: RequestContext, action_id: uuid.UUID) -> MerchantActionRow:
    row = session.execute(
        select(MerchantActionRow).where(
            MerchantActionRow.tenant_id == ctx.tenant_id,
            MerchantActionRow.merchant_id == ctx.merchant_id,
            MerchantActionRow.id == action_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise ProblemError(
            404, "Action not found", "No such merchant action.", action_id=str(action_id)
        )
    return row


def _locked(session: Session, ctx: RequestContext, action_id: uuid.UUID) -> MerchantActionRow:
    """The row, held for the rest of this transaction.

    Every mutation takes the lock. Two people on one queue is ordinary, and without it the
    second press reads a state the first has already left and writes over its decision.
    """
    row = session.execute(
        select(MerchantActionRow)
        .where(
            MerchantActionRow.tenant_id == ctx.tenant_id,
            MerchantActionRow.merchant_id == ctx.merchant_id,
            MerchantActionRow.id == action_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise ProblemError(
            404, "Action not found", "No such merchant action.", action_id=str(action_id)
        )
    return row


def _move(
    session: Session,
    ctx: RequestContext,
    action_id: uuid.UUID,
    to: MerchantActionState,
    *,
    note: str = "",
    row: MerchantActionRow | None = None,
) -> ProposedAction:
    """Ask the Controller whether the move is legal, then make it.

    The graph is not re-implemented here. A second copy of a state machine is one that will
    disagree with the first, and the disagreement will be discovered by whichever of them is
    wrong about money.
    """
    target = row if row is not None else _locked(session, ctx, action_id)
    current = MerchantActionState(target.state)
    if not may_move(current, to):
        from merchant_controller import TRANSITIONS

        raise ProblemError(
            409,
            "That is not a move this action can make",
            f"An action that is {current.value} cannot become {to.value}.",
            action_id=str(action_id),
            action_state=current.value,
            allowed=sorted(s.value for s in TRANSITIONS[current]),
        )
    _set(target, to, note)
    session.flush()
    return _view(target)


def _set(row: MerchantActionRow, to: MerchantActionState, note: str) -> None:
    row.state = to.value
    if note:
        row.outcome_note = note
    row.updated_at = datetime.now(UTC)


def _refusal(
    row: MerchantActionRow, reason: str, from_state: MerchantActionState
) -> MerchantActionResult:
    from merchant_controller import TRANSITIONS

    return MerchantActionResult(
        action_id=row.id,
        state=MerchantActionState(row.state),
        content_hash=row.content_hash,
        ok=False,
        reason=reason,
        allowed=tuple(sorted(TRANSITIONS[from_state], key=lambda s: s.value)),
    )


def _offer_terms(row: MerchantActionRow) -> scenario_service.OfferTerms:
    """The offer body, as the scenario service's own typed shape.

    Built here rather than stored as one, because the proposal is a flat document a person
    read and agreed to. Converting it at the last moment keeps the hashed thing and the
    executed thing the same thing.
    """
    proposal = row.proposal
    return scenario_service.OfferTerms(
        offer_id=str(proposal.get("offer_id", row.target)),
        label=str(proposal.get("label", "")),
        percent_bp=proposal.get("percent_bp"),
        flat_minor=proposal.get("flat_minor"),
        effective_from_epoch_ms=int(proposal.get("effective_from_epoch_ms", 0)),
        effective_to_epoch_ms=int(proposal.get("effective_to_epoch_ms", 0)),
    )


def _view(row: MerchantActionRow) -> ProposedAction:
    return ProposedAction(
        action_id=row.id,
        merchant_id=row.merchant_id,
        kind=MerchantActionKind(row.kind),
        target=row.target,
        proposal=dict(row.proposal),
        expected_revision=row.expected_revision,
        state=MerchantActionState(row.state),
        content_hash=row.content_hash,
        proposed_by=row.proposed_by,
        approved_by=row.approved_by,
        outcome_note=row.outcome_note,
        applied=tuple(dict(entry) for entry in (row.applied or ())),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
