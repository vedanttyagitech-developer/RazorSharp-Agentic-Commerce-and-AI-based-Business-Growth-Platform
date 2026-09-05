"""The airline as the kernel sees it: a state source and a Policy-at-Sale Receipt.

WHAT IS NOT HERE, AND WHY THAT IS THE RESULT
--------------------------------------------
There is no ``FlightMerchantStateSource``. :class:`merchant_sim.SimMerchantStateSource`
already satisfies admission step 8 for this vertical, unmodified, because what it does is
re-read the approved version's lines, re-quote those exact SKUs and quantities against the
store *now*, and report the total, the availability and the canonical content. Nothing in
that sentence is about groceries. For an airline it reads: re-read the approved fares and
passenger counts, re-price them against the fare buckets now, and report whether the fare
moved and whether the seats are still there.

So this module supplies the one genuinely airline-shaped thing -- the merchant's published
rules -- and delegates the rest. :func:`state_source_for` exists so callers have a single
obvious entry point and so the policy version stamped into content says ``indigo-sim``
rather than the grocery store's.

WHY THE FARE FAMILY IS A RECEIPT POLICY
---------------------------------------
For a grocery order the interesting policies are delivery and refund. For an airline the
interesting policy *is the product*: Lite carries no check-in bag and standard change
charges, UpFront carries twenty kilos and no change fee beyond 72 hours, and those
differences are the entire reason one costs 67% more than the other.

A Policy-at-Sale Receipt freezes the rules in force at the moment of sale. That matters
more for an airline than for a grocer, because the gap between buying and consuming is
weeks rather than minutes, and an airline that quietly re-tiers its fare families in the
meantime would otherwise be arguing about the buyer's rights from a document written after
the buyer paid. :func:`receipt_inputs_for` therefore records every fare family's baggage,
change and cancellation terms, per family, inside the receipt.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from commerce_domain import Money, canonical_hash
from merchant_sim import FeePolicy, MerchantStore, SimMerchantStateSource
from transaction_kernel.checkouts import ReceiptInputs
from transaction_kernel.receipts import BuyerVisibleRef, MerchantPolicy, PolicyKind

from .domain import CURRENCY
from .fares import CONVENIENCE_FEE_WAIVER_MINOR, FAMILIES

__all__ = [
    "FLIGHT_POLICY_PREFIX",
    "FLIGHT_POLICY_VERSION",
    "FLIGHT_ROUNDING_POLICY_VERSION",
    "FLIGHT_SOURCE_ID",
    "FLIGHT_TAX_POLICY_VERSION",
    "receipt_inputs_for",
    "state_source_for",
]

#: Provenance stamp on every content document this merchant produces. Distinct from the
#: grocery simulator's so a proof chain says which merchant priced the sale.
FLIGHT_SOURCE_ID: Final[str] = "indigo-sim"

#: The merchant-policy version reported to the kernel and stamped into content.
FLIGHT_POLICY_VERSION: Final[str] = "indigo-sim-1"

#: Policy document ids are ``<prefix>/<kind>``.
FLIGHT_POLICY_PREFIX: Final[str] = "indigo-flights"

#: Named arithmetic rules, so a receipt says which rounding produced the paisa the buyer
#: agreed to. The rounding rule is deliberately the same one the grocery store uses: one
#: rule, one implementation, one reproducible total.
FLIGHT_TAX_POLICY_VERSION: Final[str] = "indigo-airfare-gst-udf-asf/1"
FLIGHT_ROUNDING_POLICY_VERSION: Final[str] = "half-up-per-line/1"


def state_source_for(store: MerchantStore) -> SimMerchantStateSource:
    """What the kernel calls at admission step 8 for this airline.

    The grocery simulator's state source over an airline's store. That is not a shortcut:
    re-quoting the approved lines against current state is the whole of step 8, and it is
    vertical-neutral by construction. A fare that moved between approval and submission
    produces a different total and a different content hash, and the kernel refuses the
    stale approval -- which is the demonstration this vertical existed to give.
    """
    return SimMerchantStateSource(
        store, policy_version=FLIGHT_POLICY_VERSION, source_id=FLIGHT_SOURCE_ID
    )


def _fare_family_terms() -> dict[str, Any]:
    """Every fare family's rights, keyed by code, as integer-only JSON terms.

    Recorded per family rather than as prose because a resolution has to be able to *read*
    them: "did this ticket include a check-in bag" must be answerable from the receipt
    without parsing a sentence.
    """
    return {
        fam.code: {
            "label": fam.label,
            "cabin_bag_kg": fam.cabin_bag_kg,
            "checkin_bag_kg": fam.checkin_bag_kg,
            "checkin_bag_included": fam.checkin_bag_kg > 0,
            "change_terms": fam.change_policy,
            "cancellation_terms": fam.cancellation_policy,
            "perks": ", ".join(fam.perks) if fam.perks else "none",
            "fare_multiplier_bp": fam.multiplier_bp,
        }
        for fam in FAMILIES
    }


def _policies(fee: FeePolicy, *, prefix: str, version: int) -> tuple[MerchantPolicy, ...]:
    """One policy per :class:`PolicyKind`. Every kind present, including the refusals.

    The kernel requires all six. Where an airline has no programme -- it runs no discounts
    on a base fare in this fixture -- the answer is recorded as ``allowed: false`` rather
    than omitted, because an omitted kind gets filled from the merchant's *current* policy
    at resolution time, which is the retroactive change a receipt exists to prevent.
    """
    return (
        MerchantPolicy(
            kind=PolicyKind.CANCELLATION,
            policy_id=f"{prefix}/cancellation",
            policy_version=version,
            terms={
                "allowed": True,
                "cutoff": "BEFORE_DEPARTURE",
                "cutoff_hours_before_departure": 2,
                "charges_depend_on_fare_family": True,
                "by_family": _fare_family_terms(),
                "statutory_charges_non_refundable": True,
            },
        ),
        MerchantPolicy(
            kind=PolicyKind.REFUND,
            policy_id=f"{prefix}/refund",
            policy_version=version,
            terms={
                "allowed": True,
                "window_days": 365,
                "method": "ORIGINAL_INSTRUMENT",
                "partial_allowed": True,
                # The site's own disclaimer, recorded because it is the term a buyer is
                # most often surprised by after a cancellation.
                "convenience_fee_refundable": False,
            },
        ),
        MerchantPolicy(
            kind=PolicyKind.SUBSTITUTION,
            policy_id=f"{prefix}/substitution",
            policy_version=version,
            terms={
                # For an airline, substitution is a flight change. It is allowed, and what
                # it costs is exactly what the fare family bought.
                "allowed": True,
                "form": "FLIGHT_CHANGE",
                "fare_difference_payable": True,
                "charges_depend_on_fare_family": True,
                "by_family": _fare_family_terms(),
            },
        ),
        MerchantPolicy(
            kind=PolicyKind.DELIVERY,
            policy_id=f"{prefix}/delivery",
            policy_version=version,
            terms={
                # The non-item charge added at checkout, whose amount is frozen here for
                # the same reason the grocery delivery fee is: a fee injection between two
                # receipts must produce two different receipt hashes.
                "charge": "CONVENIENCE_FEE",
                "base_fee": fee.base_delivery_fee,
                "waived_above": fee.free_delivery_threshold,
                "fee_tax_bp": fee.delivery_tax_bp,
                "threshold_basis": "PRE_TAX_FARE_INCLUSIVE",
                "non_refundable": True,
                "ticket_delivery": "E_TICKET_EMAIL",
            },
        ),
        MerchantPolicy(
            kind=PolicyKind.DISCOUNT,
            policy_id=f"{prefix}/discount",
            policy_version=version,
            terms={"allowed": False, "reason": "this fixture files published fares only"},
        ),
        MerchantPolicy(
            kind=PolicyKind.FULFILMENT,
            policy_id=f"{prefix}/fulfilment",
            policy_version=version,
            terms={
                "mode": "SCHEDULED_AIR_CARRIAGE",
                "carrier": "6E",
                "checkin_opens_hours_before": 48,
                "checkin_closes_minutes_before": 60,
                "boarding_closes_minutes_before": 25,
                "seat_held_until_ticketed": True,
            },
        ),
    )


def receipt_inputs_for(
    source: MerchantStore | FeePolicy,
    *,
    policy_version: int = 1,
    policy_prefix: str = FLIGHT_POLICY_PREFIX,
    policy_uri: str = "https://indigo-sim.invalid/conditions-of-carriage",
) -> ReceiptInputs:
    """The airline's rules as receipt inputs, one policy per kind.

    The convenience-fee terms are copied from the fee policy *in force*, so a fee injection
    between two sales produces two different receipt hashes: the receipt records the fee
    the buyer was shown. The buyer-visible reference hashes the rendered policy set, so
    what the buyer could have read is provable later even though this fixture publishes no
    conditions-of-carriage page.
    """
    fee = source.fee_policy if isinstance(source, MerchantStore) else source
    if fee.currency != CURRENCY:
        raise ValueError(f"this airline tickets in {CURRENCY}, not {fee.currency}")
    policies = _policies(fee, prefix=policy_prefix, version=policy_version)
    rendered: Sequence[Mapping[str, Any]] = [policy.as_content() for policy in policies]
    return ReceiptInputs(
        policies=policies,
        tax_policy_version=FLIGHT_TAX_POLICY_VERSION,
        rounding_policy_version=FLIGHT_ROUNDING_POLICY_VERSION,
        buyer_visible_refs=(
            BuyerVisibleRef(
                label="IndiGo conditions of carriage and fare rules",
                uri=policy_uri,
                text_hash=str(canonical_hash(list(rendered))),
            ),
        ),
    )


# The waiver threshold is buyer-visible on the approval card via the DELIVERY policy, so a
# nonsense value would be recorded into every receipt. Checked at import.
assert CONVENIENCE_FEE_WAIVER_MINOR > 0, "a convenience-fee waiver threshold must be positive"
assert Money(CONVENIENCE_FEE_WAIVER_MINOR, CURRENCY).minor == CONVENIENCE_FEE_WAIVER_MINOR
