"""Transaction Assurance Kernel: the only component that may authorize money movement.

This surface re-exports what callers outside the kernel legitimately need. Anything absent
is an internal detail of a submodule: import it from that submodule deliberately, so the
kernel boundary stays visible in every import that crosses it.
"""

from .admission import (
    AdmissionError,
    AdmissionRequest,
    CurrentMerchantState,
    MerchantStateSource,
    admit,
)
from .audit import append, head, read_stream, verify_chain
from .authority import (
    AuthorityDecision,
    AuthorityReason,
    admit_debit,
    check_authority,
    revoke,
)
from .contracts import (
    ActorType,
    AgentPrincipal,
    CheckoutRef,
    Delta,
    KernelDecision,
    Operation,
    VerifiedAuthorityProof,
)
from .grants import GrantBinding, consume_grant, issue_grant, revoke_unused_grants
from .idempotency import IdempotentOutcome, execute_once, idempotent
from .receipts import issue_receipt, policy_for_order, verify_binding
from .recovery import NEEDS_REAPPROVAL, NOT_A_SUCCESS, RETRYABLE, RecoveryCode
from .reservations import (
    ReleaseCause,
    ReservationOutcome,
    check_validity,
    consume,
    release,
    reserve,
)
from .safe_mode import (
    GuardedOperation,
    ModeScope,
    assert_permitted,
    current_mode,
    enter_safe_mode,
    is_permitted,
    leave_safe_mode,
)
from .states import (
    CHECKOUT_TRANSITIONS,
    PAYMENT_TRANSITIONS,
    TERMINAL_CHECKOUT_STATES,
    TERMINAL_PAYMENT_STATES,
    UNCERTAIN_PAYMENT_STATES,
    CheckoutState,
    InvalidTransitionError,
    PaymentState,
    assert_transition,
    can_transition,
    is_terminal,
    monotonic_apply,
)

__all__ = [
    "CHECKOUT_TRANSITIONS",
    "NEEDS_REAPPROVAL",
    "NOT_A_SUCCESS",
    "PAYMENT_TRANSITIONS",
    "RETRYABLE",
    "TERMINAL_CHECKOUT_STATES",
    "TERMINAL_PAYMENT_STATES",
    "UNCERTAIN_PAYMENT_STATES",
    "AdmissionError",
    "AdmissionRequest",
    "ActorType",
    "AgentPrincipal",
    "AuthorityDecision",
    "AuthorityReason",
    "CheckoutRef",
    "CheckoutState",
    "CurrentMerchantState",
    "MerchantStateSource",
    "Delta",
    "GrantBinding",
    "GuardedOperation",
    "IdempotentOutcome",
    "InvalidTransitionError",
    "KernelDecision",
    "ModeScope",
    "Operation",
    "PaymentState",
    "RecoveryCode",
    "ReleaseCause",
    "ReservationOutcome",
    "VerifiedAuthorityProof",
    "admit",
    "admit_debit",
    "append",
    "assert_permitted",
    "assert_transition",
    "can_transition",
    "check_authority",
    "check_validity",
    "consume",
    "consume_grant",
    "current_mode",
    "enter_safe_mode",
    "execute_once",
    "head",
    "idempotent",
    "is_permitted",
    "is_terminal",
    "issue_grant",
    "issue_receipt",
    "leave_safe_mode",
    "monotonic_apply",
    "policy_for_order",
    "read_stream",
    "release",
    "reserve",
    "revoke",
    "revoke_unused_grants",
    "verify_binding",
    "verify_chain",
]
