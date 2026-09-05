"""Redaction by construction: a secret is not *omitted* from a log line, it is unwritable.

The difference matters. A codebase that redacts by discipline has a rule -- "do not log
the webhook body" -- and a hundred call sites that each have to remember it. It passes
review on the day it is written and fails the first time somebody adds
``_log.debug("body=%s", body)`` while chasing a signature mismatch at two in the morning.
The failure is silent, permanent and, for a card number, reportable.

``commerce_api.errors`` already takes the structural position for HTTP responses: a 5xx
never carries ``str(exc)``, an ``IntegrityError`` yields its constraint *name* and never
its SQL or parameters, and a ``ValidationError`` is serialised with ``include_url=False,
include_context=False`` so pydantic cannot hand a rejected value back to the caller. This
module is the same posture pointed at logs, and it is built from four independent layers
so that no single mistake reaches the output:

1. **The type.** :data:`LogValue` is a union of scalars. A ``dict``, a list of basket
   items or a raw ``bytes`` body is not assignable to it, so under ``mypy --strict`` the
   attempt to log a whole webhook body does not type-check. This is the layer that turns
   "must not" into "cannot".
2. **The runtime type check.** Types are erased at runtime and ``cast`` exists, so
   :func:`redact_value` also refuses a non-scalar it is handed anyway, replacing it with
   a structure marker carrying a digest and a size. The digest is deliberately the same
   SHA-256 that ``webhook_inbox.body_digest`` stores, so an operator can still join a log
   line to the row holding the evidence -- without the body ever being in the log.
3. **The field name.** :data:`DENIED_NAME_FRAGMENTS` refuses a field by what it is
   called. ``signature``, ``token``, ``card`` and ``secret`` never reach the output no
   matter what value they carry, and the caller does not have to have been thinking about
   it. :data:`ALLOWED_FIELD_NAMES` is checked first and exists for the handful of names
   that contain a denied fragment and are safe -- ``body_digest`` is the example.
4. **The value's shape.** :func:`scrub_text` runs over every string that survives the
   first three, and over the rendered message of *any* log record, including ones emitted
   by code that has never heard of this package. That is the layer that catches
   ``_log.info("verifying %s", authorization_header)`` in a package this one does not own.

What is deliberately *not* scrubbed by shape
--------------------------------------------
A 64-character hex string is both an HMAC signature and a SHA-256 audit hash. Scrubbing
by shape would destroy the audit chain's own operational visibility -- ``self_hash``,
``prev_hash`` and ``body_digest`` are the joins that make a hash chain investigable -- so
signatures are excluded by *name* instead, and hashes pass through. Provider references
(``pay_...``, ``order_...``, ``rfnd_...``) also pass through: they are in the Money Action
Proof Chain (spec 26.4) and are exactly what an operator needs. Razorpay *credentials*
(``rzp_test_...``, ``rzp_live_...``) do not pass, because the id half is worthless in a
log and the secret half is catastrophic in one.

:class:`Secret` is the escape hatch for a value that must be carried through code but
never rendered: it has no ``__str__`` other than the marker, so even an f-string cannot
get the value out. ``reveal()`` is the only way, and it is greppable.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Final

__all__ = [
    "ALLOWED_FIELD_NAMES",
    "DENIED_NAME_FRAGMENTS",
    "MAX_EXCEPTION_LENGTH",
    "MAX_MESSAGE_LENGTH",
    "MAX_VALUE_LENGTH",
    "REDACTED",
    "LogValue",
    "Secret",
    "cap_exception",
    "cap_message",
    "digest_of",
    "is_denied_name",
    "redact_fields",
    "redact_value",
    "scrub_text",
]

#: What a redacted thing looks like. One string, so a log query can count them.
REDACTED: Final[str] = "[redacted]"

#: A field value longer than this is replaced by a digest rather than truncated. Truncation
#: is the wrong instinct: the first 256 characters of a card-on-file blob is still a card
#: number, and the first 256 of a JWT is still enough to identify the subject.
MAX_VALUE_LENGTH: Final[int] = 256

#: A rendered message longer than this is cut, with a marker saying how much went. A
#: message is developer-authored text rather than attacker-supplied data, so the tail is
#: the least valuable part and losing it beats dropping the record.
MAX_MESSAGE_LENGTH: Final[int] = 2048

#: A formatted traceback is long by nature and worth keeping nearly whole.
MAX_EXCEPTION_LENGTH: Final[int] = 8192

#: The value a log field may hold. **This union is the first line of redaction**: a
#: mapping, a sequence, ``bytes`` or an arbitrary object is not assignable to it, so
#: ``fields=dict(body=payload)`` fails ``mypy --strict`` at the call site rather than
#: succeeding quietly and putting a webhook body in a log store.
type LogValue = str | int | float | bool | None


class Secret[T]:
    """A value that can be carried but not rendered.

    ``repr``, ``str`` and ``format`` all give :data:`REDACTED`, so every accidental route
    to the value -- an f-string, ``"%s" %``, a ``print``, a JSON encoder falling back to
    ``str`` -- yields the marker. :meth:`reveal` is the one way out and it is a word worth
    grepping for at review time.

    Equality compares revealed values so a secret can be tested and stored in a set;
    ordering is not defined, because sorting secrets leaks their order.
    """

    __slots__ = ("_value",)

    def __init__(self, value: T) -> None:
        self._value = value

    def reveal(self) -> T:
        """The wrapped value. The only way to it, and the only thing to review."""
        return self._value

    def __repr__(self) -> str:
        return REDACTED

    def __str__(self) -> str:
        return REDACTED

    def __format__(self, format_spec: str) -> str:
        # Without this, f"{secret:>20}" would go through object.__format__, which honours
        # the spec against str(self) -- harmless today, but the marker should not be
        # something a format spec can reshape into a hint about the value's length.
        return REDACTED

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            return bool(self._value == other._value)
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)


# ------------------------------------------------------------------ names


#: Exact field names that are safe despite containing a denied fragment. Checked before
#: :data:`DENIED_NAME_FRAGMENTS`, and short on purpose: every entry is a promise that this
#: particular name never carries the thing its fragment suggests.
#:
#: The hashes are here because a hash chain is only investigable if its links can be
#: logged (spec 26.2), and a SHA-256 digest of a body is not the body.
ALLOWED_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    {
        "body_digest",
        "content_hash",
        "checkout_hash",
        "payload_hash",
        "policy_receipt_hash",
        "prev_hash",
        "receipt_hash",
        "self_hash",
        "dedup_key",
        "idempotency_key",
        "public_key_id",
        "signing_key_id",
    }
)

#: A field whose name contains any of these is replaced by :data:`REDACTED`, whatever it
#: holds. Substring rather than exact match, so ``razorpay_signature``,
#: ``x_razorpay_signature`` and ``webhook_signature`` are all covered by one entry and a
#: name nobody anticipated is covered by default.
#:
#: Two entries deserve a note because they surprise people:
#:
#: * ``token`` also denies ``lease_token``. That is correct: an outbox lease token is a
#:   capability -- whoever holds it can complete or fail that work item -- and it belongs
#:   in the database, not in a log aggregator half the company can read.
#: * ``body`` also denies ``raw_body`` and ``request_body``, which is the point, while
#:   ``body_digest`` is rescued by :data:`ALLOWED_FIELD_NAMES` above.
DENIED_NAME_FRAGMENTS: Final[tuple[str, ...]] = (
    "address",
    "api_key",
    "apikey",
    "auth",
    "bearer",
    "body",
    "card",
    "cookie",
    "credential",
    "cvc",
    "cvv",
    "email",
    "hmac",
    "mobile",
    "otp",
    "pan",
    "passphrase",
    "password",
    "payload",
    "phone",
    "private_key",
    "secret",
    "signature",
    "token",
    "upi",
    "vpa",
)


def is_denied_name(name: str) -> bool:
    """Whether a field name is refused outright. Case-insensitive."""
    lowered = name.lower()
    if lowered in ALLOWED_FIELD_NAMES:
        return False
    return any(fragment in lowered for fragment in DENIED_NAME_FRAGMENTS)


# ------------------------------------------------------------------ shapes


#: ``Authorization: Bearer <jwt>`` and its Basic sibling, wherever they are interpolated.
_AUTH_SCHEME: Final[re.Pattern[str]] = re.compile(
    r"\b(Bearer|Basic|Token)\s+[A-Za-z0-9._\-+/=]{8,}", re.IGNORECASE
)

#: Razorpay credentials. The key *id* is not secret, but it is also useless in a log and
#: telling the two halves apart by eye is exactly the mistake this exists to prevent.
_PROVIDER_CREDENTIAL: Final[re.Pattern[str]] = re.compile(
    r"\brzp_(?:test|live)_[A-Za-z0-9]{6,}", re.IGNORECASE
)

#: Stripe-shaped and webhook-shaped secrets, for anything that reaches these logs from a
#: library that assumes those conventions.
_PREFIXED_SECRET: Final[re.Pattern[str]] = re.compile(r"\b(?:sk|whsec|rk)_[A-Za-z0-9_]{8,}")

#: A PEM block. Matched non-greedily to the first end marker so two concatenated keys do
#: not collapse into one match that could leave the second's tail behind.
_PEM_BLOCK: Final[re.Pattern[str]] = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
)

#: A candidate card number: 13 to 19 digits, optionally grouped by single spaces or
#: hyphens. Confirmed by Luhn before it is replaced, so an order id, a timestamp in
#: milliseconds or an amount in minor units is not mistaken for a PAN.
_DIGIT_RUN: Final[re.Pattern[str]] = re.compile(r"(?<![\d\-])(?:\d[ -]?){12,18}\d(?![\d\-])")


def _luhn_ok(digits: str) -> bool:
    """The Luhn check digit. Every issued card number satisfies it; almost nothing else does."""
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = ord(char) - 48
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _redact_pan(match: re.Match[str]) -> str:
    digits = "".join(char for char in match.group(0) if char.isdigit())
    if 13 <= len(digits) <= 19 and _luhn_ok(digits):
        return "[redacted:pan]"
    return match.group(0)


def scrub_text(text: str) -> str:
    """Remove the shapes that are secret whatever they are called.

    Applied to every field value, every label value and every rendered log message,
    including messages from packages that know nothing about this one -- which is the
    whole reason it works on shapes rather than on names.

    Ordered most specific first: a PEM block is removed whole before the digit-run pass
    can find a Luhn-valid substring inside its base64.
    """
    if not text:
        return text
    scrubbed = _PEM_BLOCK.sub("[redacted:private-key]", text)
    scrubbed = _AUTH_SCHEME.sub(lambda m: f"{m.group(1)} {REDACTED}", scrubbed)
    scrubbed = _PROVIDER_CREDENTIAL.sub("[redacted:provider-credential]", scrubbed)
    scrubbed = _PREFIXED_SECRET.sub("[redacted:secret]", scrubbed)
    return _DIGIT_RUN.sub(_redact_pan, scrubbed)


def digest_of(value: object) -> str:
    """The SHA-256 of a value's canonical bytes, for joining a log line to the evidence.

    ``bytes`` hash as themselves and everything else through ``repr``, which is stable
    within one process and one Python version -- enough for "these two log lines refer to
    the same object", which is all a log digest is for. The digest that has to match
    ``webhook_inbox.body_digest`` across processes is computed by the receiver over the
    raw bytes and passed in as a field; this function is the fallback for everything else.
    """
    raw = value if isinstance(value, bytes | bytearray) else repr(value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


# ------------------------------------------------------------------ the pipeline


def redact_value(name: str, value: object) -> LogValue:
    """One field, through all four layers, returning something safe to serialise.

    Never raises. A redactor that can throw is a redactor that gets wrapped in a bare
    ``except`` by the third caller, and then it is not a redactor at all.
    """
    try:
        if is_denied_name(name):
            return REDACTED
        if value is None or isinstance(value, bool):
            # bool before int: isinstance(True, int) is True and "outcome": true should
            # stay a JSON boolean rather than becoming 1.
            return value
        if isinstance(value, int | float):
            return value
        if isinstance(value, Secret):
            return REDACTED
        if isinstance(value, str):
            return _cap(scrub_text(value), value)
        if isinstance(value, bytes | bytearray):
            # Bytes in a log field is a body, a signature or a key. There is no fourth
            # thing, and none of the three may be rendered.
            return f"[redacted:bytes len={len(value)} sha256={digest_of(bytes(value))}]"
        # A mapping, a sequence, an ORM row, a pydantic model. LogValue does not admit
        # any of them; arriving here means a cast or an untyped caller, and the safe
        # reading is that it is a payload.
        return f"[redacted:{type(value).__name__} sha256={digest_of(value)}]"
    except Exception:  # pragma: no cover - defence in depth; nothing above should raise
        return REDACTED


def _cap(scrubbed: str, original: str) -> str:
    if len(scrubbed) <= MAX_VALUE_LENGTH:
        return scrubbed
    return f"[redacted:oversize chars={len(original)} sha256={digest_of(original)}]"


def redact_fields(fields: dict[str, Any]) -> dict[str, LogValue]:
    """A whole field mapping, redacted, with the keys themselves made safe.

    Keys are scrubbed and capped too. A key is usually a source literal, but a caller
    that builds one from data (``**{header_name: value}``) would otherwise smuggle the
    data into the key half of the pair.
    """
    out: dict[str, LogValue] = {}
    for raw_name, value in fields.items():
        name = str(raw_name)[:64]
        out[scrub_text(name)] = redact_value(name, value)
    return out


def cap_message(text: str) -> str:
    """Scrub a rendered log message and cut it if it is unreasonably long."""
    scrubbed = scrub_text(text)
    if len(scrubbed) <= MAX_MESSAGE_LENGTH:
        return scrubbed
    dropped = len(scrubbed) - MAX_MESSAGE_LENGTH
    return f"{scrubbed[:MAX_MESSAGE_LENGTH]}…[truncated {dropped} chars]"


def cap_exception(text: str) -> str:
    """Scrub a formatted traceback and cut it. ``str(exc)`` can carry data; a frame list cannot."""
    scrubbed = scrub_text(text)
    if len(scrubbed) <= MAX_EXCEPTION_LENGTH:
        return scrubbed
    dropped = len(scrubbed) - MAX_EXCEPTION_LENGTH
    return f"{scrubbed[:MAX_EXCEPTION_LENGTH]}…[truncated {dropped} chars]"
