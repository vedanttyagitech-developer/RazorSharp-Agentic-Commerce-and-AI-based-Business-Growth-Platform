"""The reads-only capability translation, on its own: no database, no model, no backend.

This file exists because the translation is a *decision*, and a decision needs a test that
fails when someone changes it by accident. The interesting assertions are not that the
mapping returns what it contains -- that is a tautology -- but that the shape of the result
is still usable and still safe: a shopping principal that can search and re-quote, and that
cannot write a cart, no matter which side of the vocabulary is edited.
"""

from __future__ import annotations

from agent_runtime.capabilities.registry import (
    AGENT_ALLOWLIST,
    REGISTRY_A,
    SPECIALIST_TOOLS,
    WRITE_TOOLS,
    AgentRole,
    Capability,
)
from commerce_api.deps import AGENT_CAPABILITIES, BUYER_CAPABILITIES
from commerce_api.services.agent_bridge import (
    READS_ONLY_CAPABILITIES,
    registry_a_capabilities,
)


def _tools_a_principal_would_get(role: AgentRole, held: frozenset[Capability]) -> set[str]:
    """The roster tools ``build_toolset`` would actually construct for ``held``.

    Mirrors the factory's own rule -- roster order, intersected with the role allowlist,
    then a tool is built only when its required capability is held -- without needing a
    backend or a turn, so the assertion is about the capability arithmetic and nothing else.
    """
    allowed = held & AGENT_ALLOWLIST[role]
    return {name for name in SPECIALIST_TOOLS[role] if REGISTRY_A[name] in allowed}


class TestTheTableIsSafe:
    def test_no_row_grants_a_write_capability(self) -> None:
        """The property the import-time check enforces, asserted where a reader can see it.

        Derived from ``WRITE_TOOLS`` rather than a hand-written list, so a tool that becomes
        a write later is caught here too.
        """
        writes = {REGISTRY_A[name] for name in WRITE_TOOLS}
        granted = {cap for row in READS_ONLY_CAPABILITIES.values() for cap in row}
        assert not (granted & writes), "a reads-only row grants a write"

    def test_every_granted_capability_is_required_by_some_tool(self) -> None:
        """A capability no tool needs grants nothing while looking like it grants something."""
        granted = {cap for row in READS_ONLY_CAPABILITIES.values() for cap in row}
        assert granted <= set(REGISTRY_A.values())

    def test_basket_write_grants_the_re_quote_and_not_the_mutation(self) -> None:
        """The reviewed decision, pinned.

        ``basket.write`` is one string here and five capabilities in Registry A. A model gets
        the one that lets it see a cart; the two that change one stay with the buyer's
        press. If this ever flips, the line proposal card has been made redundant by a tool
        call that carries less evidence than the card does.
        """
        row = READS_ONLY_CAPABILITIES["basket.write"]
        assert Capability.QUOTE_REQUEST in row
        assert Capability.BASKET_CREATE not in row
        assert Capability.BASKET_UPDATE not in row

    def test_propose_line_travels_with_basket_write_because_it_writes_nothing(self) -> None:
        """A proposal is not a write, and the shop stops working without it.

        ``basket_propose_line`` stages the add the buyer just asked for and performs none of
        it: the record it returns is what the buyer's own surface acts on, and the server
        re-checks the price and the stock under the cart's lock when that surface writes. So
        it belongs on the reads-only side of this table beside ``quote.request``, while
        ``basket.create`` and ``basket.update`` stay off it.

        It was missing, and the symptom was the whole shop: the model was told to propose a
        line, had no tool of that name, and answered in words instead -- so the assistant
        said it was adding something and the cart stayed empty.
        """
        row = READS_ONLY_CAPABILITIES["basket.write"]
        assert Capability.BASKET_PROPOSE_LINE in row

    def test_the_write_shaped_service_strings_translate_to_nothing(self) -> None:
        """``checkout.create`` and ``checkout.submit_approved`` are writes on both sides."""
        assert registry_a_capabilities({"checkout.create"}) == frozenset()
        assert registry_a_capabilities({"checkout.submit_approved"}) == frozenset()


class TestTheTableIsUsable:
    def test_an_agent_session_can_search_and_re_quote(self) -> None:
        """The bug this table exists to fix.

        Before it, binding a shopping principal intersected this service's vocabulary with
        Registry A's and got the empty set: the toolset came back with no tools, and a model
        handed no tools answers from nothing, fluently. So the assertion that matters is not
        that the mapping is non-empty but that the *tools* are there.
        """
        held = registry_a_capabilities(AGENT_CAPABILITIES)
        tools = _tools_a_principal_would_get(AgentRole.SHOPPING, held)
        assert tools == {
            "search",
            "product",
            "basket_get",
            "basket_propose_line",
            "present_products",
            "present_basket",
        }

    def test_the_same_session_gets_no_basket_write_tool(self) -> None:
        """The other half: usable is not the same as unrestricted."""
        held = registry_a_capabilities(AGENT_CAPABILITIES)
        tools = _tools_a_principal_would_get(AgentRole.SHOPPING, held)
        assert "basket_create" not in tools
        assert "basket_set_line" not in tools

    def test_a_buyer_session_does_not_lend_its_consent_to_the_model(self) -> None:
        """A buyer session holds Registry B strings. None of them translates.

        ``registry_a_capabilities`` drops what it cannot translate instead of raising,
        because a buyer session legitimately holds the right to approve -- and the point is
        that the right does not survive the translation, not that it is an error to hold it.
        """
        held = registry_a_capabilities(BUYER_CAPABILITIES)
        assert Capability.CHECKOUT_SUBMIT_APPROVED not in held
        assert Capability.CHECKOUT_SUBMIT_FOR_APPROVAL not in held
        for consent in ("checkout.approve", "checkout.reject", "checkout.cancel", "refund.request"):
            assert registry_a_capabilities({consent}) == frozenset(), consent

    def test_an_unknown_string_is_dropped_rather_than_raising(self) -> None:
        assert registry_a_capabilities({"not.a.capability"}) == frozenset()
