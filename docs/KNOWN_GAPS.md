# Known gaps

The defect and gap register. An entry here is something that is not built, is built and not
wired in, or is built and known to be wrong, together with enough reasoning that whoever
picks it up does not have to rediscover why it was left. Entries are numbered inside their
sections and the numbers are stable: `docs/adr/0005-protocol-layer.md` and
`docs/adr/0006-voice-runtime.md` cite items here by number, so an item is retired by
striking its body and saying so, never by renumbering the ones after it.

An entry that is closed stays, marked closed, when the decision behind it is worth keeping.
`Decided (no action)` below is one of those: it exists so nobody reads specification 25.4
and concludes the protocol layer is unfinished.

## Open: thirteen pointers expect a runbook at `docs/DEMO.md`

Recorded 2026-09-09. Eight documents that asserted a *current state* were deleted --
STATUS, SUBMISSION, PITCH, STORYBOARD, DEMO, DEMO_READINESS, BLINKIT_DESIGN_SPEC and a
handoff document -- along with README.md. That was right: each had gone false, and a document
that misstates the system is worse than no document, because a reader who checks one claim
and finds it wrong discounts every other claim in the repository.

This file was deleted with them and has been restored, because it is not the same kind of
document. It records decisions and their reasons, dated, the way the ADRs do; five
surviving permanent documents and three source files cite it **by numbered item**
(`adr/0006-voice-runtime.md` items 1, 5, 7a and 8; `test_voice_gateway.py` item 4;
`test_ar_prompts.py` item 9). An ADR is a permanent record and must not cite a deleted
file.

What is still open is the runbook. Thirteen places expect one at exactly `docs/DEMO.md`:

| Where | Count | What it expects |
| --- | --- | --- |
| `scripts/seed_demo_state.py` | 6 | step numbers, the injected milk price, troubleshooting item 3 |
| `docs/SCENARIO_RUNBOOK.md` | 3 | "follow DEMO.md for the steps and the figures"; it covers only the deltas |
| `Makefile` | 2 | the header comment and the `make help` footer |
| `scripts/README.md` | 2 | where the seeding script's steps are described |

Two constraints on whoever writes the replacement:

1. **The path is load-bearing.** Those references name `docs/DEMO.md` specifically. A
   runbook at a different path leaves thirteen dangling pointers, and the seeding script's
   comments stop explaining themselves.
2. **`SCENARIO_RUNBOOK.md` is a delta, not a whole.** It says in its own first line that it
   covers what DEMO.md does not. Without DEMO.md it describes the exceptions to a happy
   path nobody has written down.

Also still dangling, and smaller: `docs/THREAT_MODEL.md` opens by citing `docs/STATUS.md`
for its evidence, and `test_ar_grounding.py` cites `docs/DEMO_READINESS.md` for two
sentences it asserts. `conftest.py` was in this list and has been fixed: the CI rule that
forbids a silently skipped `db` test now carries its own reason instead of pointing at the
deleted evidence table.

---

## Closed: load AP2/UCP signing keys from configuration, not from a process-local fallback
File(s): packages/commerce-api/src/commerce_api/settings.py
Why: specification 15.5 requires encrypted ES256 test private keys to be stored in Secret
Manager and loaded only into a dedicated signer module, with merchant, platform and mock
credential-provider keys kept separate. The protocol layer has the signer
(`commerce_protocols.ap2.signing.InProcessSigner`, which refuses anything that is not a
private P-256 key carrying a `kid`) and the key ring, but `settings.py` belongs to another
build unit, so `routers/protocols.py` currently reads `UCP_MERCHANT_SIGNING_JWK` and
`UCP_PLATFORM_SIGNING_JWK` straight from the environment and mints an ephemeral key when
neither is set. That fallback is announced honestly -- the published profile carries
`ephemeral_keys: true` -- but an ephemeral key means the JWK Set changes on every restart,
so any merchant authorization or receipt signed before a restart stops verifying
afterwards. Specification 14.1's "rotate keys without silently invalidating stored
evidence" cannot be satisfied while the keys are ephemeral.
Proposed change: add `ucp_merchant_signing_jwk: SecretStr | None` and
`ucp_platform_signing_jwk: SecretStr | None` to `Settings`, resolved the same way the
Razorpay secrets are, and expose them on `app.state` so `routers/protocols.py` can build
its signers through `settings_of(request)` rather than through `os.environ`. Two separate
values, not one: "the merchant signed this checkout" and "the platform signed this receipt"
must stay distinguishable, and they stop being distinguishable the moment one key can
produce both signatures. The router already reads two variables and publishes two key ids,
so this is a change of source rather than of shape.

**Built on 2026-09-05, as proposed.** `Settings` gained `ucp_merchant_signing_jwk` and
`ucp_platform_signing_jwk`, both `SecretStr | None`, both validated in `_check` so a key
that will not load stops the process instead of surfacing to a counterparty fetching the
JWK Set. `routers/protocols.py` builds its signers through `settings_of(request)`;
`os.environ`, `lru_cache` and `ec.generate_private_key` are gone from it entirely.

Three things the work turned up that the entry above did not anticipate:

1. **The fallback was worse than "the keys change".** It minted fresh key material but
   reused a *fixed* `kid` (`merchant-ephemeral-1`), so evidence signed before a restart
   was refused afterwards at `signature_did_not_verify` -- the same refusal a forged
   signature produces, not the `unknown_kid` an honestly rotated key produces. Nobody
   holding real evidence could have told a restart from an attack. Demonstrated by
   running the pre-change function, lifted verbatim from git, in two processes.
2. **Unconfigured had to mean absent, not empty.** The profiles now answer 404 and
   `GET /v1/protocols` reports `signing_keys_configured: false`, following ADR 0003 D11
   and the ACP/MCP transports. An empty JWK Set would have described a surface that is
   present and broken.
3. **The `kid` collision needed refusing explicitly.** `KeyRing.of` catches a duplicate
   `kid` *within* one ring, and the merchant and platform are two separate rings, so an
   operator pasting the same JWK into both variables would not have been caught anywhere.
   `Settings.ucp_signers` refuses it at startup.

`ephemeral_keys` is gone from the published profile and from the matrix: there is no
longer a state it could report. The matrix carries `signing_keys_configured` instead.

Proven, not merely asserted: `TestSignedEvidenceSurvivesARestart` in
`packages/commerce-api/tests/test_capi_protocols.py` signs with one `Settings` and
verifies with another built from the same environment, with a negative control that a
genuinely swapped key is still refused. End to end on 2026-09-05: a process signed
evidence and exited, an API was started on `:8090`, killed, and started again, and the
pre-restart signature verified against the JWK Set the restarted server published --
through `verify_compact`, with no check relaxed.
Status: CLOSED 2026-09-05.

---

## Closed: README.md's front page carried figures and images the system does not answer

File(s): README.md
Why: the submission package (docs/PITCH.md, docs/STORYBOARD.md, docs/SUBMISSION.md,
scripts/capture_screenshots.mjs, docs/images/) was built under a rule that no figure may
appear unless it was re-measured. Applying the same rule to README turned up numbers that
disagree with what the API and the test suite answer. The prose figures were corrected on
2026-09-05; the four stills in section 1 were not, and that is what is left.

The STATUS.md half of this entry is retired: `docs/STATUS.md` was re-measured in full on
2026-09-05 and no longer contains any of the figures it named.

**Corrected in the prose on 2026-09-05**, each against the command named:

| Said | Now says | Command |
| --- | --- | --- |
| badge "tests-187 passing" | 4,591 | `pytest packages -o addopts="--strict-markers"` → `4591 passed, 6 skipped` |
| "58 grounded products across 9 categories" (§1) | 247 across 10 — §5 of the same file already said 247, so §1 contradicted §5 | `len(CATALOGUE)`, `len({p.category for p in CATALOGUE})` |
| "Commerce API — 44 routes" | 50 OpenAPI paths, 51 operations | count of method/path pairs in `create_app().openapi()` |
| "merchant simulator tests (158 tests)" | 175 | `pytest packages/merchant-sim` |
| "buyer-web unit tests (29 tests)" | 220 in 14 files | `npm test` in `apps/buyer-web` |
| kernel "13,527 lines" (twice) | 13,593 | `find packages/transaction-kernel/src -name '*.py' -exec cat {} + \| wc -l` |
| "Frontend test suites — being written", "Realtime Voice — in progress", "Protocol layer — in progress" | all three now carry their measured test counts | per-package `pytest` and `npm test` |

**Closed 2026-09-05: the four stills were replaced.**

Each replacement was opened and read before it was used, and the old set turned out to be
worse than stale. `03_refusal_hero_card.webp` and `02_agent_panel.webp` both carried a
`MOCK MODE` badge and both showed the *agent panel* offering a button that authorized
payment — "Authorize Version 2 (₹395.00)" and "Review & Authorize Payment" — against a
fictional SKU (`GRO-DAIRY-001`). The panel that ships says the opposite in its own footer:
approving and paying happen on the store's pages and never in the panel. So the front page
was illustrating the architecture's central rule with two pictures of that rule being
broken. `04_mobile_storefront_390.webp` carried the same mock-mode badge, clipped its own
header, and sat under a caption claiming a bottom-sheet drawer and zero horizontal scroll
that the image did not show.

What replaced them, and what each was checked against:

| Slot | Now | Checked |
| --- | --- | --- |
| hero | `06_the_refusal.png` | ₹579.95 struck through → ₹681.95, +₹102.00, `REAPPROVAL_REQUIRED`, v1 `INVALIDATED` beside v2 `APPROVAL_REQUIRED`, reason key `merchant_state_changed_since_approval` |
| storefront | `01_storefront_home.png` | ten categories in the grid; `len(CATALOGUE)` = 247, `len({p.category})` = 10 |
| copilot | `03_razorai_panel.png` | a Hinglish turn, five grounded hits, one "searched the catalogue" chip, and the footer disclaiming approve and pay |
| mobile | `04_mobile_storefront_390.png` | newly captured; see below |
| evidence | `09b_console_proof_chain.png` | chain verification 12/12 and 1/1, proof verdict `HOLDS`, `capture_evidence_is_verified` reported `n/a` rather than green |

There was no `.png` replacement for the 390px still, so one was taken:
`scripts/capture_screenshots.mjs` grew a `--mobile-only` mode that captures at exactly 390
CSS pixels and, crucially, **measures** the page rather than only photographing it. An
image of a page that overflows sideways looks exactly like an image of one that does not,
because the overflow is off the right edge of both — so the run records
`document.scrollWidth` against the viewport width in the manifest, and the caption quotes
that measurement (390 px against 390 px) instead of asserting the claim. The mode skips the
catalogue reset the full run opens with, because that reset is shared with every other
session on this machine and one mobile screenshot is not worth it; it merges its shot into
the existing manifest rather than replacing it.

The caption also stopped claiming the two things the image does not show — a bottom-sheet
drawer and 44px tap targets. They may well be true; this figure is not evidence of them.
Status: CLOSED 2026-09-05 — prose corrected earlier the same day, images now replaced.

---

## Decided (no action): specification 25.4's protocol tables are not being created
File(s): packages/platform-db/**
Why: specification 25.4 names six tables -- `signing_key_metadata`, `protocol_sessions`,
`protocol_messages`, `ap2_mandates`, `ap2_receipts`, `replay_guards` -- and gives no columns
for any of them. None exist in any migration or ORM model. **The protocol layer does not
need them and is complete without them**, so this is a note rather than a blocker, recorded
so nobody later reads section 25.4 and concludes the layer is unfinished.

What was built instead, and why (ADR 0005 records the reasoning in full): protocol evidence
rides on `audit_events` through `transaction_kernel.audit.append`, which already provides a
gapless, hash-chained, tenant-scoped, tamper-evident stream with a verifier endpoint
already shipped -- everything `protocol_messages` would have needed, without a new schema to
get right. Replay and nonce guards ride on `idempotency_records` through
`transaction_kernel.idempotency`, whose unique index gives a real atomic single-winner
claim; a purpose-built `replay_guards` table would have meant writing that race condition
again. `ap2_mandates` and `ap2_receipts` are not needed because mandates and receipts are
self-verifying artifacts -- their bytes are the evidence, and they are recorded in the
evidence chain by fingerprint.
Proposed change: none, now or later. If the tables are ever wanted for query performance
over protocol traffic, they would be an index over the audit chain rather than a second
source of truth, and `commerce_protocols.core.evidence` is the one module that would change.
One consequence for `packages/commerce-api/tests/conftest.py`: because this layer adds no
tables, nothing needs adding to `_TENANT_TABLES`.
Status: DECIDED, not open. Reviewed and agreed by the owner of `platform-db` (2026-09-05),
whose reading was that six new tables would have had to earn the tamper-evidence, the RLS and
the grants from scratch, and that each is then a table a reviewer must be convinced of. This
entry stays in the file as the record of a closed decision rather than as work outstanding --
recorded in ADR 0005 P2. Do not action it.

---

## Closed: mount the ACP and MCP transports
File(s): packages/commerce-api/src/commerce_api/settings.py,
packages/commerce-api/src/commerce_api/routers/{acp,mcp}.py,
packages/commerce-api/src/commerce_api/services/{protocol_transport,acp_transport,mcp_transport}.py
Why it was open: `commerce_protocols.acp.admit` and `commerce_protocols.mcp.GovernedToolServer`
were complete, tested and adversarially reviewed, and neither was reachable over HTTP.

What was built, and where each of the three warnings in the original entry landed:

1. **The tenant comes from the credential.** MCP binds it from the access token's claims,
   after the token has verified and before anything is read; ACP binds it from the client
   the `Signature-Client` header names, which is the same header `acp.auth` resolves the
   client by, so the bound tenant and the verified client cannot differ. Neither reads a
   body field or a host header. `transaction_kernel.audit`'s refusal is still the backstop
   and is still not the only check.
2. **Kernel denials are HTTP 200 with the structured decision.** MCP returns one as the
   content of a successful tool result; ACP returns one beside the session document.
   Protocol rejections go through `commerce_api.errors`, which now names `ProtocolRejection`
   in `_MAPPED_ROOTS` -- without that entry a signature that did not verify reached the
   catch-all handler and was reported as a 500.
3. **The audience is reduced, never taken by position.** `SignedTokenIntrospector` accepts a
   multi-valued `aud`, returns the resource it actually validated against, and refuses a
   token whose audience does not name this server. `open_session` compares it again.

Rate limiting: both surfaces are limited on the verified client id and tenant, through
`TokenBucketLimiter`, which grew a `take_for` so the two surfaces share one implementation
of the refill arithmetic and separate instances of the buckets.

`KernelAdmission.admit_approved` is implemented by
`services.protocol_transport.PlatformAdmission` over `admission_service.submit_checkout`,
the same function `POST /v1/checkouts/{id}/versions/{v}/submit` calls. Its signature was not
widened. `submit_checkout` was split into `submit_checkout_outcome`, which returns the typed
`KernelDecision` beside the body the routers return, because the port needs the decision and
a second admission would have been the alternative.

Two things a reader should know rather than discover:

* **The transports are absent unless configured.** `MCP_RESOURCE` + `MCP_TOKEN_SECRET`, and
  `ACP_AUDIENCE` + `ACP_CLIENTS`, each pair required together. Unconfigured, the routes
  answer 404 rather than 401, on ADR 0003 D11's reasoning. There is no generated fallback
  for the MCP token secret: a process that mints its own would answer to tokens nobody
  issued it.
* **ACP fulfilment is per request, and it is a narrowing.** `REQUIRED_FOR_READINESS` wants
  items, buyer and fulfilment. Items are basket lines and buyer is the credential's
  `buyer_ref`, both durable; this platform stores no fulfilment address at all, because the
  merchant simulator prices delivery from a policy. So a fulfilment block counts toward
  readiness for the request that carries it, a client that sends fulfilment first and items
  second must repeat it, and the session document echoes `supplied` and `requires` so a
  client can see what was counted. Inside the `COMPATIBLE_INTERFACE` boundary specification
  13.2 pins ACP at, and stated on the wire rather than only here.
Status: CLOSED 2026-09-05. `packages/commerce-api/tests/test_capi_transports.py` covers the
journey and, at more length, the refusals.

---

## ~~What the Case Specialist needs from human_review_service~~ — RESOLVED
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

**Built, as proposed above, with three deliberate changes.** `CaseBackend` is in
`backends/base.py`; `InMemoryBackend` and `HttpBackend` both implement it; the factory
builds `support_case_read` and `present_case` only against a backend that has it, exactly
as it does the merchant reads; `SessionProvenance` gained `remember_case`/`knows_case` and
`check_case_provenance`, so `present_case` is held on a case the session never read. The
Case Specialist is no longer reported in `BoundToolset.unbuilt` at all.

What was changed against the sketch, and why:

1. `state` and `priority` are enums (`CaseState`, `CasePriority`) rather than `str`, and a
   value outside them is a contract violation rather than a case with an unusual priority.
   The console rendering a chip for a fourth priority is the failure this prevents at the
   seam instead of at each surface: `Priority` has three members and there is no P4 to
   render. `reason_code` is `RecoveryCode` for the same reason.
2. `support_case_read` covers both backend methods -- with no `case_key` it lists the
   queue, with one it reads that case -- because the roster has a single read tool and a
   model with no listing could only ever name a key somebody typed into the message. The
   listing is what grounds a key so `present_case` has something to be held against.
3. `scope_note` is carried when the backend supplies one and *not* restated by
   agent-runtime when it does not. A second copy of the platform's own sentence about what
   this surface does would drift the first time the service changed its mind. The
   structural half of the limit, `resolvable_here: false`, is on every result, and
   `CaseRecord` refuses to be constructed claiming otherwise.

Still open beside it, and still warned: the Support Specialist's `policy_search`,
`resolution_evaluate` and `support_escalate`. Those need the Resolution Service and the
Policy-at-Sale Receipt reached the same way, not the review queue.
Status: RESOLVED (the case tools); the Support tools above remain OPEN

---

## What the Support Specialist's last three tools need, and why it is not another CaseBackend
File(s): packages/commerce-api/src/commerce_api/routers/{orders,review}.py,
packages/commerce-api/src/commerce_api/services/resolution_service.py,
packages/agent-runtime/src/agent_runtime/backends/base.py
Why: `policy_search`, `resolution_evaluate` and `support_escalate` are the three roster
tools the suite still warns about. It would be reasonable to assume they need what the case
tools needed -- a protocol in `backends/base.py` over a service that already answers -- and
they do not. That assumption is the thing this entry exists to correct, because acting on
it produces a backend protocol with nothing behind it on the HTTP side, which is a tool
that passes its own tests and fails in the product.

The case tools were unblocked by a *seam*: `human_review_service` already answered, and
`routers/review.py` already served it at `GET /v1/review/queue` and `/queue/{case_key}`.
Only the protocol was missing. None of that holds here.

1. **`policy_search` has no data source over HTTP at all.** The terms it must quote are the
   order's Policy-at-Sale Receipt, read through `transaction_kernel.receipts.policy_for_order`
   -- and the only caller of that function anywhere is `resolution_service._policy`. No
   route returns receipt terms: `GET /v1/orders/{id}` carries `policy_receipt_hash` and
   nothing else, which is an integrity handle, not a policy a buyer can be told about. A
   read of the terms is needed first, and it must reach the receipt the same way the
   service does -- through `policy_for_order`, with no parameter that could reach the
   merchant's *current* policy table, so a merchant who tightened their rule yesterday
   still cannot narrow a sale made last week.

2. **`resolution_evaluate` is reachable only from an operator surface, keyed by the wrong
   thing.** `resolve.evaluate` takes a `Finding` and a `Projection`, not an order, and its
   only two callers are `routers/review.py` and `human_review_service` -- both behind
   `X-Scenario-Key`. The Support Specialist is `Surface.BUYER`. Pointing a buyer-facing
   agent at an operator route keyed by a payment attempt would hand it findings about other
   people's stuck payments, which is the opposite of what the scenario key is there for. So
   this needs a buyer-scoped route keyed by an order the caller owns, resolving order ->
   attempt -> projection -> findings -> resolutions server-side, and returning the
   `Resolution` shape the review router already publishes. The amount still comes only from
   the plan; agent-runtime must not compute one, which is why this is an API change and not
   a client one.

3. **`support_escalate` has no route, and it freezes an attempt.** `transaction_kernel.
   payments.escalate` has no caller in `commerce-api` today. It takes a
   `payment_attempt_id`, moves the attempt to `ESCALATED` through the transition table, and
   `ESCALATED` is terminal -- no automated edge leaves it. That is a state change on the
   money path, made on an identifier a buyer surface does not hold. Exposing it to an agent
   needs the route, the order -> attempt resolution, and a decision about what authorises
   the call, which is not the model saying so. It is correctly last.

Proposed change: two buyer-scoped read routes first (the receipt terms, and a resolution
evaluation by order), then one `SupportBackend` protocol in `backends/base.py` over both,
built the way `CaseBackend` was -- the factory offering the tools only against a backend
that has it. `support_escalate` after that, and separately, because it is the only one of
the three that writes.

Until then the three stay in `BoundToolset.unbuilt` and the suite keeps naming them, which
is the register working: a Support Specialist that cannot yet quote a resolution says so,
and that is a better answer than one that quotes a number nobody derived.
Status: OPEN

---

### Progress on 2026-09-05, and what is still unreachable

**Landed (uncommitted at the time of writing, on `main`'s working tree):**

- `GET /v1/orders/{id}/policy` and `GET /v1/orders/{id}/resolution` in `routers/orders.py`
  -- the two buyer-scoped reads named above as prerequisites. They live in `orders.py`
  because it is already owner-checked; `ResolutionOut` is reused from `review.py`.
  Covered by `tests/test_capi_support_reads.py`.
- `SupportBackend` in `backends/base.py` (`PolicyAtSale`, `PolicyTerm`, `ResolutionPlan`,
  `RemedyOption`, `WithheldRemedy`, `OrderResolution`), implemented by both the in-memory
  and HTTP backends. `ResolutionPlan.__post_init__` refuses an option above
  `refundable_minor`, a currency mismatch, or a no-plan code that carries a plan.

**Not landed:**

- Registration in `capabilities/tools.py` (`_SUPPORT_BUILDERS`, `_bind_support`, the
  `isinstance(backend, SupportBackend)` branch beside the `CaseBackend` one). `registry.py`
  already has every row these need.
- `packages/agent-runtime/tests/test_ar_support_backend.py` (written, lost to ENOSPC).

**The finding that outranks the registration: no session can reach the Support roster.**
`deps.py` `BUYER_CAPABILITIES` and `AGENT_CAPABILITIES` contain none of `policy.search`,
`resolution.evaluate`, `support.escalate`, `support.case.read`. After `agent_service.bind`'s
two intersections the Support principal holds exactly `{order.read}`, and `build_toolset`
skips a tool whose capability the principal lacks *before* recording it, so nothing appears
in `unbuilt`. Only `OPERATOR_CAPABILITIES` carries them, and the merchant copilot routes to
GROWTH and CASE only. The fix is one line per read capability in `deps.py`; it was not made
unilaterally because `support.escalate` freezes an attempt (money path). Recommendation:
widen BUYER with the two **reads** (`policy.search`, `resolution.evaluate`) over the buyer's
own order, and leave `support.escalate` out until the route below exists.

**`support_escalate` has no gate today, so it must not be exposed as-is.**
`transaction_kernel/payments.py` `escalate` has no caller in `commerce-api`; its only guard
is the transition table under the attempt's version lock, which constrains *which hop*, never
who asks or why. Its real callers -- the reconciliation worker and the refund path -- act on
already-verified divergence. An agent-reachable route therefore needs a gate built from the
platform's own finding, not the model's text: open a case only where `recon.project` already
reports a finding on that order, derive `reason` from the finding's `FindingCode`, accept no
priority at all (`human_review_service._priority` derives P1/P2/P3 from rows). `ESCALATED`
is terminal; a wrong call is not recoverable.

## Voice (`packages/voice-runtime`, `apps/buyer-web/src/features/voice`)

Nine items, numbered and cited by number from `docs/adr/0006-voice-runtime.md` section 6.
None of them blocks the voice runtime itself -- it is built, tested and green, 386 tests
passing with 8 skipped for want of `GOOGLE_CLOUD_PROJECT` -- and item 1 has since been
resolved by the two storefront routes that now stand in front of the gateway, so what stands
between "the pipeline works" and "a buyer can talk to the storefront" is now item 2 and the
fact that a spoken yes through a real microphone has not been driven end to end here.

### 1. ~~Nothing serves the voice WebSocket to the browser~~ — RESOLVED

~~The storefront's voice panel connects to a same-origin `/api/voice/stream`, because
`src/lib/security/csp.ts` sets `connect-src 'self'` and a same-origin URL is the only one
that policy permits. The gateway is its own ASGI app (`voice_runtime.gateway.create_app`,
run it with uvicorn) and there is no route in front of it. Two routes are needed in
`apps/buyer-web/src/app/api/`, both owned by the storefront session:~~

- ~~`POST /api/voice/tickets` -> proxy to the gateway's `POST /v1/voice/tickets`, forwarding
  the buyer's bearer. Returns `{ticket, expires_in_s, session_id, speech_available}`.~~
- ~~`GET /api/voice/stream` -> WebSocket proxy to the gateway's `/v1/voice/stream?ticket=...`.~~

~~The ticket is why this is safe to proxy: it is opaque, single-use, 60 seconds, and the
bearer never leaves the server side. See `voice_runtime/wire/tickets.py`.~~

~~If a WebSocket proxy in Next is more trouble than it is worth, the alternative is to widen
`connect-src` to the gateway's origin and let the browser connect to it directly. That is a
deliberate CSP change, which is why it is a request and not a patch.~~

Both routes now exist as tracked files with tests beside them:
`apps/buyer-web/src/app/api/voice/tickets/route.ts`,
`apps/buyer-web/src/app/api/voice/stream/route.ts`, and
`apps/buyer-web/src/app/api/voice/__tests__/`. The ticket is always minted same-origin
through the storefront's own server, which is the one side that holds the buyer's bearer (it
lives in an `httpOnly` cookie), so the bearer never reaches the browser and the browser
receives only an opaque single-use handle. The socket itself was resolved by the second path
the original entry named rather than a WebSocket proxy: in development the browser dials the
gateway origin directly at `ws://127.0.0.1:8100`, which `apps/buyer-web/src/lib/security/csp.ts`
names in `connect-src` when `NODE_ENV` is not production, and `voiceGatewayOrigin` in
`apps/buyer-web/src/features/voice/session.ts` resolves; in a deployment the gateway sits
behind the same reverse proxy and `'self'` covers it, so the stream route is a documented
503 explaining who serves the path rather than a relay. What is still not proven is a spoken
yes through a real microphone: there is no audio device here, `getUserMedia` never runs, and
the real-audio tests remain skipped, so the spoken leg is exercised only by the automated
suite. The entry stays, struck, so that item 2 keeps its number and the ADR's citations keep
resolving.

**Corrected 9 September: this was marked resolved while a buyer still could not talk to the
storefront, and the reason was not in this entry at all.** Everything above is true — the
routes exist, the ticket is opaque and single-use, the CSP resolves. What none of it said is
that *nothing started the gateway*. It landed on 6 September and no script, Makefile target
or launch configuration ever mentioned it: `scripts/run_demo.sh` opened with an argument for
why it starts more than one process and then started two of three. So every run of the
demonstration since opened a storefront whose microphone button could not connect, and the
copilot said "the voice connection dropped" — which was true, and was nobody's bug, because
nothing was ever started to drop.

`run_demo.sh` starts it now, and reports what is not answering rather than assuming a
spawned process is a serving one. The lesson is the entry's, not the gateway's: **resolved
was taken to mean the code was written.** A capability nothing launches is not reachable,
and this file is where that distinction has to be made or it will be made by whoever runs
the demonstration.

### 2. `script-src` may block the AudioWorklet

`csp.ts` has `script-src 'self' 'nonce-...'` with no `blob:`. `worker-src` already allows
`blob:`, but Chrome can govern AudioWorklet modules under `script-src`, and the worklet is
loaded from a blob URL. The capture path falls back to `ScriptProcessorNode` when the
worklet fails, so voice still works either way -- but the fallback is deprecated and runs
mic downsampling on the main thread, which is exactly where audio glitches come from.

Either add `blob:` to `script-src`, or serve the worklet from `public/` as a static file
and load it by path. The second is cleaner and needs no CSP change.

### 3. ~~The storefront fixture had drifted from the catalogue~~ — RESOLVED

Three failing parity tests were recorded here. They are gone: the drifting fixture
comparison was replaced by `test_ms_catalogue_integrity.py`. The entry stays, struck, so
that item 4 keeps its number and the ADR's citations keep resolving.

### 4. `GET /v1/agent/capabilities` does not return `tenant_id`

The gateway binds a voice ticket to the session and the bearer, and would bind it to the
tenant as well. The capabilities response carries `copilot`, `actor_type`, the two
capability lists and the specialists, but no tenant. The gateway therefore reads the
session id out of `principal_id` (`session:<id>/razorai/<specialist>`) and leaves
`tenant_id` unset.

This is not a security gap -- every call the gateway makes carries the buyer's own bearer
and the server enforces tenancy on each one -- so the ticket's tenant check is defence in
depth that is currently inert. One extra field on that response would arm it.

### 5. Cloud Text-to-Speech is disabled on the Google project

Specification 19.2 pins Chirp 3 HD (`en-IN-Chirp3-HD-Kore`, `hi-IN-Chirp3-HD-Kore`) for
**transactional** speech. Calling it returns:

```
403 PermissionDenied ... reason: "SERVICE_DISABLED" service: "texttospeech.googleapis.com"
```

Gemini TTS on Vertex works (both `gemini-3.1-flash-tts-preview` and the `gemini-2.5-flash-tts`
fallback), so voice is fully functional today; the synthesizer is a fallback chain and the
substituted voice is surfaced to the buyer rather than silently different. To get the
pinned transactional voice, someone with console access needs to enable
`texttospeech.googleapis.com` on the deployment's own Google Cloud project (the value of
`GOOGLE_CLOUD_PROJECT`; see `.env.example`) and set an ADC quota project. `gcloud` on this
machine cannot do it: its user token is expired
(`invalid_grant`), and re-authenticating is an interactive login.

### 6. RazorAI writes for a screen, and voice needs a voice register

Speaking to the running gateway, "two litres of milk" comes back as a single
**350-character** sentence listing five products with full names and prices. Spoken, that
is roughly 35 seconds of audio, and the first sample cannot play until enough of it is
synthesised.

The voice layer has taken this as far as it can from outside: it splits the sentence at
its commas and synthesises two phrases ahead, which moved time-to-first-audio from 21.1 s
to 8.3 s and whole-reply delivery from 39.9 s to 20.4 s (ADR 0006 section 4.2b). The rest
is prose, and prose belongs to `agent-runtime`.

What would help, in the shopping specialist's prompt, when `modality` is `voice`:

- Name at most two or three products, not five. The screen already has all of them.
- One fact per sentence. Short sentences are what make a spoken reply feel responsive,
  because each one starts playing while the next is still being synthesised.
- Prices as "73 rupees", not "(73.00 INR)". The parenthesis is a screen convention and the
  currency code is read aloud as three letters.
- End with a question. A voice turn that does not hand the conversation back leaves the
  buyer unsure whether it is their turn.

The harness already carries `modality` on the session (`harness/base.py::_modality`) and
`context={"modality": "voice"}` reaches it, so the hook exists; nothing reads it in the
prompt yet. Voice will get whatever improvement lands here for free -- the guard checks
what is said, not how long it is.

### 7. Correction, and two real gaps for voice in the basket proposal path

**The earlier version of this item was wrong and is retracted.** It claimed no specialist
could fill a basket. There is a path, and it is the right one: the agent emits a
`basket.update` **proposal** carrying `executes_on: "trusted_surface"`, and the trusted
surface executes it. Agents propose, deterministic systems execute -- exactly the
architecture. `agent_service._line_proposal` builds it, and it only fires for a product
that appeared in this turn's own tool results, which is the provenance check working.

Driving it with real sentences found two things that matter specifically for voice.

**7a. A spoken quantity is lost. This is the one worth fixing.**

```
"add two AMUL-DAIRY-002"
 -> proposal {"action":"basket.update","sku":"AMUL-DAIRY-002","quantity":1, ...}
```

The buyer said *two*. The proposal says *one*. `_QUANTITY` matches digits, and nobody
speaks digits: they say "two litres", "do litre", "ek dozen", "aadha kilo". Typed input
mostly gets away with it because people type "2"; a voice buyer never does, so this is
close to a 100% failure rate on the spoken path for any quantity above one.

It is not a money-safety hole -- the proposal is shown on the trusted surface with the
quantity on it, and the buyer confirms there -- but the buyer has to correct the assistant
every single time they ask for more than one of something, which is most of a grocery
basket. Number words in English, Hindi and Hinglish (and the Devanagari digits ०-९) would
fix it. The voice layer cannot: it hands over a transcript, and rewriting the buyer's words
before the agent sees them is exactly the kind of quiet interpretation this project avoids.

**7b. A proposal needs the turn to resolve to one product, and speech rarely does.**

`"add Amul Gold Full Cream Milk 1 L"` returns five search hits and no proposal; only
`"add two AMUL-DAIRY-002"` narrows to `kind: product` and proposes. Nobody says a SKU
aloud. So the spoken path reaches a proposal only when the buyer's phrasing happens to
resolve to exactly one product.

Both belong to `commerce-api`'s `agent_service.py` rather than to the voice layer. Voice inherits any improvement for free: the gateway sends a sentence
and relays whatever comes back.


### 8. `POST /v1/agent/turn` returns no kernel decision, so voice cannot speak from a template

Specification 19.10 says the model does not author speech for approvals, totals, deltas,
reservation expiry, payment outcomes, cancellation effects, refunds or delegated authority.
Those sentences are rendered from versioned locale templates filled with server-confirmed
fields. `packages/voice-runtime/src/voice_runtime/tts/templates.py` does exactly that, in
both locales, recording template id, version and the verified fields for the audit trail --
and the pipeline speaks them with `deterministic=True`.

It is never reached in production. `TurnOut` carries `reply`, `language`, `specialist`,
`routing_reason`, `principal_id`, `tool_calls`, `denials` and `structured`; none of those is
a `KernelDecision`, so nothing can populate `TurnReply.decision` and `render_decision` is
never called on the live path.

The consequence is worth stating plainly. With no template to fall back to, **the outbound
content guard is the only thing between a model and a spoken transactional claim**, and a
refusal is silence rather than a correct sentence. An adversarial review of that guard
found it was passing `Your payment was successful.` and `Your money has been returned to
your account.`; it has been rewritten to fail closed, but a guard is a worse mechanism for
this than a template, and 19.10 says so.

**Update: the voice half is now built, so this is a one-sided change.**

`agent_runtime.rendering.cards.decision_card` already produces exactly the right shape, and
`agent_runtime.capabilities.tools._build_present_decision` already calls it. What is missing
is only that `commerce_api.services.agent_service` never emits it: its `structured` payload
is a product, a search page, a basket, a checkout, an order or a metrics block, and there is
no `kind: "decision"` among them.

`voice_runtime.tts.templates.render_decision_card` now renders that card through the same
template tables as the `KernelDecision` path, and a test asserts the two cannot drift.
Every `RecoveryCode` is covered in both locales. `gateway/agent_client.decision_card_in`
looks for the card on every turn and finds `None` today.

So the whole change is: emit the `decision` card in `structured` on a turn that produced a
decision. Voice will speak it deterministically, with `deterministic=True`, its template id
and version, and the amount in integer minor units, the moment it appears -- no further
change on this side.

(Rendering from the card rather than reconstructing a `KernelDecision` is deliberate: the
kernel's type rightly refuses an allowed decision that names no Execution Grant, and the
card does not carry the grant id. Faking one to satisfy a constructor would be inventing a
fact about money to make a renderer happy.)


### 9. A voice register for the shopping prompt — proposed text, ready to paste

Status: APPLIED 2026-09-06 -- the section below is in `shopping_specialist.md` verbatim (plus one bullet: do not search again when the grounding preamble already lists results), a four-bullet equivalent is in `checkout_specialist.md`, and `tests/test_ar_prompts.py` pins both.

**One correction to how this was first described.** An earlier version said "nothing reads
the modality the
harness already carries", which was wrong in a way that matters. `harness/base.py::_facts`
puts `modality` in the per-turn facts block and `runtime_adk/adapter.py` appends it to the
user turn as *"Session facts from the platform, not from the buyer: … modality=voice; …"*.
The model already receives it. What is missing is only a line in the static prompt telling
it what to do with it — and that is the right place, because ADR 0004 §1.5 requires the
static instruction to be byte-stable across turns, so a per-turn value could never go in it.

**Why it is worth doing.** Spoken, a product search currently returns one 350-character
sentence listing five products: roughly 35 seconds of audio for an answer the buyer can
read in three. The voice layer has taken this as far as it can from outside — phrase
splitting and pipelined synthesis moved time-to-first-audio from 21.1 s to 8.3 s — but the
rest is prose length, and prose is the prompt's.

Proposed addition to `prompts/shopping_specialist.md` (and the same idea, shorter, in
`checkout_specialist.md`):

```markdown
## When the session facts say `modality=voice`

The buyer is listening, not reading, and everything you say is also on their screen. Say
the shortest true thing and hand the conversation back.

- Name at most two or three products. The screen already lists the rest; say how many
  there were and stop.
- One fact per sentence. Short sentences start playing while the next is still being
  synthesised, which is most of what makes a spoken reply feel quick.
- Prices as "73 rupees", not "(73.00 INR)". The parenthesis is a screen convention and a
  currency code is read aloud as three letters.
- Do not repeat the buyer's own words back to them. They know what they said.
- End with a question. A turn that does not hand the conversation back leaves the buyer
  unsure whether it is their turn.
```

**What it must not change.** Every amount still has to be one a tool returned this turn —
the outbound guard checks each spoken sentence against the turn's grounded amounts and
refuses anything else, so a prompt that encouraged rounding or approximating ("about
seventy rupees") would produce a visibly refused sentence rather than a friendlier one.
Nothing here asks the model to author a total, a payment outcome or a refund; those remain
template-rendered.

Voice inherits any improvement automatically: the gateway sends a sentence and speaks
whatever comes back.

---

## Observability is built and is not wired in — the integration points, per file
File(s): `packages/platform-observability/**`, `docs/adr/0007-observability.md`,
two lines in the root `pyproject.toml`.
Why: the package is complete, tested at 388 passing, and deliberately touches nothing
outside its own directory. What follows is the call-site list for wiring it in, which is
still outstanding in all four places.

`packages/platform-observability` is a new workspace member with **no dependencies at all** —
nothing outside the standard library, asserted by a test that reads every import with `ast`.
That is what makes "if the metrics backend is down, commerce continues" structural rather
than intended: nothing here can be in a money path's dependency closure, every recording
returns `None` and catches its own exceptions, and there is no socket, no push and no
background thread. A counter is never the record of a money action; `transaction_kernel.audit`
remains the evidence and this is only the operational view of it.

What it provides: a tenant-labelled metrics registry with a Prometheus text exposition; a
catalogue of ~28 instruments each carrying the decision it informs; structured JSON logging
that redacts by construction in four layers (the `LogValue` type refuses a webhook body at
`mypy --strict`, and `JsonFormatter` scrubs at format time so it covers code that never
imported it); a `ContextVar` correlation scope that survives an `await` and a spawned task;
and a `timed()` span helper with an OpenTelemetry-shaped seam and no OpenTelemetry
dependency. 388 tests.

`docs/adr/0007-observability.md` has the exact wiring — imports, middleware, endpoint, and
the call sites for each instrument. The API recipe was run against a real FastAPI app before
being written down, which is how three traps were found rather than shipped:

1. `BaseHTTPMiddleware` is the wrong base — use a pure ASGI middleware.
2. **A `ContextVar` bound in a dependency or an endpoint is not visible back in the
   middleware.** FastAPI dispatches through anyio under copied contexts. `request.state` is
   the carrier that works; `bind_scope` is what serves everything *inside* the request. The
   dependency should do both.
3. Write that dependency `async def`. A sync `yield` dependency is entered and exited under
   two different contexts, which used to make `ContextVar.reset` raise
   `ValueError: Token was created in a different Context` out of the teardown — turning a
   deliberate 409 into a 500. `bind_scope` now survives that (it restores the parent value
   by hand when its token is foreign), so a sync dependency is safe; it is still wrong,
   because an `async def` endpoint will not see the scope it bound.

The wiring, per package:

- **commerce-api** — `configure_logging()` in the lifespan, `ObservabilityMiddleware`,
  `GET /metrics` returning `REGISTRY.render()` with `PROMETHEUS_CONTENT_TYPE`, and one
  increment each in `admission_service`, `refund_service` and the webhook router off values
  those services already hold (`decision.allowed`, `decision.code.value`).
- **action-executor** — `configure_logging()` in `main()`, and a `bind_scope` +
  `timed(..., WORKER_COMMAND_TIMING)` around `_run_one`. Existing `_LOG` calls need no edit;
  they become JSON with the correlation id attached. The worker has no HTTP server, so the
  scrape is the deployment's problem, not this package's.
- **voice-runtime** — specification 19.13 names frame counters, queue depth, rotation count,
  reconnect count, echo-gate engagement time and barge-in count. **Do not add them to
  `platform-observability`.** Declare an `InstrumentSpec` tuple beside the voice code and call
  `default_registry().register_all(...)`; registration is idempotent for an identical spec
  and refuses a conflicting one. The ADR has all six written out, ready to paste. Two things
  to hold to: one `bind_scope` per voice session, so one correlation id reconstructs the
  whole conversation across every stream rotation (19.13), and `EventLogger.exception` for a
  swallowed callback failure — never `debug`, which is why no `debug` shortcut is offered as
  a convenience anywhere in the package.
- **commerce-protocols** — same pattern, `protocol_` namespace:
  `protocol_messages_total{protocol,version,direction,outcome}` and
  `protocol_verification_failures_total{protocol,reason}`.

One thing to know if you `uv sync` and it fails: the root `pyproject.toml` now lists
`platform-observability` under **both** `[project].dependencies` and `[tool.uv.sources]`.
A member present in one and missing from the other makes `uv sync` fail outright — the
`agent-runtime` failure again — so `test_po_boundary` asserts both entries exist.
Status: the package, its ADR, its tests and its workspace registration are DONE. Wiring it
into the four call sites above is OPEN.

---

## Every CONFIRMED order in `commerce_dev` is seeded, and reads as a real capture

File(s): the rows themselves, and `scripts/seed_demo_state.py`
Why: re-measured 2026-09-05. `commerce_dev` holds **8 orders, all `CONFIRMED`, and all 8
carry `capture_evidence.event_id` beginning `evt_seed_`**:

```sql
select count(*) total,
       count(*) filter (where capture_evidence->>'event_id' like 'evt_seed_%') seeded
from orders;
--  total | seeded
--      8 |      8
```

When this entry was first written there was one such row. There are now eight, so the gap
grew rather than closed, and the claim it undermines is the strongest one in the submission.

**The order ids are genuine; the payment ids are not.** Of the 8 `provider_payment_id`
values on `payment_attempts`, exactly one has ever appeared in a URL the system actually
called — `pay_b13248528bee4d`, and only as the `400 BAD_REQUEST_ERROR` Razorpay returned
when asked to refund a payment it had never issued. That refusal is the provider telling us
the id is invented.

**No webhook has ever applied a capture.** `webhook_inbox` holds 2 rows, both from the
webhook forgery test — an `evt_attack_*` and an `evt_genuine_*` — and both resolved
`apply_status = IGNORED`, `apply_reason = attempt_not_found`. So nothing in this database
has been captured by verified provider evidence, and a row whose `capture_evidence` says
`"channel": "VERIFIED_WEBHOOK"` is asserting something that did not happen.

Two reasons this matters more than a stray test row.

It is invisible in a screenshot. `/orders`, `/operations`, the order detail page and the
console's evidence page all render these as confirmed orders with verified webhook capture,
because that is exactly what the rows say. `scripts/capture_screenshots.mjs` refuses to
photograph any order whose `capture_evidence.event_id` starts `evt_seed_`, and only ever
shoots the order belonging to its own checkout — but nothing stops a person taking that
screenshot by hand, and the whole submission argues that a figure on a screen is a figure
the server produced.

And it undermines the honest version of the claim. There are **30 real test-mode order
creations at `api.razorpay.com`, all HTTP 200**, and a proof chain that returns `n/a` on
`capture_evidence_is_verified` because nothing has been captured. That `n/a` is worth more
to a judge than a green tick, and it is worth less next to eight rows asserting captures
that did not happen.

Proposed change: give the seeded rows a state that reads as seeded from the outside, or move
them to `commerce_test`, which is not the database the demonstration and every screenshot
read from. `docs/SUBMISSION.md` names this under "The honest boundaries" for as long as it
stands; that paragraph changes in the same commit.

Status: OPEN, and worse than when it was filed.

---

## Browsing data is not marked stale when the merchant connector cannot answer

File(s): `packages/commerce-api/src/commerce_api/routers/` (the search and basket paths),
`apps/buyer-web/src/features/` (the surface that would carry the notice)

Why: specification 23.3's merchant-connector row asks for two things when the catalogue,
inventory and pricing connector is unavailable — "mark browsing data stale where safe;
pause quote/revalidation and affected checkout". The pause is built and now answers
honestly: admission's step-8 call to the merchant state source raises, nothing is created,
and the buyer gets a 503 problem carrying `CONNECTOR_UNAVAILABLE` that renders as a
sentence (`packages/commerce-api/tests/test_fs_merchant_connector_unavailable.py`). The
stale-marking is not built at all. Search and basket pricing keep rendering during the
outage — which is the intended behaviour, and is asserted — but they render prices with no
notice that nothing behind them can currently be confirmed.

This is a deliberate scope decision rather than an oversight, and it is recorded here so
the next reader does not have to re-derive it. Going dark on browsing would be worse than
the gap: it would remove the one part of the product that still works during the outage,
and it would do so on a signal the browsing path does not itself observe — search reads the
catalogue directly and never touches the state source, so it has no way to notice the
connector is down except by being told. The honest version needs a piece the system does
not have yet: a connector-health fact that the read paths consult and stamp onto what they
return, next to the freshness stamp they already carry.

Proposed change: extend the freshness envelope search and quote already emit
(`merchant_sim.grounding`, `source_id` and `catalogue_revision`) with a `confirmable`
flag fed by that health fact, and render it on the storefront as the staleness notice
23.3 asks for. Until then the degradation is honest at the checkout boundary and silent
at the browsing boundary, and that asymmetry is the gap.

Status: OPEN, deferred deliberately.

## Gate status 2026-09-05, and what the three closing reports did and did not prove

Appended, not merged into the entries above: the sections before this one are owned by other
sessions and are left byte-for-byte alone. This is one run of the gates plus an audit of the
three reports that closed out the evening, separating what was driven from what was asserted.

### The gates, as run

Commands were redirected to files and read back through `rc=$?`, never a pipe, so no exit
status is swallowed by one.

| gate | result | rc |
|---|---|---|
| `ruff check` (bare, whole repo) | 6 errors | 1 |
| `ruff check packages scripts conftest.py` (what `make lint` runs) | **1 error** | 1 |
| `ruff format --check` (bare, whole repo) | 4 files would be reformatted | 1 |
| `ruff format --check packages scripts conftest.py` | 393 files already formatted | 0 |
| `mypy packages/*/src` | no issues, 238 source files | 0 |
| `REQUIRE_DB=1 pytest packages` | **5426 passed, 6 skipped, 11 xfailed** in 82.85s | 0 |
| real audio, credentials set | **7 passed** in 30.28s | 0 |

Against the recorded baseline of 5378 passed, 6 skipped, 11 xfailed: **+48 passed, skips and
xfailures unchanged, nothing failing**. The +48 are the protocol-signing tests plus the two
new `commerce-api` test modules and the agent-runtime tests the two in-flight workflows added.

The backend suite was run with `addopts` overridden to drop `-q`, because `-q` is what has
previously hidden a count delta on this repo.

### The lint gate is red, and it is nobody's in-flight edit

`make lint` fails on exactly one finding:

```
packages/commerce-api/tests/test_capi_foundation.py:600:5: I001 Import block is un-sorted
```

The function-local import block at line 600 orders `typing`, then `fastapi.params`, then
`commerce_api.routers`. Ruff does not treat `commerce_api` as first-party here — there is no
`[tool.ruff.lint.isort]` `known-first-party` and the packages live under `packages/*/src`, so
its `src` detection does not find them — and therefore wants the two `from` imports in one
block, `commerce_api` before `fastapi`. It is `[*] fixable`.

**This is not the two concurrent workflows' doing.** The file is clean at HEAD, last touched
by `08a1fa7`; the same is true of every other file the bare gates flag. Their in-flight edits
break nothing — the tree is green on types and tests with their work in it.

The other five findings are **outside the scope `make lint` declares** and so have never been
gated: four `E501` and one `S606` in `infra/docker/entrypoint.py`, and Python code fences in
`docs/AGENT_ADVERSARIAL.md`, `docs/CONTINUOUS_LISTENING_ADK.md` and
`docs/adr/0007-observability.md` that `ruff format` now rewrites. That last group is version
drift, not rot: ruff is pinned only as `>=0.6`, the resolved version is 0.16.6 published two
days ago, and formatting Python blocks inside Markdown is new behaviour. Earlier reports that
called ruff "clean" were running the scoped `make lint` and were telling the truth about it.
Anyone quoting a bare `ruff check` as the gate is quoting a different, wider command.

Unrelated and latent, surfaced by the suite as a `SyntaxWarning`:
`packages/voice-runtime/src/voice_runtime/tts/guard.py:90` contains `\w` in a non-raw string,
which a future Python turns from a warning into an error.

### Area 1 — AP2/UCP signing keys

**Works, in the tree.** The generated-key fallback is gone; the router is settings-driven and
`packages/commerce-api/src/commerce_api/routers/protocols.py:235` reports
`signing_keys_configured=settings.ucp_profiles_enabled`. Driven here: mypy strict clean over
that package and the whole suite green, protocol tests included.

**Proven here, and it is a live hazard.** `GET :8000/v1/protocols` on the running stack
returns the keys `['pins', 'ephemeral_keys']` — the old shape. The supervised process has not
reloaded and is still executing the fallback code. `.env` contains **zero** occurrences of
`UCP_MERCHANT_SIGNING_JWK` or `UCP_PLATFORM_SIGNING_JWK`. So the moment anyone restarts the
:8000 stack, the well-known profiles begin answering 404 — correct, documented, degraded
behaviour, and a surprise on stage if it happens between rehearsal and demo. Generate and add
the pair to `.env` **before** the next restart, not after.

**Not verified here.** The restart-survival proof — evidence signed by an exited process,
verified against the JWK Set a twice-restarted server published — was run by that session on
its own `:8090` stack. I did not re-run it. `TestSignedEvidenceSurvivesARestart` passes in the
suite above, which is the property under test but not the same as the live HTTP demonstration.

### Area 2 — Voice

**Works, driven here.** The real-audio suite is **7 passed** with `GOOGLE_CLOUD_PROJECT`,
`GOOGLE_GENAI_USE_VERTEXAI=true` and `GOOGLE_CLOUD_LOCATION=global` set, including
`test_a_spoken_grocery_request_returns_grounded_products` and
`test_a_spoken_yes_records_no_approval`.

**A correction to the record, because it would have sent someone chasing nothing.** The
protocol session's report stated that this suite fails at `resolve_identity` because
`:8000/v1/capabilities` returns 404, and named that as damage from the workflow editing
`capabilities/`. That diagnosis is wrong. `/v1/capabilities` is not a route in this API and
never was — it returns 404 with **and** without a valid bearer. The constant the code actually
uses is `CAPABILITIES_PATH = "/v1/agent/capabilities"`
(`packages/voice-runtime/src/voice_runtime/gateway/agent_client.py:60`), and that path returns
**200** with a bearer minted from `POST /v1/demo/sessions`. Nothing is broken; the probe was
aimed at the wrong URL. Item 4 in the voice section above concerns the same endpoint and is
unaffected.

**Not verified here, and unverifiable in this environment.** `getUserMedia` — the browser pane
has no audio device and no fake-device flag, so the `MediaStream` → 100 ms PCM16 frame
conversion in `capture.ts` has never executed anywhere. Every stage downstream of it was
driven, by feeding synthesised PCM into the live socket in the frame shape `capture.ts`
emits, which is a faithful stand-in for the wire but not for the device. **This is the single
step to rehearse on real hardware before the demo.** The deterministic transaction-speech
templates likewise never fire on the live browser path; that is item 8 above, unchanged, and
its one-sided fix still sits in a file another workflow holds.

### Area 3 — Deployment and infrastructure

**Confirmed here, statically, and both are start-up blockers.** I did not re-run that
session's container work; I checked the two contradictions it turns on, which are the
load-bearing part and are readable in the tree:

1. `packages/action-executor/src/action_executor/settings.py:157` declares
   `razorpay_webhook_secret: SecretStr` with **no default** — required. `infra/terraform/locals.tf:45`
   grants `razorpay-webhook-secret` to `commerce-api` **only**. The worker cannot construct its
   settings from the secrets its own manifest gives it. The repair is a client-only credential
   loader for the worker, *not* granting it the webhook secret, which `docs/DEPLOY.md`
   explicitly prohibits.
2. `infra/kubernetes/base/workloads/action-executor.yaml:138` puts a `livenessProbe` on
   `GET /healthz` port 8001 on `containers[0]`, the `worker` container. The `action_executor`
   package serves no HTTP at all — no `/healthz` handler, no `WORKER_HEALTH_PORT` reader, no
   server of any kind. The opt-out patch is commented out in **both** overlays
   (`overlays/demo/kustomization.yaml:37`, `overlays/dev/kustomization.yaml:37`). The kubelet
   would kill the container on a loop even once blocker 1 is fixed.

Since the worker is the only process that calls Razorpay, a cluster brought up from these
manifests today executes no payment at all.

**Not verified here.** The docker builds, `terraform fmt`/`validate`, the `kubeconform` runs
and `scripts/validate_infra.sh` (reported 72 passed, 1 failed) were that session's work and
were not repeated. **Nothing cloud-side has ever run**: no `terraform apply`, so the IAM
bindings, quotas and region capacity are unproven; Workload Identity, the Cloud SQL proxy
sidecars, the Secret Manager CSI driver, the database migrations and the RLS role boundary
that is the core security claim, Ingress and its GKE-only CRDs, the FQDN NetworkPolicy, and
the Razorpay round trip and webhook receiver are all untested. Schema-valid is not functional,
and none of the above should be read as a deployment that works.

Status: gates green except the one named `I001`; three areas recorded with their proofs and
their holes.

## Razorpay's loader now runs on every route, not only `/checkout/*`

The copilot takes payment where it is mounted, so `apps/buyer-web/src/lib/security/csp.ts`
moved Razorpay's script and frame origins out of `checkoutPolicy` and into the **default**
policy. `'strict-dynamic'`, the per-request nonce and every other directive are unchanged;
`checkoutPolicy` survives as a no-difference alias so existing callers still compile, and
`requiresOwnDocument` is now a constant `false` — the document navigation it forced existed
only to reach the one route whose policy allowed Razorpay, and that distinction is gone.

This is a deliberate widening, made with the owner's decision, and it is a real cost: the
least-privilege posture of the surface ADR 0003 D8 describes is now narrower by one origin
on every route rather than on one. The reason it is acceptable is that the alternative was
worse — a payment sheet reached by navigating out of the conversation, which is the seam
this copilot exists to remove, and which was itself the thing forcing a whole-document load.

Revisit by scoping the policy to the route set that actually mounts the copilot, if that set
ever shrinks back to a subset of the app. Today it is every route, so a route-scoped policy
would be a longer way of writing the same thing.

Status: recorded as a posture change, not a defect. Pinned by `csp.test.ts` and
`csp.checkout.test.ts`, which assert the default policy permits Razorpay's script origin and
that the two policies are now identical.
