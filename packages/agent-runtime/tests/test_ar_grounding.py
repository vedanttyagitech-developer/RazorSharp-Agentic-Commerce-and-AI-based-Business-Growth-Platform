"""The turn ledger and the reply post-check (ADR 0004 section 4, rows 15 and 16).

The ledger is filled the way the tools fill it -- through the payload builders over the
in-memory backend -- so a test proves the same path the model's result travels. The
replies are hand-written prose of the kind a model produces, with one thing wrong each.
"""

from __future__ import annotations

import uuid

import pytest
from agent_runtime.backends import InMemoryBackend, InMemoryTrustedSurface
from agent_runtime.grounding import (
    DATA_BEGIN,
    DATA_END,
    GroundingLedger,
    basket_payload,
    checkout_payload,
    order_payload,
    product_payload,
    search_payload,
    verify_reply,
)
from agent_runtime.language import Language
from agent_runtime.rendering import display_amount
from agent_runtime.turn import TurnContext
from merchant_sim import Locale
from transaction_kernel import (
    ActorType,
    AgentPrincipal,
    CheckoutRef,
    Delta,
    KernelDecision,
    RecoveryCode,
)

from .conftest import MILK_SKU

FAKE_SKU = "FAKE-PROD-999"


def _turn() -> TurnContext:
    principal = AgentPrincipal(
        principal_id="agent:test/shopping",
        tenant_id=uuid.UUID("00000000-0000-4000-8000-000000000001"),
        actor_type=ActorType.AGENT,
        agent_role="shopping",
        capabilities=frozenset({"catalog.search"}),
    )
    return TurnContext(language=Language.EN, principal=principal)


@pytest.mark.asyncio
async def test_ungrounded_sku_is_rewritten_with_a_notice_and_alternatives(
    backend: InMemoryBackend,
) -> None:
    """Spec 20.4 steps 3 and 4: the sentence goes, the buyer is told, real items offered."""
    turn = _turn()
    page = await backend.search("milk", Locale.EN, 3)
    search_payload(page, turn, tool="search")
    milk_price = display_amount(page.hits[0].unit_price)
    reply = (
        f"I found {MILK_SKU} at {milk_price}. "
        f"You could also try {FAKE_SKU} which is very popular. "
        "Shall I add either?"
    )
    check = verify_reply(reply, turn.ledger)
    assert check.rewritten
    assert check.ungrounded_skus == (FAKE_SKU,)
    assert FAKE_SKU not in check.reply.split("could not verify")[0]
    assert MILK_SKU in check.reply
    assert "could not verify" in check.reply
    # Alternatives come from the ledger, by SKU, and every one of them is grounded.
    for sku in page.skus():
        assert sku in check.reply or sku == MILK_SKU
    assert len(check.dropped_sentences) == 1


@pytest.mark.asyncio
async def test_ungrounded_amount_drops_only_its_sentence(backend: InMemoryBackend) -> None:
    turn = _turn()
    card = await backend.product(MILK_SKU)
    product_payload(card, turn, tool="product")
    real = display_amount(card.unit_price)
    reply = f"Milk is {real} per litre. Two would be ₹99,999.00 in total. Want two?"
    check = verify_reply(reply, turn.ledger)
    assert check.rewritten
    assert check.ungrounded_amounts_minor == (9999900,)
    assert real in check.reply
    assert "99,999" not in check.reply
    assert "Want two?" in check.reply


@pytest.mark.asyncio
async def test_stale_turn_amount_is_ungrounded(backend: InMemoryBackend) -> None:
    """Row 16: a total the previous turn quoted is not evidence for this turn's sentence."""
    first = _turn()
    view = await backend.basket_create()
    view = await backend.basket_set_line(view.basket_id, MILK_SKU, 2)
    basket_payload(view, first, tool="basket_set_line")
    assert view.quote is not None
    total = display_amount(view.quote.total)
    assert not verify_reply(f"Your total is {total}.", first.ledger).rewritten

    second = _turn()
    check = verify_reply(f"Your total is {total}.", second.ledger)
    assert check.rewritten
    assert total not in check.reply
    assert check.reply == ""  # the harness renders the fallback and records why


def test_success_claim_without_capture_is_removed() -> None:
    ledger = GroundingLedger()
    check = verify_reply("Payment successful! Your order has been placed.", ledger)
    assert check.success_claim_removed
    assert "successful" not in check.reply
    assert check.reply == ""


@pytest.mark.asyncio
async def test_success_claim_allowed_only_after_a_captured_read(
    backend: InMemoryBackend, surface: InMemoryTrustedSurface
) -> None:
    basket = await backend.basket_create()
    await backend.basket_set_line(basket.basket_id, MILK_SKU, 1)
    card = await backend.checkout_create(basket.basket_id)
    surface.approve(
        card.checkout_id, 1, content_hash=card.content_hash, total_minor=card.total.minor
    )
    decision = await backend.checkout_submit_approved(card.checkout_id, 1, card.content_hash)
    assert decision.allowed

    admitted = _turn()
    checkout_payload(await backend.checkout_get(card.checkout_id), admitted, tool="checkout_get")
    assert verify_reply("Payment successful.", admitted.ledger).success_claim_removed

    surface.record_provider_capture(card.checkout_id)
    captured = _turn()
    checkout_payload(await backend.checkout_get(card.checkout_id), captured, tool="checkout_get")
    assert not verify_reply("Payment successful.", captured.ledger).rewritten


@pytest.mark.asyncio
async def test_order_payload_records_amounts_and_payment_state(
    backend: InMemoryBackend, surface: InMemoryTrustedSurface
) -> None:
    basket = await backend.basket_create()
    await backend.basket_set_line(basket.basket_id, MILK_SKU, 1)
    card = await backend.checkout_create(basket.basket_id)
    surface.approve(
        card.checkout_id, 1, content_hash=card.content_hash, total_minor=card.total.minor
    )
    await backend.checkout_submit_approved(card.checkout_id, 1, card.content_hash)
    surface.record_provider_capture(card.checkout_id)
    view = await backend.checkout_get(card.checkout_id)
    assert view.payment is not None and view.payment.is_captured

    order_id = next(iter(backend._orders))  # noqa: SLF001 - reading the demo's order map
    turn = _turn()
    payload = order_payload(await backend.order_track(order_id), turn, tool="order_track")
    assert payload["payment"]["is_captured"] is True
    assert payload["payment"]["capture_evidence"] == "PROVIDER_FETCH"
    assert payload["amount_minor"] == card.total.minor
    assert turn.ledger.knows_amount(card.total.minor)
    assert turn.ledger.knows_sku(MILK_SKU)
    assert turn.ledger.payment_captured()


@pytest.mark.asyncio
async def test_unverified_notice_follows_the_buyer_language(backend: InMemoryBackend) -> None:
    turn = _turn()
    product_payload(await backend.product(MILK_SKU), turn, tool="product")
    check = verify_reply(f"{FAKE_SKU} ले लीजिए।", turn.ledger, language=Language.HI)
    assert check.rewritten
    assert "सत्यापित" in check.reply
    assert FAKE_SKU in check.reply  # named once, in the notice, so the buyer knows which


@pytest.mark.asyncio
async def test_payloads_fence_merchant_text_and_carry_the_notice(backend: InMemoryBackend) -> None:
    turn = _turn()
    payload = product_payload(await backend.product(MILK_SKU), turn, tool="product")
    assert payload["merchant_text"].startswith(DATA_BEGIN)
    assert payload["merchant_text"].endswith(DATA_END)
    assert payload["quarantined"] is False
    assert payload["unit_price_display"] == display_amount(
        turn.ledger.products[MILK_SKU].unit_price
    )


def test_sku_named_by_a_kernel_delta_is_grounded() -> None:
    """A refusal's delta path names the line that moved; the reply may name it too."""
    ledger = GroundingLedger()
    decision = KernelDecision(
        decision_id=uuid.uuid4(),
        allowed=False,
        code=RecoveryCode.REAPPROVAL_REQUIRED,
        explanation="material_change",
        checkout=CheckoutRef(checkout_id=uuid.uuid4(), version=1, content_hash="h1"),
        deltas=(Delta(f"lines[{MILK_SKU}].unit_price_minor", 5000, 6200, "PRICE_CHANGED"),),
        next_version=2,
    )
    ledger.record_decision(decision, "INR")
    assert ledger.knows_sku(MILK_SKU)
    assert ledger.knows_amount(5000) and ledger.knows_amount(6200)
    assert ledger.alternatives() == ()  # a delta line has no merchant text to offer
    check = verify_reply(f"{MILK_SKU} moved from ₹50.00 to ₹62.00.", ledger)
    assert not check.rewritten


# --------------------------------------------------------- scarcity and pressure
#
# The storefront's own rule, applied to the model's prose: this shop does not tell a buyer
# that other people bought something, or that it is about to run out, in order to make
# them likelier to buy it. A unit count carries no currency mark, so the amount check reads
# straight past it; a popularity claim carries no number at all. Both need their own rules.


@pytest.mark.asyncio
async def test_fabricated_unit_count_is_dropped(backend: InMemoryBackend) -> None:
    """A count of what is left stands only if a merchant read returned that count."""
    turn = _turn()
    card = await backend.product(MILK_SKU)
    product_payload(card, turn, tool="product")
    invented = card.stock_units + 7
    assert invented not in turn.ledger.stock_counts

    reply = f"{MILK_SKU} is on the shelf. Only {invented} left, so I would add it now."
    check = verify_reply(reply, turn.ledger)

    assert check.rewritten
    assert check.ungrounded_stock_counts == (invented,)
    assert str(invented) not in check.reply
    assert "is on the shelf" in check.reply  # the grounded sentence beside it survives


@pytest.mark.asyncio
async def test_the_merchants_own_count_survives(backend: InMemoryBackend) -> None:
    """The honest form of the same sentence. Scarcity is not banned; inventing it is."""
    turn = _turn()
    card = await backend.product(MILK_SKU)
    product_payload(card, turn, tool="product")

    check = verify_reply(f"There are {card.stock_units} units left at the merchant.", turn.ledger)

    assert not check.rewritten
    assert check.ungrounded_stock_counts == ()


@pytest.mark.asyncio
async def test_a_count_from_a_previous_turn_is_not_evidence(backend: InMemoryBackend) -> None:
    """Stock moves under a basket exactly as price moves under a checkout (row 16)."""
    first = _turn()
    card = await backend.product(MILK_SKU)
    product_payload(card, first, tool="product")
    sentence = f"Only {card.stock_units} left."
    assert not verify_reply(sentence, first.ledger).rewritten

    second = _turn()
    check = verify_reply(sentence, second.ledger)
    assert check.rewritten
    assert check.ungrounded_stock_counts == (card.stock_units,)


@pytest.mark.asyncio
async def test_a_spelled_out_count_is_read_as_a_count(backend: InMemoryBackend) -> None:
    """A check that reads digits alone is one rewording away from being bypassed."""
    turn = _turn()
    product_payload(await backend.product(MILK_SKU), turn, tool="product")
    ledger = turn.ledger
    # Pick a word-number the merchant did not report, so the claim is genuinely invented.
    spelled, value = next(
        (word, number)
        for word, number in (("two", 2), ("three", 3), ("nine", 9))
        if number not in ledger.stock_counts
    )

    # No urgency word in the sentence, so only the count rule can be what removes it.
    check = verify_reply(f"We have only {spelled} left in stock.", ledger)

    assert check.rewritten
    assert not check.pressure_removed
    assert check.ungrounded_stock_counts == (value,)
    assert check.reply == ""


def test_popularity_is_removed_even_when_nothing_else_is_wrong() -> None:
    """No tool returns a demand signal, so no ledger entry could ever make this true."""
    ledger = GroundingLedger()
    check = verify_reply(
        "Milk goes with your order. This one is our best seller. Shall I add it?", ledger
    )

    assert check.rewritten
    assert check.pressure_removed
    assert "best seller" not in check.reply
    assert "Milk goes with your order." in check.reply
    assert "Shall I add it?" in check.reply


def test_pressure_in_hinglish_and_hindi_is_removed() -> None:
    ledger = GroundingLedger()
    for reply, gone in (
        ("Yeh item bahut popular hai, jaldi kijiye.", "popular"),
        ("यह सामान बहुत तेज़ी से बिक रहा है।", "बिक"),
    ):
        check = verify_reply(reply, ledger)
        assert check.pressure_removed, reply
        assert gone not in check.reply, reply


def test_a_plain_answer_carries_no_pressure_flag() -> None:
    """The check has to leave an ordinary reply alone, or it is not a check but a filter."""
    ledger = GroundingLedger()
    check = verify_reply(
        "That item is out of stock at the moment. I can look for something similar.", ledger
    )
    assert not check.rewritten
    assert not check.pressure_removed
    assert check.ungrounded_stock_counts == ()


def test_headcount_social_proof_is_removed() -> None:
    """The two sentences `docs/DEMO_READINESS.md` recorded passing verbatim, each alone.

    No read on this platform returns a buyer count, a view count or a sales total, so a
    ledger entry that could ground one of these cannot exist. They are dropped outright.
    """
    ledger = GroundingLedger()
    for reply in ("14 people bought it in the last hour.", "500 people bought it today."):
        check = verify_reply(reply, ledger)
        assert check.pressure_removed, reply
        assert check.reply == "", reply


def test_scarcity_without_a_number_needs_a_low_read_behind_it() -> None:
    """A shelf really does run out. Asserting that uninvited is what makes it a pattern."""
    unread = GroundingLedger()
    assert verify_reply("It is running out.", unread).pressure_removed

    # A merchant read came back genuinely low, so the same sentence is now reporting.
    observed = GroundingLedger()
    observed.record_stock(2)
    check = verify_reply("It is running out.", observed)
    assert not check.rewritten
    assert not check.pressure_removed


def test_a_full_shelf_does_not_license_vague_scarcity() -> None:
    """A read that came back plentiful is not evidence that the shelf is nearly empty."""
    plentiful = GroundingLedger()
    plentiful.record_stock(48)
    assert verify_reply("Almost gone, I would add it now.", plentiful).pressure_removed


def test_a_merchant_product_is_never_grounded_under_its_own_sku_as_its_name() -> None:
    """``name == sku`` is a signal, not a spelling, so the ledger refuses to record one.

    ``enforce_conversational_rules`` reads that equality as "a line the merchant reported it
    could not fulfil" and appends a sentence written for a shopper. A merchant read has a
    real product name behind it, so a caller with nothing usable to pass -- an empty name, a
    name the fence quarantined, or the SKU itself -- gets the safe label instead of putting
    a buyer's correction on a merchant's turn.
    """
    ledger = GroundingLedger()
    ledger.record_merchant_product(MILK_SKU, MILK_SKU, stock_units=0, is_available=False)
    ledger.record_merchant_product("BRIT-BAKE-001", "", stock_units=2, is_available=True)
    assert ledger.products[MILK_SKU].name == f"catalogue item {MILK_SKU}"
    assert ledger.products["BRIT-BAKE-001"].name == "catalogue item BRIT-BAKE-001"
    assert all(product.name != product.sku for product in ledger.products.values())


def test_a_merchant_product_grounds_its_sku_and_shelf_but_no_amount() -> None:
    """No price was read, so none is grounded: a zero would license "₹0.00" in prose."""
    ledger = GroundingLedger()
    ledger.record_merchant_product(MILK_SKU, "Amul Taaza Milk", stock_units=0, is_available=False)
    assert ledger.knows_sku(MILK_SKU)
    assert ledger.knows_stock_count(0)
    assert ledger.amounts_minor == set()
    assert ledger.currencies == set()

    # A read that reported no count at all grounds the SKU and nothing else.
    ledger.record_merchant_product("BRIT-BAKE-001", "Bread", stock_units=None, is_available=True)
    assert ledger.knows_sku("BRIT-BAKE-001")
    assert ledger.stock_counts == {0}
