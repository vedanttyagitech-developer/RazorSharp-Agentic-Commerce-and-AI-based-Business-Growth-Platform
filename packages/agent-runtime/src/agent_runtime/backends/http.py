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

A shape violation raises :class:`BackendError` with reason ``contract_violation`` rather
than a ``KeyError``: the agent gets a structured failure it can explain, and the log
names the endpoint and key.
"""

from __future__ import annotations

import uuid
from datetime import datetime
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
    CheckoutStatus,
    CheckoutView,
    CommerceBackend,
    OrderState,
    OrderView,
    PaymentSummary,
    PricedLine,
    Problem,
    ProductCard,
    Provenance,
    RefundRecord,
    SearchPage,
    UnavailableLine,
)

__all__ = ["HttpBackend", "parse_problem"]

_PROBLEM_MEDIA: Final[str] = "application/problem+json"
_CONTRACT: Final[str] = "urn:acr:problem:contract-violation"
#: ADR 0003 D13: provider transport timeout is 20 s; the API sits in front of it.
_DEFAULT_TIMEOUT_S: Final[float] = 20.0


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

    def provenance(self) -> Provenance:
        return Provenance(
            source=self.str_("source"),
            catalogue_revision=self.int_("catalogue_revision"),
            observed_at=self.opt_datetime("observed_at"),
        )

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
    shape = _Shape(data, where)
    currency = shape.str_("currency")
    return ProductCard(
        sku=shape.str_("sku"),
        name=shape.str_("name"),
        description=shape.opt_str("description") or "",
        category=shape.str_("category"),
        unit_label=shape.str_("unit_label"),
        unit_price=shape.money("unit_price_minor", currency),
        stock_units=shape.int_("stock_units"),
        is_listed=shape.bool_("is_listed"),
        is_available=shape.bool_("is_available"),
        provenance=shape.provenance(),
    )


def _locale(value: str, where: str) -> Locale:
    try:
        return Locale(value)
    except ValueError:
        raise _contract(where, f"unknown locale {value!r}") from None


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
            provenance=shape.provenance(),
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


# --------------------------------------------------------------------------- client


class HttpBackend(CommerceBackend):
    """Registry A over HTTP. Bearer session, one Idempotency-Key per mutation.

    A fresh UUID per mutation call is the honest choice for an agent surface: the agent
    does not own a retry loop (the kernel and the API do), so a replayed key would only
    ever come from a bug. ADR D9 makes a *different* key on a concurrent second submit
    yield ``DUPLICATE_OPERATION`` with the winner's attempt, which is the behaviour the
    checkout agent is written to handle.
    """

    def __init__(
        self,
        base_url: str,
        *,
        bearer: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json"},
            transport=transport,
            timeout=timeout,
        )

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
    ) -> Any:
        headers = {"Idempotency-Key": str(uuid.uuid4())} if mutation else {}
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
            provenance=shape.provenance(),
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
