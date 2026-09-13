# Reserve review status — 2026-09-13

This table supersedes the pasted historical findings. It describes repository behavior;
it is not a claim that the existing GCP storefront was redeployed or is NPCI certified.

| Item | Current status |
| --- | --- |
| 1. Trust distribution | Verifier-owned issuer/key fingerprint pins, expired/revoked pin rejection and isolated HSM signer exist. Shared storefront cutover and independent operational pin distribution still require the deployment steps in RESERVE_HSM_VERIFICATION.md. Local demo signing is explicitly not independent issuer assurance. |
| 2. Exact proof denial | AUTHORITY_PROOF_INVALID assertion and no-consumption tampering regressions exist in test_capi_reserve_proofs.py. |
| 3. Metrics | Admission/grant counters were already commit-aware. Added committed webhook delivery/apply-lag, reconciliation outcomes/findings, worker backlog gauges and worker /metrics. Scrape each process; API metrics are not an aggregate of worker metrics. Cloud scrape/alert rollout is pending. |
| 4. Growth/cost evidence | Added opt-in local 1/4/8-concurrency benchmark with measured results and limitations. This does not supply a real commercial A/B growth study or production GCP cost estimate. |
| 5. Single active permission | Database uniqueness and concurrent creation regression exist. Explicitly revoke an old expired permission before replacement; unresolved allocations must not be silently discarded. |
| 6. Authority RECONCILING | Reserved compatibility state, not the normal runtime payment-reconciliation state. Unknown debit outcomes hold allocation on payment attempts. Do not advertise automatic authority-state transitions. |
| 7. Expiry | New public demo permissions are finite, default 30 days for older clients; UI chooses an end date within 90 days. Exact expiry is signed and enforced by the existing DB-clock authority check. Historical authorizations are not silently rewritten or re-signed. |
| 8. V2 consent | Still a separate production feature. Current deliberately frictionless demo uses authenticated browser confirmation and V1 simulator proofs. Retired passkey endpoints stay retired. A V2 label/digest alone cannot establish independently verified buyer consent. Independent issuer enrollment/consent validation and explicit reauthorization migration remain required; never auto-upgrade V1 proof evidence. |
| 9. INR | Intentional current scope. No claim of multi-currency Reserve support. |
| 10. Runbook | docs/DEMO.md exists. |
| 11. Transaction composition | API composes authority/admission/outbox work transactionally; low-level kernel primitives are not standalone public workflow endpoints. |
| 12. Lock ordering | Shared-authority admission/worker/settlement regression and merchant serialization exist. A static LOCK_ORDER tuple alone is not a proof; retain the concurrent tests. |

## Voice integration fixes

Backend pending/unknown/reconciling/escalated outcomes now override browser manual/success
stages in spoken checkout guidance. A pending result must not tell a buyer to pay again.
Confirmed failure remains distinguishable from an unknown result. Custom local API/voice
ports now align ticket routing, public WebSocket URL and the browser origin allowlist.

## Observability operations

Scrape the API's authenticated /v1/ops/metrics and the worker's internal :8001/metrics
separately. Keep the worker port private. Gauges are refreshed during housekeeping and
reset to zero for drained groups. Reconciliation counters use committed recorded runs,
not HTTP 200 as a synonym for success. Unverified webhook bodies are rejected before
resolving a tenant; these new tenant counters cover authenticated deliveries only.
