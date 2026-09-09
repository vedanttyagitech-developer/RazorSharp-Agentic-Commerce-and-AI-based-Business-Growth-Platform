"""HTTP backend over the ADR 0003 endpoint catalogue.

The API is being built concurrently, so this module is written against the *contract*
-- the endpoint table in ``docs/adr/0003-service-layer.md`` plus D9 (idempotency) and
D15 (RFC 9457 problems) -- and unit-tested against ``httpx.MockTransport``. The JSON
shapes below are what this client expects; they mirror ``Quote.to_checkout_content()``
and ``AdmissionDecision`` field for field, so the API can produce them without inventing a
second vocabulary.

WIRE SHAPES (expected)
----------------------
product card::

    {"sku", "name", "description"?, "category", "unit_label", "unit_price_minor",
     "currency", "stock_units", "is_listed", "is_available",
     "source", "catalogue_revision", "observed_at"?}

search page  ``GET /v1/catalogue/search?q=&locale=&limit=``::

    {"query", "locale", "hits": [product card...], "source", "catalogue_revision"}

quote (inside a cart view or approval card)::

    {"currency", "lines": [{"sku", "name", "quantity", "unit_price_minor",
     "subtotal_minor", "tax_bp", "tax_minor"}], "items_subtotal_minor",
     "items_tax_minor", "delivery_fee_minor", "delivery_tax_minor", "total_minor",
     "free_delivery_applied", "gap_to_free_delivery_minor",
     "free_delivery_threshold_minor"?, "content_hash", "source", "catalogue_revision"}

cart view  ``POST /v1/carts``, ``PUT /v1/carts/{id}/lines/{sku}``, ``GET /v1/carts/{id}``::

    {"cart_id", "code", "lines": [{"sku", "quantity"}], "quote": quote|null,
     "unavailable": [{"sku", "requested", "available_units", "listed"}],
     "stale", "source", "catalogue_revision"}

approval card  ``POST /v1/carts/{id}/checkout``::

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
from datetime import datetime
from enum import StrEnum
from typing import Any, Final
from urllib.parse import quote

import httpx
from commerce_domain import AdmissionDecision, CheckoutRef, Delta, Money, RecoveryCode
from merchant_sim import Locale

from .base import (
    ApprovalCard,
    BackendError,
    CartQuote,
    CartView,
    CheckoutStatus,
    CheckoutView,
    CommerceBackend,
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
    SupportCase,
    UnavailableLine,
    WithheldReason,
    WithheldRemedy,
)

__all__ = ["HttpBackend", "parse_problem"]

_PROBLEM_MEDIA: Final[str] = "application/problem+json"
_CONTRACT: Final[str] = "urn:acr:problem:contract-violation"
#: ADR 0003 D13: provider transport timeout is 20 s; the API sits in front of it.
_DEFAULT_TIMEOUT_S: Final[float] = 20.0

#: ADR 0003 D11's stand-in for the merchant operator surface. Named here rather than
#: imported because ``commerce-api`` sits above this package in the dependency order
#: (ADR 0003 D2); it is a wire header like the paths above it.
_SCENARIO_KEY_HEADER: Final[str] = "X-Scenario-Key"


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


def _quote(data: object, where: str) -> CartQuote:
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
        return CartQuote(
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
            discount=shape.money("discount_minor", currency),
            offer_label=shape.opt_str("offer_label"),
        )
    except ValueError as exc:
        raise _contract(where, str(exc)) from None


def _code(value: str, where: str) -> RecoveryCode:
    try:
        return RecoveryCode(value)
    except ValueError:
        # Specification 6.7: an agent may not act on a code it does not recognise.
        raise _contract(where, f"unknown recovery code {value!r}") from None


def _basket(data: object, where: str) -> CartView:
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
        return CartView(
            cart_id=shape.str_("cart_id"),
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
        reference=shape.str_("reference"),
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


def _decision(data: object, where: str) -> AdmissionDecision:
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
        return AdmissionDecision(
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


# --------------------------------------------------------------------------- client


def _seg(value: object) -> str:
    """One path segment, percent-encoded. Every id in a URL below goes through this.

    The ids interpolated into these paths are chosen by a model. A SKU or an order
    reference carrying ``/``, ``..``, ``?`` or ``#`` used to be spliced into the path raw,
    so the value decided which route the request reached -- and it reached it over the
    session's own authenticated client. ``safe=""`` because nothing in a platform
    identifier is a path separator: a segment is one segment.
    """
    return quote(str(value), safe="")


class HttpBackend(CommerceBackend, SupportBackend):
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
        path = f"/v1/catalogue/products/{_seg(sku)}"
        return _product(await self._call("GET", path), f"GET {path}")

    async def basket_create(self) -> CartView:
        return _basket(await self._call("POST", "/v1/carts", mutation=True), "POST /v1/carts")

    async def basket_set_line(self, cart_id: str, sku: str, quantity: int) -> CartView:
        path = f"/v1/carts/{_seg(cart_id)}/lines/{_seg(sku)}"
        data = await self._call("PUT", path, json={"quantity": quantity}, mutation=True)
        return _basket(data, f"PUT {path}")

    async def basket_get(self, cart_id: str) -> CartView:
        path = f"/v1/carts/{_seg(cart_id)}"
        return _basket(await self._call("GET", path), f"GET {path}")

    async def checkout_create(self, cart_id: str) -> ApprovalCard:
        path = f"/v1/carts/{_seg(cart_id)}/checkout"
        return _approval(await self._call("POST", path, mutation=True), f"POST {path}")

    async def checkout_get(self, checkout_id: str) -> CheckoutView:
        path = f"/v1/checkouts/{_seg(checkout_id)}"
        return _checkout(await self._call("GET", path), f"GET {path}")

    async def checkout_submit_approved(
        self, checkout_id: str, version: int, content_hash: str
    ) -> AdmissionDecision:
        path = f"/v1/checkouts/{_seg(checkout_id)}/versions/{_seg(version)}/submit"
        data = await self._call("POST", path, json={"content_hash": content_hash}, mutation=True)
        return _decision(data, f"POST {path}")

    async def order_track(self, order_id: str) -> OrderView:
        path = f"/v1/orders/{_seg(order_id)}"
        return _order(await self._call("GET", path), f"GET {path}")

    # ---- merchant surface -------------------------------------------------
    #
    # Both keyed by an order id and carrying no scenario key. That is the point of these
    # two routes existing at all: the same figures are on the operator review surface,
    # behind ``X-Scenario-Key`` and keyed by a payment attempt, and reaching them that way
    # from a buyer-facing agent would hand it findings about other people's stuck payments.
    # The bearer session is the whole of the scoping here, as it is for ``order_track``.

    async def open_support_case(self, order_id: str, reason: str, note: str) -> SupportCase:
        """Raise a case for a person to answer, and decide nothing about it.

        A mutation, and the only one on this surface. The platform is idempotent by state
        rather than by key here: a buyer with a case already open on this order is handed
        that case back, so an agent asked twice adds nothing to the merchant's queue.
        """
        where = f"POST /v1/orders/{order_id}/support-cases"
        data = await self._call(
            "POST",
            f"/v1/orders/{_seg(order_id)}/support-cases",
            json={"reason": reason, "note": note},
            mutation=True,
        )
        shape = _Shape(data, where)
        return SupportCase(
            case_id=shape.str_("case_id"),
            order_id=shape.str_("order_id"),
            reason=shape.str_("reason"),
            status=shape.str_("status"),
        )

    async def order_policy(self, order_id: str) -> PolicyAtSale:
        """The rules this sale was made under, read from its Policy-at-Sale Receipt.

        A binding that did not verify arrives with no policies and the code that says why,
        and it is carried through as exactly that rather than raised: "nobody can say what
        this sale's rules were" is an answer the Support Specialist has to be able to give
        a buyer, and an exception here would turn it into an apology about a system fault.
        """
        where = f"GET /v1/orders/{order_id}/policy"
        data = await self._call("GET", f"/v1/orders/{_seg(order_id)}/policy")
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
        data = await self._call("GET", f"/v1/orders/{_seg(order_id)}/resolution")
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
