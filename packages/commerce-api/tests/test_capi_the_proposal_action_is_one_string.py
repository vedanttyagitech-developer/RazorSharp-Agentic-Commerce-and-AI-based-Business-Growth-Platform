"""Three packages spell one string, and a rename in any of them breaks voice silently.

WHAT THE COUPLING IS
--------------------
When the copilot offers to put something in the cart it emits a proposal whose ``action``
is ``basket.update``. Three places carry that string:

* ``commerce_protocols.mcp.tools.ToolName.BASKET_UPDATE`` -- the protocol's own definition,
  what an outside agent reads.
* ``commerce_api.services.agent_service.line_proposal_record`` -- the producer.
* ``voice_runtime.gateway.agent_client._line_proposal`` -- the consumer, which matches the
  literal to decide whether a spoken offer is "add this" or merely "here it is".

``agent_service`` already warns about it in a comment: *"the voice gateway matches the
literal string, so a rename here stops that branch firing and says nothing."* It said that
and nothing checked it. A rename would leave every suite green, every type check clean, and
a buyer saying "add two milk" to a shelf that never fills -- because the failure is a
branch not taken, which produces no error anywhere.

WHY A TEST AND NOT A SHARED CONSTANT
------------------------------------
Because voice-runtime must not import commerce-protocols. Its dependencies are
``commerce-domain`` and four libraries, and the narrowness is deliberate -- there is an
import-boundary test in that package keeping the model-driven runtime away from things it
has no business reaching. A shared constant would buy this one guarantee by widening that
boundary permanently, which is a worse trade than a test that reads both sides.

That makes this a *reading* test, the same shape as ``test_capi_foundation``'s check that
every recovery code has a spoken sentence: it imports both halves and asserts they agree,
rather than making one depend on the other.

WHAT IS **NOT** THE FIX
-----------------------
Accepting both ``basket.update`` and ``cart.update`` on the consumer side. That is a
compatibility alias, and this repository does not add them: code can call an alias, so the
second name outlives every intention to remove it. If the string is ever renamed, both
sides move in the same change -- which is precisely what this test forces.
"""

from __future__ import annotations

from typing import Any

import pytest
from commerce_api.services.agent_service import line_proposal_record
from commerce_protocols.mcp.tools import ToolName
from voice_runtime.gateway.agent_client import _line_proposal

#: A product shaped as the executor's catalogue payload, with only the fields the record
#: reads. Built here rather than fetched: this test is about a string, not about a database.
_PRODUCT: dict[str, Any] = {
    "sku": "AMUL-DAIRY-001",
    "display_name": "Amul Taaza Toned Milk 500 ml",
    "unit_price_minor": 2800,
    "unit_price": {"minor": 2800, "currency": "INR", "display": "28.00"},
    "currency": "INR",
    "unit_label": "500 ml",
    "is_available": True,
    "stock_units": 40,
}


def _record() -> dict[str, Any]:
    """The proposal, built with no cart.

    ``cart_id=None`` is the branch that returns before touching the executor, so this test
    needs no database and no session -- and the ``action`` field, which is the whole
    subject, is set on every branch. A record with a cart would additionally carry the
    re-quoted total, which is not what is under test here.
    """
    return line_proposal_record(_PRODUCT, 2, None, tools=None)  # type: ignore[arg-type]


def test_the_producer_emits_the_action_the_protocol_defines() -> None:
    assert _record()["action"] == ToolName.BASKET_UPDATE.value


def test_the_voice_gateway_recognises_what_the_producer_emits() -> None:
    """The whole coupling, end to end, with nothing in between invented by the test.

    The record the API builds is handed to the gateway's own reader. If either side is
    renamed alone, this is the assertion that goes red instead of a spoken "add two milk"
    quietly doing nothing.
    """
    record = _record()
    assert _line_proposal({"proposal": record}) == record


@pytest.mark.parametrize("renamed", ["cart.update", "basket_update", "BASKET.UPDATE", ""])
def test_a_renamed_action_is_not_recognised(renamed: str) -> None:
    """The RED direction, kept.

    Proof that the test above is measuring the string and not merely the shape: change the
    action and the gateway stops seeing a proposal. This is exactly what a one-sided rename
    would do in production, and it is silent there because nothing raises -- the branch is
    simply not taken.
    """
    assert _line_proposal({"proposal": {**_record(), "action": renamed}}) is None
