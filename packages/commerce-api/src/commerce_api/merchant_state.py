"""Where the shop's live state is kept between requests, and between deployments.

The merchant simulator used to hold prices, stock, fees and its running offer in the API
process (ADR 0003 D14). Two costs were paid for that on every single day of the project,
and neither was visible from inside one run:

* **The shop forgot itself.** Every restart reseeded the fixture. On Kubernetes the API
  rolls with ``Recreate``, so a deploy quietly returned the store to its opening-day
  numbers -- mid-demonstration, with no event saying it had happened.
* **There could only be one API process**, because a second would hold a second copy and
  the two would disagree about what things cost.

So the state lives in ``merchant_state`` and ``merchant_sku_state``, and this module is the
only thing that reads or writes them. :mod:`merchant_sim` still has no database and no
session: it hands out a :class:`~merchant_sim.MerchantSnapshot` and accepts one back, and
everything about rows, locks and transactions stops here.

**The catalogue does not live here.** Names, units, tax rates and images are a fixture and
stay one; only the three numbers a merchant actually moves become rows. That is why a
snapshot read back is applied *over* the fixture rather than replacing it, and why a
product added to the fixture tomorrow appears in a shop seeded yesterday.

**Locking.** :func:`lock_for_update` takes the shop's row before a mutation, so the
revision compare-and-set that :meth:`merchant_sim.MerchantStore.mutate` performs is
serialised across processes rather than merely across threads. That is what makes more
than one API process possible; the store's own docstring used to have to say it was not
thread-safe, and the honest fix was never a bigger lock in Python.
"""

from __future__ import annotations

import uuid
from typing import Any

from commerce_domain import Money, uuid7
from merchant_sim import MerchantSnapshot
from merchant_sim.policy import FeePolicy, Promotion
from platform_db.schema_service import MerchantSkuState, MerchantState
from sqlalchemy import select
from sqlalchemy.orm import Session

__all__ = ["load", "lock_for_update", "save", "seed_if_absent"]


def _promotion_to_json(promotion: Promotion | None) -> dict[str, Any] | None:
    """One offer as a document. Null means no offer, not an offer worth nothing."""
    if promotion is None:
        return None
    return {
        "offer_id": promotion.offer_id,
        "label": promotion.label,
        "percent_bp": promotion.percent_bp,
        # Money is split rather than stored as a string: the minor unit is the
        # authoritative figure everywhere else in this system and a formatted amount read
        # back would have to be parsed, which is a rounding decision in disguise.
        "flat_minor": None if promotion.flat is None else promotion.flat.minor,
        "flat_currency": None if promotion.flat is None else promotion.flat.currency,
        "effective_from_epoch_ms": promotion.effective_from_epoch_ms,
        "effective_to_epoch_ms": promotion.effective_to_epoch_ms,
        "enabled": promotion.enabled,
        "currency": promotion.currency,
    }


def _promotion_from_json(document: dict[str, Any] | None) -> Promotion | None:
    if document is None:
        return None
    flat_minor = document.get("flat_minor")
    return Promotion(
        offer_id=str(document["offer_id"]),
        label=str(document["label"]),
        percent_bp=document.get("percent_bp"),
        flat=(
            None if flat_minor is None else Money(int(flat_minor), str(document["flat_currency"]))
        ),
        effective_from_epoch_ms=int(document.get("effective_from_epoch_ms", 0)),
        effective_to_epoch_ms=int(document.get("effective_to_epoch_ms", 0)),
        enabled=bool(document.get("enabled", True)),
        currency=str(document["currency"]),
    )


def _head(session: Session, tenant_id: uuid.UUID, merchant_id: uuid.UUID) -> MerchantState | None:
    return session.execute(
        select(MerchantState).where(
            MerchantState.tenant_id == tenant_id,
            MerchantState.merchant_id == merchant_id,
        )
    ).scalar_one_or_none()


def lock_for_update(
    session: Session, *, tenant_id: uuid.UUID, merchant_id: uuid.UUID
) -> MerchantState | None:
    """Take the shop's row before changing it, so two writers serialise.

    ``None`` means the shop has no stored state yet, which is not an error: the caller
    seeds it. The lock is on the head row alone -- one row per shop, taken in one place,
    so there is no order in which two mutations could each hold what the other wants.
    """
    return session.execute(
        select(MerchantState)
        .where(
            MerchantState.tenant_id == tenant_id,
            MerchantState.merchant_id == merchant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def load(
    session: Session, *, tenant_id: uuid.UUID, merchant_id: uuid.UUID
) -> MerchantSnapshot | None:
    """The shop as it was left, or ``None`` if it has never been stored.

    ``None`` and an empty shop are different answers and are kept different: a caller that
    treated "never stored" as "everything is zero" would open a store with no stock and no
    prices, which reads to a buyer as a shop that has closed.
    """
    head = _head(session, tenant_id, merchant_id)
    if head is None:
        return None
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
    return MerchantSnapshot(
        revision=int(head.revision),
        fee_policy=FeePolicy(
            base_delivery_fee=Money(int(head.delivery_fee_minor), str(head.currency)),
            free_delivery_threshold=Money(
                int(head.free_delivery_threshold_minor), str(head.currency)
            ),
            delivery_tax_bp=int(head.delivery_tax_bp),
            currency=str(head.currency),
        ),
        promotion=_promotion_from_json(head.promotion),
        prices={row.sku: Money(int(row.unit_price_minor), str(row.currency)) for row in rows},
        stock={row.sku: int(row.stock_units) for row in rows},
        listed={row.sku: bool(row.is_listed) for row in rows},
    )


def save(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    merchant_id: uuid.UUID,
    snapshot: MerchantSnapshot,
) -> None:
    """Write the whole shop back, in the caller's transaction.

    Whole rather than differential, and that is the point. The snapshot is what the store
    became after one labelled injection was applied to it; writing the fields somebody
    thought had changed would make this module a second implementation of what an
    injection means, and the two would disagree the first time a new injection kind was
    added.

    **Stock is not written here on an update.** ``merchant_sku_state.stock_units`` is a
    balance carried by :mod:`commerce_api.inventory`, and a snapshot writer that also set
    it would be a second way for the shelf to change -- one that leaves no movement behind
    and makes the ledger's sum disagree with the column. The opening value on an INSERT is
    the one exception, and it is written together with the ``RECEIVED`` movements that
    account for it.

    Flushed, not committed. The change and the audit event that explains it belong to one
    transaction, and this module does not own it.
    """
    head = _head(session, tenant_id, merchant_id)
    fee = snapshot.fee_policy
    if head is None:
        head = MerchantState(
            id=uuid7(),
            tenant_id=tenant_id,
            merchant_id=merchant_id,
            revision=snapshot.revision,
            currency=fee.currency,
            delivery_fee_minor=fee.base_delivery_fee.minor,
            free_delivery_threshold_minor=fee.free_delivery_threshold.minor,
            delivery_tax_bp=fee.delivery_tax_bp,
            promotion=_promotion_to_json(snapshot.promotion),
        )
        session.add(head)
    else:
        head.revision = snapshot.revision
        head.currency = fee.currency
        head.delivery_fee_minor = fee.base_delivery_fee.minor
        head.free_delivery_threshold_minor = fee.free_delivery_threshold.minor
        head.delivery_tax_bp = fee.delivery_tax_bp
        head.promotion = _promotion_to_json(snapshot.promotion)

    existing = {
        row.sku: row
        for row in session.execute(
            select(MerchantSkuState).where(
                MerchantSkuState.tenant_id == tenant_id,
                MerchantSkuState.merchant_id == merchant_id,
            )
        )
        .scalars()
        .all()
    }
    for sku, price in snapshot.prices.items():
        row = existing.get(sku)
        stock = int(snapshot.stock[sku])
        listed = bool(snapshot.listed[sku])
        if row is None:
            session.add(
                MerchantSkuState(
                    id=uuid7(),
                    tenant_id=tenant_id,
                    merchant_id=merchant_id,
                    sku=sku,
                    unit_price_minor=price.minor,
                    currency=price.currency,
                    stock_units=stock,
                    is_listed=listed,
                )
            )
        else:
            row.unit_price_minor = price.minor
            row.currency = price.currency
            # Deliberately not `row.stock_units`. See the docstring: the shelf moves only
            # through a movement, and this writer would move it without one.
            row.is_listed = listed
    session.flush()


def seed_if_absent(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    merchant_id: uuid.UUID,
    snapshot: MerchantSnapshot,
) -> bool:
    """Store a shop's opening state the first time, and never overwrite it.

    Returns whether anything was written. Provisioning a shop is the app role's job and
    changing what it charges is not, so this is the one write that role may make -- and it
    is refused a second time by the read below rather than by a grant, because a seed that
    ran twice would silently restore fixture prices over a merchant's own.
    """
    if _head(session, tenant_id, merchant_id) is not None:
        return False
    save(session, tenant_id=tenant_id, merchant_id=merchant_id, snapshot=snapshot)
    return True
