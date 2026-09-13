"""The merchant's own surface: propose a change, agree to it, and watch it happen.

Registry D. Everything here is about what a shop sells and what it says about it, and
nothing here moves money. There is no refund, no approve-a-checkout, no grant. A
merchant-initiated financial remedy crosses a narrow financial boundary with independently
checked permissions, which is a different request rather than a wider version of one of
these.

Approval names a digest
-----------------------
``POST .../approve`` takes the content hash the approver read. That is the whole design in
one required field: an action edited between the screen rendering and the button being
pressed will not match, so an approval can never attach to a document its approver never
saw. The refusal says both digests, because "this changed" is only useful if the reader can
tell what it changed from.

Two sessions, two roles, one story
----------------------------------
Everything except execution runs as the app role. ``execute`` runs as the kernel role,
because carrying the change out writes the merchant audit event in the same transaction as
the state change, and that append belongs to the kernel. The grants on ``merchant_actions``
say the same thing from the other side: the app role may insert and update, the kernel role
may only update. It moves rows along; it does not create them.

Behind the operator key, like every other privileged surface, and each route also names its
capability: ``merchant.action.propose`` to draft and withdraw, ``merchant.action.approve``
to agree, reject or perform. The two are separate strings even though one merchant session
holds both, because a copilot that drafts a change is an AGENT and holds neither -- and a
surface that merged them would make "propose" mean "do".
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Query, Request
from merchant_controller import MerchantActionKind
from pydantic import BaseModel, ConfigDict, Field

from ..deps import (
    AppSession,
    KernelSession,
    SessionContext,
    merchant_registry,
    require_scenario_key,
)
from ..schemas import rfc3339
from ..services import merchant_action_service

router = APIRouter(
    prefix="/v1/merchant/actions",
    tags=["merchant"],
    dependencies=[Depends(require_scenario_key)],
)

__all__ = ["router"]

DEFAULT_LIMIT: Final[int] = 50
MAX_LIMIT: Final[int] = 200


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActionOut(_Out):
    """One action as every surface reads it.

    ``content_hash`` is on the wire because an approver has to be able to send it back. It
    is not decoration and not a debugging aid: it is the thing being agreed to.

    ``proposed_by`` and ``approved_by`` are separate and stay separate. A model drafting a
    change is a fact whoever approves deserves, and a model is never written into the second
    field -- it remains an AGENT principal for its whole life.
    """

    action_id: str
    merchant_id: str
    kind: MerchantActionKind
    target: str
    proposal: dict[str, Any]
    #: The catalogue revision this was written against. An action whose shop has moved on
    #: is stale, and this is the field that makes that a fact rather than a guess.
    expected_revision: int
    state: str
    content_hash: str
    proposed_by: str
    approved_by: str | None
    outcome_note: str
    #: What the change moved: ``[{"field", "before", "after"}]``, empty until it succeeded.
    #:
    #: On the wire because a record of a change that cannot say what it changed *from* is
    #: half a record, and because restoring a value requires knowing it -- the helpdesk
    #: reads this to draft a reversal rather than asking somebody to remember.
    applied: list[dict[str, Any]]
    created_at: str
    updated_at: str


class ActionListOut(_Out):
    actions: list[ActionOut]
    returned: int
    limit: int
    may_have_more: bool


class ProposeRequest(BaseModel):
    """A change a merchant wants to make.

    No ``expected_revision`` field, on purpose. A revision a caller supplies is a revision a
    caller can supply from ten minutes ago, and the entire use of that number is that it
    records the world the proposal was actually written against. The server reads it.
    """

    model_config = ConfigDict(extra="forbid")

    kind: MerchantActionKind
    #: What is being changed: a SKU, an offer id. One thing, because a list invites a batch
    #: nobody reviewed line by line.
    target: str = Field(min_length=1, max_length=128)
    #: The typed body. Integers, strings, booleans and flat lists of those; a float or a
    #: nested object is refused by name, because a proposal a person cannot read as a short
    #: list of labelled values is one whose approval is weaker than it looks.
    proposal: dict[str, Any]


class EditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal: dict[str, Any]


class ApproveRequest(BaseModel):
    """The digest the approver read, sent back.

    Required, and the reason it is required is the reason the field exists. Approving by id
    alone would mean agreeing to whatever the row says at the moment the request lands,
    which is not the same as agreeing to what was on the screen.
    """

    model_config = ConfigDict(extra="forbid")

    content_hash: str = Field(min_length=1, max_length=64)


class NoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(default="", max_length=1000)


class ExecuteOut(_Out):
    """What happened, and the action as it now stands.

    ``ok`` is false for an ordinary outcome as well as an error. A shop that refuses a
    change which would change nothing is behaving correctly -- an injection that alters
    nothing would still advance the catalogue revision and make every open quote stale for
    no reason -- so the refusal is reported rather than raised.
    """

    action: ActionOut
    ok: bool
    reason: str
    allowed: list[str]


@router.get("", response_model=ActionListOut, summary="This merchant's actions, newest first")
def read_actions(
    ctx: SessionContext,
    session: AppSession,
    state: Annotated[str | None, Query(max_length=24)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ActionListOut:
    """Newest first, which is the opposite of the support queue and deliberately so.

    A support case is somebody waiting for an answer, so the oldest is the one owed. This is
    a merchant's record of what they have been doing, and what they want on top is what they
    just did.
    """
    actions = merchant_action_service.list_actions(
        session, ctx, state=state, limit=limit + 1, offset=offset
    )
    return ActionListOut(
        actions=[_out(action) for action in actions[:limit]],
        returned=min(len(actions), limit),
        limit=limit,
        may_have_more=len(actions) > limit,
    )


@router.post("", response_model=ActionOut, status_code=201, summary="Draft one change")
def propose(
    body: ProposeRequest, ctx: SessionContext, session: AppSession, request: Request
) -> ActionOut:
    """Draft it. Nothing happens to the shop, and nobody has been asked anything yet."""
    return _out(
        merchant_action_service.propose_action(
            session,
            ctx,
            merchant_registry(request),
            kind=body.kind,
            target=body.target,
            proposal=body.proposal,
        )
    )


@router.get("/context", summary="Current merchant offer and revision for a draft")
def action_context(ctx: SessionContext, session: AppSession, request: Request) -> dict[str, Any]:
    ctx.require("merchant.action.propose")
    store = merchant_registry(request).store(session, ctx.merchant_id)
    offer = store.promotion
    return {
        "merchant_id": str(ctx.merchant_id),
        "revision": store.revision,
        "offer": None
        if offer is None
        else {
            "offer_id": offer.offer_id,
            "label": offer.label,
            "percent_bp": offer.percent_bp,
            "flat_minor": None if offer.flat is None else offer.flat.minor,
            "effective_from_epoch_ms": offer.effective_from_epoch_ms,
            "effective_to_epoch_ms": offer.effective_to_epoch_ms,
        },
    }


@router.get("/{action_id}", response_model=ActionOut, summary="One action")
def read_action(action_id: uuid.UUID, ctx: SessionContext, session: AppSession) -> ActionOut:
    return _out(merchant_action_service.read_action(session, ctx, action_id=action_id))


@router.put("/{action_id}", response_model=ActionOut, summary="Change a draft")
def edit(
    action_id: uuid.UUID,
    body: EditRequest,
    ctx: SessionContext,
    session: AppSession,
    request: Request,
) -> ActionOut:
    """Only a draft. An edit produces a new digest, which is a new proposal.

    An edit that could reach an approved action would leave an approval naming a document
    nobody read, which is the single failure the digest exists to prevent.
    """
    return _out(
        merchant_action_service.edit_action(
            session, ctx, merchant_registry(request), action_id=action_id, proposal=body.proposal
        )
    )


@router.post("/{action_id}/submit", response_model=ActionOut, summary="Put it to an approver")
def submit(action_id: uuid.UUID, ctx: SessionContext, session: AppSession) -> ActionOut:
    return _out(merchant_action_service.submit_action(session, ctx, action_id=action_id))


@router.post("/{action_id}/approve", response_model=ActionOut, summary="Agree to this document")
def approve(
    action_id: uuid.UUID, body: ApproveRequest, ctx: SessionContext, session: AppSession
) -> ActionOut:
    """Approve the exact document named by ``content_hash``.

    Answers 409 when the digest is not the current one, with both values, so the reader can
    see what moved rather than being told to try again.
    """
    return _out(
        merchant_action_service.approve_action(
            session, ctx, action_id=action_id, content_hash=body.content_hash
        )
    )


@router.post("/{action_id}/reject", response_model=ActionOut, summary="Decline it, with a reason")
def reject(
    action_id: uuid.UUID, body: NoteRequest, ctx: SessionContext, session: AppSession
) -> ActionOut:
    return _out(
        merchant_action_service.reject_action(session, ctx, action_id=action_id, note=body.note)
    )


@router.post("/{action_id}/cancel", response_model=ActionOut, summary="Withdraw it")
def cancel(
    action_id: uuid.UUID, body: NoteRequest, ctx: SessionContext, session: AppSession
) -> ActionOut:
    return _out(
        merchant_action_service.cancel_action(session, ctx, action_id=action_id, note=body.note)
    )


@router.post("/{action_id}/execute", response_model=ExecuteOut, summary="Carry out an approval")
def execute(
    action_id: uuid.UUID, ctx: SessionContext, session: KernelSession, request: Request
) -> ExecuteOut:
    """Perform the approved change, or say precisely why it was not performed.

    The kernel session is not incidental. Carrying the change out writes the merchant audit
    event in the same transaction as the state change, so a shop that moved and a record
    saying it did not are impossible -- they commit together or neither happens.

    **Answers 200 either way**, like every other decision surface on this platform. A stale
    action, an edited one and a shop that refused the change are all ordinary outcomes with
    a reason, not faults for the caller to fix.
    """
    action, result = merchant_action_service.execute_action(
        session, ctx, merchant_registry(request), action_id=action_id
    )
    return ExecuteOut(
        action=_out(action),
        ok=result.ok,
        reason=result.reason,
        allowed=[state.value for state in result.allowed],
    )


def _out(action: merchant_action_service.ProposedAction) -> ActionOut:
    return ActionOut(
        action_id=str(action.action_id),
        merchant_id=str(action.merchant_id),
        kind=action.kind,
        target=action.target,
        proposal=dict(action.proposal),
        expected_revision=action.expected_revision,
        state=action.state.value,
        content_hash=action.content_hash,
        proposed_by=action.proposed_by,
        approved_by=action.approved_by,
        outcome_note=action.outcome_note,
        applied=[dict(entry) for entry in action.applied],
        created_at=rfc3339(action.created_at),
        updated_at=rfc3339(action.updated_at),
    )
