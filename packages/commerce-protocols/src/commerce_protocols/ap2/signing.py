"""The ES256 signing profile, and the algorithm pin the AP2 SDK does not have.

Specification 15.1 narrows this project to one cryptographic profile: ``ES256`` on P-256,
and nothing else. It is worth being precise about whose decision that is. AP2 itself permits
a broader profile; the narrowing is ours, taken because every additional accepted algorithm
is another set of downgrade interactions to reason about and we would rather reason about
none. The specification says to state it that way, so this module states it that way.

The pin is not decoration, and this is the module's reason to exist
------------------------------------------------------------------
The pinned AP2 SDK does not enforce a signature algorithm anywhere. ``ap2.sdk.jwt_helper``
hardcodes ES256 when *signing*, but on the verifying side it calls ``jws.verify(key)`` with
no ``alg`` argument, which means jwcrypto falls back to its ``default_allowed_algs`` -- a
fourteen-entry list including every HMAC variant. Handed an attacker-chosen token, that
verifier reads the algorithm out of the token's own protected header and honours it.

The classic consequence is the JWS algorithm-confusion attack: a token whose header says
``HS256``, signed with the *public* key's bytes used as an HMAC secret. Our public keys are
published in a JWK Set, exactly as specification 14.1 requires, so that secret is not a
secret. Against an unpinned verifier holding a public EC key, that forgery verifies.

So :func:`verify_compact` reads the protected header first, refuses anything that is not
``ES256``, refuses a ``kid`` it cannot resolve, and only then hands the token to a verifier
whose ``allowed_algs`` is a one-element list. Two independent refusals for the same fact,
because this is the check that everything else in AP2 rests on.

Key handling, specification 15.5
--------------------------------
A private key exists in exactly one place: inside a :class:`Signer`. Nothing here returns
one, logs one, serialises one into evidence or puts one in an error message.
:meth:`Signer.public_jwk` exports the public half and asserts that it did -- a mistake that
exported a private key would otherwise be a silent catastrophe, so it is turned into a
loud one.

Merchant, platform and buyer keys are separate objects with separate ``kid`` values, which
is what makes "the merchant signed this" a different claim from "the platform signed this".
Collapsing them into one key would make the AP2 verification sequence's step 3 -- resolve
the correct key by ``kid`` -- vacuous.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final
from typing import Protocol as TypingProtocol

from commerce_domain import b64url_decode
from jwcrypto.jwk import JWK
from jwcrypto.jws import JWS

from ..core.errors import SignatureRejected

__all__ = [
    "ALLOWED_ALGORITHMS",
    "SIGNING_ALGORITHM",
    "InProcessSigner",
    "KeyRing",
    "Signer",
    "protected_header",
    "verify_compact",
]

#: The one algorithm this project signs with and the one it accepts. Specification 15.1.
SIGNING_ALGORITHM: Final[str] = "ES256"

#: Passed to jwcrypto as ``allowed_algs``. A list of one, deliberately: the type invites a
#: second entry and there must never be one, so the constant is the place that says so.
ALLOWED_ALGORITHMS: Final[list[str]] = [SIGNING_ALGORITHM]

#: The only curve ES256 is defined over. Checked explicitly because a JWK claiming
#: ``alg: ES256`` over a different curve is a contradiction, and a verifier that silently
#: resolved it would be accepting a key nobody described.
REQUIRED_CURVE: Final[str] = "P-256"


class Signer(TypingProtocol):
    """The internal signing interface specification 15.5 asks to be exposed.

    Deliberately narrow. A caller can ask for a signature and can ask which public key will
    verify it; there is no operation that yields key material. That is what makes it safe to
    hand a signer to the parts of this package that build mandates and receipts.

    The indirection is also what makes Cloud KMS a later substitution rather than a rewrite.
    Specification 15.5 is firm that a KMS-backed path may not be claimed until its DER
    signature is converted to JWS raw format and passes the same golden vectors, so this
    interface is the seam that swap would happen at -- and the vectors are the gate it would
    have to pass through.
    """

    @property
    def kid(self) -> str:
        """The stable key identifier that goes in the protected header."""
        ...

    def public_jwk(self) -> dict[str, Any]:
        """The public half, for publication in a JWK Set. Never the private half."""
        ...

    def sign(self, header: dict[str, Any], payload: bytes) -> str:
        """A compact JWS over exactly these payload bytes.

        Bytes rather than a mapping, and that signature is the whole safety property. A
        signer given a mapping has to serialize it, and its serialization will differ from
        its caller's -- ``ensure_ascii`` alone is enough, since it escapes every non-ASCII
        character and RFC 8785 does not. The caller has already decided what the canonical
        bytes are; the signer's job is to sign those and not to have an opinion.
        """
        ...


@dataclass(frozen=True, slots=True)
class InProcessSigner:
    """A :class:`Signer` holding an in-memory P-256 private key.

    "In process" is a claim about deployment, and specification 15.5 draws the line
    carefully: the demonstration loads an encrypted ES256 test key from Secret Manager into
    a dedicated signer module, and that is the honest description of this class. It is not
    a KMS-backed signer and must not be described as one.
    """

    key: JWK
    _kid: str

    @classmethod
    def from_jwk(cls, key: JWK) -> InProcessSigner:
        """Wrap a JWK, insisting it is a private P-256 key carrying a ``kid``.

        Every one of these three is checked at construction rather than at first use.
        A signer built from a public key would fail at the first signature with an error
        from three layers down; a signer with no ``kid`` would silently produce headers no
        verifier can resolve, which is worse, because the failure surfaces at the peer.
        """
        material = json.loads(key.export())
        if material.get("kty") != "EC":
            raise ValueError(f"signing key must be EC, got {material.get('kty')!r}")
        if material.get("crv") != REQUIRED_CURVE:
            raise ValueError(
                f"signing key must be on {REQUIRED_CURVE} for {SIGNING_ALGORITHM}, "
                f"got {material.get('crv')!r}"
            )
        if "d" not in material:
            raise ValueError("signing key has no private component; this is a public key")
        kid = material.get("kid")
        if not kid:
            raise ValueError(
                "signing key carries no kid; a verifier resolves keys by kid and a "
                "signature nobody can attribute is not evidence of anything"
            )
        return cls(key=key, _kid=str(kid))

    @property
    def kid(self) -> str:
        return self._kid

    def public_jwk(self) -> dict[str, Any]:
        """The public half only, asserted rather than assumed.

        ``JWK.export_public`` is the correct call and this checks its output anyway. The
        cost is one dictionary lookup; the cost of being wrong is publishing a private key
        to a well-known URL, which is not a failure anybody recovers from by rotating.
        """
        material: dict[str, Any] = json.loads(self.key.export_public())
        if "d" in material:  # pragma: no cover - jwcrypto does not do this
            raise RuntimeError(
                "export_public returned a private component; refusing to hand it out"
            )
        return material

    def sign(self, header: dict[str, Any], payload: bytes) -> str:
        """Sign exactly ``payload``, forcing the protected header's algorithm and key id.

        The caller's ``alg`` and ``kid`` are overwritten rather than validated. A caller
        that asked for the wrong algorithm has made a mistake, and correcting it is both
        safe and the only outcome that could be right -- this signer holds one key and can
        only produce one kind of signature, so honouring a different request was never
        possible.
        """
        protected = {**header, "alg": SIGNING_ALGORITHM, "kid": self._kid}
        # The payload is passed through untouched. Re-encoding it here is precisely the bug
        # this interface exists to prevent.
        jws = JWS(payload)
        jws.add_signature(
            self.key,
            alg=SIGNING_ALGORITHM,
            # Sorted and tightly separated, so the header bytes are a function of the
            # header's *content* and not of the order a caller happened to build it in.
            # The UCP/AP2 bridge recomputes these bytes independently and compares; without
            # a canonical ordering here the two would differ for the same header, and the
            # bridge would record a protected header that is not the one that was signed.
            protected=json.dumps(protected, separators=(",", ":"), sort_keys=True),
        )
        serialized: str = jws.serialize(compact=True)
        return serialized


@dataclass(frozen=True, slots=True)
class KeyRing:
    """Public keys by ``kid``, for verification. Never holds a private key.

    Rotation is why this is a mapping rather than a single key. Specification 14.1 requires
    that keys rotate "without silently invalidating stored evidence": a receipt signed last
    month under ``platform-key-1`` must still verify after ``platform-key-2`` becomes the
    signing key. A ring that only held the current key would fail every historical
    verification the moment a rotation happened, and would fail it *silently*, as an
    ordinary bad-signature result.

    So retiring a key means removing it from the signer, not from the ring. A key leaves the
    ring only when the evidence it signed is no longer being verified at all.
    """

    keys: dict[str, JWK]

    @classmethod
    def of(cls, *signers: Signer) -> KeyRing:
        """A ring holding the public halves of these signers.

        Refuses two signers sharing a ``kid``. A collision is not a configuration wrinkle:
        it means a verifier resolving that ``kid`` would pick one of two keys by dictionary
        order, so "the merchant signed this" and "the platform signed this" would become the
        same statement whenever the wrong one happened to win.
        """
        ring: dict[str, JWK] = {}
        for signer in signers:
            if signer.kid in ring:
                raise ValueError(
                    f"two signers share kid {signer.kid!r}; a kid must resolve to exactly "
                    "one key or attribution means nothing"
                )
            ring[signer.kid] = JWK(**signer.public_jwk())
        return cls(keys=ring)

    def resolve(self, kid: str | None) -> JWK:
        """The public key for ``kid``, or refuse.

        An absent ``kid`` is refused rather than defaulted to the only key in a
        single-key ring. Guessing works right up until a second key exists, at which point
        the guess silently changes meaning -- and the rotation that adds the second key is
        exactly when nobody is looking for this.
        """
        if not kid:
            raise SignatureRejected("protected_header_carries_no_kid")
        key = self.keys.get(kid)
        if key is None:
            raise SignatureRejected("unknown_kid", kid=kid)
        return key

    def public_jwks(self) -> dict[str, Any]:
        """The ring as a JWK Set, the shape specification 14.1 publishes."""
        return {"keys": [json.loads(key.export_public()) for key in self.keys.values()]}


def protected_header(token: str) -> dict[str, Any]:
    """The protected header of a compact JWS, decoded but **not** trusted.

    Reading a header before verifying anything is unavoidable -- the header is where the
    ``kid`` lives, and the key cannot be resolved without it -- but it is worth naming the
    hazard: at this point nothing in the returned dictionary has been authenticated. The
    only two things a caller may do with it are select a key and refuse an algorithm. Any
    other use is trusting an attacker's input.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise SignatureRejected("not_a_compact_jws", segments=len(parts))
    try:
        header: dict[str, Any] = json.loads(b64url_decode(parts[0]))
    except Exception as exc:
        raise SignatureRejected("protected_header_not_decodable") from exc
    if not isinstance(header, dict):
        raise SignatureRejected("protected_header_not_an_object")
    return header


def verify_compact(token: str, ring: KeyRing) -> dict[str, Any]:
    """Verify a compact JWS under the pinned profile and return its payload.

    The order is the point:

    the header is decoded, the algorithm is refused unless it is exactly ``ES256``, the
    ``kid`` is resolved against the ring, and only then is the signature checked -- by a
    ``JWS`` whose ``allowed_algs`` has been narrowed to one entry, so that even if the
    explicit check above were somehow bypassed the library would still refuse.

    Refusals are deliberately uninformative. A caller learns the token did not verify and
    which coarse stage refused it; it does not learn whether the algorithm was wrong, the
    key unknown or the bytes tampered with. A verifier that distinguishes those is a
    verifier an attacker can interrogate one bit at a time.
    """
    header = protected_header(token)
    algorithm = header.get("alg")
    if algorithm != SIGNING_ALGORITHM:
        # Refused before any key is touched. An algorithm-confusion attempt must not reach
        # a code path that has a key in scope at all.
        raise SignatureRejected("algorithm_not_permitted", announced=str(algorithm))
    key = ring.resolve(header.get("kid"))

    jws = JWS()
    jws.allowed_algs = ALLOWED_ALGORITHMS
    try:
        jws.deserialize(token)
        jws.verify(key, alg=SIGNING_ALGORITHM)
    except Exception as exc:
        # jwcrypto raises its own JWException family here, not ValueError, and a malformed
        # payload raises from json. All of them mean the same thing to a caller.
        raise SignatureRejected("signature_did_not_verify") from exc

    try:
        payload: dict[str, Any] = json.loads(jws.payload)
    except Exception as exc:
        raise SignatureRejected("verified_payload_is_not_json") from exc
    if not isinstance(payload, dict):
        raise SignatureRejected("verified_payload_is_not_an_object")
    return payload
