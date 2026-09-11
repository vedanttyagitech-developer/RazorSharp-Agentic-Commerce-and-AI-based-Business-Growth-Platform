"""Operations Specialist: the merchant's own copilot, and the first of them.

The buyer's three specialists share one shape -- read the shop, propose, never commit.
This one is the same shape pointed the other way: it reads the merchant's own records
(confirmed sales, the shelf, the action queue, the support queue) and drafts a change for
the person who owns the shop to approve.

WHAT MAKES IT WORTH HAVING AS AN AGENT AT ALL
---------------------------------------------
Not the conversation. `merchant.action.propose` and `merchant.action.approve` are two
different capabilities in the merchant's own registry, and this specialist holds exactly
the first. It can draft a restock that names a SKU, a current level and a target; it
cannot make that restock happen, and no widening of its principal can give it the verb --
`derive_principal` intersects, and a capability absent from the role's allowlist is absent
however wide the harness above it is. The merchant approves on their own surface or the
draft stays a draft.

That is the buyer-side story told on the merchant side. `checkout.approve` exists in no
agent registry; neither does `merchant.action.approve`. The two absences are the same
claim, and until this specialist existed the second one was asserted in a document and
demonstrated nowhere.

WHAT IS ENFORCED ELSEWHERE AND THEREFORE NOT ASKED OF THE PROMPT
---------------------------------------------------------------
`merchant_propose_action` runs the SKU provenance gate, so a draft naming a product no
tool returned this turn is refused before the backend sees it -- the model cannot invent a
SKU and propose against it. Every figure the reply states must appear in a tool result
from this turn (`grounding/postcheck.py`), which is what stops a revenue number being
produced rather than read. Merchant-authored catalogue text arrives fenced
(`Surface.MERCHANT`: on this side the untrusted party is the buyer text and anything
pasted in, not the shop's own words).
"""

from __future__ import annotations

from typing import Final

from ..core.grounding_rules import OPERATIONS_RULES
from ._spec import SpecialistSpec, Surface

__all__ = ["SPEC"]

_FALLBACK: Final[str] = """\
You are the Operations Specialist for a shop owner. You read this shop's own records and
help its owner decide what to do next. Be direct and practical. Lead with the number, then
what you would do about it.

Tools you may call: merchant.insights.read, catalog.search, catalog.get_product,
merchant.action.read, support.case.read, merchant.action.propose.

You propose; you never approve. merchant.action.propose records a DRAFT that changes
nothing -- no price moves, no stock moves, no policy changes -- and the owner approves it
on their own screen. Say a change is drafted and waiting for them, never that it is done.
You cannot approve it yourself and you must not imply otherwise.

Numbers: state only figures that appear in a tool result this turn, copied exactly. Read
before you answer. merchant.insights.read returns confirmed order value with its own
definition of what that figure is and is not; carry that definition when you quote the
number, and do not turn it into growth, profit or a percentage -- the platform does not
measure those and neither do you. Never compute a trend from a single period.

Stock: only from catalog.search or catalog.get_product. Before drafting a restock, read
the product, so the draft carries the level you actually saw rather than one you assumed.

Reply in the merchant's language. After answering, ask one short next-step question and
wait.
"""

SPEC: Final[SpecialistSpec] = SpecialistSpec(
    name="operations_specialist",
    role="operations",
    surface=Surface.MERCHANT,
    description="Read this shop's records and draft changes for its owner to approve.",
    actions=(
        "merchant.insights.read",
        "catalog.search",
        "catalog.get_product",
        "merchant.action.read",
        "support.case.read",
        "merchant.action.propose",
    ),
    rules=OPERATIONS_RULES,
    # No cards. The merchant workspace renders its own tables from its own reads, and a
    # second rendering of the same rows -- selected by a model, out of a turn ledger --
    # would be a second source of truth for figures the panels already show.
    cards=(),
    fallback_instruction=_FALLBACK,
)
