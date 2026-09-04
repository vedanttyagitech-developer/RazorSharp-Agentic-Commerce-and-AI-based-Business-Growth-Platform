"""Durable work: the transactional outbox and the leasing protocol built on it.

Everything here is deterministic. No model call, no prompt, no LLM. An agent may ask for
a command to be enqueued; only this module and the database decide whether it runs, how
often, and when it stops being retried.
"""

from .outbox import (
    DEFAULT_POLICY,
    MAX_ATTEMPT_CEILING,
    MAX_BATCH,
    MAX_LEASE_SECONDS,
    DeadLetter,
    DeliveryOutcome,
    Jitter,
    LeasedCommand,
    LeaseToken,
    OutboxCommand,
    OutboxError,
    OutboxStatus,
    OutboxUsageError,
    RetryPolicy,
    backoff_seconds,
    complete,
    enqueue,
    extend_lease,
    fail,
    lease,
    reap_exhausted,
    revive,
)

__all__ = [
    "DEFAULT_POLICY",
    "MAX_ATTEMPT_CEILING",
    "MAX_BATCH",
    "MAX_LEASE_SECONDS",
    "DeadLetter",
    "DeliveryOutcome",
    "Jitter",
    "LeaseToken",
    "LeasedCommand",
    "OutboxCommand",
    "OutboxError",
    "OutboxStatus",
    "OutboxUsageError",
    "RetryPolicy",
    "backoff_seconds",
    "complete",
    "enqueue",
    "extend_lease",
    "fail",
    "lease",
    "reap_exhausted",
    "revive",
]
