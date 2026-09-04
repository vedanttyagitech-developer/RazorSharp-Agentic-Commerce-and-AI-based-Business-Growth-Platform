"""Provenance and freshness stamps carried by every grounded merchant result.

Specification 6.2 and 20.1: search results, product lookups, inventory reads and quotes
must all carry *source* and *freshness*, so that an agent can never present merchant data
without saying where it came from and how current it was.

WHY FRESHNESS IS A REVISION, NOT A TIMESTAMP
--------------------------------------------
``catalogue_revision`` is the authoritative freshness token, and it is an integer that
increments on every change to merchant state. ``observed_at`` is descriptive only.

A wall-clock stamp cannot answer the question that actually matters -- "has anything
changed since I read this?" -- because a read taken one second ago is stale if a price
moved in that second, and a read taken an hour ago is perfectly current if nothing did.
Comparing revisions answers it exactly. It is also immune to clock skew between pods,
which is the same failure the kernel avoids by comparing expiry against the database
clock.

``observed_at`` therefore MUST NOT be used to decide anything. Nothing in this package
compares it, and no caller should: expiry and admission decisions belong to the
Transaction Assurance Kernel and are made against the database clock.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

#: Provenance identifier stamped on every grounded result this package produces.
#: It names the connector and its catalogue generation, so a Money Action Proof Chain can
#: show that a price came from the simulator rather than from a model.
SOURCE_ID: Final[str] = "merchant-sim:demo-grocery/v1"

#: A clock is a zero-argument callable returning an aware UTC datetime. It is injected so
#: that tests can freeze it and prove that no decision depends on it.
Clock = Callable[[], datetime]


def system_clock() -> datetime:
    """Wall clock, used only to stamp ``observed_at`` for display and audit.

    Guarantees an aware UTC value. Refuses to be load-bearing: see the module docstring.
    """
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Freshness:
    """Where a piece of merchant data came from and which catalogue generation it is."""

    source: str
    catalogue_revision: int
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.catalogue_revision < 0:
            raise ValueError("catalogue_revision must be non-negative")
        if self.observed_at.tzinfo is None:
            # A naive timestamp in evidence is unreconcilable across regions later.
            raise ValueError("observed_at must be timezone-aware")

    def is_stale_against(self, current_revision: int) -> bool:
        """True when merchant state has moved on since this datum was read.

        Any difference counts, not only an increase: a scenario reset lowers nothing but
        a restored baseline is still a different world than the one that was quoted.
        """
        return self.catalogue_revision != current_revision

    def to_payload(self) -> dict[str, str | int]:
        """Canonicalizable form for audit. Integers only, so JCS never sees a float."""
        return {
            "source": self.source,
            "catalogue_revision": self.catalogue_revision,
            # Epoch milliseconds: the integer-only JCS profile has no float, and an
            # ISO string would re-introduce formatting ambiguity into a signed payload.
            "observed_at_epoch_ms": int(self.observed_at.timestamp() * 1000),
        }
