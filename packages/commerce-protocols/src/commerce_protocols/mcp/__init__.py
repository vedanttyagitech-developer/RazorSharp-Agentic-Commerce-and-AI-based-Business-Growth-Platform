"""The governed MCP tool server, specification 17.

MCP is the last protocol this platform speaks and the one where the governing rule is
easiest to break by accident. The others carry a signed artifact from a party that knows
what it is asking for; MCP carries whatever a language model decided to type. So the whole
of this package is arranged around one property, and it is a property about design rather
than behaviour: **a tool that can approve, pay, refund, capture, revoke or reconcile does
not exist here.** Not guarded, not flagged, not restricted to a privileged client. Absent.

Read the three modules in this order and the argument is complete.

:mod:`~commerce_protocols.mcp.tools` is the registry. Thirteen tools, one closed ``StrEnum``,
one read-only mapping, and an import-time assertion that the mapping is exactly the enum,
that no tool names an amount or a tenant, and that exactly one tool reaches the kernel. The
forbidden names of specification 17.3 are written out beside it so the absence is legible
rather than inferred.

:mod:`~commerce_protocols.mcp.authorization` is the OAuth 2.1 half of specification 17.4:
audience-bound tokens, a ceiling on declared lifetime, single-use ``jti`` through the core's
replay guard, and scopes that can only narrow. It produces an :class:`McpSession` whose tool
allowlist is fixed at creation and whose principal has been through
``core.identity.principal_for`` -- so it holds no consent capability, however the token was
scoped.

:mod:`~commerce_protocols.mcp.server` is the pipeline. It turns a call into a
``ProtocolIntent`` and an evidence chain, and it executes nothing. Its single money-adjacent
method hands an already-approved checkout to a :class:`KernelAdmission` port whose signature
carries no amount, no tenant and no credential, and the kernel decides.

:mod:`~commerce_protocols.mcp.results` is why a tool result cannot carry a secret: the
screen lives in the constructor, so the prohibition is a type rather than a convention.

What this package deliberately does not contain: an HTTP client, a Razorpay client, a raw
SQL surface, a credential store, and any method whose name states an act it must not be able
to perform. Tests in ``tests/test_cp_mcp.py`` walk the source of every module here and prove
those absences rather than asserting them in prose.
"""

from __future__ import annotations

from .authorization import (
    MAX_TOKEN_LIFETIME,
    MCP_SCOPE_PREFIX,
    SESSION_LIFETIME,
    AccessToken,
    McpSession,
    TokenIntrospector,
    capabilities_for_scopes,
    open_session,
    scope_for,
)
from .results import (
    MAX_RESULT_DEPTH,
    MAX_RESULT_NODES,
    MAX_RESULT_TEXT,
    CredentialLeakError,
    ResultShapeError,
    ToolResult,
    ToolResultError,
    screen,
)
from .server import ENDPOINT, AdmittedCall, GovernedToolServer, KernelAdmission, ToolCall
from .tools import (
    FORBIDDEN_ARGUMENT_NAMES,
    FORBIDDEN_TOOL_NAMES,
    MAX_ARGUMENTS_PER_CALL,
    NEVER_ON_MCP_SURFACE,
    TOOLS,
    ArgumentKind,
    ArgumentSpec,
    NormalisedArguments,
    ToolName,
    ToolSpec,
    public_name_offends,
    resolve_tool,
)

__all__ = [
    "ENDPOINT",
    "FORBIDDEN_ARGUMENT_NAMES",
    "FORBIDDEN_TOOL_NAMES",
    "MAX_ARGUMENTS_PER_CALL",
    "MAX_RESULT_DEPTH",
    "MAX_RESULT_NODES",
    "MAX_RESULT_TEXT",
    "MAX_TOKEN_LIFETIME",
    "MCP_SCOPE_PREFIX",
    "NEVER_ON_MCP_SURFACE",
    "SESSION_LIFETIME",
    "TOOLS",
    "AccessToken",
    "AdmittedCall",
    "ArgumentKind",
    "ArgumentSpec",
    "CredentialLeakError",
    "GovernedToolServer",
    "KernelAdmission",
    "McpSession",
    "NormalisedArguments",
    "ResultShapeError",
    "ToolCall",
    "ToolName",
    "ToolResult",
    "ToolResultError",
    "ToolSpec",
    "TokenIntrospector",
    "capabilities_for_scopes",
    "open_session",
    "public_name_offends",
    "resolve_tool",
    "scope_for",
    "screen",
]
