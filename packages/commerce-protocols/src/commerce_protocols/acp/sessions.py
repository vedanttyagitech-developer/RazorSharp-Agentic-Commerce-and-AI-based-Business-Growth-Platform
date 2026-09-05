"""ACP checkout sessions, translated into the one internal vocabulary, specification 16.1.

An ACP checkout session is a mutable document an external AI buyer edits until it is happy
and then completes. This platform has no such object. It has baskets, immutable hashed
checkout versions, reservations, approvals recorded on a trusted surface, and a kernel that
admits payment. The whole of this module is the translation between those two pictures, and
the translation is where the interesting failures live: every invariant this platform keeps
is a sentence about *its* objects, and an adapter that mapped sloppily would keep none of
them while appearing to work.

The mapping
-----------
Five operations, and each becomes exactly one
:class:`~commerce_protocols.core.intent.ProtocolIntent`:

===========================  ==========================================================
ACP operation                Internal intent
===========================  ==========================================================
create a session             ``BUILD_BASKET``, or ``CREATE_CHECKOUT`` if complete at once
update a session             ``BUILD_BASKET``, or ``CREATE_CHECKOUT`` when it completes
retrieve a session           ``TRACK_ORDER``
complete a session           ``SUBMIT_APPROVED``
cancel a session             ``PROPOSE_CANCELLATION``
===========================  ==========================================================

Two rows deserve their reasoning written down.

**Construction takes the furthest intent.** One ACP update can be two internal steps: it
amends the basket *and*, if it supplies the last missing piece, freezes an immutable
checkout version with its receipt and reservation. The intent names the further of the two,
because the intent is what capability and authority get checked against, and checking a
request against the weaker of the two things it asks for is checking the wrong thing.

**Retrieval is ``TRACK_ORDER``.** The closed intent vocabulary has one read that means "tell
me the state of the thing I asked for", and this is it. Reading a session that has not been
completed answers with the checkout's state and no capture evidence, because there is none
yet. The alternative was to widen ``IntentKind`` -- a change to the protocol-neutral core,
made to save this paragraph -- and the property that matters is preserved either way:
``TRACK_ORDER`` is in ``READ_ONLY_INTENTS``, so a retrieval is a read to every layer below,
which is exactly what it is.

What an ACP completion is not
-----------------------------
It is not consent. ACP's own ``complete`` carries payment credentials the external platform
collected from its user, and a naive adapter would treat their presence as the buyer having
agreed. This one does not: :func:`map_request` refuses a completion unless *this* platform
holds a recorded approval, taken on its own trusted surface, against the exact version and
content hash being completed. The external caller cannot form an approval -- there is no
``APPROVE`` in ``IntentKind`` and no consent capability in ``PROTOCOL_CAPABILITIES`` -- so
the refusal is not a policy that could be relaxed by a later edit; it is the absence of a
representable request. :func:`approval_handoff` is how a session that has become ready is
answered: with a pointer at the trusted surface, and an intent that asks a human.

One deliberate divergence, and why the pin allows it
----------------------------------------------------
ACP's completion names a session id and nothing else. This surface additionally requires the
caller to echo the checkout version and content hash it believes it is completing. A session
id names a mutable thing; a version and a hash name an immutable one, and only the second
can be compared against what a human approved. A caller that cannot say which version it is
completing has not re-read the session since it last changed, which is precisely the stale
state that :class:`~commerce_protocols.core.StateRejected` exists to report.

That is a narrowing of the public contract, and it is inside the claim rather than outside
it: specification 13.2 pins ACP at ``COMPATIBLE_INTERFACE``, not ``LOCAL_CONFORMANCE``, and
:mod:`commerce_protocols.acp.claims` is where that distinction is enforced. A surface
claiming full schema conformance could not do this. This one says what it is.

Invariants preserved across the boundary
----------------------------------------
Specification 16.1 requires that the same version, approval, revocation, idempotency and
payment-state invariants hold here as anywhere else. They are enforced in that order in
:func:`map_request`, and the order is chosen so the most recoverable refusal is reported
first: a caller told "your version is stale" re-reads and retries, whereas one told "your
authority is revoked" needs a human.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

from commerce_domain import Money
from sqlalchemy.orm import Session
from transaction_kernel.idempotency import MAX_KEY_LENGTH

from ..core import (
    IntentKind,
    MandateRejected,
    ProtocolIntent,
    SchemaRejected,
    StateRejected,
    database_now,
)
from .auth import AdmittedRequest

__all__ = [
    "ALLOWED_FROM",
    "MUTATIONS",
    "MUTATION_OPERATION",
    "REQUIRED_FOR_READINESS",
    "TERMINAL_STATUSES",
    "AcpOperation",
    "AcpRoute",
    "AcpSession",
    "AcpSessionStatus",
    "approval_handoff",
    "idempotency_key_for",
    "map_request",
    "parse_amount",
    "route",
]

#: Where this surface lives. One constant, because the signature covers the path and a
#: router that disagreed with the signer about the prefix would refuse every request with
#: a message about cryptography.
BASE_PATH: Final[str] = "/acp/checkout_sessions"

#: The ``operation`` recorded on an ACP mutation's idempotency row. Within the kernel's
#: 48-character column and distinct from every business operation, so protocol idempotency
#: is separable from payment idempotency in the same table.
MUTATION_OPERATION: Final[str] = "ACP_SESSION_MUTATION"

#: An ACP session id as this surface will accept it. Opaque to us, but bounded and free of
#: path syntax: an id containing a slash or a dot segment would let the routing table and
#: the signed path disagree about which resource was named.
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class AcpOperation(StrEnum):
    """The ACP checkout lifecycle, as endpoints rather than as states."""

    CREATE_SESSION = "CREATE_SESSION"
    UPDATE_SESSION = "UPDATE_SESSION"
    RETRIEVE_SESSION = "RETRIEVE_SESSION"
    COMPLETE_SESSION = "COMPLETE_SESSION"
    CANCEL_SESSION = "CANCEL_SESSION"


class AcpSessionStatus(StrEnum):
    """Where a session is in its life.

    ``IN_PROGRESS`` is the payment-state invariant made visible: a completion that reached
    kernel admission has a payment attempt in flight, and until that attempt reaches a
    terminal state nothing may amend, re-complete or cancel the session. Without this
    member the window between "admitted" and "captured" would be a window in which an
    external caller could change what was being paid for.
    """

    NOT_READY_FOR_PAYMENT = "NOT_READY_FOR_PAYMENT"
    READY_FOR_PAYMENT = "READY_FOR_PAYMENT"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELED = "CANCELED"


#: Nothing moves out of these.
TERMINAL_STATUSES: Final[frozenset[AcpSessionStatus]] = frozenset(
    {AcpSessionStatus.COMPLETED, AcpSessionStatus.CANCELED}
)

#: Which statuses each operation may be attempted from. A closed table rather than a chain
#: of ``if`` statements, so "can an external caller cancel a session whose payment is in
#: flight" is answered by reading one line instead of by tracing control flow.
ALLOWED_FROM: Final[Mapping[AcpOperation, frozenset[AcpSessionStatus]]] = MappingProxyType(
    {
        AcpOperation.UPDATE_SESSION: frozenset(
            {AcpSessionStatus.NOT_READY_FOR_PAYMENT, AcpSessionStatus.READY_FOR_PAYMENT}
        ),
        AcpOperation.COMPLETE_SESSION: frozenset({AcpSessionStatus.READY_FOR_PAYMENT}),
        AcpOperation.CANCEL_SESSION: frozenset(
            {AcpSessionStatus.NOT_READY_FOR_PAYMENT, AcpSessionStatus.READY_FOR_PAYMENT}
        ),
        AcpOperation.RETRIEVE_SESSION: frozenset(AcpSessionStatus),
    }
)

#: Operations that change something, and therefore require an ``Idempotency-Key`` (16.3).
MUTATIONS: Final[frozenset[AcpOperation]] = frozenset(
    {
        AcpOperation.CREATE_SESSION,
        AcpOperation.UPDATE_SESSION,
        AcpOperation.COMPLETE_SESSION,
        AcpOperation.CANCEL_SESSION,
    }
)

#: What a session needs before it can be frozen into an approvable checkout version. Items
#: to price, a buyer to attribute it to, a fulfilment target to compute tax and shipping
#: against; without all three there is nothing stable for a human to approve.
REQUIRED_FOR_READINESS: Final[frozenset[str]] = frozenset({"items", "buyer", "fulfillment"})


@dataclass(frozen=True, slots=True)
class AcpRoute:
    """One resolved endpoint: what was asked, of which session, and whether it mutates.

    ``requires_idempotency_key`` is carried here rather than recomputed at the gate so that
    the routing table is the single place that decides which endpoints change something.
    :func:`~commerce_protocols.acp.auth.admit` takes the answer as an argument precisely so
    it cannot guess, and this is where the answer comes from.
    """

    operation: AcpOperation
    session_id: str | None
    requires_idempotency_key: bool


@dataclass(frozen=True, slots=True)
class AcpSession:
    """This platform's view of one ACP session, as the adapter needs to see it.

    Not a persistence model. The durable representation of a checkout belongs to the
    services below this layer, and inventing a second one here would be inventing a second
    truth about what a buyer approved. This is the projection :func:`map_request` needs to
    answer "may this request proceed", and every field on it is one this module reads.

    ``approved_version`` and ``approval_id`` come from the trusted surface and from nowhere
    else. An ACP caller cannot set them, cannot see a path that would, and would not be
    believed if it sent them: they are arguments to this function, not fields of a request.
    """

    session_id: str
    status: AcpSessionStatus
    supplied: frozenset[str] = frozenset()
    checkout_id: uuid.UUID | None = None
    checkout_version: int | None = None
    content_hash: str | None = None
    amount: Money | None = None
    approved_version: int | None = None
    approval_id: uuid.UUID | None = None
    approval_expires_at: datetime | None = None
    #: The authority epoch this session's approval was taken under. A revocation anywhere
    #: in the platform bumps the current epoch, and a session carrying an older one is
    #: refused rather than silently honoured.
    authority_epoch: int = 0
    order_id: uuid.UUID | None = None


# ----------------------------------------------------------------------------- routing


def route(method: str, path: str) -> AcpRoute:
    """Resolve a method and path to one ACP operation, or refuse.

    The path is required to already be canonical -- no empty segments, no trailing slash,
    no dot segments. Normalising it here instead would mean the bytes the signature covers
    and the resource this process acts on could differ, and every interesting path-handling
    vulnerability of the last twenty years lives in exactly that gap.
    """
    segments = tuple(s for s in path.split("/") if s)
    if path != "/" + "/".join(segments) or any(s in {".", ".."} for s in segments):
        raise SchemaRejected("acp_path_not_canonical", path=path)
    if segments[:2] != ("acp", "checkout_sessions"):
        raise SchemaRejected("acp_unknown_endpoint", path=path)

    rest = segments[2:]
    verb = method.upper()
    if not rest:
        return _routed(AcpOperation.CREATE_SESSION, None, expected="POST", method=verb, path=path)

    session_id = rest[0]
    if not _SESSION_ID.match(session_id):
        raise SchemaRejected("acp_session_id_malformed", length=len(session_id))
    if len(rest) == 1:
        operation = AcpOperation.RETRIEVE_SESSION if verb == "GET" else AcpOperation.UPDATE_SESSION
        expected = "GET" if verb == "GET" else "POST"
        return _routed(operation, session_id, expected=expected, method=verb, path=path)
    if len(rest) == 2 and rest[1] == "complete":
        return _routed(
            AcpOperation.COMPLETE_SESSION, session_id, expected="POST", method=verb, path=path
        )
    if len(rest) == 2 and rest[1] == "cancel":
        return _routed(
            AcpOperation.CANCEL_SESSION, session_id, expected="POST", method=verb, path=path
        )
    raise SchemaRejected("acp_unknown_endpoint", path=path)


def _routed(
    operation: AcpOperation, session_id: str | None, *, expected: str, method: str, path: str
) -> AcpRoute:
    if method != expected:
        raise SchemaRejected("acp_method_not_allowed", method=method, path=path)
    return AcpRoute(
        operation=operation,
        session_id=session_id,
        requires_idempotency_key=operation in MUTATIONS,
    )


# ------------------------------------------------------------------------- idempotency


def idempotency_key_for(client_id: str, operation: AcpOperation, presented: str) -> str:
    """The key an ACP mutation is claimed under, scoped so two callers cannot collide.

    Scoped by client so one integration cannot burn another's key by guessing it, and by
    operation because the same key on a session update and on that session's completion is
    two different operations and must not replay one another's answer. The tenant is absent
    for the reason :func:`~commerce_protocols.core.replay.nonce_key` gives: the kernel
    scopes the record by the transaction's bound tenant already, and encoding the same fact
    twice lets the two disagree.
    """
    key = f"acp:{client_id}:{operation.value}:{presented}"
    if len(key) > MAX_KEY_LENGTH:
        raise SchemaRejected(
            "acp_idempotency_key_too_long", length=len(key), maximum=MAX_KEY_LENGTH
        )
    return key


# ---------------------------------------------------------------------------- amounts


def parse_amount(body: Mapping[str, Any], *, field: str = "total") -> Money:
    """Read an amount as integer minor units, refusing every other shape.

    JSON has one number type and ``json.loads`` turns ``3950.00`` into a float, so a body
    that looks correct to a human arrives here as something that cannot represent a rupee
    exactly. Refusing it is the only honest answer: rounding would silently change what a
    buyer is charged, and this platform's rule is that no float ever touches an amount.

    ``bool`` is refused as well. It is a subclass of ``int`` in Python, so ``True`` would
    otherwise be accepted as one paisa.
    """
    raw = body.get(field)
    if not isinstance(raw, Mapping):
        raise SchemaRejected("acp_amount_absent", field=field)
    minor = raw.get("amount_minor")
    currency = raw.get("currency")
    if isinstance(minor, bool) or not isinstance(minor, int):
        raise SchemaRejected(
            "acp_amount_is_not_integer_minor_units",
            field=field,
            presented_type=type(minor).__name__,
        )
    if not isinstance(currency, str):
        raise SchemaRejected("acp_currency_absent", field=field)
    return Money(minor, currency)


# ----------------------------------------------------------------------- the mapping


def map_request(
    db: Session,
    *,
    admitted: AdmittedRequest,
    operation: AcpOperation,
    authority_epoch: int,
    acp_session: AcpSession | None = None,
) -> ProtocolIntent:
    """Turn an admitted ACP request into the one internal command, or refuse it.

    ``acp_session`` is the platform's current view, read by the caller under the same
    transaction; ``authority_epoch`` is the tenant's current epoch. Both are arguments
    rather than lookups so that this function is a pure decision over locked state: the row
    the caller locked is the row this reasons about, which is what stops the check and the
    act from disagreeing under contention.

    ``authority_epoch`` has no default, deliberately, and the absence is the point. It once
    defaulted to zero while :attr:`AcpSession.authority_epoch` also defaulted to zero, which
    meant a caller that simply forgot the argument got a revocation check that compared two
    defaults, agreed with itself and passed. A security check whose default is "permit" is
    worse than no check, because it reads like one in review. Making it required moves the
    mistake from a silent runtime pass to a type error at every call site.
    """
    if operation is AcpOperation.CREATE_SESSION:
        if acp_session is not None:
            raise StateRejected("acp_session_already_exists", session_id=acp_session.session_id)
        return _construction_intent(admitted, operation, acp_session=None)

    if acp_session is None:
        # Reported as stale state rather than as an authentication failure. The caller holds
        # a valid credential and named something this platform does not have; its remedy is
        # to re-read, which is what ``STALE_CHECKOUT`` tells every layer above.
        raise StateRejected("acp_session_not_found")

    permitted = ALLOWED_FROM[operation]
    if acp_session.status not in permitted:
        raise StateRejected(
            "acp_session_state_forbids_operation",
            status=acp_session.status.value,
            operation=operation.value,
            permitted=sorted(s.value for s in permitted),
        )

    if operation is AcpOperation.UPDATE_SESSION:
        return _construction_intent(admitted, operation, acp_session=acp_session)
    if operation is AcpOperation.RETRIEVE_SESSION:
        return _intent(
            admitted, IntentKind.TRACK_ORDER, operation, acp_session, order_id=acp_session.order_id
        )
    if operation is AcpOperation.CANCEL_SESSION:
        # A proposal, never an outcome. ``IntentKind`` has no ``CANCEL`` for exactly this
        # reason, so "the external buyer asked to cancel" cannot become "the external buyer
        # cancelled" through a later rename.
        return _intent(admitted, IntentKind.PROPOSE_CANCELLATION, operation, acp_session)
    return _completion_intent(db, admitted, acp_session, authority_epoch=authority_epoch)


def _completion_intent(
    db: Session,
    admitted: AdmittedRequest,
    acp_session: AcpSession,
    *,
    authority_epoch: int,
) -> ProtocolIntent:
    """The only ACP request that can reach kernel admission, and every check it must pass.

    The order is deliberate. Version and hash first, because a stale caller is the common
    case and its remedy is cheap. Then the recorded approval, because a completion without
    one is not a stale request but a request this platform will never honour in that shape.
    Then epoch and expiry, which are the two ways an approval that once existed stops
    counting. Amount last, because reaching it means everything about *which* checkout is
    being paid already agrees, and only *how much* is left to disagree about.

    Nothing here is a substitute for the kernel. Every one of these facts is re-checked
    against locked rows at admission, and this function's job is only to refuse early and
    to refuse legibly -- a caller that learns "your hash is stale" from a structured
    protocol error recovers, where one that learns it from a kernel denial three services
    later has to guess.
    """
    version = admitted.body.get("checkout_version")
    content_hash = admitted.body.get("content_hash")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or not isinstance(content_hash, str)
    ):
        raise SchemaRejected("acp_completion_must_echo_version_and_hash")

    if version != acp_session.checkout_version or content_hash != acp_session.content_hash:
        raise StateRejected(
            "acp_checkout_version_superseded",
            presented_version=version,
            current_version=acp_session.checkout_version,
            hash_matches=content_hash == acp_session.content_hash,
        )

    if acp_session.approval_id is None or acp_session.approved_version != version:
        # 16.2 and specification 5.3's Registry A/B split, at the one place it can be
        # violated. An ACP completion carries payment credentials the external platform
        # collected; their presence is not this platform's buyer having agreed to anything.
        raise MandateRejected(
            "acp_completion_without_recorded_approval",
            approved_version=acp_session.approved_version,
            presented_version=version,
        )

    if acp_session.authority_epoch != authority_epoch:
        raise MandateRejected(
            "acp_authority_epoch_superseded",
            approved_under=acp_session.authority_epoch,
            current=authority_epoch,
        )

    expires_at = acp_session.approval_expires_at
    if expires_at is not None and expires_at <= database_now(db):
        # The database clock, never this process's, for the reason
        # ``core.replay`` gives at length: a skewed pod must not be able to extend a
        # buyer's consent past the window they were shown.
        raise MandateRejected("acp_approval_expired", expired_at=expires_at.isoformat())

    approved_total = acp_session.amount
    if approved_total is None:
        # A projection that carries an approval but no total is this platform not knowing
        # what the human agreed to, and there is exactly one wrong way out of that: take
        # the number from the request. The body of a completion is written by the external
        # party, and letting it supply the amount on the one intent that ``moves_money``
        # would turn a missing field in our own state into the caller naming its own price.
        # Refusing costs a session that could not have been completed correctly anyway.
        raise MandateRejected(
            "acp_approved_total_unknown",
            session_id=acp_session.session_id,
            presented_version=version,
        )

    presented = parse_amount(admitted.body)
    if presented != approved_total:
        raise MandateRejected(
            "acp_amount_does_not_match_approved_total",
            presented_minor=presented.minor,
            approved_minor=approved_total.minor,
        )

    return _intent(
        admitted,
        IntentKind.SUBMIT_APPROVED,
        AcpOperation.COMPLETE_SESSION,
        acp_session,
        # The approved total, not the presented one, even though the two have just been
        # proved equal. Equality today is a check; provenance is a property, and the amount
        # travelling onward should be the one taken from what a human approved so that a
        # future edit to the comparison cannot quietly promote a body field into an amount.
        amount=approved_total,
        checkout_version=version,
        content_hash=content_hash,
    )


def _construction_intent(
    admitted: AdmittedRequest,
    operation: AcpOperation,
    *,
    acp_session: AcpSession | None,
) -> ProtocolIntent:
    """Basket work, or the freeze that ends it. See the module docstring's first rule."""
    already = acp_session.supplied if acp_session is not None else frozenset()
    supplied = already | _supplied_by(admitted.body)
    kind = (
        IntentKind.CREATE_CHECKOUT
        if supplied >= REQUIRED_FOR_READINESS
        else IntentKind.BUILD_BASKET
    )
    return _intent(admitted, kind, operation, acp_session, supplied=sorted(supplied))


def _supplied_by(body: Mapping[str, Any]) -> frozenset[str]:
    """Which readiness requirements this body satisfies.

    A key present but empty does not count. An ACP client that sends ``"items": []`` has
    named the field without supplying anything, and treating that as readiness would freeze
    a checkout with nothing in it for a human to approve.
    """
    return frozenset(name for name in REQUIRED_FOR_READINESS if body.get(name))


def _intent(
    admitted: AdmittedRequest,
    kind: IntentKind,
    operation: AcpOperation,
    acp_session: AcpSession | None,
    *,
    amount: Money | None = None,
    checkout_version: int | None = None,
    content_hash: str | None = None,
    order_id: uuid.UUID | None = None,
    supplied: list[str] | None = None,
) -> ProtocolIntent:
    """Assemble the intent, with the evidence stream's id on it.

    ``raw_reference`` is the interaction id, so a reviewer holding an intent can always get
    back to the bytes that produced it -- which is what specification 13.3 asks for and what
    a mapping that only carried the parsed fields would make impossible.
    """
    arguments: dict[str, Any] = {"acp_operation": operation.value}
    if acp_session is not None:
        arguments["acp_status"] = acp_session.status.value
    if supplied is not None:
        arguments["supplied"] = supplied
    return ProtocolIntent(
        kind=kind,
        caller=admitted.caller,
        pin=admitted.interaction.pin,
        correlation_id=admitted.caller.correlation_id,
        external_id=acp_session.session_id if acp_session is not None else None,
        checkout_id=acp_session.checkout_id if acp_session is not None else None,
        checkout_version=checkout_version,
        content_hash=content_hash,
        order_id=order_id,
        amount=amount,
        arguments=MappingProxyType(arguments),
        raw_reference=str(admitted.interaction.interaction_id),
    )


def approval_handoff(admitted: AdmittedRequest, acp_session: AcpSession) -> ProtocolIntent:
    """The intent that asks a human, raised by the adapter and never by the caller.

    A session that has just become ready needs a decision this protocol has no way to
    request: ACP's lifecycle assumes the external platform already holds the shopper's
    consent, and this platform's does not accept that. So the adapter raises
    ``REQUEST_APPROVAL`` itself, and the ACP response carries a handoff to the trusted
    surface rather than a completed purchase.

    That the caller could not have raised this is the point, and it is structural rather
    than policy: ``REQUEST_APPROVAL`` is not in the routing table, so no ACP request maps to
    it, and the approval it asks for is recorded against a hash the buyer was shown on a
    surface the external party cannot reach.
    """
    return _intent(
        admitted,
        IntentKind.REQUEST_APPROVAL,
        AcpOperation.UPDATE_SESSION,
        acp_session,
        amount=acp_session.amount,
        checkout_version=acp_session.checkout_version,
        content_hash=acp_session.content_hash,
    )
