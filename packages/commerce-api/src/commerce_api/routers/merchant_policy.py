"""What a merchant currently promises, and what they are allowed to promise about.

The write side of this was built first and has been reachable for a while: a policy change
is a merchant action like any other -- proposed, agreed to by name against an exact digest,
and only then carried out -- so it needed no route of its own. The read side had nothing at
all, which left the merchant plane in the odd position of being able to change terms it
could not display. A shop that cannot see its own returns policy cannot decide whether to
withdraw it.

**Owned by the merchant surface, and deliberately not by the buyer's.** A buyer reads the
terms of *their* sale, frozen onto it, through ``GET /v1/orders/{id}/policy``; that read
resolves through the Policy-at-Sale Receipt and has no path to this table. This one is the
opposite: the shop's current position, which governs sales not yet made and no sale already
made. Keeping them apart in two routes rather than one with a flag is what makes the second
half of that sentence true by construction.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from merchant_adapter import PUBLISHABLE_KINDS
from pydantic import BaseModel, ConfigDict

from ..deps import AppSession, SessionContext, require_scenario_key
from ..services.merchant_policy_service import current_policy

router = APIRouter(
    prefix="/v1/merchant/policy",
    tags=["merchant"],
    dependencies=[Depends(require_scenario_key)],
)

__all__ = ["router"]


class PolicyOut(BaseModel):
    """The terms in force for this merchant, and which version they are.

    ``chosen`` is false while nothing has been published and the shop is still on its
    opening position. The distinction is worth a field: "nobody has decided this yet" and
    "somebody decided this and chose these values" look identical in the terms themselves,
    and only one of them is a position anybody can be held to.
    """

    model_config = ConfigDict(extra="forbid")

    version: int
    chosen: bool
    #: Who published it. The opening position is attributed to the platform, never to the
    #: merchant, because nobody agreed to terms they were merely started with.
    published_by: str
    terms: dict[str, dict[str, Any]]
    #: The families this merchant may publish. Sent rather than hardcoded in the console,
    #: so a screen cannot offer a control for a family the server would refuse.
    publishable: list[str]


@router.get("", response_model=PolicyOut, summary="The terms this shop currently promises")
def read_policy(ctx: SessionContext, session: AppSession) -> PolicyOut:
    """The merchant's current position, which is not what any finished sale was made under.

    A read on the app role. Publishing runs through the action path and its own grants;
    nothing here can write, which is the ordinary shape of every read on this surface.
    """
    ctx.require("policy.search")
    policy = current_policy(session, tenant_id=ctx.tenant_id, merchant_id=ctx.merchant_id)
    return PolicyOut(
        version=policy.version,
        chosen=policy.chosen,
        published_by=policy.published_by,
        terms={family: dict(values) for family, values in policy.terms.items()},
        publishable=sorted(PUBLISHABLE_KINDS),
    )
