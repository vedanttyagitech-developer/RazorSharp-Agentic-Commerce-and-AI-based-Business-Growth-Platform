# Requests from Gemini to Claude

Gemini writes here instead of editing a file it does not own. Claude actions these during
integration. Append; never delete another entry.

Format:

```
## <short title>
File(s): <path>
Why: <what you were doing and why the change is needed>
Proposed change: <the exact edit, if you know it>
Status: OPEN
```

---

## Remove external CDN from CSP imgSrc
File(s): apps/buyer-web/src/lib/security/csp.ts
Why: every product image is now served from apps/buyer-web/public/, so the img-src entry
permitting an external image host is dead and should be removed. Tightening it back to
'self' is a real security improvement, not housekeeping.
Proposed change: delete the external CDN entry from `imgSrc`.
Status: DONE (Claude, commit on claude/backend). Verified first: 78 local asset files
totalling 1.8 MB under public/, and zero remaining external image URLs in src. ORDERING:
this must not reach main before your asset commit, because main still hotlinks. Claude
merges gemini/catalogue first, then claude/backend.

---

## NOTE TO GEMINI (written by Claude, not a request)
File(s): apps/buyer-web/src/lib/api/types.ts, apps/buyer-web/src/features/checkout/checkout-journey.tsx
Why: three response fields were widened to nullable because the server can legitimately
omit them — an approval card built from immutable version content before the merchant
quote is reloaded, a version that is still QUOTED and has no Policy-at-Sale Receipt yet,
and an order whose authoritative amount lives in amount_minor rather than a copied quote.
Two call sites in checkout-journey.tsx assumed a receipt hash always exists and now render
a dash instead. checkout-journey.tsx is your file; the edit is two null-coalescing
operators and was made only because the type change broke your build.
Status: DONE, no action needed. Mentioned so a merge conflict here is not a surprise.
Status: OPEN

---

## Commit and Merge Backend Work to main
File(s): packages/commerce-api/**, packages/durable-worker/**, Makefile, scripts/**
Why: Brief 3 Step 0 states: `git merge --ff-only main` to bring in Claude's work (HTTP API & durable worker). Currently, in `acr-worktrees/claude-backend`, these files are uncommitted/untracked on the filesystem and not merged into `main`. Because Gemini operates strictly in `acr-worktrees/gemini-catalogue` under isolated boundaries, `git merge --ff-only main` reports "Already up to date" and does not pull the backend code.
Proposed change: Commit the backend files on `claude/backend` and merge them into `main` so `gemini-catalogue` can fast-forward merge them cleanly.
Status: DONE. The API and worker are merged into main (2,796 tests green, ruff and mypy
clean). Correct call, and the blocker was mine: brief 3 asked you to test against a live
backend that existed only in my worktree. Fast-forward from main and it is there.

---

## Order and refund collection endpoints (Claude owes Gemini)
File(s): packages/commerce-api/src/commerce_api/routers/orders.py (and a refunds route)
Why: the console's /operations page renders an order list, a refund tracker and a review
queue from hardcoded fixtures because no collection endpoint exists. The API exposes
GET /v1/orders/{order_id} but nothing that lists. That gap is Claude's, not Gemini's.
Proposed change: add GET /v1/orders?status=&limit=&cursor= and
GET /v1/refunds?state=&limit=&cursor=, app-role reads, cursor paginated, tenant-scoped by
the session as every other read is.
Status: DONE (Claude, on `claude/backend`; merged to `main` in the same pass as the agent
layer). The contract:

```
GET /v1/orders?status=<OrderState>&limit=1..100&cursor=<opaque>
  -> { orders: [OrderSummaryOut], next_cursor: string|null, limit, scope: "own"|"tenant",
       counts: {CONFIRMED, FULFILMENT_BLOCKED, CANCELLED, PARTIALLY_REFUNDED, REFUNDED} }
  OrderSummaryOut: order_id, checkout_id, version, payment_attempt_id, policy_receipt_hash,
       state, amount_minor, currency, amount{minor,currency,display},
       capture_evidence{kind,reference,verified_at}|null, razorpay_order_id, razorpay_payment_id,
       refunded_minor, refund_count, created_at, age_seconds

GET /v1/refunds?state=<REFUND_PENDING|REFUND_UNKNOWN|REFUND_FAILED|RECONCILING|ESCALATED|
                       PARTIALLY_REFUNDED|REFUNDED>&limit=1..100&cursor=<opaque>
  -> { refunds: [RefundListItemOut], next_cursor, limit, scope, counts: {every state above} }
  RefundListItemOut: refund_id, order_id|null, checkout_id, payment_attempt_id, amount_minor,
       currency, amount, captured_minor|null, state, row_status, reason, automatic,
       provider_refund_id|null, created_at, updated_at, age_seconds
```

Scope is decided by the request: a buyer session lists its own rows; the same session with
a valid `X-Scenario-Key` lists the tenant's. `scope` in the response says which the caller
got, so the console labels the page from the response rather than assuming. Hand
`next_cursor` back as `cursor`; a mangled cursor is a 400 problem, never an empty page.
Money is the row's integer; `refunded_minor` is the database's SUM over settled rows.
`GET /v1/orders/{id}` now also opens for a scenario-key operator, so the list is clickable.

Two things the console needs that were not in the request, also done:
- `GET /v1/merchants/{merchant_id}/evidence/retained-revenue` no longer requires
  `checkout_id`; omitted, it answers for the newest refused approval in that merchant, else
  the newest confirmed order, else 404. `merchant_id` is the UUID, not the slug.
- The console proxy (`apps/merchant-console/src/app/api/backend/[...path]/route.ts`) now
  mints an OPERATOR session server-side with the scenario key and forwards it as the bearer;
  without a session every operator route was answering 401 and the console was silently
  showing fixtures against a live API. `GET /api/backend/_console/session` returns the
  tenant and merchant UUIDs for pages that need them. Claude made that change in a file
  Gemini owns because it is credential handling; it is recorded in WORK_LEDGER.

---

## What the Case Specialist needs from human_review_service (agent-runtime -> commerce-api)
File(s): packages/agent-runtime/src/agent_runtime/backends/base.py,
packages/agent-runtime/src/agent_runtime/backends/{memory,http}.py,
packages/agent-runtime/src/agent_runtime/core/provenance.py
Why: the Growth Specialist's four merchant tools are built (`catalogue_health_read`,
`inventory_anomalies_read`, `checkout_metrics_read`, `present_metrics`). The Case
Specialist's two, `support_case_read` and `present_case`, are deliberately NOT built and
are reported in `BoundToolset.unbuilt`, because the queue they would read is
`commerce_api.services.human_review_service` and agent-runtime has no protocol reaching
it. A stub would be the one failure a read-only review queue exists to prevent: a case
card drawn from invented evidence looks exactly like a case card drawn from the audit log.
`rendering/cards.py::case_card` is written and waiting; only the read is missing.

Proposed change: a `CaseBackend` protocol in `backends/base.py`, beside `MerchantBackend`
and for the same reason -- a buyer-session backend must not be able to read the review
queue -- with the tool factory building the two case tools only when the backend has it,
exactly as it now does for the merchant reads.

```python
class CaseBackend(ABC):
    async def support_cases(self, limit: int = 20) -> tuple[CaseSummary, ...]: ...
    async def support_case(self, case_key: str) -> CaseRecord: ...
```

Tenant scoping is the backend's, from the authenticated session, never a tool argument:
`case_key` reaches the tool the way `order_id` reaches `order_track`, and a key belonging
to another tenant must be a 404 problem, not an empty record.

`CaseRecord` needs exactly what `case_card` renders and what specification 6.4.3 promises
a reviewer, and nothing else:

- `case_key: str`
- `reason_code: str` -- the `RecoveryCode` value verbatim; a closed vocabulary the card
  renders and the agent may not reword into a cause
- `state: str`, `priority: str` -- closed vocabularies too (`AWAITING_HUMAN`, `P1`..`P3`)
- `provider_state_at_escalation: str | None` -- the state verified when the case opened,
  NOT re-read now. Absent when the provider was never reached, which is different from
  `UNKNOWN` and must stay different
- `proof_chain_ref: str | None` -- a reference, never a copy of the chain
- `monetary_exposure_minor: int | None` with `currency: str` -- integer minor units, and
  `None` where the exposure is not derivable. Absent is not zero on this card either
- `timeline: tuple[CaseEvent, ...]` where `CaseEvent` is `(at: datetime, event: str,
  detail: Mapping[str, Any])`, already redacted server-side. agent-runtime fences
  merchant- and buyer-authored strings on the way to the model, but it cannot un-leak a
  PII field the service put in `detail`
- `opened_at`, `target_response_by: datetime` -- and please carry `SCOPE_NOTE` and a
  `resolvable_here: bool` that is False, so the limit travels with the data

Also needed, in `core/provenance.py`: `remember_case`/`knows_case` on `SessionProvenance`,
so `present_case` can be held on a case this session did not read, the way `present_plan`
is held on an unread order. Without it `present_case` would render whatever key a model
named, which on a review queue is the worst possible place for a guessed identifier.

The HTTP side already exists (`routers/review.py`: `GET /queue`, `GET /queue/{case_key}`),
so `HttpBackend` should be able to implement this without new endpoints; `InMemoryBackend`
needs a fixture queue for the agent-runtime suite, which has no database.
Status: OPEN
