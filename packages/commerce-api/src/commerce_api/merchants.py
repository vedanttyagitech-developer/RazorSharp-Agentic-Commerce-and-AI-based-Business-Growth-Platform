"""The merchant simulator's state: one store per merchant, read from the database.

It used to live in this process, and that was the whole problem. ADR 0003 D14 made the
simulator authoritative for catalogue, inventory, price and fees and then kept that state
in memory, which cost two things every day:

* **The shop forgot itself.** Every restart reseeded the fixture, so a price a merchant
  set, a stock level a demonstration had built up to, and a running offer all vanished.
  On Kubernetes the API rolls with ``Recreate``, so a deploy did it silently, mid-demo,
  with nothing in the timeline saying the shop had moved.
* **Only one API process could exist.** A second would hold a second copy, so an injected
  price would exist for some requests and not others.

Now :mod:`commerce_api.merchant_state` keeps it in ``merchant_state`` and
``merchant_sku_state``, and this module builds a live :class:`merchant_sim.MerchantStore`
over whatever those rows say. The simulator itself is unchanged and still has no database:
it takes a snapshot at construction and hands one back, and every row, lock and transaction
stops at the module below this one.

**A store is built per call, inside the caller's transaction.** That is deliberate and it
is what makes the state trustworthy: a quote, the admission that revalidates it and the
audit event that records it all read the same rows in the same transaction, so none of them
can disagree about what the shop charged. A process-wide cache would reintroduce exactly
the drift this change removes, one process at a time.

**The lock moved to the database.** Mutations take the shop's row ``FOR UPDATE``, so the
revision compare-and-set is serialised between processes rather than between threads of
one. The reentrant lock this module used to hold is gone; it could never have defended
against a second replica, which is why the Deployment was pinned to one.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

from merchant_adapter import SimMerchantStateSource
from merchant_sim import MerchantSnapshot, MerchantStore, ScenarioController
from platform_db.tenancy import require_tenant
from sqlalchemy.orm import Session

from . import merchant_state

__all__ = ["DEFAULT_POLICY_VERSION", "MerchantRegistry"]

#: The merchant-policy version every simulated store reports to the kernel. It is stamped
#: into every content document and every Policy-at-Sale Receipt, so it changes only when
#: the simulator's fee policy genuinely changes shape.
DEFAULT_POLICY_VERSION: Final[str] = "sim-1"


class _Merchant:
    """One merchant's three collaborators, created together and never separately.

    They share a single :class:`MerchantStore`; a controller or state source built over a
    different store would inject into one world and revalidate against another.
    """

    __slots__ = ("scenario", "state_source", "store")

    def __init__(self, *, policy_version: str, snapshot: MerchantSnapshot | None) -> None:
        self.store = MerchantStore(snapshot=snapshot)
        self.scenario = ScenarioController(self.store)
        self.state_source = SimMerchantStateSource(self.store, policy_version=policy_version)


class MerchantRegistry:
    """Every simulated merchant this platform serves, read from the database on demand.

    Every accessor takes the caller's ``session`` and reads the shop's rows through it, so
    what a quote sees, what admission revalidates against and what an audit event records
    are one transaction's view of one set of rows.

    Use :meth:`store`, :meth:`state_source` and :meth:`scenario` for reads, and
    :meth:`mutating` for any change: it takes the shop's row ``FOR UPDATE`` and writes the
    result back before the block returns.
    """

    __slots__ = ("_policy_version",)

    def __init__(self, *, policy_version: str = DEFAULT_POLICY_VERSION) -> None:
        self._policy_version = policy_version

    def _hydrate(self, session: Session, merchant_id: uuid.UUID) -> _Merchant:
        """Build this merchant as the database currently describes it.

        A shop with no rows yet is opened at the catalogue fixture and that opening state
        is stored, once. Seeding on first sight rather than at provisioning keeps every
        existing caller working -- a tenant created before this table existed still gets a
        shop -- and ``seed_if_absent`` refuses a second seed, so a later restart cannot
        write fixture prices over a merchant's own.
        """
        tenant_id = require_tenant(session)
        snapshot = merchant_state.load(session, tenant_id=tenant_id, merchant_id=merchant_id)
        merchant = _Merchant(policy_version=self._policy_version, snapshot=snapshot)
        if snapshot is None:
            merchant_state.seed_if_absent(
                session,
                tenant_id=tenant_id,
                merchant_id=merchant_id,
                snapshot=merchant.store.snapshot(),
            )
        return merchant

    def store(self, session: Session, merchant_id: uuid.UUID) -> MerchantStore:
        """The authoritative catalogue, inventory, price and fee state for a merchant."""
        return self._hydrate(session, merchant_id).store

    def state_source(self, session: Session, merchant_id: uuid.UUID) -> SimMerchantStateSource:
        """What the kernel calls at admission step 8 to re-read merchant state.

        Handed to :func:`transaction_kernel.admit`. The kernel never imports the
        simulator; it receives this object, built over the rows this transaction can see.
        """
        return self._hydrate(session, merchant_id).state_source

    def scenario(self, session: Session, merchant_id: uuid.UUID) -> ScenarioController:
        """The injection controller. Every change it makes is labelled SCENARIO_INJECTION.

        For reads over a controller only. A change made through this accessor is applied
        to a store that is discarded when the call returns and is never written back;
        :meth:`mutating` is the one that persists.
        """
        return self._hydrate(session, merchant_id).scenario

    @contextmanager
    def mutating(self, session: Session, merchant_id: uuid.UUID) -> Iterator[ScenarioController]:
        """Change one merchant's state and store the result, in the caller's transaction.

        Usage::

            with registry.mutating(session, merchant_id) as scenario:
                injection = scenario.set_price(sku, new_price)

        The shop's row is taken ``FOR UPDATE`` first, so a second writer waits rather than
        racing the revision check the store performs. The write happens only on a clean
        exit: a refused injection leaves the store untouched by design, and persisting
        after an exception would store whatever half-state the failure left behind.

        Not committed here. The change and the audit event that explains it belong to one
        transaction, and this registry does not own it.
        """
        tenant_id = require_tenant(session)
        merchant_state.lock_for_update(session, tenant_id=tenant_id, merchant_id=merchant_id)
        merchant = self._hydrate(session, merchant_id)
        yield merchant.scenario
        merchant_state.save(
            session,
            tenant_id=tenant_id,
            merchant_id=merchant_id,
            snapshot=merchant.store.snapshot(),
        )

    def policy_version(self) -> str:
        """The merchant-policy version stamped into content and receipts."""
        return self._policy_version
