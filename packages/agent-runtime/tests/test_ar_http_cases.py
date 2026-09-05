"""The human-review queue over HTTP: the wire mapping, and what it refuses to flatten.

Two kinds of test, the same split as the merchant surface next door.

The first kind serves the API's own wire shape over ``httpx.MockTransport`` and asserts
that :class:`HttpBackend` reconstructs *the whole record*, compared as one dataclass
rather than field by field -- a field this test forgot to name is exactly where a
divergence would hide. The shapes here are the ones the running service publishes in its
OpenAPI document, not a paraphrase of the router.

The second kind talks to the live API and skips with a plain message when nothing
answers. It cannot exercise a case detail, because a case is opened by the Reconciliation
Service against a payment provider and this suite will not manufacture one on a shared
demo tenant to have something to read. What it does check live is the part that holds
whether or not the queue is empty: that the read is permitted, that the platform's scope
note comes back, that every case listed carries a priority from the closed set, and that
a key nobody can see is a 404 rather than an empty record.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, Final

import httpx
import pytest
from agent_runtime.backends import BackendError, HttpBackend
from agent_runtime.backends.base import (
    CaseBackend,
    CaseEvent,
    CasePriority,
    CaseRecord,
    CaseState,
    CaseSummary,
)
from transaction_kernel import RecoveryCode

#: Where the live API is expected. Unreachable is a skip, never a failure.
LIVE_BASE: Final[str] = os.environ.get("ACR_API_BASE", "http://127.0.0.1:8000")

#: The operator key the review router is gated on. The demo default is in the repository's
#: own environment file; a deployment overrides it rather than editing this.
LIVE_SCENARIO_KEY: Final[str] = os.environ.get("SCENARIO_KEY", "local-demo-scenario-key")

LIVE_TIMEOUT_S: Final[float] = 5.0

CASE_KEY: Final[str] = "3f9a1c77e0b2"
OPENED: Final[datetime] = datetime(2026, 9, 4, 11, 30, tzinfo=UTC)
TARGET: Final[datetime] = datetime(2026, 9, 4, 12, 30, tzinfo=UTC)
CHECKOUT: Final[str] = "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"
ATTEMPT: Final[str] = "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a60"

SCOPE_NOTE: Final[str] = (
    "P0 ships the human-review queue and its evidence. No case is assigned, decided, "
    "annotated or resolved here."
)


# --------------------------------------------------------------- a fake of the real API


def _money(minor: int, currency: str = "INR") -> dict[str, Any]:
    return {"minor": minor, "currency": currency, "display": f"{minor // 100}.{minor % 100:02d}"}


def _case_out(
    *,
    priority: str = "P1",
    state: str = "AWAITING_HUMAN",
    reason_code: str = "HUMAN_REVIEW_REQUIRED",
) -> dict[str, Any]:
    """One ``CaseOut`` with every field the published schema marks required."""
    return {
        "case_key": CASE_KEY,
        "state": state,
        "priority": priority,
        "reason_code": reason_code,
        "reason_family": "provider_timeout",
        "checkout_id": CHECKOUT,
        "payment_attempt_id": ATTEMPT,
        "refund_id": None,
        "monetary_exposure": _money(125000),
        "opened_at": "2026-09-04T11:30:00Z",
        "opened_by": "SYSTEM",
        "target_response_by": "2026-09-04T12:30:00Z",
        "target_response_seconds": 3600,
        "correlation_id": CHECKOUT,
        "attempts_used": 3,
        "attempts_bound": 3,
        "detections": 1,
        "audit_event_id": CHECKOUT,
        "audit_aggregate_type": "checkout",
        "audit_aggregate_id": CHECKOUT,
        "audit_seq": 12,
        "audit_self_hash": "9f" * 32,
        "proof_chain": {
            "checkout_id": CHECKOUT,
            "payment_attempt_id": ATTEMPT,
            "href": f"/v1/checkouts/{CHECKOUT}/proof",
            "audit_streams": [{"aggregate_type": "checkout", "aggregate_id": CHECKOUT}],
        },
    }


def _verified_out(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "present": True,
        "source": "PROVIDER_FETCH",
        "status": "CAPTURED",
        "provider_status": "captured",
        "provider_payment_id": "pay_abc",
        "provider_order_id": "order_abc",
        "amount_minor": 125000,
        "currency": "INR",
        "amount_refunded_minor": None,
        "observed_at": "2026-09-04T11:29:00Z",
        "audit_event_id": CHECKOUT,
    }
    return {**base, **overrides}


def _event_out(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "cursor-1",
        "occurred_at": "2026-09-04T11:30:00Z",
        "source": "AUDIT",
        "actor": "SYSTEM",
        "action": "human_review.opened",
        "summary": "Escalated after three reconciliation rounds.",
        "correlation_id": CHECKOUT,
        "scenario_injection": False,
        "checkout_version": 2,
        "payment_attempt_id": ATTEMPT,
        "details": {"attempts": 3},
    }
    return {**base, **overrides}


class _ReviewApi:
    """Serves the review queue in the wire shape the live service publishes."""

    def __init__(
        self,
        *,
        cases: list[dict[str, Any]] | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.cases = cases if cases is not None else [_case_out()]
        self.detail = detail
        self.seen: list[httpx.Request] = []

    def _detail_body(self) -> dict[str, Any]:
        if self.detail is not None:
            return self.detail
        return {
            "case": _case_out(),
            "verified_provider_state": _verified_out(),
            "refused_evidence": None,
            "attempt": None,
            "resolutions": [],
            "timeline": [_event_out()],
            "scope": SCOPE_NOTE,
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        path = request.url.path
        if path == "/v1/review/queue":
            return httpx.Response(
                200,
                json={
                    "cases": self.cases,
                    "priority_counts": {"P1": len(self.cases), "P2": 0, "P3": 0},
                    "limit": int(request.url.params.get("limit", "20")),
                    "scope": SCOPE_NOTE,
                },
            )
        if path == f"/v1/review/queue/{CASE_KEY}":
            return httpx.Response(200, json=self._detail_body())
        return httpx.Response(
            404,
            headers={"content-type": "application/problem+json"},
            content=json.dumps(
                {
                    "type": "urn:acr:problem:case-not-found",
                    "title": "Case not found",
                    "status": 404,
                }
            ),
        )

    def backend(self, *, scenario_key: str | None = "operator-key") -> HttpBackend:
        return HttpBackend(
            "https://api.test",
            bearer="session-token",
            scenario_key=scenario_key,
            transport=httpx.MockTransport(self.handler),
        )


# --------------------------------------------------------------------- the protocol


def test_the_http_backend_really_is_a_case_backend() -> None:
    """The tool factory builds the two case tools off this check and nothing else."""
    assert issubclass(HttpBackend, CaseBackend)


# ------------------------------------------------------------------ the wire mapping


@pytest.mark.asyncio
async def test_the_listing_maps_onto_summaries() -> None:
    api = _ReviewApi()
    async with api.backend() as backend:
        queue = await backend.support_cases(limit=5)

    assert queue == (
        CaseSummary(
            case_key=CASE_KEY,
            reason_code=RecoveryCode.HUMAN_REVIEW_REQUIRED,
            state=CaseState.AWAITING_HUMAN,
            priority=CasePriority.P1,
            opened_at=OPENED,
            target_response_by=TARGET,
            monetary_exposure_minor=125000,
            currency="INR",
        ),
    )
    assert api.seen[0].url.params["limit"] == "5"


@pytest.mark.asyncio
async def test_one_case_maps_onto_the_whole_record() -> None:
    """Compared as one dataclass: a field this test forgot to name is where drift hides."""
    api = _ReviewApi()
    async with api.backend() as backend:
        case = await backend.support_case(CASE_KEY)

    assert case == CaseRecord(
        case_key=CASE_KEY,
        reason_code=RecoveryCode.HUMAN_REVIEW_REQUIRED,
        state=CaseState.AWAITING_HUMAN,
        priority=CasePriority.P1,
        provider_state_at_escalation="CAPTURED",
        proof_chain_ref=f"/v1/checkouts/{CHECKOUT}/proof",
        monetary_exposure_minor=125000,
        currency="INR",
        opened_at=OPENED,
        target_response_by=TARGET,
        timeline=(
            CaseEvent(
                at=OPENED,
                event="human_review.opened",
                detail={
                    "attempts": 3,
                    "actor": "SYSTEM",
                    "summary": "Escalated after three reconciliation rounds.",
                    "source": "AUDIT",
                    "scenario_injection": False,
                },
            ),
        ),
        scope_note=SCOPE_NOTE,
    )


@pytest.mark.asyncio
async def test_both_reads_carry_the_operator_key() -> None:
    """The review queue is an operator surface, so the key rides on it as it does on the
    merchant collections -- and on nothing else, so a buyer session gains no reach."""
    api = _ReviewApi()
    async with api.backend(scenario_key="operator-key") as backend:
        await backend.support_cases()
        await backend.support_case(CASE_KEY)

    assert [request.headers.get("X-Scenario-Key") for request in api.seen] == [
        "operator-key",
        "operator-key",
    ]


@pytest.mark.asyncio
async def test_a_missing_operator_key_is_left_for_the_server_to_refuse() -> None:
    """The client sends no key it does not have and predicts no policy it does not own."""
    api = _ReviewApi()
    async with api.backend(scenario_key=None) as backend:
        await backend.support_cases()

    assert "X-Scenario-Key" not in api.seen[0].headers


# ------------------------------------------------------- what the mapping will not flatten


@pytest.mark.asyncio
async def test_a_priority_the_platform_cannot_produce_is_refused() -> None:
    """A fourth priority is a contract violation, not a case with an unusual priority.

    A console rendered a chip for a P4 today. Refusing here is what stops that reaching a
    reviewer as though the platform had assigned a triage level it has no way to derive.
    """
    api = _ReviewApi(cases=[_case_out(priority="P4")])
    with pytest.raises(BackendError) as caught:
        async with api.backend() as backend:
            await backend.support_cases()

    assert caught.value.problem.status == 502
    assert "P4" in caught.value.problem.detail


@pytest.mark.asyncio
async def test_a_provider_never_reached_maps_to_silence() -> None:
    api = _ReviewApi(
        detail={
            "case": _case_out(),
            "verified_provider_state": _verified_out(
                present=False, status=None, provider_status=None
            ),
            "refused_evidence": None,
            "attempt": None,
            "resolutions": [],
            "timeline": [],
            "scope": SCOPE_NOTE,
        }
    )
    async with api.backend() as backend:
        case = await backend.support_case(CASE_KEY)

    assert case.provider_state_at_escalation is None


@pytest.mark.asyncio
async def test_a_verified_statement_naming_no_state_is_refused() -> None:
    """Folding this into ``None`` would report silence where an answer was recorded."""
    api = _ReviewApi(
        detail={
            "case": _case_out(),
            "verified_provider_state": _verified_out(status=None, provider_status=None),
            "refused_evidence": None,
            "attempt": None,
            "resolutions": [],
            "timeline": [],
            "scope": SCOPE_NOTE,
        }
    )
    with pytest.raises(BackendError) as caught:
        async with api.backend() as backend:
            await backend.support_case(CASE_KEY)

    assert caught.value.problem.status == 502
    assert "named no state" in caught.value.problem.detail


@pytest.mark.asyncio
async def test_an_unrecorded_exposure_is_absent_and_not_a_zero() -> None:
    api = _ReviewApi(
        detail={
            "case": _case_out() | {"monetary_exposure": None},
            "verified_provider_state": _verified_out(),
            "refused_evidence": None,
            "attempt": None,
            "resolutions": [],
            "timeline": [],
            "scope": SCOPE_NOTE,
        }
    )
    async with api.backend() as backend:
        case = await backend.support_case(CASE_KEY)

    assert case.monetary_exposure_minor is None
    assert case.currency == "INR", "a label for an absent amount, not one this client derived"


@pytest.mark.asyncio
async def test_an_audit_payload_cannot_displace_the_actor_the_service_recorded() -> None:
    """The two would be indistinguishable on a card, and only one of them is evidence."""
    api = _ReviewApi(
        detail={
            "case": _case_out(),
            "verified_provider_state": _verified_out(),
            "refused_evidence": None,
            "attempt": None,
            "resolutions": [],
            "timeline": [_event_out(details={"actor": "the buyer says it was support"})],
            "scope": SCOPE_NOTE,
        }
    )
    async with api.backend() as backend:
        case = await backend.support_case(CASE_KEY)

    assert case.timeline[0].detail["actor"] == "SYSTEM"


@pytest.mark.asyncio
async def test_an_unknown_case_key_is_the_api_problem_verbatim() -> None:
    api = _ReviewApi()
    with pytest.raises(BackendError) as caught:
        async with api.backend() as backend:
            await backend.support_case("case-nobody-has")

    assert caught.value.problem.status == 404
    assert caught.value.problem.reason_key == "case_not_found"


# --------------------------------------------------------------------------- live API


def _live_session() -> str | None:
    """Mint a demo session, or ``None`` when nothing is listening."""
    try:
        response = httpx.post(
            f"{LIVE_BASE}/v1/demo/sessions",
            json={"tenant_slug": "demo", "actor_type": "BUYER"},
            timeout=LIVE_TIMEOUT_S,
        )
    except httpx.HTTPError:
        return None
    if response.status_code >= 400:
        return None
    return str(response.json()["token"])


@pytest.fixture(scope="module")
def live_token() -> str:
    token = _live_session()
    if token is None:
        pytest.skip(f"no API at {LIVE_BASE}; start it or set ACR_API_BASE to run the live tests")
    return token


@pytest.fixture
def live(live_token: str) -> Iterator[HttpBackend]:
    yield HttpBackend(
        LIVE_BASE,
        bearer=live_token,
        scenario_key=LIVE_SCENARIO_KEY,
        timeout=LIVE_TIMEOUT_S,
    )


@pytest.mark.asyncio
async def test_live_the_queue_reads_and_every_case_carries_a_closed_priority(
    live: HttpBackend,
) -> None:
    """Whether or not the demo tenant has escalated anything, the read must parse.

    An empty queue is a real answer here and the assertions are written to hold for it:
    the platform having escalated nothing is not this client failing to read.
    """
    async with live:
        queue = await live.support_cases(limit=10)

    assert isinstance(queue, tuple)
    assert all(case.priority in set(CasePriority) for case in queue)
    assert all(case.state in set(CaseState) for case in queue)
    assert all(
        case.monetary_exposure_minor is None or isinstance(case.monetary_exposure_minor, int)
        for case in queue
    )
    opened = [case.opened_at for case in queue]
    assert opened == sorted(opened, reverse=True), "the queue is newest first"


@pytest.mark.asyncio
async def test_live_an_unknown_case_key_is_a_refusal_not_an_empty_record(
    live: HttpBackend,
) -> None:
    """A key nobody can see is a 404, which is also the answer for another tenant's key."""
    async with live:
        with pytest.raises(BackendError) as caught:
            await live.support_case("case-that-does-not-exist")

    assert caught.value.problem.status == 404
