"""Two timeouts, in two packages, that must never expire at the same instant.

The API gives one agent turn a budget (``Harness.turn_timeout_s``, which ``RazorAI``
inherits) and catches its
own ``TimeoutError`` when the model overruns it, returning ``reason="timeout"``. That is
the platform's designed degradation: a buyer who asked for something slow gets a sentence
saying so, not a failure.

This gateway waits on the same turn over HTTP. Both numbers were ``30.0``. So the gateway
expired at the same instant the harness did, gave up first every time, and raised
``AgentUnavailableError`` -- the buyer heard "the agent is unavailable" and the prepared
sentence was unreachable by construction. Two layers, each correct alone, that did not
compose.

**It was invisible to every unit test.** One turn on an idle machine finishes far inside
thirty seconds, so nothing failed until eight live-audio tests ran together and the model
slowed under its own load. Then it failed on every run, in the same place. A defect that
needs load to appear needs a test that does not, which is this one: it holds the
*relationship* rather than waiting for the race.

The numbers are held apart at both ends. A change to either package fails here and the
message says which side moved -- because raising the harness's budget without raising the
gateway's silently restores exactly the bug this file exists to close.
"""

from __future__ import annotations

import inspect

import pytest
from voice_runtime.gateway.agent_client import (
    AGENT_TURN_BUDGET_S,
    AGENT_TURN_TIMEOUT_S,
)

#: The least headroom worth calling headroom.
#:
#: Everything the API does around the model call has to fit here: routing, capability
#: binding, the tool executor's own reads, the session write, the transcript, and two
#: network hops. Five seconds would technically separate the two numbers while leaving no
#: room for any of that, and a separation that only exists on paper is how this comes back.
MIN_HEADROOM_S: float = 10.0


def _harness_turn_budget() -> float:
    """The API's real per-turn budget, read from the class rather than from a constant.

    Read reflectively on purpose. A copy of this number in the test would be a third place
    for it to drift, and the whole subject of this file is a number that was copied and
    then diverged.
    """
    base = pytest.importorskip(
        "agent_runtime.harness.base",
        reason="agent-runtime is not installed in this checkout",
    )
    # Declared on `Harness`; `RazorAI` inherits it unchanged, and reading the base is what
    # keeps this correct if another harness subclass appears beside it.
    signature = inspect.signature(base.Harness.__init__)
    default = signature.parameters["turn_timeout_s"].default
    assert isinstance(default, int | float), (
        "Harness.turn_timeout_s no longer has a numeric default; this test can no longer "
        "read the API's budget and must be rewritten rather than deleted"
    )
    return float(default)


def test_the_gateway_copy_of_the_budget_still_matches_the_harness() -> None:
    """``AGENT_TURN_BUDGET_S`` is a copy, and this is what stops it going stale.

    voice-runtime does not depend on agent-runtime and should not start: this package
    reaches the agent over HTTP precisely so it need not hold the model runtime. The cost
    of that boundary is a copied number, and the price of a copied number is a test that
    reads the original.
    """
    actual = _harness_turn_budget()
    assert actual == AGENT_TURN_BUDGET_S, (
        f"the API now budgets {actual}s for an agent turn but this gateway still believes "
        f"{AGENT_TURN_BUDGET_S}s. Update AGENT_TURN_BUDGET_S in "
        f"voice_runtime/gateway/agent_client.py -- and check that AGENT_TURN_TIMEOUT_S is "
        f"still comfortably above it, which is the thing that actually matters."
    )


def test_the_gateway_waits_longer_for_a_turn_than_the_harness_takes_to_give_up() -> None:
    """The invariant. If this fails, the designed degradation is unreachable again.

    Strictly greater is not enough and equal is the original bug, so this asserts real
    headroom: the gateway's wait covers the harness's whole budget *and* everything the
    API does around it.
    """
    headroom = AGENT_TURN_TIMEOUT_S - AGENT_TURN_BUDGET_S
    assert headroom >= MIN_HEADROOM_S, (
        f"the gateway waits {AGENT_TURN_TIMEOUT_S}s for a turn the API is allowed to spend "
        f"{AGENT_TURN_BUDGET_S}s on, leaving {headroom}s for routing, binding, tool reads, "
        f"the session write and two network hops. At {headroom}s the gateway gives up "
        f"before the harness can answer, and the buyer hears 'the agent is unavailable' "
        f"instead of the platform's own timeout sentence. Raise AGENT_TURN_TIMEOUT_S."
    )


def test_the_long_wait_is_spent_only_on_the_call_that_has_a_model_behind_it() -> None:
    """The gateway's client stays short for everything else, and that is deliberate.

    One ``httpx.AsyncClient`` serves three calls: resolving identity, reading an approval
    card, and the agent turn. Only the last has a model in it. Raising the client's own
    timeout would have been the smaller edit and the worse one -- the spoken-consent window
    depends on a card arriving promptly, and a card read that hung for fifty seconds is
    worse than one that gives up in thirty.

    Asserted by reading the source, because the per-request override is a keyword argument
    at one call site and there is no object to interrogate for it afterwards.
    """
    from pathlib import Path

    import voice_runtime.gateway.agent_client as module
    import voice_runtime.gateway.app as app_module

    source = Path(module.__file__).read_text(encoding="utf-8")
    turn_post = source.split("AGENT_TURN_PATH,\n", 1)
    assert len(turn_post) == 2, "the agent turn is no longer posted to AGENT_TURN_PATH"
    # Up to the call's closing parenthesis, so a comment between the path and the keyword
    # cannot push it out of range -- which is what a fixed character window did.
    call, _, _ = turn_post[1].partition("\n            )")
    assert "timeout=AGENT_TURN_TIMEOUT_S" in call, (
        "the agent turn no longer passes its own timeout, so it has fallen back to the "
        "shared client's -- which is the arrangement that made the harness's graceful "
        "timeout unreachable"
    )

    app_source = Path(app_module.__file__).read_text(encoding="utf-8")
    assert "_HTTP_TIMEOUT_S: Final[float] = 30.0" in app_source, (
        "the shared client's timeout moved. That is allowed, but this test names the value "
        "so the move is deliberate: it exists to keep identity and card reads failing fast, "
        "and it must not quietly become the agent turn's timeout again"
    )
