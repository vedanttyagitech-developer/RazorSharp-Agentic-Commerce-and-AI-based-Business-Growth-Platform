"""The only commerce operations an agent may call.

:class:`CommerceBackend` is the whole of an agent's reach into the platform. It exposes
grounded reads (search, product, cart re-quote, checkout view), cart construction,
checkout creation and one money-adjacent operation: submitting an *already approved*
checkout version to the Transaction Trust Kernel for admission.

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
it never computes them. :class:`CartQuote` re-checks its own total at construction so a
backend that returns a total contradicting its components is refused rather than repeated
to the buyer.

Errors are RFC 9457 problem details (ADR 0003 D15) wrapped in :class:`BackendError`. A
kernel *denial* is not an error: it arrives as a structured
:class:`commerce_domain.AdmissionDecision` with ``allowed=False``, because a denial is the
system working.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

# Imported rather than mirrored: the kernel sits *below* this package, so naming its enum
# is not the layering inversion that ``CaseState`` and ``RemedyOutcome`` avoid. The set of
# policy families a sale can be governed by has one definition, and a receipt is required
# to record every member -- which is what makes an absent rule impossible to mistake for a
# permissive one, and would stop being true the moment a second copy of the enum drifted.
from commerce_domain import AdmissionDecision, Money, PolicyKind, RecoveryCode
from merchant_sim import Locale

__all__ = [
    "AGENT_OPERATIONS",
    "NEVER_ON_AGENT_SURFACE",
    "ApprovalCard",
    "BackendError",
    "CartQuote",
    "CartView",
    "CheckoutStatus",
    "CheckoutView",
    "CommerceBackend",
    "OrderResolution",
    "OrderState",
    "OrderView",
    "PaymentSummary",
    "PolicyAtSale",
    "PolicyKind",
    "PolicyTerm",
    "PricedLine",
    "Problem",
    "ProductCard",
    "Provenance",
    "RefundRecord",
    "RemedyConfirmation",
    "RemedyOption",
    "RemedyOutcome",
    "ResolutionPlan",
    "SearchPage",
    "SupportBackend",
    "UnavailableLine",
    "WithheldReason",
    "WithheldRemedy",
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
    """One priced cart line, exactly as the fee engine computed it."""

    sku: str
    name: str
    quantity: int
    unit_price: Money
    subtotal: Money
    tax_bp: int
    tax: Money


@dataclass(frozen=True, slots=True)
class CartQuote:
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
    #: What a merchant offer took off. Optional so every quote written before offers
    #: existed still constructs; absent means zero, not unknown.
    discount: Money | None = None
    #: The offer's buyer-visible name. The agent may repeat it; it may not invent one.
    offer_label: str | None = None

    @property
    def discount_amount(self) -> Money:
        """The discount as an amount, zero when no offer applied."""
        return self.discount if self.discount is not None else Money.zero(self.currency)

    def __post_init__(self) -> None:
        if not self.lines:
            raise ValueError("a quote prices at least one line")
        payable = self.items_subtotal + self.items_tax + self.delivery_fee + self.delivery_tax
        recomputed = payable - self.discount_amount
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
            # Without this the saving is a number the agent may not say out loud: the
            # grounding postcheck refuses any figure that is not in this ledger, so an
            # offer the buyer can see on the card would be unspeakable in the transcript.
            self.discount_amount,
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
class CartView:
    """A cart and its current quote, or the structured reason it has none.

    ``code`` is ``OK`` for an empty cart and for a priced one; a cart with lines the
    merchant cannot fulfil carries ``STALE_CHECKOUT`` and names them in ``unavailable``.
    """

    cart_id: str
    code: RecoveryCode
    lines: tuple[tuple[str, int], ...]
    quote: CartQuote | None
    unavailable: tuple[UnavailableLine, ...]
    stale: bool
    provenance: Provenance

    def __post_init__(self) -> None:
        if self.code is RecoveryCode.OK and self.lines and self.quote is None:
            raise ValueError("a non-empty OK cart must carry a quote")
        if self.code is not RecoveryCode.OK and not self.unavailable:
            raise ValueError("a refused cart must name the lines it could not price")

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
    quote: CartQuote
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
    #: The order as a person says it: ``RS-260908-K7M4QX2``.
    #:
    #: Carried beside the id because an agent that holds only the id can only speak the
    #: id, and a copilot reading a UUID back to a buyer who asked "where is my order" has
    #: answered in the one form of the identifier nobody can use. Derived by the platform
    #: and handed over, never computed here: a second author of an order's name is a
    #: second name.
    reference: str
    checkout_id: str
    version: int
    content_hash: str
    state: OrderState
    amount: Money
    payment: PaymentSummary
    refunds: tuple[RefundRecord, ...] = ()
    quote: CartQuote | None = None
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
        "basket_propose_line",
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
    async def basket_create(self) -> CartView:
        """POST /v1/carts: a new, empty cart."""

    @abstractmethod
    async def basket_set_line(self, cart_id: str, sku: str, quantity: int) -> CartView:
        """PUT /v1/carts/{id}/lines/{sku}: set a quantity (0 removes); returns the quote."""

    @abstractmethod
    async def basket_get(self, cart_id: str) -> CartView:
        """GET /v1/carts/{id}: re-quote and report staleness."""

    async def basket_propose_line(
        self, cart_id: str | None, sku: str, delta: int
    ) -> Mapping[str, Any]:
        """Stage a line proposal the buyer's instruction turns into a cart write.

        The record is the one ``commerce_api.services.agent_service`` renders as the line
        proposal card: the product's price and shelf count, the cart's current quantity,
        the absolute quantity the write will send, and a binding (unit price, catalogue
        revision, cart content hash) the route re-checks under the cart's lock. It
        changes nothing itself, which is why it is not a write: the panel performs the
        add the buyer asked for, and this only describes that add precisely. A backend
        with no buyer surface answers with a problem rather than a guess, which is what
        this default does.
        """
        raise backend_problem(
            "no_line_proposals",
            status=501,
            title="No line proposals",
            detail=(
                "This backend cannot stage a cart line proposal. The buyer surface that "
                "reads the product and re-quotes the cart is not attached here."
            ),
            operation="basket_propose_line",
            cart_id=cart_id,
            sku=sku,
            delta=delta,
        )

    @abstractmethod
    async def checkout_create(self, cart_id: str) -> ApprovalCard:
        """POST /v1/carts/{id}/checkout: version 1 plus receipt and reservation."""

    @abstractmethod
    async def checkout_get(self, checkout_id: str) -> CheckoutView:
        """GET /v1/checkouts/{id}: head, versions, approval card, attempt summary."""

    @abstractmethod
    async def checkout_submit_approved(
        self, checkout_id: str, version: int, content_hash: str
    ) -> AdmissionDecision:
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


# --------------------------------------------------------------------- support surface


class RemedyOutcome(StrEnum):
    """The remedies the Resolution Service can name. Closed; mirrors ``Outcome``.

    Declared here rather than imported for the reason :class:`CaseState` is: the module
    that owns this vocabulary (``commerce_api.services.resolution_service``) sits *above*
    this package, and a remedy must be readable from the in-memory backend with no HTTP
    layer present. :class:`PolicyKind` is imported instead of mirrored because the kernel
    sits *below* this package, so there is no inversion to avoid and one definition of the
    closed set is better than two.
    """

    REFUND_FULL = "REFUND_FULL"
    REFUND_PARTIAL = "REFUND_PARTIAL"
    ORDER_CANCEL = "ORDER_CANCEL"
    STORE_CREDIT = "STORE_CREDIT"


class RemedyConfirmation(StrEnum):
    """Who must confirm before the kernel may admit a remedy. Never the agent, either way.

    This distinguishes "the buyer decides on the trusted surface" from "an operator decides
    outside this surface entirely". Both are somebody else: there is no third member for
    the agent, and its absence is what makes "the agent never confirms a remedy" structural
    rather than a sentence in a prompt.
    """

    BUYER_APPROVAL = "BUYER_APPROVAL"
    OPERATOR_APPROVAL = "OPERATOR_APPROVAL"


class WithheldReason(StrEnum):
    """Why a remedy was considered and not offered. Closed, never free text.

    Carried because an empty options list with no reasons tells a buyer that nothing was
    considered, which is a different and worse answer than "these were considered, and here
    is what stopped each". The Support Specialist reads the reason and says it; it does not
    compose one.
    """

    ESCALATED_TO_HUMAN = "ESCALATED_TO_HUMAN"
    PROVIDER_STATE_UNVERIFIED = "PROVIDER_STATE_UNVERIFIED"
    NOT_RECORDED_AT_SALE = "NOT_RECORDED_AT_SALE"
    REQUIRES_A_REQUESTED_AMOUNT = "REQUIRES_A_REQUESTED_AMOUNT"
    NOTHING_REFUNDABLE = "NOTHING_REFUNDABLE"
    POLICY_FORBIDS = "POLICY_FORBIDS"
    MONEY_ALREADY_CAPTURED = "MONEY_ALREADY_CAPTURED"


@dataclass(frozen=True, slots=True)
class PolicyTerm:
    """One merchant rule exactly as the Policy-at-Sale Receipt froze it.

    ``terms`` is the receipt's own mapping, carried through and never summarised here. The
    agent quotes a term and cites ``policy_id`` and ``policy_version`` beside it, so a
    dispute can be argued against the document the buyer was actually shown; a sentence
    composed at this layer would be a second author of the rule with no version of its own.
    """

    kind: PolicyKind
    policy_id: str
    policy_version: int
    terms: Mapping[str, Any]
    applies_to: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PolicyAtSale:
    """The rules one sale was made under, and whether they can be relied on.

    ``binding_ok`` is the answer and not a status beside it. The platform refuses to hand
    back terms when the checkout/receipt binding does not re-derive from stored rows, so a
    broken binding arrives here with no policies at all and the :class:`RecoveryCode` that
    says which way it broke. An agent that read an empty list as "no rules apply" would be
    reading a verification failure as permission, so the two are kept apart structurally.
    """

    order_id: str
    binding_ok: bool
    binding_code: RecoveryCode
    receipt_hash: str | None
    policies: tuple[PolicyTerm, ...] = ()

    def __post_init__(self) -> None:
        if self.binding_ok and not self.policies:
            raise ValueError(
                "a verified Policy-at-Sale Receipt records every policy kind; a binding "
                "reported OK with no terms behind it is a backend contradicting itself"
            )
        if not self.binding_ok and self.policies:
            raise ValueError(
                "terms arrived with a binding that did not verify; a receipt that may have "
                "been edited must govern nothing, which is why the platform returns none"
            )

    def term(self, kind: PolicyKind) -> PolicyTerm | None:
        """The recorded rule of one kind, or ``None`` where the receipt records none.

        Raises on an unverified binding. "This sale has no substitution programme" and
        "nobody can say what this sale's rules were" are different answers, and an agent
        handed ``None`` for both would tell a buyer the first when the truth is the second.
        """
        if not self.binding_ok:
            raise ValueError(
                f"no at-sale policy is available for {self.order_id}: {self.binding_code}"
            )
        for policy in self.policies:
            if policy.kind is kind:
                return policy
        return None


@dataclass(frozen=True, slots=True)
class RemedyOption:
    """One remedy, its exact amount, and the at-sale rule that permits it."""

    outcome: RemedyOutcome
    amount: Money
    policy_kind: PolicyKind
    policy_id: str
    policy_version: int
    confirmation: RemedyConfirmation
    basis: str


@dataclass(frozen=True, slots=True)
class WithheldRemedy:
    """One remedy considered and not offered, with the closed reason it was not."""

    outcome: RemedyOutcome
    reason: WithheldReason
    detail: str


@dataclass(frozen=True, slots=True)
class ResolutionPlan:
    """What would settle one finding: a code always, options only where a plan was issued.

    The guards below duplicate ``resolution_service.Resolution.__post_init__`` on purpose,
    for the reason :class:`CartQuote` re-checks its own total: an HTTP backend hands us
    figures we did not compute, and the agent must not repeat an amount the ledger beside
    it does not support. A backend offering more than ``captured - already refunded or
    pending`` is refused here rather than quoted to a buyer, and a code that issues no plan
    may not arrive carrying one.

    ``recorded`` is false everywhere in P0: no ``resolution_plans`` row exists, so nothing
    can be confirmed against ``plan_id``. It is a field rather than a docstring sentence
    because a surface that assumed otherwise would be wrong in the direction of money.
    """

    finding_id: str
    code: RecoveryCode
    plan_id: str | None
    options: tuple[RemedyOption, ...]
    withheld: tuple[WithheldRemedy, ...]
    captured_minor: int
    refunds_reserved_minor: int
    refundable_minor: int
    currency: str
    explanation: str
    valid_until: datetime | None = None
    recorded: bool = False

    def __post_init__(self) -> None:
        if self.code is RecoveryCode.RESOLUTION_PLAN_ISSUED:
            if not self.options or self.plan_id is None:
                raise ValueError(
                    "RESOLUTION_PLAN_ISSUED must carry a plan id and at least one option; "
                    "a code saying a plan exists over a body with none is the one shape an "
                    "agent cannot describe without inventing something"
                )
        elif self.options or self.plan_id is not None:
            raise ValueError(
                f"{self.code} issues no plan, so it may carry no options and no plan id"
            )
        for option in self.options:
            if option.amount.currency != self.currency:
                raise ValueError(
                    f"option {option.outcome} is in {option.amount.currency} but the "
                    f"capture is in {self.currency}"
                )
            if option.amount.minor > self.refundable_minor:
                raise ValueError(
                    f"option {option.outcome} would return {option.amount.minor} of "
                    f"{self.refundable_minor} refundable minor units; the agent may not "
                    "present an amount the capture ledger does not support"
                )

    @property
    def plan_issued(self) -> bool:
        return self.code is RecoveryCode.RESOLUTION_PLAN_ISSUED

    def amounts(self) -> tuple[Money, ...]:
        """Every money fact in this plan, for the grounding ledger.

        The option amounts and the three ledger figures, so a reply naming any of them is
        naming a number this turn actually read. Nothing derived: the agent performs no
        arithmetic on money, so there is nothing else here that could be grounded.
        """
        facts: list[Money] = [option.amount for option in self.options]
        facts.extend(
            Money(minor, self.currency)
            for minor in (self.captured_minor, self.refunds_reserved_minor, self.refundable_minor)
        )
        return tuple(facts)


@dataclass(frozen=True, slots=True)
class OrderResolution:
    """Every finding on one order, and the plan that would settle each.

    ``findings`` is reported alongside ``plans`` rather than left to be counted, because
    zero has to be readable as a *measurement*: the Reconciliation Service looked at this
    order and found nothing diverging. An empty list with no count could equally mean no
    evaluation happened, and a Support Specialist that could not tell those apart would
    either invent a remedy or refuse a real one.

    ``plan_ttl_seconds`` is ``None`` and not ``0`` where the backend reported no window.
    Zero seconds reads as "this expired the instant you read it", which is a claim about a
    plan; ``None`` is the absence of a claim, and a backend that issued no plan has made
    none. A backend that did issue one must report the window, so an agent can never quote
    an amount with no idea how long it stands.
    """

    order_id: str
    recorded_state: str
    findings: int
    plans: tuple[ResolutionPlan, ...] = ()
    plan_ttl_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.findings != len(self.plans):
            raise ValueError(
                f"{self.findings} findings reported with {len(self.plans)} plans; one "
                "resolution is issued per finding, and a count disagreeing with the list "
                "would let a surface claim more or less was examined than was"
            )
        if self.plan_ttl_seconds is None and any(plan.plan_issued for plan in self.plans):
            raise ValueError(
                "a plan was issued with no validity window reported; every figure in a "
                "plan is a function of provider state a webhook can move in the next "
                "second, so an amount with no expiry is an amount nobody may rely on"
            )


@dataclass(frozen=True, slots=True)
class SupportCase:
    """A case on the merchant's queue, as the agent is told about it.

    ``case_id`` is the whole answer. There is deliberately no amount, no eligibility and
    no promise beside it, because none of those has been decided: a person on the
    merchant's side reads the case and settles what the buyer is owed.
    """

    case_id: str
    order_id: str
    reason: str
    status: str


class SupportBackend(ABC):
    """The two post-purchase reads the Support Specialist needs before it may quote.

    A second protocol rather than more methods on :class:`CommerceBackend`: an at-sale
    receipt and a resolution plan are the two things on this platform that decide what a
    buyer is *owed*, and a backend built for the shopping surface must not be able to
    reach them. A backend that does not implement
    these leaves the rows in ``unbuilt`` rather than being handed a closure that would have
    to invent a rule or an amount.

    Both take an ``order_id`` and nothing else. That is the narrowing ``order_track`` uses:
    the model names a *subject* it was grounded on, never a principal and never a
    ``payment_attempt_id`` -- which a buyer surface does not hold, and which would let a
    support agent name somebody else's stuck payment. Tenant and ownership scoping are the
    backend's, taken from the authenticated session; an order belonging to someone else is
    the same problem an unknown one gives, because a distinct refusal is an existence
    oracle over identifiers.

    Two reads and one write, and the write is deliberately not on the money path. This
    docstring used to say there was no escalate at all, and the reason it gave was sound
    for the escalation it had in mind: opening a *human-review case* freezes a payment
    attempt on a terminal transition, which is a write on money and belongs behind its own
    gate. :meth:`open_support_case` is not that. It puts a row on the merchant's support
    queue naming an order and a reason, touches no payment attempt, no ledger and no
    financial table, and decides nothing about what the buyer is owed.
    """

    @abstractmethod
    async def order_policy(self, order_id: str) -> PolicyAtSale:
        """GET /v1/orders/{id}/policy: the rules this sale was made under, never today's."""

    @abstractmethod
    async def order_resolution(self, order_id: str) -> OrderResolution:
        """GET /v1/orders/{id}/resolution: every finding on this order and what settles it."""

    @abstractmethod
    async def open_support_case(self, order_id: str, reason: str, note: str) -> SupportCase:
        """POST /v1/orders/{id}/support-cases: raise it for a person, and decide nothing.

        The only write the support surface has. It returns a ``case_id`` and nothing that
        resembles an outcome, because the outcome is a person's to reach.
        """
