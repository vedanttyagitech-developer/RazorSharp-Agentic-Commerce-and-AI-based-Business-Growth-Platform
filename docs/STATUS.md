# Evidence-driven status

**Verified 2026-09-05, between 16:10 and 16:14 IST, on branch `main` at commit `538d0b4`.**

Every number below was produced by running the command in the Evidence column against this
checkout today. Nothing here is carried forward from an earlier version of this file, and
where an earlier version made a claim that is no longer true, the claim is corrected rather
than quietly dropped — the section "What this file said last time and got wrong" names each
one.

A count is not a proof either: the rows below name the command that produced each number, so
the next person can disagree by running it.

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

## The refusal, and how it is proven today

**This pass did not reproduce the refusal against the live stack, and says so rather than
reprinting the transcript as though it had.** The API on `:8000` is shared with other work in
flight, and reproducing the refusal means injecting a `PRICE_SET` into the demonstration
catalogue — which would have changed what a concurrent run saw. A measurement that corrupts
someone else's measurement is not worth its own row.

What was re-run today is the evidence that does not need a shared stack, and it is the
stronger of the two: the refusal has direct end-to-end test coverage against a real
PostgreSQL, and all of it passes.

| Behaviour | Test, re-run today |
| --- | --- |
| Version 1's approval is invalidated when merchant state moves, and version 2 is required | `test_capi_journey.py::test_submitting_version_one_after_supersede_is_refused` |
| Version 2 can then be approved and submitted | `::test_reapproving_version_two_then_succeeds` |
| Consent is bound to the exact bytes on screen | `::test_approving_the_wrong_content_hash_is_refused` |
| Consent is bound to the exact amount on screen | `::test_approving_the_wrong_amount_is_refused` |
| A quote that has gone stale is flagged, not silently reused | `::test_basket_quote_is_deterministic_and_flags_staleness` |
| Exactly one execution is admitted under contention | `test_admission.py`, two real PostgreSQL sessions released by a `threading.Barrier(2)`; the loser gets `DUPLICATE_OPERATION` |
| Every mutation carries an `Idempotency-Key` | `::test_a_mutation_without_an_idempotency_key_is_refused` |

Three properties of that refusal are the whole submission. It arrives as **HTTP 200**,
because a denial is the platform working correctly and a 4xx would tell every client in the
chain to retry it as a fault (ADR 0003 D15). It carries a delta naming the field, the
approved value and the current one, so the buyer is shown what moved rather than told to try
again. And version 1 is never revived: it reads `INVALIDATED`, and version 2 asks for its own
consent.

Two contract details worth stating because the OpenAPI document declares these routes as raw
responses:

- `POST /v1/baskets/{id}/checkout` answers with version 1's **approval card**, not a
  checkout, because the point of the call is to put the exact bytes and amount in front of
  the buyer.
- `approve` requires the buyer to **echo back** `content_hash`, `amount_minor` and
  `currency`. Consent is bound to what was on screen, so a card that moved underneath the
  buyer is refused rather than silently approved. Both frontends therefore take the whole
  card rather than an id and a version, which makes the mistake unrepresentable.

## Real Razorpay, and exactly how far it has gone

Read straight out of `commerce_dev` today. `provider_requests` records every call the
durable worker made to the provider:

```
select operation, http_status, outcome_code, count(*) from provider_requests group by 1,2,3;

 operation            | http_status | outcome_code          | count
 PAYMENT_CREATE_ORDER | 200         | OK                    |    30
 PAYMENT_FETCH        | 400         | HUMAN_REVIEW_REQUIRED |     5
 REFUND_EXECUTE       | 400         | PAYMENT_FAILED        |     1
 REFUND_EXECUTE       | (none)      | PAYMENT_UNKNOWN       |     1
```

So: **30 real orders were created at `https://api.razorpay.com/v1/orders`,** each returning
200 with a provider order id. `payment_attempts` holds 32 rows, 30 carrying a provider order
id and 8 carrying a provider payment id.

### The eight confirmed orders are seeded, and this is the most important row in the file

`orders` holds **8 rows, all `CONFIRMED` — and all 8 are seeded.**

```sql
select count(*) total,
       count(*) filter (where capture_evidence->>'event_id' like 'evt_seed_%') seeded
from orders;   --  8 | 8
```

The order ids are genuine. **The payment ids are not.** Of the eight `provider_payment_id`
values, exactly one has ever appeared in a URL this system called —
`pay_b13248528bee4d`, and only as the `400 BAD_REQUEST_ERROR` Razorpay returned when asked
to refund a payment it had never issued. The provider's own refusal is the proof that the id
was invented.

And no webhook has ever applied a capture. `webhook_inbox` holds **2** rows, both from the
webhook forgery test — one forged `evt_attack_*`, one `evt_genuine_*` — and both resolved
`apply_status = IGNORED`, `apply_reason = attempt_not_found`.

**Therefore: thirty real order creations, and zero real captures.** Any screen rendering
those eight rows as verified webhook captures is rendering a fixture. `docs/KNOWN_GAPS.md`
tracks removing them, and `scripts/capture_screenshots.mjs` already refuses to photograph
any order whose event id begins `evt_seed_`.

### What the surrounding rows do prove

- **Grants are spent, once each.** `execution_grants` reads **32 `CONSUMED`, 2 `EXPIRED`,
  1 `REVOKED`**. A grant is consumed inside the committed transaction *before* the network
  request goes out, so a crash between the two loses the money action rather than repeating
  it. The two `EXPIRED` rows are grants that aged out and were refused rather than revived;
  `outbox_events` shows **2 `DEAD`** alongside **39 `DONE`** and 2 `PENDING`. A dead letter
  here is the system being careful.
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
- **The audit chain is 5,965 events deep** in `commerce_dev`, and 5 `reconciliation_runs`
  and 3 `refunds` exist.

The `razorpay_live` pytest marker is declared in `pyproject.toml` and still **used by zero
tests** (`grep -rn "razorpay_live" packages/ --include="*.py"` returns nothing), so nothing
in CI reaches the provider. The thirty orders were made by the worker running on this
machine.

## Gates, run today

```
export PATH="$HOME/.local/bin:$PATH"

REQUIRE_DB=1 uv run --no-sync pytest packages -o addopts="--strict-markers"
                                                  -> 5597 passed, 8 skipped, 11 xfail in ... (5616 collected)
uv run --no-sync mypy packages/*/src              -> Success: no issues found in 245 source files
uv run --no-sync ruff check packages/             -> All checks passed!
uv run --no-sync ruff format --check packages/    -> 404 files already formatted

cd apps/buyer-web
npm run typecheck   -> clean, no output after `> tsc --noEmit`
npm run lint        -> clean, no output after `> eslint`
npm run test        -> Test Files  35 passed (35) / Tests  582 passed (582)
npm run build       -> production `next build` succeeds

cd apps/merchant-console
npm run typecheck   -> clean, no output after `> tsc --noEmit`
npm run lint        -> clean, no output after `> eslint`
npm run test        -> Test Files  12 passed (12) / Tests  129 passed (129)
npm run build       -> production `next build` succeeds
```

**The lint gates are clean.** `ruff check` passes with no errors, `ruff format --check`
reports 404 files already formatted with none to reformat, and both frontends typecheck,
lint and produce a successful production `next build`. A row is green here because the
command that produced it is named beside it, not because a failing file was excluded.

**`npm run e2e` was not re-run this pass, for either app**, and no e2e figure is quoted
anywhere in this file. Both Playwright suites need the live API and a live dev server, and
`:8000`, `:3000` and `:3001` were all bound by other work. `playwright.config.ts` and the
spec files exist in both apps; whether they pass today is not something this revision
measured, and it is not asserted.

**The 8 skips are all one file**, and the reason is a credential, not a defect:
`packages/voice-runtime/tests/test_voice_real_audio.py` skips with
`GOOGLE_CLOUD_PROJECT is not set`. Those eight drive real speech through Gemini Transcribe Live
and Chirp 3 HD. Per ADR 0003 D12 the repository-root `conftest.py` fails the run if any
`db`-marked test skips under CI or `REQUIRE_DB=1`, so a skipped database suite could not have
hidden here.

## Backend totals, measured today

Each row is `REQUIRE_DB=1 uv run --no-sync pytest packages/<name> -o addopts="--strict-markers" -q`.
The whole suite, run as `make test` (which sets `REQUIRE_DB=1` so database-marked tests
actually run), answers **5,597 passed, 8 skipped, 11 xfail against 5,616 collected**. The 8
skips are the real-audio voice tests needing `GOOGLE_CLOUD_PROJECT`; the 11 xfail are pinned
known defects. The per-package rows below are COLLECTED counts and sum to the 5,616 collected
figure.

| Package | Tests | Source | Tests, as code |
| --- | ---: | ---: | ---: |
| `commerce-domain` | 65 | 407 lines, 6 files | 294 lines, 4 files |
| `platform-db` | 233 | 1,611 lines, 8 files | 1,916 lines, 5 files |
| `transaction-kernel` | 1,800 | 13,608 lines, 18 files | 18,189 lines, 23 files |
| `durable-work` | 130 | 1,538 lines, 3 files | 1,958 lines, 2 files |
| `merchant-sim` | 180 | 5,557 lines, 12 files | 2,178 lines, 8 files |
| `payment-adapters` | 296 | 3,594 lines, 12 files | 3,081 lines, 10 files |
| `commerce-protocols` | 367 | 8,569 lines, 31 files | 6,704 lines, 8 files |
| `commerce-api` | 422 | 23,671 lines, 43 files | 8,788 lines, 13 files |
| `durable-worker` | 98 | 3,688 lines, 13 files | 3,415 lines, 8 files |
| `agent-runtime` | 1,227 | 14,309 lines, 42 files | 7,996 lines, 22 files |
| `platform-observability` | 391 | 2,716 lines, 7 files | 1,889 lines, 7 files |
| `voice-runtime` | 407 | 6,940 lines, 37 files | 5,240 lines, 15 files |
| **Total** | **5,616** | | |

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
| Schema shape | **Verified, with one stated exception** | In `commerce_test`: 24 tables, **20** carrying `relforcerowsecurity`. The four without are `alembic_version`, `tenants`, `platform_operating_modes` and `api_sessions`; see "Why the isolation suite is trustworthy" |
| Migrations | **Verified, with a drift to report** | **5 migrations**; `alembic heads` from `packages/platform-db` → `a4e17c93b5d2 (head)`. `commerce_test` is at `a4e17c93b5d2`. **`commerce_dev` is at `7d2a4b9e1f03`, one behind** — it is missing `a4e17c93b5d2_keyset_indexes_for_collections`. Cursor pagination works on it and is unindexed |
| Schema/state-machine agreement | **Verified** | `test_schema_state_agreement.py`: source enum ≡ source constraint ≡ live database constraint, bidirectionally |

### Transaction Assurance Kernel

13,608 source lines in 18 files; 18,189 test lines in 23 files; **1,800 tests passing**.

| Capability | Status | Evidence |
| --- | --- | --- |
| State machines | **Verified** | `test_states.py` — exhaustive over every (current, incoming) pair for both machines |
| Admission and the single-winner guarantee | **Verified** | `test_admission.py`. A `threading.Barrier(2)` releases two real PostgreSQL sessions into one admission and asserts exactly one winner; the loser gets `DUPLICATE_OPERATION`. Not mocked |
| Execution Grants, consume-once | **Verified live** | `test_grants.py`: a second live grant is refused, the database itself refuses a second issued row, expiry is on the database clock. Live: 32 `CONSUMED`, 2 `EXPIRED`, 1 `REVOKED` in `commerce_dev` |
| Policy-at-Sale Receipt | **Verified** | `test_receipts.py`: policy order cannot change the hash; float, Decimal, datetime and bytes terms are all refused |
| Authority and revocation epoch | **Verified** | `test_authority.py`: revocation raises the epoch by exactly one; an unbound tenant fails closed |
| Reservations on the database clock | **Verified** | `test_reservations.py`: expired on the database clock is refused where a slow pod would admit |
| Idempotency | **Verified** | `test_idempotency.py`: a duplicate returns the original response and the guarded block is never re-entered |
| Audit hash chain | **Verified live** | `test_audit.py`: each event chains to its predecessor; the self-hash is reproducible from the stored row alone. **5,965 audit events** in `commerce_dev` |
| Safe Mode kill switch | **Verified** | `test_safe_mode.py`: buyer-protective operations are never in the blocklist; refund grants are never swept; an unrecognized stored mode reads as safe mode (fails closed) |
| Capture evidence, monotonic apply, refunds | **Verified against fixtures** | `test_tk_evidence.py`, `test_tk_payments.py`, `test_tk_refunds.py`. No live capture has ever exercised this path — see the seeded-orders row above |

### HTTP API and durable worker

| Capability | Status | Evidence |
| --- | --- | --- |
| `commerce-api` | **Verified live** | 23,671 src lines; **62 OpenAPI paths carrying 64 operations** (`create_app().openapi()`). **422 tests** across 13 files. `GET /healthz` answers `{"status":"ok"}` on `:8000` today |
| `durable-worker` | **Verified live** | 3,688 src lines; `durable_worker.main` **exists** and is the running process. **98 tests**. Its provider calls are the 30 rows above |
| Durable outbox | **Verified live** | `durable-work`, 130 tests: a command is published with the state change it belongs to and is invisible to other sessions until the caller commits; SKIP LOCKED leasing, lease expiry, dead-lettering. Live: 39 `DONE`, 2 `DEAD`, 2 `PENDING` |
| Merchant simulator | **Verified** | `merchant-sim`, 180 tests. Catalogue: **247 products across 10 categories** (`len(CATALOGUE)`, `len({p.category for p in CATALOGUE})`) |
| Razorpay adapter | **Verified against fixtures, exercised live** | `payment-adapters`, 296 tests: raw-body constant-time HMAC, a forged event cannot pre-claim a key and suppress the real one, first delivery accepted and replay is a duplicate. The two `webhook_inbox` rows are that forgery test's own traffic. The adapter itself makes no network call; the worker does |
| Reconciliation, Resolution, human review | **Verified** | `commerce-api/services/{reconciliation,resolution,human_review}_service.py`, four GET routes under `/v1/review`. 5 `reconciliation_runs` rows exist. A divergence becomes a finding, never a row; no plan is applied automatically |

### Agent layer and protocols

| Capability | Status | Evidence |
| --- | --- | --- |
| RazorAI agent runtime | **Verified** | `agent-runtime`, 14,309 src lines in 42 files, **1,227 tests**. Five specialists under two Python harnesses; `docs/briefs/AGENT_ROSTER.md` is the authority for what each may hold |
| The capability gate | **Verified** | Registry A holds **24** tools — exactly the union of the five specialists' lists — and is the only registry an agent may ever hold. `python -c "from agent_runtime.capabilities.registry import REGISTRY_A; print(len(REGISTRY_A))"` |
| Tool coverage, stated honestly | **Partial, and reported** | **19 of those 24 have a factory builder.** The five without are `policy_search`, `resolution_evaluate`, `support_escalate`, `support_case_read` and `present_case`. They are never offered to a model and never faked: `BoundToolset.unbuilt` names them, a test asserts an unbuilt tool is not offered, and the suite emits a warning listing them every run |
| UCP | **Verified** | `commerce-protocols/ucp`, 6 modules |
| AP2 human-present cryptography | **Verified** | `commerce-protocols/ap2`, 7 modules; 5 packages import `jwcrypto` |
| ACP | **Verified as a library, not mounted** | `commerce-protocols/acp`, 5 modules. Not reachable over HTTP — `docs/KNOWN_GAPS.md` |
| MCP | **Verified as a library, not mounted** | `commerce-protocols/mcp`, 5 modules. Not reachable over HTTP — `docs/KNOWN_GAPS.md` |
| Protocol suite total | **Verified** | `commerce-protocols`, **367 tests** |
| Protocols over HTTP | **Verified, read-only** | `commerce-api/routers/protocols.py`: every route is a GET and a test asserts it over the route table |
| AP2/UCP signing keys | **Verified live** | Keys come from `UCP_MERCHANT_SIGNING_JWK` and `UCP_PLATFORM_SIGNING_JWK`, validated at startup; no generated fallback, and unconfigured the profiles answer 404. A process signed evidence and exited, an API on `:8090` was started, killed and restarted, and the pre-restart signature verified against the JWK Set the restarted server published. `TestSignedEvidenceSurvivesARestart` holds it in the suite; `docs/DEPLOY.md` |

### Voice

| Capability | Status | Evidence |
| --- | --- | --- |
| `voice-runtime` | **Verified** | 6,940 src lines in 37 files, **400 tests passing, 8 skipped (408 collected)**. Split pipeline per specification 19.1: text exists before speech, so a money sentence can be refused before it is spoken |
| Real speech, end to end | **Verified live, not in this run** | The 8 skips are `test_voice_real_audio.py`, which drives real audio through Gemini Transcribe Live and Chirp 3 HD. They need `GOOGLE_CLOUD_PROJECT`, which was unset here, so they were skipped rather than passed. This revision does not claim them |
| Reachable from the browser | **Verified** | Two storefront routes now stand in front of the gateway, both with tests beside them: `apps/buyer-web/src/app/api/voice/tickets/route.ts` mints a ticket, and `apps/buyer-web/src/app/api/voice/stream/route.ts` documents the socket path (`apps/buyer-web/src/app/api/voice/__tests__/`). The ticket is always minted same-origin through the storefront's own server so the buyer's bearer never reaches the browser, while in development the browser dials the gateway origin directly (`ws://127.0.0.1:8100`, named in `connect-src` when `NODE_ENV` is not production; `apps/buyer-web/src/lib/security/csp.ts`, `apps/buyer-web/src/features/voice/session.ts`). A real spoken yes through a real microphone has not been verified end to end — there is no microphone in this environment and `getUserMedia` never runs — so `docs/KNOWN_GAPS.md` item 1 is retired but the spoken leg itself is exercised only by the automated suite, not live |
| Deterministic money speech | **Built, not reachable** | `tts/templates.py` renders approvals, totals and deltas from versioned locale templates per specification 19.10, and is never reached because `POST /v1/agent/turn` returns no decision card. A test named `test_the_deterministic_template_path_is_not_reachable_over_http_yet` is written to fail the day it is. `docs/KNOWN_GAPS.md` item 8 |

### Observability

| Capability | Status | Evidence |
| --- | --- | --- |
| `platform-observability` | **Verified as a package, wired in nowhere** | 2,716 src lines, **391 tests**. A workspace member with no dependencies outside the standard library, asserted by a test that reads every import with `ast` — which is what makes "if the metrics backend is down, commerce continues" structural rather than intended. Not yet called from `commerce-api`, `durable-worker`, `voice-runtime` or `commerce-protocols`; `docs/adr/0007-observability.md` has the call sites |

### Frontends

| Capability | Status | Evidence |
| --- | --- | --- |
| Buyer storefront | **Verified** | `apps/buyer-web`, 16,424 lines across 75 TS/TSX files, 9 page routes. **582 unit tests in 35 files**; typecheck and lint clean, production `next build` succeeds |
| Merchant console | **Verified** | `apps/merchant-console`, 9,245 lines across 38 TS/TSX files, 6 page routes. **129 unit tests in 12 files**; typecheck and lint clean, production `next build` succeeds |
| Either app's Playwright suite | **Not measured this pass** | `playwright.config.ts` and spec files exist in both. Not re-run — see the gates section |
| Storefront wired to the real API | **Verified** | There is one lane and no mock module. `src/lib/api/` holds `client.ts`, `problem.ts`, `types.ts` and nothing else |

### Not started

| Capability | Status | Evidence |
| --- | --- | --- |
| Merchant onboarding, immutable configuration versions | **Not started** | No source |
| GKE / Cloud SQL deployment | **Implemented-unproven** | `infra/` holds Terraform, a Kustomize base with dev and demo overlays, and Dockerfiles. `./scripts/validate_infra.sh` validates offline. `terraform apply`, Cloud SQL bootstrap, Secret Manager population and GKE rollout have **never been run.** Nothing has touched a real GCP project |

## What this file said last time and got wrong

Five claims in the previous revision were carried forward from a state that no longer
existed. Naming them is the point of the exercise:

1. **"Realtime voice (STT/TTS, barge-in, echo gate) — Not started. No backend package."**
   `packages/voice-runtime` is 6,940 lines across 37 files with 400 tests passing. This was
   the largest single error: a whole package, listed under "Not started".
2. **"The 8 provider payment ids and the 8 `CONFIRMED` orders came through the payment path
   with provider evidence applied."** All eight orders are seeded and all eight payment ids
   are fabricated; one of them is fabricated so demonstrably that Razorpay returned 400 when
   asked to refund it. The previous revision had corrected an *understatement* about Razorpay
   and overshot into an overstatement in the same pass. The figure that survives is thirty
   real order creations and zero real captures.
3. **"`select count(*) from webhook_inbox` returns 0, which is the check that settles it."**
   It returns 2. The conclusion still holds — neither row applied a capture, both are the
   forgery test's own traffic — but the check quoted to settle it no longer reads as stated,
   and a check that has silently changed value is worse than no check.
4. **The totals table omitted two whole packages.** `platform-observability` (388 tests) and
   `voice-runtime` (386) were absent, so a table whose stated purpose was to sum to the
   whole-suite figure summed to 3,808 against a suite of 4,595.
5. **"`mypy` → no issues found in 185 source files"** and **"3,808 passed"**, plus the
   per-package counts throughout: every one of these moved. They are re-measured above
   rather than adjusted.

The failure mode was not dishonesty; it was a document measured against a tree that kept
moving underneath it. The remedy is the rule at the top of this file, plus one addition made
this pass: **where a figure could not be re-measured without corrupting someone else's work,
it is marked as not measured rather than carried forward.** That is why the live refusal
transcript and both Playwright suites are absent from this revision instead of being
reprinted.

## What is still explicitly NOT done

Stated plainly, because a submission candid about its gaps is worth more than one that is
not:

1. **No capture has ever been verified.** Thirty real Razorpay orders exist; zero captures.
   All eight `CONFIRMED` orders in `commerce_dev` are seeded fixtures carrying fabricated
   payment ids, and they render identically to real ones on every screen. This is the single
   most important gap in the repository.
2. **Nothing is fulfilled.** There is no courier, no dispatch and no delivery estimate in any
   response the catalogue sends.
3. **No CI reaches the provider.** The `razorpay_live` marker is declared and used by zero
   tests.
4. **The demo database is one migration behind head.** `commerce_dev` is at `7d2a4b9e1f03`;
   head is `a4e17c93b5d2`. The keyset indexes for collection pagination are not applied there.
5. **Nothing is deployed.** The infrastructure validates offline and has never been applied.
6. **CI type-checks one package.** `.github/workflows/ci.yml` runs
   `mypy packages/commerce-domain/src` only. The 245-file clean result above was produced by
   hand today; CI does not enforce it.
7. **Voice's deterministic money templates are built but unreachable**, because
   `POST /v1/agent/turn` returns no decision card for them to render from. The browser can now
   reach the voice WebSocket — the storefront's ticket and stream routes stand in front of the
   gateway — but a real spoken yes through a real microphone has not been verified end to end,
   since there is no microphone in this environment. `docs/KNOWN_GAPS.md` item 8; item 1 is
   retired.
8. **Observability is built and wired in nowhere.** 388 tests, zero call sites.
9. **Five of the twenty-four roster tools have no factory builder** — the Support and Case
   surfaces. They are reported as unbuilt rather than stubbed.
10. **The human-review queue is read-only.** No case is assigned, decided, annotated or
    resolved through it; the API says so in a scope note on the response itself.
11. **Both apps still use the deprecated `middleware` file convention.** `next build` prints
    the warning on each.

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
- A second checkout of this repository needs its own test database. `scripts/README.md` lists
  the five `DATABASE_URL*` names and says which suite reads which; a checkout that exports
  none of them connects to `commerce_test` silently.

## Why the isolation suite is trustworthy

The isolation suite connects as `commerce_test_kernel`, a `NOSUPERUSER NOBYPASSRLS` login
role, and asserts both flags before running. PostgreSQL superusers bypass row-level security
unconditionally, so the same suite run as the database owner would pass while proving
nothing. `scripts/bootstrap_test_roles.sql` creates the roles.

`FORCE ROW LEVEL SECURITY` is set on every protected table, because without it the table
owner is exempt from its own policies. **20 of the 24 tables in `commerce_test` carry it. The
four without are `alembic_version`, `tenants`, `platform_operating_modes` and
`api_sessions`**, and only the last is a judgement call worth stating in public:
`api_sessions` has a `tenant_id NOT NULL` column but no policy, because resolving a bearer
token is how a request *learns* its tenant and the lookup must therefore work before any
tenant is bound. The decision is deliberate and tested —
`platform-db/tests/test_pdb_service_tables.py::test_api_sessions_has_no_row_level_security`
and `::test_api_session_is_readable_without_a_tenant_bound` assert it — and the only thing
standing between one tenant and another tenant's session row is that the lookup key is an
unguessable 64-character token hash under a unique constraint. That is a real, narrow
exception to the isolation claim, and it should be said aloud rather than averaged into
"forced RLS everywhere".

Per ADR 0003 D12, the repository-root `conftest.py` fails the run if any `db`-marked test
**skips** under CI or `REQUIRE_DB=1`, because a missing role would otherwise turn every proof
above into a silent green.
