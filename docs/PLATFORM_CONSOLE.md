# Open demo platform console

Open `/platform`. This is a proposed Razorpay-side operations experience for the
application, not access to Razorpay's internal systems or a bank authorization service.

## Enable locally

Follow [DIRECT_MOUNT.md](DIRECT_MOUNT.md). Open `http://localhost:8000/platform`.
The backend automatically establishes an OPERATOR demo session. No login, code,
OAuth, or Razorpay credentials are needed. Anyone who can reach the enabled demo can
view its evidence, use confirmed tenant Safe Mode controls, and explicitly revive an existing DEAD command. Use demo data.
The backend retains role, tenant, cookie and financial safeguards.

## Connected functionality

- Tenant Safe Mode read and explicit confirmed enter/leave, audited by the backend.
- Reconciliation list and attempt details; recorded state stays separate from stored
  provider verification. Opening a view does not fetch fresh provider state.
- Human-review case list and redacted case evidence, read-only.
- Filtered outbox snapshot and tenant-wide counts; DEAD commands have a review panel requiring the exact existing command identifier before revival. This can resume original execution, not create a replacement payment.
- Payment inspector, audit-chain verification, retained-revenue evidence and checkout proofs.
- Protocol configuration and recorded interaction inspection; configuration is not external certification.
- Process-wide Prometheus metrics with filtering; not replica aggregation or tenant financial reporting.
- Explicit trust boundary information; no live key inventory or signing controls.

Queues have Previous/Next pagination with exact has-more evidence and a return-to-newest control. Reconciliation findings filter each bounded page; an empty filtered page can still have older attempts. Page ordering is deterministic; concurrent new records can shift offset pages, so refresh from newest for a fresh investigation. Counts say whether they describe a page or the whole tenant. There is no fabricated
payment volume, availability, settlement or dispute metric. The console does not
proxy scenario injection, refund or recovery-plan execution.
Financial records remain scoped to the operator session tenant. Metrics describe the API process and may include labels from other tenants; they remain operator-only and demo-gated.

Writes require an explicitly configured browser origin. The backend adapter supplies
credentials from the role-specific HttpOnly cookie and dispatches into the same API
router used by API clients. Browser Authorization and scenario-key headers are discarded.

## Verification and remaining scope

Browser-boundary regression tests live in
`packages/commerce-api/tests/test_capi_browser_mount.py`, including authentication
coverage, streaming delivery, session isolation, CSRF and stable request payloads.
Checkout SSE consumer tests live in `tests/checkout-events.test.mjs` in the frontend.

Production SSO, permission groups, cross-tenant grants, independent approvals, case
assignment/resolution, key administration, settlement/dispute APIs and platform-wide
live event feeds remain unimplemented. Checkout SSE does not imply an operator event feed.

## ACP and UCP buyer capability parity

Both buyer journeys expose the same capabilities through the console: real catalogue
search and pagination, selected-basket editing/removal, checkout creation/read, editing
an unpaid checkout into a new version, exact buyer approval, payment recovery/status,
verified order tracking, cancellation requests and fresh-purchase recovery.

The shared trusted-buyer edit endpoint is
`PUT /v1/buyer-protocols/{ACP|UCP}/checkouts/{checkout_id}`. It requires the displayed
version/content hash and an idempotency key. It replaces the complete basket, keeps the
checkout ID, and generates a fresh approval version. Stale requests and edits after
payment execution starts are refused. The UI retains uncertain update requests and
retries with the same key. Cancellation uses the existing authenticated buyer endpoint
and displays its actual decision.

Capability parity does not equate protocol credentials with buyer consent. ACP creation
still verifies signed transport; UCP creation maps its lifecycle intent. Discovery,
approval/payment, order reads, and cancellation use the shared authenticated buyer
services. External ACP clients still cannot rewrite a checkout frozen for a human decision.
