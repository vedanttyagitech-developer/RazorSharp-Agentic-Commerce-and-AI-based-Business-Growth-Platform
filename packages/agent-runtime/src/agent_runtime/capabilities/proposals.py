"""The growth proposal record, written once for every half of the platform that emits one.

Two different runners answer a merchant on this platform. The agent runtime's Growth
Specialist holds ``growth_proposal_create`` as a tool, and the deterministic runner in
``commerce_api`` answers when no model is configured -- which, on a developer's machine
and in the demo, is always. Both must put the *same record* on the wire, because one
console parses it, and a console that parses ``change.body`` cannot be told that one half
of the platform spells it ``payload``.

So the contract lives here rather than in either producer: the lever vocabulary, the
evidence keys, the id derivation and the body a restock proposal names. Each producer
reads the merchant its own way -- the tool through a :class:`MerchantBackend`, the
deterministic runner through the payloads its executor already returned -- and then hands
what it read to the same assembler. The prose a merchant reads and the change a merchant
applies are therefore written in one place, and a disagreement between the halves is a
type error here instead of an empty card three weeks from now.

Nothing in this module performs a change. :data:`PROPOSAL_APPLY_ENDPOINT` is a string on a
record and ``applied`` is hard-coded false, because specification 6.6 puts price, stock,
discount, fee, campaign budget, refund rule and financial authority outside what an agent
proposal may move. A person applies it on the merchant console, or it does not happen.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from commerce_domain import canonical_hash

from ..backends.memory import LOW_STOCK_UNITS
from ..rendering.cards import PROPOSAL_WHERE
from .registry import Capability

__all__ = [
    "ANOMALY_DELISTED_WITH_STOCK",
    "ANOMALY_KINDS",
    "ANOMALY_LISTED_OUT_OF_STOCK",
    "ANOMALY_LOW_STOCK",
    "GATE_PROPOSAL_GUARDRAILS",
    "GROWTH_LEVERS",
    "LEVER_CATALOGUE_DISCOVERABILITY",
    "LEVER_CHECKOUT_CONFIGURATION",
    "LEVER_ROWS",
    "LEVER_TOP_SELLER_OUT_OF_STOCK",
    "MERCHANT_DATA_IS_SYNTHETIC",
    "PROPOSAL_APPLY_ENDPOINT",
    "PROPOSAL_ID_PREFIX",
    "PROPOSAL_WHERE",
    "REQUIRED_EVIDENCE_KEYS",
    "REQUIRED_PROPOSAL_KEYS",
    "RESTOCK_FLOOR_UNITS",
    "REVIEW_ONLY_KIND",
    "SOURCE_CATALOGUE",
    "SOURCE_COMMITTED_ROWS",
    "WINDOW_ALL_TIME",
    "WINDOW_CATALOGUE_NOW",
    "LeverRow",
    "ProposalDraft",
    "proposal_id",
    "proposal_record",
    "restock_change",
    "restock_draft",
    "subject_record",
]


# ------------------------------------------------------------------- the closed vocabulary

#: The growth levers this platform can evidence, spelled as specification 9.1's rows. Three
#: of eighteen, because a lever is supported here only when the merchant reads can actually
#: ground it. A proposal the platform cannot trace to a tool result is advice, and advice is
#: the one thing a merchant can already get without a platform.
LEVER_TOP_SELLER_OUT_OF_STOCK: Final[str] = "top_seller_out_of_stock"
LEVER_CATALOGUE_DISCOVERABILITY: Final[str] = "catalogue_discoverability_health"
LEVER_CHECKOUT_CONFIGURATION: Final[str] = "checkout_configuration_analysis"
GROWTH_LEVERS: Final[tuple[str, ...]] = (
    LEVER_TOP_SELLER_OUT_OF_STOCK,
    LEVER_CATALOGUE_DISCOVERABILITY,
    LEVER_CHECKOUT_CONFIGURATION,
)

#: The gate name a refused proposal reports, matching the row in ``specialists/_spec.py``.
#: Spelled again rather than imported because that table is deliberately data -- strings, so
#: a spec can be tested without importing an implementation -- and a test pins the two
#: spellings equal.
GATE_PROPOSAL_GUARDRAILS: Final[str] = "proposal_guardrails"

#: Prefix and length of a proposal id. The id is a hash of the proposal's own inputs, so an
#: id that still matches is a proposal whose evidence has not moved; 22 base64url characters
#: is 128 bits, which is far past collision for a record this short-lived.
PROPOSAL_ID_PREFIX: Final[str] = "prp_"
PROPOSAL_ID_CHARS: Final[int] = 22

#: The only endpoint a proposal ever names. Nothing in either producer calls it: the string
#: is data on a record, and the merchant console posts it when a person presses apply.
#: Naming one endpoint rather than composing one per lever is what lets the console refuse a
#: proposal that names anything else instead of following it.
PROPOSAL_APPLY_ENDPOINT: Final[str] = "POST /v1/scenario/injections"

#: Whether this platform's merchant data is simulated. True, and a constant rather than a
#: derived flag, because in P0 there is no other kind: the catalogue is the merchant
#: simulator's and the traffic is controlled scenario (specification 9.3). Deriving it from
#: the backend class would be worse than stating it -- an API-backed run talks to a demo
#: tenant seeded by the same simulator, so the derived answer would read "real" about
#: synthetic data. The day a production merchant exists this constant is the one place that
#: has to change, and 6.6 requires the flag on every recommendation.
MERCHANT_DATA_IS_SYNTHETIC: Final[bool] = True

#: The stock level a restock proposal names. One unit past the platform's own low-stock
#: threshold: the smallest level at which its inventory diagnostic stops reporting the
#: product at all. It is a floor and not a forecast, and the distinction is the whole point
#: -- nothing on this surface counts demand per SKU, so any figure chosen for how much a
#: merchant will *sell* would be invented, and a merchant reading it on a card would take it
#: as counted.
RESTOCK_FLOOR_UNITS: Final[int] = LOW_STOCK_UNITS + 1

#: What a proposal's ``window`` says. The catalogue reads are a snapshot of current state
#: and the order counts span every committed row; neither narrows by time, and saying so in
#: two accurate phrases beats one tidy phrase that is wrong about half of them.
WINDOW_CATALOGUE_NOW: Final[str] = "current catalogue state"
WINDOW_ALL_TIME: Final[str] = "all-time"

#: Where a proposal's figures came from. These are the two strings the metrics cards already
#: use, and they are deliberately not the merchant simulator's provenance id: the merchant
#: reads carry no ``Provenance.source`` of their own, so asserting one would claim a
#: provenance the backend never sent.
SOURCE_CATALOGUE: Final[str] = "merchant_catalogue"
SOURCE_COMMITTED_ROWS: Final[str] = "platform_committed_rows"

#: The backend's closed anomaly vocabulary, reproduced whole rather than in the part this
#: module happens to need. Two of the three ground a proposal; ``ANOMALY_LOW_STOCK`` grounds
#: none -- there is nothing to restock on a shelf that still has units and nothing to relist
#: on a product already listed -- and it is here anyway, because a closed set spelled in two
#: places is how one of its members comes to be spelled two ways. The tool factory, the
#: in-memory backend and the API bridge all read these.
ANOMALY_LISTED_OUT_OF_STOCK: Final[str] = "listed_out_of_stock"
ANOMALY_DELISTED_WITH_STOCK: Final[str] = "delisted_with_stock"
ANOMALY_LOW_STOCK: Final[str] = "low_stock"

#: Every kind :class:`~agent_runtime.backends.base.InventoryAnomaly` may carry. A backend
#: reporting a fourth is a contract violation, not an unusual anomaly.
ANOMALY_KINDS: Final[frozenset[str]] = frozenset(
    {ANOMALY_LISTED_OUT_OF_STOCK, ANOMALY_DELISTED_WITH_STOCK, ANOMALY_LOW_STOCK}
)

#: The body kind of a proposal that names no operation. The funnel lever is gated on
#: "controlled scenarios and human-reviewed proposals" and this platform has no endpoint that
#: would run one, so the record carries its counts and says plainly that there is nothing to
#: press. A body naming a real injection kind with a value nobody derived would be worse: it
#: would put an apply button on a number the platform invented.
REVIEW_ONLY_KIND: Final[str] = "REVIEW_ONLY"

#: The keys the merchant console's parser requires of a proposal and of its evidence. They
#: are named here, as data, so that a test on either producer can assert the record it built
#: carries them without restating the list and drifting from it.
REQUIRED_PROPOSAL_KEYS: Final[frozenset[str]] = frozenset(
    {
        "kind",
        "proposal_id",
        "lever",
        "title",
        "rationale",
        "metric",
        "gate",
        "evidence",
        "change",
        "applied",
    }
)
REQUIRED_EVIDENCE_KEYS: Final[frozenset[str]] = frozenset({"source", "synthetic", "read_by"})


@dataclass(frozen=True, slots=True)
class LeverRow:
    """One row of specification 9.1, as the closed vocabulary a proposal reproduces.

    ``metric`` is that table's Measurement column and ``gate`` its Deterministic gate
    column, taken verbatim rather than paraphrased: the words are the contract between a
    proposal and the specification it claims to satisfy, and a rewording is how a record
    starts describing something the spec never sanctioned. ``read_by`` names the
    capabilities the evidence rests on, which is the claim 6.6 requires a recommendation to
    make about itself.
    """

    metric: str
    gate: str
    read_by: tuple[str, ...]


LEVER_ROWS: Final[Mapping[str, LeverRow]] = {
    LEVER_TOP_SELLER_OUT_OF_STOCK: LeverRow(
        metric="failures attributable to stock",
        gate="authoritative inventory and human-applied operational proposal",
        read_by=(
            Capability.MERCHANT_INVENTORY_ANOMALIES_READ.value,
            Capability.MERCHANT_CATALOGUE_HEALTH_READ.value,
        ),
    ),
    LEVER_CATALOGUE_DISCOVERABILITY: LeverRow(
        metric="search misses due to missing attributes",
        gate="grounded catalogue diagnostics",
        read_by=(
            Capability.MERCHANT_INVENTORY_ANOMALIES_READ.value,
            Capability.MERCHANT_CATALOGUE_HEALTH_READ.value,
        ),
    ),
    LEVER_CHECKOUT_CONFIGURATION: LeverRow(
        metric="failure rate by fee/slot/policy configuration",
        gate="controlled scenarios and human-reviewed proposals",
        read_by=(Capability.MERCHANT_CHECKOUT_METRICS_READ.value,),
    ),
}


# ------------------------------------------------------------------------- the draft


@dataclass(frozen=True, slots=True)
class ProposalDraft:
    """One lever's reading of the merchant, before it becomes a proposal record.

    Everything here was counted by the platform. ``figures`` holds the counts the record
    cites, ``not_measured`` names the figures a lever would want and this platform does not
    have, and ``rows`` is the card's business alone -- :func:`proposal_record` ignores it,
    because what a person is asked to apply must not depend on how it was drawn.
    """

    subject: dict[str, Any] | None
    title: str
    rationale: str
    figures: dict[str, Any]
    source: str
    window: str
    sample_size: int
    catalogue_revision: int | None
    change: dict[str, Any]
    rows: list[dict[str, Any]] = field(default_factory=list)
    money: dict[str, dict[str, Any]] | None = None
    not_measured: tuple[str, ...] = ()


def subject_record(
    *, sku: str, merchant_text: str, quarantined: bool, basis: str
) -> dict[str, Any]:
    """The product a proposal is about: its id, and its merchant-authored name, carried apart.

    The name travels beside the SKU and never inside the title or the rationale. A product
    name is text the merchant wrote and a title is the platform speaking, so the two must
    not be the same sentence; it is also why the name is deliberately absent from the inputs
    the proposal id is derived from, since renaming a product does not change what its shelf
    says about it.
    """
    return {
        "sku": sku,
        "merchant_text": merchant_text,
        "quarantined": quarantined,
        "safe_label": f"catalogue item {sku}",
        "basis": basis,
    }


def restock_change(*, sku: str, to_units: int, from_units: int | None) -> dict[str, Any]:
    """The change a restock proposal names, as data a person sends.

    ``reversible`` is true exactly when the platform observed the level to reverse *to*. A
    record that promised reversibility without naming the state it would restore would be
    asking a merchant to trust an undo nobody wrote down.
    """
    return {
        "endpoint": PROPOSAL_APPLY_ENDPOINT,
        "body": {"kind": "STOCK_SET", "sku": sku, "value": to_units},
        "reversible": from_units is not None,
        "reverses_to": (
            None if from_units is None else {"kind": "STOCK_SET", "sku": sku, "value": from_units}
        ),
    }


def _shelf_phrase(units: int | None) -> str:
    return f"{units} units on hand" if units is not None else "no unit count returned"


def restock_draft(
    *,
    subject: dict[str, Any],
    subject_label: str,
    stock_units: int | None,
    catalogue_total: int,
    catalogue_revision: int | None,
    listed_with_no_stock: int | None,
    rows_in_this_state: int,
) -> ProposalDraft:
    """A product listed for sale with an empty shelf, and the floor that ends that.

    The lever is 9.1's "Top-seller out-of-stock alert" and the title deliberately does not
    say "top seller". Nothing on this surface ranks products by sales, so calling one a top
    seller would be inventing the one fact that makes the lever urgent. What the platform can
    say is that the product is listed and has nothing behind it, which is what the record
    says.

    Both producers call this, which is the point: the sentence a merchant reads and the body
    a merchant applies are written once, so the deterministic runner and the agent runtime
    cannot drift into proposing two different things about the same shelf.
    """
    sku = str(subject["sku"])
    return ProposalDraft(
        subject=subject,
        title=f"Restock {sku}",
        rationale=(
            f"{sku} is listed for sale with {_shelf_phrase(stock_units)}, so every buyer "
            f"who reaches it fails at the shelf. The proposed level of {RESTOCK_FLOOR_UNITS} "
            "units is the lowest at which this platform's inventory diagnostic stops "
            "reporting the product; nothing here counts demand per product, so a larger "
            "figure would be a forecast rather than a count."
        ),
        figures={
            "catalogue_total": catalogue_total,
            "breakdown_is_whole": listed_with_no_stock is not None,
            "listed_with_no_stock": listed_with_no_stock,
            "rows_in_this_state": rows_in_this_state,
            "subject_stock_units": stock_units,
            "proposed_stock_units": RESTOCK_FLOOR_UNITS,
        },
        source=SOURCE_CATALOGUE,
        window=WINDOW_CATALOGUE_NOW,
        sample_size=catalogue_total,
        catalogue_revision=catalogue_revision,
        change=restock_change(sku=sku, to_units=RESTOCK_FLOOR_UNITS, from_units=stock_units),
        rows=[
            {"label": subject_label, "ref": sku, "count": stock_units, "basis": subject["basis"]},
            {
                "label": "Proposed stock level",
                "count": RESTOCK_FLOOR_UNITS,
                "basis": "inventory_diagnostic_floor",
            },
            {
                "label": "Listed, no stock",
                "count": listed_with_no_stock,
                "basis": "whole_catalogue",
            },
            {
                "label": "Products in catalogue",
                "count": catalogue_total,
                "basis": "whole_catalogue",
            },
        ],
    )


# ------------------------------------------------------------------------ the record


def proposal_id(
    lever: str, subject_sku: str | None, evidence: Mapping[str, Any], change: Mapping[str, Any]
) -> str:
    """An id derived from the proposal's own inputs, so it moves only when they do.

    The same reasoning the resolution service applies to a plan id: an id that still matches
    is a record whose evidence has not moved underneath it, and a later apply can be checked
    against the id rather than against a description of what was proposed. The merchant's own
    text is deliberately not an input -- renaming a product does not change what its shelf
    says -- and neither is anything derived from those inputs, since a title that changed
    without a figure changing would be a rewording pretending to be a fact.

    Hashing through the canonical JSON encoder is a guard as well as an encoding: that
    encoder refuses a float, so a proposal carrying one cannot be given an identity at all.
    """
    identity = {
        "lever": lever,
        "subject_sku": subject_sku,
        "source": evidence["source"],
        "window": evidence["window"],
        "sample_size": evidence["sample_size"],
        "catalogue_revision": evidence["catalogue_revision"],
        "read_by": list(evidence["read_by"]),
        "figures": evidence["figures"],
        "change": dict(change),
    }
    return PROPOSAL_ID_PREFIX + canonical_hash(identity)[:PROPOSAL_ID_CHARS]


def proposal_evidence(lever: str, draft: ProposalDraft) -> dict[str, Any]:
    """What the record says about where its figures came from.

    ``synthetic`` is stated rather than inferred and ``read_by`` names the capabilities the
    figures rest on, because those are the two 6.6 makes non-negotiable: a recommendation
    drawn from a simulated catalogue that does not say so is the defect this whole surface
    was rebuilt to remove, and a proposal that cannot name a tool has no evidence at all.
    """
    return {
        "source": draft.source,
        "window": draft.window,
        "sample_size": draft.sample_size,
        "synthetic": MERCHANT_DATA_IS_SYNTHETIC,
        "catalogue_revision": draft.catalogue_revision,
        "read_by": list(LEVER_ROWS[lever].read_by),
        "figures": draft.figures,
        "subject": draft.subject,
        "not_measured": list(draft.not_measured),
    }


def proposal_record(lever: str, draft: ProposalDraft) -> dict[str, Any]:
    """The proposal, as it goes on the wire from either half of the platform.

    ``applied`` is false here and is not a parameter, which is the same guarantee the card
    renderer makes about authority: there is no argument through which a caller could mark a
    proposal applied, so there is nothing to validate away afterwards. Only the merchant
    console, acting for a person who pressed apply, has anything to say about that field.
    """
    evidence = proposal_evidence(lever, draft)
    subject_sku = None if draft.subject is None else str(draft.subject["sku"])
    row = LEVER_ROWS[lever]
    record: dict[str, Any] = {
        "kind": "proposal",
        "proposal_id": proposal_id(lever, subject_sku, evidence, draft.change),
        "lever": lever,
        "title": draft.title,
        "rationale": draft.rationale,
        "metric": row.metric,
        "gate": row.gate,
        "evidence": evidence,
        "change": draft.change,
        "applied": False,
        "where": PROPOSAL_WHERE,
    }
    if draft.money is not None:
        record["money"] = draft.money
    return record
