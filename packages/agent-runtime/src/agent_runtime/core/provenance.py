"""Session provenance: a write may name only what a tool returned this session.

The model proposes a basket line, a checkout submission, a resolution evaluation or a
growth proposal by *id*. Before any of those reaches the backend, the id is checked
against :class:`SessionProvenance` -- the record of every SKU, price, basket, checkout
version, order and proposal id that a tool result carried in this session. An id the
record does not hold is refused with a structured :class:`Held` result, never a guess.
This is prevention where the reply post-check is only detection: a fabricated SKU is
stopped before the basket changes, not stripped from the prose afterwards.

WHY PER SESSION, AND WHY IDS ONLY
---------------------------------
Ids are stable: a SKU the merchant returned three turns ago is still a SKU the merchant
issued. Money is not: the price seen three turns ago is exactly the fact the version
N -> N+1 demonstration proves stale. So provenance of ids is per session and persists
in the ADK session state, while money facts for prose live in the per-turn
``GroundingLedger``. The price recorded here beside a SKU is what the model was *shown*,
kept so a hold can name it; it is never evidence for a sentence.

The map is capped at :data:`PROVENANCE_CAP` newest entries per family, so a session that
browses the whole catalogue does not grow without bound and a hostile loop of searches
cannot make the state blob unbounded. Eviction is oldest-first; re-seeing an id moves it
to newest.

WRITE SERIALIZATION
-------------------
One model round can emit several tool calls that the runtime executes concurrently. Two
``basket_set_line`` calls that each read the basket, compute, and write would interleave
and one would win silently. :func:`session_write_lock` hands out one ``asyncio.Lock`` per
session so every read-compute-write runs alone; the lock lives only while something holds
it, so idle sessions cost nothing.

The cap, the newest-wins ordering and the per-session lock follow
``shopping_agent/gates.py`` and ``commerce_common/types.py::remember`` in
anthropics/commerce-agents (Apache-2.0); the code and the gate set are this package's.
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from ..backends.base import (
    ApprovalCard,
    BasketView,
    CheckoutView,
    OrderView,
    ProductCard,
    SearchPage,
)

__all__ = [
    "MAX_BASKET_LINES",
    "MAX_LINE_QUANTITY",
    "PROVENANCE_CAP",
    "PROVENANCE_STATE_KEY",
    "Held",
    "SeenCheckout",
    "SeenSku",
    "SessionProvenance",
    "check_case_provenance",
    "check_checkout_provenance",
    "check_line_count",
    "check_order_provenance",
    "check_proposal_provenance",
    "check_quantity",
    "check_sku_provenance",
    "session_write_lock",
]

#: Newest entries kept per id family. Large enough for a long browsing session, small
#: enough that the session-state blob stays a few kilobytes.
PROVENANCE_CAP: Final[int] = 200
#: Key under which the harness persists the record in ADK session state.
PROVENANCE_STATE_KEY: Final[str] = "acr:provenance"
#: Per-line quantity cap, applied to the line as it will stand *after* the write.
MAX_LINE_QUANTITY: Final[int] = 50
#: Distinct lines one basket may hold.
MAX_BASKET_LINES: Final[int] = 40

#: Gate names. They appear in the ``blocked`` field of a held result and in the audit.
GATE_PROVENANCE: Final[str] = "provenance"
GATE_QUANTITY: Final[str] = "quantity"
GATE_LINE_COUNT: Final[str] = "line_count"


# ------------------------------------------------------------------------- outcomes


@dataclass(frozen=True, slots=True)
class Held:
    """A write the gate refused. Becomes the tool result; the backend is never called.

    ``to_result`` is always a NON-EMPTY dict: on ADK an empty dict from a callback means
    "run the tool after all", so a held outcome that serialised to ``{}`` would be a
    silent pass. ``instruction`` tells the model what a valid next call looks like, in
    terms of tools, never in terms of the buyer's authority.
    """

    gate: str
    reason_key: str
    instruction: str
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_result(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False,
            "blocked": self.gate,
            "reason_key": self.reason_key,
            "instruction": self.instruction,
        }
        result.update(self.detail)
        return result


# --------------------------------------------------------------------------- records


@dataclass(frozen=True, slots=True)
class SeenSku:
    """A catalogue item some tool returned. The price is what was shown, not evidence."""

    sku: str
    unit_price_minor: int
    currency: str
    catalogue_revision: int
    is_available: bool


@dataclass(frozen=True, slots=True)
class SeenCheckout:
    """Versions of one checkout a tool returned, each with the hash the buyer saw."""

    checkout_id: str
    hashes_by_version: Mapping[int, str]

    def knows(self, version: int, content_hash: str) -> bool:
        return self.hashes_by_version.get(version) == content_hash


def _remember[T](records: dict[str, T], key: str, value: T, cap: int = PROVENANCE_CAP) -> None:
    """Insert as newest; evict oldest past the cap. Re-seeing an id refreshes its age."""
    records.pop(key, None)
    records[key] = value
    while len(records) > cap:
        del records[next(iter(records))]


@dataclass(slots=True)
class SessionProvenance:
    """Every id a tool returned this session, newest last, capped per family."""

    skus: dict[str, SeenSku] = field(default_factory=dict)
    baskets: dict[str, None] = field(default_factory=dict)
    checkouts: dict[str, SeenCheckout] = field(default_factory=dict)
    orders: dict[str, None] = field(default_factory=dict)
    proposals: dict[str, None] = field(default_factory=dict)
    cases: dict[str, None] = field(default_factory=dict)

    # ---- remembering -------------------------------------------------------

    def remember_sku(
        self,
        sku: str,
        *,
        unit_price_minor: int,
        currency: str,
        catalogue_revision: int,
        is_available: bool = True,
    ) -> None:
        _remember(
            self.skus,
            sku.upper(),
            SeenSku(sku.upper(), unit_price_minor, currency, catalogue_revision, is_available),
        )

    def remember_product(self, card: ProductCard) -> None:
        self.remember_sku(
            card.sku,
            unit_price_minor=card.unit_price.minor,
            currency=card.unit_price.currency,
            catalogue_revision=card.provenance.catalogue_revision,
            is_available=card.is_available,
        )

    def remember_search(self, page: SearchPage) -> None:
        for card in page.hits:
            self.remember_product(card)

    def remember_basket(self, view: BasketView) -> None:
        """A basket read names its own id and re-grounds every line it priced.

        A line the merchant could not price is remembered too, as unavailable: the buyer
        may ask to remove it, and a removal is a write that must pass the gate.
        """
        _remember(self.baskets, view.basket_id, None)
        if view.quote is not None:
            for line in view.quote.lines:
                self.remember_sku(
                    line.sku,
                    unit_price_minor=line.unit_price.minor,
                    currency=line.unit_price.currency,
                    catalogue_revision=view.provenance.catalogue_revision,
                )
        for gone in view.unavailable:
            self.remember_sku(
                gone.sku,
                unit_price_minor=0,
                currency=view.quote.currency if view.quote is not None else "INR",
                catalogue_revision=view.provenance.catalogue_revision,
                is_available=False,
            )

    def remember_approval(self, card: ApprovalCard) -> None:
        seen = self.checkouts.get(card.checkout_id)
        hashes = dict(seen.hashes_by_version) if seen is not None else {}
        hashes[card.version] = card.content_hash
        _remember(self.checkouts, card.checkout_id, SeenCheckout(card.checkout_id, hashes))

    def remember_checkout(self, view: CheckoutView) -> None:
        for card in view.versions:
            self.remember_approval(card)

    def remember_order(self, view: OrderView) -> None:
        _remember(self.orders, view.order_id, None)
        if view.quote is not None:
            for line in view.quote.lines:
                self.remember_sku(
                    line.sku,
                    unit_price_minor=line.unit_price.minor,
                    currency=line.unit_price.currency,
                    catalogue_revision=view.quote.provenance.catalogue_revision,
                )

    def remember_order_id(self, order_id: str) -> None:
        """An order id the trusted surface told the harness; the agent did not invent it."""
        _remember(self.orders, order_id, None)

    def remember_proposal(self, proposal_id: str) -> None:
        _remember(self.proposals, proposal_id, None)

    def remember_case(self, case_key: str) -> None:
        """A review case some tool returned: the queue listing, or a read of one case.

        The key alone, with nothing beside it. A case's reason code, provider state and
        exposure are what the reviewer is being shown, and holding a copy here would let a
        card be drawn from a record this session captured earlier rather than from the
        queue as it stands. The key is the one part that is stable enough to remember.
        """
        _remember(self.cases, case_key, None)

    # ---- queries -------------------------------------------------------------

    def knows_sku(self, sku: str) -> bool:
        return sku.upper() in self.skus

    def knows_basket(self, basket_id: str) -> bool:
        return basket_id in self.baskets

    def knows_checkout(self, checkout_id: str) -> bool:
        return checkout_id in self.checkouts

    def knows_order(self, order_id: str) -> bool:
        return order_id in self.orders

    def knows_proposal(self, proposal_id: str) -> bool:
        return proposal_id in self.proposals

    def knows_case(self, case_key: str) -> bool:
        return case_key in self.cases

    def seen_skus(self) -> frozenset[str]:
        return frozenset(self.skus)

    # ---- persistence ----------------------------------------------------------

    def to_state(self) -> dict[str, Any]:
        """JSON-safe form for ADK session state. Order is preserved, so age survives."""
        return {
            "skus": [
                {
                    "sku": s.sku,
                    "unit_price_minor": s.unit_price_minor,
                    "currency": s.currency,
                    "catalogue_revision": s.catalogue_revision,
                    "is_available": s.is_available,
                }
                for s in self.skus.values()
            ],
            "baskets": list(self.baskets),
            "checkouts": [
                {
                    "checkout_id": c.checkout_id,
                    "versions": {str(v): h for v, h in c.hashes_by_version.items()},
                }
                for c in self.checkouts.values()
            ],
            "orders": list(self.orders),
            "proposals": list(self.proposals),
            "cases": list(self.cases),
        }

    @classmethod
    def from_state(cls, state: object) -> SessionProvenance:
        """Rebuild from session state. Anything malformed yields an *empty* record.

        Empty is the safe direction: a corrupted blob then means every write is held
        until a fresh read re-grounds it, never that an unknown id slips through.
        """
        record = cls()
        if not isinstance(state, dict):
            return record
        try:
            for raw in _items(state.get("skus")):
                record.remember_sku(
                    str(raw["sku"]),
                    unit_price_minor=int(raw["unit_price_minor"]),
                    currency=str(raw["currency"]),
                    catalogue_revision=int(raw["catalogue_revision"]),
                    is_available=bool(raw.get("is_available", True)),
                )
            for basket_id in _strings(state.get("baskets")):
                _remember(record.baskets, basket_id, None)
            for raw in _items(state.get("checkouts")):
                versions = raw.get("versions")
                if not isinstance(versions, dict):
                    raise TypeError("versions must be a mapping")
                hashes = {int(v): str(h) for v, h in versions.items()}
                checkout_id = str(raw["checkout_id"])
                _remember(record.checkouts, checkout_id, SeenCheckout(checkout_id, hashes))
            for order_id in _strings(state.get("orders")):
                _remember(record.orders, order_id, None)
            for proposal_id in _strings(state.get("proposals")):
                _remember(record.proposals, proposal_id, None)
            for case_key in _strings(state.get("cases")):
                _remember(record.cases, case_key, None)
        except KeyError, TypeError, ValueError:
            return cls()
        return record


def _items(value: object) -> Iterable[dict[str, Any]]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise TypeError("expected a list")
    for item in value:
        if not isinstance(item, dict):
            raise TypeError("expected an object")
    return value


def _strings(value: object) -> Iterable[str]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise TypeError("expected a list")
    return [str(item) for item in value]


# ------------------------------------------------------------------------------ gates
#
# Pure functions: (record, arguments) -> Held | None. They hold no state, call no
# backend and know nothing about ADK, so a test can prove each one with a dict.


def check_sku_provenance(
    record: SessionProvenance, sku: str, *, basket_lines: Iterable[str] = ()
) -> Held | None:
    """A basket write may name a SKU a tool returned this session, or a line already held.

    The second clause exists for baskets that predate the session (a returning buyer):
    removing or changing a line the basket already has needs no fresh search. The
    instruction names ``product`` first because text search does not match ids, and an
    empty search reads to a model as proof the item does not exist (spec 20.4).
    """
    wanted = sku.upper()
    if record.knows_sku(wanted) or any(line.upper() == wanted for line in basket_lines):
        return None
    return Held(
        GATE_PROVENANCE,
        "sku_not_returned",
        f"SKU {wanted} was not returned by any catalogue or basket tool in this session. "
        "Resolve it first: call product with this exact SKU, or find it with search, then "
        "write using a SKU from those results.",
        {"sku": wanted},
    )


def check_checkout_provenance(
    record: SessionProvenance, checkout_id: str, version: int, content_hash: str
) -> Held | None:
    """A submit may name only a checkout version, with its hash, that a tool returned.

    Three distinct refusals on purpose. An unknown checkout, a known checkout at a version
    never shown, and a known version with a hash that does not match are three different
    mistakes, and the model needs to know which one it made to correct it with one read.
    """
    seen = record.checkouts.get(checkout_id)
    if seen is None:
        return Held(
            GATE_PROVENANCE,
            "checkout_not_returned",
            f"Checkout {checkout_id} was not returned by any tool in this session. Call "
            "checkout_get with this id first, then submit the version and hash it shows.",
            {"checkout_id": checkout_id},
        )
    if version not in seen.hashes_by_version:
        return Held(
            GATE_PROVENANCE,
            "checkout_version_not_returned",
            f"Version {version} of checkout {checkout_id} was never shown to you. Call "
            "checkout_get and submit the current version it reports.",
            {"checkout_id": checkout_id, "version": version},
        )
    if not seen.knows(version, content_hash):
        return Held(
            GATE_PROVENANCE,
            "content_hash_mismatch",
            f"The content hash does not match what was shown for version {version} of "
            f"checkout {checkout_id}. Copy the hash from the checkout_get result exactly.",
            {"checkout_id": checkout_id, "version": version},
        )
    return None


def check_order_provenance(record: SessionProvenance, order_id: str) -> Held | None:
    """A support read or evaluation may name only an order this session was shown."""
    if record.knows_order(order_id):
        return None
    return Held(
        GATE_PROVENANCE,
        "order_not_returned",
        f"Order {order_id} was not returned by any tool in this session and was not "
        "handed to this conversation by the buyer's own screen. Ask the buyer for the "
        "order reference shown on their order page, or call order_track with an id from "
        "an earlier result.",
        {"order_id": order_id},
    )


def check_proposal_provenance(record: SessionProvenance, proposal_id: str) -> Held | None:
    """A present or read of a growth proposal may name only one this session created."""
    if record.knows_proposal(proposal_id):
        return None
    return Held(
        GATE_PROVENANCE,
        "proposal_not_returned",
        f"Proposal {proposal_id} was not created or returned in this session. Create one "
        "with growth_proposal_create, or present one whose id an earlier result carried.",
        {"proposal_id": proposal_id},
    )


def check_case_provenance(record: SessionProvenance, case_key: str) -> Held | None:
    """A present of a review case may name only a case this session actually read.

    The strictest of these gates, because of where it sits. A guessed SKU is caught by the
    catalogue; a guessed case key would be drawn as a card that looks exactly like a card
    drawn from the audit log, on the one surface whose entire purpose is that a person can
    trust what is on it. So the key must have come back from the queue.
    """
    if record.knows_case(case_key):
        return None
    return Held(
        GATE_PROVENANCE,
        "case_not_returned",
        f"Case {case_key} was not returned by any tool in this session. Call "
        "support_case_read with no arguments to list the queue, then read and present a "
        "case key it returned.",
        {"case_key": case_key},
    )


def check_quantity(quantity: int, *, cap: int = MAX_LINE_QUANTITY) -> Held | None:
    """The quantity a line will hold after the write: an integer in ``[0, cap]``.

    Zero is allowed because zero removes the line. ``bool`` is refused explicitly since
    ``True`` is an ``int`` in Python and a model that writes ``true`` did not mean one.
    """
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        return Held(
            GATE_QUANTITY,
            "invalid_quantity",
            "quantity must be a whole number: 0 removes the line, 1 or more sets it.",
            {"quantity": quantity},
        )
    if quantity < 0:
        return Held(
            GATE_QUANTITY,
            "invalid_quantity",
            "quantity cannot be negative: use 0 to remove the line.",
            {"quantity": quantity},
        )
    if quantity > cap:
        return Held(
            GATE_QUANTITY,
            "quantity_exceeds_cap",
            f"quantity {quantity} exceeds the per-line limit of {cap}. Set at most {cap}; "
            "tell the buyer the limit rather than splitting the order.",
            {"quantity": quantity, "cap": cap},
        )
    return None


def check_line_count(
    current_lines: Iterable[str], sku: str, quantity: int, *, cap: int = MAX_BASKET_LINES
) -> Held | None:
    """Adding a *new* line to a basket already at the cap is held; changes and removals pass."""
    held = {line.upper() for line in current_lines}
    if quantity == 0 or sku.upper() in held or len(held) < cap:
        return None
    return Held(
        GATE_LINE_COUNT,
        "basket_full",
        f"The basket already holds {cap} distinct items, the maximum. Remove a line "
        "(quantity 0) before adding another.",
        {"line_count": len(held), "cap": cap},
    )


# -------------------------------------------------------------------------- the lock

_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


def session_write_lock(session_id: str) -> asyncio.Lock:
    """One lock per session, alive only while some coroutine holds a reference to it.

    A weak dictionary rather than a plain one: sessions are many and short, and a lock
    that outlived its session would be a leak keyed by an id nobody will use again.
    Callers must hold the returned lock in a local for the duration of the write, which
    ``async with session_write_lock(sid):`` does.
    """
    lock = _locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[session_id] = lock
    return lock
