# Running and recording the demonstration

From a clean machine to a recorded five-minute video. The sequence being demonstrated is
the eleven steps of `PROJECT_SPECIFICATION.md` section 2.5; the evidence each step must
leave is section 31.4; the endpoints are the catalogue in `docs/adr/0003-service-layer.md`.

The single claim the whole recording exists to prove:

> **Agents propose; deterministic systems authorize and execute.**

Steps 5 to 8 are the part other conversational-commerce demos skip, and they are the
reason this is a payments project rather than a shopping chatbot. If the recording is
running long, cut narration from steps 1 and 2, never from 5 to 8.

---

## 0. There is no mock mode, on purpose

An earlier version of this runbook offered a thirty-second tour against deterministic
fixtures with no database and no keys. That path is gone, and its absence is a feature
rather than a regression.

A storefront that falls back to invented data when the API is unreachable shows a buyer a
price no kernel ever agreed to. On a payments submission that is worse than an honest
error, because every real figure beside it becomes unverifiable. So neither app carries a
fixture: every number on every screen was read from the API in that page load, and a
failed read renders the failure. Turning the API off does not produce a demo, it produces
a screen that says the store is not reachable, which is the truth.

Both apps therefore need the stack below. It takes about two minutes:

```bash
make bootstrap && make seed          # PostgreSQL, roles, migrations, the demo tenant
scripts/run_demo.sh                  # the API on :8000 and the durable worker
cd apps/buyer-web        && npm install && npm run dev    # storefront on :3000
cd apps/merchant-console && npm install && npm run dev    # console    on :3001
```

The storefront's routes are `/`, `/search`, `/c/<category>`, `/p/<sku>`, `/basket`,
`/checkout/<id>`, `/orders` and `/orders/<id>`. RazorAI, the buyer copilot, is the control
at the bottom right of every page. The console's are `/`, `/catalogue`, `/operations`,
`/evidence` and `/inspector`.

Everything below is the real thing: a live API, a real database, and Razorpay test mode.

## 1. Prerequisites

| Thing | Version | Check |
| --- | --- | --- |
| PostgreSQL | 16 | `pg_isready && psql -V` |
| uv | 0.12 or newer | `uv --version` |
| Python | 3.14 (uv manages it) | `uv run --no-sync python -V` |
| Node and npm | 22 | `node -v` |
| Razorpay account | **test mode only** | keys begin `rzp_test_` |

Then create `.env` at the repository root from `.env.example` and fill in three values:

```bash
cp .env.example .env
# RAZORPAY_KEY_ID=rzp_test_...
# RAZORPAY_KEY_SECRET=...
# RAZORPAY_WEBHOOK_SECRET=...        # must differ from the key secret
```

Both processes validate these at start-up. A `rzp_live_` key is refused outside an
explicit production profile with a named approval (specification 11.5), which is a
deliberate guard, not an inconvenience.

`.env` is gitignored. Never paste it into a slide, a terminal you are recording, or a
commit.

---

## 2. Bootstrap, seed, start

Three commands. `make help` lists every target.

```bash
make bootstrap    # databases, migrations, restricted login roles -- and proof they are restricted
make seed         # one demo tenant and one merchant in commerce_dev
make demo         # the API on :8000 and the durable worker, together; Ctrl-C stops both
```

In a second terminal:

```bash
make web          # the buyer storefront on :3000
```

`make bootstrap` is the one worth watching. Its last section verifies that every login
role is `NOSUPERUSER` and `NOBYPASSRLS`, because a PostgreSQL superuser bypasses
row-level security unconditionally — a bootstrap that quietly left a role SUPERUSER would
make every tenant-isolation proof in this repository vacuous. It fails loudly rather than
half-succeeding.

`make seed` prints the identifiers you will need and is idempotent — running it twice
writes nothing the second time:

```
  tenant id       01a06dc7-09f7-7f2c-bd05-df1c773ddb22
  tenant slug     demo
  merchant id     01a06dc7-09fb-7087-bb73-b9c5c646e743
  merchant slug   demo-grocery
```

### Seeding state worth demonstrating

`make seed` gives you a tenant and a merchant. It gives you no orders, no refunds and no
dead letters, because those are money state and money state has one legitimate origin. So
a console opened on a freshly seeded tenant is honest and empty: `/operations` lists
nothing, `/evidence` has no captured revenue to account for, and `/inspector` has no
attempt to open. That demonstrates worse than it deserves.

```bash
uv run --no-sync python scripts/seed_demo_state.py
```

Ten seconds, against the running stack. It drives the real paths — mint a session, build
a basket, open a checkout, approve the exact version, submit for kernel admission, wait
for the worker's Razorpay test-mode order, apply `WEBHOOK` capture evidence through
`transaction_kernel.apply_provider_evidence` — and leaves:

* five confirmed orders from about ₹85 to about ₹1,048, each with real capture evidence;
* one **refused approval that was then re-approved and paid** — version 1 approved at
  ₹579.95, a `PRICE_SET` injection, version 1 refused `REAPPROVAL_REQUIRED`, version 2
  approved at ₹681.95 and captured. That is the shape `/evidence` accounts for, and the
  ₹102.00 it reports is the same 2 × (₹79.00 − ₹28.00) as step 7;
* refunds in `REFUND_PENDING`, `REFUND_UNKNOWN` and `REFUND_FAILED`, which are three
  different facts and not three labels for one;
* a `DEAD` outbox command, so the operations tab's revive control has a subject;
* a cancelled and a rejected checkout.

It never writes an `orders` or `refunds` row itself. Three things are seams, each named
and argued in the script's own docstring: capture evidence is applied by the script
because no webhook can reach a laptop (troubleshooting 3 below), one Execution Grant is
expired early because a grant lives five minutes and a seeder cannot wait, and two queued
commands are held so a refund stays honestly unsent. Everything else — the refusal, the
provider's rejection of a refund, the worker burying a command whose grant would not
authorise it — is the platform's own judgement, unassisted.

It is a convergence rather than a script: it surveys the tenant, creates only what is
missing, and prints found-versus-created. Running it twice writes nothing the second time.

```bash
# rebuild from empty, between takes or after a messy rehearsal
uv run --no-sync python scripts/seed_demo_state.py --reset \
  --admin-database-url postgresql+psycopg://$USER@localhost:5432/commerce_dev

# more orders, and a fresh refusal on top of whatever the tenant already carries
uv run --no-sync python scripts/seed_demo_state.py --orders 8 --refusals 2
```

`--reset` needs an administrative connection because no platform role is granted DELETE on
any table: the API, the worker and the kernel physically cannot erase a financial row.
Clearing a demo tenant is an act from outside the platform and it takes an identity from
outside the platform.

Two things to know before you record. **Run it last.** `/evidence` and the overview tile
open on the *newest* refused approval in the tenant, so anybody else driving the same
tenant afterwards moves what that page shows; the script prints a
`?checkout_id=` link to the one it built when that has already happened. And **it resets
the catalogue** at both ends, which is why the prices in section 3 are true again
afterwards — an already-injected price is the single most likely way to waste a take.

### Before you press record

```bash
export API=http://127.0.0.1:8000
export SK=local-demo-scenario-key            # or SCENARIO_KEY from your .env

# A buyer session. Keep this token: every step below sends it.
export TOKEN=$(curl -sS -X POST $API/v1/demo/sessions \
  -H 'Content-Type: application/json' \
  -d '{"tenant_slug": "demo", "merchant_slug": "demo-grocery", "actor_type": "BUYER"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
export AUTH="Authorization: Bearer $TOKEN"
```

Three header rules, and every 401/422 during a recording is one of them:

* **Every mutation needs `Idempotency-Key: <something unique>`.** Reusing a key replays
  the stored response with `Idempotent-Replayed: true`, which is a feature (ADR D9) and a
  surprise if you did not mean it.
* **Scenario, ops and evidence routes need *both* `Authorization: Bearer` and
  `X-Scenario-Key`.** The session says who you are; the scenario key says the demo
  apparatus exists in this process at all (ADR D11).
* **A kernel denial is HTTP 200 with a structured decision, never a 4xx** (ADR D15). A
  denial is the system working. Do not let a viewer read the 200 as "it went through".

### Resetting between takes

The merchant simulator's catalogue, prices and stock live in the API process's memory
(ADR D14), so an injected price survives until the process restarts. Between takes,
either restart `make demo`, or reset in place:

```bash
curl -sS -X POST $API/v1/scenario/injections \
  -H "$AUTH" -H "X-Scenario-Key: $SK" -H 'Content-Type: application/json' \
  -d '{"kind": "CATALOGUE_RESET", "note": "between takes"}'
```

A take that starts with an already-injected price runs straight through to payment and
never shows steps 6 and 7. That is the single most likely way to waste a recording.

---

## 3. The eleven steps

Two SKUs carry the whole script. Prices are the seeded baseline:

| SKU | Product | Price |
| --- | --- | --- |
| `AMUL-DAIRY-001` | Amul Taaza Toned Milk 500 ml | ₹28.00 |
| `INDI-STPL-001` | India Gate Classic Basmati Rice 5 kg | ₹499.00 |

Every number below was produced by a real run against the local API and is what the
viewer should see, to the paisa.

---

### Step 1 — Multilingual grounded product discovery

**Click:** the storefront search box. Type `doodh`.
**Or:** `curl -sS "$API/v1/catalogue/search?q=doodh" -H "$AUTH"`

**Say:** "The assistant searches the merchant's catalogue, not its own memory. Hinglish
is folded to the same index as English, and every hit carries a source id, a catalogue
revision and an observation time."

**Viewer sees:** milk results for a Hindi-transliterated query, and on each hit a
`freshness` block naming `merchant-sim:demo-grocery/v1`, a `catalogue_revision` and an
`observed_at`. Point at the freshness block. Grounding is a property of the answer, not a
claim about the model.

---

### Step 2 — Useful basket growth within merchant policy

**Click:** add 2 × milk, then 1 × basmati rice.
**Or:**

```bash
BASKET=$(curl -sS -X POST $API/v1/baskets -H "$AUTH" -H "Idempotency-Key: k-basket-1" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["basket_id"])')

curl -sS -X PUT $API/v1/baskets/$BASKET/lines/AMUL-DAIRY-001 \
  -H "$AUTH" -H "Idempotency-Key: k-line-1" -H 'Content-Type: application/json' \
  -d '{"quantity": 2}'

curl -sS -X PUT $API/v1/baskets/$BASKET/lines/INDI-STPL-001 \
  -H "$AUTH" -H "Idempotency-Key: k-line-2" -H 'Content-Type: application/json' \
  -d '{"quantity": 1}'
```

**Say:** "The basket is re-quoted by the merchant on every change. The platform never
computes a total; it asks. Adding the rice crosses the free-delivery threshold, so the
₹25 delivery fee and its tax disappear — that is merchant policy applying, not a discount
the assistant invented."

**Viewer sees:** after the milk, `delivery_fee_minor: 2500` and a total of ₹85.50, with
`gap_to_free_delivery_minor: 44300`. After the rice, `free_delivery_applied: true`,
`delivery_fee_minor: 0`, and a total of **₹579.95**.

---

### Step 3 — Checkout construction

**Click:** *Checkout*.
**Or:** `curl -sS -X POST $API/v1/baskets/$BASKET/checkout -H "$AUTH" -H "Idempotency-Key: k-co-1"`

**Say:** "This freezes version 1: an immutable content document, its canonical hash, a
Policy-at-Sale Receipt recording the rules that governed this sale, and a fifteen-minute
inventory reservation. Nothing has been authorized. This is an offer."

**Viewer sees:** `version: 1`, a `content_hash`, a `policy_receipt_hash`,
`amount_minor: 57995`, and a `reservation` in state `ACTIVE` with an `expires_at`.

Keep the `checkout_id`, `content_hash` and `amount_minor` on screen — the next step
echoes them back.

```bash
export CID=<checkout_id> HASH=<content_hash> AMT=57995
```

---

### Step 4 — Trusted approval

**Click:** the approval card, then *Approve*.
**Or:**

```bash
curl -sS -X POST $API/v1/checkouts/$CID/versions/1/approve \
  -H "$AUTH" -H "Idempotency-Key: k-ap-1" -H 'Content-Type: application/json' \
  -d "{\"content_hash\": \"$HASH\", \"amount_minor\": $AMT, \"currency\": \"INR\"}"
```

**Say:** "The buyer approves a hash and an amount, not a sentence. The body echoes both
back, so an approval can only ever attach to the exact version the buyer was shown. An
agent session cannot reach this endpoint at all — it has no `checkout.approve`
capability."

**Viewer sees:** `state: "APPROVED"`, an `approval_id`, an `expires_at` ten minutes out,
and `authority_epoch: 0`.

---

### Step 5 — Merchant state changes underneath the approved checkout

**This is the moment the whole submission turns on.** Do not rush it.

```bash
curl -sS -X POST $API/v1/scenario/injections \
  -H "$AUTH" -H "X-Scenario-Key: $SK" -H 'Content-Type: application/json' \
  -d '{"kind": "PRICE_SET", "sku": "AMUL-DAIRY-001", "value": 7900, "note": "step 5"}'
```

**Say:** "The merchant now raises the price of the milk from ₹28 to ₹79, after the buyer
approved and before anything was paid. This is the case that breaks most agentic checkout
designs. It is injected through the scenario controller, and it is labelled."

**Viewer sees:** `"label": "SCENARIO_INJECTION"`, an explicit
`{"field": "unit_price_minor", "before": 2800, "after": 7900}`, `revision_before: 0` and
`revision_after: 1`, plus an `audit_event_id` and a `scenario_run_id`.

Say the label out loud. Every injection is written into the audit as
`SCENARIO_INJECTION` and is never mixed with organic data — a panel is entitled to ask
whether the failure was staged, and the honest answer is yes, visibly, on purpose.

---

### Step 6 — The old approval is rejected

```bash
curl -sS -X POST $API/v1/checkouts/$CID/versions/1/submit \
  -H "$AUTH" -H "Idempotency-Key: k-sub-1" -H 'Content-Type: application/json'
```

**Say:** "The agent now submits the approval it holds. The kernel revalidates merchant
state inside the admission transaction and refuses. No Razorpay order exists. No payment
attempt exists. The refusal is the product."

**Viewer sees:** `"allowed": false`, `"code": "REAPPROVAL_REQUIRED"`,
`"explanation": "merchant_state_changed_since_approval"`, and — the two fields to point
at — `"grant_id": null` and `"payment_attempt_id": null`.

Note for the narrator: this response is **HTTP 200**. Say so, or a viewer reading the
status line will think the payment went through.

---

### Step 7 — Exact delta shown; version N+1 created

Same response as step 6. Scroll to `deltas` and `approval_card`.

**Say:** "It does not merely refuse. It says exactly what moved, and it has already built
version 2 with a fresh Policy-at-Sale Receipt and a fresh reservation, inside the same
transaction — so a crash here cannot leave a version that can never be approved."

**Viewer sees:**

```json
"deltas": [{"field_path": "total", "approved": 57995, "current": 68195, "reason": "total_changed"}],
"next_version": 2,
"approval_card": {"version": 2, "amount_minor": 68195, "previous_version": 1, ...}
```

₹579.95 → **₹681.95**. The arithmetic is 2 × (₹79.00 − ₹28.00) = ₹102.00.
Say the subtraction out loud; it is what makes the number credible.

---

### Step 8 — Fresh approval on N+1

**Click:** the new approval card, then *Approve*.
**Or:**

```bash
export H2=<approval_card.content_hash> A2=68195
curl -sS -X POST $API/v1/checkouts/$CID/versions/2/approve \
  -H "$AUTH" -H "Idempotency-Key: k-ap-2" -H 'Content-Type: application/json' \
  -d "{\"content_hash\": \"$H2\", \"amount_minor\": $A2, \"currency\": \"INR\"}"
```

**Say:** "Version 1 is dead and is never revived. The buyer approves the new hash and the
new amount, having been shown the difference. Consent is per version, and it is
mechanical."

**Viewer sees:** `state: "APPROVED"` on version 2.

---

### Step 9 — Razorpay test-mode payment, executed exactly once

```bash
curl -sS -X POST $API/v1/checkouts/$CID/versions/2/submit \
  -H "$AUTH" -H "Idempotency-Key: k-sub-2" -H 'Content-Type: application/json'
```

**Say:** "The kernel admits, issues one single-use Execution Grant, and writes one outbox
command in the same transaction. The API does not call Razorpay. The worker does, holding
the grant, and consumes it once."

**Viewer sees:** `"allowed": true`, a `grant_id`, a `payment_attempt_id`. Then switch to
the operator view:

```bash
curl -sS "$API/v1/ops/outbox" -H "$AUTH" -H "X-Scenario-Key: $SK"
```

`PAYMENT_CREATE_ORDER`, `PENDING` → `LEASED` → `DONE`, with `attempts` counting. Then:

```bash
curl -sS $API/v1/checkouts/$CID/payment -H "$AUTH"
```

which returns the Razorpay order id and key id for the handoff. Complete the payment in
Razorpay Standard Checkout with a test card. On return the browser posts to
`POST /v1/payments/verify`.

**Say, while the callback lands:** "The browser callback is verified and recorded, and it
is *never* treated as capture evidence. Capture is applied only from provider-fetched or
webhook evidence, through a monotonic apply, so a forged or replayed browser return
cannot move money and `CAPTURED` can never regress."

If no webhook arrives — which is normal on a laptop — see
[the third troubleshooting entry](#3-a-razorpay-webhook-cannot-reach-a-laptop). The
capture evidence arrives by provider fetch instead. That is a supported path, not a
failure, and it is worth saying on camera.

---

### Step 10 — Money Action Proof Chain verifies end to end

```bash
curl -sS $API/v1/checkouts/$CID/proof -H "$AUTH"
curl -sS $API/v1/checkouts/$CID/timeline -H "$AUTH"
```

**Click:** the Protocol Inspector view in the storefront, if it is up.

**Say:** "One document links the whole thing: who asked, what the merchant state was, the
version and its hash, the Policy-at-Sale Receipt, the buyer's approval, the kernel's
decision, the single Execution Grant, the Razorpay evidence, and the final state. Each
link is checkable on its own."

**Viewer sees:** the numbered links `1_intent`, `2_merchant_state`, `3_checkout` and the
rest; in the timeline, the `SCENARIO_INJECTION` entry flagged `scenario_injection: true`
and every entry carrying an `audit` block with a `seq` and a `self_hash`. Then verify the
chain itself:

```bash
curl -sS "$API/v1/audit/streams/checkout/$CID/verify" -H "$AUTH" -H "X-Scenario-Key: $SK"
```

**Say:** "The audit is hash-chained per stream. Editing one field, deleting one row or
swapping two rows breaks every link after it."

---

### Step 11 — Merchant retained-revenue evidence

```bash
export MERCHANT=<merchant id printed by make seed>
curl -sS "$API/v1/merchants/$MERCHANT/evidence/retained-revenue?checkout_id=$CID" \
  -H "$AUTH" -H "X-Scenario-Key: $SK"
```

**Say:** "The merchant's side of the same event. The stale version at ₹579.95 was
refused; the corrected version at ₹681.95 was captured. The difference is revenue the
merchant kept by refusing a stale approval instead of honouring it — and the response
says `controlled_scenario: true`, because this is a reproducible scenario and not a
production lift claim."

**Viewer sees:** `stale_approved_minor`, `corrected_total_minor`, `captured_minor`,
`difference_minor`, `net_retained_minor`, and `controlled_scenario: true`.

Close on that flag. Claiming less than the evidence supports is the posture the whole
project is arguing for.

---

## 4. Troubleshooting

### 1. PostgreSQL is not running

**Looks like:** `make bootstrap` stops at its first step; or the API start-up fails with
`connection to server at "127.0.0.1", port 5432 failed`.

```bash
pg_isready                      # expect: accepting connections
brew services start postgresql@16       # macOS
sudo systemctl start postgresql         # Linux
make bootstrap                  # safe to re-run; it re-verifies everything
```

If the cluster runs under a different admin role than your login name:

```bash
BOOTSTRAP_ADMIN_USER=postgres PGPASSWORD=postgres make bootstrap
```

A related failure with a different cause: `remaining connection slots are reserved for
roles with the SUPERUSER attribute` means the connection pool is exhausted, usually by
several test runs at once. `SHOW max_connections;` and
`SELECT count(*) FROM pg_stat_activity;` will confirm it. Stop the other runs; nothing is
broken.

### 2. The roles are not bootstrapped

**Looks like:** database suites skip with "PostgreSQL not reachable"; or the API starts
but every request 500s with a permission error; or `make test` fails a run that looks
green in the dots.

```bash
make bootstrap
```

Then check by hand that the check actually passed:

```bash
psql -d postgres -tAc "SELECT rolname, rolsuper, rolbypassrls, rolcanlogin
                       FROM pg_roles WHERE rolname LIKE 'commerce_%_%' ORDER BY 1"
```

Every login role must read `f | f | t`. **A `t` in the first or second column is the
serious one.** A superuser or a `BYPASSRLS` role ignores row-level security entirely, so
the isolation suites would pass while proving nothing. Fix it and re-run:

```bash
psql -d postgres -c 'ALTER ROLE commerce_dev_app NOSUPERUSER NOBYPASSRLS'
```

`make test` sets `REQUIRE_DB=1`, which turns a skipped database suite into a failure
(ADR D12) — precisely so this condition cannot hide behind a green run.

### 3. A Razorpay webhook cannot reach a laptop

**Looks like:** the payment completes in Razorpay Standard Checkout, the browser returns,
and the order does not move to captured immediately. Nothing in the logs mentions the
webhook, because it never arrived.

**This is expected, and it is not a failure.** Razorpay posts webhooks to a public URL. A
laptop on a home network does not have one, so `POST /webhooks/razorpay/demo` is never
called. The system is designed for exactly this:

* `POST /v1/payments/verify` verifies the browser callback's
  `HMAC(order_id|payment_id)`, records it as `BROWSER_CALLBACK`, and enqueues
  `RECONCILE_PAYMENT`. It never applies capture — a browser is not a trustworthy source of
  money truth (ADR D8).
* The worker then **fetches the payment from Razorpay** and applies the result as
  `PROVIDER_FETCH` evidence through `payments.apply_provider_evidence`, which uses a
  monotonic apply so `CAPTURED` never regresses.
* Reconciliation is bounded: six attempts with exponential backoff, then `ESCALATED`
  (ADR D13). It settles in seconds on test mode.

So the order does reach captured, by the provider-fetch path, and the proof chain shows
`PROVIDER_FETCH` rather than `WEBHOOK` as the evidence source. **Say this on camera.** A
system that only works when the happy-path webhook arrives is the thing this design
exists to avoid.

Watch it happen:

```bash
curl -sS "$API/v1/ops/outbox" -H "$AUTH" -H "X-Scenario-Key: $SK"   # RECONCILE_PAYMENT
curl -sS "$API/v1/inspector/payment-attempts/<attempt id>" -H "$AUTH" -H "X-Scenario-Key: $SK"
```

**If you want real webhooks anyway**, put a public URL in front of the API with a tunnel,
register `https://<public-host>/webhooks/razorpay/demo` in the Razorpay dashboard, and
set the dashboard's webhook secret to the same `RAZORPAY_WEBHOOK_SECRET` in `.env` — the
receiver verifies the HMAC over the raw bytes before parsing any JSON, so a mismatched
secret is rejected as a forgery rather than a configuration error. With that in place you
also get the idempotency demonstration for free:

```bash
curl -sS -X POST $API/v1/scenario/webhooks/<inbox_id>/replay -H "$AUTH" -H "X-Scenario-Key: $SK"
```

The same raw event, redelivered byte for byte, is marked a duplicate and state does not
change.

---

## 5. If something else goes wrong mid-recording

| Symptom | Cause | Fix |
| --- | --- | --- |
| `401 Not authenticated` on a scenario route | sent `X-Scenario-Key` but not the bearer token | send both headers |
| `404` on `/v1/scenario/...` or `/v1/demo/sessions` | `PROFILE=production` | these routes genuinely do not exist in production; use `development` |
| `422` naming `Idempotency-Key` | a mutation without the header | add it; make it unique per logical action |
| `Idempotent-Replayed: true` on a response you expected to be new | key reused | new key |
| Step 6 allows the payment instead of refusing | the price injection is still in effect from the previous take | `CATALOGUE_RESET`, or restart `make demo` |
| The API refuses to start, naming `WEB_CONCURRENCY` | more than one worker process | it must be 1 (ADR D14); the simulator's state is in-process |
| Port 8000 busy | a previous run | `lsof -i tcp:8000`, or `PORT=8080 make demo` |
| The storefront says the store is not reachable | the API is down, or `COMMERCE_API_URL` points elsewhere | start the API; the storefront has no fixture to fall back to, and that is deliberate |
| The storefront renders but nothing is clickable, and the grid shows skeletons forever | a Content-Security-Policy that blocks every script. `'strict-dynamic'` makes a browser ignore the `'self'` beside it, so Next's own chunks are refused | the policy in `src/lib/security/csp.ts` must not carry `'strict-dynamic'`, and `style-src` must carry no nonce, because a nonce there voids the `'unsafe-inline'` React needs |
| A code change to the storefront has no effect | an orphaned `next-server` from an earlier run still holds the port, so the new one never bound | `lsof -ti:3000 \| xargs kill -9`, then `rm -rf .next` and start again |
| Checkout fails with `reservation_refused: no hold could be taken ... CONCURRENT_OPERATION` | Live reservations from earlier runs are still holding the stock. Correct behaviour, and it self-heals when they lapse, but a rapid demo outruns the TTL | Expire them through the scenario controller: `POST /v1/scenario/reservations/{checkout_id}/{version}/expire` for each `ACTIVE` row, then retry. Do not delete the rows |
| Voice is silent, and the log says `PermissionDenied: 403 ... requires a quota project` | Application Default Credentials carry no quota project, so Text-to-Speech refuses before synthesising. Speech recognition is unaffected, which makes this look like a TTS bug rather than a credentials one | `gcloud auth application-default set-quota-project $GOOGLE_CLOUD_PROJECT`, once per machine |
| A scenario injection answers `409` | the price is already the value being set | inject a different value; the merchant simulator refuses a change that changes nothing |
| Two test suites interfere, or an unscripted call reaches Razorpay | two worktrees sharing `commerce_test`; the durable-worker suite reads a fourth override, `DATABASE_URL_TEST_WORKER` | give each worktree its own test database and set all four `DATABASE_URL_TEST_*` variables |

---

## 6. What to have open when you record

1. The storefront at `http://localhost:3000` — the buyer's view.
2. A terminal running `make demo`, showing `[api]` and `[worker]` lines interleaved. The
   worker lines are what make "the worker is the only process that calls Razorpay"
   visible rather than asserted.
3. A second terminal for the `curl` calls in steps 5, 10 and 11.
4. `http://localhost:8000/docs` — the OpenAPI page, if a panel asks what else exists.

The line to close on:

> Next.js renders, Gemini and ADK converse, FastAPI coordinates, PostgreSQL preserves
> truth, the Transaction Assurance Kernel authorizes, and Razorpay executes.
