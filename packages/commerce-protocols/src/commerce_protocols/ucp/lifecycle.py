"""Mapping UCP lifecycle objects onto internal ones, specification 14.2.

The rule that shapes this module is one sentence:

    Every UCP object maps to a separate internal domain object; UCP payloads are not stored
    as the domain model.

It is worth spelling out what goes wrong without it, because storing the protocol payload is
genuinely the easier thing to do and it looks fine for a while. A UCP checkout arrives as
JSON that already contains items, totals and a status; persisting it means one less mapping
to write. The costs arrive later and all at once. The pinned version becomes load-bearing on
the *stored* data, so a protocol revision is a data migration. A second protocol -- ACP, say
-- either gets a second storage shape or is coerced into UCP's, and the kernel's invariants
now have to be enforced twice or in the wrong vocabulary. And the amount a buyer approved
lives in a document an external party composed, rather than in a row this platform built.

So translation happens here, at the edge, in both directions:

**Inbound**, a UCP request becomes a :class:`~commerce_protocols.core.intent.ProtocolIntent`
and nothing else crosses. The original payload survives only in the evidence chain, where it
is a record of what was asked rather than a source of truth about what is.

**Outbound**, internal state is projected into a UCP object built fresh each time. A
projection cannot drift from the state it is built from, whereas a stored payload updated
alongside its domain object drifts the first time somebody updates one and not the other.

Amounts
-------
Every amount crossing this boundary is an integer in minor units on both sides -- UCP's
``amount`` fields are integers and so is :class:`~commerce_domain.Money`. That agreement is
lucky rather than designed, and it is asserted rather than assumed: :func:`_minor` refuses a
float outright instead of coercing one, because a float that arrives here has already lost
whatever precision it was going to lose and rounding it produces a total that differs from
the one the buyer was shown by an amount nobody can predict.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Final

from ap2.sdk.generated.types.checkout import Status
from commerce_domain import Money

from ..core.errors import SchemaRejected
from ..core.identity import AuthenticatedCaller
from ..core.intent import IntentKind, ProtocolIntent
from ..core.pins import PINS, Protocol, require_pin

__all__ = [
    "LIFECYCLE_OBJECTS",
    "UCP_INTENT_BY_OPERATION",
    "checkout_projection",
    "intent_for",
    "line_items_from",
    "total_of",
]

#: The UCP surface specification 14.2 enumerates. Held as data so the profile, the tests and
#: any coverage report read the same list rather than three drifting copies.
LIFECYCLE_OBJECTS: Final[tuple[str, ...]] = (
    "catalogue",
    "basket",
    "checkout",
    "fulfilment",
    "completion",
    "order",
    "cancellation",
    "refund",
    "post_purchase_status",
)

#: Which internal intent each UCP operation becomes. The absences are the interesting part:
#: there is no UCP operation that maps to an approval, because approval is not something an
#: external party can perform, and cancellation and refund map to *proposals* because naming
#: them anything else would make them executable.
UCP_INTENT_BY_OPERATION: Final[Mapping[str, IntentKind]] = {
    "search_catalogue": IntentKind.DISCOVER,
    "get_product": IntentKind.DISCOVER,
    "check_availability": IntentKind.CHECK_INVENTORY,
    "create_basket": IntentKind.BUILD_BASKET,
    "update_basket": IntentKind.BUILD_BASKET,
    "create_checkout": IntentKind.CREATE_CHECKOUT,
    "get_checkout": IntentKind.TRACK_ORDER,
    "complete_checkout": IntentKind.SUBMIT_APPROVED,
    "get_order": IntentKind.TRACK_ORDER,
    "cancel_order": IntentKind.PROPOSE_CANCELLATION,
    "request_refund": IntentKind.PROPOSE_REFUND,
    "get_post_purchase_status": IntentKind.TRACK_ORDER,
}


def _minor(value: Any, field: str) -> int:
    """An integer minor-unit amount, or refuse.

    ``bool`` is excluded by name because it is a subclass of ``int`` in Python, so ``True``
    would otherwise be accepted as one paisa. That is not a hypothetical: JSON ``true``
    deserialises to ``True``, and a malformed feed can put one in an amount field.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise SchemaRejected(
            "amount_is_not_an_integer_minor_unit", field=field, kind=type(value).__name__
        )
    return value


def total_of(checkout: Mapping[str, Any]) -> Money:
    """The checkout's ``total`` component as :class:`~commerce_domain.Money`.

    UCP carries totals as a list of typed components -- subtotal, discount, tax, fulfilment,
    total -- and only the one typed ``total`` is the amount a buyer owes. Summing the others
    would be an independent implementation of the merchant's arithmetic, and where the two
    disagreed this platform would be substituting its own answer for the merchant's.
    """
    currency = checkout.get("currency")
    if not isinstance(currency, str) or len(currency) != 3:
        raise SchemaRejected("checkout_currency_missing_or_malformed")
    totals = checkout.get("totals")
    if not isinstance(totals, Sequence) or isinstance(totals, str | bytes):
        raise SchemaRejected("checkout_totals_missing")
    for entry in totals:
        if isinstance(entry, Mapping) and entry.get("type") == "total":
            return Money(_minor(entry.get("amount"), "totals[type=total].amount"), currency)
    raise SchemaRejected("checkout_has_no_total_component")


def line_items_from(checkout: Mapping[str, Any]) -> dict[str, int]:
    """The checkout's line items as ``{sku: quantity}``, the internal basket shape.

    Flattened to the platform's own vocabulary rather than carried as UCP line items,
    because this is the value the merchant state source will be asked to revalidate and it
    must be in the form that source understands. Duplicate SKUs are refused rather than
    summed: two lines for one SKU is ambiguous about intent, and a platform that guessed
    would be deciding on a buyer's behalf how much of something they meant to buy.
    """
    items = checkout.get("line_items")
    if not isinstance(items, Sequence) or isinstance(items, str | bytes):
        raise SchemaRejected("checkout_line_items_missing")
    basket: dict[str, int] = {}
    for line in items:
        if not isinstance(line, Mapping):
            raise SchemaRejected("line_item_is_not_an_object")
        item = line.get("item")
        if not isinstance(item, Mapping):
            raise SchemaRejected("line_item_has_no_item")
        sku = item.get("id")
        if not isinstance(sku, str) or not sku:
            raise SchemaRejected("line_item_has_no_sku")
        quantity = _minor(line.get("quantity"), "line_items[].quantity")
        if quantity < 1:
            raise SchemaRejected("line_item_quantity_below_one", sku=sku)
        if sku in basket:
            raise SchemaRejected("line_items_name_the_same_sku_twice", sku=sku)
        basket[sku] = quantity
    if not basket:
        raise SchemaRejected("checkout_has_no_line_items")
    return basket


def intent_for(
    operation: str,
    caller: AuthenticatedCaller,
    *,
    announced_version: str | None,
    correlation_id: uuid.UUID,
    external_id: str | None = None,
    checkout_id: uuid.UUID | None = None,
    checkout_version: int | None = None,
    content_hash: str | None = None,
    amount: Money | None = None,
    arguments: Mapping[str, Any] | None = None,
    raw_reference: str | None = None,
) -> ProtocolIntent:
    """Translate one UCP operation into the internal command, 13.1 step 4.

    The version is validated here rather than by the caller, so there is no path from a UCP
    request to an intent that skips the pin. An operation this platform does not implement is
    refused by name -- not silently mapped to something adjacent, because a caller that asked
    for a capability the profile does not advertise deserves to be told so rather than given
    a surprising approximation.
    """
    pin = require_pin(Protocol.UCP, announced_version)
    kind = UCP_INTENT_BY_OPERATION.get(operation)
    if kind is None:
        raise SchemaRejected(
            "ucp_operation_not_implemented",
            operation=operation,
            implemented=sorted(UCP_INTENT_BY_OPERATION),
        )
    return ProtocolIntent(
        kind=kind,
        caller=caller,
        pin=pin,
        correlation_id=correlation_id,
        external_id=external_id,
        checkout_id=checkout_id,
        checkout_version=checkout_version,
        content_hash=content_hash,
        amount=amount,
        arguments=dict(arguments or {}),
        raw_reference=raw_reference,
    )


def checkout_projection(
    *,
    external_checkout_id: str,
    merchant_id: str,
    merchant_name: str,
    currency: str,
    line_items: Sequence[Mapping[str, Any]],
    totals: Sequence[Mapping[str, Any]],
    status: Status,
    links: Sequence[Mapping[str, Any]] = (),
    messages: Sequence[Mapping[str, Any]] = (),
    continue_url: str | None = None,
) -> dict[str, Any]:
    """Project internal state into a UCP checkout object, built fresh every time.

    Never cached and never stored. A projection recomputed from current state cannot be
    stale; a stored copy updated beside its domain object is stale the first time somebody
    updates one and forgets the other, and the buyer's total is the field it will be stale in.

    ``links`` is required by the pinned schema with no default, so an empty list is emitted
    rather than the member being omitted -- a document that fails its own schema is not a
    document a counterparty can use.
    """
    document: dict[str, Any] = {
        "id": external_checkout_id,
        "merchant": {"id": merchant_id, "name": merchant_name},
        "currency": currency,
        "line_items": [dict(line) for line in line_items],
        "totals": [dict(total) for total in totals],
        "status": status.value,
        "links": [dict(link) for link in links],
    }
    if messages:
        document["messages"] = [dict(message) for message in messages]
    if continue_url is not None:
        document["continue_url"] = continue_url
    return document


def profile_pin_version() -> str:
    """The UCP version this platform answers to. One source, for the profile and the tests."""
    return PINS[Protocol.UCP].version
