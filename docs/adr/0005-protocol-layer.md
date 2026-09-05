# ADR 0005: The protocol layer — UCP, AP2, ACP and MCP

Status: accepted, 2026-09-05. Extends PROJECT_SPECIFICATION.md sections 13, 14, 15, 16, 17,
25.4, 28 and 29.5 with implementation decisions. Depends on ADR 0003 and violates none of
its fifteen decisions. Where this file and the specification differ in detail, this file is
what is built.

## Context

Six packages and a working HTTP service existed. Nothing spoke a protocol. The task was to
add UCP, AP2, ACP and MCP without letting any of them become a second way to move money.

Specification 13.1 states the constraint that shaped every decision below: the domain and
the kernel do not depend on UCP, AP2, ACP, MCP or a proprietary LLM payload. A protocol
adapter authenticates, validates, verifies, maps to a typed internal command, produces a
`VerifiedAuthorityProof` when authority is valid, calls the same deterministic kernel, and
maps the result back. If a protocol needs the kernel to change to accommodate it, the answer
is no. Nothing in this layer required a change to the kernel, to `platform-db`, or to any
migration.

## Decisions

### P1. One internal vocabulary, and two things it deliberately cannot say

Every adapter produces a `ProtocolIntent` and nothing else crosses into the service layer.
`IntentKind` is closed and shorter than any single protocol's surface, because the list of
things a buyer can actually want is shorter than the union of four protocols' operations.

Two absences are load-bearing. There is no `APPROVE` and no `REJECT`, so a protocol caller
cannot form a request that records consent. There is no `PAY`, `REFUND` or `CANCEL` — only
`PROPOSE_*` — so the type system says that a model asking for a refund produces a request
for a human decision. Both are absences rather than guards: a guard is one edit from being
removed, and a missing enum member is not.

`VerifiedAuthorityProof` is imported from `transaction_kernel.contracts` rather than
redefined. There is one description of what a verified mandate entitles somebody to, it
belongs to the kernel, and an adapter's job is to fill it in honestly or not at all.

### P2. Protocol evidence rides on the kernel's audit chain, not on new tables

Specification 25.4 names six tables — `signing_key_metadata`, `protocol_sessions`,
`protocol_messages`, `ap2_mandates`, `ap2_receipts`, `replay_guards` — and gives no columns
for any of them. None exists in any migration or ORM model; the six names appear exactly
once in the repository, in the specification itself.

**The audit chain is the chosen substrate, not a substitute for one.** This is worth stating
positively, because "we did not create the tables" describes the same decision in a way that
invites someone to finish the job later. Specification 13.3's actual requirement is that a
reviewer can reconstruct what an external party asked for and what the platform did about it,
and
`transaction_kernel.audit.append` already provides a gapless, hash-chained, tenant-scoped,
tamper-evident stream with a verifier already shipped at
`/v1/audit/streams/{type}/{id}/verify`. Building `protocol_messages` beside it would have
meant reimplementing tamper-evidence, arguing for a new grant set in `platform_db.roles`,
and getting a row-level-security policy right — all to duplicate a mechanism that already
works and is already proved.

So a protocol interaction opens an evidence stream under `aggregate_type =
"protocol_interaction"` and appends one row per stage of the 13.1 pipeline. `replay_guards`
is likewise unnecessary: a nonce store's one hard requirement is that two concurrent claims
cannot both win, which is a unique index, and `transaction_kernel.idempotency` already owns
a correct one where the loser blocks on the index rather than racing a read.

Six new tables would have had to earn all of that from scratch -- the tamper-evidence, the
row-level-security policy, the grant set -- and each one is then a table a reviewer has to be
convinced is correct. The consequence worth stating: **this layer required no schema change at
all**, and adds nothing to `_TENANT_TABLES` in the commerce-api conftest. Zero schema changes
is the stronger answer, and this decision was reviewed and agreed by the owner of
`platform-db` before it was recorded here. The trade-off is that querying
"all ACP traffic last hour" means scanning an audit stream rather than an indexed table. If
that ever matters, the right answer is an index over the audit chain rather than a second
source of truth, and `commerce_protocols.core.evidence` is the single module that changes.

### P3. Redaction happens when evidence is written, not when it is displayed

Specification 28 says the inspector never displays credentials, private keys, full payment
signatures or unnecessary PII. We enforce that at the point of writing: a secret-bearing
artifact enters the chain only as a `Fingerprint` — algorithm, length, SHA-256 digest and an
eight-character preview.

The reason is that the audit chain is immutable. A secret written into it could not be
redacted afterwards even if somebody wanted to, so the only safe place for the rule is
before the write. The inspector can then render evidence verbatim, because it cannot leak
what was never stored.

Request *bodies* are kept verbatim. A redacted ask is not evidence of anything, and
credentials travel in headers and signature material rather than in the ask.

### P4. An external protocol caller can never hold a consent capability

`PROTOCOL_CAPABILITIES` mirrors `commerce_api.deps.AGENT_CAPABILITIES` and excludes
`checkout.approve`, `checkout.reject`, `checkout.cancel` and `refund.request`. An external
AI buyer is an agent that arrived over a protocol; it is not entitled to more for having
travelled further.

`principal_for` **intersects** the tenant's grant with that ceiling rather than validating
against it. The failure modes differ: validation would refuse every request from a
misconfigured client until a human noticed, while intersection keeps the client working for
everything it should do and silently drops the one capability it should never have had.
`assert_never_consents` re-asserts the invariant at the point of use, which should be
unreachable and is checked anyway.

Protocol callers are audited as `ActorType.PROTOCOL`, not `AGENT`. A reviewer must be able
to tell an external buyer platform from this platform's own copilot, because the two have
very different blast radii and the audit row is the only place that difference is recorded.

### P5. UCP answers `requires_escalation`, and `complete_in_progress` is refused

Specification 14.3, implemented as written. This platform has negotiated no Razorpay UCP
Payment Action and Razorpay test mode has no headless charge path, so a Complete Checkout
call cannot be honoured by the platform alone.

`complete_in_progress` would be well-formed, would validate, and would be a lie with
consequences: a conforming client receiving it polls Get Checkout and does not prompt its
buyer, because the platform has just said no buyer input is needed. The purchase then hangs
until the reservation lapses. `requires_escalation` is not the lesser status; it is the only
one that describes what has to happen next.

`decide_completion` makes this structural. The four outcomes are separate types; `Escalation`
cannot be constructed without both a `continue_url` and a structured message; `InProgress`
cannot be constructed without naming which of 14.3's two legitimate grounds applies; and the
final branch is unconditional, so no unanticipated state combination produces a more
optimistic status.

We adopted 14.3's warning about `requires_buyer_review` as a runtime refusal.
`escalation_message` builds `type`, `code` and `severity` from separate arguments and rejects
a code that is actually a severity — the malformed message the specification calls out is
not constructible.

### P6. The trusted continuation gets three properties from three mechanisms

Unforgeable is an ES256 signature: a mintable `continue_url` would let an external party
route a buyer to a trusted approval for a checkout of the attacker's choosing. Short-lived is
an expiry judged against the database clock, with a ceiling a caller cannot raise. Single-use
cannot come from a signature at all, because verifying one changes nothing — it comes from
the kernel's idempotency index.

The token carries the checkout version and content hash, because 14.3 step 4 requires the
buyer to review the same version and hash. A token naming only the checkout id would let
merchant state move between escalation and consent, which is the exact hole the version/hash
discipline exists to close.

Consumption happens before the state comparison. A token presented against a superseded
version is spent anyway, because it has been used; allowing a retry would leave a live URL
with whoever triggered the mismatch.

### P7. AP2 is pinned at the commit, and its pins were allowed to win

Specification 15.1 pins AP2 to `b4587ac1d055888a73b4b21750973cffba961793`. The SDK declares
exact pins — `cryptography==46.0.5`, `jwcrypto==1.5.6`, `pydantic==2.12.5`, `sd-jwt==0.10.4`,
`pytest==9.0.2` — and letting them win downgraded `cryptography` 50.0.1 → 46.0.5, `pydantic`
2.13.5 → 2.12.5 and `pytest` 9.1.1 → 9.0.2 across the workspace.

That downgrade was measured rather than assumed. The suite sat at 3,327 passing with the
same three pre-existing failures either side of it. Had it broken anything, the decision
would have been reported rather than forced.

The SDK ships no `py.typed` and is a PEP 420 namespace package, so strict mypy cannot see
through it. It is trusted at exactly one boundary — a `[[tool.mypy.overrides]]` for `ap2.*`
and `jwcrypto.*` — and `commerce_protocols.ap2` wraps every AP2 call in a typed function of
its own, so nothing outside that package sees an untyped value.

### P8. We added the algorithm pin the SDK does not have

This is the most consequential decision in the layer.

`ap2.sdk.jwt_helper.create_jwt` hardcodes ES256 when signing, but `verify_jwt` calls
`jws.verify(key)` with no `alg`, so jwcrypto falls back to its fourteen-entry
`default_allowed_algs` and honours the algorithm named in the token's own protected header.
`ap2.sdk.sdjwt.sd_jwt.verify` has the same shape, passing `sign_alg=None`.

Specification 14.1 requires verification keys to be published as a JWK Set, so a JWS
algorithm-confusion forgery — a token whose header says `HS256`, signed with the public key's
bytes as the HMAC secret — needs no secret at all. `test_cp_ap2.TestAlgorithmPinning`
demonstrates both halves: the SDK's own `verify_jwt` accepts that forgery, and
`verify_compact` refuses it before a key is in scope. The characterisation test is
deliberate — if a future AP2 release fixes this, it fails and tells us the pin moved.

`verify_compact` refuses twice: an explicit check on the decoded header, and
`allowed_algs = ["ES256"]` on the `JWS` object, so bypassing one still meets the other.

### P9. The bridge signs bytes, because a signer must not have an opinion

Specification 15.4's eight steps are implemented literally. `bind_checkout` compares the
bytes the signer produced against the bytes it computed, and that check caught a real bug in
its own construction: on a checkout carrying a Devanagari merchant name the two differed by
28 bytes, because `json.dumps` escapes non-ASCII and RFC 8785 does not.

The fix changed `Signer.sign` to take `bytes` rather than a mapping. A signer handed a
mapping must serialize it, and its serialization will differ from its caller's. This is
precisely the failure 15.4 exists to prevent, and it would have surfaced at a peer as "the
merchant signature is invalid" and been hunted for in the wrong place.

The golden vector reproduces all six deterministic intermediates and verifies its stored
signature **without any private key**, which is both what 15.4 requires and the only check
that could work — ECDSA is randomised, so re-signing and comparing signatures was never
available. AP2's own `compute_sha256_b64url` is asserted against our transaction id, so the
two implementations are proved to agree rather than assumed to.

### P10. Human-present AP2 is not a chain, and two SDK behaviours are overridden

AP2's human-present flow issues two independent root SD-JWTs under the buyer's key. It is
**not** the `~~`-joined open/closed chain, so `CheckoutMandateChain.parse` and
`PaymentMandateChain.parse` — which demand exactly two payloads — cannot be used, and the
constraint machinery in `ap2.sdk.constraints` is human-not-present only.

Two hardening decisions the SDK leaves to its caller:

**Expiry is required.** `CheckoutMandate.exp` and `PaymentMandate.exp` are optional, and the
chain verifier enforces `exp` only when present, so a mandate issued without one never
expires. We refuse absence.

**`MandateClient.verify` is used rather than `SdJwtMandate.from_sd_jwt`.** They look
interchangeable; `from_sd_jwt` performs no `exp` or `iat` checking at all.

We also silence `ap2.sdk.mandate._log_event`, which appends the holder public key and the
full presentation token to a file inside the installed package. Specification 15.5 forbids
AP2 key material reaching logs, and there is no configuration switch for it.

### P11. We wrote our own receipt verification, and made premature issuance unconstructible

`ReceiptClient.verify_receipt` at the pin has three defects: it annotates its key parameter
as a `cryptography` key while passing it to a function requiring a `jwcrypto` JWK; it
defaults `has_reference_in_store_cb` to `None` and then calls it unconditionally; and it
checks only that a reference is *known*, never that it is the one being settled — so it would
verify a genuine receipt for somebody else's order. It also returns a dict on failure rather
than raising, which a caller can forget to inspect.

We use the SDK's models for the receipt shape, since that is what interoperability depends
on, and verify here — pinning the algorithm, binding the reference to the expected mandate,
and raising.

Specification 15.7's "no premature success receipt" is structural: `issue_payment_receipt`
requires a `CaptureProof`, and `capture_proof` refuses every payment state except `CAPTURED`
from `WEBHOOK` or `PROVIDER_FETCH` evidence. `AUTHORIZED` is refused as firmly as `FAILED` —
a hold is not a payment — and `BROWSER_CALLBACK` is refused per ADR 0003 D8.

### P12. ACP takes its audience from configuration, never from the request

Specification 16.3 implemented in full: HTTP message signature or API key, both compared with
`hmac.compare_digest`; the five-minute window through the shared `assert_fresh`; nonces
through the shared `claim_nonce`; body-size and content-type limits; per-client and
per-tenant token buckets; idempotency required on mutations.

The audience is supplied by the verifier from its own configuration and a caller cannot
influence it. Taking it from a header would reduce audience binding to "the caller agrees
with itself". An unknown client is compared against a decoy secret so that enumeration costs
the same time as a genuine miss.

A nonce is burned only by a request that **succeeded**. A refused request leaves no claim, so
its nonce stays usable — otherwise anyone could invalidate a legitimate caller's nonces by
replaying them into a failing endpoint.

Specification 16.2's boundary is carried in code rather than only in prose: the ACP pin's
`ClaimBoundary` is `COMPATIBLE_INTERFACE`, and its disclaimer travels with it into the
published matrix.

### P13. MCP: the dangerous tool does not exist

Specification 17.3 forbids a design, not a behaviour. The registry is a closed mapping of
exactly 17.2's tools. There is no generic `call_tool(name, args)` dispatching by string, no
`getattr` dispatch, no HTTP client, no `eval`. Cancellation and refund map to
`PROPOSE_CANCELLATION` and `PROPOSE_REFUND`, which by design name no amount — a proposal that
could name one is a refund tool wearing a hat.

The tenant is never taken from a tool argument (17.3), the allowlist is fixed at session
creation, and tokens are audience-bound with replay prevention through the shared core.

### P14. The protocol HTTP surface is read-only

Every route in `commerce_api.routers.protocols` is a GET, asserted over the app's own route
table rather than by exercising each one — an absence is what a behavioural test cannot
demonstrate. A protocol caller's money path runs through the same trusted approval and the
same kernel admission as every other surface.

The merchant and platform profiles publish **separate keys under separate ids**. Sharing one
would collapse "the merchant signed this checkout" and "the platform signed this receipt"
into a single claim and make 15.3's resolve-by-`kid` step vacuous. This was a real bug in the
first version of the router, found by fetching both documents and comparing them.

### P15. Every adapter was reviewed by somebody whose brief was to assume it was wrong

The ACP and MCP adapters were written first and then handed to independent reviewers told to
hunt for a path to money that bypasses the kernel, authority widening, a non-constant-time
comparison, a replay guard with a read-then-write race, a float touching money, and tests
that pass vacuously. That second pass is recorded here because it changed the code, and
because two of its findings are the kind that a passing suite actively conceals.

**A credential returned as `bytes` defeated the MCP result screen.** `results.screen()`
walked `bytes`, `bytearray` and `memoryview` through its `Sequence` branch, transcribing a
webhook secret into a list of integers that passed both the field-name screen and the value
screen. It reached the wire *and* the hash-chained audit row, which cannot be redacted. The
module's own docstring claimed the construction was impossible; for `str` it was.

**An ACP client's registered audience was never compared.** The signing string contains the
audience, so a signature minted for the sandbox does not verify at the live audience — which
is what made the gap invisible. A client registered *only* for the sandbox, signing correctly
over the live audience, was admitted there. My own probe drove all fifteen simulator
mutations and watched `WRONG_AUDIENCE` be refused; it was refused for the wrong reason.

Three further MCP findings were about authority rather than secrecy: `submit_approved` did
not check that the `AdmittedCall` was admitted under the session presenting it, so every gate
a call had passed was checked against one caller while the principal reaching admission came
from another; it performed no session-liveness check; and it wrote a caller-controlled
argument mapping into the audit chain *before* applying its own size cap, so a refused
request chose how much of an immutable stream it consumed.

Three more were about not crying wolf, which is a security property too. The credential screen
matched the bare substring `"basic "`, so "A basic cotton shirt" raised a fault on
`catalogue.search` — the most-called tool on the surface. A screen that fires on a grocery
catalogue is a screen an operator disables.

The general lesson, recorded because it will recur: **a green suite of adversarial tests is
evidence about the cases somebody thought of.** Every one of these defects sat behind tests
that passed, and three of them sat behind tests that appeared to cover exactly the property
that was broken.

### P16. MCP and ACP are libraries; their transports are not mounted

`GovernedToolServer` and `acp.admit` are complete and tested, and neither is wired into the
running service. Both need configuration this build unit does not own — an OAuth token
introspector and a resource indicator for MCP, a client registry and an audience for ACP —
and inventing either from environment variables would have produced a surface that looks
mounted and is not configured.

Two ports are declared for whoever mounts them, and their narrowness is deliberate:

    KernelAdmission.admit_approved(session, *, principal, checkout_id, version, content_hash)

has no `tenant_id`, no `amount`, no `capabilities` and no credential parameter, so there is
nothing a model could cause to be passed. Widening that signature would undo the property
it exists to prove. `TokenIntrospector.introspect` must refuse without distinguishing why.

## What we deliberately did not do

**We did not create the six tables of specification 25.4.** See P2. The layer is complete
without them and the reasoning is recorded in `docs/briefs/REQUESTS_TO_CLAUDE.md`.

**We did not implement AP2's human-not-present chain.** Specification 15 asks for the
selected human-present flow. The open/closed mandate chain, delegated constraints and
`PaymentReference` correlation are real AP2 features and are out of P0 scope; claiming them
untested would be exactly the kind of statement 13.3 forbids.

**We did not implement a negotiated Razorpay UCP Payment Action.** That is what would make
`complete_in_progress` correct, and negotiating one is not a code change. See P5.

**We did not claim a KMS-backed AP2 path.** Specification 15.5 permits Cloud KMS only after
its DER signature is converted to JWS raw format and passes the same golden vectors. The
`Signer` interface is the seam that swap would happen at; until the vectors pass under KMS,
the honest description is in-process signing.

**We did not wire keys through Secret Manager.** That needs `commerce_api.settings`, owned by
another build unit. The router reads `UCP_MERCHANT_SIGNING_JWK` and
`UCP_PLATFORM_SIGNING_JWK` and otherwise mints ephemeral keys, publishing `ephemeral_keys:
true` so a counterparty is not misled. Written up as an OPEN request.

**We did not implement x402.** Specification 13.2 places it outside P0.

## Assumptions taken where the specification was ambiguous

Following the instruction to give the external party less authority where a reading is
unclear:

1. **An absent protocol version is refused, not defaulted.** A caller that names no contract
   has agreed to none, and defaulting would make a future version change a silent behaviour
   change for somebody's integration.
2. **A replayed nonce is refused, never answered with a stored response.** It carries
   `POLICY_EXCEPTION`, not `DUPLICATE_OPERATION`, because the latter is a code a caller may
   present to a buyer as a completed money action and nothing completed.
3. **A duplicate SKU across two UCP line items is refused, not summed.** Guessing would be
   deciding on a buyer's behalf how much of something they meant to buy.
4. **A total is read from the typed `total` component, never summed from the others.**
   Summing would substitute our arithmetic for the merchant's.
5. **A non-integer amount is refused, never coerced.** `bool` is excluded by name, since it
   subclasses `int` and JSON `true` deserialises to `True`.
6. **A protocol caller's `buyer_ref` comes from the authenticated session, never a body.**
   17.3 forbids taking the tenant from model arguments; the same reasoning covers the field
   that decides whose checkout a caller may read.
7. **A cross-tenant continuation is refused before its nonce is claimed**, so it cannot burn
   a nonce in a tenant it does not belong to.

## Consequences

Four protocols, no kernel change, no schema change, and one HTTP surface that cannot move
money. The layer's own suite is 312 tests; the protocol router adds 14 more.

The property that has to survive future edits is P1's two absences plus P4's ceiling. If a
later change adds an `APPROVE` intent or lets a tenant grant `checkout.approve` to a protocol
client, every other guarantee here becomes decorative — which is why all three are asserted
directly over the enum and the frozensets rather than inferred from behaviour.
