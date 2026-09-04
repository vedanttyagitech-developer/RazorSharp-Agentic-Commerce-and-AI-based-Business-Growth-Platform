"""Merchant simulator errors.

Two failure families, deliberately separated:

* **Caller bugs** raise. An unknown SKU, a zero quantity or a currency mismatch means the
  caller constructed something impossible; there is no buyer-facing recovery from it and
  swallowing it into a recovery code would hide a defect behind a polite sentence.
* **Merchant-state refusals** do not raise. They return a :class:`~transaction_kernel.
  RecoveryCode` inside a structured result, because the buyer genuinely can act on them
  (drop the item, pick a substitute, re-approve the new total).
"""

from __future__ import annotations

from commerce_domain import DomainError


class MerchantSimError(DomainError):
    """Base for every deterministic merchant-simulator failure."""


class UnknownSkuError(MerchantSimError):
    """A SKU that is not in the catalogue at all.

    Raised rather than returned as a recovery code because a model that invents a product
    ID must fail loudly. Specification 20.1 requires that a response may reference only
    catalogue IDs the merchant actually returned; a silent empty result would let a
    hallucinated ID pass through the basket as if it had merely gone out of stock.
    """


class InvalidBasketError(MerchantSimError):
    """A basket that cannot be priced under any merchant state: empty, non-positive
    quantity, or the same SKU listed twice.

    A duplicated SKU is rejected instead of being summed, because two lines for one SKU
    make the reservation quantity ambiguous: the kernel derives held units from the
    checkout content, and it must see exactly one authoritative quantity per SKU.
    """


class ScenarioError(MerchantSimError):
    """A demo injection that would corrupt merchant state or the injection log."""
