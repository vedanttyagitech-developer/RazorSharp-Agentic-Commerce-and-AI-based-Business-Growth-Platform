# Brief for Claude — the API, the worker, and integration

Read this at the start of any session that continues the backend. It is the scope, the
boundary and the order of work.

---

## 1. Two assistants, two worktrees

Claude and Gemini are building this repository in parallel, each in its own git worktree,
because sharing one working tree caused them to overwrite each other.

```
/Users/vedanttyagi/Desktop/acr-worktrees/claude-backend     branch claude/backend    db commerce_test
/Users/vedanttyagi/Desktop/acr-worktrees/gemini-catalogue   branch gemini/catalogue  db commerce_test_gem
/Users/vedanttyagi/Desktop/Agentic Commerce for Razorpay    branch main              integration only
```

Claude owns everything that touches money. Gemini owns catalogue data, product search and
buyer-facing UI, and is told explicitly never to edit the kernel, the database layer, the
adapters, the API, the worker, or the API contract in the frontend client. Gemini's scope
is `docs/briefs/GEMINI_BRIEF.md`; requests it cannot action itself arrive in
`docs/briefs/REQUESTS_TO_CLAUDE.md`.

**Claude is the only side that merges.** Gemini never rebases or merges.

## 2. The state that matters

The kernel is finished and proven: 13,527 lines, single-winner admission demonstrated
with real contending PostgreSQL sessions, 2,473 tests green, mypy strict clean.

The gap is stark and it is the whole job:

| Package | Lines | State |
| --- | --- | --- |
| `packages/commerce-api` | 6 | docstrings only. **Does not exist.** |
| `packages/durable-worker` | 5 | docstrings only. **Does not exist.** |

Nothing connects the kernel to the storefront. No real Razorpay call has ever been made
from this project. Every payment path is proven against fakes. That single fact is what
separates a very well-tested library from a demonstration a judge can click through.

## 3. Scope for this stretch

Build `commerce-api` and `durable-worker`, and nothing else. Not protocols (UCP, AP2,
ACP), not voice, not the agent layer, not MCP. Those are slides; this is the demo.

The target is the eleven steps of specification 2.5 running end to end against Razorpay
test mode, driven over HTTP.

### The contract is already written

`docs/adr/0003-service-layer.md` holds the endpoint catalogue and decisions D1 to D15.
Build to it. The parts that most often get built wrong:

- **D5, lock order.** `platform_operating_modes → checkout_versions → reservations →
  delegated_authorities → payment_attempts → execution_grants → refunds`. Locate an
  attempt with a plain `SELECT`, then lock `checkout_versions FOR UPDATE`, then
  `payment_attempts FOR UPDATE`. Cancel and evidence application must take the same
  order or they deadlock.
- **D7, the webhook.** Read raw bytes, cap at 256 KiB, verify the HMAC in constant time,
  and only then parse JSON. Resolve the tenant from the route, never from the body.
  Claim an inbox row, dedupe on `x-razorpay-event-id`, enqueue, return 200 fast.
- **D8, browser callbacks.** Verifying the client signature records evidence and enqueues
  a reconciliation. It never sets a payment to captured. Capture comes only from a webhook
  or a provider fetch, through `payments.apply_provider_evidence`, which uses
  `monotonic_apply` so `CAPTURED` never regresses.
- **D9, idempotency.** Same key replays the stored response with `Idempotent-Replayed`.
  Same key with a different payload is a 422. A concurrent second submit with a different
  key returns 200 carrying `DUPLICATE_OPERATION` and the winner's attempt.
- **D15, errors.** RFC 9457 problem details. A kernel denial is a 200 with the structured
  decision, never a 4xx, because a denial is the system working correctly.

### The worker is the only thing that calls Razorpay

It consumes its grant in a committed transaction **before** the network call, so a
redelivery after a crash finds the grant consumed and reconciles instead of charging
twice. Handlers: create order, apply webhook event, reconcile payment, execute refund,
reconcile refund, plus housekeeping on a timer.

### Everything the API and worker need already exists

The kernel surface is wired and reachable from the package root (131 exports). Use it:
`create_checkout`, `require_approval`, `record_approval`, `consume_recorded`, `admit`,
`record_create_order_result`, `record_browser_callback`, `apply_provider_evidence`,
`begin_reconciling`, `escalate`, `admit_refund`, `record_refund_result`,
`build_checkout_content`, `ProviderEvidence`, `link_command`. Command shapes for the
outbox are in `durable_work.commands`. The Razorpay request builders, signature
verification and webhook parsing are in `payment_adapters`. The merchant state source the
kernel calls is `merchant_sim.kernel_adapter.SimMerchantStateSource`.

Do not rewrite any of it. If something is missing, add it in the kernel with tests.

## 4. Order of work

1. Settings, dependencies, session and tenant binding, RFC 9457 errors, idempotency
   wrapper. Every mutation runs one transaction as the kernel role with the tenant GUC set
   as its first statement; reads run as the app role.
2. Discovery, basket, checkout creation, approval, submit. This gets steps 1 to 8 of the
   demonstration working, including the stale-approval denial with deltas and version N+1.
3. The worker: create-order handler and the outbox loop. Step 9.
4. Payment verification, the webhook receiver, orders. Step 10.
5. Evidence: the proof chain, the timeline with server-sent events, the inspector, and the
   retained-revenue view. Steps 10 and 11.
6. The scenario controller, so the price change in step 5 can be injected live.
7. A single end-to-end test that drives all eleven steps against a fake transport, and one
   marked `razorpay_live` that does it against real test mode.

Ship 1 to 4 before anything else. Steps 5 and 6 are what make the demo legible, but a
demo that cannot take a payment has nothing to explain.

## 4b. Phase 2, only after the API and worker run end to end

Do not begin any of this while step 7 is unfinished. Order matters: each one below is
worth more when the thing underneath it actually works.

| Step | Work | Note |
| --- | --- | --- |
| 9 | RazorAI agents through ADK and Gemini 3.8 Flash | 3,153 lines exist on `wt/agent-runtime` but `rendering/messages.py` was never written, so the package does not import, and it has zero tests. Fix the import, then write the behavioural suite before anything else. |
| 11 | Support: reconciliation service, resolution service, human-review queue | The queue and evidence only; the demonstration shows no human resolving a case. |
| 14 | UCP business profile and lifecycle | The `requires_escalation` handoff matters: test mode has no headless charge path. |
| 15 | Full AP2 v0.2 human-present cryptography | Pinned at commit `b4587ac1`; its `jwcrypto`, `cryptography` and `pydantic` pins must win resolution. |
| 16 | ACP-compatible endpoints and an external buyer simulator | |
| 10 | Voice | 4,148 lines on `wt/voice`, 91 of 97 tests pass, six rotation failures, gateway is an empty stub. Split pipeline only: never `run_live`, never native audio. |
| 21 | MCP | Last, and only if every gate above is green. |

Both phase-2 worktrees are stale and both modify `pyproject.toml` and `uv.lock`. Rebase
them onto `main` before resuming either, or the lockfile becomes a three-way conflict.

## 5. Rules

- Only the kernel writes financial tables. The API and worker call kernel functions; they
  never write `payment_attempts`, `execution_grants`, `orders`, `refunds`,
  `provider_requests` or `reconciliation_runs` directly. The database grants enforce this
  and a test should prove it.
- Every provider mutation consumes exactly one Execution Grant.
- The tenant comes from the authenticated session, never from a request body.
- No package cycles: the kernel never imports the adapters, the outbox, the simulator, the
  API or the worker.
- Test file basenames are unique repository-wide. Database tests carry `pytest.mark.db`.
- Never commit `.env` or anything matching `*api_keys*`.

## 6. Gate before every commit

```bash
cd /Users/vedanttyagi/Desktop/acr-worktrees/claude-backend
export PATH="$HOME/.local/bin:$PATH"
uv run --no-sync ruff format packages/ && uv run --no-sync ruff check packages/
uv run --no-sync mypy packages/*/src
uv run --no-sync python -m pytest packages/ -o addopts="" -q
```

Report real output. Never claim green without it.

## 7. Integration duty

Claude wires the two halves together. When Gemini reports:

1. Read `docs/briefs/GEMINI_REPORT.md` and `docs/briefs/REQUESTS_TO_CLAUDE.md`.
2. Action the requests that fall in Claude's files, in particular the content security
   policy once the hotlinked images are gone.
3. `git merge` Gemini's branch into `main`, resolve any conflict in favour of the owner of
   the file, and run the full gate on the merged result.
4. Confirm the frontend's provisional response schemas match the API's real responses, and
   fix whichever side is wrong. That reconciliation is the single most likely source of a
   broken demo, and nobody but Claude can do it.
5. Update `docs/STATUS.md` so every claim traces to a test or a recorded run.
