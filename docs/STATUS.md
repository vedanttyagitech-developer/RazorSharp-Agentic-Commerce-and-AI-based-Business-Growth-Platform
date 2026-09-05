# Evidence-driven status

**Verified as of 2026-09-05, 07:20 IST, on branch `main` at commit `63f1712`.**
Every number below was produced by running the command in the Evidence column against this
worktree today. Nothing here is carried forward from another document.

## What this project is

A governed agentic-commerce platform built for the Razorpay AI Buildathon (Track 1). Its
single claim is that **agents propose; deterministic systems authorize and execute.** A
conversational buyer agent may search, quote and assemble a basket, but it can never cause
money to move. Every money action passes through a Transaction Assurance Kernel that
versions the checkout, binds a canonical hash to an explicit human approval, issues a
Policy-at-Sale Receipt fixing the terms the buyer actually saw, admits exactly one
execution under a single-use Execution Grant, and records the whole thing in a tamper-
evident hash chain. When the merchant's state changes underneath an approved checkout, the
kernel refuses the stale approval and requires a fresh one on version N+1. That refusal —
not the shopping — is the product.

## The refusal, verified live on 2026-09-05

Not a test double. The API and the durable worker were running against PostgreSQL, and
these are the real responses, in order:

| # | Call | Result |
| --- | --- | --- |
| 1 | `POST /v1/baskets/{id}/checkout` | 201, version 1 approval card, total **₹721.95** |
| 2 | `POST /v1/checkouts/{id}/versions/1/approve` | 200, state `APPROVED`, approval recorded against the card's content hash |
| 3 | `POST /v1/scenario/injections` `PRICE_SET AMUL-DAIRY-001 13750` | 201, catalogue revision 1 → 2, `unit_price_minor 9900 → 13750` |
| 4 | `POST /v1/checkouts/{id}/versions/1/submit` | **200**, `allowed: false`, `code: REAPPROVAL_REQUIRED`, `explanation: merchant_state_changed_since_approval`, delta `total: 72195 → 79895`, `next_version: 2` |
| 5 | `GET /v1/checkouts/{id}` | state `APPROVAL_REQUIRED`, current version **2**, total **₹798.95** |
| 6 | `POST .../versions/2/approve` | 200 |
| 7 | `POST .../versions/2/submit` | 200, `allowed: true`, single-use execution grant issued |
| 8 | `GET /v1/checkouts/{id}/payment` | 200, state `CREATED`, ₹798.95 |

Three things in that table are the whole submission. The refusal arrived as **HTTP 200**,
because a denial is the platform working correctly and a 4xx would tell every client in
the chain to retry it as a fault (ADR 0003 D15). The delta names the field, the approved
value and the current one, so the buyer is shown what moved rather than told to try again.
And version 1 was never revived: the kernel required a fresh approval on version 2, and
only then issued a grant.

Two contract details were found by running this rather than by reading the OpenAPI
document, which declares these routes as raw responses:

- `POST /v1/baskets/{id}/checkout` answers with version 1's **approval card**, not a
  checkout, because the point of the call is to put the exact bytes and amount in front of
  the buyer.
- `approve` requires the buyer to **echo back** `content_hash`, `amount_minor` and
  `currency`. Consent is bound to what was on screen, so a card that moved underneath the
  buyer is refused rather than silently approved. The storefront's client therefore takes
  the whole card rather than an id and a version, which makes the mistake unrepresentable.

## Real Razorpay, verified live on 2026-09-05

The one item that had never been done. The durable worker reached Razorpay's test API and
created real orders, under single-use execution grants, in the same run as the refusal above:

```
POST https://api.razorpay.com/v1/orders  ->  200 OK   order_TYBD5rc3noKwlL   (v1, ₹721.95)
POST https://api.razorpay.com/v1/orders  ->  200 OK   order_TYBDXtQ03GfFkG   (v2, ₹798.95)
```

Four things are worth checking in the rows behind that, because each is a claim this
project makes rather than a detail:

- **The grants were consumed, once each.** Both `execution_grants` rows read `CONSUMED`
  with a `consumed_at` that precedes the HTTP call, because a grant is spent inside the
  committed transaction *before* the network request goes out. A crash between the two
  therefore loses the money action rather than repeating it.
- **A third grant reads `EXPIRED`**, and its outbox command is `DEAD` with
  `AUTHORITY_INSUFFICIENT`. That is a command left over from an earlier run whose grant
  aged out; the worker refused to execute it rather than reviving stale authority. A dead
  letter here is the system being careful, not a failure.
- **Headers were recorded by name only** — `['Accept', 'Content-Type']`. The
  `provider_requests` table keeps the method, the URL, an HTTP status, the provider's
  identifier and a body digest, and never a credential.
- **Only the worker talks to the provider.** The API process holds no HTTP client for
  Razorpay at all (ADR 0003), which is why the storefront's payment handoff can be read
  before the provider order exists and simply reports `state: CREATED`.

## Status vocabulary

| Term | Means |
| --- | --- |
| **Verified** | A test in this repository proves it, the test passes today, and CI runs it. |
| **Implemented-unproven** | Source exists and type-checks, but no test proves the behaviour end to end, or the code has never been executed against the real dependency. |
| **In progress** | Being written right now; not complete at the timestamp above. |
| **Not started** | No source exists in this repository. |

## The one sentence that matters most

**No real Razorpay payment has ever been executed end to end by this system.** Not in test
mode, not once. The Razorpay adapter builds requests, verifies signatures and parses
webhooks correctly against fixtures — 298 tests prove that — but no code in this repository
has ever opened a socket to `api.razorpay.com`. The `razorpay_live` pytest marker is
declared in `pyproject.toml` and **used by zero tests**: `grep -rn "razorpay_live" packages/`
returns nothing. The HTTP transport that would make the call
(`packages/durable-worker/src/durable_worker/transport.py`, 170 lines) exists but has no
tests and no caller, because `durable_worker.main` does not exist yet — the deployment
validation script reports exactly that as a blocked check. Until a Razorpay test-mode order
id and payment id appear in this file, step 9 of the eleven-step demonstration is unproven.

## Capability status

### Foundations — proven

| Capability | Status | Evidence |
| --- | --- | --- |
| Money in integer minor units | **Verified** | `packages/commerce-domain/tests/test_money.py`, 23 tests incl. a Hypothesis allocation-conservation property. `uv run --no-sync python -m pytest packages/commerce-domain -o addopts="" -q` → 65 passed |
| RFC 8785 JCS canonicalization (integer profile) | **Verified** | `commerce-domain/tests/test_jcs.py`, 32 tests: UTF-16 key ordering, float rejection |
| Canonical checkout hash | **Verified** | `commerce-domain/tests/test_hashing.py`, 7 tests against a frozen regression vector |
| UUIDv7 identifiers | **Verified** | `commerce-domain/tests/test_ids.py`, 3 tests |
| Tenant isolation by forced row-level security | **Verified** | `platform-db/tests/test_tenant_isolation.py`, 14 tests incl. `test_alternating_tenants_on_one_pooled_connection` and `test_unset_tenant_returns_no_rows_fails_closed` |
| Database role separation | **Verified** | Same file: `test_app_role_cannot_write_financial_tables`, `test_audit_events_cannot_be_updated_by_any_application_role`, `test_audit_events_cannot_be_deleted` |
| Schema shape | **Verified, with one stated exception** | `psql -At -c "select count(*) from information_schema.tables where table_schema='public'" commerce_test` → **24**; forced-RLS tables → **20**. The four without are `alembic_version`, `tenants`, `platform_operating_modes` and `api_sessions`; see "Why the isolation suite is trustworthy" below — `api_sessions` is a deliberate, tested exception, not an oversight |
| Migrations | **Verified** | **4 migrations**, head `7d2a4b9e1f03`. `psql -At -c 'select version_num from alembic_version' commerce_test` → `7d2a4b9e1f03`; `commerce_dev` → the same. `alembic heads`, run from `packages/platform-db`, → `7d2a4b9e1f03 (head)` |
| Schema/state-machine agreement | **Verified** | `platform-db/tests/test_schema_state_agreement.py`, 22 tests: source enum ≡ source constraint ≡ **live database** constraint, bidirectionally |

### Transaction Assurance Kernel — proven

`packages/transaction-kernel`: **13,527 source lines in 18 files, 131 public exports**
(`len(transaction_kernel.__all__)`), **15,171 test lines in 17 files, 1,730 tests passing**
in 16.31s.

| Capability | Status | Evidence |
| --- | --- | --- |
| State machines | **Verified** | `test_states.py`, **1,150 tests** — exhaustive over every (current, incoming) pair for both machines |
| Admission and the single-winner guarantee | **Verified** | `test_admission.py`, 17 tests. `threading.Barrier(2)` releases two real PostgreSQL sessions into the same admission; line 139 asserts `len(allowed) == 1, "expected exactly one winner"`; the loser receives `DUPLICATE_OPERATION`. Not mocked |
| Execution Grants, consume-once | **Verified** | `test_grants.py`, 53 tests: `test_a_second_live_grant_for_the_same_attempt_is_refused`, `test_the_database_itself_refuses_a_second_issued_row`, `test_expiry_is_computed_by_the_database_clock` |
| Policy-at-Sale Receipt | **Verified** | `test_receipts.py`, 53 tests: `test_policy_order_cannot_change_the_hash`; float, Decimal, datetime and bytes terms all refused |
| Authority and revocation epoch | **Verified** | `test_authority.py`, 44 tests: `test_revocation_raises_the_epoch_by_exactly_one_and_marks_the_row`, `test_unbound_tenant_fails_closed` |
| Reservations on the database clock | **Verified** | `test_reservations.py`, 40 tests: `test_expired_on_the_database_clock_is_refused_even_where_a_slow_pod_would_admit`, `test_source_never_consults_an_application_clock` |
| Idempotency | **Verified** | `test_idempotency.py`, 34 tests: `test_duplicate_returns_the_original_response_and_does_not_re_execute`, `test_the_guarded_block_is_never_entered_on_a_replay` |
| Audit hash chain | **Verified** | `test_audit.py`, 59 tests: `test_each_event_chains_to_its_predecessor`, `test_self_hash_is_reproducible_from_the_stored_row_alone`, `test_streams_are_scoped_to_one_tenant` |
| Safe Mode kill switch | **Verified** | `test_safe_mode.py`, 47 tests: `test_buyer_protective_operations_are_never_in_the_blocklist`, `test_refund_grants_are_never_swept_by_activation`, `test_unrecognized_stored_mode_reads_as_safe_mode` (fails closed) |
| Canonical checkout content (ADR D6) | **Verified** | `test_tk_checkout_content.py`, 38 tests, frozen vector |
| Capture evidence and monotonic apply | **Verified** | `test_tk_evidence.py` 59 tests, `test_tk_payments.py` 52 tests |
| Refunds | **Verified** | `test_tk_refunds.py`, 34 tests |

### Supporting services — proven

| Capability | Status | Evidence |
| --- | --- | --- |
| Durable outbox | **Verified** | `packages/durable-work`, 1,538 src lines, **130 tests**: `test_committing_the_state_change_publishes_the_command_with_it`, `test_command_is_invisible_to_other_sessions_until_the_caller_commits`, plus SKIP LOCKED leasing, lease expiry and dead-lettering |
| Merchant simulator (catalogue, fees, grounding, scenarios) | **Verified** | `packages/merchant-sim`, 3,121 src lines in 12 files, **155 tests**; deterministic fee engine and scenario controller |
| Razorpay adapter — request building, signatures, webhooks, refunds | **Verified against fixtures** | `packages/payment-adapters`, 3,550 src lines, **298 tests**: raw-body constant-time HMAC, `test_a_forged_event_cannot_pre_claim_a_key_and_suppress_the_real_one`, `test_the_first_delivery_is_accepted_and_the_replay_is_a_duplicate`, test-key guard. **No network call is ever made** |

### Being written right now — snapshot, not a claim

Five other agents are writing in this worktree at the timestamp above. These rows are a
point-in-time reading and will be out of date within the hour.

| Capability | Status | Evidence |
| --- | --- | --- |
| HTTP API (`commerce-api`) | **In progress** | 6,174 src lines in 31 files; **13 router modules** against ADR 0003's ~35-route catalogue. `uv run --no-sync python -m pytest packages/commerce-api -o addopts="" -q` at 00:22 → **32 passed**, from a single file `test_capi_foundation.py`. The journey, payments, evidence and scenario suites named in the build plan do not exist yet |
| Durable worker (`durable-worker`) | **In progress** | 729 src lines in 5 files (`settings.py`, `transport.py`, `faults.py`, a `handlers/` package). **Zero tests.** `durable_worker.main` does not exist — `scripts/validate_infra.sh` reports it as a blocked check |
| End-to-end journey over HTTP | **Not started** | No test carries a basket from `/v1/baskets` to a captured payment. The `e2e` marker is declared and unused |

### Buyer storefront

| Capability | Status | Evidence |
| --- | --- | --- |
| Next.js buyer storefront | **Verified as a front end** | `apps/buyer-web`, 9,172 lines across 53 TS/TSX files. `npm test` → **25 tests passed in 7 files**. Components cover the sixteen spec-8.2 UI states (`components/journey-rail.tsx`), the trusted approval card, quote breakdown, evidence drawer and Razorpay launcher |
| Storefront wired to the real API | **Not started** | `src/lib/api/index.ts` selects `live` by default but has only ever been exercised against `mock.ts`, a deterministic fixture client. No test and no manual run has connected it to `commerce-api` |

### Not built

| Capability | Status | Evidence |
| --- | --- | --- |
| RazorAI agents (ADK / Gemini) | **Not started** | No package. `find . -type d -name "*agent*"` returns nothing. Earlier documents claimed "3,153 lines exist"; that code is **not in this worktree** |
| Realtime voice (STT/TTS, barge-in, echo gate) | **Not started** | No backend package. The only voice code is `apps/buyer-web/src/lib/voice/transcript.ts`, 84 lines of client-side transcript formatting with one test file. Earlier documents claimed "4,148 lines, 91 of 97 tests pass"; that code is **not in this worktree** |
| Reconciliation Service | **Not started** | Bounded-attempt policy is fixed in ADR D13 (6 attempts, exponential backoff, then `ESCALATED`); no implementation |
| Resolution Service and human-review queue | **Not started** | No source |
| Merchant Copilot, Growth Engine, retained-revenue metrics | **Not started** | The only hits for `retained_revenue` are two in-flight `commerce-api` files; no merchant console exists |
| Merchant onboarding, immutable configuration versions | **Not started** | No source |
| UCP 2026-08-25 | **Not started** | `grep -rln "UCP" packages/*/src` → 2 files, both **docstring mentions** in `commerce-domain`. No schemas, no lifecycle |
| AP2 v0.2 human-present cryptography | **Not started** | No `jwcrypto` import anywhere in `packages/`. No mandates, no signing, no verification |
| ACP 2026-04-17 | **Not started** | No source |
| MCP | **Not started** | `find . -name "*mcp*"` returns nothing |
| Protocol Inspector | **In progress, data side only** | `commerce-api/routers/inspector.py` exists and is in flight; no UI |
| GKE / Cloud SQL deployment | **Implemented-unproven** | `infra/` holds Terraform (15 files), a Kustomize base with dev and demo overlays, and three Dockerfiles. `./scripts/validate_infra.sh` passes: terraform fmt and offline validate, kubeconform strict with 0 skipped schemas, all three images build, `buyer-web:dev` serves HTTP 200. The script itself reports **"PENDING DEPLOYMENT OPERATIONS": terraform apply, Cloud SQL bootstrap, Secret Manager population and GKE rollout have never been run.** Nothing has touched a real GCP project |
| The eleven-step demonstration (spec 2.5) | **Not started as a live run** | Steps 1–8 have kernel-level proofs but no HTTP path; step 9 has never executed; steps 10–11 have no endpoint that has been called. The full sequence has been walked **only** by `apps/buyer-web/src/lib/api/mock.ts`, a browser-side fixture with no backend |

## Totals, measured today

```
uv run --no-sync python -m pytest packages/<name> -o addopts="" -q
  commerce-domain      65 passed in 0.33s
  platform-db          95 passed in 0.81s
  transaction-kernel 1730 passed in 16.31s
  durable-work        130 passed in 16.11s
  merchant-sim        155 passed in 0.66s
  payment-adapters    298 passed in 0.18s
  ------------------------------------------
  stable backend     2473 passed
  commerce-api         32 passed  (in flight, 00:22 snapshot)
  durable-worker        0 tests

cd apps/buyer-web && npm test
  Test Files  7 passed (7)
       Tests 25 passed (25)

uv run --no-sync mypy <the six stable packages>/src
  Success: no issues found in 58 source files
```

## What is explicitly NOT done

Stated plainly, because a submission candid about its gaps is worth more than one that is
not:

1. **No money has ever moved.** Zero Razorpay API calls, zero test-mode orders, zero payment
   ids, zero captures. The adapter is proven against fixtures only.
2. **The worker cannot start.** `durable_worker.main` does not exist, so nothing drains the
   outbox and nothing talks to a provider.
3. **The API has one test file.** 6,174 lines of routers and services are covered by 32
   foundation tests. The journey, payment, evidence and scenario suites are not written.
4. **The storefront has never spoken to the backend.** Its entire demonstrated journey is a
   browser-side mock.
5. **There is no agent layer.** The project is called agentic commerce and there is currently
   no agent — no ADK, no Gemini call, no tool loop. The kernel is built to govern an agent
   that does not yet exist.
6. **There is no voice.** No STT, no TTS, no gateway.
7. **No protocol is implemented.** UCP, AP2, ACP and MCP are all zero lines. The AP2
   dependency set was proven to *resolve* on Python 3.14; that is a packaging fact, not an
   implementation.
8. **Nothing is deployed.** The infrastructure validates offline and has never been applied.
9. **CI type-checks one package.** `.github/workflows/ci.yml` runs
   `mypy packages/commerce-domain/src` only. The 58-file clean result above was produced by
   hand today; CI does not enforce it.
10. **Reconciliation, resolution, human review, merchant onboarding and the merchant console
    do not exist**, so specification section 34's acceptance criteria are not close to met.

## Deliberate profile restrictions

**JCS canonicalizes integers only.** RFC 8785's hardest requirement is ECMAScript number
serialization. This domain has no non-integer numbers: money is integer minor units;
quantities, versions and epoch timestamps are integers. A float reaching canonicalization is
a bug — most likely money that escaped the `Money` type — so it raises rather than rounds.
Enabling floats requires implementing ECMAScript number serialization and proving it against
the official RFC 8785 number vectors first.

**One API process.** ADR 0003 D14: the merchant simulator's state lives in the API process,
so settings refuse `WEB_CONCURRENCY > 1`. This is a demonstration restriction, not a
production design.

## Verified environment facts

- Python **3.14.6**. The full dependency set resolves and imports: FastAPI, Pydantic,
  SQLAlchemy 2, psycopg 3, Alembic, httpx, `google-adk` 2.8.0, `google-genai` 2.22.0,
  `google-cloud-texttospeech` 2.37.0, `razorpay` 2.0.1. The Google and Razorpay libraries are
  **installed and importable but not called by any source file in this repository.**
- **AP2 pins `==` on three libraries** (`jwcrypto==1.5.6`, `cryptography==46.0.5`,
  `pydantic==2.12.5`). Resolution succeeds only when AP2's pins take precedence. Do not raise
  those three independently. AP2 publishes no PyPI package; it is pinned by git commit
  `b4587ac1d055888a73b4b21750973cffba961793`.
- PostgreSQL 16 local. `commerce_test` and `commerce_dev` are both at migration head
  `7d2a4b9e1f03`.

## Why the isolation suite is trustworthy

The isolation suite connects as `commerce_test_kernel`, a `NOSUPERUSER NOBYPASSRLS` login
role, and asserts both flags before running. PostgreSQL superusers bypass row-level security
unconditionally, so the same suite run as the database owner would pass while proving
nothing. `scripts/bootstrap_test_roles.sql` creates the roles.

`FORCE ROW LEVEL SECURITY` is set on every protected table, because without it the table
owner is exempt from its own policies. **20 of the 24 tables carry it. The four without are
`alembic_version`, `tenants`, `platform_operating_modes` and `api_sessions`**, and only the
last is a judgement call worth stating in public: `api_sessions` has a `tenant_id NOT NULL`
column but no policy, because resolving a bearer token is how a request *learns* its tenant
and the lookup must therefore work before any tenant is bound. The decision is deliberate and
tested — `platform-db/tests/test_pdb_service_tables.py::test_api_sessions_has_no_row_level_security`
and `::test_api_session_is_readable_without_a_tenant_bound` assert it — and the only thing
standing between one tenant and another tenant's session row is that the lookup key is an
unguessable 64-character token hash under a unique constraint. That is a real, narrow
exception to the isolation claim, and it should be said aloud rather than averaged into
"forced RLS everywhere".

Per ADR 0003 D12, the repository-root `conftest.py` fails the run if any `db`-marked test
**skips** under CI or `REQUIRE_DB=1`, because a missing role would otherwise turn every proof
above into a silent green.
