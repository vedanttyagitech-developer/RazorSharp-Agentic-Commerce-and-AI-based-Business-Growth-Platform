"""The wire contract has two implementations, and they must not drift.

``wire/frames.py`` is the authority; ``apps/buyer-web/src/features/voice/wire.ts`` mirrors
it. Two copies of a contract is a contract that diverges, and the way it diverges here is
particularly bad: the client parses frames strictly, so a server frame the client has
never heard of is **dropped silently**. A degradation kind added on one side and not the
other turns a visible degradation into an invisible one -- exactly the defect
specification 19.12 names.

This happened while this package was being written: ``speech_guard_refused`` and
``stale_turn_dropped`` were added server-side after the client had been generated, and
nothing failed. Hence this test.

It reads the TypeScript as text rather than executing it. That is deliberate: a test that
needed Node would be skipped on any machine without it, and a contract test that is
usually skipped is not a contract test.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest
from voice_runtime.wire.frames import ClientFrame, DegradationKind, ServerFrame

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
WIRE_TS: Final[Path] = REPO_ROOT / "apps/buyer-web/src/features/voice/wire.ts"

pytestmark = pytest.mark.skipif(
    not WIRE_TS.exists(), reason="the storefront is not present in this checkout"
)


def _string_list(source: str, name: str) -> frozenset[str]:
    """The quoted strings inside ``export const <name> = [ ... ] as const``."""
    match = re.search(rf"export const {name} = \[(.*?)\] as const", source, re.DOTALL)
    if match is None:
        raise AssertionError(f"{name} is not declared in wire.ts as an `as const` array")
    return frozenset(re.findall(r'"([^"]+)"', match.group(1)))


def _literal_values(annotation: object) -> frozenset[str]:
    """Every ``type`` literal in a discriminated union of pydantic frame models."""
    found: set[str] = set()
    for model in getattr(annotation, "__args__", ()):  # Annotated[...] -> the union
        for member in getattr(model, "__args__", (model,)):
            field = getattr(member, "model_fields", {}).get("type")
            if field is not None and isinstance(field.default, str):
                found.add(field.default)
    return frozenset(found)


def test_every_degradation_kind_exists_on_both_sides() -> None:
    """A kind the client cannot parse is a degradation the buyer never sees."""
    server = frozenset(DegradationKind.__args__)  # type: ignore[attr-defined]
    client = _string_list(WIRE_TS.read_text(), "DEGRADATION_KINDS")
    assert server == client, (
        "degradation kinds have drifted\n"
        f"  server only: {sorted(server - client)}\n"
        f"  client only: {sorted(client - server)}"
    )


def test_every_server_frame_type_is_known_to_the_client() -> None:
    """The client parses strictly, so an unknown frame type is dropped without a trace."""
    source = WIRE_TS.read_text()
    server = _literal_values(ServerFrame)
    assert server, "no server frame types found; the union shape changed"
    missing = {kind for kind in server if f'"{kind}"' not in source}
    assert not missing, f"the storefront cannot parse these server frames: {sorted(missing)}"


def test_every_client_frame_type_is_known_to_the_server() -> None:
    """The reverse: a frame the client sends that the server rejects as invalid."""
    source = WIRE_TS.read_text()
    client_side = _literal_values(ClientFrame)
    assert client_side, "no client frame types found; the union shape changed"
    missing = {kind for kind in client_side if f'"{kind}"' not in source}
    assert not missing, f"the storefront never sends these: {sorted(missing)}"


def test_the_client_takes_its_tunables_from_the_server_rather_than_hardcoding_them() -> None:
    """Specification 19.15: none of these constants is universal, so the client is told.

    Every tunable a client must honour travels on ``session_ready`` and is REQUIRED there
    -- no default, no fallback. A client that quietly substitutes its own guess for a
    missing ``barge_in_level_rms`` has hardcoded a value measured on somebody else's
    speakers, which is precisely what re-measuring per deployment is meant to prevent.
    Failing to parse is the correct behaviour.
    """
    source = WIRE_TS.read_text()
    for tunable in ("mic_frame_ms", "echo_tail_s", "barge_in_level_rms", "playback_lead_s"):
        assert tunable in source, f"the client never learns {tunable}"
        assert f"{tunable}: z.number" in source.replace("z.number().int()", "z.number()"), (
            f"{tunable} must be a plain required field on session_ready"
        )
    # And it must not carry a default, which would silently paper over a missing field.
    for tunable in ("echo_tail_s", "barge_in_level_rms", "playback_lead_s"):
        assert f"{tunable}: z.number().default" not in source, (
            f"{tunable} has a client-side default: a guess dressed as a contract"
        )


def test_the_client_knows_that_voice_carries_no_authority() -> None:
    """19.11 travels on the wire as a field, so the client cannot forget it."""
    assert "voice_is_authority" in WIRE_TS.read_text()


# ---- the decision card, against the function that actually builds it --------------------


def test_the_captured_decision_card_still_matches_what_the_api_builds() -> None:
    """``CHECKOUT_STATE_DECISION_CARD`` is a copy, and a copy is a thing that drifts.

    The voice layer renders money sentences from this shape. If ``agent_service`` renames
    a key -- ``deltas`` to ``items``, say, or drops ``previous_version`` -- the renderer
    would quietly speak a refusal with no deltas in it, which is exactly the "something
    changed" summary specification 19.10 forbids. So the fixture is checked against the
    function, not against itself.
    """
    agent_service = pytest.importorskip(
        "commerce_api.services.agent_service", reason="commerce-api is not installed here"
    )
    build_card = getattr(agent_service, "_decision_card_from", None)
    if build_card is None:  # pragma: no cover - the function was renamed
        pytest.fail("_decision_card_from is gone; the voice renderer needs re-pointing")

    from voice_runtime.testing import CHECKOUT_STATE_DECISION_CARD

    checkout = {
        "checkout_id": CHECKOUT_STATE_DECISION_CARD["checkout_id"],
        "current_version": CHECKOUT_STATE_DECISION_CARD["current_version"],
        "state": CHECKOUT_STATE_DECISION_CARD["state"],
        # The checkout's own key is `deltas` -- that is `CheckoutOut.deltas` from the API --
        # while the card it produces calls the same rows `items`, matching the card
        # vocabulary in `agent_runtime.rendering.cards`. Both names are right in their own
        # context, and this line is where the two meet.
        "deltas": CHECKOUT_STATE_DECISION_CARD["items"],
        "approval_card": {
            "previous_version": CHECKOUT_STATE_DECISION_CARD["previous_version"],
            "version": CHECKOUT_STATE_DECISION_CARD["next_version"],
            "total": CHECKOUT_STATE_DECISION_CARD["total"],
        },
    }
    built = build_card(checkout)
    assert built is not None, "a superseded checkout must still produce a card"
    assert built == CHECKOUT_STATE_DECISION_CARD, (
        "the API's decision card has changed shape; update voice_runtime.testing and "
        "check render_decision_card still reads every field it needs"
    )


def test_a_checkout_that_was_not_superseded_yields_no_card() -> None:
    """The card is stated only under the three conditions, so voice never speaks a
    reapproval for a checkout that has not had one."""
    agent_service = pytest.importorskip("commerce_api.services.agent_service")
    build_card = agent_service._decision_card_from  # noqa: SLF001 - the seam under test

    assert build_card({"deltas": [], "approval_card": {"previous_version": 1}}) is None
    assert build_card({"deltas": [{"field_path": "total"}], "approval_card": None}) is None
    assert (
        build_card(
            {"deltas": [{"field_path": "total"}], "approval_card": {"previous_version": None}}
        )
        is None
    )
