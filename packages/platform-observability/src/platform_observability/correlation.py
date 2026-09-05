"""One id, carried across an ``await``, so a log line and an audit row can be joined.

Specification 19.13 asks that "one correlation ID reconstructs the entire conversation
... from one place", and section 24.3 puts a correlation id inside every outbox and audit
envelope. Both are already true of the *evidence*: ``commerce_api.deps.RequestContext``
resolves the id from ``X-Correlation-Id`` or mints one, and it travels into
``AgentPrincipal`` and every ``audit_events`` row from there.

What is missing is the *operational* half. A log line emitted three frames down a call
chain, or a span timed inside a provider adapter, has no access to that dataclass unless
somebody threads it through every intervening signature. Threading it through is the kind
of discipline that holds for a month: the parameter reaches ninety per cent of the call
sites and the ten per cent it misses are exactly the error paths nobody exercised.

So the id lives in a :class:`~contextvars.ContextVar`. That is the one carrier with the
semantics this needs:

* a value set before an ``await`` is still there after it, because awaiting a coroutine
  runs it in the caller's own context;
* a value set inside a coroutine that a caller awaits *does* leak back to that caller,
  which is why :func:`bind_scope` is a context manager and resets its token on exit
  rather than trusting the frame to end;
* :func:`asyncio.create_task` copies the context, so a spawned task inherits the id and
  cannot overwrite its parent's -- one request fanning out to three concurrent calls
  gives three tasks that all report the same correlation id and none that corrupts it;
* a thread does *not* inherit it, which is honest: the durable worker's threads bind
  their own scope per leased command, and silently inheriting an HTTP request's id there
  would be a lie about causation.

**This id is not authority.** ``deps.py`` says so about the header and the same is true
here: a caller supplies it, so it is a join key and nothing else. Nothing in this package
reads it to decide anything. It is copied into log lines, offered to a tracer, and never
consulted by a policy.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from typing import Final

__all__ = [
    "CORRELATION_ID_HEADER",
    "Scope",
    "bind_scope",
    "correlation_id",
    "correlation_id_from_header",
    "current_scope",
    "current_tenant",
    "new_correlation_id",
    "scope_fields",
]

#: The header ``commerce_api.deps`` reads. Repeated rather than imported: this package
#: depends on nothing in the workspace on purpose (see the package docstring), and a
#: string constant is a cheaper thing to keep in step than an import edge that would put
#: observability inside the API's dependency closure.
CORRELATION_ID_HEADER: Final[str] = "X-Correlation-Id"


@dataclass(frozen=True, slots=True)
class Scope:
    """The identifiers every log line, metric label set and span carries.

    All strings, even though the API holds UUIDs: a Prometheus label value and a JSON log
    field are both text in the end, and normalising once at the boundary means no code
    downstream has to know which form it was handed.

    ``tenant_id`` is here because it is the label that partitions every tenant-scoped
    instrument (see :class:`~platform_observability.metrics.TenantMetrics`), and
    ``actor_type`` because "who did this" is the first question asked of an operational
    log and the audit stream's answer (``audit_events.actor_type``) should read the same.
    """

    correlation_id: str
    tenant_id: str | None = None
    actor_type: str | None = None
    causation_id: str | None = None


_SCOPE: Final[ContextVar[Scope | None]] = ContextVar("platform_observability_scope", default=None)


def new_correlation_id() -> str:
    """A fresh correlation id: UUIDv7, matching the platform's internal ids (spec 24.1).

    Time-ordered, so ids sort by when their journey began, which is what makes a log
    store's index on this column useful.
    """
    return str(uuid.uuid7())


def correlation_id_from_header(raw: str | None) -> str:
    """The id a caller supplied, or a fresh one.

    Mirrors :func:`commerce_api.deps._correlation_id` exactly, including its judgement
    about malformed input: a header that is not a UUID is a client bug and is *not*
    grounds to refuse a request. Mint one and carry on. The header was never authority,
    so nothing is at stake in ignoring it.
    """
    if raw:
        try:
            return str(uuid.UUID(raw))
        except ValueError:
            pass
    return new_correlation_id()


def current_scope() -> Scope | None:
    """The scope bound to this context, or ``None`` outside any :func:`bind_scope`."""
    return _SCOPE.get()


def correlation_id() -> str | None:
    """The bound correlation id, or ``None``.

    ``None`` rather than a freshly minted id: a log line emitted outside any request or
    command should say so, and inventing an id that joins to nothing would hide that.
    """
    scope = _SCOPE.get()
    return scope.correlation_id if scope else None


def current_tenant() -> str | None:
    """The bound tenant, or ``None``."""
    scope = _SCOPE.get()
    return scope.tenant_id if scope else None


def scope_fields() -> dict[str, str]:
    """The bound scope as log fields, omitting what is not set.

    Absent keys rather than null values: a log query for ``tenant_id="..."`` should not
    have to reason about records where the key exists and is empty.
    """
    scope = _SCOPE.get()
    if scope is None:
        return {}
    fields = {"correlation_id": scope.correlation_id}
    if scope.tenant_id is not None:
        fields["tenant_id"] = scope.tenant_id
    if scope.actor_type is not None:
        fields["actor_type"] = scope.actor_type
    if scope.causation_id is not None:
        fields["causation_id"] = scope.causation_id
    return fields


@contextmanager
def bind_scope(
    correlation_id: str | uuid.UUID | None = None,
    *,
    tenant_id: str | uuid.UUID | None = None,
    actor_type: str | None = None,
    causation_id: str | uuid.UUID | None = None,
) -> Iterator[Scope]:
    """Bind a scope for the duration of the block, then restore what was there.

    Every argument is optional and every omitted one **inherits** from the scope already
    bound, so a middleware can establish the correlation id once and a service deeper in
    can add the tenant without having to know the id::

        with bind_scope(ctx.correlation_id, tenant_id=ctx.tenant_id,
                        actor_type=ctx.actor_type.value):
            ...

        # deeper, and further out in time:
        with bind_scope(actor_type="WORKER"):   # keeps the correlation id and tenant
            ...

    With nothing bound and no ``correlation_id`` given, one is minted, because a scope
    without an id is not a scope.

    Restoration is by token, so nested binds unwind in the right order even when an
    exception skips past several of them at once -- and if the token cannot be used, the
    parent value is restored by hand. See :func:`_restore`; that second path is not
    theoretical, it is what FastAPI does.
    """
    parent = _SCOPE.get()
    if correlation_id is not None:
        resolved = str(correlation_id)
    elif parent is not None:
        resolved = parent.correlation_id
    else:
        resolved = new_correlation_id()

    if parent is None:
        scope = Scope(correlation_id=resolved)
    else:
        scope = replace(parent, correlation_id=resolved)
    if tenant_id is not None:
        scope = replace(scope, tenant_id=str(tenant_id))
    if actor_type is not None:
        scope = replace(scope, actor_type=actor_type)
    if causation_id is not None:
        scope = replace(scope, causation_id=str(causation_id))

    token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        _restore(token, parent)


def _restore(token: Token[Scope | None], parent: Scope | None) -> None:
    """Undo a bind. Never raises, whichever context this is unwinding in.

    ``ContextVar.reset`` refuses a token created in a different :class:`~contextvars.Context`
    with a ``ValueError``, and there is one very ordinary way to end up there: a FastAPI
    dependency written as a **synchronous** generator::

        def session_scope(request: Request) -> Iterator[None]:
            with bind_scope(tenant_id=...):
                yield

    FastAPI runs a sync ``yield`` dependency through ``anyio``'s thread pool, and the
    ``next(gen)`` that enters the block and the ``gen.throw``/``next`` that leaves it are
    dispatched to worker threads under *different* copied contexts. The token is then
    foreign at exit, ``reset`` raises, and the ``ValueError`` propagates out of the
    dependency's teardown -- turning a deliberate 409 into a 500. An observability layer
    that can do that is precisely the thing this package promises not to be, so the token
    path is best-effort and the fallback is to put the parent value back by hand.

    The fallback is safe because the context it writes into is the one being torn down. If
    it is a copy, it is discarded immediately; if it is the caller's own, the value it
    restores is the one ``reset`` would have restored.

    An ``async def`` dependency shares the request's context and takes the token path, which
    is the arrangement to prefer -- see ``docs/adr/0007-observability.md``.
    """
    try:
        _SCOPE.reset(token)
    except ValueError:
        _SCOPE.set(parent)
