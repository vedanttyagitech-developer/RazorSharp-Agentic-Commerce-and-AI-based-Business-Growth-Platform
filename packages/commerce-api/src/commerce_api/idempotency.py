"""``Idempotency-Key`` handling for HTTP mutations (ADR 0003 D9, specification 10.6).

The kernel already owns the hard part: :func:`transaction_kernel.idempotency.idempotent`
claims a key inside the caller's transaction, so the record commits with the effect it
describes and never without it. This module is the thin HTTP shell around it, and it
exists to make one thing impossible.

**A replay must not be able to fall through into the work.** The kernel raises
``IdempotentReplayError`` from the context manager's ``__enter__``, so the guarded block
never runs. This module preserves that: it converts the kernel's signal into
:class:`IdempotentReplay`, which is an exception with an installed handler, rather than
into a flag on a yielded object. A flag is forgettable, and forgetting it means creating
a second Razorpay order for a request that already has one.

The three outcomes, all decided before the guarded block:

===================================  ===================================================
Key unused                           the block runs once; the response is stored with it
Key used, byte-identical payload     HTTP 200, the **stored** body, ``Idempotent-Replayed: true``
Key used, different payload          HTTP 422 problem (D9)
===================================  ===================================================

That third row is the one worth being careful about. A retry whose amount changed from
395.00 to 3950.00 but whose key did not is a client bug or an attack. Re-executing
charges the new amount under a key that promised not to re-execute; returning the stored
response says "3950.00 succeeded" when 395.00 is what happened. So it is refused, and
nothing about the stored result is disclosed.

Usage -- the whole pattern, and the only supported one::

    from commerce_api.idempotency import idempotent_mutation

    @router.post("/{cart_id}/checkout")
    def create_checkout(
        cart_id: uuid.UUID,
        ctx: SessionContext,
        session: KernelSession,
        key: IdempotencyKey,
    ) -> JSONResponse:
        payload = {"cart_id": str(cart_id), "lines": [...]}
        operation = Operation.PAYMENT_CREATE_ORDER
        with idempotent_mutation(session, ctx, key, operation, payload) as slot:
            body = do_the_work(session, ctx)   # kernel calls, head updates, enqueue
            slot.store(body)                   # bind the result to the key
        return JSONResponse(body)

Four rules for that block:

1. ``slot.store(body)`` takes the **exact** object the handler returns. Store one thing
   and return another and a replay hands back a body the first caller never saw.
2. The body must be JSON with integer numbers only. It is canonicalized (RFC 8785,
   integer-only profile) before it is written, so a float raises here rather than
   becoming an unverifiable replay later.
3. ``payload`` is the request's identity, not its transport. Include the path parameters
   and the body fields that change what happens; leave out timestamps, correlation ids
   and anything else that differs between two deliveries of the same retry.
4. Do not commit inside the block. The dependency owns the transaction
   (:mod:`commerce_api.deps`); committing early would publish a claim for work that had
   not finished.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from platform_db import current_tenant
from sqlalchemy.orm import Session
from transaction_kernel import Operation
from transaction_kernel.idempotency import (
    MAX_KEY_LENGTH,
    IdempotencySlot,
    IdempotentReplayError,
    idempotent,
)

from .deps import RequestContext
from .errors import ProblemError

__all__ = [
    "IDEMPOTENT_REPLAYED_HEADER",
    "IdempotentReplay",
    "idempotent_mutation",
    "install_idempotency_handler",
    "request_fingerprint",
]

#: Present and ``"true"`` only on a replayed response. Absent on the original, because a
#: header that is always there says nothing.
IDEMPOTENT_REPLAYED_HEADER: Final[str] = "Idempotent-Replayed"


class IdempotentReplay(Exception):  # noqa: N818 - a control-flow signal, not a failure
    """This key already completed; the stored response is the answer.

    Raised out of :func:`idempotent_mutation`'s ``__enter__`` so the guarded block cannot
    execute. The installed handler turns it into the stored body with
    ``Idempotent-Replayed: true``. Nothing in a router needs to catch it.
    """

    def __init__(self, key: str, operation: str, response: Mapping[str, Any]) -> None:
        super().__init__(f"idempotency key {key!r} already completed {operation}")
        self.key = key
        self.operation = operation
        self.response = dict(response)


@contextmanager
def idempotent_mutation(
    session: Session,
    ctx: RequestContext,
    key: str,
    operation: Operation | str,
    payload: Any,
) -> Iterator[IdempotencySlot]:
    """Guard one HTTP mutation so it runs at most once per (tenant, key).

    Runs in the caller's transaction -- the one ``kernel_session`` opened -- so the
    idempotency record, the kernel's writes, the head update and the outbox row commit
    together or roll back together.

    :param session: the request's kernel-role session, already inside its transaction
        with the tenant bound.
    :param ctx: the request context. Used to check that the transaction's bound tenant is
        the session's tenant, so a record can never be written under one tenant and read
        under another.
    :param key: the client's ``Idempotency-Key`` (see
        :func:`commerce_api.deps.idempotency_key`).
    :param operation: a :class:`~transaction_kernel.Operation`, or a short stable string
        for operations the kernel has no enum member for (``"CHECKOUT_CREATE"``).
        Whatever it is, it must be the same on every retry of the same request.
    :param payload: the request's identity, hashed with
        ``commerce_domain.canonical_hash``. Key order and whitespace do not matter; a
        changed amount does.

    :yields: the kernel's :class:`~transaction_kernel.idempotency.IdempotencySlot`. Call
        ``slot.store(body)`` with the response body exactly once.

    :raises IdempotentReplay: same key, same payload, result known. **The guarded block
        does not run.** Handled into a 200 with the stored body.
    :raises ProblemError: 422 when the key is bound to a different payload (D9), or 400
        when the key is longer than the column holds.
    :raises transaction_kernel.idempotency.IdempotencyInFlightError: the key is claimed by
        a record with no response -- an unknown outcome, mapped to 409 by
        :mod:`commerce_api.errors`. Reconcile; never re-run.
    """
    bound = current_tenant(session)
    if bound != ctx.tenant_id:
        # A mismatch means the transaction was not opened by this request's dependency,
        # or someone rebound the tenant mid-request. Either way the record about to be
        # written would be invisible to the session that must later read it.
        raise ProblemError(
            500,
            "Tenant context mismatch",
            None,
            bound_tenant=str(bound) if bound else None,
        )

    scoped = _scoped_key(key, ctx)
    try:
        with idempotent(session, scoped, operation, payload) as slot:
            yield slot
    except IdempotentReplayError as replay:
        raise IdempotentReplay(key, str(operation), replay.response) from None


def _scoped_key(key: str, ctx: RequestContext) -> str:
    """Bind the client's key to the buyer that supplied it.

    Specification 10.6 asks for a "stable merchant-scoped idempotency key". The record's
    unique index is ``(tenant_id, idem_key)``, which already isolates tenants, but two
    buyers in one tenant could otherwise collide on a client-chosen key like ``"1"`` --
    and the loser would be handed the winner's response, which is somebody else's order.
    Prefixing the buyer reference makes that impossible without asking clients to
    generate globally unique keys.

    Refused rather than truncated when the result cannot fit
    ``idempotency_records.idem_key``: truncation is what makes two different operations
    share one record, which is the exact collision this table exists to prevent.
    """
    scoped = f"{ctx.buyer_ref}:{key}"
    if len(scoped) > MAX_KEY_LENGTH:
        raise ProblemError(
            400,
            "Idempotency-Key too long",
            f"The key must be at most {MAX_KEY_LENGTH - len(ctx.buyer_ref) - 1} characters.",
            header="Idempotency-Key",
        )
    return scoped


def install_idempotency_handler(app: FastAPI) -> None:
    """Register the handler that turns a replay into its stored response.

    Called by :func:`commerce_api.app.create_app`. Separate from
    :func:`commerce_api.errors.install_error_handlers` because a replay is not an error:
    it is a 200 carrying the original body, and grouping it with the problem handlers
    would invite somebody to give it a 4xx.
    """

    async def on_replay(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001
        assert isinstance(exc, IdempotentReplay)
        return JSONResponse(
            content=exc.response,
            status_code=200,
            headers={IDEMPOTENT_REPLAYED_HEADER: "true"},
        )

    app.add_exception_handler(IdempotentReplay, on_replay)


def request_fingerprint(
    *, path_params: Mapping[str, Any] | None = None, body: Any = None
) -> dict[str, Any]:
    """Build a ``payload`` for :func:`idempotent_mutation` from a request's own parts.

    A convenience so every unit fingerprints a mutation the same way: path parameters
    plus the parsed body, with UUIDs rendered as strings so the hash is stable across a
    JSON round trip. Anything a caller passes must already be canonicalizable -- integers
    and strings, never floats.
    """
    fingerprint: dict[str, Any] = {}
    if path_params:
        fingerprint["path"] = {
            name: str(value) if isinstance(value, uuid.UUID) else value
            for name, value in sorted(path_params.items())
        }
    if body is not None:
        fingerprint["body"] = body
    return fingerprint
