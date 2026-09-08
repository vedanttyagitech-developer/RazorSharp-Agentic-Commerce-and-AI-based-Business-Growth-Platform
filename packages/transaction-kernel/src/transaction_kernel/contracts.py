"""The two contracts that could not leave the kernel.

Everything else that used to live here is now in :mod:`commerce_domain.contracts`, because
the buyer copilot and the voice runtime needed to read it and must not be able to reach the
code that decides. These two stayed, and the reason is the same rule read the other way:
both name something only the kernel may do.

An :class:`Operation` is a provider mutation, and every one of them consumes exactly one
Execution Grant. A :class:`VerifiedAuthorityProof` is what entitles a caller to ask for one.
A package that cannot admit a payment has no use for either, and a package that holds them
is describing itself as one that can. Moving them down would have made the boundary tidier
and the claim weaker.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from commerce_domain import CheckoutRef, Money


class Operation(StrEnum):
    """Provider mutations. Each consumes exactly one Execution Grant."""

    PAYMENT_CREATE_ORDER = "PAYMENT_CREATE_ORDER"
    REFUND_EXECUTE = "REFUND_EXECUTE"
    RESERVE_DEBIT = "RESERVE_DEBIT"


@dataclass(frozen=True, slots=True)
class VerifiedAuthorityProof:
    """What a protocol gateway hands the kernel after verification, specification 13.1.

    The kernel never parses a JWS, SD-JWT or raw protocol payload. It receives this and
    independently re-checks every field against locked rows.
    """

    protocol: str
    protocol_version: str
    issuer: str
    subject: str
    key_id: str
    algorithm: str
    mandate_ref: str
    tenant_id: uuid.UUID
    merchant_id: uuid.UUID
    checkout: CheckoutRef
    amount: Money
    action: str
    expires_at: datetime
    authority_epoch: int
    verification_receipt_id: str
    correlation_id: uuid.UUID


__all__ = ["Operation", "VerifiedAuthorityProof"]
