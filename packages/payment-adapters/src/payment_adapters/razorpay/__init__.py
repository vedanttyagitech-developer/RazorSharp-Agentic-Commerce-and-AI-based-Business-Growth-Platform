"""Razorpay test-mode adapter, specification 11.

Deterministic throughout: no model call, no prompt, no clock read for a security decision,
and no network except through the injected ``HttpTransport``.

What this package guarantees
----------------------------
* A live key cannot be loaded into a development or demo process (:mod:`.config`).
* A signature is compared in constant time, and a webhook is verified over the raw request
  bytes -- never over a re-serialised body (:mod:`.signatures`).
* An outcome that might have moved money is reported as ``PAYMENT_UNKNOWN``, which is not
  retryable, rather than as a failure that is (:mod:`.transport`).
* A receipt over 40 characters is refused, never truncated (:mod:`.orders`).
* A duplicated or out-of-order webhook changes state at most once, and never regresses
  ``CAPTURED`` to ``AUTHORIZED`` (:mod:`.webhooks`).
* Fulfilment requires a verified capture; a signed browser callback is not enough
  (:mod:`.fulfilment`).
* A refund whose outcome is unknown reconciles and never retries; only a
  provider-confirmed failure may retry, and only under a new Execution Grant
  (:mod:`.refunds`).
"""

from .config import (
    API_BASE_URL,
    LIVE_KEY_PREFIX,
    TEST_KEY_PREFIX,
    RazorpayConfig,
    RazorpayProfile,
)
from .errors import (
    ConfigurationError,
    RazorpayAdapterError,
    RefundNotPermittedError,
    RequestConstructionError,
    SignatureMismatchError,
    UnmappableEventError,
)
from .fulfilment import CaptureEvidence, may_fulfil, requires_release_not_capture
from .orders import (
    MAX_RECEIPT_LENGTH,
    CreateOrderResult,
    build_create_order_request,
    build_order_lookup_request,
    create_order,
    find_order_by_receipt,
)
from .refunds import (
    IDEMPOTENCY_HEADER,
    CaptureLedger,
    RefundDecision,
    RefundPlan,
    RefundRefusal,
    RefundResult,
    RefundSpeed,
    build_refund_request,
    execute_refund,
    may_retry_after,
    must_reconcile_before_retry,
    plan_refund,
    refund_idempotency_key,
)
from .signatures import (
    payment_signature_payload,
    require_payment_signature,
    require_webhook_signature,
    verify_payment_signature,
    verify_webhook_signature,
)
from .transport import (
    DEFAULT_TIMEOUT_SECONDS,
    HttpRequest,
    HttpResponse,
    HttpTransport,
    TransportError,
    TransportTimeoutError,
    classify_failure,
)
from .webhooks import (
    EVENT_ID_HEADER,
    SIGNATURE_HEADER,
    InboxRecord,
    InboxStore,
    InMemoryInboxStore,
    RazorpayWebhookEvent,
    WebhookAdmission,
    WebhookInbox,
    apply_event,
    dedup_key_for,
    parse_event,
    payment_state_for_event,
)

__all__ = [
    "API_BASE_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "EVENT_ID_HEADER",
    "IDEMPOTENCY_HEADER",
    "LIVE_KEY_PREFIX",
    "MAX_RECEIPT_LENGTH",
    "SIGNATURE_HEADER",
    "TEST_KEY_PREFIX",
    "CaptureEvidence",
    "CaptureLedger",
    "ConfigurationError",
    "CreateOrderResult",
    "HttpRequest",
    "HttpResponse",
    "HttpTransport",
    "InMemoryInboxStore",
    "InboxRecord",
    "InboxStore",
    "RazorpayAdapterError",
    "RazorpayConfig",
    "RazorpayProfile",
    "RazorpayWebhookEvent",
    "RefundDecision",
    "RefundNotPermittedError",
    "RefundPlan",
    "RefundRefusal",
    "RefundResult",
    "RefundSpeed",
    "RequestConstructionError",
    "SignatureMismatchError",
    "TransportError",
    "TransportTimeoutError",
    "UnmappableEventError",
    "WebhookAdmission",
    "WebhookInbox",
    "apply_event",
    "build_create_order_request",
    "build_order_lookup_request",
    "build_refund_request",
    "classify_failure",
    "create_order",
    "dedup_key_for",
    "execute_refund",
    "find_order_by_receipt",
    "may_fulfil",
    "may_retry_after",
    "must_reconcile_before_retry",
    "parse_event",
    "payment_signature_payload",
    "payment_state_for_event",
    "plan_refund",
    "refund_idempotency_key",
    "require_payment_signature",
    "require_webhook_signature",
    "requires_release_not_capture",
    "verify_payment_signature",
    "verify_webhook_signature",
]
