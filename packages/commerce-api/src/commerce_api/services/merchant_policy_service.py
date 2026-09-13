"""What a shop promises, published as versions that never change.

The Policy-at-Sale Receipt has always frozen a merchant's terms onto each order, so that a
later change cannot narrow what a buyer was sold. Until this module the terms were constants
in the simulator, which made the guarantee real machinery aimed at an event nobody could
cause. A reviewer asking to see it survive a policy change got told the merchant cannot make
one.

This is the writer that lets them. It is deliberately small: publish a version, read the
version in force. There is no scheduling, no draft state and no separate authoring surface,
because the approval path already exists -- a policy change is a merchant action like any
other, proposed, agreed to by name, and only then carried out.

One family per publication
--------------------------
A published version carries the complete term set, and a publication changes one family
within it. That is not a compromise with the action model's flat proposal, although it fits
it: a version that is a diff makes the oldest receipt the hardest to verify, and a
publication that changed four families at once would be four decisions somebody approved
with one press.

The current version is the highest one
--------------------------------------
No pointer column. A pointer is a second source of truth that can disagree with the rows it
points at, and the only thing it would buy is publishing a version without making it
effective -- which is scheduling. Two concurrent publications are serialised on the
merchant's own row, so they produce two versions or one failure, never one version with two
meanings.

Immutable by grant, and that is the whole of it. No role holds UPDATE, so narrowing a term
means adding a version beside the old one; the missing grant is what makes that the only way
rather than the polite way. Both writing roles may INSERT, because carrying out an approved
change runs in whichever transaction is already recording the action.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from commerce_domain import uuid7
from merchant_adapter import DEFAULT_TERMS, PUBLISHABLE_KINDS
from platform_db import Merchant
from platform_db.schema_service import MerchantPolicyVersion
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..deps import RequestContext
from ..errors import ProblemError

__all__ = [
    "OPENING_AUTHOR",
    "OPENING_VERSION",
    "PublishedPolicy",
    "current_policy",
    "kind_of",
    "publish_family",
]

#: The version a shop's opening position is, whether or not a row exists for it yet.
#:
#: One rather than zero, because the receipt's own contract requires a policy version of at
#: least one -- a receipt naming version zero is refused by the kernel, which is right: it
#: would be a sale under terms nobody numbered.
#:
#: There is no row for it until somebody publishes a change, and then there are two: see
#: :func:`publish_family`. Until that happens every sale is made under the same opening
#: position, so a row saying so would be a row nothing distinguishes.
OPENING_VERSION: Final[int] = 1

#: Who a shop's opening position is attributed to. Not a person and not a session: nobody
#: approved it, and writing a merchant's name against terms they never chose would be the
#: same lie as writing a model's name against terms a person approved.
OPENING_AUTHOR: Final[str] = "platform:opening_position"


@dataclass(frozen=True, slots=True)
class PublishedPolicy:
    """The terms in force, and which version they are."""

    version: int
    terms: Mapping[str, Mapping[str, Any]]
    #: False when nothing has been published and the shop's opening position applies.
    chosen: bool
    #: Who published this version. :data:`OPENING_AUTHOR` while nothing has been, because
    #: attributing a merchant's name to terms they were merely started with is the same
    #: lie in miniature as attributing a model's name to terms a person approved.
    published_by: str = OPENING_AUTHOR


def current_policy(
    session: Session, *, tenant_id: uuid.UUID, merchant_id: uuid.UUID
) -> PublishedPolicy:
    """The version in force for one merchant, or the opening position.

    Read on the checkout path, so it is one indexed row and no join. The index is
    ``(tenant_id, merchant_id, version)``, which serves this and the publication's own
    next-version read.
    """
    row = session.execute(
        select(MerchantPolicyVersion)
        .where(
            MerchantPolicyVersion.tenant_id == tenant_id,
            MerchantPolicyVersion.merchant_id == merchant_id,
        )
        .order_by(MerchantPolicyVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return PublishedPolicy(version=OPENING_VERSION, terms=DEFAULT_TERMS, chosen=False)
    return PublishedPolicy(
        version=row.version,
        terms=dict(row.terms),
        chosen=True,
        published_by=str(row.published_by),
    )


def _refuse_ill_typed_terms(kind: str, terms: Mapping[str, Any]) -> None:
    """Refuse a term whose type is not the type of the opening position it replaces.

    Nothing checked this, and the cost was not a bad policy document -- it was the
    storefront. ``merchant_adapter`` builds the Policy-at-Sale Receipt by reading these
    values back, and one of them it reads as a number::

        cancellation["fee"] = Money(int(cancellation.pop("fee_minor", 0)), currency)

    So publishing ``{"fee_minor": "waived"}`` -- which the merchant console's own form can
    send, and which this function used to accept because the mapping was non-empty --
    succeeded, and from that moment every ``receipt_inputs_for`` raised ``ValueError``.
    That call is on the *buyer's* checkout-creation and requote paths, so a merchant
    editing their own cancellation policy took their storefront down permanently, and the
    failure surfaced on a buyer's checkout button with no way back except another publish.

    The rule is the opening position's own shape: a published value must be the type of the
    default it replaces. That needs no schema of its own, cannot drift from
    ``DEFAULT_TERMS``, and gives the merchant the field name back. ``bool`` is checked
    before ``int`` because it is a subclass of it, and ``True`` is not a fee.
    """
    opening = DEFAULT_TERMS.get(kind, {})
    for field, value in terms.items():
        expected = opening.get(field)
        if expected is None:
            # A term the opening position does not name. Merchants may add their own, and
            # nothing reads them as numbers, so there is nothing here to get wrong.
            continue
        if isinstance(expected, bool):
            ok = isinstance(value, bool)
            wanted = "true or false"
        elif isinstance(expected, int):
            ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
            wanted = "a whole number of minor units, zero or more"
        else:
            ok = isinstance(value, str)
            wanted = "text"
        if not ok:
            raise ProblemError(
                422,
                "That term is the wrong kind of value",
                f"{field!r} must be {wanted}. The receipt reads this value back when a "
                "sale is made, so a value of the wrong type would fail at checkout rather "
                "than here.",
                kind=kind,
                field=field,
                expected=wanted,
            )


def publish_family(
    session: Session,
    ctx: RequestContext,
    *,
    kind: str,
    terms: Mapping[str, Any],
    action_id: uuid.UUID | None = None,
) -> PublishedPolicy:
    """Publish a new version in which one family is replaced and the rest carry forward.

    Serialised by a transaction lock scoped to the tenant and merchant.
    Racing publications would otherwise read the same highest version and try to
    write the same next version. The lock serializes them; the unique constraint
    catches callers that skip this function.

    ``action_id`` links the version to the merchant action that was approved for it, so a
    term can be traced to the person who agreed to it. Nullable, because a version seeded
    outside the approval path is a different thing from one nobody approved -- and both are
    better than a fabricated id.
    """
    if kind not in PUBLISHABLE_KINDS:
        raise ProblemError(
            422,
            "That is not a policy family a merchant publishes",
            "Delivery charges come from the fee policy and discounts from the running "
            "offer. Both already reach the receipt by their own path.",
            requested=kind,
            allowed=sorted(PUBLISHABLE_KINDS),
        )
    if not terms:
        raise ProblemError(
            422,
            "A policy with no terms promises nothing",
            "Publishing an empty family would replace a promise with a blank.",
            kind=kind,
        )
    _refuse_ill_typed_terms(kind, terms)

    # Publication needs serialization, not permission to UPDATE merchant identity.
    # A transaction lock also covers the first version, before any version row exists.
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"merchant-policy:{ctx.tenant_id}:{ctx.merchant_id}"},
    )
    locked = session.execute(
        select(Merchant.id).where(
            Merchant.tenant_id == ctx.tenant_id, Merchant.id == ctx.merchant_id
        )
    ).scalar_one_or_none()
    if locked is None:
        raise ProblemError(404, "Merchant not found", "No such merchant in this tenant.")

    highest = session.execute(
        select(func.max(MerchantPolicyVersion.version)).where(
            MerchantPolicyVersion.tenant_id == ctx.tenant_id,
            MerchantPolicyVersion.merchant_id == ctx.merchant_id,
        )
    ).scalar_one_or_none()
    current = current_policy(session, tenant_id=ctx.tenant_id, merchant_id=ctx.merchant_id)

    if highest is None:
        # The first change is also when the opening position gets written down. Until now
        # every sale was made under the same unnumbered terms and a row saying so would have
        # distinguished nothing; from here on there is a change, and a change needs
        # something to be a change *from*. Orders sold earlier name version one, and now
        # version one is a row somebody auditing them can actually read.
        session.add(
            MerchantPolicyVersion(
                id=uuid7(),
                tenant_id=ctx.tenant_id,
                merchant_id=ctx.merchant_id,
                version=OPENING_VERSION,
                terms={family: dict(values) for family, values in DEFAULT_TERMS.items()},
                action_id=None,
                published_by=OPENING_AUTHOR,
            )
        )
        highest = OPENING_VERSION

    composed = {family: dict(values) for family, values in current.terms.items()}
    composed[kind] = dict(terms)

    row = MerchantPolicyVersion(
        id=uuid7(),
        tenant_id=ctx.tenant_id,
        merchant_id=ctx.merchant_id,
        version=highest + 1,
        terms=composed,
        action_id=action_id,
        published_by=ctx.principal.principal_id,
    )
    session.add(row)
    session.flush()
    return PublishedPolicy(
        version=row.version,
        terms=composed,
        chosen=True,
        published_by=str(row.published_by),
    )


def kind_of(target: str) -> str:
    """Read a policy family off an action's target, or refuse by name.

    A merchant action names one thing it changes, and for a policy publication that thing is
    the family. Validating here rather than at the route means the same refusal reaches a
    caller whether they came through HTTP or through the approval path.
    """
    family = target.strip().upper()
    if family not in PUBLISHABLE_KINDS:
        raise ProblemError(
            422,
            "That is not a policy family a merchant publishes",
            f"Name one of {', '.join(sorted(PUBLISHABLE_KINDS))} as the target.",
            requested=target,
            allowed=sorted(PUBLISHABLE_KINDS),
        )
    return family
