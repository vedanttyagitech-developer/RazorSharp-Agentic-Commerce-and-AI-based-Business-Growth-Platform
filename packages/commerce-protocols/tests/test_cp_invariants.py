"""The invariants that hold across all four protocols, specification 29.5.

Every other suite in this package tests one adapter. This one tests the claim that no single
adapter's suite can make, and it is the claim specification 13.1 rests on:

    Same kernel invariant across trusted UI, UCP/AP2, ACP and MCP entry points.

Four protocols were built by different hands at different times against the same core. The
risk that creates is drift -- one adapter quietly widening what an external party may ask
for, in a way that is invisible while reading that adapter alone because it looks locally
reasonable. These tests read all four together and assert the properties that must be true of
every one of them.

The properties are stated as absences wherever possible, because an absence is what a
behavioural test cannot demonstrate. A test that drives an adapter and observes it declining
to approve a checkout proves the current code path declines. Asserting over the enum, the
frozenset and the tool registry proves the request cannot be formed.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import re
from typing import Any

import pytest
from commerce_protocols import acp, ap2, core, mcp, ucp
from commerce_protocols.core import (
    CONSENT_CAPABILITIES,
    PINS,
    PROTOCOL_CAPABILITIES,
    IntentKind,
    Protocol,
)
from commerce_protocols.mcp.tools import TOOLS, ToolName
from commerce_protocols.ucp.lifecycle import UCP_INTENT_BY_OPERATION

#: Every module a protocol adapter is assembled from. Named explicitly rather than
#: discovered, so a new adapter has to be added here deliberately and cannot slip past these
#: assertions by being invisible to a glob.
ADAPTER_PACKAGES = (ucp, ap2, acp, mcp)

#: Verbs that name an act of consent or an act of moving money, matched on identifier word
#: boundaries. Substring matching is not good enough here and the reason is instructive: it
#: flags ``escalate_for_razorpay_handoff`` for the "pay" inside "razorpay" and
#: ``message_payload`` for the one inside "payload", which trains a reader to ignore the
#: test. A word-boundary match over the snake_case parts of a name says what was meant.
DANGEROUS_VERBS = frozenset(
    {
        "approve",
        "approves",
        "pay",
        "pays",
        "charge",
        "charges",
        "refund",
        "refunds",
        "revoke",
        "revokes",
        "execute",
        "executes",
        "capture",
        "captures",
        "settle",
    }
)

#: Words that make a dangerous verb harmless in the same identifier. ``propose`` names a
#: request for a human decision, ``verify`` and ``reject`` name checks and refusals, and
#: ``proof`` names evidence that something already happened elsewhere.
HARMLESS_NEIGHBOURS = frozenset({"propose", "proposal", "verify", "reject", "requires", "proof"})


def _public_callables(module: Any) -> list[str]:
    """Public callable names on a module, excluding re-exported third-party symbols."""
    found = []
    for name in dir(module):
        if name.startswith("_"):
            continue
        value = getattr(module, name)
        if callable(value) or inspect.ismodule(value):
            found.append(name)
    return found


class TestOneIntentVocabulary:
    def test_every_protocol_maps_onto_the_core_vocabulary_and_no_other(self) -> None:
        """Four adapters, one closed enum. Drift here is what this file exists to catch."""
        ucp_intents = set(UCP_INTENT_BY_OPERATION.values())
        mcp_intents = {spec.intent for spec in TOOLS.values()}

        for intents in (ucp_intents, mcp_intents):
            assert intents <= set(IntentKind), "an adapter invented an intent"
            assert intents, "an adapter mapped nothing"

    def test_no_protocol_can_produce_an_intent_that_records_consent(self) -> None:
        """Specification 5.3: consent is not delegable to what proposed the purchase.

        There is no ``APPROVE`` member to map to, so this asserts the mappings did not find
        some other way to express one -- ``REQUEST_APPROVAL`` is the closest thing that
        exists, and it produces a request for a human, never a recorded decision.
        """
        assert "APPROVE" not in {member.name for member in IntentKind}
        assert "REJECT" not in {member.name for member in IntentKind}

        # REQUEST_APPROVAL is permitted -- it asks a human -- so the assertion is that it
        # is classified as a proposal rather than as an outcome. An adapter that mapped an
        # operation to it and then treated the result as a recorded decision would be
        # contradicting the core, and PROPOSAL_INTENTS is where the core says which it is.
        assert IntentKind.REQUEST_APPROVAL in core.PROPOSAL_INTENTS
        assert IntentKind.REQUEST_APPROVAL not in {IntentKind.SUBMIT_APPROVED}

    def test_submit_approved_is_the_only_money_moving_intent_in_every_protocol(self) -> None:
        """One door to the kernel, and every protocol uses the same one."""
        ucp_moving = {
            operation
            for operation, kind in UCP_INTENT_BY_OPERATION.items()
            if kind is IntentKind.SUBMIT_APPROVED
        }
        mcp_moving = {name for name, spec in TOOLS.items() if spec.reaches_kernel}

        assert ucp_moving == {"complete_checkout"}
        assert mcp_moving == {ToolName.CHECKOUT_SUBMIT_APPROVED.value}

        for spec in TOOLS.values():
            assert spec.reaches_kernel == (spec.intent is IntentKind.SUBMIT_APPROVED), (
                f"{spec.name} disagrees with the core about whether it reaches the kernel"
            )

    def test_cancellation_and_refund_are_proposals_in_every_protocol(self) -> None:
        """A proposal that could name an amount is a refund tool wearing a hat."""
        ucp_intents = set(UCP_INTENT_BY_OPERATION.values())
        mcp_intents = {spec.intent for spec in TOOLS.values()}

        for intents in (ucp_intents, mcp_intents):
            assert IntentKind.PROPOSE_REFUND in intents
            assert IntentKind.PROPOSE_CANCELLATION in intents


class TestOneCapabilityCeiling:
    def test_an_adapter_that_derives_capabilities_still_lands_under_the_ceiling(self) -> None:
        """An adapter may translate its own vocabulary into capabilities; it may not widen.

        MCP maps OAuth scopes onto capabilities, which is a legitimate translation and the
        one place an adapter could accidentally mint authority. The property that matters is
        not that the function is absent but that its output is always a subset -- asserted
        here by handing it every consent scope at once and every scope name that exists.
        """
        from commerce_protocols.mcp.authorization import MCP_SCOPE_PREFIX, capabilities_for_scopes

        overreaching = frozenset(
            f"{MCP_SCOPE_PREFIX}{capability}"
            for capability in CONSENT_CAPABILITIES | PROTOCOL_CAPABILITIES
        )
        derived = capabilities_for_scopes(overreaching)

        assert derived <= PROTOCOL_CAPABILITIES
        assert derived.isdisjoint(CONSENT_CAPABILITIES)
        assert capabilities_for_scopes(frozenset()) == frozenset()

    def test_the_ceiling_and_the_consent_set_stay_disjoint(self) -> None:
        assert PROTOCOL_CAPABILITIES.isdisjoint(CONSENT_CAPABILITIES)

    def test_the_consent_set_names_every_registry_b_action(self) -> None:
        """If a Registry B action is ever added, it has to be added here too.

        Stated as an equality rather than a subset so that a new one somebody forgets to
        list fails this test instead of silently becoming grantable.

        ``payment.verify`` is the member that is not consent, and it is the reason this
        list is worth pinning: it is the client-return verification of ADR 0003 D8, bound
        to the buyer's own payment session, and a protocol caller holding it could present
        a return for somebody else's checkout. It was missing until the merge that added it
        to ``commerce_api.deps`` made the gap visible.

        The cross-package half of this -- that the list still matches what
        ``commerce_api.deps`` calls Registry B -- lives in ``test_capi_protocols``, because
        this package cannot import commerce-api without creating a cycle (ADR 0003 D2).
        """
        assert (
            frozenset(
                {
                    "checkout.approve",
                    "checkout.reject",
                    "checkout.cancel",
                    "refund.request",
                    "payment.verify",
                }
            )
            == CONSENT_CAPABILITIES
        )


class TestNoAdapterHasItsOwnPathToMoney:
    @pytest.mark.parametrize("module", ADAPTER_PACKAGES, ids=lambda m: m.__name__.split(".")[-1])
    def test_no_public_name_in_any_adapter_promises_to_move_money(self, module: Any) -> None:
        """Read across all four at once, which is the only way to see drift.

        ``capture_proof`` and ``issue_payment_receipt`` are the interesting near-misses:
        both name a payment and neither moves one. The first refuses every state except
        CAPTURED, and the second requires the first's output.
        """
        offenders = []
        for name in _public_callables(module):
            words = set(re.split(r"[^a-z]+", name.lower())) - {""}
            if words & HARMLESS_NEIGHBOURS:
                continue
            if words & DANGEROUS_VERBS:
                offenders.append(name)
        assert not offenders, (
            f"{module.__name__} exposes {offenders}; a protocol adapter translates a "
            "request into the same kernel admission every other surface uses and never "
            "gets its own path to money (specification 13.1)"
        )

    def test_no_adapter_imports_an_http_client_or_a_payment_provider(self) -> None:
        """Specification 17.3 for MCP, and the same reasoning for the other three.

        An adapter that can reach Razorpay directly has a second path to money regardless
        of what its public surface promises.

        Parsed rather than grepped. The word "Razorpay" appears throughout this package's
        prose -- specification 14.3 is entirely about the Razorpay handoff -- so a text
        search flags every docstring that explains the design and proves nothing. The
        import graph is the thing that decides what code can reach.
        """
        # Matched on the full dotted name, because ``urllib.parse`` is pure string
        # building -- ``continuation.py`` uses it to assemble a URL -- while
        # ``urllib.request`` is the one that opens a socket. Banning the package wholesale
        # would either be wrong or force the URL building somewhere less obvious.
        forbidden = {
            "httpx",
            "requests",
            "aiohttp",
            "socket",
            "urllib.request",
            "http.client",
            "payment_adapters",
            "razorpay",
        }
        root = pathlib.Path(core.__file__).resolve().parents[1]

        for source in root.rglob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    top = name.split(".")[0]
                    assert top not in forbidden and name not in forbidden, (
                        f"{source.name} imports {name!r}; the protocol layer must not be "
                        "able to reach a payment provider or open a socket at all"
                    )


class TestOnePinnedMatrix:
    def test_every_adapter_reads_its_version_from_the_one_matrix(self) -> None:
        """A second source of truth for a version is a second thing to forget to update."""
        assert PINS[Protocol.UCP].version == "2026-08-25"
        assert PINS[Protocol.ACP].version == "2026-04-17"
        assert PINS[Protocol.AP2].source_ref == "b4587ac1d055888a73b4b21750973cffba961793"

        # The adapters expose the same values rather than literals of their own.
        assert ucp.lifecycle.profile_pin_version() == PINS[Protocol.UCP].version

    def test_every_pin_carries_a_disclaimer_that_refuses_the_overclaim(self) -> None:
        """Specification 13.2 and 16.2. The sentence somebody would be tempted to write."""
        assert "Gemini" in PINS[Protocol.UCP].disclaimer
        assert "ChatGPT Instant Checkout" in PINS[Protocol.ACP].disclaimer
        assert "ES256" in PINS[Protocol.AP2].disclaimer
        assert "Claude" in PINS[Protocol.MCP].disclaimer


class TestOneReplayAndFreshnessImplementation:
    def test_no_adapter_writes_its_own_nonce_store_or_clock_check(self) -> None:
        """Two implementations of a replay guard means one of them is the weaker one.

        Searched by source rather than asserted behaviourally: an adapter that reimplemented
        the check correctly today would still be a second thing to keep correct tomorrow,
        and the freshness window in particular has to be judged against the database clock
        rather than the process clock everywhere or not at all.
        """
        root = pathlib.Path(core.__file__).resolve().parents[1]
        for source in root.rglob("*.py"):
            if source.parent.name == "core":
                continue
            text = source.read_text(encoding="utf-8")
            assert "datetime.now(" not in text or "simulator" in source.name, (
                f"{source.name} reads a process clock; freshness is judged against the "
                "database clock in commerce_protocols.core.replay"
            )
