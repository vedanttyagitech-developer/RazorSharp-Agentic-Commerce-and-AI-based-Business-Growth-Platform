"""Read-only operator conversation tools; never delegates into merchant or buyer agents."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from platform_db.schema import OutboxEvent, Refund
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import RequestContext, require_operator
from . import project_guide, reconciliation_service
from .runtime_health import runtime_health

CONSOLE_TOOLS = (
    "knowledge.read",
    "console.readiness",
    "console.reconciliation",
    "console.refunds",
    "console.executions",
    "navigate",
)


@dataclass(frozen=True)
class ReadResult:
    ok: bool
    payload: dict[str, Any]
    reason_key: str | None = None


class ConsoleTools:
    def __init__(self, session: Session, ctx: RequestContext) -> None:
        require_operator(ctx)
        self.session = session
        self.ctx = ctx

    def call(self, name: str, **args: Any) -> ReadResult:
        require_operator(self.ctx)
        if name not in CONSOLE_TOOLS:
            return ReadResult(False, {}, "TOOL_FORBIDDEN")
        if name == "knowledge.read":
            return ReadResult(
                True, project_guide.answer(str(args.get("query", "")), step="console") or {}
            )
        if name == "console.readiness":
            return ReadResult(True, {"services": asyncio.run(runtime_health())})
        if name == "navigate":
            destination = args.get("destination")
            if destination not in {"console", "shopping", "merchant"}:
                return ReadResult(False, {}, "INVALID_DESTINATION")
            return ReadResult(True, {"navigate": destination})
        try:
            limit = max(1, min(int(args.get("limit", 10)), 50))
        except ValueError, TypeError:
            return ReadResult(False, {}, "INVALID_ARGUMENT")
        if name in {"console.refunds", "console.executions"}:
            table = Refund if name == "console.refunds" else OutboxEvent
            rows = self.session.execute(
                select(table.id, table.status, table.created_at)
                .where(table.tenant_id == self.ctx.tenant_id)
                .order_by(table.created_at.desc(), table.id.desc())
                .limit(limit + 1)
            ).all()
            return ReadResult(
                True,
                {
                    "items": [
                        {
                            "id": str(row.id),
                            "status": row.status,
                            "created_at": row.created_at.isoformat(),
                        }
                        for row in rows[:limit]
                    ],
                    "has_more": len(rows) > limit,
                    "scope": "current tenant",
                    "limit": limit,
                },
            )
        unresolved = args.get("unresolved_only", True)
        if unresolved not in (True, False, "true", "false"):
            return ReadResult(False, {}, "INVALID_ARGUMENT")
        attempts = reconciliation_service.survey(
            self.session,
            tenant_id=self.ctx.tenant_id,
            unresolved_only=unresolved in (True, "true"),
            limit=limit,
        )
        return ReadResult(
            True,
            {
                "items": [
                    {
                        "payment_attempt_id": str(row.payment_attempt_id),
                        "checkout_id": str(row.checkout_id),
                        "state": str(row.recorded_state),
                    }
                    for row in attempts
                ],
                "scope": "current tenant",
                "limit": limit,
            },
        )
