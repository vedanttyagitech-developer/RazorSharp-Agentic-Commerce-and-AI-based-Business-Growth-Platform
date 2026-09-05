"""The closed tool registry, specification 17.2 and 17.3.

Specification 17.3 forbids a *design*, not a behaviour. A tool that can approve, pay,
refund or revoke must not exist -- not be guarded by a capability check, not be hidden
behind a feature flag, not be reachable only by an authorised client. Absence by
construction, because a guard is a thing that can be forgotten, misconfigured or
bypassed by whatever bug ships next, and an absent method cannot be called by anybody.

The model is ``agent_runtime.backends.base.CommerceBackend``, which has no ``approve``
method at all. This module does the same job for a protocol surface, where the enumeration
has to be data rather than an abstract class because the wire protocol names tools by
string. So :class:`ToolName` is a closed ``StrEnum`` and :data:`TOOLS` is a read-only
mapping over exactly its members. A name that arrives on the wire is turned into an enum
member or refused; it is never used as a lookup key into anything wider, never passed to
``getattr``, and never forwarded to an HTTP client. There is no generic dispatcher here
because there is nothing generic left to dispatch to.

Three properties are asserted at import time rather than in a test, because a package that
will not import cannot be deployed, whereas a red test can be skipped under deadline
pressure:

**The registry is exactly the enum.** Every member has a spec, every spec is its own key,
and :data:`FORBIDDEN_TOOL_NAMES` -- the specification 17.3 list, written out so a reviewer
can read the absence rather than infer it -- is disjoint from it.

**No tool can name an amount.** :class:`ArgumentKind` has no monetary member and no tool
declares an argument whose name is an amount, a total or a currency. This is what makes
``PROPOSE_CANCELLATION`` and ``PROPOSE_REFUND`` honest: specification 29.4 says a proposal
names no amount, and a proposal that *could* name one is a refund tool wearing a hat. The
same rule stops a model naming the tenant, the merchant, its own capabilities or a bearer
credential, which is specification 17.3's fourth and fifth bullets.

**Exactly one tool reaches the kernel.** ``checkout.submit_approved`` maps to
``IntentKind.SUBMIT_APPROVED``, the only intent for which ``ProtocolIntent.moves_money`` is
true, and it still cannot move money by itself: it hands an already-approved version to the
same admission every other surface uses, and admission decides.

Every capability in the table below is drawn from ``core.identity.PROTOCOL_CAPABILITIES``,
which contains no consent capability and never will. Where the honest capability for a tool
does not exist in that ceiling -- there is no ``support.escalate`` in it -- the tool takes
the narrower entitlement it genuinely needs rather than the core growing a member for the
convenience of this adapter.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

from ..core import PROTOCOL_CAPABILITIES, IntentKind, SchemaRejected

__all__ = [
    "FORBIDDEN_ARGUMENT_NAMES",
    "FORBIDDEN_TOOL_NAMES",
    "MAX_ARGUMENTS_PER_CALL",
    "NEVER_ON_MCP_SURFACE",
    "TOOLS",
    "ArgumentKind",
    "ArgumentSpec",
    "NormalisedArguments",
    "ToolName",
    "ToolSpec",
    "public_name_offends",
    "resolve_tool",
]

#: How many raw keys a single call may carry before it is refused unread. A model that
#: sends thirty-two arguments to a three-argument tool is not being helpful, and walking an
#: unbounded object to decide which fields to ignore is work an attacker gets to choose.
MAX_ARGUMENTS_PER_CALL: Final[int] = 32

#: The specification 17.3 prohibitions, spelled out. Nothing reads this at runtime except
#: the import-time assertion below; it exists so the absence is legible. A reviewer opening
#: this file sees the dangerous tools named and proved absent, rather than having to
#: convince themselves that a list of thirteen allowed tools is complete.
FORBIDDEN_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "approval.record",
        "authority.revoke",
        "checkout.approve",
        "checkout.cancel",
        "checkout.reject",
        "credentials.read",
        "execution_grant.consume",
        "execution_grant.issue",
        "http.request",
        "keys.read",
        "payment.capture",
        "payment.create_order",
        "payment.execute",
        "payment.reconcile",
        "payment.verify",
        "payout.create",
        "razorpay.orders.create",
        "razorpay.payments.capture",
        "razorpay.request",
        "reconciliation.run",
        "refund.create",
        "refund.execute",
        "refund.issue",
        "sql.query",
        "webhook.apply",
        "webhook.replay",
    }
)

#: Verbs that may never appear as a segment of a public name anywhere in this package --
#: not a tool, not a method, not a module attribute. Imperatives only: ``submit_approved``
#: legitimately contains the past participle "approved", because submitting a version a
#: human already approved is specification 17.2's sixth bullet and is precisely the thing
#: this architecture exists to make safe. "approve" is the act; "approved" is a fact about
#: something that already happened on the trusted surface.
NEVER_ON_MCP_SURFACE: Final[frozenset[str]] = frozenset(
    {
        "approve",
        "capture",
        "charge",
        "disburse",
        "execute",
        "payout",
        "reconcile",
        "revoke",
        "settle",
        "transfer",
        "void",
        "withdraw",
    }
)

#: Argument names no tool may declare. The first group is authority a model must never be
#: able to state: the tenant comes from the authenticated session (17.3, fourth bullet),
#: the capabilities come from the token's scopes, and a credential in an argument would be
#: token passthrough (fifth bullet). The second group is money, which no MCP tool names at
#: all -- not the proposals, and not the submit, whose amount comes from the locked
#: checkout version the buyer approved.
FORBIDDEN_ARGUMENT_NAMES: Final[frozenset[str]] = frozenset(
    {
        "access_token",
        "actor_type",
        "amount",
        "amount_minor",
        "api_key",
        "authorization",
        "buyer_ref",
        "capabilities",
        "client_id",
        "credential",
        "currency",
        "grant_id",
        "merchant",
        "merchant_id",
        "minor",
        "price",
        "principal",
        "principal_id",
        "refund_amount",
        "scope",
        "scopes",
        "secret",
        "subtotal",
        "tenant",
        "tenant_id",
        "token",
        "total",
        "total_minor",
    }
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HASH = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_SEGMENTS = re.compile(r"[^a-z0-9]+")

#: A camel hump. Split before lowercasing so ``refundExecute`` reads as two segments and is
#: caught by the same rule that catches ``refund_execute``; Python names here are snake_case,
#: but the screen also walks names that arrived from somewhere else.
_CAMEL_HUMP = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


class ToolName(StrEnum):
    """Every tool this server exposes. Specification 17.2, one member per allowed thing.

    Closed, and the closure is the security property. Adding a member is a decision about
    what an external model may ask this platform to do, which is exactly the kind of
    decision that should require an edit here, a mapping to an ``IntentKind`` that already
    exists, and a review -- rather than falling out of a handler someone registered.
    """

    #: Grounded catalogue search over the merchant's live listings.
    CATALOGUE_SEARCH = "catalogue.search"
    #: One product by SKU, with the availability the merchant reports right now.
    CATALOGUE_PRODUCT = "catalogue.product"
    #: Availability for a specific quantity of a specific SKU.
    INVENTORY_CHECK = "inventory.check"
    #: A new, empty basket. No money, no authority, fully reversible.
    BASKET_CREATE = "basket.create"
    #: Set one line's quantity. Zero removes it.
    BASKET_UPDATE = "basket.update"
    #: Re-price a basket and report whether the quote has gone stale.
    QUOTE_REQUEST = "quote.request"
    #: Turn a basket into an immutable, hashed checkout version holding a reservation.
    RESERVATION_REQUEST = "reservation.request"
    #: Ask the buyer to decide, on the trusted surface, about a version. Never approves.
    CHECKOUT_SUBMIT_FOR_APPROVAL = "checkout.submit_for_approval"
    #: Hand a version the buyer already approved to kernel admission. The only tool that
    #: reaches money, and admission -- not this tool -- decides whether it moves.
    CHECKOUT_SUBMIT_APPROVED = "checkout.submit_approved"
    #: Read an order's state and the evidence by which the platform learned it.
    ORDER_TRACK = "order.track"
    #: Ask a human to consider cancelling. Names no amount.
    ORDER_PROPOSE_CANCELLATION = "order.propose_cancellation"
    #: Ask a human to consider a refund. Names no amount, for the same reason.
    REFUND_PROPOSE = "refund.propose"
    #: Hand the conversation to a person, creating at most one support case.
    SUPPORT_ESCALATE = "support.escalate"


class ArgumentKind(StrEnum):
    """The complete vocabulary of what a model may put in a tool argument.

    There is deliberately no monetary member, and its absence is asserted at import. A kind
    the enum does not contain is a value the wire cannot express, so "an MCP tool may not
    name an amount" is enforced by the type system rather than by every future reviewer of
    every future tool.
    """

    #: Free text the model composed. Untrusted, length-bounded, never parsed for meaning.
    TEXT = "TEXT"
    #: An opaque platform identifier: a SKU, a basket id. Pattern-bounded.
    IDENTIFIER = "IDENTIFIER"
    #: A canonical UUID naming a row this platform owns.
    UUID = "UUID"
    #: A non-negative integer count: a quantity, a page size, a checkout version.
    COUNT = "COUNT"
    #: A base64url content hash the caller is echoing back, never one it invented.
    HASH = "HASH"


#: How each kind is published to the model in the tool's JSON schema.
_JSON_TYPE: Final[Mapping[ArgumentKind, str]] = MappingProxyType(
    {
        ArgumentKind.TEXT: "string",
        ArgumentKind.IDENTIFIER: "string",
        ArgumentKind.UUID: "string",
        ArgumentKind.COUNT: "integer",
        ArgumentKind.HASH: "string",
    }
)


@dataclass(frozen=True, slots=True)
class ArgumentSpec:
    """One argument a tool accepts, and the bounds it is validated against.

    ``max_length`` and the count bounds are not politeness. An argument is the one part of
    a tool call a model composes freely, so it is the one part an injected instruction
    arrives in, and an unbounded string is an unbounded amount of work for whatever reads
    it next.
    """

    kind: ArgumentKind
    summary: str
    required: bool = True
    max_length: int = 200
    min_value: int = 0
    max_value: int = 1000


@dataclass(frozen=True, slots=True)
class NormalisedArguments:
    """What survived validation, and what was dropped on the way.

    ``ignored`` is kept and recorded rather than discarded because specification 17.3 says
    a model-supplied tenant id must be *ignored*, and an ignored field that leaves no trace
    is indistinguishable from one that was never sent. A client repeatedly asserting a
    tenant is either broken or probing, and that is only visible if the drop is evidence.
    """

    accepted: Mapping[str, str | int]
    ignored: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One row of the specification 17.2 table.

    ``capability`` is drawn from ``core.identity.PROTOCOL_CAPABILITIES`` and validated
    against it here, so a tool cannot be written that requires an entitlement an external
    protocol caller is not allowed to hold. ``intent`` is a member of the core's closed
    ``IntentKind``, which has no ``PAY``, ``REFUND``, ``APPROVE`` or ``REVOKE`` -- so the
    strongest thing any row of this table can express is bounded by a type this package
    does not own and may not extend.
    """

    name: ToolName
    intent: IntentKind
    capability: str
    summary: str
    arguments: Mapping[str, ArgumentSpec] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, ToolName):
            raise ValueError(f"{self.name!r} is not a member of the closed tool enum")
        if not isinstance(self.intent, IntentKind):
            raise ValueError(f"{self.intent!r} is not a member of the core intent vocabulary")
        if self.capability not in PROTOCOL_CAPABILITIES:
            raise ValueError(
                f"tool {self.name} requires {self.capability!r}, which is not in the "
                "protocol capability ceiling; an external caller may never hold it"
            )
        offending = sorted(set(self.arguments) & FORBIDDEN_ARGUMENT_NAMES)
        if offending:
            raise ValueError(
                f"tool {self.name} declares arguments a model may never state: {offending}"
            )
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))

    @property
    def reaches_kernel(self) -> bool:
        """True only for the submit of an already-approved version. See :data:`TOOLS`."""
        return self.intent is IntentKind.SUBMIT_APPROVED

    def to_schema(self) -> dict[str, Any]:
        """The MCP ``tools/list`` entry for this tool.

        ``additionalProperties`` is false, which is the published half of the rule the
        server enforces anyway: the schema a model reads states that a tenant id is not an
        argument, and :meth:`normalise` makes sure that stating one changes nothing.
        """
        return {
            "name": self.name.value,
            "description": self.summary,
            "inputSchema": {
                "type": "object",
                "properties": {
                    field_name: {"type": _JSON_TYPE[spec.kind], "description": spec.summary}
                    for field_name, spec in self.arguments.items()
                },
                "required": sorted(n for n, s in self.arguments.items() if s.required),
                "additionalProperties": False,
            },
        }

    def normalise(self, raw: Mapping[str, Any]) -> NormalisedArguments:
        """Validate the declared arguments and drop everything else.

        Unknown fields are ignored uniformly rather than refused, and the uniformity is the
        point. Specification 17.3 requires that a model-supplied tenant id be ignored; a
        normaliser that refused unknown fields would need one special case for ``tenant_id``
        and would then have to be right about every other spelling of authority somebody
        might try. Dropping everything undeclared needs no list to be complete.

        A *declared* argument that is malformed is refused rather than dropped, because a
        caller that meant to name a SKU and named something else has asked a question this
        platform cannot answer, and silently answering a different one is worse.
        """
        if len(raw) > MAX_ARGUMENTS_PER_CALL:
            raise SchemaRejected(
                "too_many_arguments",
                tool=self.name.value,
                count=len(raw),
                maximum=MAX_ARGUMENTS_PER_CALL,
            )
        accepted: dict[str, str | int] = {}
        for field_name, spec in self.arguments.items():
            if field_name not in raw:
                if spec.required:
                    raise SchemaRejected(
                        "argument_missing", tool=self.name.value, argument=field_name
                    )
                continue
            accepted[field_name] = _validate(self.name, field_name, spec, raw[field_name])
        # The dropped names are stringified before they are sorted. JSON has only string
        # keys, so a mapping that arrives with anything else did not come off the wire --
        # but sorting a mixed set raises a ``TypeError`` from inside the normaliser, and a
        # refusal that surfaces as a 500 is a refusal a caller can use.
        ignored = tuple(sorted(str(name) for name in set(raw) - set(self.arguments)))
        return NormalisedArguments(accepted=MappingProxyType(accepted), ignored=ignored)


def _validate(tool: ToolName, field_name: str, spec: ArgumentSpec, value: Any) -> str | int:
    """One argument, checked against its declared kind. Refuses, never coerces."""
    rejected = SchemaRejected(
        "argument_malformed", tool=tool.value, argument=field_name, kind=spec.kind.value
    )
    if spec.kind is ArgumentKind.COUNT:
        # ``bool`` is an ``int`` in Python, and ``True`` as a quantity would silently mean
        # one. A caller that sent a boolean did not mean a count.
        if not isinstance(value, int) or isinstance(value, bool):
            raise rejected
        if not spec.min_value <= value <= spec.max_value:
            raise SchemaRejected(
                "argument_out_of_range",
                tool=tool.value,
                argument=field_name,
                minimum=spec.min_value,
                maximum=spec.max_value,
            )
        return value
    if not isinstance(value, str):
        raise rejected
    text = value.strip()
    if not text or len(text) > spec.max_length:
        raise rejected
    if spec.kind is ArgumentKind.IDENTIFIER and not _IDENTIFIER.match(text):
        raise rejected
    if spec.kind is ArgumentKind.HASH and not _HASH.match(text):
        raise rejected
    if spec.kind is ArgumentKind.UUID:
        try:
            return str(uuid.UUID(text))
        except ValueError as exc:
            raise rejected from exc
    return text


#: The complete surface, specification 17.2. Read it top to bottom: there is no tool here
#: that approves, pays, refunds, captures, reconciles, revokes or talks to a provider, and
#: :func:`_assert_registry_is_closed` proves that the absence is structural rather than an
#: oversight this table happens to have today.
#:
#: Three mappings are worth their justification. ``quote.request`` carries ``basket.write``
#: because in this platform a quote is a property of a basket obtained by re-quoting it,
#: and ``basket.write`` is the narrowest entitlement the core's ceiling actually contains
#: for that; taking the stronger capability is the honest answer, inventing a weaker one
#: in a package that does not own the ceiling is not. ``order.propose_cancellation``,
#: ``refund.propose`` and ``support.escalate`` carry ``order.read`` for the mirror reason:
#: a proposal costs nothing, names no amount and produces a request for a human, so the
#: only entitlement it genuinely needs is being allowed to see the order it is about.
TOOLS: Final[Mapping[ToolName, ToolSpec]] = MappingProxyType(
    {
        ToolName.CATALOGUE_SEARCH: ToolSpec(
            name=ToolName.CATALOGUE_SEARCH,
            intent=IntentKind.DISCOVER,
            capability="catalogue.read",
            summary="Search the merchant's live catalogue. Results are grounded facts.",
            arguments={
                "query": ArgumentSpec(ArgumentKind.TEXT, "What the buyer is looking for."),
                "locale": ArgumentSpec(
                    ArgumentKind.TEXT, "BCP 47 language tag.", required=False, max_length=16
                ),
                "limit": ArgumentSpec(
                    ArgumentKind.COUNT,
                    "How many results to return.",
                    required=False,
                    min_value=1,
                    max_value=50,
                ),
            },
        ),
        ToolName.CATALOGUE_PRODUCT: ToolSpec(
            name=ToolName.CATALOGUE_PRODUCT,
            intent=IntentKind.DISCOVER,
            capability="catalogue.read",
            summary="Look up one product by SKU with its current availability.",
            arguments={"sku": ArgumentSpec(ArgumentKind.IDENTIFIER, "The merchant's SKU.")},
        ),
        ToolName.INVENTORY_CHECK: ToolSpec(
            name=ToolName.INVENTORY_CHECK,
            intent=IntentKind.CHECK_INVENTORY,
            capability="catalogue.read",
            summary="Ask whether a quantity of one SKU is available right now.",
            arguments={
                "sku": ArgumentSpec(ArgumentKind.IDENTIFIER, "The merchant's SKU."),
                "quantity": ArgumentSpec(
                    ArgumentKind.COUNT, "Units wanted.", min_value=1, max_value=999
                ),
            },
        ),
        ToolName.BASKET_CREATE: ToolSpec(
            name=ToolName.BASKET_CREATE,
            intent=IntentKind.BUILD_BASKET,
            capability="basket.write",
            summary="Create an empty basket. Reversible, carries no money and no authority.",
        ),
        ToolName.BASKET_UPDATE: ToolSpec(
            name=ToolName.BASKET_UPDATE,
            intent=IntentKind.BUILD_BASKET,
            capability="basket.write",
            summary="Set one basket line's quantity. Zero removes the line.",
            arguments={
                "basket_id": ArgumentSpec(ArgumentKind.IDENTIFIER, "The basket to amend."),
                "sku": ArgumentSpec(ArgumentKind.IDENTIFIER, "The line's SKU."),
                "quantity": ArgumentSpec(
                    ArgumentKind.COUNT, "Units wanted; zero removes.", max_value=999
                ),
            },
        ),
        ToolName.QUOTE_REQUEST: ToolSpec(
            name=ToolName.QUOTE_REQUEST,
            intent=IntentKind.BUILD_BASKET,
            capability="basket.write",
            summary="Re-price a basket and report whether its quote has gone stale.",
            arguments={"basket_id": ArgumentSpec(ArgumentKind.IDENTIFIER, "The basket to price.")},
        ),
        ToolName.RESERVATION_REQUEST: ToolSpec(
            name=ToolName.RESERVATION_REQUEST,
            intent=IntentKind.CREATE_CHECKOUT,
            capability="checkout.create",
            summary=(
                "Turn a basket into an immutable, hashed checkout version holding a stock "
                "reservation. Awaits the buyer's decision; approves nothing."
            ),
            arguments={"basket_id": ArgumentSpec(ArgumentKind.IDENTIFIER, "The basket to fix.")},
        ),
        ToolName.CHECKOUT_SUBMIT_FOR_APPROVAL: ToolSpec(
            name=ToolName.CHECKOUT_SUBMIT_FOR_APPROVAL,
            intent=IntentKind.REQUEST_APPROVAL,
            capability="checkout.create",
            summary=(
                "Ask the buyer to decide about a checkout version. The decision is taken on "
                "the trusted buyer surface against the hash the buyer is shown."
            ),
            arguments={
                "checkout_id": ArgumentSpec(ArgumentKind.UUID, "The checkout."),
                "version": ArgumentSpec(
                    ArgumentKind.COUNT, "Which version.", min_value=1, max_value=999
                ),
            },
        ),
        ToolName.CHECKOUT_SUBMIT_APPROVED: ToolSpec(
            name=ToolName.CHECKOUT_SUBMIT_APPROVED,
            intent=IntentKind.SUBMIT_APPROVED,
            capability="checkout.submit_approved",
            summary=(
                "Submit a version the buyer has already approved to the same kernel "
                "admission every other surface uses. Admission decides; this does not."
            ),
            arguments={
                "checkout_id": ArgumentSpec(ArgumentKind.UUID, "The checkout."),
                "version": ArgumentSpec(
                    ArgumentKind.COUNT, "The approved version.", min_value=1, max_value=999
                ),
                "content_hash": ArgumentSpec(
                    ArgumentKind.HASH, "The hash the buyer approved, echoed back."
                ),
            },
        ),
        ToolName.ORDER_TRACK: ToolSpec(
            name=ToolName.ORDER_TRACK,
            intent=IntentKind.TRACK_ORDER,
            capability="order.read",
            summary="Read an order's state and the evidence the platform captured for it.",
            arguments={"order_id": ArgumentSpec(ArgumentKind.UUID, "The order.")},
        ),
        ToolName.ORDER_PROPOSE_CANCELLATION: ToolSpec(
            name=ToolName.ORDER_PROPOSE_CANCELLATION,
            intent=IntentKind.PROPOSE_CANCELLATION,
            capability="order.read",
            summary=(
                "Ask a human to consider cancelling an order. Names no amount and cancels "
                "nothing; it produces a request for a decision."
            ),
            arguments={
                "order_id": ArgumentSpec(ArgumentKind.UUID, "The order."),
                "reason": ArgumentSpec(
                    ArgumentKind.TEXT, "Why the buyer wants this.", max_length=500
                ),
            },
        ),
        ToolName.REFUND_PROPOSE: ToolSpec(
            name=ToolName.REFUND_PROPOSE,
            intent=IntentKind.PROPOSE_REFUND,
            capability="order.read",
            summary=(
                "Ask a human to consider a refund. Names no amount and refunds nothing; the "
                "amount is decided under policy on the trusted surface."
            ),
            arguments={
                "order_id": ArgumentSpec(ArgumentKind.UUID, "The order."),
                "reason": ArgumentSpec(
                    ArgumentKind.TEXT, "Why the buyer wants this.", max_length=500
                ),
            },
        ),
        ToolName.SUPPORT_ESCALATE: ToolSpec(
            name=ToolName.SUPPORT_ESCALATE,
            intent=IntentKind.ESCALATE_SUPPORT,
            capability="order.read",
            summary="Hand the conversation to a person, creating at most one support case.",
            arguments={
                "summary": ArgumentSpec(
                    ArgumentKind.TEXT, "What the buyer needs, in one paragraph.", max_length=1000
                ),
                "order_id": ArgumentSpec(
                    ArgumentKind.UUID, "The order, if there is one.", required=False
                ),
            },
        ),
    }
)


def resolve_tool(name: str) -> ToolSpec:
    """Turn a name that arrived on the wire into a registered tool, or refuse it.

    This is the whole of the server's dispatch, and what it deliberately is not: there is
    no fallback lookup, no ``getattr``, no handler map keyed by an arbitrary string and no
    prefix that forwards the remainder somewhere else. A name is either one of thirteen
    enum members or the request stops here.
    """
    try:
        tool = ToolName(name)
    except ValueError as exc:
        raise SchemaRejected("tool_not_in_registry", tool=str(name)[:64]) from exc
    return TOOLS[tool]


def public_name_offends(name: str) -> bool:
    """True when a public identifier names an act this surface must not be able to do.

    Segment-wise and exact, never substring: ``submit_approved`` is allowed and
    ``submit_approve`` would not be, which is the distinction specification 17.2's sixth
    bullet turns on. Used by the import-time assertion and by the tests that walk the
    package's public surface.
    """
    spaced = _CAMEL_HUMP.sub(" ", name).lower()
    return bool({part for part in _SEGMENTS.split(spaced) if part} & NEVER_ON_MCP_SURFACE)


def _assert_registry_is_closed() -> None:
    """Prove the specification 17.3 properties at import, not in a test that can be skipped."""
    if set(TOOLS) != set(ToolName):
        missing = sorted(str(n) for n in set(ToolName) - set(TOOLS))
        raise RuntimeError(f"tool enum members without a registry row: {missing}")
    for key, spec in TOOLS.items():
        if spec.name is not key:
            raise RuntimeError(f"registry row {key} is keyed by a name it does not carry")
    overlap = {tool.value for tool in TOOLS} & FORBIDDEN_TOOL_NAMES
    if overlap:
        raise RuntimeError(f"the registry names a forbidden tool: {sorted(overlap)}")
    offending = sorted(tool.value for tool in TOOLS if public_name_offends(tool.value))
    if offending:
        raise RuntimeError(f"a tool name states an act this surface may not perform: {offending}")
    monetary = {"MONEY", "AMOUNT", "CURRENCY", "PRICE", "DECIMAL", "FLOAT"} & set(
        ArgumentKind.__members__
    )
    if monetary:
        raise RuntimeError(f"an argument kind can express money: {sorted(monetary)}")
    reaching = sorted(tool.value for tool, spec in TOOLS.items() if spec.reaches_kernel)
    if reaching != [ToolName.CHECKOUT_SUBMIT_APPROVED.value]:
        raise RuntimeError(f"exactly one tool may reach kernel admission; found {reaching}")


_assert_registry_is_closed()
