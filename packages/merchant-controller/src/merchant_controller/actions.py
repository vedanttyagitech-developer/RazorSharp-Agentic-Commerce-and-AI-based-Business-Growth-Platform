"""What a merchant action is, what states it may be in, and what it hashes to.

The Controller's own vocabulary, and deliberately not the kernel's. A merchant action is
not an ``Operation``, its outcome is not an ``AdmissionDecision``, and nothing here is an
Execution Grant. Those three name provider mutations that move a buyer's money under a
single-use authority, and reusing them for "raise the price of milk" would say that
changing a shelf label and taking a payment are the same kind of event, reviewed by the
same rules. They are not, and the moment one type covers both, the narrower guarantee is
the one that quietly loosens.

So this package imports nothing from the Transaction Trust Kernel, and a test asserts it.

What the hash is for
--------------------
A merchant action is proposed -- possibly by a model -- and approved by a person. Between
those two moments the proposal must not change, and after approval the thing executed must
be the thing approved. That is the same problem the buyer's checkout has, and it gets the
same answer: one canonical document, one hash over it, and an approval that names the hash
rather than the row.

An edit therefore produces a new hash, which invalidates the approval by construction
rather than by anybody remembering to revoke it. There is no state in which an approved
action and an edited action are the same action.

``expected_revision`` is inside the hashed document, and that is the second half of the
guarantee. An action approved against catalogue revision 41 says so; if the catalogue has
moved to 42 by the time it runs, the world it was approved against is gone and the action
is stale. Without it, an approval means "somebody agreed to this change" rather than
"somebody agreed to this change to *that* shelf".

Integers only, as everywhere else on this platform. Money is minor units beside an ISO
4217 code; the JSON profile in :mod:`commerce_domain` refuses floats outright, so a float
fails to hash rather than hashing to something a later verifier cannot reproduce.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from commerce_domain import CanonicalizationError, DomainError, canonical_hash

__all__ = [
    "ACTION_KEYS",
    "ACTION_VERSION",
    "LIVE_STATES",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "MerchantAction",
    "MerchantActionError",
    "MerchantActionKind",
    "MerchantActionResult",
    "MerchantActionState",
    "action_hash",
    "build_action_content",
    "may_move",
]

#: Inside the hashed document. A change to the shape -- a new key, a renamed key, a
#: different ordering -- must bump this, because every stored approval names a hash
#: computed under the old shape. The regression vector in the tests pins one document and
#: its digest; if that fails, stored approvals are already unverifiable.
ACTION_VERSION: Final = "merchant_action/1"

#: The closed key set of the hashed document. Closed rather than minimal: a document with
#: an unexpected key is refused rather than hashed, because a producer that could add one
#: could add a field the approver never saw and the executor would honour.
ACTION_KEYS: Final[frozenset[str]] = frozenset(
    {
        "action_version",
        "tenant_id",
        "merchant_id",
        "kind",
        "target",
        "proposal",
        "expected_revision",
    }
)


class MerchantActionError(DomainError):
    """A proposal that cannot be represented, hashed or moved."""


class MerchantActionKind(StrEnum):
    """What the merchant is changing.

    Closed, and short on purpose. Each member has to have somewhere to go: an action kind
    with no business module behind it is a queue entry that can be approved and never
    executed, which is worse than not offering it, because the approval is a promise.
    """

    PRICE_CHANGE = "PRICE_CHANGE"
    STOCK_ADJUSTMENT = "STOCK_ADJUSTMENT"
    #: Units arrived from a supplier. Not the same event as an adjustment even when the
    #: resulting number is: a delivery is somebody bringing goods, and a correction is
    #: somebody saying the shelf disagrees with the ledger. The inventory ledger keeps them
    #: apart, so "where did these units come from" has an answer years later.
    STOCK_RECEIPT = "STOCK_RECEIPT"
    LISTING_CHANGE = "LISTING_CHANGE"
    OFFER_START = "OFFER_START"
    OFFER_END = "OFFER_END"
    POLICY_PUBLISH = "POLICY_PUBLISH"


class MerchantActionState(StrEnum):
    """Where an action is in its life, specification 9.

    Six of these are outcomes and six are stages, and the split is what makes the queue
    readable: a person looking at the list needs to know which rows are waiting on them and
    which are finished, without having to remember which words mean which.

    ``UNKNOWN`` is not a failure. It is the honest state of an action whose executor sent
    the change and never heard back, and it is distinct from ``FAILED`` because the two
    call for opposite next steps -- one is retried, and the other must be reconciled before
    anybody touches it again. The kernel makes the same distinction about payments for the
    same reason.
    """

    DRAFT = "DRAFT"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    QUEUED = "QUEUED"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    STALE = "STALE"


#: The graph, and it is the whole safety story of the lifecycle.
#:
#: Read the shape rather than the entries. Nothing returns from an outcome, so an executed
#: action cannot be edited and re-run under its old approval. ``APPROVED`` does not go back
#: to ``DRAFT``: an edit is a new hash and therefore a new proposal, and a route that let a
#: draft keep its approval across an edit is the one bug this design exists to prevent.
#:
#: ``STALE`` is reachable from ``APPROVED`` and ``QUEUED`` but not from ``EXECUTING``. Once
#: the executor has begun, whether the world moved underneath is the executor's finding to
#: report as ``FAILED`` or ``UNKNOWN``; a third party marking it stale mid-flight would be
#: deciding an outcome it cannot see.
TRANSITIONS: Final[dict[MerchantActionState, frozenset[MerchantActionState]]] = {
    MerchantActionState.DRAFT: frozenset(
        {
            MerchantActionState.AWAITING_APPROVAL,
            MerchantActionState.CANCELLED,
        }
    ),
    MerchantActionState.AWAITING_APPROVAL: frozenset(
        {
            MerchantActionState.APPROVED,
            MerchantActionState.REJECTED,
            MerchantActionState.CANCELLED,
            MerchantActionState.EXPIRED,
        }
    ),
    #: Two ways out of APPROVED, and which one is taken says where the work runs.
    #:
    #: ``QUEUED`` is the out-of-process path: an executor leases the action and carries it
    #: out somewhere else. ``EXECUTING`` directly is the in-process one, which is what this
    #: platform does today and not a shortcut. Merchant state lives in the API process's own
    #: memory behind a lock (ADR D14); the durable worker is a separate process with no
    #: merchant registry, so an action queued to it would be delivered to something that
    #: cannot perform it. Queuing exists in this graph for the day a merchant executor does,
    #: and until then the honest path is the one that runs where the state is.
    MerchantActionState.APPROVED: frozenset(
        {
            MerchantActionState.QUEUED,
            MerchantActionState.EXECUTING,
            MerchantActionState.CANCELLED,
            MerchantActionState.EXPIRED,
            MerchantActionState.STALE,
        }
    ),
    MerchantActionState.QUEUED: frozenset(
        {
            MerchantActionState.EXECUTING,
            MerchantActionState.CANCELLED,
            MerchantActionState.STALE,
        }
    ),
    MerchantActionState.EXECUTING: frozenset(
        {
            MerchantActionState.SUCCEEDED,
            MerchantActionState.FAILED,
            MerchantActionState.UNKNOWN,
        }
    ),
    MerchantActionState.SUCCEEDED: frozenset(),
    MerchantActionState.FAILED: frozenset(),
    #: An unknown outcome is reconciled, never retried. Reconciliation discovers what
    #: actually happened and moves it to the truth, which is why this one is not terminal.
    MerchantActionState.UNKNOWN: frozenset(
        {
            MerchantActionState.SUCCEEDED,
            MerchantActionState.FAILED,
        }
    ),
    MerchantActionState.REJECTED: frozenset(),
    MerchantActionState.EXPIRED: frozenset(),
    MerchantActionState.CANCELLED: frozenset(),
    MerchantActionState.STALE: frozenset(),
}

#: States in which somebody or something still owes this action a move.
LIVE_STATES: Final[frozenset[MerchantActionState]] = frozenset(
    {
        MerchantActionState.DRAFT,
        MerchantActionState.AWAITING_APPROVAL,
        MerchantActionState.APPROVED,
        MerchantActionState.QUEUED,
        MerchantActionState.EXECUTING,
        MerchantActionState.UNKNOWN,
    }
)

#: States from which nothing further happens. Derived from the graph rather than listed, so
#: the two cannot disagree.
TERMINAL_STATES: Final[frozenset[MerchantActionState]] = frozenset(
    state for state, onward in TRANSITIONS.items() if not onward
)


def may_move(current: MerchantActionState, to: MerchantActionState) -> bool:
    """Whether the graph permits this move. The single answer to that question."""
    return to in TRANSITIONS[current]


def build_action_content(
    *,
    tenant_id: uuid.UUID,
    merchant_id: uuid.UUID,
    kind: MerchantActionKind,
    target: str,
    proposal: Mapping[str, Any],
    expected_revision: int,
) -> dict[str, Any]:
    """The canonical document a merchant action hashes to.

    ``target`` names what is being changed -- a SKU, an offer id, a policy family -- as one
    string, because every kind here changes exactly one thing and a list would invite a
    batch nobody reviewed line by line.

    ``proposal`` is the typed body for the kind, and it is validated for *shape* rather
    than for meaning: integers, strings, booleans and nothing else, nested no deeper than
    one level. What a price change may contain is the business module's question; what can
    be hashed and shown to an approver is this one. A float or a decimal is refused here
    rather than at the canonicaliser, so the error names the field.

    Raises :class:`MerchantActionError` rather than returning a partial document. There is
    no half-built proposal worth passing on: it would be hashed, approved, and executed.
    """
    if expected_revision < 0:
        raise MerchantActionError(
            f"expected_revision must not be negative, got {expected_revision}"
        )
    if not target or len(target) > 128:
        raise MerchantActionError("target must be a non-empty string of at most 128 characters")
    document = {
        "action_version": ACTION_VERSION,
        "tenant_id": str(tenant_id),
        "merchant_id": str(merchant_id),
        "kind": kind.value,
        "target": target,
        "proposal": _clean(proposal),
        "expected_revision": expected_revision,
    }
    keys = frozenset(document)
    if keys != ACTION_KEYS:
        raise MerchantActionError(f"document keys {sorted(keys)} are not {sorted(ACTION_KEYS)}")
    return document


def _clean(proposal: Mapping[str, Any]) -> dict[str, Any]:
    """The proposal body, refused unless every value is something a person can be shown.

    Nesting is capped at one level deliberately. A proposal is read by a human before it is
    approved, and a structure that cannot be rendered as a short list of labelled values is
    one whose approval means less than it appears to.
    """
    if not proposal:
        raise MerchantActionError("a proposal with no fields changes nothing")
    out: dict[str, Any] = {}
    for key, value in proposal.items():
        if not isinstance(key, str) or not key:
            raise MerchantActionError(f"proposal keys must be non-empty strings, got {key!r}")
        out[key] = _scalar(value, key)
    return out


def _scalar(value: Any, path: str) -> Any:
    # `bool | int | str` accepts booleans in its own right rather than through `int`,
    # which is what matters: `isinstance(True, int)` is true in Python, and a boolean
    # coerced to 1 inside a hashed document makes two proposals that read differently to a
    # human hash identically to a verifier.
    if isinstance(value, bool | int | str):
        return value
    if value is None:
        return None
    if isinstance(value, list | tuple):
        return [_scalar(item, f"{path}[]") for item in value]
    raise MerchantActionError(
        f"proposal field {path!r} is a {type(value).__name__}; a merchant action carries "
        "integers, strings, booleans, nulls and flat lists of those. Money is minor units "
        "beside its currency, never a decimal."
    )


def action_hash(content: Mapping[str, Any]) -> str:
    """The digest an approval names. One definition, so two producers cannot disagree."""
    try:
        return canonical_hash(dict(content))
    except CanonicalizationError as cause:
        raise MerchantActionError(f"action content cannot be canonicalised: {cause}") from cause


@dataclass(frozen=True, slots=True)
class MerchantAction:
    """One proposal, its state, and the hash an approval binds to.

    Frozen. A state change produces a new value rather than mutating this one, which is
    what lets a caller hold the version it decided against while the row moves on.
    """

    action_id: uuid.UUID
    tenant_id: uuid.UUID
    merchant_id: uuid.UUID
    kind: MerchantActionKind
    target: str
    proposal: Mapping[str, Any]
    expected_revision: int
    state: MerchantActionState
    content_hash: str
    #: Who proposed it. An ``AGENT`` principal here is a fact worth keeping: a change a
    #: model drafted and a change a person typed deserve different scrutiny from whoever
    #: approves, and only the row remembers which it was.
    proposed_by: str
    #: Who approved it, once somebody has. Never the proposer when the proposer is a model:
    #: a model remains an AGENT principal and is never relabelled as the human who agreed.
    approved_by: str | None = None

    def content(self) -> dict[str, Any]:
        """Rebuild the hashed document from the stored fields."""
        return build_action_content(
            tenant_id=self.tenant_id,
            merchant_id=self.merchant_id,
            kind=self.kind,
            target=self.target,
            proposal=self.proposal,
            expected_revision=self.expected_revision,
        )

    def hash_matches(self) -> bool:
        """Whether the stored hash still describes the stored fields.

        The check that makes an edit visible. A row whose fields moved without its hash
        moving is a row whose approval refers to something that no longer exists, and this
        is how the executor finds that out before acting rather than after.
        """
        return action_hash(self.content()) == self.content_hash


@dataclass(frozen=True, slots=True)
class MerchantActionResult:
    """What the Controller answers. Structured, never prose, and never the kernel's type.

    Astra's plan names this type specifically, and the reason is worth keeping beside it:
    an ``AdmissionDecision`` carries a grant id because every allowed admission issues
    exactly one single-use authority over a buyer's money. A merchant action issues no such
    thing. Borrowing the shape would put a field here that is always null and teach every
    reader that the two are interchangeable.

    ``reason`` is a stable key, not a sentence. The console renders it; nothing may alter
    the fields from the rendering.
    """

    action_id: uuid.UUID
    state: MerchantActionState
    content_hash: str
    ok: bool
    reason: str
    #: What the graph would have allowed, when the answer is no. Empty otherwise, so a
    #: caller never has to distinguish "no moves offered" from "field not populated".
    allowed: tuple[MerchantActionState, ...] = ()

    def __post_init__(self) -> None:
        if self.ok and self.allowed:
            raise ValueError("a successful result does not carry a list of alternatives")
        if not self.ok and self.reason == "ok":
            raise ValueError("a refusal must say which rule refused it")
