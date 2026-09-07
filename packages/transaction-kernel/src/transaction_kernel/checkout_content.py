"""Canonical checkout content, ADR 0003 D6.

Why one module owns this shape
------------------------------
The content hash is the thing a buyer approves. It is copied into the approval, bound into
the Policy-at-Sale Receipt, compared at admission and carried by the Execution Grant. If
two producers -- the merchant simulator today, a real connector tomorrow -- could each
decide what the payload looks like, the same cart would hash differently depending on
who built it, and an approval recorded against one shape would be refused against the
other. So the shape is owned here, by the kernel, and every producer builds content only
through :func:`build_checkout_content`. The simulator does not define the contract; it
satisfies it.

Two consumers already read this payload and fix two of its keys:

* :mod:`transaction_kernel.reservations` derives held inventory from
  ``content -> 'lines'``, an array of objects carrying ``sku`` and an integer
  ``quantity``. Renaming either breaks oversell protection.
* :mod:`transaction_kernel.admission` compares the approved total against what the
  merchant says now and, on a material change, writes version N+1 from a copy of this
  payload with ``checkout_id`` and ``version`` re-stamped. Every other key is carried
  through unchanged, which is why this module validates the whole document rather than
  only the parts it computes.

Both projections are present at once: ``lines`` for reservation accounting and
``line_items`` (``sku -> quantity``) for delta reporting. They are derived from the same
source, and :func:`validate_checkout_content` refuses a document in which they disagree.

Integers only. Money is minor units beside an ISO 4217 code, never a decimal; the JCS
profile in :mod:`commerce_domain` refuses floats, so a float here would fail to hash
rather than hash to something a later verifier cannot reproduce.

Frozen contract. ``CONTENT_VERSION`` is inside the hashed document. A change to the shape
-- a new key, a renamed key, a different sort order -- must bump it, because every stored
approval, receipt and grant names a hash computed under the old shape. The regression
vector in ``test_tk_checkout_content.py`` pins one document and its hash; if that test
fails, stored approvals are already unverifiable and the change must be reverted or
versioned.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from commerce_domain import (
    CanonicalizationError,
    DomainError,
    Money,
    MoneyError,
    canonical_hash,
)

__all__ = [
    "CONTENT_KEYS",
    "CONTENT_VERSION",
    "LINE_KEYS",
    "ContentContractError",
    "ContentLine",
    "build_checkout_content",
    "content_hash",
    "lines_of",
    "total_of",
    "units_of",
    "validate_checkout_content",
]

#: Version tag inside every hashed document. See the module docstring for the rule.
CONTENT_VERSION: Final = "checkout_content/1"

#: The exact key set of one line. Closed: an extra key would enter the hash unreviewed.
LINE_KEYS: Final[frozenset[str]] = frozenset(
    {"sku", "name", "quantity", "unit_minor", "line_minor", "tax_minor"}
)

#: The exact top-level key set. Closed for the same reason.
CONTENT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "content_version",
        "checkout_id",
        "version",
        "currency",
        "lines",
        "line_items",
        "subtotal_minor",
        "tax_minor",
        "delivery_fee_minor",
        "discount_minor",
        "total_minor",
        "policy_version",
        "catalogue_revision",
        "source_id",
    }
)


class ContentContractError(DomainError):
    """The document is not canonical checkout content.

    ``path`` names the offending location in JSON-pointer-ish dotted form (``lines[2].sku``,
    ``total_minor``), so a producer can find the defect without reading this module.
    Raised rather than returned: a malformed document must never reach a hash, an
    approval or a reservation, and a returned code is a code somebody can ignore.
    """

    def __init__(self, path: str, message: str) -> None:
        self.path = path
        super().__init__(f"{path}: {message}")


# --------------------------------------------------------------------------- checks


def _require_int(value: object, path: str, *, minimum: int) -> int:
    """An int at ``path``, at least ``minimum``. Refuses bool: True is not a quantity."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContentContractError(path, f"must be an integer, got {type(value).__name__}")
    if value < minimum:
        raise ContentContractError(path, f"must be >= {minimum}, got {value}")
    return value


def _require_str(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContentContractError(path, "must be a non-empty string")
    return value


def _require_currency(value: object, path: str) -> str:
    code = _require_str(value, path)
    try:
        Money(0, code)
    except MoneyError as exc:
        raise ContentContractError(path, f"not a supported currency code: {exc}") from exc
    return code


def _require_uuid(value: object, path: str) -> str:
    text = _require_str(value, path)
    try:
        parsed = uuid.UUID(text)
    except ValueError as exc:
        raise ContentContractError(path, "must be a UUID string") from exc
    if str(parsed) != text:
        # The hash covers the bytes as written. Two spellings of one UUID would be two
        # hashes for one checkout, so only the canonical lower-case hyphenated form passes.
        raise ContentContractError(path, "must be the canonical lower-case hyphenated form")
    return text


# ---------------------------------------------------------------------------- lines


@dataclass(frozen=True, slots=True)
class ContentLine:
    """One priced line, in integer minor units.

    ``line_minor`` is carried rather than recomputed so that the document states what the
    buyer was shown; the constructor then insists it equals ``unit_minor * quantity``, so
    the stated figure cannot disagree with the arithmetic.
    """

    sku: str
    name: str
    quantity: int
    unit_minor: int
    line_minor: int
    tax_minor: int

    def __post_init__(self) -> None:
        self.validate("line")

    def validate(self, path: str) -> None:
        _require_str(self.sku, f"{path}.sku")
        _require_str(self.name, f"{path}.name")
        _require_int(self.quantity, f"{path}.quantity", minimum=1)
        _require_int(self.unit_minor, f"{path}.unit_minor", minimum=0)
        _require_int(self.line_minor, f"{path}.line_minor", minimum=0)
        _require_int(self.tax_minor, f"{path}.tax_minor", minimum=0)
        if self.line_minor != self.unit_minor * self.quantity:
            raise ContentContractError(
                f"{path}.line_minor",
                f"{self.line_minor} does not equal unit_minor {self.unit_minor} "
                f"x quantity {self.quantity}",
            )

    def as_content(self) -> dict[str, Any]:
        """This line's contribution to the hashed document. Key set is :data:`LINE_KEYS`."""
        return {
            "sku": self.sku,
            "name": self.name,
            "quantity": self.quantity,
            "unit_minor": self.unit_minor,
            "line_minor": self.line_minor,
            "tax_minor": self.tax_minor,
        }

    @classmethod
    def from_content(cls, value: object, path: str) -> ContentLine:
        """Parse one stored line, refusing anything outside the closed key set."""
        if not isinstance(value, Mapping):
            raise ContentContractError(path, "must be an object")
        keys = set(value.keys())
        if keys != LINE_KEYS:
            missing = sorted(LINE_KEYS - keys)
            extra = sorted(keys - LINE_KEYS)
            raise ContentContractError(
                path,
                f"line keys must be exactly {sorted(LINE_KEYS)}; "
                f"missing {missing}, unexpected {extra}",
            )
        line = cls(
            sku=_require_str(value["sku"], f"{path}.sku"),
            name=_require_str(value["name"], f"{path}.name"),
            quantity=_require_int(value["quantity"], f"{path}.quantity", minimum=1),
            unit_minor=_require_int(value["unit_minor"], f"{path}.unit_minor", minimum=0),
            line_minor=_require_int(value["line_minor"], f"{path}.line_minor", minimum=0),
            tax_minor=_require_int(value["tax_minor"], f"{path}.tax_minor", minimum=0),
        )
        line.validate(path)
        return line


def _ordered_lines(lines: Sequence[ContentLine], path: str) -> tuple[ContentLine, ...]:
    """Lines sorted by SKU with duplicates refused.

    Sorted, so that two producers assembling one cart in different orders hash alike:
    a reordered list would otherwise read as a material change and demand a fresh
    approval. Unique, because the reservation module and the ``line_items`` projection
    each need exactly one authoritative quantity per SKU.
    """
    if not lines:
        raise ContentContractError(path, "a checkout must price at least one line")
    seen: set[str] = set()
    for index, line in enumerate(lines):
        if not isinstance(line, ContentLine):
            raise ContentContractError(f"{path}[{index}]", "must be a ContentLine")
        if line.sku in seen:
            raise ContentContractError(
                f"{path}[{index}].sku", f"{line.sku!r} appears twice; one line per SKU"
            )
        seen.add(line.sku)
    return tuple(sorted(lines, key=lambda line: line.sku))


# --------------------------------------------------------------------------- builder


def build_checkout_content(
    *,
    checkout_id: uuid.UUID,
    version: int,
    currency: str,
    lines: Sequence[ContentLine],
    subtotal_minor: int,
    tax_minor: int,
    delivery_fee_minor: int,
    discount_minor: int,
    total_minor: int,
    policy_version: str,
    catalogue_revision: int,
    source_id: str,
) -> dict[str, Any]:
    """The one canonical checkout payload. Pure: no I/O, no clock.

    Guarantees a document that :func:`validate_checkout_content` accepts and that
    :func:`commerce_domain.canonical_hash` can hash: integers and strings only, lines
    sorted by SKU, ``line_items`` derived from ``lines``, and

        ``total_minor == subtotal_minor + tax_minor + delivery_fee_minor - discount_minor``
        ``subtotal_minor == sum(line.line_minor)``
        ``tax_minor >= sum(line.tax_minor)``

    ``tax_minor`` is the whole tax charged, line taxes plus any tax on fees, which is why
    it may exceed the line sum but never fall short of it. The arithmetic is checked, not
    trusted: a producer that hands in a total its own components do not add up to gets a
    :class:`ContentContractError`, because that total would otherwise travel unchallenged
    into the approval and out to the payment provider.
    """
    if not isinstance(checkout_id, uuid.UUID):
        raise ContentContractError("checkout_id", "must be a uuid.UUID")
    content: dict[str, Any] = {
        "content_version": CONTENT_VERSION,
        "checkout_id": str(checkout_id),
        "version": version,
        "currency": currency,
        "lines": [line.as_content() for line in _ordered_lines(lines, "lines")],
        "line_items": {line.sku: line.quantity for line in _ordered_lines(lines, "lines")},
        "subtotal_minor": subtotal_minor,
        "tax_minor": tax_minor,
        "delivery_fee_minor": delivery_fee_minor,
        "discount_minor": discount_minor,
        "total_minor": total_minor,
        "policy_version": policy_version,
        "catalogue_revision": catalogue_revision,
        "source_id": source_id,
    }
    validate_checkout_content(content)
    return content


# ------------------------------------------------------------------------- validator


def validate_checkout_content(content: object) -> None:
    """Refuse anything that is not a complete, consistent canonical document.

    Checks shape (closed key sets at both levels), types (integers that are not bools,
    non-empty strings, a canonical UUID, a supported currency), order (lines sorted by
    SKU, no duplicate SKU), the ``line_items`` projection against ``lines``, and the
    money arithmetic stated in :func:`build_checkout_content`.

    Raises :class:`ContentContractError` naming the first offending path. Never repairs:
    a document that had to be fixed before hashing is not the document the buyer saw.
    """
    if not isinstance(content, Mapping):
        raise ContentContractError("$", "content must be a JSON object")
    keys = set(content.keys())
    if keys != CONTENT_KEYS:
        missing = sorted(CONTENT_KEYS - keys)
        extra = sorted(keys - CONTENT_KEYS)
        raise ContentContractError(
            "$",
            f"keys must be exactly {sorted(CONTENT_KEYS)}; missing {missing}, unexpected {extra}",
        )
    for key in content:
        if not isinstance(key, str):  # pragma: no cover - CONTENT_KEYS equality already holds
            raise ContentContractError("$", f"non-string key {key!r}")

    if content["content_version"] != CONTENT_VERSION:
        raise ContentContractError(
            "content_version",
            f"expected {CONTENT_VERSION!r}, got {content['content_version']!r}; a document "
            "under another version must be hashed by the code that understands it",
        )
    _require_uuid(content["checkout_id"], "checkout_id")
    _require_int(content["version"], "version", minimum=1)
    _require_currency(content["currency"], "currency")
    _require_str(content["policy_version"], "policy_version")
    _require_int(content["catalogue_revision"], "catalogue_revision", minimum=0)
    _require_str(content["source_id"], "source_id")

    raw_lines = content["lines"]
    if not isinstance(raw_lines, Sequence) or isinstance(raw_lines, str | bytes):
        raise ContentContractError("lines", "must be an array of line objects")
    parsed = tuple(
        ContentLine.from_content(item, f"lines[{i}]") for i, item in enumerate(raw_lines)
    )
    if not parsed:
        raise ContentContractError("lines", "a checkout must price at least one line")
    skus = [line.sku for line in parsed]
    if skus != sorted(skus):
        raise ContentContractError("lines", "must be sorted by sku so one cart has one hash")
    if len(set(skus)) != len(skus):
        raise ContentContractError("lines", "must name each sku once")

    line_items = content["line_items"]
    if not isinstance(line_items, Mapping):
        raise ContentContractError("line_items", "must be an object mapping sku to quantity")
    expected_items = {line.sku: line.quantity for line in parsed}
    if dict(line_items) != expected_items:
        raise ContentContractError(
            "line_items", f"must equal the sku -> quantity projection of lines: {expected_items}"
        )

    subtotal = _require_int(content["subtotal_minor"], "subtotal_minor", minimum=0)
    tax = _require_int(content["tax_minor"], "tax_minor", minimum=0)
    delivery = _require_int(content["delivery_fee_minor"], "delivery_fee_minor", minimum=0)
    discount = _require_int(content["discount_minor"], "discount_minor", minimum=0)
    total = _require_int(content["total_minor"], "total_minor", minimum=0)

    line_sum = sum(line.line_minor for line in parsed)
    if subtotal != line_sum:
        raise ContentContractError(
            "subtotal_minor", f"{subtotal} does not equal the sum of line_minor ({line_sum})"
        )
    line_tax = sum(line.tax_minor for line in parsed)
    if tax < line_tax:
        raise ContentContractError(
            "tax_minor", f"{tax} is less than the sum of line taxes ({line_tax})"
        )
    expected_total = subtotal + tax + delivery - discount
    if total != expected_total:
        raise ContentContractError(
            "total_minor",
            f"{total} does not equal subtotal {subtotal} + tax {tax} + delivery {delivery} "
            f"- discount {discount} = {expected_total}",
        )


# ----------------------------------------------------------------------- projections


def content_hash(content: Mapping[str, Any]) -> str:
    """The approval hash of a canonical document: validate, then ``canonical_hash``.

    Validation is not optional here. Hashing an unvalidated document is exactly how a
    wrong-shaped payload acquires a buyer's approval.
    """
    validate_checkout_content(content)
    try:
        digest: str = canonical_hash(dict(content))
    except CanonicalizationError as exc:  # pragma: no cover - validation excludes this
        raise ContentContractError("$", f"cannot canonicalize: {exc}") from exc
    return digest


def total_of(content: Mapping[str, Any]) -> Money:
    """The amount the buyer approves, as :class:`Money`."""
    validate_checkout_content(content)
    return Money(int(content["total_minor"]), str(content["currency"]))


def units_of(content: Mapping[str, Any]) -> dict[str, int]:
    """``sku -> quantity`` of a canonical document. The delta-reporting projection."""
    validate_checkout_content(content)
    return {str(sku): int(quantity) for sku, quantity in content["line_items"].items()}


def lines_of(content: Mapping[str, Any]) -> tuple[ContentLine, ...]:
    """The priced lines of a canonical document, in SKU order."""
    validate_checkout_content(content)
    return tuple(
        ContentLine.from_content(item, f"lines[{i}]") for i, item in enumerate(content["lines"])
    )
