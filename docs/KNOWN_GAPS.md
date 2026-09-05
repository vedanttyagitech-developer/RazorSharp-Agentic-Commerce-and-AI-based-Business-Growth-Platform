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

## Load AP2/UCP signing keys from configuration, not from a process-local fallback
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
Status: OPEN

---

## README.md's front page still carries figures the system does not answer

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

**What is left: the four stills in section 1 predate the frontend rebuild.**

`01_storefront_home.webp`, `02_agent_panel.webp`, `03_refusal_hero_card.webp` and
`04_mobile_storefront_390.webp` show a storefront that no longer runs.
`scripts/capture_screenshots.mjs` writes replacements against the live stack, and the ones a
README would want are `docs/images/06_the_refusal.png` (the hero — ₹579.95 struck through,
₹681.95, +₹102.00, v1 INVALIDATED, v2 offered), `01_storefront_home.png`,
`03_razorai_panel.png`, and `09b_console_proof_chain.png`, which is a stronger image than
any of the four currently there. Every figure in them is in
`docs/images/capture-manifest.json`.

The old `.webp` files are deliberately still on disk: README still links them, and a broken
image is worse than a stale one. Delete them in the same commit that repoints the links —
and look at each replacement first, because the front page's hero image is the one thing in
this repository most likely to be judged without being read. There is no `.png` replacement
for the 390px mobile still, so that capture has to be taken before the swap is complete.

Status: OPEN — prose corrected, images not.

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

## Mount the ACP and MCP transports (the two protocol surfaces that are libraries, not routes)
File(s): packages/commerce-api/src/commerce_api/settings.py, plus a new router
Why: `commerce_protocols.acp.admit` and `commerce_protocols.mcp.GovernedToolServer` are
complete, tested (112 and 86 tests) and adversarially reviewed, and neither is reachable over
HTTP. Both need configuration `settings.py` owns. `packages/commerce-api/src/commerce_api/
routers/protocols.py` is deliberately read-only — every route is a GET and a test asserts it
over the route table — so mounting these means a new router file, not an edit to that one.

ACP needs a client registry (client id, tenant, merchant, signing secret, API-key digest,
audience) and this deployment's audience string. MCP needs an RFC 8707 resource indicator and
two ports implemented:

- `commerce_protocols.mcp.KernelAdmission.admit_approved(session, *, principal:
  AgentPrincipal, checkout_id: uuid.UUID, version: int, content_hash: str) -> KernelDecision`
  over the existing admission path — the same one
  `POST /v1/checkouts/{id}/versions/{v}/submit` uses. **Do not widen that signature.** It has
  no tenant_id, no amount, no capabilities and no credential parameter, and that narrowness
  is the proof that a model cannot name any of them. Please do not add a second admission
  route for MCP.
- `commerce_protocols.mcp.TokenIntrospector.introspect(presented: str) -> AccessToken`,
  raising `core.errors.AuthenticationRejected` for anything that does not verify, without
  distinguishing why.

Three details that are easy to get wrong:
1. Bind `app.tenant_id` for the transaction to the tenant the **access token** names, never
   to a request body field or a host header. `transaction_kernel.audit` refuses the first
   evidence row if they disagree, which is the intended backstop, but the route should not
   rely on that as its only check.
2. Kernel denials come back as a `KernelDecision` with `allowed=False` and must be surfaced
   as HTTP 200 carrying the structured decision (ADR 0003 D15). Protocol rejections are
   `core.errors.ProtocolRejection` subclasses carrying a `RecoveryCode` and go through the
   existing problem-detail mapping in `commerce_api.errors`.
3. A JWT `aud` and an RFC 8707 resource indicator are both legitimately multi-valued, but
   `AccessToken.audience` is a single string compared by exact equality. The introspector
   must reduce a multi-valued audience to the one resource it actually validated, and must
   not simply take the first entry.

Neither surface has a per-client or per-tenant rate limit at the MCP layer (ACP has one).
Specification 16.3 requires one on a public protocol surface, and MCP is a public bearer-token
endpoint, so whoever mounts it must supply one keyed on the token's client id and tenant —
never on a body field or a caller-supplied header.
Status: OPEN

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

## Voice (`packages/voice-runtime`, `apps/buyer-web/src/features/voice`)

Nine items, numbered and cited by number from `docs/adr/0006-voice-runtime.md` section 6.
None of them blocks the voice runtime itself -- it is built, tested and green, 233 tests
passing with 6 skipped for want of `GOOGLE_CLOUD_PROJECT` -- but items 1 and 2 are what
stand between "the pipeline works" and "a buyer can talk to the storefront".

### 1. Nothing serves the voice WebSocket to the browser

The storefront's voice panel connects to a same-origin `/api/voice/stream`, because
`src/lib/security/csp.ts` sets `connect-src 'self'` and a same-origin URL is the only one
that policy permits. The gateway is its own ASGI app (`voice_runtime.gateway.create_app`,
run it with uvicorn) and there is no route in front of it. Two routes are needed in
`apps/buyer-web/src/app/api/`, both owned by the storefront session:

- `POST /api/voice/tickets` -> proxy to the gateway's `POST /v1/voice/tickets`, forwarding
  the buyer's bearer. Returns `{ticket, expires_in_s, session_id, speech_available}`.
- `GET /api/voice/stream` -> WebSocket proxy to the gateway's `/v1/voice/stream?ticket=...`.

The ticket is why this is safe to proxy: it is opaque, single-use, 60 seconds, and the
bearer never leaves the server side. See `voice_runtime/wire/tickets.py`.

If a WebSocket proxy in Next is more trouble than it is worth, the alternative is to widen
`connect-src` to the gateway's origin and let the browser connect to it directly. That is a
deliberate CSP change, which is why it is a request and not a patch.

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
`texttospeech.googleapis.com` on `project-92b707ef-478d-4e01-ab0` and set an ADC quota
project. `gcloud` on this machine cannot do it: its user token is expired
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

Status: proposed, not applied. It is written out here rather than committed because it is a
change to what an agent *says*, and prose is the one part of this system where a mistake is
a quality problem rather than a security one -- so it is worth reading before it is pasted.

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
- **durable-worker** — `configure_logging()` in `main()`, and a `bind_scope` +
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
