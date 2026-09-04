"""Payment provider adapters.

Every adapter in this package obeys the same three rules:

1. **The transport is injected.** Nothing here opens a socket. A caller supplies an
   ``HttpTransport``; tests supply a fake, and continuous integration never touches the
   provider or a credential.
2. **Provider constraints are enforced here, not in the platform core** (specification
   12.5). The kernel does not need to know that Razorpay caps a receipt at 40 characters.
3. **An outcome that is not certain is reported as unknown.** Adapters return
   ``RecoveryCode`` values from ``transaction_kernel.recovery``; they never invent a code
   and never soften ``PAYMENT_UNKNOWN`` into ``PAYMENT_FAILED``.
"""

from . import razorpay

__all__ = ["razorpay"]
