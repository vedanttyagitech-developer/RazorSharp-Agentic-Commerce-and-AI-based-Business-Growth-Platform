"""Money formatting for buyer-facing text.

Formatting is the only money operation this package performs. It converts an exact
:class:`commerce_domain.Money` to a display string; it never parses prose back into an
amount to act on it, and it never adds two amounts. The grounding post-check parses
display strings only to prove they came from a tool result.

Grouping is Indian (specification 19.10: locale-correct for ``en-IN`` and ``hi-IN``,
including Indian digit grouping and paise), so 1234567 paise reads ``₹12,345.67`` and
123456700 paise reads ``₹12,34,567.00``. The grouping is done on the integer minor units
by string slicing, never through a float and never through a rounding formatter: a
formatter that rounds is a formatter that can quote a total the fee engine did not
compute.

:func:`is_money_field` and :func:`display_delta_value` exist because a
:class:`commerce_domain.Delta` carries a raw ``field_path`` and an untyped value. The
kernel writes ``total``; the in-memory backend writes ``total_minor``. Both mean paise,
and a renderer that recognises only one of them shows a buyer the bare number ``39500``
where the sentence promised ``₹395.00``. One predicate, shared by every renderer, is how
those two vocabularies stay a single contract.
"""

from __future__ import annotations

from typing import Any, Final

from commerce_domain import Money, exponent_for

__all__ = ["ABSENT", "display_amount", "display_delta_value", "display_minor", "is_money_field"]

_SYMBOL: Final[dict[str, str]] = {"INR": "₹"}

#: Shown where a delta has no value on one side -- an item the merchant no longer lists
#: has no current price. An em dash, never ``0``: zero is a price, absence is not.
ABSENT: Final[str] = "—"

#: Leaf names that mean "integer minor units" even without the ``_minor`` suffix. The
#: kernel's own deltas use these bare names (``transaction_kernel.admission``), so a
#: renderer keyed only on the suffix would print paise as a plain integer.
_MONEY_LEAVES: Final[frozenset[str]] = frozenset(
    {
        "amount",
        "captured",
        "delivery_fee",
        "delivery_tax",
        "free_delivery_threshold",
        "gap_to_free_delivery",
        "items_subtotal",
        "items_tax",
        "refundable",
        "refunded",
        "subtotal",
        "tax",
        "total",
        "unit_price",
    }
)


def _group_indian(units: str) -> str:
    """``"1234567"`` -> ``"12,34,567"``: last three digits, then pairs, Indian convention."""
    if len(units) <= 3:
        return units
    head, tail = units[:-3], units[-3:]
    groups: list[str] = []
    while len(head) > 2:
        head, group = head[:-2], head[-2:]
        groups.append(group)
    if head:
        groups.append(head)
    return f"{','.join(reversed(groups))},{tail}"


def display_minor(minor: int, currency: str) -> str:
    """``2800, "INR"`` -> ``"₹28.00"``. Integer arithmetic only; no float ever touches it."""
    exponent = exponent_for(currency)
    scale = 10**exponent
    sign = "-" if minor < 0 else ""
    units, fraction = divmod(abs(minor), scale)
    symbol = _SYMBOL.get(currency, f"{currency} ")
    grouped = _group_indian(str(units))
    if exponent == 0:
        return f"{sign}{symbol}{grouped}"
    return f"{sign}{symbol}{grouped}.{fraction:0{exponent}d}"


def display_amount(money: Money) -> str:
    """Format an exact :class:`commerce_domain.Money`. The only money call a renderer makes."""
    return display_minor(money.minor, money.currency)


def _leaf(field_path: str) -> str:
    """``"lines[AMUL-DAIRY-001].unit_price_minor"`` -> ``"unit_price_minor"``."""
    return field_path.rsplit(".", 1)[-1]


def is_money_field(field_path: str) -> bool:
    """True when this delta path names integer minor units and must be shown as money.

    Accepts both vocabularies in use: the ``*_minor`` suffix the service layer writes, and
    the bare kernel leaf names. A path this returns False for is rendered with ``str()`` --
    quantities, availability flags and SKUs are not money.
    """
    leaf = _leaf(field_path)
    return leaf.endswith("_minor") or leaf in _MONEY_LEAVES


def display_delta_value(field_path: str, value: Any, currency: str) -> str:
    """Render one side of a :class:`commerce_domain.Delta` for a buyer.

    ``None`` is :data:`ABSENT`, money is formatted, everything else is shown verbatim.
    ``bool`` is tested before ``int`` because ``True`` is an ``int`` in Python and
    ``₹0.01`` is not what ``free_delivery_applied`` means.
    """
    if value is None:
        return ABSENT
    if isinstance(value, bool) or not isinstance(value, int):
        return str(value)
    if is_money_field(field_path):
        return display_minor(value, currency)
    return str(value)
