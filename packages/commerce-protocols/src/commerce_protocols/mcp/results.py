"""What an MCP tool is allowed to hand back, and why the shape is the guarantee.

Specification 17.3 forbids transmitting Razorpay credentials, webhook secrets and AP2
private keys through the MCP surface. A rule phrased that way is a rule somebody has to
remember at every call site, and the call sites are written by whoever adds the next tool.
So the rule is moved into the only object a tool result can be: nothing leaves this server
except a :class:`ToolResult`, and a :class:`ToolResult` that contains a credential cannot
be constructed. The prohibition stops being a review comment and becomes a type.

The screen runs on both halves of the value. A **key** whose name says "secret" is refused
whatever it holds, because a field called ``key_secret`` that happens to be empty today is
a field somebody will fill in tomorrow. A **value** that looks like a credential is refused
whatever it is called, because the leak that actually happens is an exception message or a
provider payload pasted into a ``detail`` field, and that field is never called
``api_key``. Either half alone would be trivially bypassed; together they cover the two
ways this has ever gone wrong.

Floats are refused outright, and the reason is money rather than secrecy. This platform
represents every amount as integer minor units, and a result mapping cannot tell which of
its numbers is an amount. Refusing the type is the only version of that rule that a
generic container can enforce, and no legitimate tool result needs one: a quantity is an
integer, a price is minor units, and a rating nobody has asked for can be a string.

A refusal here raises rather than returning a rejection, and the distinction is the one
:mod:`commerce_protocols.core.errors` draws. A caller presenting a bad nonce is an external
party asking for something it may not have; a *tool implementation* trying to return a
webhook secret is this platform being wrong, in a way that would be catastrophic and silent
if it were merely logged. It is a fault, it is loud, and the request fails.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from .tools import ToolName

__all__ = [
    "MAX_RESULT_DEPTH",
    "MAX_RESULT_NODES",
    "MAX_RESULT_TEXT",
    "CredentialLeakError",
    "ResultShapeError",
    "ToolResult",
    "ToolResultError",
    "screen",
]

#: How deeply a result may nest. Four levels holds an order with its lines and their
#: provenance, and stops a cyclic or pathological structure from being walked forever.
MAX_RESULT_DEPTH: Final[int] = 4

#: How many scalars one result may carry, so a tool cannot answer with a whole catalogue.
MAX_RESULT_NODES: Final[int] = 512

#: The longest string a result may carry. Generous enough for a policy paragraph and far
#: too short for a PEM bundle or a base64 key set.
MAX_RESULT_TEXT: Final[int] = 4096

#: Terms that make a field name a leak wherever they appear inside it. Matched against the
#: key with its separators removed and its camel humps split, so ``api_key``, ``apiKey``,
#: ``api-key`` and ``razorpayKeySecret`` are one rule rather than four an author has to
#: think of. These are long enough that a substring match cannot fire on an innocent word.
_LEAKY_SUBSTRINGS: Final[frozenset[str]] = frozenset(
    {
        "apikey",
        "authorization",
        "bearer",
        "credential",
        "keysecret",
        "passphrase",
        "password",
        "privatekey",
        "secret",
    }
)

#: Terms that are a leak only when they stand as a whole segment of a name. Short enough
#: that a substring rule would fire inside ordinary words, so ``pem`` refuses a field called
#: ``pem`` and leaves one called ``shipment`` alone.
_LEAKY_SEGMENTS: Final[frozenset[str]] = frozenset(
    {"hmac", "jwk", "jwks", "pem", "signature", "token", "tokens"}
)

#: Substrings that mark a value as a credential regardless of the field it arrived in.
#: ``rzp_`` covers Razorpay key ids and secrets, ``whsec_`` the webhook secret, ``-----``
#: any PEM block, and ``"d"`` the private scalar of a serialized EC JWK -- which is exactly
#: how an AP2 signing key would escape if one were ever handed to a tool.
_CREDENTIAL_MARKERS: Final[tuple[str, ...]] = (
    "-----begin",
    "-----end",
    '"d":',
    "'d':",
    "authorization:",
    "rzp_live_",
    "rzp_test_",
    "sk_live_",
    "sk_test_",
    "whsec_",
)

#: An HTTP authentication scheme followed by something credential-shaped. The blob has to
#: be sixteen unbroken credential characters, and that bound is the whole point: a bare
#: ``bearer``/``basic`` substring is a word this catalogue legitimately uses -- "a basic
#: cotton shirt" -- and refusing it raised a fault on an honest product description, which
#: is a screen that takes the surface down rather than one that protects it. A real
#: presented credential is never one short English word long.
_CREDENTIAL_SCHEME = re.compile(r"\b(?:bearer|basic)\s+[A-Za-z0-9+/=_.~-]{16,}", re.IGNORECASE)

#: A compact JWS or JWT: a base64url header that decodes to ``{"`` followed by two more
#: segments. Catches a presented access token or an AP2 mandate echoed into a result.
_COMPACT_JWS = re.compile(r"eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]*")

#: A camel hump: the boundary between a lowercase or digit and an uppercase letter. Splitting
#: on it first is what makes ``webhookSecret`` and ``webhook_secret`` the same name here.
_CAMEL_HUMP = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

_SEPARATORS = re.compile(r"[^a-z0-9]+")


class ToolResultError(RuntimeError):
    """A tool tried to return something this surface may not carry. Always a bug here."""


class CredentialLeakError(ToolResultError):
    """The result names or contains a credential. Specification 17.3, third bullet.

    The message deliberately names the *path* and not the offending value: an exception
    that quotes the secret it caught puts the secret in a stack trace, a log aggregator and
    an incident ticket, which is a longer-lived leak than the one it prevented.
    """


class ResultShapeError(ToolResultError):
    """The result is not a shape this surface can carry at all."""


def _key_offends(key: str) -> bool:
    """True when a field name announces that it holds a credential.

    Normalised once and screened twice. Camel humps become separators, everything is
    lowercased, and the name is then read both as a set of segments and as one squashed
    run -- because ``api_key`` hides the term in its separators and ``myApiKey`` hides it
    in its neighbours, and a screen that reads a name only one way misses one of them.
    """
    spaced = _CAMEL_HUMP.sub(" ", key).lower()
    segments = {part for part in _SEPARATORS.split(spaced) if part}
    if segments & _LEAKY_SEGMENTS:
        return True
    squashed = _SEPARATORS.sub("", spaced)
    return any(term in squashed for term in _LEAKY_SUBSTRINGS)


def _screen_text(value: str, path: str) -> None:
    if len(value) > MAX_RESULT_TEXT:
        raise ResultShapeError(
            f"{path} is {len(value)} characters; a tool result carries at most "
            f"{MAX_RESULT_TEXT}, and anything longer is a document, not an answer"
        )
    lowered = value.lower()
    for marker in _CREDENTIAL_MARKERS:
        if marker in lowered:
            raise CredentialLeakError(
                f"{path} contains a credential marker ({marker!r}); specification 17.3 "
                "forbids transmitting credentials, webhook secrets or private keys"
            )
    if _CREDENTIAL_SCHEME.search(value):
        raise CredentialLeakError(
            f"{path} presents an HTTP authentication scheme with a credential after it; "
            "specification 17.3 forbids transmitting credentials through this surface"
        )
    if _COMPACT_JWS.search(value):
        raise CredentialLeakError(
            f"{path} contains a compact JWS or JWT; a signed token is never part of an "
            "answer to a model, and echoing one back is token passthrough"
        )


def screen(content: Mapping[str, Any]) -> dict[str, Any]:
    """Return a plain, JSON-safe copy of ``content``, or refuse it.

    Copying rather than validating in place is deliberate on two counts. The caller keeps
    no reference into the structure the result now owns, so a tool that mutates its own
    working dictionary afterwards cannot change what was answered or what was recorded as
    evidence; and the copy is built from plain ``dict`` and ``list`` only, so the same
    object can go to the audit chain's canonical JSON without a second conversion that
    might disagree with the one the screen walked.
    """
    budget = MAX_RESULT_NODES

    def walk(node: Any, path: str, depth: int) -> Any:
        nonlocal budget
        budget -= 1
        if budget < 0:
            raise ResultShapeError(
                f"a tool result carries at most {MAX_RESULT_NODES} values; {path} exceeds it"
            )
        if depth > MAX_RESULT_DEPTH:
            raise ResultShapeError(
                f"{path} nests deeper than {MAX_RESULT_DEPTH}; a tool answers with facts, "
                "not with a document tree"
            )
        # ``bool`` is checked before ``int`` because it is a subclass of it, and ``float``
        # before both because refusing it is the money rule rather than a shape rule.
        if node is None or isinstance(node, bool):
            return node
        if isinstance(node, float):
            raise ResultShapeError(
                f"{path} is a float; money in this platform is integer minor units and a "
                "result mapping cannot tell which of its numbers is an amount"
            )
        if isinstance(node, int):
            return node
        if isinstance(node, str):
            _screen_text(node, path)
            return node
        # Bytes are refused before the sequence branch, and the ordering is the whole
        # defence. ``bytes``, ``bytearray`` and ``memoryview`` are all registered as
        # ``Sequence``, so falling through would walk one element at a time and hand back
        # ``whsec_...`` as a list of integers -- a form the value screen cannot read and a
        # reader trivially can. A secret does not stop being a secret for having been
        # transcribed, so the type is refused rather than decoded and re-screened: a tool
        # that has bytes to answer with has not decided what they mean yet.
        if isinstance(node, bytes | bytearray | memoryview):
            raise ResultShapeError(
                f"{path} is a {type(node).__name__}; a tool result carries text and "
                "integers, never raw bytes, because bytes are how a credential leaves "
                "unread by a screen that only understands strings"
            )
        if isinstance(node, Mapping):
            out: dict[str, Any] = {}
            for raw_key, value in node.items():
                if not isinstance(raw_key, str):
                    raise ResultShapeError(f"{path} has a non-string field name {raw_key!r}")
                if _key_offends(raw_key):
                    raise CredentialLeakError(
                        f"{path}.{raw_key} names a credential; no field of a tool result "
                        "may be a secret, a token, a signature or a private key"
                    )
                out[raw_key] = walk(value, f"{path}.{raw_key}", depth + 1)
            return out
        if isinstance(node, Sequence):
            return [walk(item, f"{path}[{index}]", depth + 1) for index, item in enumerate(node)]
        raise ResultShapeError(
            f"{path} is a {type(node).__name__}; a tool result carries only strings, "
            "integers, booleans, nulls and containers of those"
        )

    screened = walk(dict(content), "result", 0)
    # ``walk`` returns whatever it was given; the top level is a Mapping by signature, so
    # this narrowing is for mypy rather than for the reader.
    if not isinstance(screened, dict):  # pragma: no cover - unreachable by signature
        raise ResultShapeError("a tool result is an object")
    return screened


@dataclass(frozen=True, slots=True)
class ToolResult:
    """One answer to one governed tool call.

    ``is_error`` is the MCP wire flag for a tool that could not do what was asked, and it
    is *not* how a kernel denial is reported. A denial is the platform working: it travels
    as the structured decision the kernel produced, in a successful result, exactly as
    ADR 0003 D15 requires of every other surface. ``is_error`` is for "that SKU does not
    exist", not for "you may not have this".
    """

    tool: ToolName
    content: Mapping[str, Any]
    is_error: bool = False
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.tool, ToolName):
            raise ResultShapeError(f"{self.tool!r} is not a tool this server exposes")
        if self.is_error != (self.reason is not None):
            raise ResultShapeError(
                "a failed tool result names the stable reason it failed, and a successful "
                "one names none; the flag and the key are one fact"
            )
        object.__setattr__(self, "content", MappingProxyType(screen(self.content)))

    @classmethod
    def refusal(cls, tool: ToolName, reason: str, **details: Any) -> ToolResult:
        """A tool answering that it could not do the thing, with a stable reason key."""
        return cls(tool=tool, content=dict(details), is_error=True, reason=reason)

    def as_payload(self) -> dict[str, Any]:
        """A plain dict for the wire and for the evidence chain. Already screened."""
        return {
            "tool": self.tool.value,
            "isError": self.is_error,
            "reason": self.reason,
            "content": dict(self.content),
        }
