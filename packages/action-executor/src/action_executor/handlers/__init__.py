"""One handler per command type. A handler consumes its grant before any network call.

This module holds only what every handler shares -- the result type, the failure type and
two small helpers -- and deliberately imports none of the handler modules, so a handler
can import from here without a cycle.

**What a handler returns.** :class:`HandlerResult` carries a
:class:`~commerce_domain.recovery.RecoveryCode`, and the code decides what the loop
does with the outbox row. The distinction that matters is not "did the payment succeed"
but "did this command run to a recorded conclusion":

* a create-order that timed out returns ``OK``. The unknown outcome is durably recorded,
  reconciliation is enqueued, and re-running the command would consume nothing and change
  nothing. Reporting it as a failure would schedule a retry of a provider call that may
  already have landed, which is precisely the mistake the whole design is built to avoid;
* a command whose payload cannot be acted on returns a non-retryable code, and the outbox
  buries it as a dead letter for a person to look at;
* an infrastructure fault raises, and the loop reports a retryable code so the command is
  redelivered. That is safe for every handler here, because each one begins by consuming
  its Execution Grant: a redelivery finds the grant consumed and reconciles instead of
  sending a second request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from commerce_domain import RecoveryCode

__all__ = [
    "MAX_REASON_LENGTH",
    "HandlerError",
    "HandlerResult",
    "backoff_seconds",
    "reason_key",
]

#: ``payments.record_reconciliation_run`` and ``record_webhook_applied`` both store a
#: reason in a 64-character column and refuse anything outside ``[a-z0-9_.:-]``.
MAX_REASON_LENGTH: Final[int] = 64

#: Codes that mean "this command is finished"; every other code fails the outbox row.
#: ``DUPLICATE_OPERATION`` is a success: the work was already done by an earlier delivery.
_COMPLETED: Final[frozenset[RecoveryCode]] = frozenset(
    {RecoveryCode.OK, RecoveryCode.DUPLICATE_OPERATION}
)


class HandlerError(Exception):
    """A handler cannot finish, and says how the outbox should treat the command.

    Raised rather than returned for the cases where continuing would be wrong: a payload
    the handler cannot act on, or a row that is not in the state the command assumes.
    ``code`` decides burial versus redelivery through ``durable_work.outbox.fail``.
    """

    def __init__(self, message: str, *, code: RecoveryCode) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class HandlerResult:
    """What one command did, in the vocabulary the outbox and the timeline share."""

    code: RecoveryCode
    #: A short stable key naming the branch taken, for the worker log and the tests.
    detail: str
    #: Commands this handler enqueued, by type, so a caller can assert the follow-up
    #: without reading the outbox table.
    followups: tuple[str, ...] = ()

    @property
    def completed(self) -> bool:
        """True when the outbox row should be completed rather than failed."""
        return self.code in _COMPLETED


def reason_key(value: str) -> str:
    """Coerce text into the reason vocabulary the kernel's evidence columns accept.

    The kernel refuses anything outside ``[a-z0-9_.:-]{1,64}``, and a refusal here would
    abort a transaction that is recording what the provider said. Sanitising is the right
    trade only because the reason is a label beside the real evidence -- the provider
    request row, the recorded outcome and the audit payload all carry the untruncated
    facts.
    """
    lowered = "".join(
        char if char.isalnum() or char in "_.:-" else "_" for char in value.strip().lower()
    )
    return (lowered or "unspecified")[:MAX_REASON_LENGTH]


def backoff_seconds(attempt_number: int, base_seconds: int) -> int:
    """Exponential delay before reconciliation round ``attempt_number + 1`` (ADR D13).

    Doubles per round from ``base_seconds`` and is capped, because the bound on rounds is
    what stops the loop -- the delay only decides how long the platform waits between
    asking the provider the same question, and an unbounded delay would push the sixth
    round past any demonstration.
    """
    if base_seconds <= 0:
        return 0
    exponent = max(0, min(attempt_number - 1, 8))
    return min(base_seconds * int(2**exponent), 3600)
