"""The governed MCP tool server, specification 17.

What this server does is translate. A tool call arrives, and what leaves is a
:class:`~commerce_protocols.core.intent.ProtocolIntent` -- the same typed internal command
UCP, AP2 and ACP produce -- plus an evidence chain recording every step of specification
13.1 that got it there. What this server does *not* do is act. It holds no HTTP client, no
provider SDK, no raw SQL surface and no credential; the only session it touches is the
caller's transaction, and it touches it for exactly three things -- the database clock, the
replay guard and the audit chain -- none of which it implements itself.

That is why there is no ``execute`` here, and why the single method that reaches money is
:meth:`GovernedToolServer.submit_approved`. It takes an already-admitted call, refuses it
unless the call is ``checkout.submit_approved``, and hands the *session's* principal --
never anything derived from a tool argument -- to a :class:`KernelAdmission` port whose
signature cannot carry an amount, a tenant or a credential. The kernel decides. A denial
comes back as a structured ``AdmissionDecision`` and is recorded and returned as such, because
ADR 0003 D15 is emphatic that a denial is the system working rather than an error.

Dispatch, and what it deliberately is not
-----------------------------------------
The MCP wire protocol names tools by string, so at some point a string has to become a
decision. Here it becomes a member of a closed enum through
:func:`~commerce_protocols.mcp.tools.resolve_tool`, or the request stops. There is no
handler map keyed by arbitrary strings, no ``getattr`` on a name a model chose, no prefix
that forwards the remainder to a provider, and no callable the caller can pass in to be
invoked for a tool of its choosing. A reviewer looking for the dangerous tool will not find
it guarded; they will find that there is nowhere for it to have been registered.

The tenant, once
----------------
Specification 17.3's fourth bullet is enforced in two places that cannot disagree. The
arguments a model sends are normalised against a closed per-tool schema, so ``tenant_id``
is dropped before an intent exists -- and the drop is recorded, because an ignored field
that leaves no trace is indistinguishable from one that was never sent. Separately, the
tenant that scopes the transaction and the principal that reaches admission both come from
:class:`~commerce_protocols.mcp.authorization.McpSession`, which was built from the access
token. There is no code path from an argument to either.
"""

from __future__ import annotations

import typing
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from commerce_domain import canonical_hash
from sqlalchemy.orm import Session
from transaction_kernel import AdmissionDecision, AgentPrincipal

from ..core import (
    AGGREGATE_TYPE,
    AuthenticationRejected,
    EvidenceStage,
    IntentKind,
    Protocol,
    ProtocolIntent,
    ProtocolInteraction,
    ProtocolRejection,
    ReplayRejected,
    SchemaRejected,
    StateRejected,
    assert_fresh,
    assert_never_consents,
    claim_nonce,
    database_now,
    open_interaction,
)
from .authorization import McpSession, TokenIntrospector, open_session
from .results import ToolResult
from .tools import (
    MAX_ARGUMENTS_PER_CALL,
    TOOLS,
    NormalisedArguments,
    ToolName,
    ToolSpec,
    resolve_tool,
)

__all__ = [
    "ENDPOINT",
    "AdmittedCall",
    "GovernedToolServer",
    "KernelAdmission",
    "ToolCall",
]

#: What the evidence chain records as the endpoint. One constant, so a reviewer filtering
#: protocol evidence for MCP traffic reads the same string the server writes.
ENDPOINT: Final[str] = "mcp:tools/call"

#: How many characters of a rejected tool name are echoed back. A model that sends a
#: megabyte as a tool name should not get a megabyte into the audit chain.
_MAX_ECHOED_NAME: Final[int] = 64

#: How many characters of a single argument value reach the evidence row. Long enough for
#: the ask to be legible, short enough that a row is bounded whatever a model typed.
_MAX_ECHOED_VALUE: Final[int] = 512

#: The namespace a per-call nonce is claimed under, inside the client's nonce space. Its
#: counterpart in :mod:`.authorization` namespaces the token ``jti`` the same way, and the
#: pair is what keeps them separate: without it a caller could spend a tool call's nonce on
#: the string a future access token's ``jti`` will be claimed under, and refuse that token
#: a session it was entitled to. Two namespaces, so one caller's traffic cannot poison the
#: other half of its own guard.
_CALL_CLAIM_PREFIX: Final[str] = "call"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One ``tools/call`` as it arrived, before anything about it has been believed.

    ``tool`` is a plain string on purpose: it is what the model sent, and typing it as
    :class:`ToolName` here would move the moment of validation somewhere this server cannot
    see. ``arguments`` is likewise unvalidated, which is why nothing reads it before
    :meth:`ToolSpec.normalise` has decided which of its keys exist.

    ``nonce`` and ``requested_at`` are the replay and freshness material of specification
    16.3. They are required rather than optional: a surface where the guard is opt-in is a
    surface where somebody's integration opts out.
    """

    tool: str
    arguments: Mapping[str, Any]
    nonce: str
    requested_at: datetime
    call_id: str | None = None


class KernelAdmission(typing.Protocol):
    """The one way this server can reach money, and the shape of that reach.

    Read the signature as a security argument. There is no ``amount``, so a model cannot
    name one; the amount is whatever the locked checkout version says, and that version is
    the one the buyer approved. There is no ``tenant_id``, so a model cannot name one; the
    tenant travels inside ``principal``, which the session built from the access token.
    There is no ``capabilities`` and no credential, so nothing a model says can widen what
    the caller may do or be forwarded to a provider.

    The implementation is the platform's own admission path -- the same one the trusted
    buyer surface and the agent runtime call. This package defines the port and never the
    adapter, because an MCP server that had its own route to the kernel would be a second
    admission, and specification 17 exists to prevent exactly that.
    """

    def admit_approved(
        self,
        session: Session,
        *,
        principal: AgentPrincipal,
        checkout_id: uuid.UUID,
        version: int,
        content_hash: str,
    ) -> AdmissionDecision: ...


@dataclass(frozen=True, slots=True)
class AdmittedCall:
    """A call that passed every gate: a registered tool, a fresh nonce, a typed intent.

    ``__post_init__`` checks that ``spec`` is the object the registry holds, by identity.
    That closes the last way a forbidden tool could be conjured: a caller that hand-built a
    :class:`ToolSpec` and handed it to :meth:`GovernedToolServer.submit_approved` would
    otherwise be dispatching a spec the registry never contained. Identity rather than
    equality, because equality would accept a copy with the same name and a different
    intent.

    ``session_id`` names the session that earned this admission, and it is what makes the
    admission non-transferable. Every check that let the call through -- the allowlist, the
    capability, the tenant the evidence chain is bound to -- was made against one session,
    so an admitted call handed to a *different* session is a set of conclusions attached to
    a set of premises that were never tested together. The kernel would then be asked to
    act for one caller on a call another caller was permitted to make, and the audit chain
    would record it under the first caller's principal.
    """

    spec: ToolSpec
    intent: ProtocolIntent
    interaction: ProtocolInteraction
    arguments: NormalisedArguments
    session_id: uuid.UUID

    def __post_init__(self) -> None:
        if TOOLS.get(self.spec.name) is not self.spec:
            raise AuthenticationRejected(
                "tool_spec_is_not_from_the_registry", tool=str(self.spec.name)[:_MAX_ECHOED_NAME]
            )


def _evidence_safe(arguments: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    """The arguments as the audit chain can hold them, verbatim where that is possible.

    The chain canonicalises to JSON, and a model may send anything. Scalars are kept
    exactly -- the ask is the evidence, and a redacted ask proves nothing -- while anything
    else is replaced by its type name. Replacing rather than dropping matters: a reviewer
    must be able to see that a field was sent even when its value could not be recorded.

    Verbatim stops at :data:`~commerce_protocols.mcp.tools.MAX_ARGUMENTS_PER_CALL` fields,
    and the count that did not fit is returned rather than the fields. The row is written
    before the call has been validated, which is right -- the bytes arrived and the arrival
    is the evidence -- but it means the size of an immutable, hash-chained row would
    otherwise be chosen by whoever sent the request. A caller that floods the surface must
    not get to decide how much of the audit stream its refusal consumes, and the number of
    fields it sent is the part of that ask a reviewer actually needs.
    """
    safe: dict[str, Any] = {}
    omitted = 0
    for key, value in arguments.items():
        if len(safe) >= MAX_ARGUMENTS_PER_CALL:
            omitted += 1
            continue
        name = str(key)[:_MAX_ECHOED_NAME]
        if value is None or isinstance(value, bool | int | str):
            safe[name] = value if not isinstance(value, str) else value[:_MAX_ECHOED_VALUE]
        else:
            safe[name] = f"<{type(value).__name__}>"
    return safe, omitted


class GovernedToolServer:
    """The MCP surface. Translates governed tool calls into intents; executes nothing.

    Every public method is named in the class body below, and the list is the argument:
    open a session, describe the tools that session may call, admit one call, submit an
    already-approved checkout to admission, answer. There is no approve, no pay, no refund,
    no capture, no revoke and no reconcile -- not guarded, not present.
    """

    __slots__ = ("_admission", "_introspector", "_resource")

    def __init__(
        self,
        *,
        resource: str,
        introspector: TokenIntrospector,
        admission: KernelAdmission,
    ) -> None:
        """``resource`` is this server's RFC 8707 resource indicator: what it answers to.

        It is required rather than defaulted. A resource server that guesses its own
        identity cannot refuse a token minted for a different one, and an audience check
        against a value nobody chose is theatre.
        """
        if not resource:
            raise ValueError(
                "an MCP resource server must state the resource indicator it answers to; "
                "without one the audience check has nothing to compare against"
            )
        self._resource = resource
        self._introspector = introspector
        self._admission = admission

    @property
    def resource(self) -> str:
        return self._resource

    def open_session(
        self,
        session: Session,
        *,
        presented: str,
        announced_version: str | None,
        registered_tools: frozenset[ToolName] | None = None,
        correlation_id: uuid.UUID | None = None,
    ) -> McpSession:
        """Establish a session from a presented access token. See :mod:`.authorization`."""
        return open_session(
            session,
            introspector=self._introspector,
            presented=presented,
            resource=self._resource,
            announced_version=announced_version,
            registered_tools=registered_tools,
            correlation_id=correlation_id,
        )

    def describe_tools(self, mcp_session: McpSession) -> tuple[dict[str, Any], ...]:
        """The ``tools/list`` answer for one session: its allowlist, and nothing wider.

        A tool the session may not call is not described as forbidden, it is not described
        at all. A model that cannot see a tool does not spend a turn discovering it is
        denied, and a tool list that enumerates what the caller may not have is a map of
        the platform for anyone who obtains a low-scoped token.
        """
        return tuple(TOOLS[name].to_schema() for name in sorted(mcp_session.allowed_tools, key=str))

    def admit_call(self, session: Session, mcp_session: McpSession, call: ToolCall) -> AdmittedCall:
        """Run one tool call through specification 13.1's pipeline, or refuse it.

        Nothing here executes the tool. What comes back is the typed intent the platform's
        own services honour, so a bug in this file can refuse a legitimate request or fail
        to refuse an illegitimate one -- but it cannot move money, because there is nothing
        in this method that could.

        Evidence is appended for every stage, including the refusals, in the caller's
        transaction. A refusal therefore commits with its evidence or not at all.

        One consequence worth naming: the first thing this method does is write an evidence
        row for the session's tenant, and ``transaction_kernel.audit`` refuses an event
        whose tenant is not the transaction's bound one. So a session established for one
        tenant cannot be driven against a transaction bound to another -- the call fails on
        its first line, loudly, before any tool has been resolved.
        """
        interaction = open_interaction(
            pin=mcp_session.pin,
            tenant_id=mcp_session.tenant_id,
            principal=mcp_session.principal,
            correlation_id=mcp_session.caller.correlation_id,
        )
        recorded, omitted = _evidence_safe(call.arguments)
        interaction.record_received(
            session,
            endpoint=ENDPOINT,
            announced_version=mcp_session.pin.version,
            body={
                "tool": str(call.tool)[:_MAX_ECHOED_NAME],
                "call_id": call.call_id,
                "arguments": recorded,
                "arguments_omitted": omitted,
            },
            headers_seen={
                "mcp-session-id": str(mcp_session.session_id),
                "mcp-client-id": mcp_session.client_id,
            },
            credential=mcp_session.token_fingerprint,
        )
        try:
            return self._admit(session, mcp_session, call, interaction)
        except ProtocolRejection as rejection:
            interaction.record_rejection(
                session,
                stage=_STAGE_FOR.get(type(rejection), EvidenceStage.REJECTED),
                reason=rejection.reason,
                code=rejection.code.value,
                details=rejection.details,
            )
            raise

    def _admit(
        self,
        session: Session,
        mcp_session: McpSession,
        call: ToolCall,
        interaction: ProtocolInteraction,
    ) -> AdmittedCall:
        """The pipeline itself, so the caller above owns evidence and this owns decisions."""
        now = database_now(session)
        if not mcp_session.is_live_at(now):
            raise AuthenticationRejected(
                "session_expired", expired_at=mcp_session.expires_at.isoformat()
            )
        assert_never_consents(mcp_session.principal)

        spec = resolve_tool(call.tool)
        if not mcp_session.may_call(spec.name):
            raise AuthenticationRejected(
                "tool_not_in_session_allowlist",
                tool=spec.name.value,
                allowlist=sorted(name.value for name in mcp_session.allowed_tools),
            )
        if not mcp_session.principal.can(spec.capability):
            raise AuthenticationRejected(
                "capability_missing", tool=spec.name.value, capability=spec.capability
            )
        interaction.record(
            session,
            EvidenceStage.AUTHENTICATED,
            tool=spec.name.value,
            client_id=mcp_session.client_id,
            authenticated_by=mcp_session.caller.authenticated_by,
            capability=spec.capability,
        )

        arguments = spec.normalise(call.arguments)
        interaction.record(
            session,
            EvidenceStage.VALIDATED,
            tool=spec.name.value,
            accepted=dict(arguments.accepted),
            ignored=list(arguments.ignored),
        )

        assert_fresh(session, timestamp=call.requested_at)
        claim_nonce(
            session,
            protocol=Protocol.MCP,
            client_id=mcp_session.client_id,
            nonce=f"{_CALL_CLAIM_PREFIX}:{call.nonce}",
            request_digest=canonical_hash(
                {
                    "tool": spec.name.value,
                    "session": str(mcp_session.session_id),
                    "arguments": dict(arguments.accepted),
                }
            ),
        )
        interaction.record(
            session, EvidenceStage.VERIFIED, tool=spec.name.value, nonce_claimed=True
        )

        intent = self._map(mcp_session, spec, arguments, call, interaction)
        interaction.record(
            session,
            EvidenceStage.MAPPED,
            tool=spec.name.value,
            intent=intent.kind.value,
            moves_money=intent.moves_money,
            names_amount=intent.amount is not None,
        )
        return AdmittedCall(
            spec=spec,
            intent=intent,
            interaction=interaction,
            arguments=arguments,
            session_id=mcp_session.session_id,
        )

    def _map(
        self,
        mcp_session: McpSession,
        spec: ToolSpec,
        arguments: NormalisedArguments,
        call: ToolCall,
        interaction: ProtocolInteraction,
    ) -> ProtocolIntent:
        """Step 4: the typed internal command, built only from things the session owns.

        ``caller`` is the session's, ``pin`` is the session's, ``correlation_id`` is the
        session's. The only contribution the model makes is ``arguments``, which have been
        reduced to the tool's declared schema, and the identifiers promoted out of them.

        ``amount`` is written as ``None`` explicitly rather than left to the default. It is
        the one field whose absence is a specification requirement rather than a
        convenience, and a reader of this constructor should see it stated.
        """
        accepted = arguments.accepted
        return ProtocolIntent(
            kind=spec.intent,
            caller=mcp_session.caller,
            pin=mcp_session.pin,
            correlation_id=mcp_session.caller.correlation_id,
            external_id=call.call_id,
            checkout_id=_as_uuid(accepted.get("checkout_id")),
            checkout_version=_as_int(accepted.get("version")),
            content_hash=_as_str(accepted.get("content_hash")),
            order_id=_as_uuid(accepted.get("order_id")),
            amount=None,
            arguments=accepted,
            raw_reference=f"{AGGREGATE_TYPE}:{interaction.interaction_id}",
        )

    def submit_approved(
        self, session: Session, mcp_session: McpSession, admitted: AdmittedCall
    ) -> AdmissionDecision:
        """Hand an already-approved checkout version to the platform's kernel admission.

        The only method on this class that reaches money, and it still does not decide
        anything: admission does. What this method guarantees is that the thing reaching
        admission is a principal built from an access token, a checkout the caller named by
        id, and a version and hash the buyer already approved -- and that a call for any
        other tool cannot get here, because the intent check refuses it before the port is
        reached.

        The decision is recorded verbatim, allowed or denied, and returned unchanged. A
        denial is not raised: ADR 0003 D15 makes a kernel denial a successful, structured
        answer everywhere else in the platform, and a protocol adapter that turned one into
        an exception would be the surface where that stopped being true.

        The session binding is checked before anything else and without writing evidence.
        An admitted call and a session that do not belong together are two different
        callers, and the interaction's chain is bound to the tenant of the first of them --
        so recording the refusal there would be writing one caller's mistake into the
        other's audit stream, and against a transaction bound to neither.
        """
        if admitted.session_id != mcp_session.session_id:
            raise AuthenticationRejected(
                "admitted_call_belongs_to_another_session",
                admitted_under=str(admitted.session_id),
                presented_by=str(mcp_session.session_id),
            )
        try:
            return self._submit(session, mcp_session, admitted)
        except ProtocolRejection as rejection:
            admitted.interaction.record_rejection(
                session,
                stage=_STAGE_FOR.get(type(rejection), EvidenceStage.REJECTED),
                reason=rejection.reason,
                code=rejection.code.value,
                details=rejection.details,
            )
            raise

    def _submit(
        self, session: Session, mcp_session: McpSession, admitted: AdmittedCall
    ) -> AdmissionDecision:
        """The submission's own checks, so the caller above owns evidence and this owns them.

        Liveness is re-read from the database clock rather than inherited from admission.
        A session that expired between the call being admitted and its being submitted is a
        session whose authority ran out, and the submission is the half that reaches money;
        trusting the earlier check would make the expiry a property of when a caller chose
        to make the second request.
        """
        now = database_now(session)
        if not mcp_session.is_live_at(now):
            raise AuthenticationRejected(
                "session_expired", expired_at=mcp_session.expires_at.isoformat()
            )
        if admitted.spec.intent is not IntentKind.SUBMIT_APPROVED:
            raise StateRejected(
                "tool_does_not_reach_the_kernel",
                tool=admitted.spec.name.value,
                intent=admitted.spec.intent.value,
            )
        intent = admitted.intent
        if intent.checkout_id is None or intent.checkout_version is None or not intent.content_hash:
            raise SchemaRejected("approved_submit_is_incomplete", tool=admitted.spec.name.value)
        assert_never_consents(mcp_session.principal)
        decision = self._admission.admit_approved(
            session,
            principal=mcp_session.principal,
            checkout_id=intent.checkout_id,
            version=intent.checkout_version,
            content_hash=intent.content_hash,
        )
        admitted.interaction.record_decision(session, decision)
        return decision

    def answer(
        self,
        session: Session,
        admitted: AdmittedCall,
        content: Mapping[str, Any],
    ) -> ToolResult:
        """Step 7: the protocol-shaped answer, screened and recorded.

        The screen that keeps a credential off the wire is the same one that keeps it out
        of the audit chain, and that is deliberate. The chain is immutable, so a secret
        written into it could not be redacted afterwards even if somebody wanted to; making
        the recorded content and the returned content the same screened object means there
        is no second path where the rule could be weaker.
        """
        result = ToolResult(tool=admitted.spec.name, content=content)
        admitted.interaction.record(
            session,
            EvidenceStage.ANSWERED,
            tool=result.tool.value,
            content=dict(result.content),
            is_error=result.is_error,
        )
        return result


def _as_uuid(value: str | int | None) -> uuid.UUID | None:
    """A validated ``UUID`` argument, already known to parse. ``None`` stays ``None``."""
    return None if value is None else uuid.UUID(str(value))


def _as_int(value: str | int | None) -> int | None:
    return None if value is None else int(value)


def _as_str(value: str | int | None) -> str | None:
    return None if value is None else str(value)


#: Which pipeline stage each refusal is recorded against. The evidence row's own stage is
#: ``REJECTED``; this is the ``failed_at`` a reviewer sorts by. An authorization failure is
#: recorded at ``AUTHENTICATED`` even though it is detected after the tool name has been
#: parsed, because the question the row answers is "which check refused this", not "how far
#: down the function did it get".
_STAGE_FOR: Final[Mapping[type[ProtocolRejection], EvidenceStage]] = {
    AuthenticationRejected: EvidenceStage.AUTHENTICATED,
    SchemaRejected: EvidenceStage.VALIDATED,
    ReplayRejected: EvidenceStage.VERIFIED,
    StateRejected: EvidenceStage.DECIDED,
}
