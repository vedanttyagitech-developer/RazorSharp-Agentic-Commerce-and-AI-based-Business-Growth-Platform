"""The storefront's checkout vocabulary and ``CheckoutState`` must agree exactly.

This suite exists because of a real defect. ``CHECKOUT_STATES`` in the buyer storefront
declared sixteen strings under a docstring citing specification 8.2. Six of them were not
checkout states at all -- ``SUBMITTED``, ``RECONCILING`` and ``STALE_CAPTURE`` belong to
``PaymentState``, and ``PAYMENT_PENDING``, ``REJECTED`` and ``FAILED`` exist nowhere as a
checkout state -- and four real ones were absent, including ``AWAITING_PAYMENT``, which
every paying buyer passes through, and ``INVALIDATED_AWAITING_PAYMENT_RESULT``, the
specification 31.2 late-capture demonstration.

The cause was not carelessness about any one name. Specification 8.2 describes sixteen UI
*journey stages in prose*, spanning the checkout, payment and refund machines; someone read
that prose as a list of checkout states and matched the count. Nothing then checked the
result against the enum it claimed to mirror, so the wrong list sat behind an authoritative
citation and drifted for as long as it liked. A hand-maintained copy of somebody else's
vocabulary is only safe if something fails when it stops being a copy. This is that thing.

**Why the check lives in the Python suite.** TypeScript cannot import a Python enum, so a
check on the storefront side would have to compare ``CHECKOUT_STATES`` against a second
hardcoded list of the kernel's states -- a duplicate of exactly the artefact that drifted,
verifying itself. Only a Python test can hold the real ``CheckoutState`` and read the
storefront's declaration as text, which is what makes the comparison mean anything.

**Why in commerce-api.** The kernel does not know a storefront exists and should not start
now. This package is the boundary that serialises ``checkout.state`` onto the wire the
storefront parses, so the agreement between the two is this package's contract to keep.
It is the same shape of test as ``platform-db``'s ``test_schema_state_agreement``, which
pins the database CHECK constraints to the same enum, one layer down.

Equality is asserted in BOTH directions on purpose. A subset check in either direction
passes while half the drift is still present:

- enum ⊄ storefront: the API returns a state the storefront has never heard of. It does
  not crash -- the raw name is rendered -- but the buyer is shown an enum member instead of
  a sentence, at the moment they most need a sentence.
- storefront ⊄ enum: the storefront carries a state the platform can never produce, and
  with it copy no buyer can ever be shown. Dead copy is worse than missing copy, because it
  reads as coverage.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from transaction_kernel.states import CheckoutState

#: packages/commerce-api/tests/ -> packages/commerce-api/ -> packages/ -> repository root.
_REPO_ROOT = Path(__file__).resolve().parents[3]

STOREFRONT_TYPES = _REPO_ROOT / "apps" / "buyer-web" / "src" / "lib" / "api" / "types.ts"

STATE_BANNER = (
    _REPO_ROOT / "apps" / "buyer-web" / "src" / "features" / "checkout" / "state-banner.tsx"
)

#: Skipped when there is no storefront to check, which is the same distinction
#: ``test_proxy_mint_guards`` and ``test_voice_wire_contract`` already draw: a missing
#: sibling app is a different fact from a stale list, and a test that failed for the first
#: would be failing for a reason it cannot fix.
#:
#: The whole front end was deleted on 2026-09-09. Kept rather than removed so that a
#: storefront arriving later has this contract enforced again without anybody remembering
#: to write it: the kernel's own state vocabulary is the authority either way, and this is
#: the only thing that has ever held a client's copy of it in step.
pytestmark = pytest.mark.skipif(
    not STOREFRONT_TYPES.exists(),
    reason="no storefront in this checkout; the contract has nothing to hold in step",
)

#: The six names that were in ``CHECKOUT_STATES`` and are not checkout states, kept by name
#: so a re-introduction is reported as the specific mistake it is rather than as an
#: anonymous set difference.
PHANTOMS = {
    "SUBMITTED": "a PaymentState: a payment attempt is submitted, a checkout is not",
    "PAYMENT_PENDING": "not a state at all; the kernel's name is AWAITING_PAYMENT",
    "RECONCILING": "a PaymentState: reconciliation happens to an attempt, not to a checkout",
    "STALE_CAPTURE": "a PaymentState: a capture is stale, a checkout is not",
    "REJECTED": "no such checkout state; declining a version answers CANCELLED",
    "FAILED": "a PaymentState; the checkout equivalent is PAYMENT_FAILED",
}


def _declared_states() -> list[str]:
    """The storefront's ``CHECKOUT_STATES``, in the order the file lists them.

    Read from the source text rather than from a build artefact or a running dev server,
    so this fails on the commit that introduces the drift rather than on whoever next
    happens to run the storefront.
    """
    source = STOREFRONT_TYPES.read_text()
    match = re.search(
        r"export const CHECKOUT_STATES = \[(.*?)\] as const;",
        source,
        re.S,
    )
    assert match, f"no `export const CHECKOUT_STATES = [...] as const;` in {STOREFRONT_TYPES}"
    return re.findall(r'"([A-Z_]+)"', match.group(1))


def _named(states: set[str]) -> str:
    return ", ".join(sorted(states)) if states else "none"


class TestStorefrontCheckoutVocabulary:
    def test_the_storefront_lists_exactly_the_kernel_s_states(self):
        """Both directions at once, with the offenders named on each side."""
        declared = set(_declared_states())
        kernel = {state.value for state in CheckoutState}

        missing = kernel - declared
        invented = declared - kernel
        assert declared == kernel, (
            "CHECKOUT_STATES in apps/buyer-web/src/lib/api/types.ts has drifted from "
            "CheckoutState in packages/transaction-kernel/src/transaction_kernel/states.py.\n"
            f"  states the kernel declares and the storefront does not list: {_named(missing)}\n"
            f"    -- the API returns these in checkout.state and the buyer sees a raw enum "
            "name where a sentence should be.\n"
            f"  states the storefront lists and the kernel cannot produce: {_named(invented)}\n"
            "    -- these carry banner copy no buyer can ever be shown."
        )

    def test_no_phantom_state_returns(self):
        """The specific six, reported with why each one is not a checkout state.

        Kept separate from the equality test so that a regression reintroducing one of the
        original phantoms says which vocabulary it actually belongs to, instead of leaving
        the next reader to work out why ``STALE_CAPTURE`` looked plausible.
        """
        declared = set(_declared_states())
        returned = sorted(declared & set(PHANTOMS))
        assert not returned, "not checkout states: " + "; ".join(
            f"{name} ({PHANTOMS[name]})" for name in returned
        )

    def test_the_storefront_lists_them_in_the_kernel_s_declaration_order(self):
        """The file says it is in declaration order, so it has to be.

        Order carries no runtime meaning here, but the claim is written in the docstring
        above the list, and a docstring nothing checks is how this list went wrong the
        first time.
        """
        assert _declared_states() == [state.value for state in CheckoutState]

    def test_every_state_has_banner_copy(self):
        """A state in the vocabulary with no sentence is a blank banner in front of a buyer.

        TypeScript already makes this a compile error -- ``MEANINGS`` is a total
        ``Record`` over ``CHECKOUT_STATES`` -- but the compile error only says a key is
        missing. This says which state, in the same failure the drift itself reports, so a
        kernel change that adds a state tells its author both things they have to do.
        """
        banner = STATE_BANNER.read_text()
        without_copy = [
            state.value
            for state in CheckoutState
            if not re.search(rf"^  {state.value}: {{$", banner, re.M)
        ]
        assert not without_copy, (
            "no banner copy in apps/buyer-web/src/features/checkout/state-banner.tsx for: "
            + ", ".join(without_copy)
        )
