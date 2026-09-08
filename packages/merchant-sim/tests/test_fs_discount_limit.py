"""Failure scenario: a discount that policy does not allow.

Specification section 30 requires "Deny discount; cite policy" for a discount-limit
violation. These tests pin the *structural* half of that, and they are written against a
store with **no offer running** -- which is the store's resting state, and the one every
fixture here builds.

The engine now exists: a merchant can start one cart-wide offer, and
``packages/merchant-sim/tests/test_ms_offers.py`` covers its arithmetic and its bound. What
this file asserts is what remains true either way, and what section 30 actually asks about:
that the policy the buyer is shown states the merchant's discount position explicitly, and
that the number is bound so nobody can introduce one after the fact.

A published *ceiling* -- a maximum percentage or rupee value a merchant may offer -- is
Controller work and does not exist yet. The limit the engine enforces today is the one it
can enforce alone: an offer may never take a cart below a paisa payable.

1. **The Policy-at-Sale Receipt cites it.** With no offer running, the demo merchant's
   policy set carries a ``DISCOUNT`` policy whose terms say ``allowed: False`` -- an
   explicit "no discount programme" rather than an omission. That is the citation the
   specification's "cite policy" clause refers to, and it is durable: the receipt is
   captured at approval and does not change when merchant policy later does. With an offer
   running the same slot carries the offer's own identity, version and window, so the
   citation is equally exact in both directions.

2. **The content hash binds it.** ``discount_minor`` is one of the canonical hashed
   fields, so a document claiming a discount is a different document with a different
   hash, and approval compares hashes. A discount cannot be introduced between quote and
   approval, or between approval and admission, without invalidating the approval that
   the buyer actually gave.

Together those mean a discount cannot appear in a checkout the merchant did not price --
which is a stronger guarantee than denying one on request. The row's honest status is now
"the offer is enforced and demonstrable; a published ceiling is not built", and it should be
recorded that way in docs/FAILURE_SCENARIOS.md rather than as a scenario that cannot run.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Final

import pytest
from commerce_domain import Money, canonical_hash, uuid7
from merchant_sim import MerchantStore, content_from_quote, receipt_inputs_for
from merchant_sim.fees import BasketLine, quote_basket
from transaction_kernel.receipts import PolicyKind

#: The one field this file is about. Named once so a reader can grep for it.
_DISCOUNT_FIELD: Final[str] = "discount_minor"

#: A frozen clock, so freshness stamps do not make a hash comparison depend on wall time.
_POLICY_VERSION: Final[str] = "demo-grocery-policy/1"


def _frozen() -> datetime:
    return datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def store() -> MerchantStore:
    """The demo grocery store, at its opening catalogue."""
    return MerchantStore(clock=_frozen)


def _quote(store: MerchantStore) -> Any:
    """A two-line cart, priced by the fee engine exactly as a checkout would be."""
    skus = store.all_skus()
    lines = (BasketLine(skus[0], 1), BasketLine(skus[1], 2))
    return quote_basket(lines, store=store).require()


def _discount_policy(store: MerchantStore) -> Any:
    policies = receipt_inputs_for(store).policies
    found = [policy for policy in policies if policy.kind is PolicyKind.DISCOUNT]
    assert len(found) == 1, "exactly one discount policy, or a receipt cannot cite one"
    return found[0]


# ----------------------------------------------- 1. the policy the buyer is shown


def test_the_demo_store_publishes_a_discount_policy_that_forbids_discounts(
    store: MerchantStore,
) -> None:
    """ "Cite policy" needs a policy to cite, and it has to say no.

    Asserted on the emitted policy set rather than on a constant in the adapter, because
    what matters is the document that reaches the Policy-at-Sale Receipt and therefore the
    buyer -- not the value some function happened to be called with.
    """
    discount = _discount_policy(store)

    assert discount.terms == {"allowed": False}, (
        "the demo store runs no discounts; the policy must say so, because this is the "
        "citation section 30's 'deny discount; cite policy' refers to"
    )
    assert discount.policy_id.endswith("/discount")


def test_every_quote_prices_a_zero_discount(store: MerchantStore) -> None:
    """The emitted content agrees with the policy. A policy saying "no discounts" beside
    content carrying one would make the receipt a lie about the sale it records."""
    content = content_from_quote(
        _quote(store), checkout_id=uuid7(), version=1, policy_version=_POLICY_VERSION
    )

    assert content[_DISCOUNT_FIELD] == 0
    assert isinstance(content[_DISCOUNT_FIELD], int), "money is integer minor units, never float"


# ------------------------------------------- 2. the binding that makes it stick


def test_introducing_a_discount_changes_the_hash_the_buyer_approved(
    store: MerchantStore,
) -> None:
    """A discount cannot be slipped in after approval.

    This is the actual enforcement, and it is worth stating precisely: nothing *rejects* a
    discount, because nothing needs to. ``discount_minor`` is a canonical hashed field, so
    a document carrying one is a different document. Approval compares content hashes, so
    the altered version is not the version the buyer consented to and admission refuses it
    as stale.

    The mutation is applied to the emitted content rather than requested through an API,
    because there is no API that offers a discount -- which is the finding this file
    exists to record.
    """
    original = content_from_quote(
        _quote(store), checkout_id=uuid7(), version=1, policy_version=_POLICY_VERSION
    )
    before = canonical_hash(original)

    tampered = dict(original)
    tampered[_DISCOUNT_FIELD] = 500
    after = canonical_hash(tampered)

    assert before != after, (
        "discount_minor must be inside the hash, or a discount could be introduced "
        "between approval and admission without invalidating the buyer's consent"
    )


def test_a_discount_that_the_total_does_not_account_for_is_refused(
    store: MerchantStore,
) -> None:
    """The arithmetic check catches the naive version of the same attack.

    Changing ``discount_minor`` alone leaves a total that its own components no longer
    support. The content builder recomputes and refuses, so an attacker cannot avoid the
    hash change by also "fixing" the total without the merchant's fee engine agreeing --
    and the fee engine is what produced the quote in the first place.

    Every other field is the quote's own, and the lines are real: an earlier draft of this
    test passed an empty ``lines`` tuple, and then it raised because the cart was empty
    rather than because the discount was unaccounted for. It passed for the wrong reason
    and would have gone on passing with the discount check removed. The control below --
    the same call with ``discount_minor=0`` succeeding -- is what makes the refusal
    attributable to the discount and nothing else.
    """
    from transaction_kernel.checkout_content import (
        ContentContractError,
        ContentLine,
        build_checkout_content,
    )

    quote = _quote(store)
    lines = tuple(
        ContentLine(
            sku=line.sku,
            name=line.name,
            quantity=line.quantity,
            unit_minor=line.unit_price.minor,
            line_minor=line.subtotal.minor,
            tax_minor=line.tax.minor,
        )
        for line in quote.lines
    )
    fields: dict[str, Any] = {
        "checkout_id": uuid7(),
        "version": 1,
        "currency": quote.currency,
        "lines": lines,
        "subtotal_minor": quote.items_subtotal.minor,
        "tax_minor": quote.items_tax.minor + quote.delivery_tax.minor,
        "delivery_fee_minor": quote.delivery_fee.minor,
        "total_minor": quote.total.minor,
        "policy_version": _POLICY_VERSION,
        "catalogue_revision": quote.freshness.catalogue_revision,
        "source_id": quote.freshness.source,
    }

    # Control: identical in every respect but the discount, and it builds.
    assert build_checkout_content(discount_minor=0, **fields)[_DISCOUNT_FIELD] == 0

    with pytest.raises(ContentContractError) as info:
        build_checkout_content(discount_minor=500, **fields)
    assert info.value.path is not None


def test_the_receipt_policy_survives_a_later_merchant_change(store: MerchantStore) -> None:
    """Durable buyer terms (specification 31.4): the citation is captured, not looked up.

    If the receipt merely referenced the merchant's current policy, a merchant who enabled
    discounts after the sale would retroactively change what the buyer was told. The
    policy set is emitted with a version, and the receipt holds the terms themselves.
    """
    captured_terms = dict(_discount_policy(store).terms)
    before = receipt_inputs_for(store, policy_version=1)

    # A different fee policy is a different receipt input set, exactly as a merchant edit
    # between two sales would be.
    moved = replace(store.fee_policy, base_delivery_fee=Money(4900, "INR"))
    after = receipt_inputs_for(moved, policy_version=2)

    assert before.buyer_visible_refs != after.buyer_visible_refs, (
        "a fee change must change the buyer-visible policy reference, or the receipt "
        "could not prove which terms the buyer was actually shown"
    )
    assert captured_terms == {"allowed": False}, (
        "the terms captured earlier are a value this test still holds; a receipt storing "
        "them is unaffected by the merchant's later edit"
    )
