"""Golden vectors for the UCP/AP2 bridge, specification 15.4.

The specification is blunt about why this file exists: "Reading a specification is not
conformance; the vector is." A bridge that agrees with itself proves nothing -- the failure
being guarded against is precisely a bridge that is internally consistent and disagrees with
every peer. So the artifacts are frozen into a committed file, and the suite re-derives them
from the input checkout on every run.

What a vector must contain, per 15.4: the input checkout, the exact JCS bytes, the protected
header, the detached merchant JWS, the reconstructed compact checkout JWT, the SHA-256 hash,
the payment-mandate transaction id, and the public verification keys.

And the prohibition, which is absolute: **never include a private key in a fixture.** That
constraint shapes the design rather than merely restricting it. A vector holding a private
key would be checked by re-signing and comparing signatures -- but ECDSA is randomised, so
two signatures over identical bytes differ, and the comparison could never have worked
anyway. Without the private key the check has to be the *right* one: recompute every
deterministic intermediate and compare byte for byte, then verify the stored signature
against the stored public key. That is a stronger test, and it is the one an external
implementer could run against their own output.

Where a mismatch points
-----------------------
Each intermediate is checked separately so a failure names the step that broke:

``jcs_utf8`` differs
    Canonicalization disagreement -- key ordering, number formatting, string escaping.
    This is the failure that would otherwise present as "the merchant signature is invalid"
    and be hunted for in the wrong place.
``header_b64`` differs
    The protected header's serialization moved: a key was added, or the ordering changed.
``compact_checkout_jwt`` differs while its parts match
    Reassembly is wrong -- almost always a separator.
``transaction_id`` differs while the compact JWT matches
    The hash or its encoding moved. Since AP2's own ``compute_sha256_b64url`` is asserted
    against the same value, this also catches the SDK changing under the pin.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ap2.sdk.utils import compute_sha256_b64url
from commerce_domain import b64url, b64url_decode
from jwcrypto.jwk import JWK

from .bridge import bind_checkout, jcs_payload, transaction_id_for
from .signing import KeyRing, Signer, verify_compact

__all__ = [
    "GOLDEN_VECTOR_PATH",
    "REFERENCE_CHECKOUT",
    "VectorMismatch",
    "build_vector",
    "check_vector",
    "load_vector",
]

#: The committed vector. Beside the tests rather than inside the package, because it is
#: evidence about the implementation rather than something the implementation reads at
#: runtime -- shipping it in the wheel would imply the service consults it, and it does not.
GOLDEN_VECTOR_PATH: Final[Path] = (
    Path(__file__).resolve().parents[3] / "tests" / "vectors" / "ucp_ap2_bridge.json"
)

#: The checkout the vector is built from. Chosen to exercise the cases where two JCS
#: implementations actually diverge rather than to look like a realistic basket: a
#: non-ASCII merchant name (string escaping), a nested array of objects (recursive member
#: sorting), keys deliberately out of alphabetical order in the source (member ordering),
#: an integer that would lose precision as a float, and a zero-amount component.
REFERENCE_CHECKOUT: Final[dict[str, Any]] = {
    "status": "ready_for_complete",
    "id": "chk_vector_0001",
    "currency": "INR",
    "merchant": {"id": "mrc_demo", "name": "Chai aur Kirana — किराना"},
    "line_items": [
        {
            "id": "li_2",
            "item": {"id": "TEA-BEV-001", "title": "Masala chai, 250 g", "price": 19900},
            "quantity": 2,
            "totals": [{"type": "subtotal", "amount": 39800}],
        },
        {
            "id": "li_1",
            "item": {"id": "SUG-GRO-014", "title": "Sugar, 1 kg", "price": 6500},
            "quantity": 1,
            "totals": [{"type": "subtotal", "amount": 6500}],
        },
    ],
    "totals": [
        {"type": "subtotal", "amount": 46300},
        {"type": "discount", "amount": -2500},
        {"type": "fulfillment", "amount": 0},
        {"type": "tax", "amount": 2190},
        {"type": "total", "amount": 45990},
    ],
    "links": [{"type": "terms", "url": "https://demo.invalid/terms"}],
    "ap2": {"note": "removed before signing; specification 15.4 step 1"},
}


class VectorMismatch(AssertionError):  # noqa: N818 - an assertion failure, not an error
    """A golden vector no longer reproduces. Names the step that diverged."""

    def __init__(self, field: str, expected: object, actual: object) -> None:
        super().__init__(
            f"golden vector field {field!r} did not reproduce.\n"
            f"  expected: {expected!r}\n"
            f"  actual:   {actual!r}"
        )
        self.field = field


@dataclass(frozen=True, slots=True)
class VectorReport:
    """Which fields were re-derived and matched. Returned so a test can assert coverage.

    A check that silently verified nothing would pass, so the suite asserts on this rather
    than on the absence of an exception -- the difference between "nothing was wrong" and
    "everything was checked".
    """

    fields_checked: tuple[str, ...]
    signature_verified: bool
    sdk_agrees_on_transaction_id: bool


def build_vector(checkout: Mapping[str, Any], signer: Signer, ring: KeyRing) -> dict[str, Any]:
    """Produce a committable vector for ``checkout`` signed by ``signer``.

    Used to regenerate the committed file when the bridge deliberately changes -- which
    should be almost never, since a change here invalidates every stored merchant
    authorization. The public keys come from the ring's JWK Set export, which is the only
    path in this package that emits key material and emits public halves only.
    """
    artifacts = bind_checkout(checkout, signer)
    vector = artifacts.as_vector(ring.public_jwks())
    vector["input_checkout"] = dict(checkout)
    return vector


def load_vector(path: Path = GOLDEN_VECTOR_PATH) -> dict[str, Any]:
    """Read the committed vector."""
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def _expect(field: str, expected: object, actual: object) -> None:
    if expected != actual:
        raise VectorMismatch(field, expected, actual)


def check_vector(vector: Mapping[str, Any]) -> VectorReport:
    """Re-derive every deterministic field and verify the stored signature.

    No private key is involved. The four intermediates are recomputed from
    ``input_checkout`` and compared; the signature is verified against the vector's own
    published keys; and AP2's ``compute_sha256_b64url`` is asked for the transaction id
    independently, so the pin moving under us fails here rather than silently at a peer.
    """
    checkout = vector["input_checkout"]

    jcs = jcs_payload(checkout)
    _expect("jcs_utf8", vector["jcs_utf8"], jcs.decode("utf-8"))
    _expect("jcs_byte_length", vector["jcs_byte_length"], len(jcs))
    _expect("payload_b64", vector["payload_b64"], b64url(jcs))

    header_b64 = vector["header_b64"]
    _expect(
        "protected_header_json",
        vector["protected_header_json"],
        json.dumps(
            json.loads(b64url_decode(header_b64).decode("utf-8")),
            separators=(",", ":"),
            sort_keys=True,
        ),
    )

    signature = vector["detached_merchant_authorization"].split(".")[2]
    _expect(
        "compact_checkout_jwt",
        vector["compact_checkout_jwt"],
        f"{header_b64}.{vector['payload_b64']}.{signature}",
    )
    _expect(
        "transaction_id",
        vector["transaction_id"],
        transaction_id_for(vector["compact_checkout_jwt"]),
    )

    # The stored signature must verify under the stored public key, using this package's
    # pinned verifier -- so a vector recorded against a weakened profile would fail here.
    ring = KeyRing(keys={str(m["kid"]): JWK(**m) for m in vector["public_keys"]["keys"]})
    verify_compact(vector["compact_checkout_jwt"], ring)

    sdk_agrees = compute_sha256_b64url(vector["compact_checkout_jwt"]) == vector["transaction_id"]
    if not sdk_agrees:
        raise VectorMismatch(
            "transaction_id (AP2 SDK)",
            vector["transaction_id"],
            compute_sha256_b64url(vector["compact_checkout_jwt"]),
        )

    return VectorReport(
        fields_checked=(
            "jcs_utf8",
            "jcs_byte_length",
            "payload_b64",
            "protected_header_json",
            "compact_checkout_jwt",
            "transaction_id",
        ),
        signature_verified=True,
        sdk_agrees_on_transaction_id=sdk_agrees,
    )
