"""UCP structured messages, and the ``requires_buyer_review`` trap.

Specification 14.3 spends a whole sentence on one mistake, which is a good sign it is a
mistake people make:

    ``requires_buyer_review`` is the message **severity**, not the message type or code,
    and must not be used as though it were the whole message.

A UCP message has three independent fields and they answer three different questions.
``type`` says what kind of message this is -- ``error``, ``warning`` or ``info``. ``code``
is the application-specific identifier a client branches on. ``severity`` says what the
state of the resource is and what the client should do about it. So the escalation this
platform returns is an ``error`` whose ``severity`` is ``requires_buyer_review`` and whose
``code`` names the actual reason -- three separate statements, not one word used three ways.

The failure the sentence is guarding against is a message like
``{"type": "requires_buyer_review"}``, which validates against nothing, tells a client
nothing it can branch on, and reads correct to anyone who has skimmed the specification.
:func:`escalation_message` makes that construction impossible by building the three fields
from separate arguments and refusing to accept a code that is a severity.

Why the enum comes from the AP2 SDK
------------------------------------
``Severity`` is imported from ``ap2.sdk.generated.types.message_error`` rather than
redeclared. That module is generated from the pinned UCP schema, so importing it means the
four permitted values are the schema's four values by construction, and a schema change
under the pin becomes an import error rather than a silently accepted fifth value.

One quirk of those generated models is worth knowing before comparing anything:
``content_type`` is declared ``ContentType | None = 'plain'``, with a bare *string* as the
default. A model built in Python without passing it dumps ``'plain'``; one parsed from JSON
dumps ``ContentType.plain``. :func:`message_payload` normalises to the string form so that
two messages meaning the same thing serialise the same way.
"""

from __future__ import annotations

from typing import Any, Final

from ap2.sdk.generated.types.message_error import ContentType, MessageError, Severity

__all__ = [
    "CODE_MERCHANT_STATE_CHANGED",
    "CODE_NO_HEADLESS_PAYMENT_PATH",
    "CODE_RESERVATION_EXPIRED",
    "CODE_STALE_CHECKOUT",
    "SEVERITIES",
    "escalation_message",
    "message_payload",
    "recoverable_message",
]

#: The four severities the pinned schema permits, as a set, so a caller can assert
#: membership without importing the enum and without hardcoding four strings.
SEVERITIES: Final[frozenset[str]] = frozenset(member.value for member in Severity)

#: The application code for the Razorpay handoff. Named as a constant because it appears in
#: the escalation response, in evidence, and in the tests, and a typo in any one of them
#: would leave a client unable to branch on it.
CODE_NO_HEADLESS_PAYMENT_PATH: Final[str] = "payment_requires_buyer_present_checkout"

#: The checkout the caller is holding is no longer the current one.
CODE_STALE_CHECKOUT: Final[str] = "checkout_version_superseded"

#: Merchant truth moved between approval and submission; a new version exists.
CODE_MERCHANT_STATE_CHANGED: Final[str] = "merchant_state_changed_reapproval_required"

#: The inventory hold lapsed before the buyer completed.
CODE_RESERVATION_EXPIRED: Final[str] = "reservation_expired"


def _reject_severity_as_code(code: str) -> None:
    """Refuse a code that is actually a severity.

    This is specification 14.3's warning turned into a runtime refusal. Passing
    ``code="requires_buyer_review"`` produces a message that looks plausible, validates
    against the schema, and tells a client nothing -- the severity field already said that,
    and the code field has now said nothing. Catching it here is cheap; catching it in an
    interoperability test with a partner is not.
    """
    if code in SEVERITIES:
        raise ValueError(
            f"{code!r} is a UCP message severity, not an application code. Specification "
            "14.3: the severity is a separate field, and a code that repeats it leaves the "
            "client with no reason for the message."
        )


def message_payload(message: MessageError) -> dict[str, Any]:
    """One message as a JSON-ready mapping, with ``content_type`` normalised.

    The generated model's ``content_type`` default is a raw string while a parsed value is
    an enum member, so a round trip through JSON would otherwise change the serialised
    shape of a message nobody edited.
    """
    payload: dict[str, Any] = message.model_dump(mode="json", exclude_none=True)
    content_type = payload.get("content_type")
    if isinstance(content_type, ContentType):  # pragma: no cover - model_dump resolves it
        payload["content_type"] = content_type.value
    return payload


def escalation_message(
    *,
    code: str,
    content: str,
    path: str | None = None,
) -> MessageError:
    """The message that accompanies ``requires_escalation``, specification 14.3 step 3.

    Fixed at ``type: error`` and ``severity: requires_buyer_review``, because those two are
    what the specification requires of this particular message and leaving them to a caller
    would mean every call site could get them wrong independently. ``code`` and ``content``
    are the caller's, since they are the parts that actually say what happened.

    ``content`` is prose for a person and carries no authority. It must never be built from
    an unverified artifact: it can end up rendered in a partner's interface, and a message
    assembled from attacker-controlled text is a way to put words in this platform's mouth.
    """
    _reject_severity_as_code(code)
    return MessageError(
        type="error",
        code=code,
        path=path,
        content=content,
        content_type=ContentType.plain,
        severity=Severity.requires_buyer_review,
    )


def recoverable_message(*, code: str, content: str, path: str | None = None) -> MessageError:
    """A message for a condition the caller can fix and retry without a human.

    ``recoverable`` rather than ``requires_buyer_review``, and the distinction matters to
    the receiving agent: a recoverable message means "try again differently", while
    ``requires_buyer_review`` means "stop and get a person". Using the escalation severity
    for an ordinary stale-state condition would send buyers to a trusted surface for
    something their agent could have resolved by re-reading the checkout.
    """
    _reject_severity_as_code(code)
    return MessageError(
        type="error",
        code=code,
        path=path,
        content=content,
        content_type=ContentType.plain,
        severity=Severity.recoverable,
    )
