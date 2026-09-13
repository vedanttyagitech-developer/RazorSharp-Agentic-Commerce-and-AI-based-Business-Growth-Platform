# Reserve simulator: HSM signing and independently provisioned trust

This deployment uses Cloud Run and Cloud KMS HSM, with no GKE dependency. It is a
simulator issuer, not evidence of bank authorization, bank funds, or independent
buyer consent. HSM custody prevents exporting the private key; it does not make an
incorrect authorization request legitimate.

## Boundaries

The dedicated API identity invokes the IAM-protected signer. Only the signer
service account receives `roles/cloudkms.signerVerifier` on the specific key.
The build identity can publish images but cannot sign authorizations. The signer
runtime contains no commerce application or database packages. Its requests have
fixed claims, operator-enrolled buyers, merchant scopes, spending bounds and TTLs.
Caller email and immutable Google subject must both match the operator configuration.

The current demo API and worker share a GCE identity. That identity must NEVER receive
signer invocation or caller impersonation permissions. Enabling remote mode on this
VM without moving the API to a dedicated workload would break the intended boundary.
Use a dedicated Cloud Run API service or dedicated GCE API instance; keep the worker
on an identity without signing or caller-token permissions. This requires a separate
network/database and API routing rollout, not merely an environment-variable change.

The remote adapter rejects a configured local signing key, unknown modes, altered
returned bounds and failed signatures. There is no local fallback or automatic retry
on signer failure. It refuses verified-consent claims: the isolated issuer currently
supports v1 simulator authorizations only. Verified-consent v2 needs an independent
consent-validation flow before a live cutover can preserve that feature.

## Trust enrollment

`/.well-known/reserve/jwks` distributes public keys. It does not establish trust.
Obtain the public key directly through the authenticated KMS administrative channel,
verify its immutable key version and HSM protection, and approve its RFC 7638 SHA-256
JWK thumbprint separately from the application's endpoint. Pin issuer, audience,
endpoint, key ID and fingerprint with `scripts/provision_reserve_trust.py`.

A verifier must set `RESERVE_TRUST_CONFIG_PATH` to an operator-owned, read-only mounted
configuration; the application must not be able to replace the file or its parent
directory. `RESERVE_SIGNER_MODE=remote` requires that configuration. The JWKS ring is
public configuration, never a private key. Unknown or revoked keys, key substitution,
invalid issuer/audience and expired trust all fail closed. Maximum trust validity is
24 hours, with one hour the provisioning script's default. Arrange an operator-owned
refresh before expiry; the application must not self-enroll or self-renew trust.

`scripts/fetch_reserve_public_keys.py` only accepts keys matching existing pins. It
does not add pins. `scripts/verify_reserve_authorization.py --trusted-jwks ...
--trust-config ...` verifies offline using the same independent pins. The compatibility
mode without `--trust-config` trusts the caller-supplied key set; it does not claim
independent fingerprint enrollment. Offline verification does not establish current
capacity or authority revocation state in the database.

## Rotation and emergency withdrawal

1. Create a new HSM ES256 key version and obtain its public key via the administrative
   channel. Use a new `kid`; never reuse a revoked ID or substitute material under it.
2. Provision the new fingerprint to every verifier alongside the existing key. Confirm
   propagation before changing `signing_key_version`, `signing_kid` and `key_fingerprint`
   in the signer deployment. Use an immutable image digest and review the Terraform plan.
3. Keep the old public key pinned while valid old authorizations must still verify.
   Disabling the old private key version stops new signing; it does NOT invalidate
   signatures already issued with it.
4. For compromise, mark the old pin revoked and distribute the change immediately to
   every verifier, then disable the compromised KMS version. Stale trust expires within
   its configured validity, not instantly. No claim of instantaneous global revocation
   is made. Apply database authority revocation where appropriate as well.
5. Preserve reconciliation of already-started/unknown payments. Never release a held
   amount or repeat a provider call merely because signing/trust is unavailable.

Key destruction is deliberately protected in Terraform. Do not destroy an old version
as a substitute for revocation. Independently test restore/redeployment and retain
public keys needed for historical audit.

## Administration and operations

KMS DATA_READ and DATA_WRITE audit logging is enabled by this module. Review
AsymmetricSign activity, denied calls, key state changes, IAM changes and signer
revisions. Configure notification destinations and volume baselines before calling
monitoring production-ready. Never log bearer tokens, private keys or full buyer data.

Separate key administration, trust approval and application deployment in production,
using distinct human identities, MFA and independently reviewed changes. Project owners
can still change IAM; application-level pinning cannot defeat a malicious operator who
controls both the deployment and the verifier configuration. This module does not
invent a second approver or claim organizational separation has already happened.

Deploy from `infra/reserve-signer` using a saved reviewed plan and the complete
operator tfvars. Keep state, plans and tfvars private and backed up. Do not run the
legacy `infra/terraform` stack for this setup: it provisions unrelated GKE resources.
The standalone module creates the signer identity, caller identity, build identity,
HSM key, source bucket, image repository and optional authenticated Cloud Run service.

Smoke tests use a synthetic buyer and merchant with short-lived operator bounds;
remove those scopes and temporary impersonation access afterward. Do not enroll
all real buyers to make a demo pass. Real buyer enrollment and consent remain explicit
issuer decisions.
