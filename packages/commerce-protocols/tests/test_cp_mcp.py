"""The governed MCP tool server, proved by the absences rather than by the features.

Specification 17.3 forbids a design. So most of this file is not a behaviour suite: it
walks the registry, the module surface and the abstract syntax tree of every module in
``commerce_protocols.mcp`` and asserts that certain things do not exist anywhere. A
behaviour test can only ever show that today's guard held; these show that there is no
code path a future edit could route around, because the thing being guarded is not there.

The two halves and why the file keeps them together
---------------------------------------------------
The first half is pure. Enumerating a registry, refusing a forbidden enum member, screening
a result for a credential -- none of it needs a database, and a reviewer should be able to
read the strongest claims in the file without one.

The second half cannot be honest without a real PostgreSQL, and lives under
:class:`TestGovernedSessionAgainstPostgres` with ``pytestmark = pytest.mark.db``. What it
asserts is that a replayed access token is refused, that a replayed call nonce is refused,
and that the freshness window is judged by the database clock rather than by this process's
-- and every one of those is a claim about a uniqueness constraint and a transaction clock.
Against a fake, all three pass while proving nothing. They run as ``commerce_test_kernel``,
NOSUPERUSER NOBYPASSRLS, for the reason the package conftest gives.

The most important test in the file is
``test_a_model_supplied_tenant_id_is_ignored_and_never_reaches_the_kernel``. It is written
against a recording admission port rather than against a mock's call log, because what it
has to prove is not "the server passed the right argument" but "there is no argument it
could have passed": the port has no ``tenant_id`` parameter, and the principal that arrives
carries the tenant the access token named.
"""

from __future__ import annotations

import ast
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import pytest
from commerce_domain import uuid7
from commerce_protocols import mcp
from commerce_protocols.core import (
    AGGREGATE_TYPE,
    CONSENT_CAPABILITIES,
    PROTOCOL_CAPABILITIES,
    AuthenticationRejected,
    EvidenceStage,
    IntentKind,
    ReplayRejected,
    SchemaRejected,
    StateRejected,
    VersionRejected,
    database_now,
)
from commerce_protocols.mcp import (
    TOOLS,
    AccessToken,
    AdmittedCall,
    ArgumentKind,
    ArgumentSpec,
    CredentialLeakError,
    GovernedToolServer,
    McpSession,
    ResultShapeError,
    ToolCall,
    ToolName,
    ToolResult,
    ToolSpec,
    public_name_offends,
    resolve_tool,
    scope_for,
)
from platform_db import AuditEvent
from sqlalchemy import select
from sqlalchemy.orm import Session
from transaction_kernel import AdmissionDecision, AgentPrincipal, RecoveryCode
from transaction_kernel import audit as kernel_audit
from transaction_kernel.audit import AuditTenantError

#: This server's RFC 8707 resource indicator throughout the suite.
RESOURCE: Final[str] = "https://commerce.example/mcp"

#: An audience belonging to somebody else. Specification 29.7's "MCP token audience
#: mismatch": a real, well-formed token minted for a different resource server.
OTHER_RESOURCE: Final[str] = "https://calendar.example/mcp"

#: The pinned MCP version. Read from the matrix so a pin change breaks here loudly.
PINNED_VERSION: Final[str] = "2025-06-18"

ALL_SCOPES: Final[frozenset[str]] = frozenset(
    scope_for(capability) for capability in PROTOCOL_CAPABILITIES
)

#: Specification 17.2, transcribed by hand. If the registry and this list ever disagree,
#: one of them changed without the other, and that is the change worth catching.
ALLOWED_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "catalogue.search",
        "catalogue.product",
        "inventory.check",
        "basket.create",
        "basket.update",
        "quote.request",
        "reservation.request",
        "checkout.submit_for_approval",
        "checkout.submit_approved",
        "order.track",
        "order.propose_cancellation",
        "refund.propose",
        "support.escalate",
    }
)


# ------------------------------------------------------------------------- test doubles


@dataclass(frozen=True, slots=True)
class _Introspector:
    """Stands in for the authorization server. Returns claims it was told to return.

    Deliberately does no verification of its own: every check this suite cares about is one
    the *resource server* must make after introspection succeeds, and a double that refused
    tokens would hide which of the two did the refusing.
    """

    token: AccessToken

    def introspect(self, presented: str) -> AccessToken:
        return self.token


class _RecordingAdmission:
    """A kernel admission port that records exactly what reached it.

    The point of recording rather than asserting inside is that the tenant test needs to
    inspect the *principal*: the port's signature already proves a tenant id could not have
    been passed alongside it, so what remains to show is which tenant the principal carries.
    """

    def __init__(self, *, allowed: bool = True) -> None:
        self.calls: list[dict[str, Any]] = []
        self._allowed = allowed

    def admit_approved(
        self,
        session: Session,
        *,
        principal: AgentPrincipal,
        checkout_id: uuid.UUID,
        version: int,
        content_hash: str,
    ) -> AdmissionDecision:
        self.calls.append(
            {
                "principal": principal,
                "checkout_id": checkout_id,
                "version": version,
                "content_hash": content_hash,
            }
        )
        if self._allowed:
            return AdmissionDecision(
                decision_id=uuid7(),
                allowed=True,
                code=RecoveryCode.OK,
                explanation="admitted",
                grant_id=uuid7(),
                correlation_id=principal.correlation_id,
            )
        return AdmissionDecision(
            decision_id=uuid7(),
            allowed=False,
            code=RecoveryCode.REAPPROVAL_REQUIRED,
            explanation="price_changed",
            correlation_id=principal.correlation_id,
        )


def _token(
    now: datetime,
    tenant_id: uuid.UUID,
    merchant_id: uuid.UUID,
    *,
    audience: str = RESOURCE,
    scopes: frozenset[str] = ALL_SCOPES,
    lifetime: timedelta = timedelta(minutes=4),
    issued_at: datetime | None = None,
    token_id: str | None = None,
    client_id: str = "mcp-client-1",
) -> AccessToken:
    """One introspected access token, dated against the database clock."""
    minted = issued_at if issued_at is not None else now
    return AccessToken(
        token_id=token_id or uuid.uuid4().hex,
        client_id=client_id,
        subject="buyer-42",
        issuer="https://issuer.example",
        audience=audience,
        tenant_id=tenant_id,
        merchant_id=merchant_id,
        scopes=scopes,
        issued_at=minted,
        expires_at=minted + lifetime,
    )


def _server(token: AccessToken, admission: _RecordingAdmission | None = None) -> GovernedToolServer:
    return GovernedToolServer(
        resource=RESOURCE,
        introspector=_Introspector(token),
        admission=admission or _RecordingAdmission(),
    )


def _call(tool: ToolName, **arguments: Any) -> ToolCall:
    """A tool call whose nonce is unique and whose timestamp is a caller-supplied one.

    ``requested_at`` is filled in by the test that has the database clock; this helper
    exists only so the argument bag reads as the interesting part of each test.
    """
    return ToolCall(
        tool=tool.value,
        arguments=arguments,
        nonce=uuid.uuid4().hex,
        requested_at=datetime.now().astimezone(),
    )


def _approved_submit(now: datetime) -> ToolCall:
    """The one call that reaches the kernel, well formed, dated by the database clock."""
    return replace(
        _call(
            ToolName.CHECKOUT_SUBMIT_APPROVED,
            checkout_id=str(uuid7()),
            version=1,
            content_hash="Zm9vYmFyYmF6cXV4MTIzNDU2",
        ),
        requested_at=now,
    )


# ------------------------------------------------------- the registry, and what is absent


def test_the_registry_contains_exactly_the_tools_specification_17_2_allows() -> None:
    assert {tool.value for tool in TOOLS} == ALLOWED_TOOL_NAMES
    assert set(TOOLS) == set(ToolName)
    for name, spec in TOOLS.items():
        assert spec.name is name, f"{name} is keyed by a spec that names something else"


def test_every_allowed_tool_maps_onto_a_core_intent_kind() -> None:
    expected = {
        ToolName.CATALOGUE_SEARCH: IntentKind.DISCOVER,
        ToolName.CATALOGUE_PRODUCT: IntentKind.DISCOVER,
        ToolName.INVENTORY_CHECK: IntentKind.CHECK_INVENTORY,
        ToolName.BASKET_CREATE: IntentKind.BUILD_BASKET,
        ToolName.BASKET_UPDATE: IntentKind.BUILD_BASKET,
        ToolName.QUOTE_REQUEST: IntentKind.BUILD_BASKET,
        ToolName.RESERVATION_REQUEST: IntentKind.CREATE_CHECKOUT,
        ToolName.CHECKOUT_SUBMIT_FOR_APPROVAL: IntentKind.REQUEST_APPROVAL,
        ToolName.CHECKOUT_SUBMIT_APPROVED: IntentKind.SUBMIT_APPROVED,
        ToolName.ORDER_TRACK: IntentKind.TRACK_ORDER,
        ToolName.ORDER_PROPOSE_CANCELLATION: IntentKind.PROPOSE_CANCELLATION,
        ToolName.REFUND_PROPOSE: IntentKind.PROPOSE_REFUND,
        ToolName.SUPPORT_ESCALATE: IntentKind.ESCALATE_SUPPORT,
    }
    assert {name: spec.intent for name, spec in TOOLS.items()} == expected


def test_no_tool_approves_pays_refunds_or_revokes_by_any_spelling() -> None:
    """The registry names none of the forbidden operations, however they are written."""
    assert not {tool.value for tool in TOOLS} & mcp.FORBIDDEN_TOOL_NAMES
    for tool in TOOLS:
        assert not public_name_offends(tool.value), f"{tool.value} states a forbidden act"
    # ``refund.propose`` survives that screen and must: proposing is not refunding. What
    # makes it safe is its intent and its arguments, asserted separately.
    assert TOOLS[ToolName.REFUND_PROPOSE].intent is IntentKind.PROPOSE_REFUND


def test_the_surface_screen_separates_the_act_from_the_fact_of_having_been_approved() -> None:
    """The one distinction the screen turns on, asserted rather than assumed.

    ``checkout.submit_approved`` contains the letters of "approve" and is allowed, because
    submitting a version a human already approved is specification 17.2's sixth bullet.
    ``checkout.approve`` is the act and is refused. A substring screen would have to reject
    both or accept both; a segment screen gets the distinction right.
    """
    assert public_name_offends("checkout.approve")
    assert public_name_offends("submit_and_approve")
    assert public_name_offends("payment.execute")
    assert public_name_offends("refundExecute")
    assert not public_name_offends("checkout.submit_approved")
    assert not public_name_offends("refund.propose")


def test_a_name_that_hides_a_forbidden_tool_behind_a_prefix_resolves_to_nothing() -> None:
    """There is no prefix match and no path traversal, because there is no path."""
    for shaped in (
        "catalogue.search/../payment.execute",
        "catalogue.search\x00payment.execute",
        "CATALOGUE.SEARCH",
        "catalogue.search ",
        "tools/catalogue.search",
    ):
        with pytest.raises(SchemaRejected) as caught:
            resolve_tool(shaped)
        assert caught.value.reason == "tool_not_in_registry"


def test_the_public_surface_of_the_package_names_no_forbidden_act() -> None:
    """Not just the registry: every exported name and every server method."""
    surface = set(mcp.__all__)
    surface |= {name for name in dir(mcp.server.GovernedToolServer) if not name.startswith("_")}
    for module in (mcp.tools, mcp.server, mcp.authorization, mcp.results):
        surface |= {name for name in dir(module) if not name.startswith("_")}
    offending = sorted(name for name in surface if public_name_offends(name))
    assert offending == [], f"the package exposes names stating forbidden acts: {offending}"


def test_the_server_has_no_method_that_could_move_money_by_itself() -> None:
    methods = {name for name in dir(GovernedToolServer) if not name.startswith("_")}
    assert methods == {
        "answer",
        "admit_call",
        "describe_tools",
        "open_session",
        "resource",
        "submit_approved",
    }


def test_a_forbidden_tool_name_cannot_be_constructed_at_all() -> None:
    for forbidden in sorted(mcp.FORBIDDEN_TOOL_NAMES):
        with pytest.raises(ValueError, match="not a valid ToolName"):
            ToolName(forbidden)
        with pytest.raises(SchemaRejected) as caught:
            resolve_tool(forbidden)
        assert caught.value.reason == "tool_not_in_registry"


def test_the_registry_cannot_be_extended_at_runtime() -> None:
    """A read-only mapping, so a forbidden tool cannot be injected into a live process."""
    assert isinstance(TOOLS, MappingProxyType)
    with pytest.raises(TypeError):
        TOOLS[ToolName.ORDER_TRACK] = TOOLS[ToolName.ORDER_TRACK]  # type: ignore[index]


def test_a_tool_spec_cannot_require_a_capability_outside_the_protocol_ceiling() -> None:
    for capability in sorted(CONSENT_CAPABILITIES):
        with pytest.raises(ValueError, match="protocol capability ceiling"):
            ToolSpec(
                name=ToolName.ORDER_TRACK,
                intent=IntentKind.TRACK_ORDER,
                capability=capability,
                summary="a tool that consents on a human's behalf",
            )


def test_a_tool_spec_cannot_declare_an_amount_or_a_tenant_argument() -> None:
    for forbidden in ("amount_minor", "total", "currency", "tenant_id", "access_token"):
        with pytest.raises(ValueError, match="a model may never state"):
            ToolSpec(
                name=ToolName.REFUND_PROPOSE,
                intent=IntentKind.PROPOSE_REFUND,
                capability="order.read",
                summary="a proposal that names an amount",
                arguments={forbidden: ArgumentSpec(ArgumentKind.TEXT, "no")},
            )


def test_the_argument_vocabulary_cannot_express_money() -> None:
    """There is no monetary kind, so no future tool can accept one without editing this."""
    assert {"MONEY", "AMOUNT", "PRICE", "CURRENCY", "DECIMAL", "FLOAT"}.isdisjoint(
        ArgumentKind.__members__
    )


def test_no_tool_anywhere_declares_a_forbidden_argument() -> None:
    for name, spec in TOOLS.items():
        offending = sorted(set(spec.arguments) & mcp.FORBIDDEN_ARGUMENT_NAMES)
        assert offending == [], f"{name.value} accepts {offending}"


def test_exactly_one_tool_reaches_the_kernel_and_it_is_the_approved_submit() -> None:
    reaching = {name for name, spec in TOOLS.items() if spec.reaches_kernel}
    assert reaching == {ToolName.CHECKOUT_SUBMIT_APPROVED}
    assert TOOLS[ToolName.CHECKOUT_SUBMIT_APPROVED].capability == "checkout.submit_approved"


def test_every_tool_capability_is_inside_the_protocol_ceiling() -> None:
    used = {spec.capability for spec in TOOLS.values()}
    assert used <= PROTOCOL_CAPABILITIES
    assert not used & CONSENT_CAPABILITIES


def test_a_proposal_tool_cannot_name_an_amount() -> None:
    """Specification 29.4: a proposal names no amount, and the schema says so out loud."""
    for name in (ToolName.ORDER_PROPOSE_CANCELLATION, ToolName.REFUND_PROPOSE):
        spec = TOOLS[name]
        schema = spec.to_schema()["inputSchema"]
        assert schema["additionalProperties"] is False
        assert set(spec.arguments) == {"order_id", "reason"}
        normalised = spec.normalise(
            {"order_id": str(uuid7()), "reason": "damaged", "amount_minor": 39500}
        )
        assert "amount_minor" not in normalised.accepted
        assert normalised.ignored == ("amount_minor",)


def test_a_declared_argument_that_is_malformed_is_refused_rather_than_dropped() -> None:
    spec = TOOLS[ToolName.ORDER_TRACK]
    with pytest.raises(SchemaRejected) as caught:
        spec.normalise({"order_id": "not-a-uuid"})
    assert caught.value.reason == "argument_malformed"


def test_a_boolean_is_not_a_quantity() -> None:
    """``True`` is an ``int`` in Python and would silently mean one unit."""
    spec = TOOLS[ToolName.INVENTORY_CHECK]
    with pytest.raises(SchemaRejected):
        spec.normalise({"sku": "MILK-DAIRY-001", "quantity": True})


def test_a_float_never_survives_an_argument_however_it_was_meant() -> None:
    """No float touches a number this platform will act on, in either declared kind.

    A ``COUNT`` refuses one because a fractional quantity is not a quantity; a ``TEXT``
    refuses one because it is not text. Neither coerces, so there is no path by which
    ``39.5`` becomes ``39`` or ``"39.5"`` somewhere downstream and is then read as money.
    """
    with pytest.raises(SchemaRejected):
        TOOLS[ToolName.INVENTORY_CHECK].normalise({"sku": "MILK-DAIRY-001", "quantity": 2.0})
    with pytest.raises(SchemaRejected):
        TOOLS[ToolName.REFUND_PROPOSE].normalise({"order_id": str(uuid7()), "reason": 395.0})


def test_a_call_carrying_a_flood_of_arguments_is_refused_unread() -> None:
    spec = TOOLS[ToolName.BASKET_CREATE]
    with pytest.raises(SchemaRejected) as caught:
        spec.normalise({f"field_{index}": index for index in range(64)})
    assert caught.value.reason == "too_many_arguments"


def test_an_argument_bag_whose_keys_are_not_all_strings_is_dropped_rather_than_crashing() -> None:
    """JSON has only string keys, so a mixed bag is a caller that did not come off the wire.

    It is still dropped the way every undeclared field is dropped. Sorting the names for
    the evidence row without stringifying them first would raise a ``TypeError`` from
    inside the normaliser, and a refusal that arrives as a 500 tells a caller nothing and
    tells an operator that the platform is broken.
    """
    mixed: dict[Any, Any] = {1: "a", "b": "c"}
    assert TOOLS[ToolName.BASKET_CREATE].normalise(mixed).ignored == ("1", "b")


# ------------------------------------------------------------------ results and secrets


@pytest.mark.parametrize(
    "content",
    [
        {"razorpay_key_secret": "abc"},
        {"apiKey": "abc"},
        {"api_key": "abc"},
        {"webhook_secret": "abc"},
        {"authorization": "abc"},
        {"private_key": "abc"},
        {"nested": {"credentials": {"value": "abc"}}},
        {"signature": "deadbeef"},
        {"razorpayKeySecret": "abc"},
        {"myApiKey": "abc"},
        {"lines": [{"access_token": "abc"}]},
    ],
)
def test_a_tool_result_cannot_carry_a_field_that_names_a_credential(
    content: dict[str, Any],
) -> None:
    with pytest.raises(CredentialLeakError):
        ToolResult(tool=ToolName.ORDER_TRACK, content=content)


def test_the_field_screen_does_not_fire_on_an_innocent_name() -> None:
    """A screen nobody can answer a question through is not a screen, it is an outage."""
    result = ToolResult(
        tool=ToolName.ORDER_TRACK,
        content={
            "shipment_state": "PACKED",
            "capture_evidence": "WEBHOOK",
            "catalogue_revision": 12,
            "policy_receipt_hash": "Zm9vYmFyYmF6cXV4MTIzNDU2",
        },
    )
    assert result.content["capture_evidence"] == "WEBHOOK"


@pytest.mark.parametrize(
    "value",
    [
        "rzp_test_ABCDEFGHIJ",
        "rzp_live_ABCDEFGHIJ",
        "whsec_ABCDEFGHIJ",
        "-----BEGIN EC PRIVATE KEY-----",
        '{"kty":"EC","d":"ZmFrZS1wcml2YXRlLXNjYWxhcg"}',
        "Bearer eyJhbGciOiJFUzI1NiJ9",
        "eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJidXllciJ9.c2lnbmF0dXJl",
    ],
)
def test_a_tool_result_cannot_carry_a_value_that_looks_like_a_credential(value: str) -> None:
    """The value screen, not the key screen: the leak that happens is in a ``detail`` field."""
    with pytest.raises(CredentialLeakError):
        ToolResult(tool=ToolName.ORDER_TRACK, content={"detail": value})


@pytest.mark.parametrize(
    "value",
    [b"whsec_ABCDEFGHIJ", bytearray(b"rzp_live_ABCDEFGHIJ"), memoryview(b"sk_live_ABCDEFG")],
)
def test_a_credential_offered_as_bytes_is_refused_rather_than_transcribed(value: Any) -> None:
    """``bytes`` is a ``Sequence``, so a screen that walks sequences would transcribe it.

    Nothing about the secret changes when each byte is written out as an integer; what
    changes is that the value screen, which reads strings, can no longer see it. So the
    type is refused before the sequence branch is ever reached.
    """
    with pytest.raises(ResultShapeError, match="never raw bytes"):
        ToolResult(tool=ToolName.ORDER_TRACK, content={"detail": value})


@pytest.mark.parametrize(
    "prose",
    [
        "A basic cotton shirt in three colours",
        "Basic White Tee, regular fit",
        "Bearer of the warranty must present the receipt",
    ],
)
def test_the_value_screen_does_not_fire_on_ordinary_catalogue_prose(prose: str) -> None:
    """A screen that refuses a product description takes the surface down, not the leak.

    ``CredentialLeakError`` is a fault: it is a 500 and a failed request. A bare
    ``bearer``/``basic`` substring is a word English uses, so the screen matches the scheme
    only when something credential-shaped follows it -- which is what an actual leaked
    ``Authorization`` header always looks like and what a sentence never does.
    """
    result = ToolResult(tool=ToolName.CATALOGUE_PRODUCT, content={"description": prose})
    assert result.content["description"] == prose


def test_an_authentication_scheme_with_a_credential_after_it_is_still_refused() -> None:
    for leaked in (
        "Authorization: Basic YWRtaW46aHVudGVyMg==",
        "bearer abcdefghijklmnopqrstuvwxyz",
    ):
        with pytest.raises(CredentialLeakError):
            ToolResult(tool=ToolName.ORDER_TRACK, content={"detail": leaked})


def test_a_tool_result_refuses_a_float_because_money_is_integer_minor_units() -> None:
    with pytest.raises(ResultShapeError, match="integer minor units"):
        ToolResult(tool=ToolName.ORDER_TRACK, content={"total": 395.0})


def test_a_tool_result_that_carries_only_facts_is_accepted_and_frozen() -> None:
    result = ToolResult(
        tool=ToolName.ORDER_TRACK,
        content={"state": "CONFIRMED", "amount_minor": 39500, "lines": [{"sku": "MILK-001"}]},
    )
    assert result.content["amount_minor"] == 39500
    assert isinstance(result.content, MappingProxyType)
    with pytest.raises(TypeError):
        result.content["state"] = "REFUNDED"  # type: ignore[index]


def test_a_tool_result_owns_its_content_so_a_later_mutation_cannot_change_the_answer() -> None:
    mutable: dict[str, Any] = {"state": "CONFIRMED"}
    result = ToolResult(tool=ToolName.ORDER_TRACK, content=mutable)
    mutable["state"] = "REFUNDED"
    assert result.content["state"] == "CONFIRMED"


def test_a_failed_tool_result_names_a_stable_reason_and_a_successful_one_names_none() -> None:
    refusal = ToolResult.refusal(ToolName.CATALOGUE_PRODUCT, "unknown_sku", sku="NOPE-000")
    assert refusal.is_error and refusal.reason == "unknown_sku"
    with pytest.raises(ResultShapeError):
        ToolResult(tool=ToolName.ORDER_TRACK, content={}, is_error=True)


# --------------------------------------------------------- absence, proved from the source


def _module_paths() -> tuple[Path, ...]:
    package = Path(mcp.__file__).parent
    return tuple(sorted(package.glob("*.py")))


def test_no_module_in_the_package_imports_a_transport_or_a_provider_sdk() -> None:
    """Specification 17.3's first bullet, enforced at the import graph.

    A raw Razorpay tool cannot be built out of a module that cannot reach the network. This
    reads the syntax tree rather than ``sys.modules`` so a lazily imported client inside a
    function body is caught too.
    """
    banned = {
        "http",
        "httpx",
        "razorpay",
        "requests",
        "socket",
        "ssl",
        "subprocess",
        "urllib",
        "urllib3",
    }
    for path in _module_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                roots = {(node.module or "").split(".")[0]}
            else:
                continue
            offending = sorted(roots & banned)
            assert offending == [], f"{path.name} imports {offending}"


def test_no_module_in_the_package_executes_sql_of_its_own() -> None:
    """No raw SQL surface (17.3). The only database work is the core's replay and audit."""
    for path in _module_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = node.func
            name = called.attr if isinstance(called, ast.Attribute) else getattr(called, "id", "")
            assert name not in {"execute", "text", "exec_driver_sql"}, (
                f"{path.name} builds its own SQL through {name}()"
            )


def test_the_server_exposes_no_generic_dispatcher() -> None:
    """No ``call_tool(name, args)``: the string becomes an enum member or the call stops."""
    source = (Path(mcp.__file__).parent / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            assert name not in {"getattr", "eval", "exec", "__import__"}, (
                f"server.py dispatches through {name}()"
            )


# ------------------------------------------------------------------ sessions, against a db


class TestGovernedSessionAgainstPostgres:
    """Authorization, replay and admission. Every claim here needs a real database."""

    pytestmark = pytest.mark.db

    @staticmethod
    def _open(
        session: Session,
        token: AccessToken,
        *,
        admission: _RecordingAdmission | None = None,
        registered_tools: frozenset[ToolName] | None = None,
    ) -> tuple[GovernedToolServer, McpSession]:
        server = _server(token, admission)
        opened = server.open_session(
            session,
            presented="presented-credential",
            announced_version=PINNED_VERSION,
            registered_tools=registered_tools,
        )
        return server, opened

    # ------------------------------------------------------------------ authorization

    def test_a_token_whose_audience_names_another_resource_server_is_refused(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """Specification 29.7: MCP token audience mismatch.

        The token is entirely valid -- unexpired, correctly scoped, minted by the issuer we
        trust. It is simply not for us, and that alone is the whole refusal.
        """
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        token = _token(now, tenant_id, merchant_id, audience=OTHER_RESOURCE)
        with pytest.raises(AuthenticationRejected) as caught:
            self._open(cp_session, token)
        assert caught.value.reason == "token_audience_mismatch"
        assert caught.value.details["resource"] == RESOURCE

    def test_an_expired_token_is_refused(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        token = _token(
            now,
            tenant_id,
            merchant_id,
            issued_at=now - timedelta(minutes=4),
            lifetime=timedelta(minutes=1),
        )
        with pytest.raises(AuthenticationRejected) as caught:
            self._open(cp_session, token)
        assert caught.value.reason == "token_expired"

    def test_a_token_whose_declared_lifetime_is_long_is_refused_before_it_expires(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """Specification 17.4 asks for short lifetimes; expiry alone does not deliver that."""
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        token = _token(now, tenant_id, merchant_id, lifetime=timedelta(days=365))
        with pytest.raises(AuthenticationRejected) as caught:
            self._open(cp_session, token)
        assert caught.value.reason == "token_lifetime_exceeds_ceiling"

    def test_a_token_dated_in_the_future_is_refused(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        token = _token(now, tenant_id, merchant_id, issued_at=now + timedelta(minutes=3))
        with pytest.raises(AuthenticationRejected) as caught:
            self._open(cp_session, token)
        assert caught.value.reason == "token_issued_in_the_future"

    def test_a_replayed_token_is_refused_not_answered(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """One access token establishes one session. The second presentation is a refusal.

        ``ReplayRejected`` rather than a duplicate-operation success, because nothing was
        done under the first presentation that the second caller is entitled to read back.
        """
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        token = _token(now, tenant_id, merchant_id)
        self._open(cp_session, token)
        with pytest.raises(ReplayRejected) as caught:
            self._open(cp_session, token)
        assert caught.value.reason == "nonce_already_presented"
        assert caught.value.code is RecoveryCode.POLICY_EXCEPTION

    def test_an_unpinned_protocol_version_is_refused_before_the_credential_is_read(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        server = _server(_token(now, tenant_id, merchant_id))
        with pytest.raises(VersionRejected) as caught:
            server.open_session(cp_session, presented="whatever", announced_version="2099-01-01")
        assert caught.value.details["supported"] == PINNED_VERSION

    def test_a_token_granting_no_tool_is_refused_rather_than_given_an_empty_session(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        token = _token(now, tenant_id, merchant_id, scopes=frozenset({"openid", "profile"}))
        with pytest.raises(AuthenticationRejected) as caught:
            self._open(cp_session, token)
        assert caught.value.reason == "token_grants_no_tool"

    def test_a_scope_naming_a_consent_capability_grants_nothing(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """Registry B is unreachable however the authorization server is configured."""
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        widened = ALL_SCOPES | {scope_for(capability) for capability in CONSENT_CAPABILITIES}
        token = _token(now, tenant_id, merchant_id, scopes=widened)
        _, opened = self._open(cp_session, token)
        assert not opened.principal.capabilities & CONSENT_CAPABILITIES
        assert opened.principal.capabilities <= PROTOCOL_CAPABILITIES

    def test_the_session_keeps_only_a_fingerprint_of_the_presented_credential(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """No token passthrough: there is no field the credential could have been kept in."""
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        _, opened = self._open(cp_session, _token(now, tenant_id, merchant_id))
        rendered = repr(opened)
        assert "presented-credential" not in rendered
        assert opened.token_fingerprint.length == len("presented-credential")
        assert opened.token_fingerprint.digest

    def test_the_session_allowlist_cannot_be_widened_after_creation(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """``dataclasses.replace`` is the obvious way past a frozen dataclass. It is refused."""
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        token = _token(now, tenant_id, merchant_id, scopes=frozenset({scope_for("catalogue.read")}))
        _, opened = self._open(cp_session, token)
        assert ToolName.CHECKOUT_SUBMIT_APPROVED not in opened.allowed_tools
        with pytest.raises(AuthenticationRejected) as caught:
            replace(opened, allowed_tools=frozenset(ToolName))
        assert caught.value.reason == "session_allowlist_exceeds_granted_capabilities"

    def test_a_client_registration_can_only_narrow_what_the_scopes_granted(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        _, opened = self._open(
            cp_session,
            _token(now, tenant_id, merchant_id),
            registered_tools=frozenset({ToolName.CATALOGUE_SEARCH, ToolName.ORDER_TRACK}),
        )
        assert opened.allowed_tools == {ToolName.CATALOGUE_SEARCH, ToolName.ORDER_TRACK}

    # ------------------------------------------------------------------------- calls

    def test_a_tool_outside_the_session_allowlist_is_refused_though_the_registry_has_it(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        token = _token(now, tenant_id, merchant_id, scopes=frozenset({scope_for("catalogue.read")}))
        server, opened = self._open(cp_session, token)
        assert ToolName.ORDER_TRACK in TOOLS
        call = replace(_call(ToolName.ORDER_TRACK, order_id=str(uuid7())), requested_at=now)
        with pytest.raises(AuthenticationRejected) as caught:
            server.admit_call(cp_session, opened, call)
        assert caught.value.reason == "tool_not_in_session_allowlist"

    def test_an_unknown_tool_name_is_refused_rather_than_dispatched(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        server, opened = self._open(cp_session, _token(now, tenant_id, merchant_id))
        call = ToolCall(
            tool="payment.execute",
            arguments={"amount_minor": 39500},
            nonce=uuid.uuid4().hex,
            requested_at=now,
        )
        with pytest.raises(SchemaRejected) as caught:
            server.admit_call(cp_session, opened, call)
        assert caught.value.reason == "tool_not_in_registry"

    def test_a_replayed_call_nonce_is_refused_not_answered(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        server, opened = self._open(cp_session, _token(now, tenant_id, merchant_id))
        call = replace(_call(ToolName.CATALOGUE_SEARCH, query="milk"), requested_at=now)
        server.admit_call(cp_session, opened, call)
        with pytest.raises(ReplayRejected) as caught:
            server.admit_call(cp_session, opened, call)
        assert caught.value.reason == "nonce_already_presented"

    def test_a_call_nonce_cannot_burn_the_replay_claim_a_later_access_token_needs(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """Both guards ride one nonce store, so each side has to own a namespace in it.

        Sharing a flat space means a caller holding any session can spend the key a future
        token's ``jti`` will be claimed under, and refuse that token a session it was
        entitled to -- a denial of service against its own client id that leaves the token
        looking replayed. The call here names exactly the string the session guard would
        use, and the token that follows opens a session regardless.
        """
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        client = "shared-client"
        jti = "the-next-token-id"
        server, opened = self._open(
            cp_session, _token(now, tenant_id, merchant_id, client_id=client)
        )
        collision = replace(_call(ToolName.CATALOGUE_SEARCH, query="milk"), nonce=f"session:{jti}")
        server.admit_call(cp_session, opened, replace(collision, requested_at=now))

        _, later = self._open(
            cp_session, _token(now, tenant_id, merchant_id, client_id=client, token_id=jti)
        )
        assert later.token_id == jti

    def test_a_flood_of_arguments_cannot_choose_the_size_of_an_immutable_audit_row(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """The ask is the evidence, but its size is not the caller's to decide.

        The arrival row is written before the call has been validated, which is right. It
        means a request refused for sending a thousand fields would otherwise write all
        thousand into a hash-chained row that can never be pruned. What a reviewer needs
        from such an ask is how many fields it carried, and that survives.
        """
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        server, opened = self._open(cp_session, _token(now, tenant_id, merchant_id))
        flood = {f"field_{index}": "x" * 4096 for index in range(1000)}
        call = ToolCall(
            tool=ToolName.CATALOGUE_SEARCH.value,
            arguments=flood,
            nonce=uuid.uuid4().hex,
            requested_at=now,
        )
        with pytest.raises(SchemaRejected) as caught:
            server.admit_call(cp_session, opened, call)
        assert caught.value.reason == "too_many_arguments"

        received = next(
            row
            for row in self._protocol_events(cp_session, tenant_id)
            if row.event_type == "protocol.received"
        )
        recorded = received.payload["request"]["arguments"]
        assert len(recorded) == mcp.MAX_ARGUMENTS_PER_CALL
        assert received.payload["request"]["arguments_omitted"] == 1000 - mcp.MAX_ARGUMENTS_PER_CALL
        assert all(len(value) <= 512 for value in recorded.values())

    def test_a_call_older_than_the_freshness_window_is_refused_by_the_database_clock(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        server, opened = self._open(cp_session, _token(now, tenant_id, merchant_id))
        stale = replace(
            _call(ToolName.CATALOGUE_SEARCH, query="milk"),
            requested_at=now - timedelta(minutes=30),
        )
        with pytest.raises(ReplayRejected) as caught:
            server.admit_call(cp_session, opened, stale)
        assert caught.value.reason == "request_outside_freshness_window"

    def test_an_expired_session_is_refused(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        server, opened = self._open(cp_session, _token(now, tenant_id, merchant_id))
        stale_session = replace(
            opened,
            established_at=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1),
        )
        call = replace(_call(ToolName.CATALOGUE_SEARCH, query="milk"), requested_at=now)
        with pytest.raises(AuthenticationRejected) as caught:
            server.admit_call(cp_session, stale_session, call)
        assert caught.value.reason == "session_expired"

    def test_describe_tools_shows_only_what_this_session_may_call(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        token = _token(now, tenant_id, merchant_id, scopes=frozenset({scope_for("catalogue.read")}))
        server, opened = self._open(cp_session, token)
        described = {entry["name"] for entry in server.describe_tools(opened)}
        assert described == {"catalogue.search", "catalogue.product", "inventory.check"}
        assert "checkout.submit_approved" not in described

    def test_a_credential_smuggled_into_an_argument_is_dropped_before_an_intent_exists(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """Specification 17.3's fifth bullet: no token passthrough, in either direction.

        A model cannot hand this server a credential to forward, because the tool's schema
        has nowhere to put one. What it sends is dropped, recorded as dropped, and never
        appears in the intent that the platform's services act on.
        """
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        server, opened = self._open(cp_session, _token(now, tenant_id, merchant_id))
        call = replace(
            _call(
                ToolName.CATALOGUE_SEARCH,
                query="milk",
                authorization="Bearer eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJhIn0.c2ln",
                api_key="rzp_test_ABCDEFGHIJ",
                webhook_secret="whsec_ABCDEFGHIJ",  # noqa: S106 - the point of the test
            ),
            requested_at=now,
        )
        admitted = server.admit_call(cp_session, opened, call)
        assert dict(admitted.intent.arguments) == {"query": "milk"}
        assert admitted.arguments.ignored == ("api_key", "authorization", "webhook_secret")

    def test_a_session_for_one_tenant_cannot_be_driven_against_another_tenants_transaction(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """Tenant confusion fails on the first line of the call, not somewhere downstream.

        The token names a tenant nobody bound to this transaction. Nothing about the tool,
        the scopes or the arguments matters: the evidence row cannot be written, so the
        call cannot proceed. It raises rather than returning a rejection because a
        transaction bound to the wrong tenant is a construction bug in the layer above.
        """
        _, merchant_id = cp_tenant
        foreign_tenant = uuid.uuid4()
        now = database_now(cp_session)
        server, opened = self._open(cp_session, _token(now, foreign_tenant, merchant_id))
        assert opened.tenant_id == foreign_tenant
        call = replace(_call(ToolName.CATALOGUE_SEARCH, query="milk"), requested_at=now)
        with pytest.raises(AuditTenantError):
            server.admit_call(cp_session, opened, call)

    # ----------------------------------------------------------------- the tenant rule

    def test_a_model_supplied_tenant_id_is_ignored_and_never_reaches_the_kernel(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """Specification 17.3, fourth bullet, in the only form that matters.

        The call names somebody else's tenant, somebody else's merchant and a principal id
        of its own invention. All three are dropped before an intent exists, and the
        principal that reaches admission carries the tenant the access token named.
        """
        tenant_id, merchant_id = cp_tenant
        attacker_tenant = uuid.uuid4()
        now = database_now(cp_session)
        admission = _RecordingAdmission()
        server, opened = self._open(
            cp_session, _token(now, tenant_id, merchant_id), admission=admission
        )
        checkout_id = uuid7()
        call = replace(
            _call(
                ToolName.CHECKOUT_SUBMIT_APPROVED,
                checkout_id=str(checkout_id),
                version=1,
                content_hash="Zm9vYmFyYmF6cXV4MTIzNDU2",
                tenant_id=str(attacker_tenant),
                merchant_id=str(uuid7()),
                principal_id="protocol:mcp:someone-else",
            ),
            requested_at=now,
        )
        admitted = server.admit_call(cp_session, opened, call)

        assert "tenant_id" not in admitted.intent.arguments
        assert admitted.arguments.ignored == ("merchant_id", "principal_id", "tenant_id")
        assert admitted.intent.caller.tenant_id == tenant_id

        decision = server.submit_approved(cp_session, opened, admitted)
        assert decision.allowed
        (reached,) = admission.calls
        principal: AgentPrincipal = reached["principal"]
        assert principal.tenant_id == tenant_id
        assert principal.tenant_id != attacker_tenant
        assert principal.merchant_id == merchant_id
        assert principal.principal_id == f"protocol:mcp:{opened.client_id}"
        assert reached["checkout_id"] == checkout_id
        assert "tenant_id" not in reached

    def test_an_intent_from_this_adapter_never_names_an_amount(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        server, opened = self._open(cp_session, _token(now, tenant_id, merchant_id))
        for name, arguments in (
            (ToolName.REFUND_PROPOSE, {"order_id": str(uuid7()), "reason": "missing item"}),
            (
                ToolName.CHECKOUT_SUBMIT_APPROVED,
                {
                    "checkout_id": str(uuid7()),
                    "version": 1,
                    "content_hash": "Zm9vYmFyYmF6cXV4MTIzNDU2",
                },
            ),
        ):
            call = replace(_call(name, **arguments, amount_minor=39500), requested_at=now)
            admitted = server.admit_call(cp_session, opened, call)
            assert admitted.intent.amount is None
            assert "amount_minor" in admitted.arguments.ignored

    # ---------------------------------------------------------------- kernel boundary

    def test_only_the_approved_submit_may_reach_the_kernel(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        admission = _RecordingAdmission()
        server, opened = self._open(
            cp_session, _token(now, tenant_id, merchant_id), admission=admission
        )
        call = replace(_call(ToolName.CATALOGUE_SEARCH, query="milk"), requested_at=now)
        admitted = server.admit_call(cp_session, opened, call)
        with pytest.raises(StateRejected) as caught:
            server.submit_approved(cp_session, opened, admitted)
        assert caught.value.reason == "tool_does_not_reach_the_kernel"
        assert admission.calls == []

    def test_a_forged_admitted_call_is_refused_because_its_spec_is_not_the_registrys(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """The last way a forbidden tool could be conjured: hand-build the spec.

        A copy of the registry's own row is refused too, because the check is identity: a
        copy is exactly what an attacker would build to change one field.
        """
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        server, opened = self._open(cp_session, _token(now, tenant_id, merchant_id))
        call = replace(
            _call(
                ToolName.CHECKOUT_SUBMIT_APPROVED,
                checkout_id=str(uuid7()),
                version=1,
                content_hash="Zm9vYmFyYmF6cXV4MTIzNDU2",
            ),
            requested_at=now,
        )
        admitted = server.admit_call(cp_session, opened, call)
        counterfeit = ToolSpec(
            name=ToolName.CHECKOUT_SUBMIT_APPROVED,
            intent=IntentKind.SUBMIT_APPROVED,
            capability="checkout.submit_approved",
            summary="a spec the registry never held",
        )
        with pytest.raises(AuthenticationRejected) as caught:
            AdmittedCall(
                spec=counterfeit,
                intent=admitted.intent,
                interaction=admitted.interaction,
                arguments=admitted.arguments,
                session_id=opened.session_id,
            )
        assert caught.value.reason == "tool_spec_is_not_from_the_registry"

    def test_a_call_admitted_under_one_session_cannot_be_submitted_under_another(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """An admission is a set of conclusions about one session, and it is not portable.

        Every gate the call passed -- the allowlist, the capability, the tenant the
        evidence chain is bound to -- was tested against the session that presented it.
        Handing the result to a second session asks the kernel to act for a caller that was
        never checked, under an interaction recorded against the first caller's principal.
        Both sessions here are legitimate and both belong to the same tenant, which is the
        point: nothing about either one is wrong, only their pairing.
        """
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        admission = _RecordingAdmission()
        server, first = self._open(
            cp_session,
            _token(now, tenant_id, merchant_id, client_id="client-a"),
            admission=admission,
        )
        second_server, second = self._open(
            cp_session,
            _token(now, tenant_id, merchant_id, client_id="client-b"),
            admission=admission,
        )
        assert second_server is not server
        admitted = server.admit_call(cp_session, first, _approved_submit(now))

        with pytest.raises(AuthenticationRejected) as caught:
            server.submit_approved(cp_session, second, admitted)
        assert caught.value.reason == "admitted_call_belongs_to_another_session"
        assert admission.calls == []
        assert server.submit_approved(cp_session, first, admitted).allowed

    def test_a_session_that_expires_between_admission_and_submission_may_not_submit(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """The half that reaches money re-reads the clock rather than trusting the earlier one."""
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        admission = _RecordingAdmission()
        server, opened = self._open(
            cp_session, _token(now, tenant_id, merchant_id), admission=admission
        )
        admitted = server.admit_call(cp_session, opened, _approved_submit(now))
        expired = replace(
            opened,
            established_at=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1),
        )
        with pytest.raises(AuthenticationRejected) as caught:
            server.submit_approved(cp_session, expired, admitted)
        assert caught.value.reason == "session_expired"
        assert admission.calls == []

    def test_a_refused_submission_names_the_check_that_refused_it_in_the_same_chain(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """A refusal at the kernel boundary is evidence too, on the interaction it refused."""
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        server, opened = self._open(cp_session, _token(now, tenant_id, merchant_id))
        call = replace(_call(ToolName.CATALOGUE_SEARCH, query="milk"), requested_at=now)
        admitted = server.admit_call(cp_session, opened, call)
        with pytest.raises(StateRejected):
            server.submit_approved(cp_session, opened, admitted)
        events = kernel_audit.read_stream(
            cp_session,
            tenant=tenant_id,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=admitted.interaction.interaction_id,
        )
        rejected = next(e for e in events if e.payload["stage"] == EvidenceStage.REJECTED.value)
        assert rejected.payload["reason"] == "tool_does_not_reach_the_kernel"
        assert rejected.payload["failed_at"] == EvidenceStage.DECIDED.value

    def test_a_kernel_denial_comes_back_as_a_decision_rather_than_an_error(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """ADR 0003 D15 at a protocol edge: a denial is the system working."""
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        admission = _RecordingAdmission(allowed=False)
        server, opened = self._open(
            cp_session, _token(now, tenant_id, merchant_id), admission=admission
        )
        call = replace(
            _call(
                ToolName.CHECKOUT_SUBMIT_APPROVED,
                checkout_id=str(uuid7()),
                version=2,
                content_hash="Zm9vYmFyYmF6cXV4MTIzNDU2",
            ),
            requested_at=now,
        )
        admitted = server.admit_call(cp_session, opened, call)
        decision = server.submit_approved(cp_session, opened, admitted)
        assert decision.allowed is False
        assert decision.code is RecoveryCode.REAPPROVAL_REQUIRED

    # ------------------------------------------------------------------------ evidence

    def test_every_stage_of_an_admitted_call_lands_in_one_gapless_evidence_chain(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        server, opened = self._open(cp_session, _token(now, tenant_id, merchant_id))
        call = replace(
            _call(ToolName.CATALOGUE_SEARCH, query="milk", tenant_id=str(uuid.uuid4())),
            requested_at=now,
        )
        admitted = server.admit_call(cp_session, opened, call)
        server.answer(cp_session, admitted, {"hits": [{"sku": "MILK-DAIRY-001"}]})

        events = kernel_audit.read_stream(
            cp_session,
            tenant=tenant_id,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=admitted.interaction.interaction_id,
        )
        stages = [event.payload["stage"] for event in events]
        assert stages == [
            EvidenceStage.RECEIVED.value,
            EvidenceStage.AUTHENTICATED.value,
            EvidenceStage.VALIDATED.value,
            EvidenceStage.VERIFIED.value,
            EvidenceStage.MAPPED.value,
            EvidenceStage.ANSWERED.value,
        ]
        validated = next(e for e in events if e.payload["stage"] == EvidenceStage.VALIDATED.value)
        assert validated.payload["ignored"] == ["tenant_id"]
        verification = kernel_audit.verify_chain(
            cp_session,
            tenant=tenant_id,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=admitted.interaction.interaction_id,
        )
        assert verification.intact

    def test_a_refused_call_keeps_the_ask_and_names_the_check_that_refused_it(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """Specification 13.3: a rejection must be reconstructable, not merely logged.

        The tool asked for is one specification 17.3 forbids outright. What the evidence
        chain has to show afterwards is both halves: the request verbatim, including the
        amount the caller tried to name, and which check refused it.
        """
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        server, opened = self._open(cp_session, _token(now, tenant_id, merchant_id))
        call = ToolCall(
            tool="refund.execute",
            arguments={"amount_minor": 39500},
            nonce=uuid.uuid4().hex,
            requested_at=now,
        )
        with pytest.raises(SchemaRejected):
            server.admit_call(cp_session, opened, call)

        rows = self._protocol_events(cp_session, tenant_id)
        received = next(row for row in rows if row.event_type == "protocol.received")
        assert received.payload["request"]["tool"] == "refund.execute"
        assert received.payload["request"]["arguments"] == {"amount_minor": 39500}
        rejected = next(row for row in rows if row.event_type == "protocol.rejected")
        assert rejected.payload["failed_at"] == EvidenceStage.VALIDATED.value
        assert rejected.payload["reason"] == "tool_not_in_registry"
        assert rejected.payload["details"]["tool"] == "refund.execute"

    @staticmethod
    def _protocol_events(session: Session, tenant_id: uuid.UUID) -> list[AuditEvent]:
        """Every protocol evidence row for this tenant, in sequence.

        Read through the ORM rather than through ``audit.read_stream`` because a refused
        call raises before its interaction id is handed back -- which is correct, and is
        also why a reviewer investigating a refusal starts from the tenant's stream rather
        than from an identifier the caller never received.
        """
        return list(
            session.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.tenant_id == tenant_id,
                    AuditEvent.aggregate_type == AGGREGATE_TYPE,
                )
                .order_by(AuditEvent.seq)
            )
            .scalars()
            .all()
        )

    def test_a_tool_answer_and_the_evidence_row_carry_the_same_screened_content(
        self, cp_session: Session, cp_tenant: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """One screen, two consumers, so the audit chain cannot hold what the wire may not."""
        tenant_id, merchant_id = cp_tenant
        now = database_now(cp_session)
        server, opened = self._open(cp_session, _token(now, tenant_id, merchant_id))
        call = replace(_call(ToolName.ORDER_TRACK, order_id=str(uuid7())), requested_at=now)
        admitted = server.admit_call(cp_session, opened, call)
        with pytest.raises(CredentialLeakError):
            server.answer(cp_session, admitted, {"webhook_secret": "whsec_leaked"})
        answered = server.answer(cp_session, admitted, {"state": "CONFIRMED"})
        events = kernel_audit.read_stream(
            cp_session,
            tenant=tenant_id,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=admitted.interaction.interaction_id,
        )
        recorded = next(
            event for event in events if event.payload["stage"] == EvidenceStage.ANSWERED.value
        )
        assert recorded.payload["content"] == dict(answered.content)
