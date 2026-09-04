"""Razorpay test-mode adapter, specification 11.

Deterministic throughout: no model call, no prompt, no clock read for a security decision,
and no network except through the injected ``HttpTransport``.

What this package guarantees
----------------------------
* A live key cannot be loaded into a development or demo process (:mod:`.config`,
  :mod:`.env`).
* A signature is compared in constant time, and a webhook is verified over the raw request
  bytes -- never over a re-serialised body (:mod:`.signatures`).
* An outcome that might have moved money is reported as ``PAYMENT_UNKNOWN``, which is not
  retryable, rather than as a failure that is (:mod:`.transport`).
* A receipt over 40 characters is refused, never truncated (:mod:`.orders`).
* Provider state is read by authoritative identifier and echo-checked before it becomes
  evidence; a payment for the wrong amount or order is refused, never recorded
  (:mod:`.payments`).
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
from .env import (
    API_CREDENTIAL_VARIABLE,
    KEY_ID_VARIABLE,
    PRODUCTION_APPROVAL_VARIABLE,
    PROFILE_VARIABLE,
    REQUIRED_VARIABLES,
    WEBHOOK_CREDENTIAL_VARIABLE,
    load_config_from_env,
)
from .errors import (
    ConfigurationError,
    EvidenceMismatchError,
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
    OrderLookupResult,
    build_create_order_request,
    build_order_lookup_request,
    create_order,
    find_order_by_receipt,
)
from .payments import (
    EVIDENCE_SOURCE,
    FetchClassification,
    OrderPaymentsResult,
    PaymentFetchResult,
    ProviderEvidence,
    build_fetch_payment_request,
    build_order_payments_request,
    fetch_order_payments,
    fetch_payment,
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
    parse_json_body,
    parse_json_body_bytes,
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
    header_value,
    parse_event,
    payment_state_for_event,
)

__all__ = [
    "API_BASE_URL",
    "API_CREDENTIAL_VARIABLE",
    "DEFAULT_TIMEOUT_SECONDS",
    "EVENT_ID_HEADER",
    "EVIDENCE_SOURCE",
    "IDEMPOTENCY_HEADER",
    "KEY_ID_VARIABLE",
    "LIVE_KEY_PREFIX",
    "MAX_RECEIPT_LENGTH",
    "PRODUCTION_APPROVAL_VARIABLE",
    "PROFILE_VARIABLE",
    "REQUIRED_VARIABLES",
    "SIGNATURE_HEADER",
    "TEST_KEY_PREFIX",
    "WEBHOOK_CREDENTIAL_VARIABLE",
    "CaptureEvidence",
    "CaptureLedger",
    "ConfigurationError",
    "CreateOrderResult",
    "EvidenceMismatchError",
    "FetchClassification",
    "HttpRequest",
    "HttpResponse",
    "HttpTransport",
    "InMemoryInboxStore",
    "InboxRecord",
    "InboxStore",
    "OrderLookupResult",
    "OrderPaymentsResult",
    "PaymentFetchResult",
    "ProviderEvidence",
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
    "build_fetch_payment_request",
    "build_order_lookup_request",
    "build_order_payments_request",
    "build_refund_request",
    "classify_failure",
    "create_order",
    "dedup_key_for",
    "execute_refund",
    "fetch_order_payments",
    "fetch_payment",
    "find_order_by_receipt",
    "header_value",
    "load_config_from_env",
    "may_fulfil",
    "may_retry_after",
    "must_reconcile_before_retry",
    "parse_event",
    "parse_json_body",
    "parse_json_body_bytes",
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
