"""``order.track``, and the provenance it promises an external model.

The tool's own docstring makes a claim: an order exists only where a webhook or a provider
fetch put it there (ADR 0003 D8), so the answer names which. The claim is worth nothing if
the field is null, and the field is read out of a wire object whose name for the evidence
kind is ``kind`` while the JSONB column beneath it calls the same fact ``source``. Reading
the column's name off the wire object costs nothing at import time, raises nothing at
runtime, and quietly turns every answer into ``null``.

So these tests hold the two halves of that seam: that the tool reports the source it was
given, for both sources an order can have, and that ``CaptureEvidenceOut`` really is where
the name lives -- a rename on either side fails here rather than degrading into a null that
reads like "no evidence yet".

No database. ``_order``'s two reads and its ownership check are stubbed, because what is
under test is the translation from a real :class:`~commerce_api.schemas.OrderOut` into the
tool result, and a real ``OrderOut`` is exactly what the stubbed renderer returns.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, Final

import pytest
import transaction_kernel as tk
from commerce_api.deps import RequestContext
from commerce_api.schemas import (
    AttemptOut,
    CaptureEvidenceOut,
    MoneyOut,
    OrderOut,
    OrderState,
)
from commerce_api.services import mcp_transport, refund_service
from commerce_api.services.payment_service import AttemptRow
from commerce_api.services.refund_service import OrderRecord
from commerce_domain import ActorType, AgentPrincipal, Money, order_reference, uuid7
from sqlalchemy.orm import Session

ORDER_ID: Final[uuid.UUID] = uuid7()
CHECKOUT_ID: Final[uuid.UUID] = uuid7()
ATTEMPT_ID: Final[uuid.UUID] = uuid7()
TENANT_ID: Final[uuid.UUID] = uuid7()
MERCHANT_ID: Final[uuid.UUID] = uuid7()
BUYER_REF: Final[str] = "mcp-order-track-buyer"
PAYMENT_ID: Final[str] = "pay_ordertrack0001"
AMOUNT: Final[Money] = Money(39500, "INR")


def _context() -> RequestContext:
    """A protocol caller's context: an agent principal that may read an order and no more."""
    return RequestContext(
        tenant_id=TENANT_ID,
        merchant_id=MERCHANT_ID,
        buyer_ref=BUYER_REF,
        principal=AgentPrincipal(
            principal_id="mcp/order-track-test",
            tenant_id=TENANT_ID,
            actor_type=ActorType.AGENT,
            merchant_id=MERCHANT_ID,
            buyer_ref=BUYER_REF,
            capabilities=frozenset({"order.read"}),
        ),
        correlation_id=uuid7(),
        session_id=uuid7(),
        expires_at=datetime.now(tz=UTC),
    )


def _record() -> OrderRecord:
    """What ``load_order`` would hand back. Only ``checkout_id`` is read by the tool."""
    return OrderRecord(
        order_id=ORDER_ID,
        checkout_id=CHECKOUT_ID,
        checkout_version=1,
        attempt=AttemptRow(
            attempt_id=ATTEMPT_ID,
            checkout_id=CHECKOUT_ID,
            checkout_version=1,
            state=tk.PaymentState.CAPTURED,
            amount=AMOUNT,
            receipt="rcpt_order_track",
            provider_order_id="order_ordertrack0001",
            provider_payment_id=PAYMENT_ID,
        ),
        content_hash="cf83e1357eefb8bd",
        content=None,
        policy_receipt_hash="ba7816bf8f01cfea",
        state=OrderState.CONFIRMED,
        amount=AMOUNT,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _payload(evidence: CaptureEvidenceOut | None) -> OrderOut:
    """A real ``OrderOut``, so the tool is mapping the shape the wire actually carries."""
    record = _record()
    return OrderOut(
        order_id=str(ORDER_ID),
        reference=order_reference(ORDER_ID),
        checkout_id=str(CHECKOUT_ID),
        version=1,
        content_hash=record.content_hash,
        policy_receipt_hash=record.policy_receipt_hash,
        state=OrderState.CONFIRMED,
        amount_minor=AMOUNT.minor,
        currency=AMOUNT.currency,
        amount=MoneyOut.of(AMOUNT),
        quote=None,
        payment=AttemptOut(
            attempt_id=str(ATTEMPT_ID),
            version=1,
            state=tk.PaymentState.CAPTURED,
            razorpay_order_id=record.attempt.provider_order_id,
            razorpay_payment_id=PAYMENT_ID,
            grant_id=None,
            capture_evidence=evidence,
            reconciliation_attempts=0,
        ),
        refunds=[],
        created_at="2026-01-01T00:00:00Z",
    )


@pytest.fixture
def unbound_session() -> Iterator[Session]:
    """A session nothing queries: every read below the tool is stubbed out."""
    session = Session()
    yield session
    session.close()


def _track(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    evidence: CaptureEvidenceOut | None,
) -> dict[str, Any]:
    """Run ``order.track``'s handler against an order rendered with ``evidence``."""

    def _owner(_session: Session, _ctx: RequestContext, checkout_id: uuid.UUID) -> None:
        """Ownership is asserted through the checkout, which is the order's, not the tool's."""
        assert checkout_id == CHECKOUT_ID

    def _load(_session: Session, _ctx: RequestContext, *, order_id: uuid.UUID) -> OrderRecord:
        assert order_id == ORDER_ID
        return _record()

    def _render(_session: Session, _ctx: RequestContext, _order: OrderRecord) -> OrderOut:
        return _payload(evidence)

    monkeypatch.setattr(mcp_transport, "assert_owner", _owner)
    monkeypatch.setattr(refund_service, "load_order", _load)
    monkeypatch.setattr(refund_service, "order_payload", _render)
    return mcp_transport._order(session, _context(), ORDER_ID)


@pytest.mark.parametrize("source", [tk.EvidenceSource.WEBHOOK, tk.EvidenceSource.PROVIDER_FETCH])
def test_order_track_names_the_evidence_the_order_was_confirmed_from(
    monkeypatch: pytest.MonkeyPatch, unbound_session: Session, source: tk.EvidenceSource
) -> None:
    """Both sources an order can have reach the model, spelled as the kernel spells them.

    Parametrised over the pair rather than asserting "not null", because a tool that
    reported the right shape and the wrong constant would be a worse failure than the null:
    a model relaying ``WEBHOOK`` for a reconciled payment is stating something false about
    how the platform learned the money moved.
    """
    evidence = CaptureEvidenceOut(
        kind=source.value, reference=PAYMENT_ID, verified_at="2026-01-01T00:00:00Z"
    )
    result = _track(monkeypatch, unbound_session, evidence)
    assert result["capture_evidence_source"] == source.value


def test_order_track_says_null_only_when_there_is_no_evidence_to_name(
    monkeypatch: pytest.MonkeyPatch, unbound_session: Session
) -> None:
    """``None`` is reserved for an order the renderer gave no evidence for.

    This is the case that hid the defect: a null here is a legitimate answer for an
    attempt with no confirmed order, so a null on a confirmed one looked like data rather
    than a bug. The test pins that the tool still answers rather than raising, which is
    why the null was survivable in the first place.
    """
    result = _track(monkeypatch, unbound_session, None)
    assert result["capture_evidence_source"] is None
    assert result["state"] == OrderState.CONFIRMED


def test_the_wire_object_names_the_evidence_kind_and_not_its_source() -> None:
    """The seam itself, so the reason the tool reads ``kind`` is readable from the test.

    ``orders.capture_evidence`` stores ``source``; ``CaptureEvidenceOut`` publishes it as
    ``kind``. Both names are correct in their own layer, which is exactly why reading one
    where the other lives is easy and silent.
    """
    assert "kind" in CaptureEvidenceOut.model_fields
    assert "source" not in CaptureEvidenceOut.model_fields
