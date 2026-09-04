# Evidence-driven status

Per specification section 35, no component is described as working without evidence.
`Verified` means a test in this repository proves it and CI runs that test.

| Component | Status | Evidence |
| --- | --- | --- |
| Money, integer minor units | **Verified** | `packages/commerce-domain/tests/test_money.py`, incl. Hypothesis allocation-conservation property |
| RFC 8785 JCS canonicalization | **Verified (integer profile)** | `test_jcs.py`, incl. UTF-16 key ordering and float rejection |
| Canonical checkout hash | **Verified** | `test_hashing.py`, frozen regression vector |
| UUIDv7 identifiers | **Verified** | `test_ids.py` |
| Tenant isolation (RLS) | **Verified** | `platform-db/tests/test_tenant_isolation.py`, incl. alternating tenants on one pooled connection |
| Database role separation | **Verified** | App role denied writes to financial tables; audit append-only for every role |
| Transaction-core schema | **Verified** | 13 tables, Alembic migration, RLS forced on 11 tenant-owned tables |
| Kernel: state machines | **Verified** | `test_states.py`, exhaustive over all 256 (current, incoming) pairs |
| Kernel: Execution Grants | **Verified** | `test_grants.py`, consume-once under real contending sessions |
| Kernel: authority + revocation epoch | **Verified** | `test_authority.py`, revocation/admission race |
| Kernel: reservations | **Verified** | `test_reservations.py`, database-clock expiry |
| Kernel: Policy-at-Sale Receipt | **Verified** | `test_receipts.py`, immutability and binding detection |
| Kernel: idempotency | **Verified** | `test_idempotency.py`, same-key-different-payload rejection |
| Kernel: audit hash chain | **Verified** | `test_audit.py`, three tamper modes detected |
| Kernel: Safe Mode | **Verified** | `test_safe_mode.py`, refunds and support stay available |
| Durable outbox | **Verified** | `durable-work`, SKIP LOCKED leasing, expiry, dead-lettering |
| Merchant simulator | **Verified** | `merchant-sim`, fee engine and scenario controller |
| Razorpay test-mode adapter | **Verified** | `payment-adapters`, raw-body HMAC, replay dedupe, test-key guard |
| Schema/state agreement | **Verified** | `test_schema_state_agreement.py`, bidirectional drift guard |
| Admission transaction | Planned | Integrates the modules above; written next |
| Razorpay test-mode adapter | Planned | — |
| Reconciliation Service | Planned | — |
| Resolution Service | Planned | — |
| Human review queue | Planned | — |
| Commerce Assistant agents | Planned | — |
| Realtime STT/TTS | Planned | — |
| UCP 2026-08-25 | Planned | — |
| AP2 v0.2 human-present | Planned | Dependency resolution proven on Python 3.14 |
| ACP 2026-04-17 | Planned | — |
| MCP | Planned (P0 tail) | — |

## Deliberate profile restrictions

**JCS canonicalizes integers only.** RFC 8785's hardest requirement is ECMAScript number
serialization. This domain has no non-integer numbers: money is integer minor units,
quantities, versions and epoch timestamps are integers. A float reaching canonicalization
is a bug — most likely money that escaped the `Money` type — so it raises rather than
rounds. Enabling floats requires implementing ECMAScript number serialization and proving
it against the official RFC 8785 number vectors first.

## Verified environment facts

- Python **3.14.6**. The full dependency set resolves and imports: FastAPI, Pydantic,
  SQLAlchemy 2, psycopg 3, Alembic, httpx, `google-adk` 2.8.0, `google-genai` 2.22.0,
  `google-cloud-texttospeech` 2.37.0, `razorpay` 2.0.1.
- **AP2 pins `==` on three libraries** (`jwcrypto==1.5.6`, `cryptography==46.0.5`,
  `pydantic==2.12.5`). Resolution succeeds only when AP2's pins take precedence; the
  whole stack then coexists in one environment with all imports green. Do not raise those
  three libraries independently.
- AP2 publishes **no PyPI package**; it is pinned by git commit
  `b4587ac1d055888a73b4b21750973cffba961793`.

## Isolation testing note

The isolation suite connects as `commerce_test_kernel`, a `NOSUPERUSER NOBYPASSRLS` login
role, and asserts both flags before running. PostgreSQL superusers bypass row-level
security unconditionally, so the same suite run as the database owner would pass while
proving nothing. `scripts/bootstrap_test_roles.sql` creates the roles.

`FORCE ROW LEVEL SECURITY` is set on every protected table, because without it the table
owner is exempt from its own policies.
