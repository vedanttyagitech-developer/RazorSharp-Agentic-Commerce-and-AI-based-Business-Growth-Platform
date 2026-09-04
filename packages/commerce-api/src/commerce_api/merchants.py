"""The merchant simulator's state, one store per merchant, held in this process.

ADR 0003 D14: the simulator is authoritative for catalogue, inventory, price and fees
throughout the demonstration, and it keeps that state in memory. Two consequences follow,
and both are deliberate:

* **The API runs as one process.** :class:`commerce_api.settings.Settings` refuses
  ``WEB_CONCURRENCY > 1``. A second process would hold a second copy of this registry,
  so the price injected in step 5 would exist for some requests and not others, and the
  reapproval it is meant to trigger would fire at random. That is a demo restriction
  written down, not an architectural claim.

* **Mutations are serialised.** :class:`merchant_sim.MerchantStore` is a plain object
  with no internal locking, and FastAPI runs synchronous endpoints in a thread pool, so
  two scenario injections genuinely can land at once. Every mutation here happens under
  one reentrant lock. Reads are not locked: a store's mutation replaces values rather
  than rebuilding structures, so a read that races an injection sees the state from
  before or after it, never a half-applied one -- and the freshness stamp on what it
  returns says which.

The registry is keyed by ``merchant_id`` rather than being a singleton because the
platform is multi-tenant everywhere else, and a single global store would be the one
place a second tenant's injection changed the first tenant's prices.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

from merchant_sim import (
    MerchantStore,
    ScenarioController,
    SimMerchantStateSource,
)

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

    def __init__(self, *, policy_version: str) -> None:
        self.store = MerchantStore()
        self.scenario = ScenarioController(self.store)
        self.state_source = SimMerchantStateSource(self.store, policy_version=policy_version)


class MerchantRegistry:
    """Every simulated merchant this process serves, created on first use.

    Thread-safe for creation and for mutation. Hold :meth:`mutating` around any call that
    changes merchant state; use :meth:`store`, :meth:`state_source` and :meth:`scenario`
    for reads and for handing the kernel its state source.
    """

    __slots__ = ("_lock", "_merchants", "_policy_version")

    def __init__(self, *, policy_version: str = DEFAULT_POLICY_VERSION) -> None:
        self._policy_version = policy_version
        self._merchants: dict[uuid.UUID, _Merchant] = {}
        # Reentrant so a mutation helper may call another one without deadlocking.
        self._lock = threading.RLock()

    def _get(self, merchant_id: uuid.UUID) -> _Merchant:
        """Fetch or create, under the lock so two first requests make one store."""
        with self._lock:
            merchant = self._merchants.get(merchant_id)
            if merchant is None:
                merchant = _Merchant(policy_version=self._policy_version)
                self._merchants[merchant_id] = merchant
            return merchant

    def store(self, merchant_id: uuid.UUID) -> MerchantStore:
        """The authoritative catalogue, inventory, price and fee state for a merchant."""
        return self._get(merchant_id).store

    def state_source(self, merchant_id: uuid.UUID) -> SimMerchantStateSource:
        """What the kernel calls at admission step 8 to re-read merchant state.

        Handed to :func:`transaction_kernel.admit`. The kernel never imports the
        simulator; it receives this object.
        """
        return self._get(merchant_id).state_source

    def scenario(self, merchant_id: uuid.UUID) -> ScenarioController:
        """The injection controller. Every change it makes is labelled SCENARIO_INJECTION.

        Prefer :meth:`mutating`, which takes the lock. This accessor exists for reading
        the injection history, which needs no lock.
        """
        return self._get(merchant_id).scenario

    @contextmanager
    def mutating(self, merchant_id: uuid.UUID) -> Iterator[ScenarioController]:
        """Hold the registry lock while changing one merchant's state.

        Usage::

            with registry.mutating(merchant_id) as scenario:
                injection = scenario.set_price(sku, new_price)

        The lock is process-wide rather than per merchant. Injections are demo apparatus
        that happen a handful of times per run, so the contention costs nothing and one
        lock is one thing to reason about.
        """
        merchant = self._get(merchant_id)
        with self._lock:
            yield merchant.scenario

    def policy_version(self) -> str:
        """The merchant-policy version stamped into content and receipts."""
        return self._policy_version

    def known(self) -> tuple[uuid.UUID, ...]:
        """Merchants this process has materialised, oldest first. For diagnostics."""
        with self._lock:
            return tuple(self._merchants)

    def reset(self, merchant_id: uuid.UUID) -> None:
        """Discard a merchant's state so the next request rebuilds the baseline catalogue.

        Used between test cases and by the scenario controller's reset. Deliberately
        *not* reachable from an unauthenticated route: it would silently undo an
        injection a demonstration is standing on.
        """
        with self._lock:
            self._merchants.pop(merchant_id, None)
