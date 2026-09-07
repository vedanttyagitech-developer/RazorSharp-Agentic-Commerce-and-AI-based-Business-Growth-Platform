# Deploying the demo to Google Cloud

From an empty project to a working `https://<host>/` in about an hour, most of it waiting
for Cloud SQL and GKE. Everything is reproducible: Terraform for the platform
(`infra/terraform`), Kustomize for the workloads (`infra/kubernetes`), Dockerfiles for the
images (`infra/docker`). Nothing in this document prints a secret value, and nothing in the
repository contains one.

Topology (spec 23): Global HTTPS load balancer → GKE Autopilot (`commerce` namespace:
`buyer-web`, `merchant-console`, `commerce-api`, `action-executor`) → Cloud SQL PostgreSQL 16
(private IP, IAM auth through proxy sidecars), Memorystore Redis 7.2, Secret Manager,
Gemini, Razorpay test mode. One replica of the API and of the worker (ADR 0003 D14).

**Two web surfaces, two hosts, two credentials.** The buyer storefront (port 3000) and the
operator console (port 3001) are separate Next.js applications with separate images,
service accounts and Secret Manager grants. Each has a server-side proxy route holding a
credential the browser never receives, and the console additionally holds the scenario key,
which lets it mint OPERATOR sessions. They are served from different host names — the
console at `console.$HOST` — because a path prefix would put them on one origin, and the
console is the surface where a session can revive an outbox command or throw the Safe Mode
switch. `buyer-web` is deliberately *not* granted the scenario key: a storefront that could
read it could mint itself an operator session and read every buyer's orders.

## 0. Prerequisites

- `gcloud` (with `gke-gcloud-auth-plugin`), `kubectl` ≥ 1.29, `terraform` ≥ 1.9, `kubeconform` ≥ 0.8.0.
  Docker is optional: Cloud Build builds the images.
- A domain you control for the demo host (an `A` record you can create). Google-managed
  certificates need a public name.
- A Razorpay account in test mode (`rzp_test_` keys) and a Gemini API key or Vertex AI
  access in the project.
- Both `apps/buyer-web/next.config.ts` and `apps/merchant-console/next.config.ts` contain
  `output: "standalone"` (see `infra/docker/README.md`). `validate_infra.sh` checks this.
- Run the local validation before touching any cloud resources. It renders and
  strictly schema-checks every overlay, cross-checks the manifests against the applications
  that exist, scans for secrets in anything that ships, builds all four images and smoke-tests
  them:
  ```sh
  ./scripts/validate_infra.sh          # NO_DOCKER=1 to skip the image builds
  ```
  It reports PASSED / FAILED / SKIPPED counts and never reports a skipped check as a pass.
  What it cannot check is listed at the end of its own output, and none of it is a cloud
  operation: **validating a manifest is not deploying it.**

### Local Non-Mutating Validation Commands vs Mutating Cloud Commands

To prevent accidental resource creation or state mutations, distinguish these two command categories:

| Category | Commands | Effect / Permissibility |
| --- | --- | --- |
| **Local Non-Mutating Validation** | `./scripts/validate_infra.sh`<br>`terraform -chdir=infra/terraform fmt -check`<br>`terraform -chdir=infra/terraform init -backend=false`<br>`terraform -chdir=infra/terraform validate`<br>`kubectl kustomize infra/kubernetes/overlays/<env>`<br>`kubeconform ...`<br>`docker build ...` (local build) | Safe to run offline or in CI; connects to no remote state backend; creates no cloud resources; incurs no billing; performs no network mutations. |
| **Mutating Cloud / Cluster Commands** | `gcloud projects create ...`<br>`gcloud billing projects link ...`<br>`terraform apply` / `terraform destroy`<br>`gcloud secrets versions add ...`<br>`gcloud sql users set-password ...`<br>`gcloud builds submit ...` / `docker push ...`<br>`kubectl apply -k ...` / `kubectl delete ...` | **Mutating**: Allocates billable GCP infrastructure, modifies GKE cluster state, injects secrets, or runs migrations. Only run during actual authorized deployment. |


```sh
export PROJECT_ID=my-commerce-demo   # choose
export REGION=asia-south1
export HOST=shop.example.org         # the demo host name
```

## 1. Project and billing

```sh
gcloud projects create "$PROJECT_ID" --name="Governed Agentic Commerce demo"
gcloud billing accounts list                                   # pick ACCOUNT_ID
gcloud billing projects link "$PROJECT_ID" --billing-account=ACCOUNT_ID
gcloud config set project "$PROJECT_ID"
gcloud services enable serviceusage.googleapis.com cloudresourcemanager.googleapis.com
gcloud auth application-default login                          # Terraform credentials
```

## 2. Platform: Terraform (two applies)

```sh
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars    # set project_id (and your CIDR if you want)
terraform init
terraform apply                                 # ~15–25 min: APIs, VPC, GKE, Cloud SQL, Redis
```

FQDN network policies (the `api.razorpay.com` / `*.googleapis.com` allowlists) can only be
enabled on an existing Autopilot cluster, so:

```sh
sed -i '' 's/enable_fqdn_network_policy = false/enable_fqdn_network_policy = true/' terraform.tfvars
terraform apply                                 # cluster update, a few minutes
```

Keep these outputs handy:

```sh
terraform output                                # instance_connection_name, redis_host, ingress_ip, ...
```

Create both DNS records now so the certificate can be issued while you do the rest. The
managed certificate names two domains and will stay in `Provisioning` until *both* resolve:

```
A  $HOST          →  $(terraform output -raw ingress_ip)
A  console.$HOST  →  $(terraform output -raw ingress_ip)
```

If you do not want the console publicly reachable, leave the second record out, delete the
console host rule from the overlay and drop the domain from the `ManagedCertificate` — then
reach it with `kubectl -n commerce port-forward svc/merchant-console 3001:3001`. That is the
better posture for anything but a recorded demo.

## 3. Secret values (never echoed)

Terraform created ten empty secrets. Add one version to each; the helper reads the value
without echo and pipes it straight into `gcloud`. The `db-url-*` values contain no password
(IAM authentication through the proxy sidecars) but are stored as secrets by policy.

Which workload may read which is declared once, in `infra/terraform/locals.tf`, and is
narrower than "every pod gets every secret" on purpose. `scenario-key` goes to
`commerce-api` (which verifies it) and `merchant-console` (which presents it) and to
nothing else; each web cookie secret goes to exactly one workload, so a storefront pod
cannot forge an operator cookie.

```sh
add_secret() {
  printf 'Value for %s: ' "$1"; IFS= read -rs value; echo
  printf '%s' "$value" | gcloud secrets versions add "$1" --data-file=-
  unset value
}
add_secret razorpay-key-id          # rzp_test_...
add_secret razorpay-key-secret
add_secret razorpay-webhook-secret  # you choose it; the same value goes into the Razorpay dashboard (step 9)
add_secret gemini-api-key

# The three below are values you choose. Generate them rather than typing something
# memorable: the scenario key gates the scenario controller and operator-session minting,
# and each cookie secret signs a session cookie.
for name in scenario-key web-session-cookie-secret console-cookie-secret; do
  openssl rand -hex 32 | tr -d '\n' | gcloud secrets versions add "$name" --data-file=-
done

# The two protocol signing keys. These are the keys the UCP profiles publish and that
# every piece of protocol evidence is verified against, so they are generated ONCE and
# then left alone -- see the warning below.
for role in merchant platform; do
  uv run --no-sync python -c "
import json
from cryptography.hazmat.primitives.asymmetric import ec
from jwcrypto.jwk import JWK
material = json.loads(JWK.from_pyca(ec.generate_private_key(ec.SECP256R1())).export())
material['kid'] = '$role-key-1'
print(json.dumps(material, sort_keys=True))
" | tr -d '\n' | gcloud secrets versions add "ucp-$role-signing-jwk" --data-file=-
done

printf '%s' "postgresql+psycopg://db-commerce-app%40$PROJECT_ID.iam@127.0.0.1:5432/commerce"    | gcloud secrets versions add db-url-app    --data-file=-
printf '%s' "postgresql+psycopg://db-commerce-kernel%40$PROJECT_ID.iam@127.0.0.1:5433/commerce" | gcloud secrets versions add db-url-kernel --data-file=-
printf '%s' "postgresql+psycopg://db-commerce-worker%40$PROJECT_ID.iam@127.0.0.1:5432/commerce" | gcloud secrets versions add db-url-worker --data-file=-
```

`%40` is `@` inside the user name (`db-commerce-app@PROJECT_ID.iam` is the Cloud SQL IAM
user). Port 5432 is a process's primary role proxy, 5433 the kernel-role proxy.

### The two signing keys are durable state, not configuration

`ucp-merchant-signing-jwk` and `ucp-platform-signing-jwk` are the one pair of values here
that **must not be regenerated on a redeploy**. Every signed protocol artifact this
platform has ever produced is verified against them, so replacing a key retroactively
invalidates evidence that was valid when it was written — the Protocol Inspector's claim
is that an interaction can be reconstructed *and verified* afterwards, and afterwards has
to mean after a restart.

Three consequences worth knowing before you deploy:

* **The two keys must carry different `kid` values.** The process refuses to start if they
  match, because "the merchant authorised this checkout" and "the platform issued this
  receipt" stop being distinguishable claims the moment one key can produce both.
* **They are configured together or not at all.** With neither set, the well-known
  profiles answer `404` and `GET /v1/protocols` reports
  `signing_keys_configured: false`. That is the supported degraded mode: a deployment
  that signs nothing has no profile to publish. There is deliberately **no generated
  fallback** — an earlier build minted a key when none was configured, and because it
  reused a fixed `kid` with fresh key material on each start, evidence signed before a
  restart was refused afterwards as `signature_did_not_verify`, which is exactly the
  refusal a forgery produces.
* **To rotate, add before you remove.** `commerce_protocols.ap2.signing.KeyRing` resolves
  by `kid` and is a mapping precisely so a retired key can keep verifying the evidence it
  signed. A key leaves the ring only once nothing verifies against it any more.

## 4. Database bootstrap (one time)

The built-in `postgres` user has no password yet; set one interactively and use Cloud SQL
Studio (console → SQL → instance → Cloud SQL Studio; user `postgres`, database `commerce`):

```sh
gcloud sql users set-password postgres --instance="commerce-demo-pg-01" --prompt-for-password
```

Run `infra/sql/01-grant-migration-identity.sql` with `PROJECT_ID` replaced. It gives the
migration Job's identity `cloudsqlsuperuser` (needed to create the NOLOGIN group roles).

## 5. Images

Four images: `commerce-api`, `action-executor`, `buyer-web`, `merchant-console`.

```sh
cd "$(git rev-parse --show-toplevel)"
gcloud builds submit --config infra/docker/cloudbuild.yaml \
  --substitutions=_TAG=demo,_TENANT_SLUG=demo,_REGISTRY=$REGION-docker.pkg.dev/$PROJECT_ID/commerce .
```

`_TENANT_SLUG` is baked into both web bundles, so pointing a deployment at a different demo
tenant needs a rebuild, not only a ConfigMap edit. No secret is ever passed as a build
argument: `docker history` prints build arguments back out of a finished image.

(Local alternative and the required root `.dockerignore`: `infra/docker/README.md`.)

## 6. Cluster access and overlay values

```sh
gcloud container clusters get-credentials commerce-demo --region "$REGION"
REDIS_HOST=$(cd infra/terraform && terraform output -raw redis_host)
grep -rl 'PROJECT_ID\|REDIS_HOST\|commerce.example.com' infra/kubernetes/overlays/demo \
  | xargs sed -i '' -e "s/PROJECT_ID/$PROJECT_ID/g" -e "s/REDIS_HOST/$REDIS_HOST/g" -e "s/commerce.example.com/$HOST/g"
kubectl kustomize infra/kubernetes/overlays/demo | grep -c 'SET-BY-OVERLAY\|PROJECT_ID'   # 0
```

GNU sed: `sed -i -e ...` without `''`. Check `INSTANCE_CONNECTION_NAME` in
`overlays/demo/platform/platform-config.yaml` equals `terraform output instance_connection_name`.

## 7. Migrations (before every rollout)

```sh
kubectl delete job -n commerce db-migrate --ignore-not-found
kubectl apply -k infra/kubernetes/overlays/demo/db-migrate
kubectl wait -n commerce --for=condition=complete job/db-migrate --timeout=15m
kubectl logs -n commerce job/db-migrate -c migrate
```

After the **first** migration, run `infra/sql/02-grant-app-identities.sql` in Cloud SQL
Studio (replace `PROJECT_ID`): it makes each database identity a member of its PostgreSQL
role. The final `SELECT` must show `rolsuper = false` and `rolbypassrls = false` for all three.

## 8. Rollout

```sh
kubectl apply -k infra/kubernetes/overlays/demo
kubectl -n commerce rollout status \
  deployment/commerce-api deployment/action-executor \
  deployment/buyer-web deployment/merchant-console
kubectl -n commerce get pods,svc,ingress
kubectl -n commerce get managedcertificate commerce   # Provisioning -> Active (up to ~60 min after both DNS names resolve)
```

## 9. Razorpay dashboard webhook

Dashboard → Settings → Webhooks → Add:

- URL: `https://$HOST/webhooks/razorpay/<tenant_slug>` (the slug of the demo tenant the
  API creates, e.g. `zepto-demo`; ADR 0003 D7).
- Secret: the exact value you stored in `razorpay-webhook-secret`.
- Active events: `payment.authorized`, `payment.captured`, `payment.failed`,
  `order.paid`, `refund.processed`, `refund.failed`.

The receiver verifies the HMAC over the raw body (256 KiB cap) before parsing anything.

## 10. Verify

```sh
curl -sS "https://$HOST/healthz"
curl -sS "https://$HOST/v1/config" | jq .      # redacted runtime facts: profile, degraded[], safe_mode
curl -sSI "http://$HOST/" | head -1             # 301 to https
open "https://$HOST/"
open "https://console.$HOST/"
```

`/v1/config` is the deployment's own honesty check. `degraded` must be `[]` and
`database.reachable` must be `true`; if the database is unreachable the endpoint still
answers 200 and says so, which is the behaviour
`packages/commerce-api/tests/test_fs_database_unavailable.py` proves. It reports
`scenario_routes_enabled`, which is `false` if the API did not receive `scenario-key` — in
which case the console's reads will 404 rather than 401, because the routes genuinely do not
exist without it.

Confirm the two credentials stayed server-side. Neither value may appear in anything the
browser receives:

```sh
curl -s "https://console.$HOST/" | grep -c "$(gcloud secrets versions access latest --secret=scenario-key)"   # 0
curl -s "https://$HOST/"          | grep -ci 'rzp_test_.*secret'                                              # 0
```

Executor: `kubectl -n commerce logs deploy/action-executor -c executor --tail=50`. Proxies:
`-c cloud-sql-proxy-app`, `-c cloud-sql-proxy-kernel`, `-c cloud-sql-proxy-worker`. Both web
pods log which secret *names* they loaded from the CSI mount on startup and never the
values:

```sh
kubectl -n commerce logs deploy/merchant-console -c web | head -1
# entrypoint: loaded 2 secret(s) from files: OPERATOR_COOKIE_SECRET, SCENARIO_KEY
```

## Redeploying a change

1. Build with a new `_TAG` (step 5).
2. Set `newTag` in `infra/kubernetes/overlays/demo/images/kustomization.yaml`.
3. Steps 7 and 8. The API and the worker use `Recreate`, so expect a few seconds of API
   downtime per rollout; both web front ends roll without downtime.

## Deployment Ordering: Build, Migration and Rollout

Strict ordering is required to ensure database schemas and credentials exist before workloads start:

1. **Packaging & Image Build**:
   Build the four container images (`commerce-api`, `action-executor`, `buyer-web`,
   `merchant-console`) with matching tags (e.g. `_TAG=demo`).
2. **Platform & Networking Infrastructure**:
   Execute Terraform applies (cluster, VPC, Cloud SQL, Memorystore, Artifact Registry, IAM, Secret Manager).
3. **Secret Values Population**:
   Add versions for all 10 Secret Manager secrets before pod creation so CSI driver mounts do not hang.
4. **Pre-Migration Database Bootstrap**:
   Run `infra/sql/01-grant-migration-identity.sql` in Cloud SQL Studio to grant `cloudsqlsuperuser` to `db-migration@PROJECT_ID.iam` so it can create the NOLOGIN PostgreSQL group roles.
5. **Database Migration Job**:
   Apply `infra/kubernetes/overlays/<env>/db-migrate`. Wait for Job completion (`kubectl wait --for=condition=complete job/db-migrate --timeout=15m`).
6. **Post-Migration Role Grants**:
   Run `infra/sql/02-grant-app-identities.sql` in Cloud SQL Studio to grant `commerce_app`, `commerce_kernel`, and `commerce_worker` roles to the IAM identities.
7. **Workload Rollout**:
   Apply `infra/kubernetes/overlays/<env>` to roll out `commerce-api`, `action-executor`,
   `buyer-web` and `merchant-console`.

## What has actually been verified, and what has not

This table is the honest boundary. It is easy to write a deployment document that reads as
though it were deployed; the distinction below is the point.

| Claim | Status | How it was checked |
| --- | --- | --- |
| All four images build from the repository root | **Verified** | `docker build` for each of `commerce-api`, `action-executor`, `buyer-web`, `merchant-console`; `scripts/validate_infra.sh` step 7 |
| The Python images can start | **Verified** | `python -c "import commerce_api.app"` and `import action_executor.main` inside the built images |
| Both web images serve HTTP 200 | **Verified** | container run, `GET /` |
| The Secret Manager file mount reaches the process environment | **Verified locally** | a directory of files mounted at `/var/run/secrets/app`; the shim logs the names it loaded and the value does not appear in the served HTML. The *CSI driver* itself is not exercised locally — only the file contract it produces |
| Kustomize renders and passes strict schema validation | **Verified** | `kubectl kustomize` + `kubeconform -strict` with **no** `-ignore-missing-schemas`; 38 resources, 0 skipped, for both overlays |
| Terraform configuration is valid | **Verified offline** | `terraform validate` with `-backend=false`; no plan against a real project, so this proves syntax and provider schema, not that an apply would succeed |
| The manifests describe the applications that exist | **Verified** | `validate_infra.sh` step 5 derives each app's required environment from `process.env` reads in its own source and asserts a manifest or secret mount supplies it |
| No secret ships in a manifest, an image layer or a browser bundle | **Verified** | `validate_infra.sh` step 6, with negative controls: injecting an inline `SCENARIO_KEY` value into a manifest makes the check fail |
| **A GKE cluster is running this** | **NOT DONE** | No `terraform apply`, no `gcloud builds submit`, no `kubectl apply` has been performed from this repository. There is no cluster |
| Cloud SQL, Secret Manager and Workload Identity work end to end | **NOT DONE** | These need a project. The manifests declare them correctly; whether the IAM bindings are sufficient is unproven until an apply |
| Certificate issuance and the public host | **NOT DONE** | Needs DNS and a live load balancer |

> [!IMPORTANT]
> The correct wording for the acceptance criterion is: **the manifests are valid, the images
> build and start, and here is the command that would apply them.** Anyone reading a "GKE
> deployment: verified" row in a status table should be able to ask for a cluster and be
> shown one. Until then this stays as it is.

The commands that would do it are steps 1–8 above, in order. The ordering is not optional:
secret versions must exist before a pod schedules, or the CSI mount hangs in
`ContainerCreating`; the migration Job must complete before the workloads start, or the API
starts against a schema that does not exist.

## Configuration Contracts

Documenting the configuration each workload requires:

### 1. `commerce-api`

| Variable Name | Purpose | Required? | Source | Implemented in Code? |
| --- | --- | --- | --- | --- |
| `APP_MODULE` | ASGI app factory path (`commerce_api.app:create_app`; the command passes `--factory`) | Required | ConfigMap (`platform-config`) | Yes (Docker entrypoint expands) |
| `PORT` | HTTP server port (`8000`) | Required | ConfigMap (`platform-config`) | Yes (Docker entrypoint expands) |
| `WEB_CONCURRENCY` | Worker concurrency (`1` per ADR 0003 D14) | Required | ConfigMap (`platform-config`) | Yes (Docker entrypoint expands) |
| `DATABASE_URL_APP` | Cloud SQL async connection string for `commerce_app` role | Required | Secret Manager (`db-url-app`) via CSI mount | Yes (`platform_db.engine`) |
| `DATABASE_URL_KERNEL` | Cloud SQL async connection string for `commerce_kernel` role | Required | Secret Manager (`db-url-kernel`) via CSI mount | Yes (`platform_db.engine`) |
| `RAZORPAY_KEY_ID` | Razorpay API Key ID (`rzp_test_...`) | Required | Secret Manager (`razorpay-key-id`) via CSI mount | Yes (`payment_adapters.razorpay.env`) |
| `RAZORPAY_KEY_SECRET` | Razorpay API Secret | Required | Secret Manager (`razorpay-key-secret`) via CSI mount | Yes (`payment_adapters.razorpay.env`) |
| `RAZORPAY_WEBHOOK_SECRET` | Webhook raw-body HMAC secret | Required | Secret Manager (`razorpay-webhook-secret`) via CSI mount | Yes (`payment_adapters.razorpay.env`) |
| `RAZORPAY_PROFILE` | Profile constraint (`DEMO` or `DEVELOPMENT`) | Optional (default: `DEVELOPMENT`) | ConfigMap (`platform-config`) | Yes (`payment_adapters.razorpay.env`) |
| `UCP_MERCHANT_SIGNING_JWK` | The merchant's ES256 signing key, one private P-256 JWK as JSON, carrying a `kid`. Signs "the merchant authorised this checkout" | Required with `UCP_PLATFORM_SIGNING_JWK`, or neither | Secret Manager (`ucp-merchant-signing-jwk`) via CSI mount | Yes (`commerce_api.settings.Settings.ucp_signers`) |
| `UCP_PLATFORM_SIGNING_JWK` | The platform's ES256 signing key, same shape, **different `kid`**. Signs "the platform issued this receipt" | Required with `UCP_MERCHANT_SIGNING_JWK`, or neither | Secret Manager (`ucp-platform-signing-jwk`) via CSI mount | Yes (`commerce_api.settings.Settings.ucp_signers`) |
| `GEMINI_API_KEY` | Developer API key for Gemini models | Optional (if Vertex AI used) | Secret Manager (`gemini-api-key`) via CSI mount | Yes (Google GenAI SDK) |
| `GOOGLE_GENAI_USE_VERTEXAI` | Boolean flag to toggle Vertex AI ADC | Optional (default: `false`) | ConfigMap (`platform-config`) | Pending API integration |
| `GEMINI_MODEL_ID` | Model identifier (`gemini-3.8-flash`) | Optional (default: `gemini-3.8-flash`) | ConfigMap (`platform-config`) | Pending API integration |
| `REDIS_URL` | Non-authoritative Redis cache URL | Required | ConfigMap (`platform-config`) | Pending API integration |
| `PUBLIC_BASE_URL` | Canonical public URL (`https://...`) | Required | ConfigMap (`platform-config`) | Pending API integration |
| `LOG_LEVEL` | Logging level (`INFO` or `DEBUG`) | Optional (default: `INFO`) | ConfigMap (`platform-config`) | Yes (standard Python logging) |

### 2. `action-executor`

| Variable Name | Purpose | Required? | Source | Implemented in Code? |
| --- | --- | --- | --- | --- |
| `WORKER_MODULE` | Module execution path (`action_executor.main`) | Required | ConfigMap (`platform-config`) | Yes (Docker entrypoint expands) |
| `WORKER_HEALTH_PORT` | Worker health server port (`8001`) | Required | ConfigMap (`platform-config`) | Pending worker implementation |
| `DATABASE_URL_WORKER` | Cloud SQL connection string for `commerce_worker` role | Required | Secret Manager (`db-url-worker`) via CSI mount | Yes (`platform_db.engine`) |
| `DATABASE_URL_KERNEL` | Cloud SQL connection string for `commerce_kernel` role | Required | Secret Manager (`db-url-kernel`) via CSI mount | Yes (`platform_db.engine`) |
| `RAZORPAY_KEY_ID` | Razorpay API Key ID (`rzp_test_...`) | Required | Secret Manager (`razorpay-key-id`) via CSI mount | Yes (`payment_adapters.razorpay.env`) |
| `RAZORPAY_KEY_SECRET` | Razorpay API Secret | Required | Secret Manager (`razorpay-key-secret`) via CSI mount | Yes (`payment_adapters.razorpay.env`) |
| `RAZORPAY_PROFILE` | Profile constraint (`DEMO` or `DEVELOPMENT`) | Optional | ConfigMap (`platform-config`) | Yes (`payment_adapters.razorpay.env`) |

> [!IMPORTANT]
> **Executor Credential Contract vs Adapter Loader Decision**:
> The existing `payment_adapters.razorpay.env.load_config_from_env()` requires `RAZORPAY_WEBHOOK_SECRET` alongside `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET`. However, per ADR 0003 (D3, D7), only `commerce-api` serves the webhook route (`POST /webhooks/razorpay/{tenant_slug}`) and verifies incoming HMAC signatures. The `action-executor` only executes outbound HTTP calls (orders, captures, refunds) using HTTP Basic Auth.
>
> In accordance with the principle of least privilege, `razorpay-webhook-secret` is intentionally **withheld** from `action-executor` in Terraform and Kubernetes CSI SecretProviderClass. When implementing `action_executor.main`, the worker must either:
> 1. Use a client-only configuration loader that does not demand `RAZORPAY_WEBHOOK_SECRET`, or
> 2. Initialize its HTTP transport directly from `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET`.
>
> Blindly granting `razorpay-webhook-secret` to `action-executor` to satisfy `load_config_from_env()` is prohibited as it broadens secret access unnecessarily.

### 3. `buyer-web`

Read by `apps/buyer-web/src/app/api/backend/[...path]/route.ts`, the server-side proxy. The
browser talks only to `/api/backend/...` on this app's own origin.

| Variable Name | Purpose | Required? | Source |
| --- | --- | --- | --- |
| `PORT` | Next.js HTTP server port (`3000`) | Required | Container env |
| `HOSTNAME` | Listening address (`0.0.0.0`) | Required | Container env |
| `NODE_ENV` | Runtime environment (`production`) | Required | Container env |
| `APP_SECRETS_DIR` | Where the CSI mount lands (`/var/run/secrets/app`) | Required | Container env |
| `COMMERCE_API_URL` | In-cluster URL for `commerce-api` | Required | Deployment env |
| `NEXT_PUBLIC_TENANT_SLUG` | Which demo tenant this storefront serves | Required | `web-config` ConfigMap; also a build arg, because `NEXT_PUBLIC_*` is inlined into the client bundle |
| `SESSION_COOKIE_SECRET` | Signs the buyer session cookie | Recommended | Secret Manager (`web-session-cookie-secret`) via CSI mount |

`SESSION_COOKIE_SECRET` is "recommended" rather than "required" because the proxy falls back
to a per-process random key when it is absent. That is correct for one pod and wrong for
two: each would reject the other's cookies, and every restart would sign every buyer out.

### 4. `merchant-console`

Read by `apps/merchant-console/src/app/api/backend/[...path]/route.ts`. This is the only
place the console holds a credential, and it holds two.

| Variable Name | Purpose | Required? | Source |
| --- | --- | --- | --- |
| `PORT` | Next.js HTTP server port (`3001`) | Required | Container env |
| `HOSTNAME` | Listening address (`0.0.0.0`) | Required | Container env |
| `NODE_ENV` | Runtime environment (`production`) | Required | Container env |
| `APP_SECRETS_DIR` | Where the CSI mount lands (`/var/run/secrets/app`) | Required | Container env |
| `COMMERCE_API_URL` | In-cluster URL for `commerce-api` | Required | Deployment env |
| `NEXT_PUBLIC_TENANT_SLUG` | Which tenant this console operates | Required | `web-config` ConfigMap; also a build arg |
| `SCENARIO_KEY` | Mints the OPERATOR session and widens tenant-scoped reads | Required | Secret Manager (`scenario-key`) via CSI mount |
| `OPERATOR_COOKIE_SECRET` | Signs the operator session cookie | Recommended | Secret Manager (`console-cookie-secret`) via CSI mount |

Neither secret may ever be given a `NEXT_PUBLIC_` name or passed as a build argument. A
`NEXT_PUBLIC_*` value is inlined into the client bundle by `next build`, so it would be
readable in any visitor's network panel; a build argument is recorded in the image history
and printed back by `docker history`. `scripts/validate_infra.sh` step 6 checks both.

**Why a shim is needed for these two.** The GKE Secret Manager add-on mounts secrets as
files and cannot sync them into Kubernetes Secrets or environment variables. The Python
images solve this with `infra/docker/entrypoint.py`; the Next images use
`infra/docker/node-entrypoint.mjs`, which has a deliberately identical contract — a file
named after the variable it carries, an already-set variable wins, only the trailing newline
is stripped, names are logged and values never are. Without it the only way to give a Next
process its credentials would be an environment literal in a manifest, which is what
specification 21.8 forbids.

### 5. `db-migrate` (Job)

| Variable Name | Purpose | Required? | Source | Implemented in Code? |
| --- | --- | --- | --- | --- |
| `PROJECT_ID` | GCP Project ID used to construct IAM user | Required | ConfigMap (`platform-config`) | Yes (Job manifest env) |
| `DATABASE_URL` | Direct migration database URL | Required | Job container env | Yes (`packages/platform-db/alembic.ini`) |
| `PYTHONUNBUFFERED` | Unbuffered stdout/stderr | Required | Job container env | Yes (Python runtime) |

### Liveness vs Readiness Distinction

- **Liveness** (`/healthz` on API and worker, `/` on web): Determines if the process is deadlock-free. It intentionally does *not* assert database or provider readiness to avoid cascading restart storms during temporary backend blips.
- **Readiness** (`/healthz` on API, `/` on web): Determines if the container is ready to accept user traffic from the Ingress / NEG. Does not depend on the LLM (spec 23.2).

Kubernetes `$(VAR)` and entrypoint `${VAR}` expansion both work in `args`.


## What a demo day costs (rough, asia-south1, verify in the Pricing Calculator)

| Item | Sizing | ≈ USD / day |
| --- | --- | --- |
| GKE Autopilot cluster fee | one cluster (one Autopilot/zonal cluster per billing account is free) | 0 – 2.4 |
| Autopilot Pod resources | ~1.5 vCPU + ~3 GiB requested across API, worker, web, proxies | 2.0 – 2.5 |
| Cloud SQL `db-custom-1-3840`, zonal, 10 GB SSD, PITR | 1 vCPU, 3.75 GB | 1.7 – 2.0 |
| Memorystore Redis Basic 1 GB | | 1.1 – 1.3 |
| Global external Application LB | forwarding rule + small traffic | 0.6 – 1.0 |
| Cloud NAT | gateway + minimal egress | 1.0 – 1.2 |
| Artifact Registry, Secret Manager, logging, Cloud Build | demo volumes | < 0.5 |
| **Total** | | **≈ 7 – 11** |

Gemini and Razorpay test mode are outside this table (Gemini per token; test mode free).

## Teardown

```sh
kubectl delete -k infra/kubernetes/overlays/demo            # releases the LB, NEGs and certificate
kubectl delete job -n commerce db-migrate --ignore-not-found
cd infra/terraform
sed -i '' 's/deletion_protection = true/deletion_protection = false/' terraform.tfvars
terraform apply                                             # lifts deletion protection
terraform destroy
```

Secret Manager secrets are destroyed with the rest; if you want to keep the Razorpay values,
`terraform state rm 'google_secret_manager_secret.app'` first. The Cloud SQL instance name is
blocked for about a week after deletion: bump `db_instance_suffix` (and the overlay's
`INSTANCE_CONNECTION_NAME`) when recreating.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Proxy sidecar logs `iam.serviceAccounts.getAccessToken denied` | the workload GSA lacks `roles/iam.serviceAccountTokenCreator` on the db identity, or the KSA annotation in `workload-identity.yaml` still says `PROJECT_ID` |
| `password authentication failed for user "db-commerce-app@..."` or `permission denied for table` | `infra/sql/02-grant-app-identities.sql` not run, or run before the migration |
| Migration Job fails with `permission denied to create role` | `infra/sql/01-grant-migration-identity.sql` not run |
| Worker cannot reach `api.razorpay.com` | `enable_fqdn_network_policy` still `false` (second Terraform apply), or Cloud NAT missing |
| `ManagedCertificate` stuck in `Provisioning` | DNS `A` record missing or not yet propagated; the LB also needs ~10 minutes |
| Pod stuck `ContainerCreating` with `secrets-store` errors | secret has no version, or the accessor binding is missing; `kubectl -n commerce describe pod` shows the secret name |
| Pod rejected by Autopilot | requests/limits changed and now violate the 1:1–1:6.5 CPU:memory ratio |
| API restarts with `WEB_CONCURRENCY` error | the ConfigMap value was changed away from `1`. It must stay `1`: the merchant simulator's state lives in the API process (ADR 0003 D14), so a second worker serves a different shop |
| Console reads return 404 rather than data | the API did not receive `scenario-key`, so the scenario routes genuinely do not exist. `GET /v1/config` reports `scenario_routes_enabled: false` |
| Console reads return 401 | the API has the key and the console has a different one. Both read the same `scenario-key` secret; check the version each pod mounted |
| Operators are signed out on every console request | `console-cookie-secret` has no version, so the proxy fell back to a per-process random key. `kubectl logs deploy/merchant-console -c web \| head -1` lists the secret names it loaded |
| `ManagedCertificate` stuck with two domains | *both* `A` records must resolve. A missing `console.$HOST` record blocks the whole certificate, including the storefront's domain |

### A query that "shows nothing" is probably showing you a policy

Every tenant-owned table in this schema carries row-level security with **`FORCE`** set, and
the policy is keyed on the transaction-local setting `app.tenant_id`. A connection that has
not bound a tenant therefore matches **no rows and raises no error**. The query returns an
empty result, and an empty result is indistinguishable from the truth.

That applies to `outbox_events`, `payment_attempts`, `refunds`, `execution_grants`,
`scenario_faults`, `reservations`, `approvals`, `audit_events` and the rest. `tenants` is
the one table with no tenant column and so no policy, which is how a connection discovers
the uuid it needs in the first place:

```sql
SELECT id FROM tenants WHERE slug = '<your-slug>';

SET app.tenant_id = '<that-uuid>';
SELECT status, count(*) FROM outbox_events GROUP BY status;
```

**A superuser bypasses row-level security entirely**, and that is what makes this trap
durable rather than merely annoying. Whoever writes the query is often connected as the
database owner, so it works for them, gets pasted into a document, and returns silence for
every reader who runs it under an application role. A query working for its author is not
evidence that it works.

Before concluding a table is empty:

```sql
SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user;
```

If `rolsuper` is false and you bound no tenant, you have measured your own permissions, not
the data. This caught two separate people on this project within one hour — once as a wrong
sentence in a status report, and once as an instruction in a runbook telling a presenter to
check whether a scenario fault was armed. "Nothing is armed" and "you cannot see what is
armed" are opposite facts wearing the same empty result.
