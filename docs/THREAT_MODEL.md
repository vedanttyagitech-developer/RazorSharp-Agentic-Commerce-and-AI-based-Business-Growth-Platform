# Threat model: governed agentic commerce platform (P0)

Date: 2026-09-04. Describes HEAD `0141229` ("WIP checkpoint: wave 1 of the service layer,
mid-build") plus the uncommitted changes present while this was written: modified
`c6ffa021cbb0_service_tables.py`, `razorpay/env.py`, both `scripts/bootstrap_*_roles.sql`,
`test_razorpay_orders.py`; untracked `test_rzp_env.py`, `test_rzp_payments.py`,
`test_pdb_service_tables.py`, `apps/buyer-web/src/lib/`. Sources opened: ADR 0003,
`docs/STATUS.md`, specification sections 3, 5.2-5.4, 10.3, 10.6, 11.2-11.5, 20, 21, 26, 30,
36, every module under `packages/*/src` cited below and its tests (870 test functions),
`.github/workflows/ci.yml`, `.gitignore`, `.env.example`.

Status vocabulary:

- **Implemented**: code in `packages/` plus a named test in this repository.
- **Library only**: the primitive exists and is tested; nothing wires it to HTTP.
  `packages/commerce-api/src/commerce_api/` and `packages/action-executor/src/action_executor/`
  contain only docstring `__init__.py` files.
- **Planned**: ADR decision or specification text only.

One caveat governs every database-backed "Implemented" claim. The `db` suites `pytest.skip`
when the `commerce_test_*` roles are unreachable (`platform-db/tests/conftest.py:42`,
`transaction-kernel/tests/conftest.py:55`), and `.github/workflows/ci.yml` neither applies
migrations nor runs `scripts/bootstrap_test_roles.sql`; it exports only `DATABASE_URL`, which
those conftests do not read. ADR D12 ("CI fails if any `db` test is skipped") is **planned**.
Until it lands, database evidence is proven on a developer machine. Section 7 reproduces it.

## 1. System and trust boundaries

```
 Browser (buyer UI,        LLM agents (Gemini via        Protocol callers
 Razorpay Checkout)        ADK; propose only)            UCP / AP2 / ACP / MCP later
   | bearer session          | Registry A tools only        | signed payloads
 ==|=========================|== B1: untrusted input =======|=====================
   v                         v                              v
 +-------------------------------------------------------------------------+
 | commerce-api (FastAPI, one process; ADR D14)                            |
 |  trusted buyer surface (Registry B) . protocol gateways . webhook inbox |
 |  route . scenario controller (X-Scenario-Key, absent in production)     |
 +------------------+-------------------------------+----------------------+
                    | commerce_kernel (mutations)    | commerce_app (reads,
 ===================|====== B2: database grant set ==|== non-financial writes)
                    v                                v
 +-------------------------------------------------------------------------+
 | PostgreSQL: 20 RLS-FORCEd tables (api_sessions deliberately excluded),  |
 | financial tables writable only by commerce_kernel, append-only          |
 | audit_events, outbox_events, webhook_inbox                              |
 +------------------+------------------------------------------------------+
                    | commerce_worker leases outbox; commerce_kernel consumes grant
 ===================|====== B3: single provider egress =====================
                    v
 +----------------------+  HTTPS, key_id + key_secret   +--------------------+
 | action-executor      | ----------------------------> | Razorpay test mode |
 | only Razorpay caller | <---------------------------- | orders/payments/   |
 | (ADR D3)             |  webhook, raw-body HMAC, to   | refunds            |
 +----------------------+  /webhooks/razorpay/{slug} B1 +--------------------+
```

| Secret or credential | Lives behind | Must never cross | Enforced by |
| --- | --- | --- | --- |
| `RAZORPAY_KEY_SECRET` | B3, worker process env | B1, B2, logs, model context | `RazorpayConfig._redact` / `repr=False` (`razorpay/config.py`); `env.load_config_from_env` names a variable, never its value; process split is ADR D3 (planned) |
| `RAZORPAY_KEY_ID` | worker; public id may reach the browser for Checkout | model context | `config.py` prefix guard; `RAZORPAY_PROFILE` defaults to `DEVELOPMENT` (`env.py`) |
| `RAZORPAY_WEBHOOK_SECRET` | api process, webhook route only | anything else; must differ from key secret | `RazorpayConfig._check_secrets` refuses equality |
| Database role credentials `DATABASE_URL_{APP,KERNEL,WORKER}` | one per process (`platform_db/engine.py:database_url`) | any other process | NOLOGIN group roles (`rls.create_roles_sql`), login members created per environment by `scripts/bootstrap_*_roles.sql` |
| AP2 merchant/platform signing keys | api gateway, `AP2_*_KEY_REF` | B1, model context (spec 21.6) | planned |
| Buyer/agent session bearer token | browser or agent runtime to api | tenant context, logs | `api_sessions.token_hash` stores SHA-256 only, `UNIQUE`, with `expires_at` and a `capabilities` JSONB (`schema_service.ApiSession`, migration `c6ffa021cbb0`); minting route planned |
| `X-Scenario-Key` | demo/dev api profile only | production profile | planned (ADR D11) |
| Execution Grant | B2 rows only; never returned to LLM or browser (spec 10.3.1) | B1 | `transaction_kernel/grants.py`; id is a UUIDv7, not a random nonce (section 6) |
| Gemini / Google Cloud credentials | agent runtime via Workload Identity (spec 21.8) | everything else | planned |

Developer disks: `razorpay_test_api_keys_*.csv` and `.env` are in the working tree, git-ignored
(`.gitignore` rules `*_api_keys*`, `.env`; `git ls-files` shows only `.env.example`). Test-mode
material, but `.env.example` asks for the CSV to be moved out of the repository; it has not been.

## 2. Assets

| Asset | Where it lives | Loss means | Primary control |
| --- | --- | --- | --- |
| Buyer money | Razorpay ledger; `payment_attempts`, `refunds` | duplicate charge, charge on a changed basket, missed refund | admission transaction, single-winner indexes, monotonic states |
| Merchant revenue | `payment_attempts.status = CAPTURED`; retained-revenue projection | fulfilment without capture, double refund | `may_fulfil`, refund ledger cap |
| Approval evidence | `approvals` (one `RECORDED` per version), `checkout_versions.content_hash`, receipts | approval replayed against different bytes | hash binding in `admit()`; consumption in admission is planned (D4a) |
| Audit chain integrity | `audit_events` (`prev_hash`, `self_hash`, gapless `seq`) | evidence edited or deleted unnoticed | append-only grants + `audit.verify_chain` |
| Tenant data isolation | every table in `schema.RLS_TABLES` | cross-tenant read or write | `FORCE ROW LEVEL SECURITY`, transaction-local `set_config` |
| Razorpay credentials | worker env | attacker creates orders/refunds directly | profile guard, redaction, process split (D3 planned) |
| Webhook secret | api env | attacker forges captures | raw-body HMAC before any write |
| Protocol signing keys | api env (planned) | forged mandates | planned gateway; kernel accepts only a typed `VerifiedAuthorityProof` |
| Session tokens | browser / agent runtime; hashed in `api_sessions` | acting as another buyer | planned ownership checks on every `session (owner)` route |

## 3. Attackers and capabilities

| Attacker | Assumed capabilities | Not assumed |
| --- | --- | --- |
| Malicious product content | Controls description, name, alt text; instruction-like text, hidden Unicode, markup | Cannot call tools; cannot alter typed price/stock fields |
| Compromised or hallucinating agent | Any tool arguments: wrong amount, stale version, other tenant, invented SKU; Registry B/C names | Holds no DB role; cannot sign as the trusted surface |
| Malicious buyer, two tabs | Same approval submitted twice concurrently, same or different idempotency keys | Cannot forge another buyer's session |
| Webhook forger / replayer | Arbitrary bodies and headers, including a real `x-razorpay-event-id`; replay of a genuine delivery | Does not hold the webhook secret |
| Network partition / lost provider response | Create-order, refund or fetch times out after the provider acted | Cannot alter Razorpay's records |
| Insider with `commerce_app` role | Any SELECT within a tenant; the app role's writes | No `commerce_kernel`, no superuser, no table ownership |
| Cross-tenant prober | Valid session in tenant A; guesses tenant B ids; `tenant_id` in bodies or host headers | Cannot set `app.tenant_id` |
| Denial-of-wallet | Unbounded model calls or voice sessions | Cannot bypass the kernel |
| Stolen session token | Full buyer capability until `expires_at` | No Registry B step-up beyond the session's own |

Out of model: a compromised `commerce_kernel` credential, database owner or superuser, and a
compromised worker holding provider credentials (spec 10.3.1 defers these to workload
isolation, egress policy and rotation).

## 4. STRIDE table by component

S = spoofing, T = tampering, R = repudiation, I = information disclosure, D = denial of service,
E = elevation. Test names are from `packages/*/tests/`.

| Component | Threat | Control | Status | Evidence | Residual risk |
| --- | --- | --- | --- | --- | --- |
| Trusted buyer surface | S: approve/reject/verify on a checkout the session does not own | `session (owner)` check on those routes (ADR endpoint catalogue) | Planned | none | No HTTP layer; `apps/buyer-web` is a create-next-app scaffold with an untracked API client |
| Trusted buyer surface | T: browser callback presented as capture | `may_fulfil` excludes `CaptureEvidence.BROWSER_CALLBACK` (`razorpay/fulfilment.py`); `verify_payment_signature` HMAC over `order_id\|payment_id` with `key_secret`, `compare_digest` (`razorpay/signatures.py`); server-side truth comes from `payments.fetch_payment` as `PROVIDER_FETCH` | Library only; route is ADR D8 | `test_a_browser_callback_alone_is_not_capture_evidence`, `test_a_signature_for_a_different_payment_is_refused`, `test_a_captured_fetch_is_sufficient_capture_evidence`, `test_an_authorized_fetch_is_not_capture` | `RECONCILE_PAYMENT` handler absent |
| Trusted buyer surface | R: buyer denies approving | audit row in the same transaction as the decision (`admission._deny`, `audit.append`); one `RECORDED` approval per version (`uq_approvals_one_recorded_per_version`) | Implemented | `test_writes_audit_in_the_same_transaction`, `test_second_recorded_approval_per_version_is_refused` | Approval consumption at admission (D4a) planned |
| Trusted buyer surface | E: CSRF, wildcard CORS, no CSP | spec 21.5 | Planned | none | `apps/buyer-web/next.config.ts` is empty; no middleware |
| LLM agents | E: agent invokes Registry B/C | registries are separate lists (spec 5.3); `admit()` step 4 denies an `ActorType.AGENT` lacking `checkout.submit_approved` (`AgentPrincipal.can`) | Implemented at kernel; broker and ADK runtime planned | `test_agent_without_submit_capability_is_denied` | No agent runtime exists |
| LLM agents | E: sub-agent gains capability | `AgentPrincipal.subset_for` raises on any excess (`contracts.py:65`) | Implemented, **untested**, no caller in `src/` | none | Add a test before relying on it |
| LLM agents | S: tenant id supplied in tool arguments | `AdmissionRequest.__post_init__` refuses principal/request mismatch; RLS bounds every row | Implemented | `test_principal_from_another_tenant_is_refused`, `test_with_check_blocks_writing_into_another_tenant` | Session-to-principal construction planned |
| LLM agents | T: prompt injection via catalogue text | `textfold.normalize` strips format and control characters; search returns only catalogue SKUs; the kernel compares numbers and hashes, never prose | Partial | `test_strips_zero_width_and_soft_hyphen`, `test_strips_control_characters`, `test_search_never_reveals_a_sku_outside_the_catalogue` | Instruction scanning, quarantine, adversarial corpus (spec 20.3) planned |
| LLM agents | D: denial-of-wallet | spec 21.7 budgets and caps | Planned | none | No model call exists in the repository |
| LLM agents | I: secrets or PII in model context | spec 21.6 | Planned | none | |
| commerce-api | S: host-header tenant spoof | spec 21.2 | Planned | none | |
| commerce-api | T: same `Idempotency-Key`, different payload | `idempotency.execute_once` raises `IdempotencyKeyReuseError`; keys tenant-scoped | Library only; HTTP replay header is ADR D9 | `test_a_changed_amount_under_the_same_key_is_refused`, `test_the_same_key_string_in_two_tenants_is_two_records` | |
| commerce-api | D: oversized webhook or JSON body | 256 KiB cap (ADR D7/D13) | Planned | none | |
| commerce-api | E: scenario routes reachable in production | ADR D11 | Planned | none | `scenario_faults`/`scenario_runs` tables exist with app-arms/worker-consumes grants (`test_app_arms_faults_and_worker_consumes_them`) |
| commerce-api | S: UCP/ACP/AP2 replay or forged mandate | spec 16.3, 21.4; kernel refuses a request carrying both approval and proof | Gateways planned; kernel guard implemented | `test_both_approval_and_proof_is_refused` | No verifier; `proof` path has no integration test |
| Kernel | T: approval for a changed price or stock | `admit` steps 8-11: revalidate, `_compute_deltas`, invalidate N, insert N+1, no provider order | Implemented | `test_changed_total_denies_and_supersedes_the_version`, `test_denial_creates_no_payment_attempt`, `test_unavailable_item_denies` | N+1 gets no receipt or reservation yet (D4c planned) |
| Kernel | T: stale approval replayed on an invalidated version | `invalidated_at`, stored-hash and newer-version checks before any write | Implemented | `test_invalidated_version_cannot_be_admitted_afterwards`, `test_wrong_content_hash_is_refused` | |
| Kernel | T: two admissions for one checkout | `checkout_versions FOR UPDATE`, reservation consume, partial unique index on non-terminal `payment_attempts` | Implemented | `test_two_concurrent_admissions_produce_exactly_one_attempt` (two real threads; loser denied with a structured code) | Index-collision path is `session.rollback(); raise` in `admission.py`, not the D4b SAVEPOINT denial |
| Kernel | R/T: approval reused or expired | `approvals.consume_recorded` after the version lock; 600 s TTL (ADR D4a, D13) | **Planned** | none | `approval_id` is checked for presence only; reuse is blocked indirectly by reservation consumption and hash mismatch |
| Kernel | E: revoked authority still spends | `authority.admit_debit` under `FOR UPDATE` with `expected_epoch` equality | Implemented | `test_racing_revocation_and_admission_never_both_succeed`, `test_two_concurrent_debits_cannot_both_take_the_last_capacity` | |
| Kernel | E: agent enters or leaves Safe Mode | `safe_mode._validate_actor` refuses `ActorType.AGENT`; tenant-bound transaction cannot flip global | Implemented | `test_an_agent_cannot_enter_or_leave_safe_mode`, `test_a_tenant_bound_transaction_cannot_flip_the_platform_switch` | Operator authentication (Registry D) outside P0 |
| Kernel | T: Safe Mode activation racing an in-flight admission | `LOCK_ORDER` names `platform_operating_modes` first, but `safe_mode.is_permitted` reads without `FOR UPDATE`/`FOR SHARE` | **Gap** | none | A delegated grant can commit after activation's sweep; mitigation is a worker-side `is_permitted` before `consume_grant` (planned) |
| Kernel | T: grant re-pointed after admission | `grants.consume_grant` compares every bound field; mismatch refuses and leaves the grant `ISSUED` | Implemented | `test_any_altered_field_refuses_and_leaves_the_grant_unspent`, `test_reports_every_altered_field_not_only_the_first` | |
| Kernel | T: grant consumed twice | `SELECT ... FOR UPDATE`, guarded `ISSUED -> CONSUMED`; index `uq_execution_grants_one_active_per_attempt` | Implemented | `test_two_concurrent_consumers_exactly_one_wins`, `test_a_consumed_attempt_never_gets_a_second_grant` | |
| Kernel | T: reservation expiry judged by a skewed pod clock | `reservations.check_validity` compares against `now()` in SQL; the module imports no clock | Implemented | `test_expired_on_the_database_clock_is_refused_even_where_a_slow_pod_would_admit`, `test_source_never_consults_an_application_clock` | |
| Kernel | T: Policy-at-Sale Receipt swapped or edited | `receipts.verify_binding` recomputes the hash and checks the version binding | Implemented | `test_repointing_the_checkout_at_another_receipt_is_detected`, `test_editing_a_term_and_its_hash_together_is_still_detected` | |
| PostgreSQL | I: cross-tenant read on a pooled connection | `rls.rls_statements`: ENABLE + FORCE on all 20 `RLS_TABLES`, predicate `NULLIF(current_setting('app.tenant_id', true), '')::uuid`; `tenancy.set_tenant` uses `set_config(..., true)` only | Implemented | `test_alternating_tenants_on_one_pooled_connection`, `test_unset_tenant_returns_no_rows_fails_closed`, `test_tenant_owned_table_is_enabled_and_forced`; conftests assert `rolsuper = false`, `rolbypassrls = false` | `api_sessions` has no RLS by design (token lookup precedes tenant knowledge; `test_api_sessions_has_no_row_level_security`); superuser/owner bypass is out of model |
| PostgreSQL | T: app or worker role writes financial state | `rls.grant_statements`: `FINANCIAL_TABLES` get INSERT/UPDATE for kernel only, explicit REVOKE from app and worker; `orders`, `provider_requests`, `reconciliation_runs` are in `FINANCIAL_TABLES` | Implemented | `test_app_role_cannot_write_financial_tables`, `test_app_role_cannot_insert_financial_evidence`, `test_worker_role_cannot_insert_financial_evidence`, `test_financial_service_tables_are_kernel_only_in_generated_sql` | Grants run inside migrations; `bootstrap_*_roles.sql` re-asserts them and must run after `alembic upgrade head` |
| PostgreSQL | T: audit row edited, deleted or reordered | UPDATE revoked on `APPEND_ONLY_TABLES`, DELETE revoked everywhere; chain with gapless `seq` under `pg_advisory_xact_lock` | Implemented | `test_audit_events_cannot_be_updated_by_any_application_role`, `test_no_application_role_may_delete`, `test_an_edited_payload_is_detected`, `test_a_deleted_middle_row_is_detected`, `test_a_reordered_pair_is_detected` | Tail truncation undetectable from the stream alone (`test_a_truncated_tail_is_not_detectable_from_the_stream_alone`); publishing `audit.head` planned |
| PostgreSQL | T: one Razorpay order mapped to two attempts | partial unique `(tenant_id, provider_order_id)` on `payment_attempts` (migration `c6ffa021cbb0`) | Implemented | `test_one_razorpay_order_per_attempt_within_a_tenant`, `test_uniqueness_is_per_tenant` | Column is written by nothing yet |
| Outbox / worker | T: two workers take one command | `FOR UPDATE SKIP LOCKED` in `outbox.lease` | Implemented | `test_concurrent_workers_partition_the_queue_without_overlap` | |
| Outbox / worker | T: crashed worker completes a reassigned command | `leased_until` fencing token in `complete`/`fail`/`extend_lease` | Implemented | `test_the_superseded_worker_cannot_complete_the_reassigned_command` | Handlers, transport, reconciliation loop planned |
| Outbox / worker | T: command payload substituted between enqueue and execution | `commands.CreateOrderCommand.grant_binding` builds the exact `GrantBinding` that `consume_grant` compares; a leased row naming another tenant is refused | Library only | `test_a_substituted_payload_does_not_bind`, `test_binding_covers_every_field_consume_grant_compares`, `test_leased_row_whose_payload_names_another_tenant_is_refused` | |
| Outbox / worker | D: unknown provider outcome retried blindly | `fail` buries `PAYMENT_UNKNOWN` immediately; `RETRYABLE` excludes it (`recovery.py`) | Implemented | `test_an_unknown_payment_outcome_is_buried_immediately_not_retried` | |
| Razorpay adapter | T: lost create-order response leads to a second order | timeout maps to `PAYMENT_UNKNOWN`; `find_order_by_receipt` before any re-create; echo of amount/currency/receipt verified | Library only | `test_a_timeout_is_unknown_and_never_failed`, `test_a_failed_lookup_is_never_evidence_of_absence`, `test_duplicate_receipts_are_escalated_never_guessed` | |
| Razorpay adapter | T: reconciliation trusts a foreign or ambiguous payment | `payments.fetch_payment`/`fetch_order_payments` refuse an entity that does not echo the attempt; two settled payments escalate; unrecognised status escalates | Library only | `test_a_payment_that_does_not_echo_the_attempt_is_refused_not_classified`, `test_two_settled_payments_on_one_order_are_escalated_not_chosen`, `test_a_timeout_is_unknown_and_must_reconcile_again` | |
| Razorpay adapter | T: double refund | `REFUND_UNKNOWN` is reconcile-only; `refund_idempotency_key(payment, amount, sequence)`; `CaptureLedger` caps the total | Library only | `test_an_unknown_refund_is_never_replanned`, `test_repeated_partial_refunds_can_never_exceed_the_capture`, `test_a_timeout_is_refund_unknown_and_never_refund_failed` | |
| Razorpay adapter | E: live key in dev/demo | `RazorpayConfig._check_key_id`: `rzp_live_` refused outside `PRODUCTION`; `PRODUCTION` needs `production_approval_ref`; `env.load_config_from_env` defaults the profile to `DEVELOPMENT` | Implemented | `test_development_profile_rejects_a_live_key`, `test_replace_cannot_smuggle_a_live_key_past_the_guard`, `test_a_live_key_in_a_test_only_profile_refuses_to_start`, `test_the_profile_defaults_to_development` | |
| Razorpay adapter | I: secrets in logs or error messages | `_redact` length-only, `repr=False`; env loader names the variable only | Implemented | `test_repr_never_discloses_secret_material`, `test_a_guard_failure_never_renders_secret_material` | Structured logging layer planned |
| Webhook receiver | S: forged webhook | HMAC-SHA256 over raw bytes with the webhook secret, `compare_digest`, verified before parse or write | Implemented | `test_an_unverified_event_is_refused_and_stores_nothing`, `test_the_api_secret_does_not_verify_a_webhook`, `test_webhook_verification_goes_through_compare_digest` | |
| Webhook receiver | D: forged pre-claim suppresses the genuine event | verify precedes `InboxStore.claim` | Implemented | `test_a_forged_event_cannot_pre_claim_a_key_and_suppress_the_real_one` | |
| Webhook receiver | T: replay or out-of-order delivery | namespaced dedupe tiers `evt:`/`jcs:`/`raw:`; `states.monotonic_apply`; `webhook_inbox` table with `UNIQUE (tenant_id, dedup_key)`, kernel inserts, worker updates, app nothing | Implemented (in-memory); table exists | `test_a_replayed_event_changes_state_at_most_once`, `test_captured_never_regresses_to_authorized`, `test_the_tier_namespaces_cannot_collide`, `test_kernel_receives_webhooks_and_worker_applies_them` | The only `InboxStore` is `InMemoryInboxStore`; no SQL-backed `claim`, no route, no tenant-slug resolution, no worker-side tenant check (D7) |
| Webhook receiver | T: `refund.processed` guessed as full | `UnmappableEventError` unless the payment entity or caller states full/partial | Implemented | `test_refund_processed_is_refused_without_the_full_or_partial_fact` | |
| Merchant simulator | T: demo injection indistinguishable from organic change | `ScenarioInjection` is the only mutator and carries `SCENARIO_INJECTION` structurally | Implemented | `test_the_label_cannot_be_anything_else`, `test_the_store_exposes_no_other_mutator` | HTTP scenario routes planned |
| Build / supply chain | T: action or dependency tampering | Actions pinned by SHA in `ci.yml`; `uv.lock` | Implemented | `ci.yml` | `pip-audit --strict \|\| true` is advisory; mypy runs only on `commerce-domain`; no import-boundary test exists despite ADR D1; `db` suites skip in CI |

Planned-only controls, in one list: approval consumption and expiry (D4a); SAVEPOINT denial on
the attempt index (D4b); reservation release plus receipt for N+1 (D4c); every HTTP route,
session mint, ownership and idempotency-replay behaviour; SQL-backed webhook inbox claim,
tenant-slug routing, body cap and worker-side tenant check (D7); worker transport, handlers and
reconciliation; protocol gateways and MCP auth; prompt-injection scanning and quarantine; rate
limits and budgets; CSRF/CORS/CSP; log redaction layer; proof-chain verifier API; CI role
bootstrap and skip-fails (D12); import-boundary test; `WEB_CONCURRENCY` guard (D14).

## 5. Twelve abuse cases

Each lists the attack, the control, and what a reviewer runs, after section 7 step 0.
`uv run pytest` and `.venv/bin/python -m pytest` are interchangeable below.

**5.1 Agent submits approval for a changed price.** Attack: price rises after approval; the agent
resubmits the old approval and hash. Control: `admit()` re-reads merchant state after locking
`checkout_versions`, computes deltas, denies `REAPPROVAL_REQUIRED`, stamps `invalidated_at` on N,
inserts N+1, creates no provider order. Run:
`uv run pytest packages/transaction-kernel/tests/test_admission.py -q -k "changed_total or denial_creates_no or invalidated_version"`.

**5.2 Webhook delivered twice, out of order.** Attack: `payment.authorized` arrives after
`payment.captured`, then `payment.captured` again. Control: `dedup_key_for` claims once;
`monotonic_apply` keeps `CAPTURED`; `webhook_inbox` carries the unique key. Run:
`uv run pytest packages/payment-adapters/tests/test_razorpay_webhooks.py -q -k "replay or regress or in_order"` and
`uv run pytest packages/transaction-kernel/tests/test_states.py -q -k MonotonicApply`.

**5.3 Browser callback forged.** Attack: client posts a fabricated `razorpay_signature`, or a
genuine one for a different payment id. Control: `verify_payment_signature` with `key_secret`
and `compare_digest`; a valid callback is still only `BROWSER_CALLBACK`, which `may_fulfil`
rejects; capture truth comes from `fetch_payment` (`PROVIDER_FETCH`). Run:
`uv run pytest packages/payment-adapters/tests/test_razorpay_signatures.py packages/payment-adapters/tests/test_razorpay_fulfilment.py -q` and
`uv run pytest packages/payment-adapters/tests/test_rzp_payments.py -q -k "does_not_echo or foreign_payment or timeout or escalated"`.
Gap: `POST /v1/payments/verify` (ADR D8) does not exist.

**5.4 Grant reused after worker crash.** Attack: a worker consumes a grant and dies before the
provider call; a replacement re-leases the command. Control: `consume_grant` is `FOR UPDATE`
and `ISSUED -> CONSUMED` once; a consumed attempt never gets a second grant for the same
operation; the lease fence refuses the dead worker's late completion; the binding is rebuilt
from the stored payload and must match. Run:
`uv run pytest packages/transaction-kernel/tests/test_grants.py -q -k "second_consumption or concurrent_consumers or never_gets_a_second"`,
`uv run pytest packages/durable-work/tests/test_outbox.py -q -k "superseded_worker or lapsed_lease"` and
`uv run pytest packages/durable-work/tests/test_dw_commands.py -q -k "substituted_payload or another_tenant"`.

**5.5 Refund issued twice after timeout.** Attack: refund call times out; caller retries.
Control: timeout is `REFUND_UNKNOWN`; `plan_refund` refuses to plan from it;
`must_reconcile_before_retry` is true; the idempotency key derives from
`(payment_id, amount, sequence)`. Run:
`uv run pytest packages/payment-adapters/tests/test_razorpay_refunds.py -q -k "unknown or never_replanned or exceed_the_capture"`.

**5.6 Sub-agent gains capability.** Attack: a coordinator derives a child principal holding
`refund.confirm`. Control: `AgentPrincipal.subset_for` raises `ValueError` on any excess. Run:
`.venv/bin/python -c "from commerce_domain.contracts import *; import uuid; p=AgentPrincipal('p',uuid.uuid4(),ActorType.AGENT,capabilities=frozenset({'catalog.search'})); p.subset_for('child',frozenset({'refund.confirm'}))"`
(expect `ValueError: sub-agent would gain capabilities its parent lacks`). Gap: no test, no caller.

**5.7 Tenant id supplied in body.** Attack: body names tenant B while the session is tenant A.
Control: `AdmissionRequest.__post_init__` raises on principal/request mismatch; RLS `WITH CHECK`
rejects rows for another tenant; unset context matches nothing. Run:
`uv run pytest packages/transaction-kernel/tests/test_admission.py -q -k another_tenant` and
`uv run pytest packages/platform-db/tests/test_tenant_isolation.py -q`.

**5.8 Live key in dev config.** Attack: `rzp_live_` pasted into `.env`. Control:
`RazorpayConfig` refuses to construct in `DEVELOPMENT`/`DEMO`; `PRODUCTION` additionally needs
`production_approval_ref`; the env loader defaults to `DEVELOPMENT`. Run:
`uv run pytest packages/payment-adapters/tests/test_razorpay_config.py packages/payment-adapters/tests/test_rzp_env.py -q -k "live_key or approval or defaults"`.

**5.9 Audit row edited.** Attack: an application role updates `payload`, deletes a middle row,
or rewrites `self_hash` to match. Control: UPDATE/DELETE revoked; `verify_chain` recomputes
every hash and checks `prev_hash` and gapless `seq`. Run:
`uv run pytest packages/transaction-kernel/tests/test_audit.py -q -k "VerifyChain or SequenceIntegrity"` and
`uv run pytest packages/platform-db/tests/test_tenant_isolation.py -q -k audit_events`.

**5.10 Voice transcript treated as approval.** Attack: a spoken or transcribed "yes" is
forwarded as consent. Control: `AdmissionRequest` cannot be constructed without exactly one of
`approval_id` (Registry B row) or `proof` (verified mandate); no text field can stand in. Run:
`uv run pytest packages/transaction-kernel/tests/test_admission.py -q -k "neither_approval or both_approval"`.
Gap: no voice code exists; spec 19.11 is design only.

**5.11 Safe Mode bypass by agent.** Attack: agent calls `leave_safe_mode`, a tenant-scoped
transaction flips the global switch, or a delegated debit is submitted during Safe Mode.
Control: `_validate_actor` refuses `AGENT`; `_bind_scope_for_write` refuses cross-scope;
`admit()` step 5 denies `SAFE_MODE_ACTIVE` before locking anything. Run:
`uv run pytest packages/transaction-kernel/tests/test_safe_mode.py -q -k "agent_cannot or cannot_flip or blocks_machine or keeps_buyer"`.
Residual: the TOCTOU row in section 4.

**5.12 Reservation expiry during payment.** Attack: the hold lapses while Razorpay Checkout is
open, or the provider outcome is unknown. Control: validity is judged by `now()` in SQL at
admission; an `UNKNOWN` outcome keeps the hold (`HOLDING_CAUSES`); `PAYMENT_UNKNOWN` cannot be
invalidated outright. Run:
`uv run pytest packages/transaction-kernel/tests/test_reservations.py -q -k "database_clock or unknown_outcome"` and
`uv run pytest packages/transaction-kernel/tests/test_states.py -q -k unknown_payment`.

## 6. Out of scope for P0 and accepted risks

| Item | Consequence | Why accepted |
| --- | --- | --- |
| One Razorpay test account per process (ADR D7) | no per-tenant provider credentials; a webhook-secret leak affects every tenant in the process | demo scale; tenant resolved from the route slug and re-checked by the worker (planned) |
| Single API replica, `WEB_CONCURRENCY = 1` (ADR D14) | simulator state is process-local; no HA | documented demo restriction; the guard is planned |
| No human reviewer in P0 (spec 5.3 Registry D, 36) | `HUMAN_REVIEW_REQUIRED` cases queue and stop | queue and evidence only, so the operator path is designed rather than improvised |
| Demo bearer sessions instead of Identity Platform | no OIDC/PKCE, MFA or step-up locally | `POST /v1/demo/sessions` exists only in dev/demo profile (planned); tokens stored hashed |
| `api_sessions` outside RLS | a leaked table exposes hashes, expiry and capability lists, not tokens | token lookup must precede tenant binding; SHA-256 only |
| Superuser and table owner bypass RLS | DBA can read or write across tenants | PostgreSQL semantics; NOLOGIN group roles and IAM on GKE |
| Grant id is UUIDv7, not a random nonce (spec 10.3.1) | id is time-ordered and guessable | never crosses B1; useless without the kernel role plus a matching binding |
| Audit tail truncation invisible without a published head | deleting the newest rows in a stream verifies clean | `audit.head` exists; periodic publication planned |
| Safe Mode read not row-locked at admission | one delegated grant may commit after activation | worker re-check before consume planned; no delegated path is live in P0 |
| `pip-audit` advisory; mypy limited to `commerce-domain` | vulnerable dependency or type error can merge | to be tightened with the service layer |
| CI does not bootstrap DB roles | `db` suites skip silently in CI | ADR D12 planned; section 7 reproduces locally |
| Test-key CSV and `.env` on the developer disk | local secret exposure | git-ignored; test mode only; `.env.example` asks for relocation |
| Uncommitted work in progress (listed in the header) | assessed only where cited above | tree was mid-build when written |

## 7. Verification checklist

```bash
# 0. Environment: PostgreSQL 16, migrations, then the non-superuser test roles (order matters)
uv sync                                     # uv is at ~/.local/bin/uv if not on PATH
createdb commerce_test
(cd packages/platform-db && DATABASE_URL=postgresql+psycopg://$USER@localhost:5432/commerce_test uv run alembic upgrade head)
psql commerce_test -f scripts/bootstrap_test_roles.sql
psql -U commerce_test_kernel commerce_test -c "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"   # both f

# 1. Full suite; -rs lists skips. Any skipped db test means the claim above it is unproven.
uv run pytest packages/ -q -rs

# 2. Money invariants
uv run pytest packages/transaction-kernel/tests/test_admission.py -q -k SingleWinner
uv run pytest packages/transaction-kernel/tests/test_grants.py -q -k "TestConsume or NoReplacement"
uv run pytest packages/transaction-kernel/tests/test_authority.py -q -k TestConcurrency
uv run pytest packages/transaction-kernel/tests/test_idempotency.py -q -k "KeyReuse or TestConcurrency"

# 3. Isolation and privilege
uv run pytest packages/platform-db/tests/test_tenant_isolation.py -q
uv run pytest packages/platform-db/tests/test_pdb_service_tables.py -q -k "TestRowLevelSecurity or TestGrants"
grep -n "FORCE ROW LEVEL SECURITY\|NOBYPASSRLS\|REVOKE DELETE" packages/platform-db/src/platform_db/rls.py

# 4. Provider boundary
uv run pytest packages/payment-adapters/tests -q
grep -n "compare_digest" packages/payment-adapters/src/payment_adapters/razorpay/signatures.py

# 5. Evidence integrity
uv run pytest packages/transaction-kernel/tests/test_audit.py -q -k VerifyChain

# 6. Confirm the planned-only items are really absent
grep -n "consume_recorded\|SAVEPOINT\|begin_nested" packages/transaction-kernel/src/transaction_kernel/admission.py   # nothing: D4a/D4b
grep -n "FOR UPDATE\|FOR SHARE" packages/transaction-kernel/src/transaction_kernel/safe_mode.py                        # nothing: TOCTOU row
grep -rn "def claim(" packages/*/src                              # only InMemoryInboxStore in razorpay/webhooks.py
grep -rln "subset_for" packages/*/tests                           # nothing: untested
ls packages/commerce-api/src/commerce_api/routers/ packages/action-executor/src/action_executor/handlers/   # only __init__.py

# 7. Secrets hygiene and CI
git ls-files | grep -Ei '\.env$|\.csv$'                           # expect nothing
git check-ignore -v razorpay_test_api_keys_*.csv .env
grep -n "|| true\|bootstrap_test_roles\|alembic" .github/workflows/ci.yml   # advisory audit; no role bootstrap, no migration
```
