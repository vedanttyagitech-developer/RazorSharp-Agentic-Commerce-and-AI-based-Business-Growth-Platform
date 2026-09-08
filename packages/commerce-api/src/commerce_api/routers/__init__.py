"""Every router in the service, and the list the app includes.

This module is the reason five engineers can work on this API at once. Each unit owns one
module here; :data:`ROUTERS` names them all; ``create_app`` includes whatever is in that
list. Adding an endpoint therefore touches exactly one file -- the router it belongs in --
and never ``app.py``, which is the file every parallel branch would otherwise conflict
on.

Order in the list is include order, which is the order FastAPI matches paths in. It is
arranged so the more specific prefixes are registered before the broader ones:
``/v1/inspector``, ``/v1/scenario`` and ``/v1/review`` before ``evidence``, which claims
the bare ``/v1`` prefix because its paths hang off several different nouns.

Two routers deliberately share the ``/v1/checkouts`` prefix. ``checkouts`` owns
construction and the read model; ``approvals`` owns approve, reject, submit and cancel.
FastAPI merges them, and the two build units never open the same file.

``protocols``, ``mcp`` and ``acp`` are three files for one layer, and the split is not
arbitrary. ``protocols`` is read-only -- profiles, the pinned matrix, the inspector -- and
a test asserts that over the route table, so the two transports that must accept POSTs
live beside it rather than inside it. Each carries its own tag, so that assertion keeps
saying what it means.
"""

from fastapi import APIRouter

from . import (
    acp,
    agent,
    approvals,
    carts,
    catalogue,
    checkouts,
    demo,
    evidence,
    health,
    inspector,
    mcp,
    merchant_actions,
    merchant_policy,
    ops,
    orders,
    payments,
    protocols,
    refunds,
    review,
    scenario,
    support,
    webhooks,
)

__all__ = ["ROUTERS"]

#: Included by :func:`commerce_api.app.create_app`, in this order.
ROUTERS: tuple[APIRouter, ...] = (
    health.router,
    demo.router,
    catalogue.router,
    carts.router,
    checkouts.router,
    approvals.router,
    payments.router,
    orders.router,
    refunds.router,
    inspector.router,
    scenario.router,
    ops.router,
    review.router,
    support.router,
    merchant_actions.router,
    merchant_policy.router,
    evidence.router,
    protocols.router,
    mcp.router,
    acp.router,
    webhooks.router,
    agent.router,
)
