"""Replay and freshness guards, specification 16.3 and 13.1 step 3.

Two independent defences, and both are needed because each covers the other's blind spot.

The **freshness window** refuses a request whose timestamp is too old. It is cheap, it
needs no state, and it bounds how long a captured request stays useful. On its own it is
not enough: inside the window a captured request can be replayed as often as an attacker
likes.

The **nonce store** refuses a request whose nonce has been seen before. It closes that
window completely. On its own *it* is not enough either, because a store that must remember
every nonce ever seen grows without bound; it is the freshness window that makes forgetting
safe, since a nonce older than the window is refused by the clock regardless.

Together they are complete: a request must be recent *and* unseen.

Why this rides on ``idempotency_records``
-----------------------------------------
A nonce store has exactly one hard requirement -- that two concurrent presentations of the
same nonce cannot both win -- and that requirement is a uniqueness constraint, not a lookup.
An implementation that reads, finds nothing, and then inserts has a race between the read
and the insert wide enough to drive both requests through.

``transaction_kernel.idempotency`` already solves precisely this, against a real unique
index, with the loser blocking on the index until the winner's transaction ends. Building
a second mechanism beside it would mean writing that race condition again and hoping to get
it right the second time. So a nonce is claimed as an idempotency key, and the module's
three refusals -- replayed, reused with a different body, claimed but unresolved -- all
collapse into one answer here.

That collapse is the important difference between this module and its host, and it is
deliberate. Idempotency exists to make a retry *safe*: same key, same request, so return
what happened last time and do not do it twice. A replayed nonce is not a retry. Nothing
happened last time that the caller is entitled to, and answering with a stored response
would turn the nonce store into a cache that hands an attacker the original request's
result. Every prior sighting is therefore a refusal, and :class:`ReplayRejected` carries
``POLICY_EXCEPTION`` rather than ``DUPLICATE_OPERATION`` for that reason.

The clock
---------
Freshness is judged against the database's clock, read inside the caller's transaction,
never against the process clock. A pod with a skewed clock must not be able to widen or
narrow the window, and in a demo running on a laptop that has been asleep this is not a
theoretical concern.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from transaction_kernel.idempotency import (
    MAX_KEY_LENGTH,
    IdempotencyError,
    idempotent,
)

from .errors import ReplayRejected
from .pins import Protocol

__all__ = [
    "DEFAULT_MAX_REQUEST_AGE",
    "MAX_NONCE_LENGTH",
    "REPLAY_OPERATION",
    "assert_fresh",
    "claim_nonce",
    "database_now",
    "nonce_key",
]

#: Specification 16.3: "Five-minute maximum request-age window unless the pinned
#: specification is stricter." An adapter whose protocol pins something tighter passes its
#: own value; nothing may pass a looser one, which :func:`assert_fresh` enforces.
DEFAULT_MAX_REQUEST_AGE: Final[timedelta] = timedelta(minutes=5)

#: The ``operation`` recorded on the claim row. Kept under the kernel's 48-character limit
#: and distinct from every business operation, so protocol replay claims are trivially
#: separable from payment idempotency in the same table.
REPLAY_OPERATION: Final[str] = "PROTOCOL_REPLAY_GUARD"

#: The longest nonce a caller may present. The key column holds 128 characters and the
#: prefix below consumes some of them; refusing a longer nonce is better than truncating
#: one, because a truncated nonce silently collides with every nonce sharing its prefix.
MAX_NONCE_LENGTH: Final[int] = MAX_KEY_LENGTH - 32


def database_now(session: Session) -> datetime:
    """The database transaction clock. The only clock this module trusts."""
    return session.execute(select(func.now())).scalar_one()


def nonce_key(protocol: Protocol, client_id: str, nonce: str) -> str:
    """The idempotency key a nonce is claimed under.

    Scoped by protocol and by client, so two unrelated integrations cannot collide, and one
    client cannot burn another's nonce by guessing it. The tenant is not in the key because
    the kernel scopes the record by the transaction's bound tenant already -- adding it here
    would encode the same fact twice and let the two disagree.
    """
    return f"rp:{protocol.value}:{client_id}:{nonce}"


def assert_fresh(
    session: Session,
    *,
    timestamp: datetime,
    max_age: timedelta = DEFAULT_MAX_REQUEST_AGE,
) -> None:
    """Refuse a request that is too old, or dated in the future.

    Both directions are refused and the future one is not pedantry: a request timestamped
    an hour ahead would sit inside its own freshness window for an hour after its nonce
    expired from any bounded store, which is exactly the gap the two defences are supposed
    to close between them. A small allowance for genuine clock skew is folded into the same
    window rather than configured separately, because two knobs invite somebody to widen
    one of them.

    ``max_age`` may only ever be tightened. A caller passing something longer than
    specification 16.3's five minutes is refused, so a per-protocol override cannot become
    a way to opt out of the window.
    """
    if max_age > DEFAULT_MAX_REQUEST_AGE:
        raise ValueError(
            f"max_age {max_age} exceeds the specification 16.3 ceiling of "
            f"{DEFAULT_MAX_REQUEST_AGE}; a pinned protocol may be stricter, never looser"
        )
    now = database_now(session)
    if timestamp.tzinfo is None:
        raise ReplayRejected("request_timestamp_is_naive")
    age = now - timestamp
    if age > max_age:
        raise ReplayRejected(
            "request_outside_freshness_window",
            age_seconds=int(age.total_seconds()),
            max_age_seconds=int(max_age.total_seconds()),
        )
    if -age > max_age:
        raise ReplayRejected(
            "request_timestamp_in_the_future",
            skew_seconds=int((-age).total_seconds()),
            max_age_seconds=int(max_age.total_seconds()),
        )


def claim_nonce(
    session: Session,
    *,
    protocol: Protocol,
    client_id: str,
    nonce: str,
    request_digest: str,
) -> uuid.UUID:
    """Claim a nonce, or refuse the request as a replay.

    Returns the claim's record id on success. Raises :class:`ReplayRejected` if this nonce
    has been presented before in any state -- answered, refused, or still in flight.

    ``request_digest`` binds the nonce to the body it arrived with. A caller that replays a
    nonce with a *different* body is refused as a replay just the same, but the distinction
    is preserved in the rejection's details, because the two mean different things
    operationally: a repeated identical request is usually a retrying proxy, while a
    repeated nonce over changed content is somebody probing.

    The claim commits with the caller's transaction. A request that is refused later, or
    that fails, therefore leaves no claim behind and its nonce stays usable -- which is
    correct. Burning a nonce on a request the platform then refused would let anyone
    invalidate a legitimate caller's nonces by replaying them into a failing endpoint.
    """
    if not nonce:
        raise ReplayRejected("nonce_absent")
    if len(nonce) > MAX_NONCE_LENGTH:
        raise ReplayRejected("nonce_too_long", length=len(nonce), maximum=MAX_NONCE_LENGTH)

    key = nonce_key(protocol, client_id, nonce)
    try:
        with idempotent(session, key, REPLAY_OPERATION, {"digest": request_digest}) as slot:
            # The claim itself is the whole point; there is no result to remember. An empty
            # response is stored because the kernel refuses to commit a claim whose outcome
            # was never recorded, and "seen" is the entire outcome a nonce guard has.
            slot.store({})
            return slot.record_id
    except IdempotencyError as exc:
        raise ReplayRejected(
            "nonce_already_presented",
            protocol=protocol.value,
            client_id=client_id,
            refusal=type(exc).__name__,
        ) from exc
