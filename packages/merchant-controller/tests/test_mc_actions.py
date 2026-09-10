"""The action's own guarantees, before any of it touches a database.

Three things are worth proving here and the rest follows from them.

The hash covers what an approver read. If a field can change without the digest changing,
the approval means nothing, because the executor checks the digest and not the row. So the
tests below move each field in turn and assert the hash moves with it -- including the ones
it would be easy to leave out, the expected revision and the merchant.

The graph refuses the moves that would lose the record. An approved action cannot go back
to draft, an executed one cannot be re-run, and nothing returns from an outcome except an
unknown, which is reconciled rather than retried.

And the document refuses what a person cannot read. A proposal is shown to somebody before
they agree to it; a value that renders as an object or a float is one whose approval is
weaker than it looks, so it is refused at construction rather than hashed.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

import pytest
from commerce_domain import canonical_hash
from merchant_controller import (
    ACTION_KEYS,
    ACTION_VERSION,
    LIVE_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    MerchantAction,
    MerchantActionError,
    MerchantActionKind,
    MerchantActionResult,
    MerchantActionState,
    action_hash,
    build_action_content,
    may_move,
)

TENANT: Final = uuid.UUID("01a06fd5-0fe4-7a1d-a7b1-42747790b3ce")
MERCHANT: Final = uuid.UUID("01a06fd5-09ca-7774-83d1-fab021012c17")


def content(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "tenant_id": TENANT,
        "merchant_id": MERCHANT,
        "kind": MerchantActionKind.PRICE_CHANGE,
        "target": "AMUL-DAIRY-001",
        "proposal": {"unit_price_minor": 2800, "currency": "INR", "reason": "supplier increase"},
        "expected_revision": 41,
    }
    base.update(overrides)
    return build_action_content(**base)


# --------------------------------------------------------------------------- the document


class TestTheHashedDocument:
    def test_it_carries_its_own_version(self) -> None:
        """Inside the hash, not beside it.

        A version stored next to the digest can be edited without the digest moving, which
        makes it a label rather than a fact. Inside, a shape change produces a different
        hash and every approval recorded under the old shape stops matching -- which is the
        loud failure this platform wants, rather than a quiet reinterpretation.
        """
        assert content()["action_version"] == ACTION_VERSION
        assert "action_version" in ACTION_KEYS

    def test_the_key_set_is_closed(self) -> None:
        assert frozenset(content()) == ACTION_KEYS

    def test_every_field_moves_the_hash(self) -> None:
        """The property the whole design rests on, checked field by field.

        `expected_revision` and `merchant_id` are the two it would be easy to leave out of
        a hand-written document, and they are the two that matter most: the first is what
        makes an approval specific to the world it was given in, and the second is what
        stops one merchant's approval authorising a change to another's shelf.
        """
        original = action_hash(content())
        moved = {
            "target": "INDI-STPL-001",
            "expected_revision": 42,
            "merchant_id": uuid.uuid4(),
            "tenant_id": uuid.uuid4(),
            "kind": MerchantActionKind.LISTING_CHANGE,
            "proposal": {
                "unit_price_minor": 2801,
                "currency": "INR",
                "reason": "supplier increase",
            },
        }
        for field, value in moved.items():
            assert action_hash(content(**{field: value})) != original, field

    def test_the_same_proposal_hashes_the_same_twice(self) -> None:
        assert action_hash(content()) == action_hash(content())

    def test_key_order_in_the_proposal_does_not_change_the_hash(self) -> None:
        """JCS sorts. Two producers writing the same fields in a different order agree."""
        forwards = content(proposal={"a": 1, "b": 2})
        backwards = content(proposal={"b": 2, "a": 1})
        assert action_hash(forwards) == action_hash(backwards)

    def test_an_outside_verifier_reaches_the_same_digest(self) -> None:
        # No private scheme: somebody holding commerce_domain and the stored JSONB gets the
        # same string, which is what makes the approval checkable by someone who does not
        # trust this package.
        assert action_hash(content()) == canonical_hash(content())

    def test_a_true_and_a_one_do_not_hash_alike(self) -> None:
        """`isinstance(True, int)` is true in Python, and that is the trap.

        A boolean coerced to an integer on the way into the document makes "list this
        product" and "list 1 of this product" the same approved change.
        """
        assert action_hash(content(proposal={"listed": True})) != action_hash(
            content(proposal={"listed": 1})
        )


class TestWhatTheDocumentRefuses:
    def test_a_float_is_refused_by_name(self) -> None:
        with pytest.raises(MerchantActionError, match="unit_price"):
            content(proposal={"unit_price": 28.5})

    def test_a_nested_object_is_refused(self) -> None:
        """A proposal a person cannot read as a short list of labelled values."""
        with pytest.raises(MerchantActionError, match="dict"):
            content(proposal={"price": {"minor": 2800, "currency": "INR"}})

    def test_an_empty_proposal_is_refused(self) -> None:
        with pytest.raises(MerchantActionError, match="changes nothing"):
            content(proposal={})

    def test_a_negative_revision_is_refused(self) -> None:
        with pytest.raises(MerchantActionError, match="expected_revision"):
            content(expected_revision=-1)

    def test_an_empty_target_is_refused(self) -> None:
        with pytest.raises(MerchantActionError, match="target"):
            content(target="")


# ----------------------------------------------------------------------------- the graph


class TestTheLifecycle:
    def test_every_state_appears_in_the_graph(self) -> None:
        """A state the graph does not know is one `may_move` raises a KeyError on."""
        assert set(TRANSITIONS) == set(MerchantActionState)

    def test_an_approved_action_cannot_go_back_to_draft(self) -> None:
        """The single most important refusal in the file.

        An edit is a new hash, so a draft that kept its approval across an edit would be an
        approval for a document nobody read. There is deliberately no path back.
        """
        assert not may_move(MerchantActionState.APPROVED, MerchantActionState.DRAFT)

    def test_nothing_comes_back_from_an_outcome_except_an_unknown(self) -> None:
        """Unknown is not failure. It is reconciled to the truth, which is why it moves."""
        for state in (
            MerchantActionState.SUCCEEDED,
            MerchantActionState.FAILED,
            MerchantActionState.REJECTED,
            MerchantActionState.EXPIRED,
            MerchantActionState.CANCELLED,
            MerchantActionState.STALE,
        ):
            assert TRANSITIONS[state] == frozenset(), state
        assert TRANSITIONS[MerchantActionState.UNKNOWN] == frozenset(
            {MerchantActionState.SUCCEEDED, MerchantActionState.FAILED}
        )

    def test_an_executing_action_cannot_be_declared_stale_from_outside(self) -> None:
        """Whether the world moved underneath is the executor's finding, not a bystander's.

        Once the change has been sent, only the component that sent it can say what
        happened. A third party marking it stale would be deciding an outcome it cannot
        see, and the honest answers are already there: FAILED, or UNKNOWN.
        """
        assert not may_move(MerchantActionState.EXECUTING, MerchantActionState.STALE)
        assert may_move(MerchantActionState.APPROVED, MerchantActionState.STALE)
        assert may_move(MerchantActionState.QUEUED, MerchantActionState.STALE)

    def test_the_live_and_terminal_sets_agree_with_the_graph(self) -> None:
        """Derived rather than listed, so the two cannot drift apart."""
        assert frozenset(s for s, onward in TRANSITIONS.items() if not onward) == TERMINAL_STATES
        assert LIVE_STATES.isdisjoint(TERMINAL_STATES)
        assert set(MerchantActionState) == LIVE_STATES | TERMINAL_STATES

    def test_an_action_reaches_execution_only_through_approval(self) -> None:
        """No path from draft to queued that skips somebody agreeing."""
        reachable = {MerchantActionState.DRAFT}
        frontier = [MerchantActionState.DRAFT]
        while frontier:
            state = frontier.pop()
            for onward in TRANSITIONS[state]:
                if onward is MerchantActionState.APPROVED or onward in reachable:
                    continue
                reachable.add(onward)
                frontier.append(onward)
        assert MerchantActionState.QUEUED not in reachable
        assert MerchantActionState.EXECUTING not in reachable
        assert MerchantActionState.SUCCEEDED not in reachable


# ---------------------------------------------------------------------------- the record


def action(**overrides: Any) -> MerchantAction:
    document = content()
    base: dict[str, Any] = {
        "action_id": uuid.uuid4(),
        "tenant_id": TENANT,
        "merchant_id": MERCHANT,
        "kind": MerchantActionKind.PRICE_CHANGE,
        "target": "AMUL-DAIRY-001",
        "proposal": document["proposal"],
        "expected_revision": 41,
        "state": MerchantActionState.AWAITING_APPROVAL,
        "content_hash": action_hash(document),
        "proposed_by": "agent:merchant_copilot",
    }
    base.update(overrides)
    return MerchantAction(**base)


class TestTheRecord:
    def test_a_stored_action_reproduces_its_own_hash(self) -> None:
        assert action().hash_matches()

    def test_an_edited_field_stops_matching(self) -> None:
        """How the executor learns the row moved after somebody approved it."""
        assert not action(target="INDI-STPL-001").hash_matches()
        assert not action(expected_revision=42).hash_matches()

    def test_the_proposer_is_recorded_separately_from_the_approver(self) -> None:
        """A model proposing is a fact the approver deserves, and it is never relabelled.

        `proposed_by` may name an agent. `approved_by` must not be filled from it: a model
        remains an AGENT principal and is never written down as the human who agreed.
        """
        drafted = action()
        assert drafted.proposed_by.startswith("agent:")
        assert drafted.approved_by is None


class TestTheResult:
    def test_a_refusal_names_the_rule_and_the_alternatives(self) -> None:
        refused = MerchantActionResult(
            action_id=uuid.uuid4(),
            state=MerchantActionState.APPROVED,
            content_hash="x",
            ok=False,
            reason="not_a_permitted_move",
            allowed=(MerchantActionState.QUEUED,),
        )
        assert not refused.ok
        assert refused.allowed == (MerchantActionState.QUEUED,)

    def test_a_success_carries_no_alternatives(self) -> None:
        """A success with a list of other options invites a caller to read it as a choice."""
        with pytest.raises(ValueError, match="alternatives"):
            MerchantActionResult(
                action_id=uuid.uuid4(),
                state=MerchantActionState.APPROVED,
                content_hash="x",
                ok=True,
                reason="ok",
                allowed=(MerchantActionState.QUEUED,),
            )

    def test_a_refusal_must_say_why(self) -> None:
        with pytest.raises(ValueError, match="which rule"):
            MerchantActionResult(
                action_id=uuid.uuid4(),
                state=MerchantActionState.APPROVED,
                content_hash="x",
                ok=False,
                reason="ok",
            )


class TestTheBoundary:
    def test_this_package_does_not_import_the_kernel(self) -> None:
        """The rule this whole package exists for.

        A merchant action is not an Operation and its outcome is not an AdmissionDecision.
        If this package could import the kernel it could borrow those, and the day it does
        is the day "changing a shelf label" and "taking a payment" are reviewed by one set
        of rules -- which loosens the narrower one.
        """
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "src" / "merchant_controller"
        offenders: list[str] = []
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    offenders += [
                        f"{path.name}:{node.lineno}"
                        for a in node.names
                        if a.name.split(".")[0] == "transaction_kernel"
                    ]
                elif (
                    isinstance(node, ast.ImportFrom)
                    and node.module
                    and node.module.split(".")[0] == "transaction_kernel"
                ):
                    offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == [], f"the Controller reached into the kernel: {offenders}"
