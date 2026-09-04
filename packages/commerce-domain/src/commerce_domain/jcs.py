"""RFC 8785 JSON Canonicalization Scheme, restricted to an integer-only number profile.

Why canonicalization exists here: the checkout hash, the AP2 merchant authorization and
the payment-mandate correlation must all reproduce byte-identical signing input from
semantically identical JSON, across processes and across months. JSON key order and
whitespace are not stable; JCS makes them stable.

PROFILE RESTRICTION -- deliberate, and stricter than RFC 8785:

    This implementation REFUSES float. It serializes int and nothing else numeric.

RFC 8785's hardest and most error-prone requirement is ECMAScript number serialization
(1E30 -> "1e+30", 4.50 -> "4.5", 333333333.33333329 -> "333333333.3333333"). Getting that
subtly wrong produces a signature that verifies locally and fails at a counterparty.

This domain never needs it. Every number in a checkout is an integer: money is integer
minor units, quantities are integers, versions are integers, timestamps are epoch
integers. A float reaching this function is therefore a bug -- most likely money that
escaped the Money type -- and the correct response is to fail loudly at canonicalization
time rather than to round it into a signature.

If a future payload genuinely requires non-integer numbers, implement ECMAScript number
serialization here and prove it against the official RFC 8785 number test vectors before
enabling it. Do not relax this quietly.
"""

from __future__ import annotations

from typing import Any

from .errors import CanonicalizationError

# RFC 8785 section 3.2.2.2: two-character escapes for these code points.
_SHORT_ESCAPES = {
    0x08: "\\b",
    0x09: "\\t",
    0x0A: "\\n",
    0x0C: "\\f",
    0x0D: "\\r",
    0x22: '\\"',
    0x5C: "\\\\",
}


def _escape_string(value: str) -> str:
    out = ['"']
    for ch in value:
        cp = ord(ch)
        short = _SHORT_ESCAPES.get(cp)
        if short is not None:
            out.append(short)
        elif cp < 0x20:
            # All other control characters use lowercase 4-hex-digit form.
            out.append(f"\\u{cp:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _sort_key(key: str) -> bytes:
    """RFC 8785 sorts object keys by their UTF-16 code units.

    Python compares str by code point, which disagrees with UTF-16 ordering for
    supplementary-plane characters (a surrogate pair starts with 0xD800-0xDBFF, which
    sorts below BMP characters above 0xE000). Comparing the UTF-16 big-endian encoding
    as bytes reproduces UTF-16 code-unit order exactly.
    """
    return key.encode("utf-16-be")


def _serialize(value: Any, path: str) -> str:
    # bool must precede int: in Python, bool is a subclass of int.
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        raise CanonicalizationError(
            f"float at {path}: this profile canonicalizes integers only. "
            "Money must be integer minor units; see commerce_domain.money.Money."
        )
    if isinstance(value, str):
        return _escape_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_serialize(v, f"{path}[{i}]") for i, v in enumerate(value)) + "]"
    if isinstance(value, dict):
        # Validate before sorting: sorting a non-string key would raise AttributeError
        # from the UTF-16 sort key rather than a domain error the caller can act on.
        for key in value:
            if not isinstance(key, str):
                raise CanonicalizationError(
                    f"non-string object key {key!r} at {path}: JSON object keys must be strings"
                )
        parts = [
            f"{_escape_string(key)}:{_serialize(value[key], f'{path}.{key}')}"
            for key in sorted(value, key=_sort_key)
        ]
        return "{" + ",".join(parts) + "}"
    raise CanonicalizationError(f"cannot canonicalize {type(value).__name__} at {path}")


def canonicalize(value: Any) -> bytes:
    """Return the RFC 8785 canonical UTF-8 encoding of ``value``.

    Deterministic: the same semantic value always yields the same bytes.
    """
    return _serialize(value, "$").encode("utf-8")


def canonicalize_str(value: Any) -> str:
    """Canonical form as ``str``. Prefer :func:`canonicalize`; signatures are over bytes."""
    return _serialize(value, "$")
