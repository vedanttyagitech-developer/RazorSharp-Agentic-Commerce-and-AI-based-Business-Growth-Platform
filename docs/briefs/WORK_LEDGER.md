# Work ledger: what is done, what is left, and who does it

Measured against the twenty-one step implementation sequence in `PROJECT_SPECIFICATION.md`
section 32. Evidence is a real test count or a measured file size, never a claim.

**Reconciled against `docs/STATUS.md` on 2026-09-05 at 00:22 IST, branch `claude/backend`,
commit `a3426b0`.** Where this ledger and STATUS.md previously disagreed, the numbers
re-measured today win. Four figures in the earlier version of this file were wrong and are
corrected below with a note; do not restore them.

Note: the copy of the specification in `~/Downloads` is 120 lines behind and still describes
three capability registries and no section 2.5. **The copy in this repository is canonical.**

## Done

| # | Step | Evidence, re-measured today |
| --- | --- | --- |
| 1 | Repository, lockfiles, CI, status table, threat model | CI bootstraps DB roles and runs the isolation suites; root `conftest.py` fails a run where a `db` test skips (ADR D12); `docs/THREAT_MODEL.md`. **Caveat: CI's type-check step is `mypy packages/commerce-domain/src` only** |
| 2 | Domain types, integer money, JCS, golden vectors | `commerce-domain` 407 src lines / 6 files, **65 tests**; canonical hash frozen with a pinned regression vector |
| 3 | Schemas, tenant/RLS, AgentPrincipal, roles, migrations | `platform-db` 1,485 src lines / 7 files, **95 tests**, **4 migrations** (was recorded as 3), head `7d2a4b9e1f03` applied to `commerce_test` and `commerce_dev`, **24 tables, 20 with forced row-level security**. The four exceptions are `alembic_version`, `tenants`, `platform_operating_modes` and `api_sessions`; the last is a deliberate, tested exception (token resolution must precede tenant binding) and STATUS.md states it in public |
| 4 | Transaction kernel and everything in it | `transaction-kernel` **13,527 src lines / 18 files, 131 exports, 1,730 tests** |
| 5 | Concurrency and property tests | `test_admission.py` releases two real PostgreSQL sessions through a `threading.Barrier`; exactly one is allowed, the loser gets `DUPLICATE_OPERATION`. `test_states.py` is exhaustive at 1,150 cases |
| 6 | Merchant simulator and deterministic services | `merchant-sim` 3,121 src lines / 12 files, **155 tests** |

Verified totals: **2,473 backend tests green across the six stable packages**; **mypy strict
clean across 58 source files** (previously recorded as 63 — 58 is the measured count for
those six packages; the six-package `src` file count is 6+7+18+3+12+12 = 58).

Frontend: `apps/buyer-web`, 9,172 lines across 53 TS/TSX files, **25 vitest tests in 7 files**.

## In progress

| # | Step | State at 00:22 IST | Owner |
| --- | --- | --- | --- |
| 7 | Razorpay adapter, trusted surface, verification, webhook inbox, refunds, reconciliation | Adapter done (3,550 src lines, **298 tests**, fixtures only — **it has never called `api.razorpay.com`**). API now **6,174 src lines / 31 files, 13 router modules, 32 passing tests in one file**; worker **729 src lines / 5 files with zero tests and no `main` module**. Reconciliation not started | Five agents, in this worktree, now |
| 8 | Buyer storefront screens | Zepto-fidelity clone with the sixteen spec-8.2 UI states and an agent surface. **Runs only against `lib/api/mock.ts`; never connected to the live API** | Gemini, now |
| 17 | Protocol Inspector | `commerce-api/routers/inspector.py` exists and is in flight; no UI | Claude data side, Gemini view |
| 18 | GKE, Cloud SQL, Memorystore, ingress, secrets | Written and **validated offline**: `./scripts/validate_infra.sh` passes terraform fmt/validate, kubeconform strict (0 skipped schemas) and all three image builds. **Never applied to a real GCP project**, and `durable-worker:dev` fails its startup smoke check because `durable_worker.main` is missing | Claude |

## Not started

| # | Step | Correction to the earlier ledger | Owner and phase |
| --- | --- | --- | --- |
| 9 | RazorAI agents via ADK and Gemini 3.8 Flash | Earlier entry said "3,153 lines exist but do not import". **No such package is in this worktree** — `find . -type d -name "*agent*"` returns nothing. Treat as zero lines | Claude, phase 2 |
| 10 | Realtime speech in and out, STT and TTS, deterministic transactional speech | **Owner decision 2026-09-05: Claude builds this once and completely, immediately after the agents and harnesses, ahead of everything else in phase 2.** The frontend voice shell was removed on purpose so it is not built twice. Prior runtime work sits on `wt/voice` (4,148 lines, 91 of 97 tests) to salvage from | Claude, next after agents |
| 11 | Reconciliation, Resolution, human-review queue, Support Agent | — | Claude, phase 2 |
| 12 | Merchant onboarding and immutable configuration versions | — | Claude backend, Gemini console UI |
| 13 | Merchant Copilot, Operations, Growth Engine | — | Gemini UI over Claude's metrics endpoints |
| 14 | UCP business profile and lifecycle | Zero lines. The only `UCP` hits in `packages/*/src` are two docstring mentions in `commerce-domain` | Claude, phase 2 |
| 15 | Full AP2 human-present cryptography | Zero lines. No `jwcrypto` import exists anywhere in `packages/`. Only the dependency *resolution* is proven | Claude, phase 2 |
| 16 | ACP-compatible endpoints | Zero lines | Claude, phase 2 |
| 19 | Security, load, end-to-end and recovery tests | The `e2e` and `razorpay_live` markers are declared in `pyproject.toml` and **used by zero tests** | Claude backend, Gemini frontend and accessibility |
| 20 | Record the demonstration and freeze evidence | Blocked on step 7 finishing and on one real test-mode payment | Owner, with Gemini on presentation assets |
| 21 | MCP, last, only if every gate is green | Zero lines; `find . -name "*mcp*"` returns nothing | Claude, phase 2, optional |

## The critical path, stated plainly

The submission's whole claim rests on one thing that has not happened: **a single Razorpay
test-mode payment executed end to end through the kernel.** Everything else is either proven
(the kernel, 2,473 tests) or decorative until that happens. The shortest path to it:

1. `durable_worker.main` — the worker cannot start without it, so the outbox never drains.
2. The API's submit → grant → `CREATE_RAZORPAY_ORDER` outbox command path.
3. `durable_worker.transport` actually calling test mode, with the order id recorded.
4. `POST /v1/payments/verify` and the webhook receiver applying `PROVIDER_FETCH` /
   `WEBHOOK` evidence through `payments.apply_provider_evidence`.
5. One end-to-end test carrying a basket to `CAPTURED`, marked `e2e`.

Steps 9 (agents), 10 (voice) and 14–16 (protocols) are worth nothing to the demonstration if
step 9 of spec 2.5 — the payment — has not run.

## The split

**Claude, this stretch:** step 7. The HTTP API and the durable worker. No protocols, no
voice, no agent layer until a payment executes.

**Claude, phase 2, in this order:** the agent layer (9), then **voice (10) in full — STT,
TTS, the split pipeline and the gateway, built once and completely as the owner directed**,
then support services (11), UCP (14), AP2 (15), ACP (16), and MCP (21) only if everything
else is green.

**Gemini, now:** finish the storefront clone with local assets and build the AI agent surface
(8), then grow the catalogue and search (6).

**Gemini, next:** the merchant console (13), the onboarding UI (12), the Protocol Inspector
view (17), frontend end-to-end and accessibility (19), demo assets (20). The agent panel is
the differentiator: tool activity made visible, and the kernel's refusal rendered as the hero
moment.

**Never Gemini:** anything under `transaction-kernel`, `platform-db`, `payment-adapters`,
`durable-work`, `commerce-api`, `durable-worker`, `commerce-domain`, or the four
money-bearing modules of `merchant-sim`. The reason is not seniority. It is that a change to
how money is represented, hashed or transitioned is invisible in review and fatal in
production, and this project's entire claim is that those paths are proven.

## Integration

Claude merges. When Gemini reports, Claude reads `GEMINI_REPORT.md` and
`REQUESTS_TO_CLAUDE.md`, actions the requests that fall in Claude-owned files, merges the
branch into `main`, runs the full gate on the merged result, and reconciles the frontend's
provisional response schemas against the API's real responses. That reconciliation is the
most likely source of a broken demonstration and nobody else can do it.

## Rule for whoever edits this file next

Do not copy a number out of another document. Re-run the command. Four figures in the
previous version of this file (3 migrations, 63 mypy files, 3,153 agent lines, 4,148 voice
lines) were carried forward from a state that no longer existed, and two of them described
code that is not in this worktree at all.
