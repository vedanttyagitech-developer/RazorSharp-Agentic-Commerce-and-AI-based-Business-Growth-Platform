# Scenario runbook: driving the failures live

`docs/DEMO.md` is the runbook for the happy path — specification 31.1, with the exact
rupee figures a viewer should see. **It is the document to follow on stage.** This one is
its companion, and it covers the parts DEMO.md does not:

- the **pre-flight checks** that make a lever fail confusingly if skipped;
- a **lever reference** saying which injections work today and which do not, so nobody
  reaches on stage for one that is wired to nothing;
- **specification 31.2**, the secondary scenario — late capture, refund, Safe Mode — which
  DEMO.md does not cover at all;
- the **honest state** of each step, including one that does not currently do what its own
  code comment says it does.

## How to read the verification marks

An unverified step in a runbook is worse than an absent one, so every step is marked:

| Mark | Means |
| --- | --- |
| **[RUN]** | Executed against a live API and worker, and the output quoted is the output seen |
| **[READ]** | Established from source and tests, not executed end to end |
| **[NOT RUN]** | Neither — listed so nobody assumes it was checked |

Everything marked **[RUN]** was driven against an isolated stack (a private database, API
on port 8090, its own worker) rather than the shared demo stack, so the figures are real
but the identifiers will differ from yours.

---

## 1. Pre-flight: four checks, in this order

Skipping any of these produces a failure that looks like a broken platform and is not.

### 1.1 The worker must be running — this one will cost you the demo

**[RUN]** Several steps need the durable worker, and the failure mode is silent until it
is baffling. A checkout only reaches `AWAITING_PAYMENT` once the worker has recorded the
Razorpay order, and **the late-capture lever refuses any earlier state**:

```
# Without a worker running:
POST /v1/scenario/checkouts/{id}/invalidate-open
409  illegal_transition: CheckoutState: EXECUTION_PENDING -> INVALIDATED_AWAITING_PAYMENT_RESULT is not a legal transition
```

That reads like a broken lever. It is a correct refusal from the state machine: the
payment surface was never opened, so there is nothing to invalidate. Check first:

```bash
ps -eo pid,etime,command | grep "[d]urable_worker"
```

If nothing comes back, start it (`scripts/run_demo.sh --worker-only`) and wait for the
checkout to reach `AWAITING_PAYMENT` before throwing the lever.

### 1.2 No faults may be left armed from rehearsal

**[RUN]** This bit me during verification and it will bite a presenter harder. A scenario
fault is single-use, but an armed fault that is **never reached stays armed indefinitely**
and fires on the next matching command — which may be a later, unrelated run.

A tenant-wide `CREATE_ORDER_TIMEOUT` armed at 15:21 during verification fired on an
unrelated checkout at 15:29, which went to `PAYMENT_UNKNOWN` in the middle of what was
supposed to be a clean sequence. Nothing was wrong; the platform did exactly what it had
been told to do eight minutes earlier.

```bash
psql -d "$DEMO_DB" -c \
  "SELECT kind, armed, consumed_at IS NOT NULL AS consumed, checkout_id IS NULL AS tenant_wide
     FROM scenario_faults ORDER BY created_at;"
```

Every row must read `armed = f` before you start. To clear them:

```bash
psql -d "$DEMO_DB" -c "UPDATE scenario_faults SET armed = false WHERE armed;"
```

There is no disarm endpoint; SQL is the only way. Prefer arming a fault **scoped to a
checkout id** rather than tenant-wide, so a stray one cannot catch an unrelated payment.

### 1.3 Confirm Safe Mode from the right endpoint

**[RUN]** `GET /v1/config` reports `safe_mode` for the **global** scope. It is
unauthenticated and process-wide, so it cannot know which tenant you mean. A tenant-scoped
Safe Mode therefore does **not** appear there:

```
tenant in SAFE_MODE  ->  GET /v1/config          {"safe_mode": false}     <- global scope
                         GET /v1/ops/safe-mode   {"safe_mode": true, "scope": "TENANT"}
```

Use `/v1/ops/safe-mode` to confirm the switch. Reading `/v1/config` and announcing "Safe
Mode is off" while it is on is an easy and very public mistake.

### 1.4 The scenario key must reach both processes

**[READ]** The console presents `X-Scenario-Key`; the API verifies it. If the API has no
key configured the routes **do not exist** and answer `404`, not `401` — deliberately, so a
production deployment does not advertise them. `GET /v1/config` reports
`scenario_routes_enabled`; it must be `true`. A `401` instead means both sides have a key
and they differ.

---

## 2. Lever reference — what works today

**[RUN]** unless marked otherwise. Every row was exercised against a live API.

| Lever | Status | Verified result |
| --- | --- | --- |
| `POST /v1/scenario/injections` `SELL_OUT` / `STOCK_SET` | works | `201`, delta `stock_units 48 → 0`, one labelled audit row |
| `POST /v1/scenario/injections` `PRICE_SET` | works | `201`; the next submit answers `200 allowed=false code=REAPPROVAL_REQUIRED next_version=2` |
| `POST /v1/scenario/injections` `DELIVERY_FEE_SET`, `FREE_DELIVERY_THRESHOLD_SET`, `AVAILABILITY_SET`, `CATALOGUE_RESET` | **[READ]** | Same code path as the two above |
| `POST /v1/scenario/reservations/{id}/{v}/expire` | works | `200`; next submit answers `200 allowed=false code=RESERVATION_EXPIRED` |
| `POST /v1/scenario/duplicate-submit` | works | `200`, `admitted_count=1`, one grant, codes `['OK', 'DUPLICATE_OPERATION']` |
| Idempotent replay (same `Idempotency-Key`) | works | `201` then `200` with `Idempotent-Replayed: true` and the same basket id |
| `POST /v1/scenario/webhooks/{inbox_id}/replay` | works | `200`, `duplicate_confirmed=true`, count `0 → 1`, `signature_reverified=true` |
| `POST /v1/scenario/faults` `CREATE_ORDER_TIMEOUT` | works | `201` armed; the attempt goes `UNKNOWN` and reconciliation is enqueued |
| `POST /v1/scenario/faults` `REFUND_TIMEOUT` | works | `201` armed |
| `POST /v1/scenario/checkouts/{id}/invalidate-open` | works, **needs the worker** | `200`, `AWAITING_PAYMENT → INVALIDATED_AWAITING_PAYMENT_RESULT` |
| `POST /v1/ops/safe-mode` | works | `200`, `mode: SAFE_MODE`, `scope: TENANT`, audited actor |

### Known defects — do not demonstrate these until they land

All five were found by driving the platform rather than reading it, on the evening of
5 September 2026. **Every one is being fixed by the sessions that own the code**, and each
row says what to do on stage in the meantime. The `Status` column is meant to be flipped to
**FIXED** as each lands — check it against the tree you are actually presenting from, not
against this sentence.

| # | Defect | Symptom on stage | Status | Until it lands |
| --- | --- | --- | --- | --- |
| 1 | `PAYMENT_FETCH_TIMEOUT` is armable but has **no consumer** | Answers `201 armed: true`, then nothing ever happens. You arm a payment-fetch timeout and watch a normal payment succeed. The row sits `armed=true, consumed=false` in `scenario_faults` forever | **Being fixed tonight** — fault-lever workflow, owns `scenario_service.py` and `faults.py` | **Do not arm it.** Use `CREATE_ORDER_TIMEOUT` to show an unknown payment |
| 2 | `RECONCILE_FETCH_TIMEOUT` is consumed by the worker but the API **refuses to arm it** (`422`) | The ADR D13 bounded-attempts escalation is the one injection that cannot be started | **Being fixed tonight** — same workflow | **Do not script the escalation demo.** The behaviour is covered by `test_dwk_reconcile.py`; cite the test, do not promise a live run |
| 3 | Merchant connector failure answers **`409`, not `503`**, with an internal class name as the title | A dead catalogue connector is reported to the buyer as a state conflict they could resolve by re-approving. They cannot. No `RecoveryCode` exists for it, so there is no deterministic buyer-facing message | **Being fixed tonight** — kernel `RecoveryCode` plus status mapping and rendered message | **Do not induce it deliberately.** If it happens by accident (conference wifi), say the money invariant held — nothing was fabricated — and move on. That part is true and is the part that matters |
| 4 | **Late capture produces no refund.** `admit_stale_capture_refund` exists in the kernel, is unit-tested, and nothing calls it | Specification 31.2's "one refund is created" does not happen. Zero `refunds`, zero `REFUND_EXECUTE` commands. The fulfilment block *is* real — zero `orders` | **Being fixed tonight** — refund-wiring workflow, owns `apply_webhook.py` and `reconcile.py` | **Do not say "and one refund appears."** Demonstrate the fulfilment block, which is proven, and say the automatic refund is implemented in the kernel and not yet connected to the capture path |
| 5 | No LLM / STT / TTS fault lever exists | Specification 31.3 requires this injection; every spelling is refused `422` | **Being fixed tonight** — fault-lever workflow | The *responses* are implemented and tested (`voice_runtime.pipeline` degradation frames; the agent harness fallback). Cite those tests; do not promise a live injection |

Defect 4 is the one most likely to be reached on stage, because it sits inside a scripted
31.2 step rather than behind a lever somebody has to choose to pull. Read section 4.4 before
performing that sequence.

Fuller write-ups, with the evidence for each, are in `docs/FAILURE_SCENARIOS.md`.

---

## 3. Specification 31.1 — prerequisites overlay

**Follow `docs/DEMO.md` for the steps and the figures.** This section adds only the things
that make a step fail in a way that does not look like the step's own fault.

| 31.1 step | Prerequisite that bites |
| --- | --- |
| 1–3 discovery, basket, proposals | **[READ]** A seeded tenant. `POST /v1/demo/sessions` needs `tenant_slug`; a fresh database has no tenants and answers `404` |
| 4 reservation | **[RUN]** None |
| 5 approval | **[RUN]** The approval must echo the **exact** `content_hash` and `amount_minor` from the card. A stale card gives a refusal that looks like a bug and is consent working |
| 6 injection | **[RUN]** Pre-flight 1.2. A leftover armed fault will change this step's outcome |
| 7–9 refusal, delta, N+1 | **[RUN]** Nothing extra. The denial is `200 allowed=false`, **not** a 4xx — a presenter reading HTTP status in a network panel may think it failed |
| 10 approve N+1 | **[RUN]** None |
| 11 grant + worker creates the order | **[RUN]** Pre-flight 1.1. Without the worker the checkout sits at `EXECUTION_PENDING` forever and every later step fails |
| 12–13 Razorpay Checkout, capture verified | **[NOT RUN]** Needs the browser and a reachable webhook. Capture is only ever learned from a webhook or a provider fetch, never from the browser callback |
| 14 webhook replayed, marked duplicate | **[RUN]** Needs an inbox row to replay, so step 13 must have delivered one first |
| 15 proof chain | **[NOT RUN]** UI not exercised |
| 16 Merchant Copilot revenue | **[NOT RUN]** UI not exercised |

---

## 4. Specification 31.2 — the secondary scenario

This is the part `docs/DEMO.md` does not cover. Sequence verified end to end except where
marked.

### 4.1 Open a checkout and let the worker create the order

**[RUN]**

```
POST /v1/checkouts/{id}/versions/{v}/submit     ->  200 allowed=true code=OK
# wait for the worker
GET  /v1/checkouts/{id}                          ->  state = AWAITING_PAYMENT
```

`AWAITING_PAYMENT` is the gate for everything below. Do not proceed until you see it.

### 4.2 Invalidate the checkout while the payment surface is open

**[RUN]**

```
POST /v1/scenario/checkouts/{id}/invalidate-open   {"reason": "SCENARIO_LATE_CAPTURE"}
200  AWAITING_PAYMENT -> INVALIDATED_AWAITING_PAYMENT_RESULT
```

The reservation is deliberately **kept**. Stock that may in fact have been paid for must
not be resold before the late capture is resolved.

### 4.3 The late capture arrives

**[RUN]** A signed `payment.captured` webhook naming the attempt's `provider_order_id`:

```
POST /webhooks/razorpay/{tenant_slug}
200  {"received": true, "duplicate": false, "inbox_id": "...", "event_type": "payment.captured"}
```

Then, once applied:

```
payment attempt status   ->  STALE_CAPTURE
orders written           ->  0        <- fulfilment blocked, as required
```

**Fulfilment is blocked and that half is proven.** Zero rows in `orders` for that checkout.

### 4.4 The refund — READ THIS BEFORE DEMONSTRATING IT

**[RUN], and it does not currently happen.**

Specification 31.2 says "One refund is created", specification 34 says late capture
"triggers one refund and no fulfilment", and the lever's own docstring in
`scenario_service.invalidate_open_checkout` says a capture arriving now means "no order is
written, and **exactly one automatic refund is admitted**".

Observed after a real late capture:

```
refunds for the attempt        ->  0
REFUND_EXECUTE outbox commands ->  0
```

The kernel primitive `transaction_kernel.admit_stale_capture_refund` **exists and is unit
tested** (five references in `test_tk_refunds.py`). Nothing in the application calls it:
the only mention outside the kernel and its own tests is the docstring quoted above. The
webhook-apply handler classifies the capture as `STALE_CAPTURE` through
`apply_provider_evidence` and stops there.

**Do not demonstrate this step as "and one refund appears" until it is wired.** Say what is
true: the fulfilment block is proven, the refund admission is implemented in the kernel and
not yet connected to the capture path. The money invariant that matters — nothing is
fulfilled against an invalidated version — holds.

### 4.5 The duplicate webhook is swallowed

**[RUN]** Replay the stored bytes:

```
POST /v1/scenario/webhooks/{inbox_id}/replay
200  duplicate_confirmed=true   duplicate_count 0 -> 1   signature_reverified=true
```

`signature_reverified` is the interesting field: the signature is re-checked against the
stored **raw body**, which proves the inbox kept the bytes that were signed rather than a
re-serialisation of them. `{"a":1,"b":2}` and `{"b":2,"a":1}` mean the same thing and hash
differently, so a receiver that stored parsed JSON could never prove this.

Orders and refunds are unchanged by the replay.

### 4.6 Support explanation and the human-review case

**[NOT RUN]** Neither the support turn nor the human-review queue was exercised. The queue
and its evidence are in P0; no human resolution is demonstrable by design.

### 4.7 Safe Mode

**[RUN]** All four calls verified live.

```
GET  /v1/ops/safe-mode                  ->  200  mode=NORMAL scope=GLOBAL
POST /v1/ops/safe-mode {"enabled": true, "reason": "OPERATOR_DECLARED_INCIDENT"}
                                        ->  200  mode=SAFE_MODE scope=TENANT
                                                 actor="operator:session:..."
```

Then, **with the switch on**, a fresh human-present checkout still admits:

```
POST /v1/checkouts/{id}/versions/{v}/submit  ->  200 allowed=true code=OK
```

That is the point of the step and it is easy to under-sell. Safe Mode's activation sweep
withdraws **delegated** grants only (`RESERVE_DEBIT`); a human-present Standard Checkout
the buyer is sitting in front of runs to completion, and refund grants are never swept.
Stopping a buyer who has already approved a total makes nobody safer — it strands them
holding a reservation and no order over an incident that had nothing to do with them.

Stand down when finished:

```
POST /v1/ops/safe-mode {"enabled": false, "reason": "INCIDENT_RESOLVED"}  ->  200 mode=NORMAL
```

Grants revoked on activation are **not** resurrected. A stopped delegated debit must be
re-admitted, which re-checks reservation, approval and authority epoch from scratch.

An **AGENT** session calling this route is refused `403`. The LLM cannot touch the kill
switch, and that refusal is worth showing.

---

## 5. What was not executed, and by whom

Stated plainly so nobody treats this document as broader evidence than it is. Not exercised
during this verification pass:

- **Any browser UI.** The storefront (`:3000`) and console (`:3001`) were never opened.
  Every result above is from the HTTP API.
- **Razorpay Standard Checkout itself** (31.1 steps 12–13). The provider *order* creation is
  verified — the worker made real test-mode orders — but no card was entered and no
  genuine provider capture was observed. The late-capture webhook in 4.3 was constructed
  and signed locally with the configured webhook secret.
- **Voice** (31.1 step 1's spoken variant). Covered by `packages/voice-runtime` and its
  real-audio suite, which requires `GOOGLE_CLOUD_PROJECT`; not driven here.
- **Protocol Inspector, proof-chain UI, Merchant Copilot revenue** (steps 15–16).
- **Support turn and human-review queue** (4.6).
- **The multilingual step.** No Hindi or Hinglish input was sent.

For the storefront and console journeys, the sessions that built them hold the evidence.
For the real-audio voice rows, see section 6 — the headline suite figure is not evidence
for them, and the number of tests that skip is not what it looks like.

---

## 6. Reproducing the real-audio voice evidence

**[RUN]** for the skip behaviour and both counts; the passing run is **[READ]**, performed
by the session that owns the voice runtime and quoted here with their figures.

`packages/voice-runtime/tests/test_voice_real_audio.py` holds **seven** tests, all marked
`voice_live`. They are the evidence for specification 35's realtime-STT-rotation, echo-gate
and barge-in rows. Read this section before concluding anything from a run of them.

### They are not deselected — they skip for want of a credential

There is no `-m` filter anywhere: not in `addopts` (`-q --strict-markers`), not in CI, not
in any config. **The tests are collected on every run, including CI.** They skip at
*runtime*, from `pytest.skip("GOOGLE_CLOUD_PROJECT is not set")` inside the `project()`
helper the fixtures call.

The distinction matters because it changes what a green CI run means. CI collects these,
skips them for the missing credential, and stays green — correct behaviour, since the
repository-wide rule in `conftest.py` deliberately enforces only `db`-marked skips. But it
means **CI never supplies the evidence for those three specification-35 rows**, and nobody
should read a green pipeline as though it did.

### The skip count is cache-dependent, so neither 6 nor 7 is a stable number

`speech_16k()` caches synthesised PCM under `tempfile.gettempdir()/voice-runtime-speech-cache`.
One test, `test_synthesised_speech_meets_the_recognizer_contract`, only needs synthesised
audio — so on a machine where that cache is warm it reads from disk, never reaches
`project()`, and passes without any credential at all.

Measured both ways, with `TMPDIR` pointed at an empty directory rather than deleting
anyone's cache:

```
cold cache, no credential (a fresh machine, CI):   7 skipped
warm cache, no credential (a laptop that has run them before):   1 passed, 6 skipped
```

So the honest statement is: **all seven require `GOOGLE_CLOUD_PROJECT`; on a machine with a
warm speech cache one of them passes from cache.** Quoting "six skip" or "seven skip" as a
fixed property of the repository is wrong either way, and both of us did it before
measuring.

### If you see `7 skipped`, nothing is broken

That is the single most likely misreading, and it is why this section exists. A fresh
checkout on a fresh machine shows seven skipped voice tests and no explanation beyond the
skip reason. **It means you have no Vertex credential, not that the voice runtime is
failing.** Set the credential and they run.

### The command that produces real evidence

```bash
GOOGLE_CLOUD_PROJECT=<your-project> GOOGLE_GENAI_USE_VERTEXAI=true VOICE_TEST_API_BASE_URL=http://127.0.0.1:8000 uv run --no-sync pytest packages/voice-runtime/tests/test_voice_real_audio.py   -o addopts="" -m voice_live -v
```

It needs Vertex ADC configured, and for the end-to-end cases a commerce API running at
`VOICE_TEST_API_BASE_URL` with a seeded tenant. The reported result from the session that
owns this runtime is **`7 passed, 2 warnings in 31.03s`** against Gemini Transcribe Live
and Chirp 3 HD.

Three of the seven are worth naming, because they are what a judge would probe:

- `test_assistant_audio_played_into_the_microphone_is_not_transcribed` — the echo gate,
  tested with real audio rather than mocked frames. This is the barge-in property.
- `test_a_spoken_grocery_request_returns_grounded_products` — real speech in, real
  recognizer, real commerce API, grounded products out.
- `test_a_spoken_yes_records_no_approval` — **a spoken "yes" records no approval.** Voice
  cannot approve a payment. That is the project's central argument holding at the modality
  boundary, which is exactly where it would be most tempting and most wrong to let a
  transcription stand in for consent.

### Status-table wording

> Realtime STT rotation / echo gate and barge-in: **Verified.** Seven real-audio tests pass
> against Gemini Transcribe Live and Chirp 3 HD with Vertex ADC (`7 passed in 31.03s`).
> They require `GOOGLE_CLOUD_PROJECT` and are **skipped for want of that credential** in
> the default suite run, so the headline suite line is not evidence for these rows — cite
> the real-audio run above.
