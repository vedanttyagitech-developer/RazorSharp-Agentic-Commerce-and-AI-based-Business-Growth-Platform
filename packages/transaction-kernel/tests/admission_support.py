"""Helpers shared by the admission suite and its conftest.

Lives beside conftest.py so tests import it by plain module name (pytest prepend import
mode puts this directory on sys.path). conftest.py must not be imported by tests.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from commerce_domain import Money
from sqlalchemy import text
from sqlalchemy.orm import Session
from transaction_kernel.admission import CurrentMerchantState
from transaction_kernel.contracts import AgentPrincipal, CheckoutRef

SET_TENANT = text("SELECT set_config('app.tenant_id', :t, true)")

APPROVED_TOTAL = Money(39500, "INR")


@dataclass(frozen=True, slots=True)
class Fixture:
    tenant_id: uuid.UUID
    merchant_id: uuid.UUID
    checkout: CheckoutRef
    principal: AgentPrincipal
    correlation_id: uuid.UUID


def _content(checkout_id: uuid.UUID, version: int, total: Money) -> dict[str, Any]:
    """The canonical checkout payload. Must match CurrentMerchantState.content_for_hash."""
    return {
        "checkout_id": str(checkout_id),
        "version": version,
        "currency": total.currency,
        "total_minor": total.minor,
        "line_items": {"sku_milk": 2, "sku_bread": 1},
        "policy_version": "pol-v12",
    }


class StubMerchant:
    """A merchant whose current state the test controls.

    ``unchanged`` reproduces exactly what was approved; the mutators simulate the merchant
    moving underneath an approval, which is the scenario the kernel exists for.
    """

    def __init__(
        self, checkout_id: uuid.UUID, total: Money = APPROVED_TOTAL, available: bool = True
    ) -> None:
        self._checkout_id = checkout_id
        self.total = total
        self.available = available
        #: ``None`` means "whatever the canonical content says". Set to ``{}`` to model a
        #: merchant that can supply nothing at all -- see :meth:`sell_out_everything`.
        self.line_items: dict[str, Any] | None = None

    def sell_out_everything(self) -> None:
        """Model a total sellout: nothing left, nothing to charge for.

        This is what a real connector reports when every approved line has gone --
        merchant-sim returns exactly this shape (zero total, empty allocation,
        ``all_available`` false) rather than a remainder to re-price.
        """
        self.available = False
        self.line_items = {}
        self.total = Money(0, self.total.currency)

    def revalidate(
        self, session: Session, *, checkout_id: uuid.UUID, version: int
    ) -> CurrentMerchantState:
        content = _content(checkout_id, version, self.total)
        return CurrentMerchantState(
            total=self.total,
            line_items=content["line_items"] if self.line_items is None else self.line_items,
            all_available=self.available,
            policy_version=content["policy_version"],
        )
