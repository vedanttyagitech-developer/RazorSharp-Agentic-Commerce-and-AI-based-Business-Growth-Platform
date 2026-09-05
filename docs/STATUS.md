# Evidence-driven status

**Verified 2026-09-05, 10:31 IST, on branch `main` at commit `5993fea`, working tree clean.**

Every number below was produced by running the command in the Evidence column against this
worktree today. Nothing here is carried forward from another document, and where an earlier
version of this file made a claim that is no longer true, the claim is corrected rather than
quietly dropped — the section "What this file said last time and got wrong" names each one.

Two cautions about reading it. Six agents were writing in this worktree while it was measured;
the figures here were all taken after the last of them committed, against a clean tree, but
they will move again. And a count is not a proof: the rows below name the command that
produced each number, so the next person can disagree by running it.

## What this project is

A governed agentic-commerce platform built for the Razorpay AI Buildathon (Track 1). Its
single claim is that **agents propose; deterministic systems authorize and execute.** A
conversational buyer agent may search, quote and assemble a basket, but it can never cause
money to move. Every money action passes through a Transaction Assurance Kernel that
versions the checkout, binds a canonical hash to an explicit human approval, issues a
Policy-at-Sale Receipt fixing the terms the buyer actually saw, admits exactly one execution
under a single-use Execution Grant, and records the whole thing in a tamper-evident hash
chain. When the merchant's state changes underneath an approved checkout, the kernel refuses
the stale approval and requires a fresh one on version N+1. That refusal — not the shopping
— is the product.

## The refusal, reproduced live today

Not a fixture and not a replayed transcript. The API and the durable worker were running
against PostgreSQL, and this is the sequence as it answered at 10:19 IST:

| # | Call | Result |
| --- | --- | --- |
| 1 | `POST /v1/baskets/{id}/checkout` | `201`, version 1 approval card, `amount_minor` **8550** |
| 2 | `POST /v1/checkouts/{cid}/versions/1/approve` | `200`, state `APPROVED`, bound to the card's own `content_hash` |
| 3 | `POST /v1/scenario/injections` `PRICE_SET AMUL-DAIRY-001 80989` | `201` |
| 4 | `POST /v1/checkouts/{cid}/versions/1/submit` | **`200`**, `allowed: false`, `code: REAPPROVAL_REQUIRED`, `explanation: merchant_state_changed_since_approval`, `next_version: 2` |
| 5 | delta carried by that refusal | `{field_path: total, approved: 8550, current: 161978, reason: total_changed}` |
| 6 | `GET /v1/checkouts/{cid}` | state `APPROVAL_REQUIRED`, `current_version` **2**, versions `[(1, INVALIDATED), (2, APPROVAL_REQUIRED)]` |

Three things in that table are the whole submission. The refusal arrived as **HTTP 200**,
because a denial is the platform working correctly and a 4xx would tell every client in the
chain to retry it as a fault (ADR 0003 D15). The delta names the field, the approved value
and the current one, so the buyer is shown what moved rather than told to try again. And
version 1 was never revived: it reads `INVALIDATED`, and version 2 asks for its own consent.

The seeded price was put back to 2800 paise afterwards, and `GET /v1/catalogue/search`
confirms `AMUL-DAIRY-001` at `{minor: 2800, currency: INR}` with 48 units. A verification run
that leaves the demo catalogue moved is a verification run that breaks the next one.

Two contract details were found by running this rather than by reading the OpenAPI document,
which declares these routes as raw responses:

- `POST /v1/baskets/{id}/checkout` answers with version 1's **approval card**, not a
  checkout, because the point of the call is to put the exact bytes and amount in front of
  the buyer.
- `approve` requires the buyer to **echo back** `content_hash`, `amount_minor` and
  `currency`. Consent is bound to what was on screen, so a card that moved underneath the
  buyer is refused rather than silently approved. Both frontends therefore take the whole
  card rather than an id and a version, which makes the mistake unrepresentable.

A third detail is worth stating because it costs a caller a 400 otherwise: **every mutation
must carry an `Idempotency-Key` header.** `POST /v1/baskets` with no key answers `400` with a
problem document saying so.

## Real Razorpay, and exactly how far it has gone

The `provider_requests` table records every call the durable worker made to the provider.
Read straight out of `commerce_dev`:

```
operation             | http_status | outcome_code           | count
PAYMENT_CREATE_ORDER  | 200         | OK                     | 27
PAYMENT_FETCH         | 400         | HUMAN_REVIEW_REQUIRED  |  5
REFUND_EXECUTE        | (none)      | PAYMENT_UNKNOWN        |  1
REFUND_EXECUTE        | 400         | PAYMENT_FAILED         |  1
```

So: **27 real orders were created at `https://api.razorpay.com/v1/orders`,** each returning
200 with a provider order id (`order_TYE5c6qVzwIpMV` is the most recent). Alongside them,
`payment_attempts` holds 29 rows, 27 carrying a provider order id and **8 carrying a provider
payment id**, and the `orders` table holds **8 rows, all `CONFIRMED`**. Three refunds exist.

Four things behind those rows are claims this project makes rather than details:

- **Grants are spent, once each.** `execution_grants` reads **29 `CONSUMED`, 2 `EXPIRED`,
  1 `REVOKED`**. A grant is consumed inside the committed transaction *before* the network
  request goes out, so a crash between the two loses the money action rather than repeating
  it. The two `EXPIRED` rows are grants that aged out and were refused rather than revived;
  the outbox shows **2 `DEAD`** alongside **34 `DONE`** and 2 `PENDING`. A dead letter here
  is the system being careful.
- **Credentials were never recorded.** `header_names` on every provider request reads
  `["Accept", "Content-Type"]`. The table keeps the method, the URL, the status, the
  provider's identifier and a body digest, and never a secret.
- **A provider failure escalates rather than guesses.** The five `PAYMENT_FETCH` rows that
  came back 400 are recorded as `HUMAN_REVIEW_REQUIRED`, and one `REFUND_EXECUTE` with no
  HTTP status at all — a transport failure — is recorded as `PAYMENT_UNKNOWN`. Neither was
  resolved into a settlement fact by the code that observed it.
- **Only the worker talks to the provider.** The API process holds no HTTP client for
  Razorpay (ADR 0003), which is why the storefront's payment handoff can be read before the
  provider order exists and simply reports `state: CREATED`.

What has **not** happened: no payment has been captured through a real Razorpay checkout
sheet driven by a human in this worktree today. The 8 provider payment ids and the 8
`CONFIRMED` orders came through the payment path with provider evidence applied; the
`razorpay_live` pytest marker is declared in `pyproject.toml` and still **used by zero
tests** (`grep -rn "razorpay_live" packages/` returns nothing), so nothing in CI reaches the
provider.

## Gates, run today

All three gate sets are green at the commit named above. These are the exact final lines.

```
export PATH="$HOME/.local/bin:$PATH"

uv run --no-sync ruff check packages/                     -> All checks passed!
uv run --no-sync ruff format --check packages/            -> 283 files already formatted
uv run --no-sync mypy packages/*/src                      -> Success: no issues found in 185 source files
uv run --no-sync python -m pytest packages/ -o addopts="" -q
                                                          -> 3808 passed, 6 warnings in 55.92s

cd apps/buyer-web
npm run typecheck   -> clean, no output after `> tsc --noEmit`
npm run lint        -> clean, no output after `> eslint`
npm run test        -> Test Files  6 passed (6) / Tests  135 passed (135)
npm run build       -> 11 routes emitted; last line `ƒ  (Dynamic)  server-rendered on demand`
npm run e2e         -> 10 passed (10.4s)

cd apps/merchant-console
npm run typecheck   -> clean, no output after `> tsc --noEmit`
npm run lint        -> clean, no output after `> eslint`
npm run test        -> Test Files  4 passed (4) / Tests  50 passed (50)
npm run build       -> 9 routes emitted; last line `ƒ  (Dynamic)  server-rendered on demand`
npm run e2e         -> 8 passed (4.5s)
```

Every command above was re-run against a clean working tree at the commit named at the top
of this file, after the last of the concurrent agents had committed. An earlier pass of the
same commands, taken while a `/review` page was still uncommitted in the console's tree,
reported eight routes; that measurement described a tree nobody would ever check out again,
which is precisely the failure this revision exists to correct, so it was thrown away rather
than reconciled.

`mypy` runs strict. Both end-to-end suites talk to the live API on `127.0.0.1:8000` and to a
live dev server, and both restore whatever they moved.

## Backend totals, measured today

Each row is `uv run --no-sync python -m pytest packages/<name> -o addopts="" -q`. The rows
sum to 3808, which is the whole-suite figure above; they are not two independent estimates.

| Package | Tests | Source | Tests, as code |
| --- | ---: | ---: | ---: |
| `commerce-domain` | 65 | 407 lines, 6 files | 294 lines, 4 files |
| `platform-db` | 232 | 1,496 lines, 7 files | 1,874 lines, 5 files |
| `transaction-kernel` | 1,730 | 13,527 lines, 18 files | 15,176 lines, 17 files |
| `durable-work` | 130 | 1,538 lines, 3 files | 1,958 lines, 2 files |
| `merchant-sim` | 167 | 5,537 lines, 12 files | 1,944 lines, 7 files |
| `payment-adapters` | 298 | 3,550 lines, 12 files | 3,081 lines, 10 files |
| `commerce-protocols` | 367 | 8,515 lines, 31 files | 6,704 lines, 8 files |
| `commerce-api` | 200 | 17,255 lines, 43 files | 7,587 lines, 11 files |
| `durable-worker` | 63 | 3,180 lines, 12 files | 2,580 lines, 7 files |
| `agent-runtime` | 556 | 11,209 lines, 41 files | 7,078 lines, 21 files |
| **Total** | **3,808** | | |

## Capability status

| Term | Means |
| --- | --- |
| **Verified** | A test in this repository proves it and the test passes today. |
| **Verified live** | Additionally exercised against the running API, the real database, or the real provider, today. |
| **Implemented-unproven** | Source exists and type-checks, but no test proves the behaviour end to end. |
| **Not started** | No source exists in this repository. |

### Foundations

| Capability | Status | Evidence |
| --- | --- | --- |
| Money in integer minor units | **Verified** | `commerce-domain/tests/test_money.py`, incl. a Hypothesis allocation-conservation property. Package total 65 passed |
| RFC 8785 JCS canonicalization (integer profile) | **Verified** | `test_jcs.py`: UTF-16 key ordering, float rejection |
| Canonical checkout hash | **Verified** | `test_hashing.py`, against a frozen regression vector |
| Tenant isolation by forced row-level security | **Verified** | `platform-db/tests/test_tenant_isolation.py`, incl. `test_alternating_tenants_on_one_pooled_connection` and `test_unset_tenant_returns_no_rows_fails_closed` |
| Database role separation | **Verified** | Same file: the app role cannot write financial tables; audit events cannot be updated or deleted by any application role |
| Schema shape | **Verified, with one stated exception** | `psql -At -c "select count(*) from information_schema.tables where table_schema='public'" commerce_test` → **24**; forced-RLS tables → **20**. The four without are `alembic_version`, `tenants`, `platform_operating_modes` and `api_sessions`; see "Why the isolation suite is trustworthy" |
| Migrations | **Verified, with a drift to report** | **5 migrations**; `alembic heads` from `packages/platform-db` → `a4e17c93b5d2 (head)`. `commerce_test` is at `a4e17c93b5d2`. **`commerce_dev` is at `7d2a4b9e1f03`, one behind** — it is missing `a4e17c93b5d2_keyset_indexes_for_collections`. Cursor pagination works on it and is unindexed |
| Schema/state-machine agreement | **Verified** | `test_schema_state_agreement.py`: source enum ≡ source constraint ≡ live database constraint, bidirectionally |

### Transaction Assurance Kernel

13,527 source lines in 18 files; 15,176 test lines in 17 files; **1,730 tests passing**.

| Capability | Status | Evidence |
| --- | --- | --- |
| State machines | **Verified** | `test_states.py` — exhaustive over every (current, incoming) pair for both machines |
| Admission and the single-winner guarantee | **Verified** | `test_admission.py`. A `threading.Barrier(2)` releases two real PostgreSQL sessions into one admission and asserts exactly one winner; the loser gets `DUPLICATE_OPERATION`. Not mocked |
| Execution Grants, consume-once | **Verified live** | `test_grants.py`: a second live grant is refused, the database itself refuses a second issued row, expiry is on the database clock. Live: 29 `CONSUMED`, 2 `EXPIRED`, 1 `REVOKED` in `commerce_dev` |
| Policy-at-Sale Receipt | **Verified** | `test_receipts.py`: policy order cannot change the hash; float, Decimal, datetime and bytes terms are all refused |
| Authority and revocation epoch | **Verified** | `test_authority.py`: revocation raises the epoch by exactly one; an unbound tenant fails closed |
| Reservations on the database clock | **Verified** | `test_reservations.py`: expired on the database clock is refused where a slow pod would admit |
| Idempotency | **Verified live** | `test_idempotency.py`: a duplicate returns the original response and the guarded block is never re-entered. Live: `POST /v1/baskets` with no key answers 400 |
| Audit hash chain | **Verified live** | `test_audit.py`: each event chains to its predecessor; the self-hash is reproducible from the stored row alone. **1,736 audit events** in `commerce_dev` |
| Safe Mode kill switch | **Verified** | `test_safe_mode.py`: buyer-protective operations are never in the blocklist; refund grants are never swept; an unrecognized stored mode reads as safe mode (fails closed) |
| Capture evidence, monotonic apply, refunds | **Verified** | `test_tk_evidence.py`, `test_tk_payments.py`, `test_tk_refunds.py` |

### HTTP API and durable worker

| Capability | Status | Evidence |
| --- | --- | --- |
| `commerce-api` | **Verified live** | 17,255 src lines, **17 router modules**, **50 OpenAPI paths / 51 operations**. **200 tests** across ten files: `test_capi_foundation` 32, `test_capi_agent` 25, `test_capi_scenario` 24, `test_capi_journey` 21, `test_capi_payments` 20, `test_capi_protocols` 17, `test_capi_evidence` 14, `test_capi_boundary` 11, `test_capi_listing` 10, `test_capi_review` 26 |
| `durable-worker` | **Verified live** | 3,180 src lines; `durable_worker.main` **exists** and is the running process. **63 tests**: `test_dwk_loop` 21, `test_dwk_create_order` 12, `test_dwk_import_boundary` 8, `test_dwk_reconcile` 8, `test_dwk_refund` 7, `test_dwk_webhook` 7. Its provider calls are the 27 rows above |
| Durable outbox | **Verified live** | `durable-work`, 130 tests: a command is published with the state change it belongs to and is invisible to other sessions until the caller commits; SKIP LOCKED leasing, lease expiry, dead-lettering. Live: 34 `DONE`, 2 `DEAD`, 2 `PENDING` |
| Merchant simulator | **Verified live** | `merchant-sim`, 167 tests. Live catalogue: `matched: 247` across **10 categories** summing to 247 |
| Razorpay adapter | **Verified against fixtures, exercised live** | `payment-adapters`, 298 tests: raw-body constant-time HMAC, a forged event cannot pre-claim a key and suppress the real one, first delivery accepted and replay is a duplicate. The adapter itself makes no network call; the worker does |
| Reconciliation, Resolution, human review | **Verified** | `commerce-api/services/reconciliation_service.py`, `resolution_service.py`, `human_review_service.py`, four GET routes under `/v1/review`, **26 tests** in `test_capi_review.py`. 5 `reconciliation_runs` rows exist. A divergence becomes a finding, never a row; no plan is applied automatically |

### Agent layer and protocols

| Capability | Status | Evidence |
| --- | --- | --- |
| RazorAI agent runtime | **Verified** | `agent-runtime`, 11,209 src lines in 41 files, **556 tests** across 18 files: `test_ar_messages` 106, `test_ar_fencing` 52, `test_ar_backends` 48, `test_ar_grounding_rules` 45, `test_ar_routing` 35, `test_ar_harness` 33, `test_ar_specialists` 31, `test_ar_provenance` 28, `test_ar_capabilities` 27, `test_ar_merchant_tools` 27, `test_ar_http_merchant` 22, `test_ar_language` 22, `test_ar_adk_adapter` 18, `test_ar_prompts` 17, `test_ar_merchant_equivalence` 15, `test_ar_turn` 14, `test_ar_grounding` 9, `test_ar_injection` 7 |
| UCP | **Verified** | `commerce-protocols/ucp`, `test_cp_ucp.py` 41 tests |
| AP2 human-present cryptography | **Verified** | `commerce-protocols/ap2` — `signing.py`, `mandates.py`, `receipts.py`, `vectors.py`, all importing `jwcrypto`. `test_cp_ap2.py` 49 + `test_cp_ap2_verify.py` 18 tests |
| ACP | **Verified** | `commerce-protocols/acp`, `test_cp_acp.py` 120 tests |
| MCP | **Verified** | `commerce-protocols/mcp`, `test_cp_mcp.py` 86 tests |
| Cross-protocol invariants | **Verified** | `test_cp_invariants.py` 15, `test_cp_core.py` 38 |
| Protocols over HTTP | **Verified** | `commerce-api/routers/protocols.py`, `test_capi_protocols.py` 17 tests |

### Frontends

| Capability | Status | Evidence |
| --- | --- | --- |
| Buyer storefront | **Verified live** | `apps/buyer-web`, **12,185 lines across 55 TS/TSX files**, 11 routes. 135 unit tests in 6 files; **10 Playwright specs pass against the live API**, including a run that approves, injects a `PRICE_SET`, presses Pay, and reads the old total, the new total and the difference back from the server |
| Storefront wired to the real API | **Verified live** | There is one lane and no mock module. `src/lib/api/` holds `client.ts`, `problem.ts`, `types.ts` and nothing else; the e2e suite drives the browser against `127.0.0.1:8000` |
| Merchant console | **Verified live** | `apps/merchant-console`, **6,533 lines across 28 TS/TSX files**, 9 routes. 50 unit tests in 4 files; **8 Playwright specs pass against the live API**, asserting against data the test itself fetched through the console's own proxy |
| Console security posture | **Verified live** | A nonce-based CSP built per request in `src/lib/security/csp.ts` and set by `src/middleware.ts`; `curl -I http://localhost:3001/catalogue` returns exactly one `Content-Security-Policy` header carrying that nonce. The proxy refuses a cross-site write (403), refuses to mint a session on a page's behalf (404), and answers 401 with no cookie |
| Protocol Inspector | **Verified live** | `commerce-api/routers/inspector.py` plus the console's `/inspector` route |

### Not started

| Capability | Status | Evidence |
| --- | --- | --- |
| Realtime voice (STT/TTS, barge-in, echo gate) | **Not started** | No backend package. No voice module remains in either frontend |
| Merchant onboarding, immutable configuration versions | **Not started** | No source |
| GKE / Cloud SQL deployment | **Implemented-unproven** | `infra/` holds Terraform, a Kustomize base with dev and demo overlays, and Dockerfiles. `./scripts/validate_infra.sh` validates offline. `terraform apply`, Cloud SQL bootstrap, Secret Manager population and GKE rollout have **never been run.** Nothing has touched a real GCP project |

## What this file said last time and got wrong

The task that produced this revision found four figures in the previous version carried
forward from a state that no longer existed. Naming them is the point of the exercise:

1. **"No real Razorpay payment has ever been executed end to end by this system. Not in test
   mode, not once."** That sentence was printed under the heading "The one sentence that
   matters most" in a document whose own second section, four hundred words earlier, listed
   two Razorpay order ids created live. Both halves could not be true. The table above
   replaces the claim with the count: 27 orders created, 8 attempts carrying a provider
   payment id, 8 orders `CONFIRMED`.
2. **"The storefront has never spoken to the backend"** and **"its entire demonstrated
   journey is a browser-side mock."** There is no mock module in the storefront at all, and
   ten Playwright specs drive it against the live API.
3. **"There is no agent layer… no ADK, no Gemini call, no tool loop"** and **"no protocol is
   implemented. UCP, AP2, ACP and MCP are all zero lines."** Those two packages now carry
   556 and 367 passing tests.
4. **"The API has one test file… 32 foundation tests"**, **"the worker cannot start —
   `durable_worker.main` does not exist"**, and **"reconciliation, resolution, human review
   and the merchant console do not exist."** The API has ten test files and 200 tests, the
   worker is the running process, all three services exist with 26 tests over their routes,
   and the console has 50 unit tests and 8 end-to-end specs.

The failure mode was not dishonesty; it was a document updated in one section and not
another, describing a repository that six agents were rewriting underneath it. The remedy is
the rule at the top of this file: every figure is re-measured, and the command that produced
it is named beside it.

## What is still explicitly NOT done

Stated plainly, because a submission candid about its gaps is worth more than one that is
not:

1. **Nothing is fulfilled.** There is no courier, no dispatch and no delivery estimate in any
   response the catalogue sends. Both frontends were audited this pass to remove every
   delivery promise that had crept into their copy and their artwork.
2. **No CI reaches the provider.** The `razorpay_live` marker is declared and used by zero
   tests. The 27 live orders were made by the worker running on this machine.
3. **The demo database is one migration behind head.** `commerce_dev` is at `7d2a4b9e1f03`;
   head is `a4e17c93b5d2`. The keyset indexes for collection pagination are not applied there.
4. **Nothing is deployed.** The infrastructure validates offline and has never been applied.
5. **CI type-checks one package.** `.github/workflows/ci.yml` runs
   `mypy packages/commerce-domain/src` only. The 185-file clean result above was produced by
   hand today; CI does not enforce it.
6. **There is no voice**, and no merchant onboarding or configuration-versioning surface.
7. **The human-review queue is read-only.** No case is assigned, decided, annotated or
   resolved through it; a reviewer acts outside that surface through the operator path, and
   the API says so in a scope note on the response itself.
8. **Both apps still use the deprecated `middleware` file convention.** `next build` prints
   the warning on each. One codemod should migrate both together; neither was migrated this
   pass, because rewriting the file that carries each app's security policy is not something
   to do in the same commit as unrelated work.

## Deliberate profile restrictions

**JCS canonicalizes integers only.** RFC 8785's hardest requirement is ECMAScript number
serialization. This domain has no non-integer numbers: money is integer minor units;
quantities, versions and epoch timestamps are integers. A float reaching canonicalization is
a bug — most likely money that escaped the `Money` type — so it raises rather than rounds.

**One API process.** ADR 0003 D14: the merchant simulator's state lives in the API process,
so settings refuse `WEB_CONCURRENCY > 1`. This is a demonstration restriction, not a
production design.

## Verified environment facts

- Python **3.14.6**. PostgreSQL 16 local, with `commerce_test` and `commerce_dev` both
  present; see the migration-drift note above for which head each carries.
- **AP2 pins `==` on three libraries** (`jwcrypto==1.5.6`, `cryptography==46.0.5`,
  `pydantic==2.12.5`). Resolution succeeds only when AP2's pins take precedence. Do not raise
  those three independently. AP2 publishes no PyPI package; it is pinned by git commit
  `b4587ac1d055888a73b4b21750973cffba961793`.
- The demo tenant is `demo`; operator reads additionally require
  `X-Scenario-Key: local-demo-scenario-key`.

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
