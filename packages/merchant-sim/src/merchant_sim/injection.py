"""Scenario injection records: the only description of a change to merchant state.

Specification 31.3: every demo injection is labelled ``SCENARIO_INJECTION`` in the audit
and is never mixed with organic production data.

WHY THE LABEL IS STRUCTURAL, NOT A CONVENTION
---------------------------------------------
The obvious design is a controller that mutates state and *also* writes a log line saying
it did. That design fails the moment someone adds a mutator and forgets the log line, and
the failure is invisible: the demo now contains an unexplained stock-out that looks exactly
like a real one. During a live panel demo, the difference between "we injected this" and
"our inventory service broke" is the entire credibility of the system.

So the injection record is not a description of a mutation that already happened -- it *is*
the mutation. :meth:`~merchant_sim.store.MerchantStore.mutate` takes one of these and
nothing else, and this class refuses to exist with any label but ``SCENARIO_INJECTION``.
There is therefore no code path that changes merchant state without producing a labelled,
appended audit record.

Every field is an integer, string or bool so that :func:`commerce_domain.canonical_hash`
can hash the audit payload under the integer-only JCS profile.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from .errors import ScenarioError

__all__ = [
    "SCENARIO_LABEL",
    "InjectionKind",
    "ScenarioInjection",
    "StateDelta",
    "expected_field_for",
]

#: The one permitted label. Audit consumers filter on this exact string to separate demo
#: state from organic state; it is never abbreviated, translated or lower-cased.
SCENARIO_LABEL: Final[str] = "SCENARIO_INJECTION"


class InjectionKind(StrEnum):
    """What a scenario injection changes. Closed set, like RecoveryCode: a new demo lever
    is a reviewed addition here, never an ad-hoc string."""

    STOCK_SET = "STOCK_SET"
    STOCK_DECREMENT = "STOCK_DECREMENT"
    PRICE_SET = "PRICE_SET"
    AVAILABILITY_SET = "AVAILABILITY_SET"
    DELIVERY_FEE_SET = "DELIVERY_FEE_SET"
    FREE_DELIVERY_THRESHOLD_SET = "FREE_DELIVERY_THRESHOLD_SET"
    OFFER_START = "OFFER_START"
    OFFER_END = "OFFER_END"
    CATALOGUE_RESET = "CATALOGUE_RESET"


#: Which state field each kind is allowed to touch. The store validates against this, so a
#: PRICE_SET carrying a stock delta is refused instead of quietly writing the wrong field.
_FIELD_FOR_KIND: Final[dict[InjectionKind, str]] = {
    InjectionKind.STOCK_SET: "stock_units",
    InjectionKind.STOCK_DECREMENT: "stock_units",
    InjectionKind.PRICE_SET: "unit_price_minor",
    InjectionKind.AVAILABILITY_SET: "available",
    InjectionKind.DELIVERY_FEE_SET: "base_delivery_fee_minor",
    InjectionKind.FREE_DELIVERY_THRESHOLD_SET: "free_delivery_threshold_minor",
    InjectionKind.OFFER_START: "offer_value",
}

#: Kinds that name a single SKU. The remainder are store-wide.
SKU_SCOPED: Final[frozenset[InjectionKind]] = frozenset(
    {
        InjectionKind.STOCK_SET,
        InjectionKind.STOCK_DECREMENT,
        InjectionKind.PRICE_SET,
        InjectionKind.AVAILABILITY_SET,
    }
)


def expected_field_for(kind: InjectionKind) -> str | None:
    """The state field ``kind`` may change, or None for a whole-store reset."""
    return _FIELD_FOR_KIND.get(kind)


@dataclass(frozen=True, slots=True)
class StateDelta:
    """One before/after pair, in primitives the canonical hasher accepts."""

    field: str
    before: int | bool
    after: int | bool

    def __post_init__(self) -> None:
        if not self.field:
            raise ScenarioError("a state delta must name the field it changes")
        # bool is a subclass of int, so an explicit type-identity check is needed to stop
        # `available: True -> 3` from being accepted as a boolean flip.
        if isinstance(self.before, bool) != isinstance(self.after, bool):
            raise ScenarioError(f"{self.field}: before and after must be the same type")

    def to_payload(self) -> dict[str, Any]:
        return {"field": self.field, "before": self.before, "after": self.after}


@dataclass(frozen=True, slots=True)
class ScenarioInjection:
    """A labelled, replayable change to merchant state.

    Guarantees: the label is always ``SCENARIO_INJECTION``; the deltas match the kind;
    ``revision_after`` is exactly ``revision_before + 1``, so the injection log doubles as
    a total order over merchant state and a gap in it is detectable.

    Refuses: any other label, a SKU on a store-wide kind or a missing SKU on a
    SKU-scoped one, a delta naming a field the kind may not change, and a revision jump.
    """

    injection_id: uuid.UUID
    kind: InjectionKind
    deltas: tuple[StateDelta, ...]
    revision_before: int
    revision_after: int
    injected_at: datetime
    note: str = ""
    sku: str | None = None
    currency: str | None = None
    label: str = SCENARIO_LABEL
    #: An offer is five facts and a delta names one number, so the identity and the window
    #: ride here while the delta carries the figure that changes the arithmetic. That keeps
    #: the audit payload of an offer the same shape as the audit payload of a price change.
    offer_id: str | None = None
    offer_label: str | None = None
    offer_is_percent: bool = True
    offer_from_epoch_ms: int | None = None
    offer_to_epoch_ms: int | None = None

    def __post_init__(self) -> None:
        if self.label != SCENARIO_LABEL:
            raise ScenarioError(
                f"injections must be labelled {SCENARIO_LABEL!r}; an unlabelled demo "
                "change is indistinguishable from an organic merchant failure"
            )
        if self.revision_after != self.revision_before + 1:
            raise ScenarioError(
                "an injection advances the catalogue revision by exactly one; "
                f"got {self.revision_before} -> {self.revision_after}"
            )
        if self.injected_at.tzinfo is None:
            raise ScenarioError("injected_at must be timezone-aware")

        offer_fields = (
            self.offer_id,
            self.offer_label,
            self.offer_from_epoch_ms,
            self.offer_to_epoch_ms,
        )
        if self.kind is InjectionKind.OFFER_START:
            if any(field is None for field in offer_fields):
                raise ScenarioError(
                    "OFFER_START must name the offer, its label and the window it runs for"
                )
        elif any(field is not None for field in offer_fields):
            raise ScenarioError(f"{self.kind} is not an offer and must carry no offer fields")

        sku_required = self.kind in SKU_SCOPED
        if sku_required and not self.sku:
            raise ScenarioError(f"{self.kind} must name the SKU it changes")
        if not sku_required and self.sku is not None:
            raise ScenarioError(f"{self.kind} is store-wide and must not name a SKU")

        expected = expected_field_for(self.kind)
        if expected is None:
            if self.deltas:
                raise ScenarioError(f"{self.kind} restores a baseline and carries no deltas")
        else:
            if len(self.deltas) != 1:
                raise ScenarioError(f"{self.kind} carries exactly one delta")
            if self.deltas[0].field != expected:
                raise ScenarioError(
                    f"{self.kind} may only change {expected!r}, not {self.deltas[0].field!r}"
                )

    @property
    def delta(self) -> StateDelta:
        """The single delta, for the kinds that carry one."""
        if not self.deltas:
            raise ScenarioError(f"{self.kind} carries no delta")
        return self.deltas[0]

    def to_audit_payload(self) -> dict[str, Any]:
        """Canonicalizable audit record. Integers and strings only, so the whole thing can
        be fed to :func:`commerce_domain.canonical_hash` and chained into the audit log."""
        return {
            "label": self.label,
            "injection_id": str(self.injection_id),
            "kind": self.kind.value,
            "sku": self.sku or "",
            "currency": self.currency or "",
            "deltas": [delta.to_payload() for delta in self.deltas],
            "revision_before": self.revision_before,
            "revision_after": self.revision_after,
            "note": self.note,
            # Epoch milliseconds: the JCS profile in use refuses floats.
            "injected_at_epoch_ms": int(self.injected_at.timestamp() * 1000),
        }
