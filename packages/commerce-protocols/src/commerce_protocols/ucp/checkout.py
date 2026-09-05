"""UCP Complete Checkout semantics, specification 14.3.

The question this module answers is narrow and it is the one every UCP integration gets
wrong in the same direction: what status does Complete Checkout return when the platform
cannot actually complete the payment by itself?

For this P0 the answer is ``requires_escalation``, and the reason is a fact about the
deployment rather than a limitation of UCP. UCP does support negotiated payment and
authentication Actions. This platform has not negotiated a Razorpay-specific UCP Payment
Action, and Razorpay Standard Checkout in test mode has no headless charge path -- the
buyer has to complete the payment in a browser. So there is no Action to process, and the
honest answer is to escalate to a human.

The status that must not be returned, and why
----------------------------------------------
``complete_in_progress`` means *the Complete Checkout request has been accepted and is being
processed*. It is correct in exactly two situations: asynchronous processing that needs no
further buyer input, or a formally negotiated payment/authentication Action that the
platform is now carrying out. Neither is true here.

Returning it anyway would be a lie with consequences rather than a cosmetic one. A
conforming client that receives ``complete_in_progress`` will poll Get Checkout waiting for
a terminal state and will not prompt its buyer for anything, because the platform has just
told it no buyer input is needed. The purchase then hangs until the reservation lapses.
Escalating instead is not the lesser status -- it is the only one that describes what has
to happen next.

:func:`decide_completion` is written so the distinction is structural. The four outcomes are
separate types, the ``requires_escalation`` branch cannot be constructed without a
continuation URL and a message, and ``complete_in_progress`` cannot be constructed without
naming which of the two legitimate grounds applies.

Step 2, which is easy to skip
------------------------------
"Preserve the verified mandate evidence without consuming it for a charge." The AP2
verification that precedes an escalation is real work with a real result, and that result is
recorded -- but escalating does not spend it. The buyer's approval on the trusted surface,
and the kernel admission that follows, are what authorise the payment. A mandate consumed at
escalation time would be authority spent on a step that authorises nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ap2.sdk.generated.types.checkout import Status
from ap2.sdk.generated.types.message_error import MessageError

from ..core.errors import SchemaRejected
from .messages import CODE_NO_HEADLESS_PAYMENT_PATH, escalation_message, message_payload

__all__ = [
    "AsyncGround",
    "CompleteOutcome",
    "Completed",
    "Escalation",
    "InProgress",
    "Incomplete",
    "decide_completion",
    "escalate_for_razorpay_handoff",
]


class AsyncGround(StrEnum):
    """The only two grounds on which ``complete_in_progress`` may be returned, 14.3.

    An enum rather than a boolean because the two are genuinely different situations that a
    reviewer must be able to tell apart in evidence, and because a caller has to name one --
    which is the point. There is no third member, and adding one would be a decision about
    protocol conformance rather than a convenience.
    """

    #: The business is performing asynchronous processing needing no new buyer input.
    ASYNCHRONOUS_PROCESSING = "ASYNCHRONOUS_PROCESSING"
    #: A formally negotiated UCP payment or authentication Action is being processed.
    NEGOTIATED_ACTION = "NEGOTIATED_ACTION"


@dataclass(frozen=True, slots=True)
class Escalation:
    """``requires_escalation``: a person has to continue this on a trusted surface.

    Both fields are required. A ``requires_escalation`` with no ``continue_url`` tells a
    client to get a human and gives it nowhere to send them, and one with no message gives
    no reason -- specification 14.3 step 3 asks for both, and constructing this without
    either is impossible rather than discouraged.
    """

    continue_url: str
    messages: tuple[MessageError, ...]
    status: Status = Status.requires_escalation

    def __post_init__(self) -> None:
        if not self.continue_url:
            raise SchemaRejected("escalation_without_continue_url")
        if not self.messages:
            raise SchemaRejected("escalation_without_a_structured_message")


@dataclass(frozen=True, slots=True)
class InProgress:
    """``complete_in_progress``: the request was genuinely accepted.

    ``ground`` names which of specification 14.3's two legitimate cases applies. It has no
    default, so this status cannot be returned by someone who has not decided which of them
    they are in -- which is the mistake the status exists to be misused for.
    """

    ground: AsyncGround
    status: Status = Status.complete_in_progress


@dataclass(frozen=True, slots=True)
class Completed:
    """``completed``: verified capture has happened and the order exists.

    Reachable only from capture evidence the kernel has applied. Specification 14.3 step 9
    is explicit that the UCP checkout becomes ``completed`` *only after* verified capture,
    which is why this carries the order id -- there is no completed checkout without one.
    """

    order_id: str
    status: Status = Status.completed


@dataclass(frozen=True, slots=True)
class Incomplete:
    """``incomplete``: something is missing that the caller can supply and retry."""

    messages: tuple[MessageError, ...]
    status: Status = Status.incomplete

    def __post_init__(self) -> None:
        if not self.messages:
            raise SchemaRejected("incomplete_without_a_structured_message")


#: The four outcomes of a Complete Checkout call. A union rather than a status string, so a
#: caller has to handle each case rather than passing an enum member around and hoping the
#: rest of the response was populated to match it.
CompleteOutcome = Escalation | InProgress | Completed | Incomplete


@dataclass(frozen=True, slots=True)
class CompletionContext:
    """What the platform knows when it has to choose a status.

    Assembled by the caller from its own locked state and from the verification it has just
    performed. Every field is a fact about *this* platform: nothing here is taken from the
    Complete Checkout request, because the request is the thing being judged.
    """

    checkout_id: uuid.UUID
    version: int
    #: True only when a Razorpay-specific UCP Payment Action has been formally negotiated
    #: for this session. False for this P0, and the specification says so plainly.
    negotiated_payment_action: bool
    #: True when the platform is genuinely mid-flight on work needing no buyer input.
    asynchronous_work_in_flight: bool
    #: Set once the kernel has applied verified capture evidence.
    captured_order_id: str | None = None
    #: Reasons the request cannot proceed that the caller could fix and retry.
    blocking_messages: tuple[MessageError, ...] = field(default_factory=tuple)


def escalate_for_razorpay_handoff(continue_url: str) -> Escalation:
    """The P0 Standard Checkout escalation, specification 14.3 step 3.

    One structured error message, ``severity: requires_buyer_review``, and an application
    code naming the actual reason: this payment needs the buyer present because the platform
    has no negotiated headless path to Razorpay. The severity says what the client should
    do; the code says why.
    """
    return Escalation(
        continue_url=continue_url,
        messages=(
            escalation_message(
                code=CODE_NO_HEADLESS_PAYMENT_PATH,
                content=(
                    "This payment must be completed by the buyer on the merchant's trusted "
                    "checkout surface. No headless payment Action has been negotiated for "
                    "this merchant, so the platform cannot complete the charge on the "
                    "buyer's behalf."
                ),
                path="$.status",
            ),
        ),
    )


def decide_completion(context: CompletionContext, *, continue_url: str) -> CompleteOutcome:
    """Choose the UCP status for a Complete Checkout call.

    The order of the branches is the order of the specification's own reasoning. A capture
    that has already happened is ``completed`` regardless of anything else, because it is
    the terminal truth. A blocking condition the caller can fix is ``incomplete``. A
    genuinely accepted request is ``complete_in_progress`` -- and only on one of the two
    grounds 14.3 permits. Everything remaining escalates.

    The final branch has no condition, and that is deliberate: escalation is the default for
    this deployment rather than an exceptional case, so there is no path where an
    unanticipated combination of state silently produces a more optimistic status.
    """
    if context.captured_order_id is not None:
        return Completed(order_id=context.captured_order_id)

    if context.blocking_messages:
        return Incomplete(messages=context.blocking_messages)

    if context.negotiated_payment_action:
        # Not reachable in this P0 -- no Razorpay UCP Payment Action has been negotiated.
        # Kept because the branch is what documents the condition under which
        # complete_in_progress would become correct, and removing it would leave the
        # distinction living only in prose.
        return InProgress(ground=AsyncGround.NEGOTIATED_ACTION)

    if context.asynchronous_work_in_flight:
        return InProgress(ground=AsyncGround.ASYNCHRONOUS_PROCESSING)

    return escalate_for_razorpay_handoff(continue_url)


def outcome_payload(outcome: CompleteOutcome) -> dict[str, Any]:
    """A UCP-shaped response body for an outcome.

    The status is always read off the outcome object rather than passed alongside it, so a
    body cannot claim one status while carrying another's fields -- which is the shape of
    the ``complete_in_progress`` mistake this module exists to prevent.
    """
    body: dict[str, Any] = {"status": outcome.status.value}
    if isinstance(outcome, Escalation):
        body["continue_url"] = outcome.continue_url
        body["messages"] = [message_payload(m) for m in outcome.messages]
    elif isinstance(outcome, Incomplete):
        body["messages"] = [message_payload(m) for m in outcome.messages]
    elif isinstance(outcome, Completed):
        body["order_id"] = outcome.order_id
    return body
