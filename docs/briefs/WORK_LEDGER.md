# Work ledger: what is done, what is left, and who does it

Measured against the twenty-one step implementation sequence in `PROJECT_SPECIFICATION.md`
section 32. Evidence is a real test count or a file size, never a claim.

Note: the copy of the specification in `~/Downloads` is 120 lines behind and still
describes three capability registries and no section 2.5. **The copy in this repository is
canonical.**

## Done

| # | Step | Evidence |
| --- | --- | --- |
| 1 | Repository, lockfiles, CI, status table, threat model | CI bootstraps DB roles and runs the isolation suites; `docs/THREAT_MODEL.md` 309 lines |
| 2 | Domain types, integer money, JCS, golden vectors | `commerce-domain` 407 lines, canonical hash frozen with a pinned regression vector |
| 3 | Schemas, tenant/RLS, AgentPrincipal, roles, migrations | `platform-db` 1,485 lines, 3 migrations, 24 tables, forced row-level security |
| 4 | Transaction kernel and everything in it | `transaction-kernel` 13,527 lines, 131 exports |
| 5 | Concurrency and property tests | Single-winner admission proven with real contending sessions; loser gets `DUPLICATE_OPERATION` |
| 6 | Merchant simulator and deterministic services | `merchant-sim` 3,121 lines |

Total: **2,473 backend tests green, mypy strict clean across 63 source files.**

## In progress

| # | Step | State | Owner |
| --- | --- | --- | --- |
| 7 | Razorpay adapter, trusted surface, verification, webhook inbox, refunds, reconciliation | Adapter done (3,550 lines). **API and worker are 6 and 5 lines: they do not exist.** | **Claude, now** |
| 8 | Buyer storefront screens | Built and passing, but clones a real competitor and has a lost-update bug | **Gemini, now** |

## Not started

| # | Step | Owner and phase |
| --- | --- | --- |
| 9 | Commerce Assistant agents via ADK and Gemini 3.8 Flash | Claude, phase 2. 3,153 lines exist but do not import and have no tests |
| 10 | Realtime speech in and out, deterministic transactional speech | Claude, phase 2. 4,148 lines exist, 91 of 97 tests pass, gateway is a stub |
| 11 | Support: reconciliation, resolution, human-review queue, Support Agent | Claude, phase 2 |
| 12 | Merchant onboarding and immutable configuration versions | Claude backend, Gemini console UI |
| 13 | Merchant Copilot, Operations, Growth Engine | Gemini UI over Claude's metrics endpoints |
| 14 | UCP business profile and lifecycle | Claude, phase 2 |
| 15 | Full AP2 human-present cryptography | Claude, phase 2 |
| 16 | ACP-compatible endpoints | Claude, phase 2 |
| 17 | Protocol Inspector | Claude exposes the data, Gemini builds the view |
| 18 | GKE, Cloud SQL, Memorystore, ingress, secrets | Written but never run against a real project |
| 19 | Security, load, end-to-end and recovery tests | Claude backend, Gemini frontend and accessibility |
| 20 | Record the demonstration and freeze evidence | Owner, with Gemini on presentation assets |
| 21 | MCP, last, only if every gate is green | Claude, phase 2, optional |

## The split, stated plainly

**Claude, this stretch, and nothing else:** step 7. The HTTP API and the durable worker.
That is what turns a proven kernel into a demonstration somebody can click through. No
protocols, no voice, no agent layer until it works.

**Claude, phase 2 (Opus 5), in this order:** the agent layer (9), then support services
(11), then UCP (14), AP2 (15), ACP (16), voice (10), and MCP (21) only if everything else
is green.

**Gemini, now:** de-brand and repair the storefront (8), then grow the catalogue and
search (6).

**Gemini, next:** the merchant console (13), the onboarding UI (12), the Protocol
Inspector view (17), frontend end-to-end and accessibility (19), demo assets (20).

**Never Gemini:** anything under `transaction-kernel`, `platform-db`, `payment-adapters`,
`durable-work`, `commerce-api`, `durable-worker`, `commerce-domain`, or the four
money-bearing modules of `merchant-sim`. The reason is not seniority. It is that a change
to how money is represented, hashed or transitioned is invisible in review and fatal in
production, and this project's entire claim is that those paths are proven.

## Integration

Claude merges. When Gemini reports, Claude reads `GEMINI_REPORT.md` and
`REQUESTS_TO_CLAUDE.md`, actions the requests that fall in Claude-owned files, merges the
branch into `main`, runs the full gate on the merged result, and reconciles the frontend's
provisional response schemas against the API's real responses. That reconciliation is the
most likely source of a broken demonstration and nobody else can do it.
