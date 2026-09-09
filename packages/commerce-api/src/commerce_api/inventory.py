"""The inventory ledger: every unit that moved, and the balance those movements make.

A sold unit used to do nothing to the shop's stock number. ``stock_units`` was a figure
only a merchant could change, and the only trace of a sale was the reservation row that
had held the units -- so the platform held two half-answers to "how many are left", one
that never fell and one that only rose, and subtracted them at check time. It never
oversold. It also never settled, and a store eventually refused every checkout because its
sales had outgrown a number that had not moved since the day it was seeded.

So this module is the one place a unit ever moves. :func:`record` appends a movement and
carries the balance with it in the same statement pair, inside the caller's transaction.
Nothing else may write ``merchant_sku_state.stock_units`` -- not the snapshot writer, not
a service, not a test helper. That is the whole guarantee: the balance cannot disagree with
the ledger, because there is no way to change one without the other.

**Why keep a balance column at all**, when the sum is the truth. Because the sum is read on
every quote, every availability check and every reservation, and a ``SUM`` over a growing
ledger on each of those is a cost that only ever rises. The column is a maintained balance
in the ledger sense, not a second opinion, and
``test_capi_inventory_ledger.py::test_every_balance_equals_its_movements`` recomputes it
from the rows and fails on any drift. That test is the reason the column is allowed to
exist.

**A stock change from a merchant is a movement too.** ``STOCK_SET`` says "there are N now",
which is a statement about a quantity rather than about a delivery, so it is recorded as
``ADJUSTED`` for the difference -- the ledger keeps what actually changed, and the reason
key keeps why. A restock is ``RECEIVED``, and the two are not the same event even when the
resulting number is.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from enum import StrEnum
from typing import Final

from commerce_domain import uuid7
from platform_db.schema import CheckoutVersion
from platform_db.schema_service import Checkout, InventoryMovement, MerchantSkuState
from sqlalchemy import func, select
from sqlalchemy.orm import Session

__all__ = [
    "MovementKind",
    "balances",
    "open_shop",
    "record",
    "record_sale",
    "recomputed_balances",
]


class MovementKind(StrEnum):
    """Why units moved. Closed, and the sign of each is fixed by a CHECK on the table.

    ``ADJUSTED`` is the only kind that may go either way, which is exactly why it is the
    one a person signs for: a correction is somebody saying the shelf disagrees with the
    ledger, and that claim needs an author.
    """

    RECEIVED = "RECEIVED"
    SOLD = "SOLD"
    RETURNED = "RETURNED"
    ADJUSTED = "ADJUSTED"


#: Reason keys this module writes. Stable strings, never prose: a reason is read by code
#: and rendered by a surface, and a sentence stored here would be a sentence in one
#: language stored in a column that outlives the language choice.
REASON_SHOP_OPENED: Final[str] = "shop_opened"
REASON_LEDGER_OPENING: Final[str] = "ledger_opening"
REASON_SALE_ADMITTED: Final[str] = "sale_admitted"
REASON_MERCHANT_ADJUSTMENT: Final[str] = "merchant_adjustment"
REASON_MERCHANT_RECEIPT: Final[str] = "merchant_receipt"


def _balance_row(
    session: Session, *, tenant_id: uuid.UUID, merchant_id: uuid.UUID, sku: str
) -> MerchantSkuState | None:
    return session.execute(
        select(MerchantSkuState)
        .where(
            MerchantSkuState.tenant_id == tenant_id,
            MerchantSkuState.merchant_id == merchant_id,
            MerchantSkuState.sku == sku,
        )
        .with_for_update()
    ).scalar_one_or_none()


def record(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    merchant_id: uuid.UUID,
    sku: str,
    kind: MovementKind,
    units: int,
    reason: str,
    checkout_id: uuid.UUID | None = None,
    checkout_version: int | None = None,
    merchant_action_id: uuid.UUID | None = None,
) -> int:
    """Append one movement and carry the balance with it. Returns the new balance.

    ``units`` is signed and its sign must match ``kind``; the database enforces that, so a
    ``SOLD`` row that added stock is refused rather than quietly producing a balance that
    is right for the wrong reason.

    The balance row is taken ``FOR UPDATE`` first, so two concurrent movements on one SKU
    serialise. A movement that would take the shelf below zero fails on the table's own
    non-negative CHECK, which is the fail-closed direction: refusing a sale is recoverable
    and a negative shelf is not.

    Flushed, never committed. A movement and whatever explains it -- an admission, an
    approved merchant action -- belong to one transaction, and this module does not own it.
    """
    if units == 0:
        raise ValueError("a movement of zero units is not a movement")
    session.add(
        InventoryMovement(
            id=uuid7(),
            tenant_id=tenant_id,
            merchant_id=merchant_id,
            sku=sku,
            kind=kind.value,
            units=units,
            reason=reason,
            checkout_id=checkout_id,
            checkout_version=checkout_version,
            merchant_action_id=merchant_action_id,
        )
    )
    row = _balance_row(session, tenant_id=tenant_id, merchant_id=merchant_id, sku=sku)
    if row is None:
        raise LookupError(f"{sku} has no stock row in this shop; open the shop first")
    row.stock_units = int(row.stock_units) + units
    session.flush()
    return int(row.stock_units)


def open_shop(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    merchant_id: uuid.UUID,
    opening: Mapping[str, int],
) -> None:
    """Write a shop's opening balance as ``RECEIVED`` movements, once.

    Called from the same act that creates the shop's state rows, so the ledger and the
    balance start life agreeing. A SKU whose opening quantity is zero gets no row, because
    a delivery of nothing did not happen -- the balance is already zero and saying so twice
    would put a movement in the ledger that never occurred.
    """
    for sku, units in opening.items():
        if units <= 0:
            continue
        session.add(
            InventoryMovement(
                id=uuid7(),
                tenant_id=tenant_id,
                merchant_id=merchant_id,
                sku=sku,
                kind=MovementKind.RECEIVED.value,
                units=int(units),
                reason=REASON_SHOP_OPENED,
            )
        )
    session.flush()


def adopt_existing_balance(
    session: Session, *, tenant_id: uuid.UUID, merchant_id: uuid.UUID
) -> int:
    """Start the ledger for a shop that already had balances and no movements.

    Writes one ``ADJUSTED`` row per SKU for exactly what the shelf says today, so the sum
    matches the balance from the first moment the ledger exists. Returns how many rows it
    wrote.

    It does not invent the history it does not have. A shop that sold two hundred units
    before this table existed gets one row saying what is left, not two hundred saying what
    happened -- and the reason key says which of the two this is, so nobody later reads a
    reconstruction as a record.
    """
    if session.execute(
        select(func.count())
        .select_from(InventoryMovement)
        .where(
            InventoryMovement.tenant_id == tenant_id,
            InventoryMovement.merchant_id == merchant_id,
        )
    ).scalar_one():
        return 0
    rows = (
        session.execute(
            select(MerchantSkuState).where(
                MerchantSkuState.tenant_id == tenant_id,
                MerchantSkuState.merchant_id == merchant_id,
            )
        )
        .scalars()
        .all()
    )
    written = 0
    for row in rows:
        if int(row.stock_units) == 0:
            continue
        session.add(
            InventoryMovement(
                id=uuid7(),
                tenant_id=tenant_id,
                merchant_id=merchant_id,
                sku=row.sku,
                kind=MovementKind.ADJUSTED.value,
                units=int(row.stock_units),
                reason=REASON_LEDGER_OPENING,
            )
        )
        written += 1
    session.flush()
    return written


def balances(session: Session, *, tenant_id: uuid.UUID, merchant_id: uuid.UUID) -> dict[str, int]:
    """What the shop has, from the maintained balance. One row per SKU."""
    rows = session.execute(
        select(MerchantSkuState.sku, MerchantSkuState.stock_units).where(
            MerchantSkuState.tenant_id == tenant_id,
            MerchantSkuState.merchant_id == merchant_id,
        )
    ).all()
    return {str(row.sku): int(row.stock_units) for row in rows}


def recomputed_balances(
    session: Session, *, tenant_id: uuid.UUID, merchant_id: uuid.UUID
) -> dict[str, int]:
    """The same question answered from the ledger itself, by summing every movement.

    The balance column exists so that a quote does not pay for this sum. This function is
    how that shortcut is kept honest: a test recomputes every SKU and fails on any drift,
    and an operator can ask the same question of a live shop.
    """
    rows = session.execute(
        select(InventoryMovement.sku, func.sum(InventoryMovement.units).label("units"))
        .where(
            InventoryMovement.tenant_id == tenant_id,
            InventoryMovement.merchant_id == merchant_id,
        )
        .group_by(InventoryMovement.sku)
    ).all()
    return {str(row.sku): int(row.units) for row in rows}


def record_sale(
    session: Session, *, tenant_id: uuid.UUID, checkout_id: uuid.UUID, version: int
) -> int:
    """Take the sold units off the shelf, one movement per line. Returns how many lines.

    Called wherever the Kernel has just admitted a checkout and consumed its hold -- and it
    has to be called from *every* such place, because the hold stops defending those units
    the moment it is consumed. The partial unique index on ``SOLD`` rows makes a second
    call for the same version a refusal rather than a second sale, so a retried admission
    cannot sell the same units twice.

    Read from the checkout *version*, which is what was approved and what was admitted, so
    the units that leave the shelf are exactly the units the buyer agreed to buy.

    A refund does not put them back, and that is a decision rather than an omission: the
    money returns, and the goods do not walk back onto the shelf because a card was
    refunded. A return is its own movement, made when the goods actually arrive.
    """
    row = session.execute(
        select(Checkout.merchant_id, CheckoutVersion.content)
        .join(
            CheckoutVersion,
            (CheckoutVersion.tenant_id == Checkout.tenant_id)
            & (CheckoutVersion.checkout_id == Checkout.id)
            & (CheckoutVersion.version == version),
        )
        .where(Checkout.tenant_id == tenant_id, Checkout.id == checkout_id)
    ).one_or_none()
    if row is None:
        raise LookupError(f"checkout {checkout_id} version {version} cannot be read")
    lines = row.content.get("lines") if isinstance(row.content, dict) else None
    recorded = 0
    for line in lines if isinstance(lines, list) else []:
        sku = str(line.get("sku", ""))
        quantity = int(line.get("quantity", 0))
        if not sku or quantity <= 0:
            continue
        record(
            session,
            tenant_id=tenant_id,
            merchant_id=row.merchant_id,
            sku=sku,
            kind=MovementKind.SOLD,
            units=-quantity,
            reason=REASON_SALE_ADMITTED,
            checkout_id=checkout_id,
            checkout_version=version,
        )
        recorded += 1
    return recorded
