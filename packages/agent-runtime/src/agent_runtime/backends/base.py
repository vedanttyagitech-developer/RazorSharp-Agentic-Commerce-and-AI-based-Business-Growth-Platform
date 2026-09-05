"""The only commerce operations an agent may call.

:class:`CommerceBackend` is the whole of an agent's reach into the platform. It exposes
grounded reads (search, product, basket re-quote, checkout view), basket construction,
checkout creation and one money-adjacent operation: submitting an *already approved*
checkout version to the Transaction Assurance Kernel for admission.

WHAT IS DELIBERATELY ABSENT
---------------------------
Approve, reject, revoke, pay, refund and cancel do not exist on this interface. They are
Registry B (trusted buyer-surface) or Registry C (kernel-internal) operations in
specification 5.3, and an agent manifest can never name them. Keeping them off the class
-- not merely off the tool list -- means a compromised prompt has nothing to call: there
is no method for a capability gate to forget to guard.

RESULT SHAPES
-------------
Every result is a frozen dataclass carrying exact :class:`commerce_domain.Money` values
and provenance (source, catalogue revision). The agent copies these numbers into prose;
it never computes them. :class:`BasketQuote` re-checks its own total at construction so a
backend that returns a total contradicting its components is refused rather than repeated
to the buyer.

Errors are RFC 9457 problem details (ADR 0003 D15) wrapped in :class:`BackendError`. A
kernel *denial* is not an error: it arrives as a structured
:class:`transaction_kernel.KernelDecision` with ``allowed=False``, because a denial is the
system working.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from commerce_domain import Money
from merchant_sim import Locale
from transaction_kernel import KernelDecision, RecoveryCode

__all__ = [
    "AGENT_OPERATIONS",
    "NEVER_ON_AGENT_SURFACE",
    "ApprovalCard",
    "BackendError",
    "BasketQuote",
    "BasketView",
    "CaseBackend",
    "CaseEvent",
    "CasePriority",
    "CaseRecord",
    "CaseState",
    "CaseSummary",
    "CheckoutStatus",
    "CheckoutView",
    "CommerceBackend",
    "OrderState",
    "OrderView",
    "PaymentSummary",
    "PricedLine",
    "Problem",
    "ProductCard",
    "Provenance",
    "RefundRecord",
    "SearchPage",
    "UnavailableLine",
    "backend_problem",
]


# --------------------------------------------------------------------------- errors


@dataclass(frozen=True, slots=True)
class Problem:
    """RFC 9457 problem details. ``problem_type`` is the ``type`` member (a URI)."""

    problem_type: str
    title: str
    status: int
    detail: str = ""
    instance: str | None = None
    extensions: Mapping[str, Any] = field(default_factory=dict)

    @property
    def reason_key(self) -> str:
        """Stable key for the agent to explain: the last path segment of ``type``.

        ``urn:acr:problem:unknown-sku`` and ``https://api/problems/unknown-sku`` both give
        ``unknown_sku``. The agent renders the key; it never parses ``detail`` for meaning.
        """
        tail = self.problem_type.rstrip("/").replace(":", "/").rsplit("/", 1)[-1]
        return (tail or "error").replace("-", "_").lower()


class BackendError(Exception):
    """A backend refused or failed a call. Carries the structured problem, never prose alone."""

    def __init__(self, problem: Problem) -> None:
        message = f"{problem.status} {problem.title}"
        if problem.detail:
            message = f"{message}: {problem.detail}"
        super().__init__(message)
        self.problem = problem


def backend_problem(
    reason: str, *, status: int, title: str, detail: str = "", **extensions: Any
) -> BackendError:
    """Build a :class:`BackendError` for an in-process backend with a stable reason key."""
    return BackendError(
        Problem(
            problem_type=f"urn:acr:problem:{reason}",
            title=title,
            status=status,
            detail=detail,
            extensions=dict(extensions),
        )
    )


# --------------------------------------------------------------------------- results


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a fact came from and which catalogue generation it belongs to (spec 6.2)."""

    source: str
    catalogue_revision: int
    observed_at: datetime | None = None

    def to_payload(self) -> dict[str, Any]:
        return {"source": self.source, "catalogue_revision": self.catalogue_revision}


@dataclass(frozen=True, slots=True)
class ProductCard:
    """One product as the merchant reports it right now.

    ``name`` and ``description`` are merchant-authored text and therefore untrusted
    (specification 20.1). They are fenced before a model sees them; the structured fields
    beside them are what the agent may quote as fact.
    """

    sku: str
    name: str
    description: str
    category: str
    unit_label: str
    unit_price: Money
    stock_units: int
    is_listed: bool
    is_available: bool
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class SearchPage:
    """Grounded search results. ``skus()`` is the set a model may reference afterwards."""

    query: str
    locale: Locale
    hits: tuple[ProductCard, ...]
    provenance: Provenance

    def skus(self) -> tuple[str, ...]:
        return tuple(hit.sku for hit in self.hits)


@dataclass(frozen=True, slots=True)
class PricedLine:
    """One priced basket line, exactly as the fee engine computed it."""

    sku: str
    name: str
    quantity: int
    unit_price: Money
    subtotal: Money
    tax_bp: int
    tax: Money


@dataclass(frozen=True, slots=True)
class BasketQuote:
    """A deterministic quote. Refuses to exist if its total contradicts its components.

    The check duplicates :class:`merchant_sim.Quote`'s on purpose: an HTTP backend hands
    us numbers we did not compute, and the agent must not repeat a total the components
    do not support.
    """

    lines: tuple[PricedLine, ...]
    items_subtotal: Money
    items_tax: Money
    delivery_fee: Money
    delivery_tax: Money
    total: Money
    free_delivery_applied: bool
    gap_to_free_delivery: Money
    currency: str
    content_hash: str
    provenance: Provenance
    free_delivery_threshold: Money | None = None

    def __post_init__(self) -> None:
        if not self.lines:
            raise ValueError("a quote prices at least one line")
        recomputed = self.items_subtotal + self.items_tax + self.delivery_fee + self.delivery_tax
        if self.total != recomputed:
            raise ValueError(
                f"quote total {self.total} does not equal its components {recomputed}; "
                "the agent may not present a total the fee engine did not compute"
            )
        if self.free_delivery_applied != self.delivery_fee.is_zero:
            raise ValueError("free_delivery_applied disagrees with the delivery fee charged")

    def amounts(self) -> tuple[Money, ...]:
        """Every money fact in this quote, for the grounding ledger."""
        facts: list[Money] = [
            self.items_subtotal,
            self.items_tax,
            self.delivery_fee,
            self.delivery_tax,
            self.total,
            self.gap_to_free_delivery,
        ]
        if self.free_delivery_threshold is not None:
            facts.append(self.free_delivery_threshold)
        for line in self.lines:
            facts.extend((line.unit_price, line.subtotal, line.tax))
        return tuple(facts)


@dataclass(frozen=True, slots=True)
class UnavailableLine:
    """Why one requested line could not be priced. Mirrors merchant-sim's Unavailability."""

    sku: str
    requested: int
    available_units: int
    listed: bool


@dataclass(frozen=True, slots=True)
class BasketView:
    """A basket and its current quote, or the structured reason it has none.

    ``code`` is ``OK`` for an empty basket and for a priced one; a basket with lines the
    merchant cannot fulfil carries ``STALE_CHECKOUT`` and names them in ``unavailable``.
    """

    basket_id: str
    code: RecoveryCode
    lines: tuple[tuple[str, int], ...]
    quote: BasketQuote | None
    unavailable: tuple[UnavailableLine, ...]
    stale: bool
    provenance: Provenance

    def __post_init__(self) -> None:
        if self.code is RecoveryCode.OK and self.lines and self.quote is None:
            raise ValueError("a non-empty OK basket must carry a quote")
        if self.code is not RecoveryCode.OK and not self.unavailable:
            raise ValueError("a refused basket must name the lines it could not price")

    @property
    def is_empty(self) -> bool:
        return not self.lines


class CheckoutStatus(StrEnum):
    """Lifecycle of one checkout version as the agent surface sees it."""

    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    INVALIDATED = "INVALIDATED"
    ADMITTED = "ADMITTED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class ApprovalCard:
    """Facts of one immutable checkout version: what the trusted surface will show.

    The agent presents these facts and says where approval happens. It cannot approve.
    """

    checkout_id: str
    version: int
    content_hash: str
    status: CheckoutStatus
    quote: BasketQuote
    expires_at: datetime | None = None

    @property
    def total(self) -> Money:
        return self.quote.total


@dataclass(frozen=True, slots=True)
class PaymentSummary:
    """Attempt id and provider-verified state. ``CAPTURED`` is the only success state.

    ``capture_evidence`` names how the platform learned the money moved and is never
    ``BROWSER_CALLBACK`` (ADR 0003 D8). It is carried here so an agent explaining a
    completed payment quotes the evidence kind rather than the conversation's belief.
    """

    attempt_id: str
    state: str
    capture_evidence: str | None = None

    @property
    def is_captured(self) -> bool:
        return self.state == "CAPTURED"


@dataclass(frozen=True, slots=True)
class CheckoutView:
    """Head, every version and the attempt summary of one checkout."""

    checkout_id: str
    current_version: int
    versions: tuple[ApprovalCard, ...]
    payment: PaymentSummary | None

    def __post_init__(self) -> None:
        if not self.versions:
            raise ValueError("a checkout has at least one version")
        if self.current.version != self.current_version:
            raise ValueError("current_version does not name the last version")

    @property
    def current(self) -> ApprovalCard:
        return self.versions[-1]


class OrderState(StrEnum):
    """Lifecycle of a confirmed sale. Mirrors ``commerce_api.schemas.OrderState``.

    Declared here rather than imported because ``commerce-api`` sits above this package in
    the dependency order (ADR 0003 D2) and an agent must be able to read an order from the
    in-memory backend with no HTTP layer present at all.
    """

    CONFIRMED = "CONFIRMED"
    FULFILMENT_BLOCKED = "FULFILMENT_BLOCKED"
    CANCELLED = "CANCELLED"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    REFUNDED = "REFUNDED"


@dataclass(frozen=True, slots=True)
class RefundRecord:
    """One refund against a captured payment, as the order reports it.

    ``automatic`` marks the stale-capture path: the platform protecting a buyer from money
    that landed on an invalidated checkout, rather than a support action. An agent may read
    this; it has no way to create one.
    """

    refund_id: str
    amount: Money
    state: str
    reason: str
    automatic: bool


@dataclass(frozen=True, slots=True)
class OrderView:
    """A confirmed sale and every money fact an agent may quote about it.

    An order exists only where verified capture evidence put it there, so this is the one
    result an agent may speak about in the past tense. Even here it quotes ``payment.state``
    and ``payment.capture_evidence`` rather than asserting success on its own account.
    """

    order_id: str
    checkout_id: str
    version: int
    content_hash: str
    state: OrderState
    amount: Money
    payment: PaymentSummary
    refunds: tuple[RefundRecord, ...] = ()
    quote: BasketQuote | None = None
    policy_receipt_hash: str | None = None

    def amounts(self) -> tuple[Money, ...]:
        """Every money fact on this order, for the grounding ledger."""
        facts: list[Money] = [self.amount]
        facts.extend(refund.amount for refund in self.refunds)
        if self.quote is not None:
            facts.extend(self.quote.amounts())
        return tuple(facts)


# --------------------------------------------------------------------------- interface

#: The complete set of operations an agent surface may expose. Tests assert that the
#: abstract interface has exactly these and nothing else.
AGENT_OPERATIONS: Final[frozenset[str]] = frozenset(
    {
        "search",
        "product",
        "basket_create",
        "basket_set_line",
        "basket_get",
        "checkout_create",
        "checkout_get",
        "checkout_submit_approved",
        "order_track",
    }
)

#: Names that must never appear on any agent-reachable backend. Registry B and C verbs.
NEVER_ON_AGENT_SURFACE: Final[frozenset[str]] = frozenset(
    {"approve", "reject", "revoke", "pay", "refund", "cancel", "capture", "verify_payment"}
)


class CommerceBackend(ABC):
    """Registry A over the ADR 0003 endpoint catalogue. Agents propose through this only."""

    @abstractmethod
    async def search(self, query: str, locale: Locale, limit: int) -> SearchPage:
        """GET /v1/catalogue/search: grounded discovery with freshness."""

    @abstractmethod
    async def product(self, sku: str) -> ProductCard:
        """GET /v1/catalogue/products/{sku}: live detail. Unknown SKU is a problem, not empty."""

    @abstractmethod
    async def basket_create(self) -> BasketView:
        """POST /v1/baskets: a new, empty basket."""

    @abstractmethod
    async def basket_set_line(self, basket_id: str, sku: str, quantity: int) -> BasketView:
        """PUT /v1/baskets/{id}/lines/{sku}: set a quantity (0 removes); returns the quote."""

    @abstractmethod
    async def basket_get(self, basket_id: str) -> BasketView:
        """GET /v1/baskets/{id}: re-quote and report staleness."""

    @abstractmethod
    async def checkout_create(self, basket_id: str) -> ApprovalCard:
        """POST /v1/baskets/{id}/checkout: version 1 plus receipt and reservation."""

    @abstractmethod
    async def checkout_get(self, checkout_id: str) -> CheckoutView:
        """GET /v1/checkouts/{id}: head, versions, approval card, attempt summary."""

    @abstractmethod
    async def checkout_submit_approved(
        self, checkout_id: str, version: int, content_hash: str
    ) -> KernelDecision:
        """POST /v1/checkouts/{id}/versions/{v}/submit: kernel admission, decision verbatim.

        The agent's final money-adjacent operation (specification 6.3). It submits a version
        the buyer already approved on the trusted surface; the kernel decides admission and
        answers with a structured decision the agent may explain but never override.
        """

    @abstractmethod
    async def order_track(self, order_id: str) -> OrderView:
        """GET /v1/orders/{id}: order state, capture evidence and refunds already issued.

        Read-only, and the only past-tense fact source an agent has. ``order.propose_cancel``
        and ``refund.propose`` in specification 6.3 are proposals made in conversation, not
        methods here: proposing costs nothing, and executing is Registry B.
        """


# ---------------------------------------------------------------------- merchant surface


@dataclass(frozen=True, slots=True)
class CatalogueHealth:
    """The shape of a merchant's catalogue right now, counted rather than sampled.

    Every field is a count over the whole catalogue, because a merchant asking "how is my
    catalogue" is asking about all of it, and answering from a page is how a console comes
    to report that a category is empty when it is merely off the end of the first request.
    """

    total: int
    listed: int
    delisted: int
    available: int
    out_of_stock: int
    by_category: Mapping[str, int]
    catalogue_revision: int


@dataclass(frozen=True, slots=True)
class InventoryAnomaly:
    """One product whose state a merchant would probably want to know about.

    ``kind`` is a closed vocabulary rather than a sentence, so the console decides how to
    phrase it and the agent cannot invent a new category of problem. An anomaly is an
    observation, never a recommendation: what to do about it is the merchant's call, and
    a growth proposal is a separate, staged thing a human applies.
    """

    sku: str
    name: str
    kind: str
    detail: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CheckoutMetrics:
    """Counts over checkouts and orders, each one a figure the platform can derive.

    Money is integer minor units and every amount here was summed by the database over
    committed rows. A metric this platform cannot derive is absent rather than zero:
    ``None`` says "not measured" and ``0`` says "none", and a merchant reading a
    dashboard is entitled to the difference.
    """

    orders_total: int
    orders_by_state: Mapping[str, int]
    refunds_by_state: Mapping[str, int]
    captured_minor: int | None
    refunded_minor: int | None
    currency: str


class MerchantBackend(ABC):
    """Registry A's merchant-side reads, kept apart from the buyer surface deliberately.

    A separate protocol rather than more methods on :class:`CommerceBackend`, because the
    two surfaces answer to different people. A backend built for a buyer session has no
    business being able to read catalogue health across the merchant, and requiring it to
    implement those methods -- even to raise -- would put the capability within reach of a
    principal that must never hold it.

    The tool factory checks for this protocol and simply does not build the merchant tools
    against a backend that lacks it. That surfaces through the existing ``unbuilt``
    reporting, which says plainly that a roster row has no closure rather than offering a
    tool that fails when called.
    """

    @abstractmethod
    async def catalogue_health(self) -> CatalogueHealth:
        """How many products this merchant lists, stocks and has run out of."""

    @abstractmethod
    async def inventory_anomalies(self, limit: int = 20) -> tuple[InventoryAnomaly, ...]:
        """Products worth a merchant's attention: out of stock, delisted with stock, and so on."""

    @abstractmethod
    async def checkout_metrics(self) -> CheckoutMetrics:
        """Counts over checkouts, orders and refunds, derived from committed rows only."""


# ------------------------------------------------------------------------ review queue


class CaseState(StrEnum):
    """Specification 6.4.3's case states. Mirrors ``human_review_service.CaseState``.

    Declared here rather than imported for the reason :class:`OrderState` is (ADR 0003
    D2): ``commerce-api`` sits above this package, and a case must be readable from the
    in-memory backend with no HTTP layer present at all. Every P0 case is
    ``AWAITING_HUMAN`` -- nothing in the product advances one -- and the other three are
    the vocabulary a later operator increment will move a case through.
    """

    OPEN = "OPEN"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class CasePriority(StrEnum):
    """How a case is ordered for a reviewer. Exactly three, and there is no fourth.

    An enum rather than a string because priority is *derived* -- from how little the
    platform can say about money that may have moved -- and these three are the whole of
    what it can derive. A surface rendering a fourth is showing a reviewer a triage level
    nobody assigned, which on a review queue is worse than showing none: it invents an
    ordering. So a value outside this set arriving from a backend is a contract violation
    rather than a case with an unusual priority.
    """

    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


@dataclass(frozen=True, slots=True)
class CaseEvent:
    """One row of a case's timeline, redacted by the service that owns the audit stream.

    ``detail`` is carried through rather than composed here. agent-runtime fences
    merchant- and buyer-authored strings on the way to a model, but fencing is not
    redaction: it marks text as data so the model does not read it as an instruction, and
    it cannot un-leak a field the writer chose to include. Redaction stays with the writer.
    """

    at: datetime
    event: str
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CaseSummary:
    """One case as the queue lists it: enough to choose one, never enough to explain one.

    No timeline and no proof-chain reference, on purpose. A reviewer's question is
    answered from :class:`CaseRecord`; a listing that carried most of a case would invite
    an agent to answer out of the list and never open the case it was talking about.
    """

    case_key: str
    reason_code: RecoveryCode
    state: CaseState
    priority: CasePriority
    opened_at: datetime
    target_response_by: datetime
    monetary_exposure_minor: int | None
    currency: str


@dataclass(frozen=True, slots=True)
class CaseRecord:
    """One escalated case with what specification 6.4.3 promises a reviewer, and no more.

    Three fields carry a distinction that is easy to flatten and expensive to flatten:

    ``provider_state_at_escalation``
        What was verified when the case opened, never re-read now. ``None`` means the
        provider was never reached, which is a different fact from the provider saying
        ``UNKNOWN`` and must stay different: one is silence, the other is an answer.

    ``monetary_exposure_minor``
        Integer minor units, and ``None`` where the escalating path recorded no amount.
        Absent is not zero here either -- "we did not record what was at risk" and
        "nothing was at risk" are different things to tell somebody triaging a queue.

    ``proof_chain_ref``
        A reference to the Money Action Proof Chain, never a copy of it. The chain is
        verified by recomputation at read time, and a copy would be a snapshot that could
        disagree with the verifier the next time anybody ran it.

    ``scope_note`` and ``resolvable_here`` travel with the data rather than living in a
    document, so a surface states the limit in words instead of implying a capability by
    the absence of a button.
    """

    case_key: str
    reason_code: RecoveryCode
    state: CaseState
    priority: CasePriority
    provider_state_at_escalation: str | None
    proof_chain_ref: str | None
    monetary_exposure_minor: int | None
    currency: str
    opened_at: datetime
    target_response_by: datetime
    timeline: tuple[CaseEvent, ...] = ()
    scope_note: str = ""
    resolvable_here: bool = False

    def __post_init__(self) -> None:
        if self.resolvable_here:
            # P0 has no assign, no decision, no note and no resolve anywhere -- not in the
            # service, not on the router, and no capability in Registry A for one. A record
            # claiming otherwise would put a control on a card that settles nothing, and
            # the first click would prove it. When a resolution workflow does exist, this
            # line is where the change is argued rather than a default somebody flipped.
            raise ValueError("no surface in this release resolves a case; see SCOPE_NOTE")

    def summary(self) -> CaseSummary:
        """This case as the queue would list it. The listing is a projection, never a copy."""
        return CaseSummary(
            case_key=self.case_key,
            reason_code=self.reason_code,
            state=self.state,
            priority=self.priority,
            opened_at=self.opened_at,
            target_response_by=self.target_response_by,
            monetary_exposure_minor=self.monetary_exposure_minor,
            currency=self.currency,
        )


class CaseBackend(ABC):
    """The human-review queue, kept apart from both the buyer and the merchant surfaces.

    A third protocol rather than more methods on either of the others, for the reason
    :class:`MerchantBackend` is a second one: a case carries the money at risk on somebody
    else's stuck payment and the provider's statement about it, so a backend built for a
    buyer session must not be able to read one. Requiring it to implement these -- even to
    raise -- would put the capability within reach of a principal that must never hold it.

    Tenant scoping is the backend's, taken from the authenticated session, and never a
    tool argument. ``case_key`` reaches a tool the way ``order_id`` reaches ``order_track``:
    the model names a *subject*, never a principal. A key belonging to another tenant is a
    404 problem rather than an empty record, because an empty record still answers "does
    this case exist somewhere else", and a case key is a hash somebody could probe.

    Read-only, and there is no resolve, assign or annotate left off it: P0's queue has none
    of those (specification 6.4.3), so there is nothing here for a capability gate to miss.
    """

    @abstractmethod
    async def support_cases(self, limit: int = 20) -> tuple[CaseSummary, ...]:
        """GET /v1/review/queue: this tenant's cases, most recently opened first."""

    @abstractmethod
    async def support_case(self, case_key: str) -> CaseRecord:
        """GET /v1/review/queue/{key}: one case with its evidence. An unknown key is a problem."""
