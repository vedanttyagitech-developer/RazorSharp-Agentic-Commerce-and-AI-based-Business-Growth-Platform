"""What counts as a material change between two checkout documents.

A buyer approves an exact purchase. Before money moves, the kernel asks the merchant the
same question again and compares the answers. The comparison this module performs is the
one that decides whether the approval still stands.

**Why a comparator and not a hash.** Both documents are canonically hashed, so a single
equality test would be shorter. It would also be wrong. ``catalogue_revision`` and
``source_id`` are inside the hashed key set, and the revision is a store-wide counter that
advances for *every* mutation the merchant makes -- a price change on a product this buyer
never looked at moves it. Comparing digests would therefore invalidate every open checkout
in the shop each time anything in it changed, which is the opposite of the promise: an
unrelated catalogue edit does not touch a purchase somebody already approved.

So the key set is partitioned explicitly, and the partition is asserted against
:data:`~transaction_kernel.checkout_content.CONTENT_KEYS` by a test. A key added to the
content contract without a decision about which side of the line it falls on fails that
test rather than silently defaulting to "does not matter".

**Degrading rather than guessing.** A document that is not canonical -- the legacy minimal
shape a :class:`~transaction_kernel.admission.MerchantStateSource` may project, or ``None``
from a source that supplies no document at all -- yields no deltas. That is deliberate.
This comparator's output is the evidence a buyer is shown for a refusal, and inventing rows
from a document whose shape is unknown would produce claims the kernel cannot stand behind.
The caller's own total comparison still runs, so such a source loses the itemisation, never
the refusal.

**Pure.** No session, no clock, no I/O, the same discipline as ``checkout_content``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from commerce_domain import Delta

from .checkout_content import CONTENT_KEYS, LINE_KEYS

__all__ = [
    "IDENTITY_KEYS",
    "IMMATERIAL_KEYS",
    "MATERIAL_LINE_KEYS",
    "MATERIAL_TOP_LEVEL",
    "REPORTED_BY_CALLER",
    "material_deltas",
]

#: Top-level keys that describe the purchase. A change to any of these changes what the
#: buyer agreed to pay, or what they agreed to pay it for.
MATERIAL_TOP_LEVEL: Final[frozenset[str]] = frozenset(
    {
        "currency",
        "lines",
        "line_items",
        "subtotal_minor",
        "tax_minor",
        "delivery_fee_minor",
        "discount_minor",
        "total_minor",
    }
)

#: Keys that say *which* document this is rather than what it costs. They differ between
#: version N and version N+1 by construction, so comparing them would report a difference
#: on every supersede.
IDENTITY_KEYS: Final[frozenset[str]] = frozenset({"content_version", "checkout_id", "version"})

#: Keys that move for reasons that have nothing to do with this purchase.
#: ``catalogue_revision`` advances on every store mutation, ``source_id`` names the quoting
#: system, and ``policy_version`` is the merchant's rule set -- whose change is governed by
#: the Policy-at-Sale Receipt, not by re-approval, because the terms the buyer accepted are
#: frozen at the freeze and are not renegotiated by a price movement.
IMMATERIAL_KEYS: Final[frozenset[str]] = frozenset(
    {"policy_version", "catalogue_revision", "source_id"}
)

#: Material, but reported by the caller rather than here, so a changed total produces one
#: row and not two. Admission compares the *approval record's* amount against merchant
#: truth, which is a different fact from one document disagreeing with another, and the
#: kernel must not fold the two together: if they ever disagree, that is a defect to
#: surface rather than to average away.
REPORTED_BY_CALLER: Final[frozenset[str]] = frozenset({"total_minor"})

#: Line keys worth comparing. ``sku`` is the identity a line is matched on, and ``name`` is
#: display text -- a merchant fixing a spelling has not changed the purchase.
MATERIAL_LINE_KEYS: Final[frozenset[str]] = frozenset(
    {"quantity", "unit_minor", "line_minor", "tax_minor"}
)

#: Why each top-level key moved, as a stable reason key the renderers translate.
_TOP_LEVEL_REASON: Final[Mapping[str, str]] = {
    "currency": "currency_changed",
    "subtotal_minor": "subtotal_changed",
    "tax_minor": "tax_changed",
    "delivery_fee_minor": "delivery_fee_changed",
    "discount_minor": "discount_changed",
}

#: Why each line key moved.
_LINE_REASON: Final[Mapping[str, str]] = {
    "quantity": "quantity_changed",
    "unit_minor": "unit_price_changed",
    "line_minor": "line_total_changed",
    "tax_minor": "line_tax_changed",
}

#: Compared line by line instead, so that a buyer is told *which* item moved rather than
#: that the set of items is not the set it was.
_COVERED_BY_LINES: Final[frozenset[str]] = frozenset({"lines", "line_items"})


def _is_canonical(document: Mapping[str, Any] | None) -> bool:
    """Whether this document has exactly the shape the content contract closes over.

    Key-set equality rather than a validation call: the comparator only needs to know that
    every key it is about to read is present and that no key it has never heard of is, and
    a full re-validation would raise on documents this function must answer False for.
    """
    if not isinstance(document, Mapping):
        return False
    return frozenset(document) == CONTENT_KEYS


def _lines_by_sku(document: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """The document's lines, keyed by SKU.

    Matched by SKU rather than by position because a removed line shifts every index after
    it, which would report every later line as changed when only one was withdrawn. The
    content contract guarantees SKUs are unique within a document, so the mapping is total.
    """
    lines = document.get("lines")
    if not isinstance(lines, Sequence):
        return {}
    out: dict[str, Mapping[str, Any]] = {}
    for line in lines:
        if isinstance(line, Mapping) and frozenset(line) == LINE_KEYS:
            out[str(line["sku"])] = line
    return out


def material_deltas(
    approved: Mapping[str, Any] | None, current: Mapping[str, Any] | None
) -> list[Delta]:
    """Every material difference between an approved document and a current one.

    Returns an empty list when either side is absent or non-canonical -- see the module
    docstring for why that is a refusal to guess rather than a claim that nothing moved.

    Rows are ordered document-level first, then by SKU and by line key, so that two runs
    over the same pair of documents produce the same list. A buyer re-reading a refusal
    should not find its rows shuffled.
    """
    if not _is_canonical(approved) or not _is_canonical(current):
        return []
    assert approved is not None and current is not None  # narrowed by _is_canonical

    deltas: list[Delta] = []
    for key in sorted(MATERIAL_TOP_LEVEL - _COVERED_BY_LINES - REPORTED_BY_CALLER):
        before, after = approved[key], current[key]
        if before != after:
            deltas.append(
                Delta(
                    field_path=key,
                    approved=before,
                    current=after,
                    reason=_TOP_LEVEL_REASON[key],
                )
            )

    before_lines = _lines_by_sku(approved)
    after_lines = _lines_by_sku(current)
    for sku in sorted(set(before_lines) | set(after_lines)):
        was = before_lines.get(sku)
        now = after_lines.get(sku)
        if was is not None and now is None:
            # Reported as the quantity falling to zero rather than as a bare absence: it is
            # the same fact, said in the units the rest of the table is written in.
            deltas.append(
                Delta(
                    field_path=f"lines[{sku}].quantity",
                    approved=was["quantity"],
                    current=0,
                    reason="item_unavailable",
                )
            )
            continue
        if was is None and now is not None:
            deltas.append(
                Delta(
                    field_path=f"lines[{sku}].quantity",
                    approved=0,
                    current=now["quantity"],
                    reason="item_added",
                )
            )
            continue
        assert was is not None and now is not None
        for key in sorted(MATERIAL_LINE_KEYS):
            if was[key] != now[key]:
                deltas.append(
                    Delta(
                        field_path=f"lines[{sku}].{key}",
                        approved=was[key],
                        current=now[key],
                        reason=_LINE_REASON[key],
                    )
                )
    return deltas
