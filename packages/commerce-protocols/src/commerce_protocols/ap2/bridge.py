"""The canonical UCP/AP2 bridge, specification 15.4.

One rule, used identically by the platform, the merchant gateway and the local PSP-verifier
bridge, for turning a UCP checkout into the two AP2 artifacts that refer to it: a detached
merchant authorization, and a transaction correlation value that binds a payment mandate to
this exact checkout and no other.

The specification gives eight steps and this module is those eight steps, in order, with
nothing between them:

1. remove the ``ap2`` member from the checkout
2. serialize what remains with RFC 8785 JCS
3. base64url-encode the protected ES256 header and the exact JCS payload, unpadded
4. sign the ASCII string ``header.payload``
5. reconstruct the compact checkout JWT as ``header.payload.signature``
6. compute ``SHA-256`` over the ASCII of that compact JWT
7. use the encoded hash as the payment mandate's transaction correlation value
8. keep the UCP detached merchant authorization as a separate artifact

Why every step is byte-exact, and why that is the whole difficulty
------------------------------------------------------------------
Steps 2 and 6 both hash something. If any participant serializes the checkout even slightly
differently -- a space after a colon, a different key order, a non-minimal number, a
different Unicode escape -- it computes a different payload, a different signature and a
different correlation value. Nothing detects this as a canonicalization difference. It
surfaces as "the merchant signature does not verify", which every participant will read as
tampering, and the bug will be looked for in the wrong place for a long time.

RFC 8785 exists to make that impossible, and it is used here rather than
``json.dumps(sort_keys=True)`` because the two are not the same function: JCS also pins
number formatting to ECMAScript's algorithm and pins string escaping, and those are exactly
the cases where two well-intentioned implementations diverge. The platform already has a
tested JCS in ``commerce_domain``; this module uses it rather than bringing a second one,
so the bridge and the approval hash can never disagree about what canonical means.

Step 1 deserves its own note. The ``ap2`` member is removed before serializing because it is
the container the signature will eventually live in, and a signature cannot cover itself.
Removing it is what makes the merchant's authorization verifiable by someone who has the
complete, assembled checkout in front of them: they delete the same member and get back the
same bytes.

Status, stated as specification 15.4 requires
----------------------------------------------
This bridge is an **implementation target, verified only when its golden vectors pass
against the pinned AP2 and UCP sources**. :mod:`commerce_protocols.ap2.vectors` holds those
vectors and the suite that runs them. Until they pass, no status table may describe the
byte-level bridge as verified. Reading a specification is not conformance; the vector is.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from commerce_domain import b64url, canonicalize, sha256_b64url

from ..core.errors import SchemaRejected, SignatureRejected
from .signing import SIGNING_ALGORITHM, KeyRing, Signer, protected_header, verify_compact

__all__ = [
    "AP2_MEMBER",
    "BridgeArtifacts",
    "bind_checkout",
    "compact_from_detached",
    "detach",
    "jcs_payload",
    "transaction_id_for",
    "verify_merchant_authorization",
]

#: The member removed before signing. Named as a constant because three different places
#: have to agree on the spelling and a typo would produce a signature over the wrong bytes
#: that still verified against itself.
AP2_MEMBER: Final[str] = "ap2"

#: The protected header's ``typ``. Fixed so the golden vector's header bytes are stable;
#: a header that varied between runs would make the vector unreproducible, which would
#: defeat its entire purpose.
CHECKOUT_JWT_TYP: Final[str] = "JWT"


def jcs_payload(checkout: Mapping[str, Any]) -> bytes:
    """Steps 1 and 2: drop the ``ap2`` member, then RFC 8785 the remainder.

    A shallow copy is taken rather than mutating the caller's mapping. The caller usually
    holds the assembled checkout it is about to send, and silently deleting a member from
    it would be a spectacularly confusing action at a distance.

    Only the top-level ``ap2`` member is removed. Nested occurrences are left alone
    deliberately -- the UCP merchant-authorization rule is about the checkout's own ``ap2``
    container, and stripping a member of that name from arbitrary depth would quietly
    corrupt a line item that happened to have one.
    """
    if not isinstance(checkout, Mapping):
        raise SchemaRejected("checkout_is_not_an_object")
    remainder = {k: v for k, v in checkout.items() if k != AP2_MEMBER}
    return canonicalize(remainder)


@dataclass(frozen=True, slots=True)
class BridgeArtifacts:
    """Everything step 8 says to keep, and the intermediate bytes that prove each step.

    The intermediates are carried rather than recomputed because they are what a golden
    vector is made of. A vector that only recorded the final hash would tell you *that* two
    implementations disagree and never which of the eight steps they disagreed at, which is
    the one question worth answering when a bridge fails.
    """

    #: Step 2. The exact bytes that were signed over, before encoding.
    jcs_bytes: bytes
    #: Step 3. The protected header, as the JSON that was encoded -- not as a dict, because
    #: the dict does not determine the bytes and the bytes are what is signed.
    protected_json: str
    #: Step 3, encoded.
    header_b64: str
    payload_b64: str
    #: Step 5. ``header.payload.signature``.
    compact_checkout_jwt: str
    #: Step 8. ``header..signature`` -- the UCP detached merchant authorization.
    detached_merchant_authorization: str
    #: Steps 6 and 7. base64url(SHA-256(ASCII(compact_checkout_jwt))).
    transaction_id: str

    @property
    def signature_b64(self) -> str:
        return self.compact_checkout_jwt.rsplit(".", 1)[1]

    def as_vector(self, public_keys: Mapping[str, Any]) -> dict[str, Any]:
        """This binding as a committable golden vector, specification 15.4.

        ``jcs_bytes`` is emitted as UTF-8 text *and* as its byte length. The text is what a
        reviewer reads; the length is what catches the one failure text alone would hide,
        which is a trailing newline or a BOM that a file round-trip introduced.

        No private key can reach this dictionary: the only key material is whatever the
        caller passes as ``public_keys``, and every caller in this package passes
        :meth:`KeyRing.public_jwks`, which exports public halves only.
        """
        return {
            "jcs_utf8": self.jcs_bytes.decode("utf-8"),
            "jcs_byte_length": len(self.jcs_bytes),
            "protected_header_json": self.protected_json,
            "header_b64": self.header_b64,
            "payload_b64": self.payload_b64,
            "compact_checkout_jwt": self.compact_checkout_jwt,
            "detached_merchant_authorization": self.detached_merchant_authorization,
            "transaction_id": self.transaction_id,
            "public_keys": dict(public_keys),
        }


def detach(compact: str) -> str:
    """Step 8: ``header.payload.signature`` becomes ``header..signature``.

    The detached form is what UCP carries as ``ap2.merchant_authorization``, and specification
    15.2 requires exactly this shape. The payload is omitted rather than emptied because the
    checkout itself is already present in the document the authorization travels in;
    including it again would let the two copies disagree, and a verifier would then have to
    decide which one the signature covered.
    """
    parts = compact.split(".")
    if len(parts) != 3:
        raise SignatureRejected("not_a_compact_jws", segments=len(parts))
    return f"{parts[0]}..{parts[2]}"


def compact_from_detached(detached: str, payload_b64: str) -> str:
    """Reattach a payload to a detached JWS, giving back a verifiable compact JWS.

    This is the operation a verifier performs, and it is the mirror of :func:`detach`. The
    ``payload_b64`` a verifier supplies is one it recomputed itself from the checkout in
    front of it -- never one the sender provided -- which is precisely what makes the
    detached form safe: the signature is checked against the verifier's own reading of the
    document, so a sender cannot present bytes that differ from the ones it signed.
    """
    parts = detached.split(".")
    if len(parts) != 3 or parts[1] != "":
        raise SignatureRejected("not_a_detached_jws")
    return f"{parts[0]}.{payload_b64}.{parts[2]}"


def transaction_id_for(compact_checkout_jwt: str) -> str:
    """Steps 6 and 7: the payment mandate's correlation value.

    ASCII rather than UTF-8, and that is not pedantry. A compact JWS is base64url plus two
    dots, so every byte is ASCII by construction, and encoding as ASCII turns "somebody
    passed me something that is not a compact JWS" into an immediate error instead of a
    silently different hash. The two encodings agree on every input this can legitimately
    receive; they differ exactly on the inputs that should be refused.
    """
    try:
        raw = compact_checkout_jwt.encode("ascii")
    except UnicodeEncodeError as exc:
        raise SignatureRejected("compact_jwt_is_not_ascii") from exc
    return sha256_b64url(raw)


def bind_checkout(checkout: Mapping[str, Any], signer: Signer) -> BridgeArtifacts:
    """Run all eight steps and return every artifact they produced.

    The signature is computed over ``header.payload`` assembled here rather than by handing
    the payload to a library and trusting it to encode identically. That is the difference
    between a bridge that agrees with its peers and one that agrees with itself: the bytes
    that get signed are the bytes this function built, and they are returned so a vector can
    assert on them.
    """
    payload = jcs_payload(checkout)
    protected = {
        "alg": SIGNING_ALGORITHM,
        "typ": CHECKOUT_JWT_TYP,
        "kid": signer.kid,
    }
    # Serialized with the tightest separators so the header bytes are determined by this
    # dictionary and nothing else. json.dumps' default separators insert spaces, which would
    # be perfectly valid JSON and a different signing input.
    protected_json = json.dumps(protected, separators=(",", ":"), sort_keys=True)
    header_b64 = b64url(protected_json.encode("utf-8"))
    payload_b64 = b64url(payload)

    compact = signer.sign(protected, payload)
    # The signer serializes the header and payload independently of the bytes computed
    # above. If either encoding differs, the artifacts returned here would describe bytes
    # that were never signed -- and a golden vector built from them would be a confident,
    # reproducible lie. Both are compared rather than assumed, because the whole value of
    # this module is that its recorded intermediates are the real ones.
    signed_header, signed_payload, signature = compact.split(".")
    if signed_header != header_b64:
        raise SignatureRejected(
            "signer_header_encoding_differs_from_canonical",
            expected=header_b64,
            actual=signed_header,
        )
    if signed_payload != payload_b64:
        raise SignatureRejected(
            "signer_payload_encoding_differs_from_jcs",
            expected_length=len(payload_b64),
            actual_length=len(signed_payload),
        )

    compact_checkout_jwt = f"{header_b64}.{payload_b64}.{signature}"
    return BridgeArtifacts(
        jcs_bytes=payload,
        protected_json=protected_json,
        header_b64=signed_header,
        payload_b64=payload_b64,
        compact_checkout_jwt=compact_checkout_jwt,
        detached_merchant_authorization=detach(compact_checkout_jwt),
        transaction_id=transaction_id_for(compact_checkout_jwt),
    )


def verify_merchant_authorization(
    checkout: Mapping[str, Any],
    detached: str,
    ring: KeyRing,
) -> BridgeArtifacts:
    """Verify a detached merchant authorization against a checkout, specification 15.3.

    The verifier recomputes the JCS payload from the checkout it was given, reattaches it to
    the detached signature, and verifies. That ordering is the security property: the
    signature is checked against *this* verifier's canonicalization of *this* document, so a
    merchant cannot sign one set of bytes and present another, and a relay cannot edit the
    checkout in transit without the signature failing.

    Returns the same :class:`BridgeArtifacts` a signer would have produced, so a caller that
    has verified an authorization immediately holds the correlation value it needs to check
    the payment mandate against -- rather than having to recompute it and risk computing it
    differently.
    """
    payload = jcs_payload(checkout)
    payload_b64 = b64url(payload)
    compact = compact_from_detached(detached, payload_b64)

    # The header is read for the artifacts only. verify_compact re-reads it and pins the
    # algorithm itself, so nothing is trusted on the strength of this decode.
    header = protected_header(compact)
    verify_compact(compact, ring)

    return BridgeArtifacts(
        jcs_bytes=payload,
        protected_json=json.dumps(header, separators=(",", ":"), sort_keys=True),
        header_b64=compact.split(".")[0],
        payload_b64=payload_b64,
        compact_checkout_jwt=compact,
        detached_merchant_authorization=detached,
        transaction_id=transaction_id_for(compact),
    )
