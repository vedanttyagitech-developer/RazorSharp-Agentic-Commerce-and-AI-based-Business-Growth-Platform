# HSM signer verification — 2026-09-12

## Deployed and verified

- Project: `project-b9d1f592-6366-4856-884`; region: `asia-south1`.
- Cloud Run service: `reserve-isolated-signer` (IAM authentication required).
- HSM key: `reserve-simulator-trust/authority-es256`, immutable version `1`.
- GCP reported `ENABLED`, `EC_SIGN_P256_SHA256`, protection level `HSM`.
- Public JWK thumbprint obtained through authenticated KMS administration:
  `hTvCpwLoAV4B5f5CxkzTZnFCZLzZeTRhlkQ5G0jGB80`; kid `reserve-hsm-v1`.
- Image digest:
  `sha256:573e4b519a043b68a78349cb044a0c6f046ffb73688bdc2cb7129d9eb2730555`.
- Only `reserve-isolated-signer` has a resource-level signing grant on this key.
  Only `reserve-api-caller` has a resource-level Cloud Run invoker grant.
  This does not deny the administrative powers of project owners.
- KMS DATA_READ/DATA_WRITE audit logging configured. An `AsymmetricSign` audit event
  at `2026-09-12T14:16:49.126355885Z` identifies the isolated signer account and version 1.

## Live tests

| Test | Observed result |
| --- | --- |
| Unauthenticated issuance | HTTP 403 |
| Unenrolled buyer | HTTP 403 |
| Wrong merchant | HTTP 403 |
| Capacity beyond operator bound | HTTP 403 |
| Caller-supplied arbitrary issuer claim | HTTP 422 |
| Enrolled synthetic buyer, finite expiry, bounded amount | HTTP 200 |
| Returned HSM signature, offline verifier with independently acquired public-key pin | VALID |
| Same artifact with its pin revoked | REJECTED |
| Actual shared GCE API/worker identity calling the authorization route | HTTP 403 |

No payment was executed. Test scope used synthetic UUIDs and a test-only buyer.
The operator's temporary ID-token-only permission was removed after testing. The
synthetic scope was removed and the final deployed signer has zero enrolled scopes
and an expired default authorization policy. Live configuration and IAM were checked
after cleanup. A later deployment must explicitly provision real authorized scopes.

The initial shared-VM health-route probe returned 404; the actual authorization-route
probe returned 403. Only the latter is counted as the identity-denial test.

## Local regressions

- Kernel: **1,914 passed**.
- Commerce API: **783 passed**.
- Isolated signer and trust package: **18 passed**.
- Mypy: six relevant source files passed.
- Ruff on new signer/trust code and utility/test files passed.
- Terraform validate and git diff whitespace check passed.
- Signer Docker image built successfully in Cloud Build; provider checksums locked
  for darwin_arm64 and linux_amd64.

Unit tests include substituted matching key/artifact rejection, revoked/expired pins,
unapproved rotation, corrupted signatures and refusal of software-protected keys.
An actual second HSM key version rotation was not performed; rotation is covered by
pin-validation tests and the operational procedure, not a claimed live rotation drill.

## Remaining before storefront cutover

The existing storefront remains on its previous signing setup. The new Cloud Run
signer is deployed but intentionally not wired into that shared VM.

1. Isolate the API runtime identity from the worker, and validate database access,
   internal routing, voice integration and rollback on the dedicated runtime.
2. Implement independent verified-consent v2 validation. The current remote adapter
   deliberately refuses v2 consent claims; switching the existing demo blindly would
   break that path. v1 simulator signing is what was live-tested here.
3. Provision operator-controlled read-only trust configuration to API and worker,
   including refresh, rotation and emergency revocation distribution. Do not make
   the application its own trust approver.
4. Enroll approved real scopes, separate human key/trust/deployment responsibilities,
   configure alert notification destinations and exercise operational recovery.

No GKE resources were used or created. No existing storefront resources or data were
deleted. See [the trust runbook](RESERVE_TRUST.md) for the security boundaries and
rotation procedure. This evidence is for a simulator issuer, not bank authorization.
