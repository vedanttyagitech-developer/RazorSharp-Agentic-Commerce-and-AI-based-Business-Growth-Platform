"""HTTP backend over the ADR 0003 endpoint catalogue.

The API is being built concurrently, so this module is written against the *contract*
-- the endpoint table in ``docs/adr/0003-service-layer.md`` plus D9 (idempotency) and
D15 (RFC 9457 problems) -- and unit-tested against ``httpx.MockTransport``. The JSON
shapes below are what this client expects; they mirror ``Quote.to_checkout_content()``
and ``KernelDecision`` field for field, so the API can produce them without inventing a
second vocabulary.

WIRE SHAPES (expected)
----------------------
product card::

    {"sku", "name", "description"?, "category", "unit_label", "unit_price_minor",
     "currency", "stock_units", "is_listed", "is_available",
     "source", "catalogue_revision", "observed_at"?}

search page  ``GET /v1/catalogue/search?q=&locale=&limit=``::

    {"query", "locale", "hits": [product card...], "source", "catalogue_revision"}

quote (inside a basket view or approval card)::

    {"currency", "lines": [{"sku", "name", "quantity", "unit_price_minor",
     "subtotal_minor", "tax_bp", "tax_minor"}], "items_subtotal_minor",
     "items_tax_minor", "delivery_fee_minor", "delivery_tax_minor", "total_minor",
     "free_delivery_applied", "gap_to_free_delivery_minor",
     "free_delivery_threshold_minor"?, "content_hash", "source", "catalogue_revision"}

basket view  ``POST /v1/baskets``, ``PUT /v1/baskets/{id}/lines/{sku}``, ``GET /v1/baskets/{id}``::

    {"basket_id", "code", "lines": [{"sku", "quantity"}], "quote": quote|null,
     "unavailable": [{"sku", "requested", "available_units", "listed"}],
     "stale", "source", "catalogue_revision"}

approval card  ``POST /v1/baskets/{id}/checkout``::

    {"checkout_id", "version", "content_hash", "status", "quote": quote, "expires_at"?}

checkout view  ``GET /v1/checkouts/{id}``::

    {"checkout_id", "current_version", "versions": [approval card...],
     "payment": {"attempt_id", "state"}|null}

order  ``GET /v1/orders/{id}``::

    {"order_id", "checkout_id", "version", "content_hash", "policy_receipt_hash"?,
     "state", "amount_minor", "currency", "quote": quote|null,
     "payment": {"attempt_id", "state", "capture_evidence": {"kind", ...}|null},
     "refunds": [{"refund_id", "amount_minor", "currency", "state", "reason",
     "automatic"}]}

decision  ``POST /v1/checkouts/{id}/versions/{v}/submit`` (200 even when denied, D15)::

    {"decision_id", "allowed", "code", "explanation",
     "checkout": {"checkout_id", "version", "content_hash"}|null,
     "deltas": [{"field_path", "approved", "current", "reason"}],
     "grant_id"?, "payment_attempt_id"?, "next_version"?, "correlation_id"?}

catalogue page  ``GET /v1/catalogue/products?limit=&cursor=&category=&listed=&available=``::

    {"products": [{"sku", "name_en", "name_hi", "display_name", "category", "unit_label",
     "unit_price_minor", "currency", "stock_units", "is_listed", "is_available",
     "freshness": {...}}], "next_cursor": str|null, "limit", "matched",
     "counts_by_category": {category: count}, "revision"}

order and refund collections  ``GET /v1/orders?limit=&cursor=``, ``GET /v1/refunds?...``::

    {"orders"|"refunds": [row...], "next_cursor": str|null, "limit",
     "scope": "own"|"tenant", "counts": {state: count}}

review queue  ``GET /v1/review/queue?limit=`` (operator)::

    {"cases": [{"case_key", "state", "priority", "reason_code",
     "monetary_exposure": {"minor", "currency", "display"}|null, "opened_at",
     "target_response_by", "proof_chain": {"href", ...}, ...}],
     "priority_counts": {priority: count}, "limit", "scope"}

one case  ``GET /v1/review/queue/{case_key}`` (operator)::

    {"case": case row, "verified_provider_state": {"present", "status", "provider_status",
     ...}, "refused_evidence": {...}|null, "attempt": projection|null, "resolutions": [...],
     "timeline": [{"occurred_at", "action", "actor", "summary", "source",
     "scenario_injection", "details": {...}}], "scope"}

at-sale policy  ``GET /v1/orders/{id}/policy``::

    {"order_id", "checkout_id", "checkout_version", "binding_ok", "binding_code",
     "binding_reason", "policy_receipt_id": str|null, "policy_receipt_hash": str|null,
     "policies": [{"kind", "policy_id", "policy_version", "applies_to": [str],
     "terms": {...}, "document_ref": str|null, "document_hash": str|null}]}

resolution by order  ``GET /v1/orders/{id}/resolution``::

    {"order_id", "payment_attempt_id", "recorded_state", "findings",
     "resolutions": [{"finding_id", "code", "plan_issued", "plan_id": str|null,
     "recorded", "options": [{"outcome", "amount": {"minor", "currency", "display"},
     "policy_kind", "policy_id", "policy_version", "confirmation", "basis"}],
     "withheld": [{"outcome", "reason", "detail"}], "captured_minor",
     "refunds_reserved_minor", "refundable_minor", "currency", "evaluated_at",
     "valid_until": str|null, "explanation", ...}], "plan_ttl_seconds"}

Those two are buyer-scoped and carry **no** scenario key, unlike the review routes above:
the same resolution is on the operator surface keyed by a payment attempt, and reaching it
that way from a buyer-facing agent would hand it findings about other people's payments.

The merchant surface is built from those three: ``matched`` and ``counts_by_category``
already span the catalogue, and ``counts`` already spans the collection's scope, so this
client walks pages only for the figures the API does not pre-compute. The review queue is
read as it arrives: the API composes a case, and nothing here recomputes one.

A shape violation raises :class:`BackendError` with reason ``contract_violation`` rather
than a ``KeyError``: the agent gets a structured failure it can explain, and the log
names the endpoint and key.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

import httpx
from commerce_domain import Money
from merchant_sim import Locale
from transaction_kernel import CheckoutRef, Delta, KernelDecision, RecoveryCode

from .base import (
    ApprovalCard,
    BackendError,
    BasketQuote,
    BasketView,
    CaseBackend,
    CaseEvent,
    CasePriority,
    CaseRecord,
    CaseState,
    CaseSummary,
    CatalogueHealth,
    CheckoutMetrics,
    CheckoutStatus,
    CheckoutView,
    CommerceBackend,
    InventoryAnomaly,
    MerchantBackend,
    OrderResolution,
    OrderState,
    OrderView,
    PaymentSummary,
    PolicyAtSale,
    PolicyKind,
    PolicyTerm,
    PricedLine,
    Problem,
    ProductCard,
    Provenance,
    RefundRecord,
    RemedyConfirmation,
    RemedyOption,
    RemedyOutcome,
    ResolutionPlan,
    SearchPage,
    SupportBackend,
    UnavailableLine,
    WithheldReason,
    WithheldRemedy,
)
from .memory import LOW_STOCK_UNITS

__all__ = ["HttpBackend", "parse_problem"]

_PROBLEM_MEDIA: Final[str] = "application/problem+json"
_CONTRACT: Final[str] = "urn:acr:problem:contract-violation"
#: ADR 0003 D13: provider transport timeout is 20 s; the API sits in front of it.
_DEFAULT_TIMEOUT_S: Final[float] = 20.0

#: The largest page ``GET /v1/catalogue/products`` and the collection routes accept.
#: Asking for the maximum keeps a merchant read to as few round trips as the API allows.
_PAGE_LIMIT: Final[int] = 100

#: How many pages one merchant read will walk before it stops and reports what it counted.
#: An unbounded loop over a catalogue that grows is a denial of service against our own
#: API, so the walk stops; :meth:`HttpBackend.catalogue_health` then declares the stop
#: arithmetically -- ``total`` is the size the API reported while the breakdown covers only
#: the rows actually read, so ``listed + delisted < total`` *is* the statement "partial".
_MAX_PAGES: Final[int] = 20

#: ADR 0003 D11's stand-in for the merchant operator surface. Named here rather than
#: imported because ``commerce-api`` sits above this package in the dependency order
#: (ADR 0003 D2); it is a wire header like the paths above it.
_SCENARIO_KEY_HEADER: Final[str] = "X-Scenario-Key"

_OWN_SCOPE: Final[str] = "own"
_TENANT_SCOPE: Final[str] = "tenant"

#: The label a metrics card carries when no order has named a currency yet. It labels a
#: figure that is absent, never one this client computed from rows of mixed currencies.
_SETTLEMENT_CURRENCY: Final[str] = "INR"


def _contract(where: str, detail: str) -> BackendError:
    return BackendError(
        Problem(
            problem_type=_CONTRACT,
            title="API response did not match the agent contract",
            status=502,
            detail=f"{where}: {detail}",
        )
    )


class _Shape:
    """Strict reader over one JSON object. A missing or mistyped key is a contract error."""

    __slots__ = ("_data", "_where")

    def __init__(self, data: object, where: str) -> None:
        if not isinstance(data, dict):
            raise _contract(where, f"expected an object, got {type(data).__name__}")
        self._data: dict[str, Any] = data
        self._where = where

    def _get(self, key: str) -> Any:
        if key not in self._data:
            raise _contract(self._where, f"missing key {key!r}")
        return self._data[key]

    def str_(self, key: str) -> str:
        value = self._get(key)
        if not isinstance(value, str):
            raise _contract(self._where, f"{key!r} must be a string")
        return value

    def opt_str(self, key: str) -> str | None:
        value = self._data.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise _contract(self._where, f"{key!r} must be a string or null")
        return value

    def int_(self, key: str) -> int:
        value = self._get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise _contract(self._where, f"{key!r} must be an integer")
        return value

    def opt_int(self, key: str) -> int | None:
        value = self._data.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise _contract(self._where, f"{key!r} must be an integer or null")
        return value

    def bool_(self, key: str) -> bool:
        value = self._get(key)
        if not isinstance(value, bool):
            raise _contract(self._where, f"{key!r} must be a boolean")
        return value

    def list_(self, key: str) -> list[Any]:
        value = self._data.get(key, [])
        if not isinstance(value, list):
            raise _contract(self._where, f"{key!r} must be a list")
        return value

    def counts(self, key: str) -> dict[str, int]:
        """A name-to-count map, every value checked.

        Counts are read strictly rather than coerced because the whole value of the API's
        ``counts`` maps is that a state present with zero means "none" while a state
        missing means the platform did not measure it. A float or a string sneaking in
        would destroy that distinction quietly, one dashboard row at a time.
        """
        value = self._get(key)
        if not isinstance(value, dict):
            raise _contract(self._where, f"{key!r} must be an object of counts")
        counted: dict[str, int] = {}
        for name, count in value.items():
            if isinstance(count, bool) or not isinstance(count, int):
                raise _contract(self._where, f"{key}[{name!r}] must be an integer")
            counted[str(name)] = count
        return counted

    def obj(self, key: str) -> _Shape:
        return _Shape(self._get(key), f"{self._where}.{key}")

    def opt_obj(self, key: str) -> _Shape | None:
        value = self._data.get(key)
        return None if value is None else _Shape(value, f"{self._where}.{key}")

    def money(self, key: str, currency: str) -> Money:
        return Money(self.int_(key), currency)

    def opt_uuid(self, key: str) -> uuid.UUID | None:
        value = self.opt_str(key)
        if value is None:
            return None
        try:
            return uuid.UUID(value)
        except ValueError:
            raise _contract(self._where, f"{key!r} must be a UUID") from None

    def uuid_(self, key: str) -> uuid.UUID:
        value = self.opt_uuid(key)
        if value is None:
            raise _contract(self._where, f"missing key {key!r}")
        return value

    def opt_datetime(self, key: str) -> datetime | None:
        value = self.opt_str(key)
        if value is None:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            raise _contract(self._where, f"{key!r} must be an ISO-8601 timestamp") from None

    def datetime_(self, key: str) -> datetime:
        value = self.opt_datetime(key)
        if value is None:
            raise _contract(self._where, f"missing key {key!r}")
        return value

    def provenance(self) -> Provenance:
        """Provenance spread flat across this object. Only a quote is shaped that way.

        A quote is hashed and signed as one flat content object, so its source and its
        revision sit beside its amounts rather than in a nested block. Everything else the
        API returns carries a ``freshness`` object instead; read those with
        :meth:`freshness` and not this.
        """
        return Provenance(
            source=self.str_("source"),
            catalogue_revision=self.int_("catalogue_revision"),
            observed_at=self.opt_datetime("observed_at"),
        )

    def freshness(self) -> Provenance:
        """Provenance from this object's nested ``freshness`` block.

        Missing or mistyped is a contract violation rather than an empty provenance,
        because provenance is what lets a buyer surface say when a price was read. A card
        that rendered with a blank source would look like an answer and be a guess about
        how old it is, which is worse on this surface than refusing to render at all.
        """
        return self.obj("freshness").provenance()

    def raw(self, key: str) -> Any:
        return self._data.get(key)


def parse_problem(response: httpx.Response) -> Problem:
    """Read an RFC 9457 body; fall back to the status line when the body is not one."""
    content_type = response.headers.get("content-type", "")
    body: Any = None
    if content_type.startswith(_PROBLEM_MEDIA) or content_type.startswith("application/json"):
        try:
            body = response.json()
        except ValueError:
            body = None
    if isinstance(body, dict):
        known = {"type", "title", "status", "detail", "instance"}
        status = body.get("status")
        return Problem(
            problem_type=str(body.get("type") or "about:blank"),
            title=str(body.get("title") or response.reason_phrase or "error"),
            status=status if isinstance(status, int) else response.status_code,
            detail=str(body.get("detail") or ""),
            instance=str(body["instance"]) if body.get("instance") else None,
            extensions={k: v for k, v in body.items() if k not in known},
        )
    return Problem(
        problem_type="about:blank",
        title=response.reason_phrase or "error",
        status=response.status_code,
        detail=response.text[:200],
    )


# --------------------------------------------------------------------------- parsers


def _product(data: object, where: str) -> ProductCard:
    """One product row, from the shape the catalogue routes actually emit.

    ``display_name`` rather than ``name_en``: the API has already applied the locale the
    request asked for, and the buyer surface should show a Hindi speaker the Hindi name.
    The merchant reads take ``name_en`` instead, because an anomaly whose name changed
    with the console's language could not be compared against the same anomaly anywhere
    else. Both fields are on the wire; which one is right depends on who is reading.
    """
    shape = _Shape(data, where)
    currency = shape.str_("currency")
    return ProductCard(
        sku=shape.str_("sku"),
        name=shape.str_("display_name"),
        # The catalogue routes carry no description. It stays optional rather than
        # required so a surface that grows one is not a contract violation on the day it
        # ships, and empty is the honest reading of a field the API does not send.
        description=shape.opt_str("description") or "",
        category=shape.str_("category"),
        unit_label=shape.str_("unit_label"),
        unit_price=shape.money("unit_price_minor", currency),
        stock_units=shape.int_("stock_units"),
        is_listed=shape.bool_("is_listed"),
        is_available=shape.bool_("is_available"),
        provenance=shape.freshness(),
    )


def _locale(value: str, where: str) -> Locale:
    try:
        return Locale(value)
    except ValueError:
        raise _contract(where, f"unknown locale {value!r}") from None


def _closed[EnumT: StrEnum](enum: type[EnumT], value: str, *, where: str, field: str) -> EnumT:
    """One closed-vocabulary field, or a contract violation naming what actually arrived.

    Refused rather than carried through as a string. On a review queue that is the whole
    point: a state or a priority this platform cannot produce, rendered as though it
    could, shows a reviewer a triage level nobody assigned. Refusing says the server and
    this client disagree about the vocabulary, which is the true statement and the one a
    log can be searched for.
    """
    try:
        return enum(value)
    except ValueError:
        raise _contract(where, f"unknown {field} {value!r}") from None


def _quote(data: object, where: str) -> BasketQuote:
    shape = _Shape(data, where)
    currency = shape.str_("currency")
    lines: list[PricedLine] = []
    for index, raw in enumerate(shape.list_("lines")):
        line = _Shape(raw, f"{where}.lines[{index}]")
        lines.append(
            PricedLine(
                sku=line.str_("sku"),
                name=line.str_("name"),
                quantity=line.int_("quantity"),
                unit_price=line.money("unit_price_minor", currency),
                subtotal=line.money("subtotal_minor", currency),
                tax_bp=line.int_("tax_bp"),
                tax=line.money("tax_minor", currency),
            )
        )
    threshold = shape.opt_int("free_delivery_threshold_minor")
    try:
        return BasketQuote(
            lines=tuple(lines),
            items_subtotal=shape.money("items_subtotal_minor", currency),
            items_tax=shape.money("items_tax_minor", currency),
            delivery_fee=shape.money("delivery_fee_minor", currency),
            delivery_tax=shape.money("delivery_tax_minor", currency),
            total=shape.money("total_minor", currency),
            free_delivery_applied=shape.bool_("free_delivery_applied"),
            gap_to_free_delivery=shape.money("gap_to_free_delivery_minor", currency),
            currency=currency,
            content_hash=shape.str_("content_hash"),
            provenance=shape.provenance(),
            free_delivery_threshold=None if threshold is None else Money(threshold, currency),
        )
    except ValueError as exc:
        raise _contract(where, str(exc)) from None


def _code(value: str, where: str) -> RecoveryCode:
    try:
        return RecoveryCode(value)
    except ValueError:
        # Specification 6.7: an agent may not act on a code it does not recognise.
        raise _contract(where, f"unknown recovery code {value!r}") from None


def _basket(data: object, where: str) -> BasketView:
    shape = _Shape(data, where)
    lines = tuple(
        (_Shape(raw, f"{where}.lines").str_("sku"), _Shape(raw, f"{where}.lines").int_("quantity"))
        for raw in shape.list_("lines")
    )
    quote_shape = shape.opt_obj("quote")
    unavailable = tuple(
        UnavailableLine(
            sku=item.str_("sku"),
            requested=item.int_("requested"),
            available_units=item.int_("available_units"),
            listed=item.bool_("listed"),
        )
        for item in (_Shape(raw, f"{where}.unavailable") for raw in shape.list_("unavailable"))
    )
    try:
        return BasketView(
            basket_id=shape.str_("basket_id"),
            code=_code(shape.str_("code"), where),
            lines=lines,
            quote=None if quote_shape is None else _quote(shape.raw("quote"), f"{where}.quote"),
            unavailable=unavailable,
            stale=shape.bool_("stale"),
            provenance=shape.freshness(),
        )
    except ValueError as exc:
        raise _contract(where, str(exc)) from None


def _status(value: str, where: str) -> CheckoutStatus:
    try:
        return CheckoutStatus(value)
    except ValueError:
        raise _contract(where, f"unknown checkout status {value!r}") from None


def _approval(data: object, where: str) -> ApprovalCard:
    shape = _Shape(data, where)
    return ApprovalCard(
        checkout_id=shape.str_("checkout_id"),
        version=shape.int_("version"),
        content_hash=shape.str_("content_hash"),
        status=_status(shape.str_("status"), where),
        quote=_quote(shape.raw("quote"), f"{where}.quote"),
        expires_at=shape.opt_datetime("expires_at"),
    )


def _payment(shape: _Shape) -> PaymentSummary:
    """``capture_evidence`` is an object on the wire; the agent only ever quotes its kind."""
    evidence = shape.opt_obj("capture_evidence")
    return PaymentSummary(
        attempt_id=shape.str_("attempt_id"),
        state=shape.str_("state"),
        capture_evidence=None if evidence is None else evidence.str_("kind"),
    )


def _order_state(value: str, where: str) -> OrderState:
    try:
        return OrderState(value)
    except ValueError:
        raise _contract(where, f"unknown order state {value!r}") from None


def _order(data: object, where: str) -> OrderView:
    shape = _Shape(data, where)
    currency = shape.str_("currency")
    refunds = tuple(
        RefundRecord(
            refund_id=item.str_("refund_id"),
            amount=item.money("amount_minor", item.str_("currency")),
            state=item.str_("state"),
            reason=item.str_("reason"),
            automatic=item.bool_("automatic"),
        )
        for item in (
            _Shape(raw, f"{where}.refunds[{i}]") for i, raw in enumerate(shape.list_("refunds"))
        )
    )
    return OrderView(
        order_id=shape.str_("order_id"),
        checkout_id=shape.str_("checkout_id"),
        version=shape.int_("version"),
        content_hash=shape.str_("content_hash"),
        state=_order_state(shape.str_("state"), where),
        amount=shape.money("amount_minor", currency),
        payment=_payment(shape.obj("payment")),
        refunds=refunds,
        quote=None if shape.raw("quote") is None else _quote(shape.raw("quote"), f"{where}.quote"),
        policy_receipt_hash=shape.opt_str("policy_receipt_hash"),
    )


def _checkout(data: object, where: str) -> CheckoutView:
    shape = _Shape(data, where)
    payment_shape = shape.opt_obj("payment")
    payment = None if payment_shape is None else _payment(payment_shape)
    try:
        return CheckoutView(
            checkout_id=shape.str_("checkout_id"),
            current_version=shape.int_("current_version"),
            versions=tuple(
                _approval(raw, f"{where}.versions[{i}]")
                for i, raw in enumerate(shape.list_("versions"))
            ),
            payment=payment,
        )
    except ValueError as exc:
        raise _contract(where, str(exc)) from None


def _decision(data: object, where: str) -> KernelDecision:
    shape = _Shape(data, where)
    checkout_shape = shape.opt_obj("checkout")
    checkout = (
        None
        if checkout_shape is None
        else CheckoutRef(
            checkout_id=checkout_shape.uuid_("checkout_id"),
            version=checkout_shape.int_("version"),
            content_hash=checkout_shape.str_("content_hash"),
        )
    )
    deltas = tuple(
        Delta(
            field_path=item.str_("field_path"),
            approved=item.raw("approved"),
            current=item.raw("current"),
            reason=item.str_("reason"),
        )
        for item in (_Shape(raw, f"{where}.deltas") for raw in shape.list_("deltas"))
    )
    try:
        return KernelDecision(
            decision_id=shape.uuid_("decision_id"),
            allowed=shape.bool_("allowed"),
            code=_code(shape.str_("code"), where),
            explanation=shape.str_("explanation"),
            checkout=checkout,
            deltas=deltas,
            grant_id=shape.opt_uuid("grant_id"),
            payment_attempt_id=shape.opt_uuid("payment_attempt_id"),
            next_version=shape.opt_int("next_version"),
            correlation_id=shape.opt_uuid("correlation_id"),
        )
    except ValueError as exc:
        raise _contract(where, str(exc)) from None


# ----------------------------------------------------------------- merchant parsers


@dataclass(frozen=True, slots=True)
class _CatalogueRow:
    """The part of a catalogue row a count or an anomaly is allowed to depend on.

    Deliberately not a :class:`ProductCard`. A card is what a buyer is shown and carries
    merchant-authored prose that has to be fenced before a model sees it; a merchant count
    needs none of that, and a reader that never compiles the description cannot leak one.
    """

    sku: str
    name: str
    stock_units: int
    is_listed: bool
    is_available: bool


@dataclass(frozen=True, slots=True)
class _CataloguePage:
    """One page of the merchant's catalogue, with the shape of the whole beside it."""

    rows: tuple[_CatalogueRow, ...]
    next_cursor: str | None
    matched: int
    counts_by_category: dict[str, int]
    revision: int


@dataclass(frozen=True, slots=True)
class _CatalogueWalk:
    """Every row a bounded walk reached, and whether it reached the end.

    ``total`` and ``by_category`` come from the API and always span the catalogue;
    ``rows`` spans only what was walked. ``complete`` is what separates a count from a
    prefix of one, and no figure derived from ``rows`` may be labelled a total without it.
    """

    rows: tuple[_CatalogueRow, ...]
    total: int
    by_category: dict[str, int]
    revision: int
    complete: bool


@dataclass(frozen=True, slots=True)
class _OrderRow:
    """The money facts of one order row, exactly as the database summed them.

    ``refunded_minor`` is the API's sum over *settled* refund rows for this order. It is
    taken from the order rather than recomputed from the refund collection on purpose:
    the API already decided which refund states count as money that came back, and a
    second opinion formed here would eventually disagree with the console beside it.
    """

    amount_minor: int
    currency: str
    refunded_minor: int


@dataclass(frozen=True, slots=True)
class _OrdersPage:
    rows: tuple[_OrderRow, ...]
    next_cursor: str | None
    scope: str
    counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class _OrdersWalk:
    rows: tuple[_OrderRow, ...]
    scope: str
    counts: dict[str, int]
    complete: bool


def _catalogue_row(data: object, where: str) -> _CatalogueRow:
    shape = _Shape(data, where)
    return _CatalogueRow(
        sku=shape.str_("sku"),
        # ``name_en`` rather than ``display_name``: the latter follows the request locale,
        # and an anomaly whose SKU changed name depending on which language the console
        # happened to ask in would not compare against anything, least of all against the
        # same anomaly read from the in-memory backend.
        name=shape.str_("name_en"),
        stock_units=shape.int_("stock_units"),
        is_listed=shape.bool_("is_listed"),
        is_available=shape.bool_("is_available"),
    )


def _catalogue_page(data: object, where: str) -> _CataloguePage:
    shape = _Shape(data, where)
    return _CataloguePage(
        rows=tuple(
            _catalogue_row(raw, f"{where}.products[{i}]")
            for i, raw in enumerate(shape.list_("products"))
        ),
        next_cursor=shape.opt_str("next_cursor"),
        matched=shape.int_("matched"),
        counts_by_category=shape.counts("counts_by_category"),
        revision=shape.int_("revision"),
    )


def _list_scope(value: str, where: str) -> str:
    """``own`` or ``tenant``, refused if it is neither.

    Treating an unrecognised scope as the narrow one would be the safe-looking mistake: a
    scope this client had not heard of would silently turn every money figure into "not
    measured", the dashboard would go quiet, and nothing would say why.
    """
    if value not in (_OWN_SCOPE, _TENANT_SCOPE):
        raise _contract(where, f"unknown list scope {value!r}")
    return value


def _orders_page(data: object, where: str) -> _OrdersPage:
    shape = _Shape(data, where)
    rows: list[_OrderRow] = []
    for index, raw in enumerate(shape.list_("orders")):
        row = _Shape(raw, f"{where}.orders[{index}]")
        rows.append(
            _OrderRow(
                amount_minor=row.int_("amount_minor"),
                currency=row.str_("currency"),
                refunded_minor=row.int_("refunded_minor"),
            )
        )
    return _OrdersPage(
        rows=tuple(rows),
        next_cursor=shape.opt_str("next_cursor"),
        scope=_list_scope(shape.str_("scope"), where),
        counts=shape.counts("counts"),
    )


# --------------------------------------------------------------------------- client


class HttpBackend(CommerceBackend, MerchantBackend, CaseBackend, SupportBackend):
    """Registry A over HTTP. Bearer session, one Idempotency-Key per mutation.

    A fresh UUID per mutation call is the honest choice for an agent surface: the agent
    does not own a retry loop (the kernel and the API do), so a replayed key would only
    ever come from a bug. ADR D9 makes a *different* key on a concurrent second submit
    yield ``DUPLICATE_OPERATION`` with the winner's attempt, which is the behaviour the
    checkout agent is written to handle.

    It implements the merchant and review-queue surfaces as well, so a specialist reads
    the same figures whichever backend it was handed. That equivalence is the whole point
    of the protocols: an agent tested against the simulator and run against the API must
    not answer two different questions.
    """

    def __init__(
        self,
        base_url: str,
        *,
        bearer: str,
        scenario_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json"},
            transport=transport,
            timeout=timeout,
        )
        # Held apart from the client's default headers and attached only to the two
        # merchant collection reads. The scenario key is what widens a collection from
        # the caller's own rows to the tenant's (ADR 0003 D11), and it would widen
        # ``order_track`` too if it rode on every request -- an agent holding a merchant
        # backend would silently gain the ability to open any buyer's order. The buyer
        # surface stays exactly as narrow as it was before this key existed.
        self._scenario_key = scenario_key

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> HttpBackend:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        mutation: bool = False,
        operator: bool = False,
    ) -> Any:
        headers = {"Idempotency-Key": str(uuid.uuid4())} if mutation else {}
        if operator and self._scenario_key is not None:
            headers[_SCENARIO_KEY_HEADER] = self._scenario_key
        try:
            response = await self._client.request(
                method, path, params=params, json=json, headers=headers
            )
        except httpx.HTTPError as exc:
            raise BackendError(
                Problem(
                    problem_type="urn:acr:problem:transport",
                    title="API unreachable",
                    status=503,
                    detail=f"{type(exc).__name__}: {exc}",
                )
            ) from exc
        if response.status_code >= 400:
            raise BackendError(parse_problem(response))
        try:
            return response.json()
        except ValueError:
            raise _contract(f"{method} {path}", "response body is not JSON") from None

    async def search(self, query: str, locale: Locale, limit: int) -> SearchPage:
        where = "GET /v1/catalogue/search"
        data = await self._call(
            "GET",
            "/v1/catalogue/search",
            params={"q": query, "locale": locale.value, "limit": str(limit)},
        )
        shape = _Shape(data, where)
        return SearchPage(
            query=shape.str_("query"),
            locale=_locale(shape.str_("locale"), where),
            hits=tuple(
                _product(raw, f"{where}.hits[{i}]") for i, raw in enumerate(shape.list_("hits"))
            ),
            provenance=shape.freshness(),
        )

    async def product(self, sku: str) -> ProductCard:
        path = f"/v1/catalogue/products/{sku}"
        return _product(await self._call("GET", path), f"GET {path}")

    async def basket_create(self) -> BasketView:
        return _basket(await self._call("POST", "/v1/baskets", mutation=True), "POST /v1/baskets")

    async def basket_set_line(self, basket_id: str, sku: str, quantity: int) -> BasketView:
        path = f"/v1/baskets/{basket_id}/lines/{sku}"
        data = await self._call("PUT", path, json={"quantity": quantity}, mutation=True)
        return _basket(data, f"PUT {path}")

    async def basket_get(self, basket_id: str) -> BasketView:
        path = f"/v1/baskets/{basket_id}"
        return _basket(await self._call("GET", path), f"GET {path}")

    async def checkout_create(self, basket_id: str) -> ApprovalCard:
        path = f"/v1/baskets/{basket_id}/checkout"
        return _approval(await self._call("POST", path, mutation=True), f"POST {path}")

    async def checkout_get(self, checkout_id: str) -> CheckoutView:
        path = f"/v1/checkouts/{checkout_id}"
        return _checkout(await self._call("GET", path), f"GET {path}")

    async def checkout_submit_approved(
        self, checkout_id: str, version: int, content_hash: str
    ) -> KernelDecision:
        path = f"/v1/checkouts/{checkout_id}/versions/{version}/submit"
        data = await self._call("POST", path, json={"content_hash": content_hash}, mutation=True)
        return _decision(data, f"POST {path}")

    async def order_track(self, order_id: str) -> OrderView:
        path = f"/v1/orders/{order_id}"
        return _order(await self._call("GET", path), f"GET {path}")

    # ---- merchant surface -------------------------------------------------
    #
    # The same three reads :class:`InMemoryBackend` answers from the simulator, answered
    # here from the API. Both walk the whole catalogue rather than a page, because a
    # merchant asking how their catalogue is doing is asking about all of it, and a count
    # taken from the first page is how a console comes to report a category as empty when
    # it is merely off the end of the request.

    async def _fetch_catalogue_page(self, cursor: str | None) -> _CataloguePage:
        params = {"limit": str(_PAGE_LIMIT)}
        if cursor is not None:
            params["cursor"] = cursor
        data = await self._call("GET", "/v1/catalogue/products", params=params)
        return _catalogue_page(data, "GET /v1/catalogue/products")

    async def _walk_catalogue(self) -> _CatalogueWalk:
        """Page through the catalogue once, bounded, keeping the rows the counts need.

        No filter is sent: ``listed``/``available`` would each need their own walk, and
        one pass over the whole catalogue answers both questions plus the anomalies from
        the same rows. ``matched`` and ``counts_by_category`` are taken from the response
        rather than recomputed, since they already span the catalogue.

        The revision reported is the *lowest* seen across the pages. A price or a listing
        can be injected mid-walk, so the pages need not share a generation; the lowest is
        the only revision the whole count is guaranteed to be no older than, and a console
        comparing it against a fresher read then notices rather than being reassured.
        """
        where = "GET /v1/catalogue/products"
        page = await self._fetch_catalogue_page(None)
        rows: list[_CatalogueRow] = list(page.rows)
        revision = page.revision
        cursor = page.next_cursor
        pages = 1
        while cursor is not None and pages < _MAX_PAGES:
            page = await self._fetch_catalogue_page(cursor)
            rows.extend(page.rows)
            revision = min(revision, page.revision)
            if page.next_cursor == cursor:
                raise _contract(where, "next_cursor did not advance; the walk would not end")
            cursor = page.next_cursor
            pages += 1
        return _CatalogueWalk(
            rows=tuple(rows),
            total=page.matched,
            by_category=dict(sorted(page.counts_by_category.items())),
            revision=revision,
            complete=cursor is None,
        )

    async def catalogue_health(self) -> CatalogueHealth:
        """Listed, delisted, available and out of stock, counted across the catalogue.

        A delisted product is not counted as out of stock: it is not missing from the
        shelf, it has been taken off sale, and a merchant chasing restocks should not be
        handed a list of the latter.
        """
        walk = await self._walk_catalogue()
        listed = delisted = available = out_of_stock = 0
        for row in walk.rows:
            if row.is_listed:
                listed += 1
            else:
                delisted += 1
            if row.is_available:
                available += 1
            elif row.is_listed:
                out_of_stock += 1
        return CatalogueHealth(
            total=walk.total,
            listed=listed,
            delisted=delisted,
            available=available,
            out_of_stock=out_of_stock,
            by_category=walk.by_category,
            catalogue_revision=walk.revision,
        )

    async def inventory_anomalies(self, limit: int = 20) -> tuple[InventoryAnomaly, ...]:
        """Products worth a merchant's attention, most actionable first.

        Kinds, threshold and ordering are :class:`InMemoryBackend`'s, because a merchant
        agent must not rank its own findings differently depending on which backend it
        was handed. A listed product with nothing behind it is losing sales right now,
        which is more urgent than a delisted product still holding stock.

        The walk is bounded, so on a catalogue larger than the bound these are the
        anomalies among the rows read rather than among all of them. They are still the
        most urgent of those, because the sort runs after the whole walk rather than
        page by page -- stopping as soon as ``limit`` rows were collected would let a
        low-stock note on page one outrank an empty shelf on page two.

        Args:
            limit: How many anomalies to return, most actionable first.
        """
        walk = await self._walk_catalogue()
        anomalies: list[InventoryAnomaly] = []
        for row in sorted(walk.rows, key=lambda item: item.sku):
            if row.is_listed and row.stock_units == 0:
                anomalies.append(
                    InventoryAnomaly(row.sku, row.name, "listed_out_of_stock", {"stock_units": 0})
                )
            elif not row.is_listed and row.stock_units > 0:
                anomalies.append(
                    InventoryAnomaly(
                        row.sku, row.name, "delisted_with_stock", {"stock_units": row.stock_units}
                    )
                )
            elif row.is_listed and 0 < row.stock_units <= LOW_STOCK_UNITS:
                anomalies.append(
                    InventoryAnomaly(
                        row.sku, row.name, "low_stock", {"stock_units": row.stock_units}
                    )
                )
        order = {"listed_out_of_stock": 0, "delisted_with_stock": 1, "low_stock": 2}
        anomalies.sort(key=lambda row: (order.get(row.kind, 9), row.sku))
        return tuple(anomalies[:limit])

    async def _fetch_orders_page(self, cursor: str | None) -> _OrdersPage:
        params = {"limit": str(_PAGE_LIMIT)}
        if cursor is not None:
            params["cursor"] = cursor
        data = await self._call("GET", "/v1/orders", params=params, operator=True)
        return _orders_page(data, "GET /v1/orders")

    async def _walk_orders(self) -> _OrdersWalk:
        """Walk the order collection, bounded, keeping scope and counts from the first page.

        ``scope`` and ``counts`` span the whole collection and repeat on every page, so
        they are read once; the rows are walked only because the money each one carries is
        not pre-computed anywhere.
        """
        where = "GET /v1/orders"
        first = await self._fetch_orders_page(None)
        rows: list[_OrderRow] = list(first.rows)
        cursor = first.next_cursor
        pages = 1
        while cursor is not None and pages < _MAX_PAGES:
            page = await self._fetch_orders_page(cursor)
            rows.extend(page.rows)
            if page.next_cursor == cursor:
                raise _contract(where, "next_cursor did not advance; the walk would not end")
            cursor = page.next_cursor
            pages += 1
        return _OrdersWalk(
            rows=tuple(rows), scope=first.scope, counts=first.counts, complete=cursor is None
        )

    async def checkout_metrics(self) -> CheckoutMetrics:
        """Counts over orders and refunds, and the money this session is entitled to total.

        Three conditions have to hold before a money figure is reported, and each of them
        is a way the number would otherwise mean something narrower than its label:

        The scope must be ``tenant``. Without an operator scenario key these collections
        return the caller's own rows, and "your orders" summed under a heading that says
        "captured" is a merchant dashboard lying about its own revenue -- including when
        the caller has no orders and the sum would read as a confident zero.

        The walk must have completed. A prefix of the orders summed and presented as a
        total is the same lie with a smaller error bar.

        The rows must agree on a currency. :class:`CheckoutMetrics` carries one currency
        for one pair of sums, so a tenant selling in two of them has no honest total to
        report here; adding the minor units across currencies would produce a number that
        is not money at all.

        Where all three hold, the figures are integer minor units summed over rows the
        server sent, ``captured_minor`` from the orders and ``refunded_minor`` from each
        order's settled-refund sum. An order exists only where verified capture evidence
        put it there, which is what makes it a capture rather than a forecast.
        """
        orders = await self._walk_orders()
        where = "GET /v1/refunds"
        # One row: the page is fetched for ``counts``, which spans the scope regardless of
        # how much of it is returned, and dragging refund rows across the wire to throw
        # them away would be a slower way to learn nothing.
        refunds = _Shape(
            await self._call("GET", "/v1/refunds", params={"limit": "1"}, operator=True), where
        )
        _list_scope(refunds.str_("scope"), where)

        currencies = sorted({row.currency for row in orders.rows})
        single_currency = currencies[0] if len(currencies) == 1 else None
        measurable = (
            orders.scope == _TENANT_SCOPE
            and orders.complete
            and (single_currency is not None or not orders.rows)
        )
        return CheckoutMetrics(
            orders_total=sum(orders.counts.values()),
            orders_by_state=dict(sorted(orders.counts.items())),
            refunds_by_state=dict(sorted(refunds.counts("counts").items())),
            captured_minor=sum(row.amount_minor for row in orders.rows) if measurable else None,
            refunded_minor=sum(row.refunded_minor for row in orders.rows) if measurable else None,
            currency=single_currency or _SETTLEMENT_CURRENCY,
        )

    # ---- review queue -----------------------------------------------------

    async def support_cases(self, limit: int = 20) -> tuple[CaseSummary, ...]:
        """The tenant's human-review queue, most recently opened first.

        An operator read, so the scenario key rides on it exactly as it rides on the
        merchant collections. Without one the API refuses, and that refusal is left to the
        server rather than predicted here: the server owns who may open this queue, and a
        client that second-guessed it would start refusing reads the platform allows the
        first time that policy changed. The refusal arrives as a structured problem the
        agent explains, which is the gate working rather than the tool breaking.
        """
        where = "GET /v1/review/queue"
        data = await self._call(
            "GET", "/v1/review/queue", params={"limit": str(limit)}, operator=True
        )
        shape = _Shape(data, where)
        return tuple(
            _case_summary(raw, f"{where}.cases[{index}]")
            for index, raw in enumerate(shape.list_("cases"))
        )

    async def support_case(self, case_key: str) -> CaseRecord:
        """One case with the evidence specification 6.4.3 promises a reviewer.

        A key this tenant cannot see is the API's own 404, which is also its answer for a
        key belonging to somebody else. Nothing here turns that into an empty record: an
        empty record would still confirm that the key names a case somewhere.
        """
        where = f"GET /v1/review/queue/{case_key}"
        data = await self._call("GET", f"/v1/review/queue/{case_key}", operator=True)
        detail = _Shape(data, where)
        case = detail.obj("case")
        exposure = case.opt_obj("monetary_exposure")
        return CaseRecord(
            case_key=case.str_("case_key"),
            reason_code=_closed(
                RecoveryCode, case.str_("reason_code"), where=where, field="reason_code"
            ),
            state=_closed(CaseState, case.str_("state"), where=where, field="state"),
            priority=_closed(CasePriority, case.str_("priority"), where=where, field="priority"),
            provider_state_at_escalation=_verified_state(
                detail.obj("verified_provider_state"), f"{where}.verified_provider_state"
            ),
            proof_chain_ref=case.obj("proof_chain").opt_str("href"),
            monetary_exposure_minor=None if exposure is None else exposure.int_("minor"),
            # The currency labels an amount that is absent, never one this client derived,
            # which is the same thing the settlement label does on a metrics card.
            currency=_SETTLEMENT_CURRENCY if exposure is None else exposure.str_("currency"),
            opened_at=case.datetime_("opened_at"),
            target_response_by=case.datetime_("target_response_by"),
            timeline=tuple(
                _case_event(raw, f"{where}.timeline[{index}]")
                for index, raw in enumerate(detail.list_("timeline"))
            ),
            scope_note=detail.opt_str("scope") or "",
        )

    # ---- support surface --------------------------------------------------
    #
    # Both keyed by an order id and carrying no scenario key. That is the point of these
    # two routes existing at all: the same figures are on the operator review surface,
    # behind ``X-Scenario-Key`` and keyed by a payment attempt, and reaching them that way
    # from a buyer-facing agent would hand it findings about other people's stuck payments.
    # The bearer session is the whole of the scoping here, as it is for ``order_track``.

    async def order_policy(self, order_id: str) -> PolicyAtSale:
        """The rules this sale was made under, read from its Policy-at-Sale Receipt.

        A binding that did not verify arrives with no policies and the code that says why,
        and it is carried through as exactly that rather than raised: "nobody can say what
        this sale's rules were" is an answer the Support Specialist has to be able to give
        a buyer, and an exception here would turn it into an apology about a system fault.
        """
        where = f"GET /v1/orders/{order_id}/policy"
        data = await self._call("GET", f"/v1/orders/{order_id}/policy")
        shape = _Shape(data, where)
        return PolicyAtSale(
            order_id=shape.str_("order_id"),
            binding_ok=shape.bool_("binding_ok"),
            binding_code=_closed(
                RecoveryCode, shape.str_("binding_code"), where=where, field="binding_code"
            ),
            receipt_hash=shape.opt_str("policy_receipt_hash"),
            policies=tuple(
                _policy_term(raw, f"{where}.policies[{index}]")
                for index, raw in enumerate(shape.list_("policies"))
            ),
        )

    async def order_resolution(self, order_id: str) -> OrderResolution:
        """Every finding on this order, and the plan the Resolution Service would issue.

        ``findings`` is read from the response rather than counted from the list. The two
        agree today and :class:`OrderResolution` refuses them when they do not: a server
        that examined more than it could price is a disagreement worth surfacing, not one
        to paper over by counting locally and reporting a number the server never sent.
        """
        where = f"GET /v1/orders/{order_id}/resolution"
        data = await self._call("GET", f"/v1/orders/{order_id}/resolution")
        shape = _Shape(data, where)
        return OrderResolution(
            order_id=shape.str_("order_id"),
            recorded_state=shape.str_("recorded_state"),
            findings=shape.int_("findings"),
            plans=tuple(
                _resolution_plan(raw, f"{where}.resolutions[{index}]")
                for index, raw in enumerate(shape.list_("resolutions"))
            ),
            plan_ttl_seconds=shape.opt_int("plan_ttl_seconds"),
        )


def _policy_term(data: object, where: str) -> PolicyTerm:
    """One frozen merchant rule. ``terms`` is carried through, never interpreted here.

    ``kind`` goes through the enum that bounds it, so a policy family this platform cannot
    issue is a contract violation rather than a rule the agent would quote under a name
    nobody defined.
    """
    shape = _Shape(data, where)
    raw_terms = shape.raw("terms")
    if not isinstance(raw_terms, dict):
        raise _contract(where, "terms must be an object")
    return PolicyTerm(
        kind=_closed(PolicyKind, shape.str_("kind"), where=where, field="kind"),
        policy_id=shape.str_("policy_id"),
        policy_version=shape.int_("policy_version"),
        terms=dict(raw_terms),
        applies_to=tuple(str(target) for target in shape.list_("applies_to")),
    )


def _resolution_plan(data: object, where: str) -> ResolutionPlan:
    """One resolution, with every closed vocabulary read through the enum that bounds it.

    ``currency`` labels the whole plan and every option is checked against it by
    :class:`ResolutionPlan` itself, so a server that priced one option in another currency
    is refused here rather than quoted to a buyer in the currency they were expecting.
    """
    shape = _Shape(data, where)
    currency = shape.str_("currency")
    return ResolutionPlan(
        finding_id=shape.str_("finding_id"),
        code=_closed(RecoveryCode, shape.str_("code"), where=where, field="code"),
        plan_id=shape.opt_str("plan_id"),
        options=tuple(
            _remedy_option(raw, f"{where}.options[{index}]")
            for index, raw in enumerate(shape.list_("options"))
        ),
        withheld=tuple(
            _withheld_remedy(raw, f"{where}.withheld[{index}]")
            for index, raw in enumerate(shape.list_("withheld"))
        ),
        captured_minor=shape.int_("captured_minor"),
        refunds_reserved_minor=shape.int_("refunds_reserved_minor"),
        refundable_minor=shape.int_("refundable_minor"),
        currency=currency,
        explanation=shape.str_("explanation"),
        valid_until=shape.opt_datetime("valid_until"),
        recorded=shape.bool_("recorded"),
    )


def _remedy_option(data: object, where: str) -> RemedyOption:
    """One offered remedy, in the currency the *option* names rather than the plan's.

    Reading the currency off the plan and stamping it here would relabel a disagreeing
    amount instead of catching it. Taking the option's own and letting
    :class:`ResolutionPlan` compare the two turns a server that priced a remedy in another
    currency into a refusal, which is the only safe reading of that disagreement.
    """
    shape = _Shape(data, where)
    amount = shape.obj("amount")
    return RemedyOption(
        outcome=_closed(RemedyOutcome, shape.str_("outcome"), where=where, field="outcome"),
        amount=amount.money("minor", amount.str_("currency")),
        policy_kind=_closed(
            PolicyKind, shape.str_("policy_kind"), where=where, field="policy_kind"
        ),
        policy_id=shape.str_("policy_id"),
        policy_version=shape.int_("policy_version"),
        confirmation=_closed(
            RemedyConfirmation, shape.str_("confirmation"), where=where, field="confirmation"
        ),
        basis=shape.str_("basis"),
    )


def _withheld_remedy(data: object, where: str) -> WithheldRemedy:
    """One remedy considered and not offered, with the closed reason it was not."""
    shape = _Shape(data, where)
    return WithheldRemedy(
        outcome=_closed(RemedyOutcome, shape.str_("outcome"), where=where, field="outcome"),
        reason=_closed(WithheldReason, shape.str_("reason"), where=where, field="reason"),
        detail=shape.str_("detail"),
    )


def _case_summary(data: object, where: str) -> CaseSummary:
    """One queue row. Every closed vocabulary is read through the enum that bounds it."""
    shape = _Shape(data, where)
    exposure = shape.opt_obj("monetary_exposure")
    return CaseSummary(
        case_key=shape.str_("case_key"),
        reason_code=_closed(
            RecoveryCode, shape.str_("reason_code"), where=where, field="reason_code"
        ),
        state=_closed(CaseState, shape.str_("state"), where=where, field="state"),
        priority=_closed(CasePriority, shape.str_("priority"), where=where, field="priority"),
        opened_at=shape.datetime_("opened_at"),
        target_response_by=shape.datetime_("target_response_by"),
        monetary_exposure_minor=None if exposure is None else exposure.int_("minor"),
        currency=_SETTLEMENT_CURRENCY if exposure is None else exposure.str_("currency"),
    )


def _verified_state(shape: _Shape, where: str) -> str | None:
    """The provider state verified at escalation, or ``None`` if the provider never answered.

    ``present: false`` is the API saying there is no provider statement at all, and that
    is the only thing this returns ``None`` for. A statement that is present but names no
    state is a contract violation rather than a third answer: folding it into ``None``
    would report silence where the platform recorded an answer, and the difference between
    those two is exactly what a reviewer uses to decide whether to go and ask the provider.
    """
    if not shape.bool_("present"):
        return None
    state = shape.opt_str("status") or shape.opt_str("provider_status")
    if state is None:
        raise _contract(where, "a verified provider statement named no state")
    return state


def _case_event(data: object, where: str) -> CaseEvent:
    """One redacted timeline row, with the platform's own description of it.

    ``details`` is the audit payload the service already redacted. The four fields written
    after it are the platform's description of the row, and they are written *after* on
    purpose: a redacted payload that happened to carry a key called ``actor`` must not
    displace the actor the service recorded, because the two would be indistinguishable on
    a card and only one of them is evidence.
    """
    shape = _Shape(data, where)
    details = shape.raw("details")
    detail: dict[str, Any] = dict(details) if isinstance(details, dict) else {}
    detail.update(
        {
            "actor": shape.str_("actor"),
            "summary": shape.str_("summary"),
            "source": shape.str_("source"),
            "scenario_injection": shape.bool_("scenario_injection"),
        }
    )
    return CaseEvent(at=shape.datetime_("occurred_at"), event=shape.str_("action"), detail=detail)
