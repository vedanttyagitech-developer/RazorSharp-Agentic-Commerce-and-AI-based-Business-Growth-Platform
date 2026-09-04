# Deploying the demo to Google Cloud

From an empty project to a working `https://<host>/` in about an hour, most of it waiting
for Cloud SQL and GKE. Everything is reproducible: Terraform for the platform
(`infra/terraform`), Kustomize for the workloads (`infra/kubernetes`), Dockerfiles for the
images (`infra/docker`). Nothing in this document prints a secret value, and nothing in the
repository contains one.

Topology (spec 23): Global HTTPS load balancer → GKE Autopilot (`commerce` namespace:
`buyer-web`, `commerce-api`, `durable-worker`) → Cloud SQL PostgreSQL 16 (private IP, IAM
auth through proxy sidecars), Memorystore Redis 7.2, Secret Manager, Gemini, Razorpay test
mode. One replica of the API and of the worker (ADR 0003 D14).

## 0. Prerequisites

- `gcloud` (with `gke-gcloud-auth-plugin`), `kubectl` ≥ 1.29, `terraform` ≥ 1.9, `kubeconform` ≥ 0.8.0.
  Docker is optional: Cloud Build builds the images.
- A domain you control for the demo host (an `A` record you can create). Google-managed
  certificates need a public name.
- A Razorpay account in test mode (`rzp_test_` keys) and a Gemini API key or Vertex AI
  access in the project.
- `apps/buyer-web/next.config.ts` contains `output: "standalone"` (see
  `infra/docker/README.md`); `durable_worker.main` serves `GET /healthz` on port 8001
  (see "Contracts" below).
- Run repeatable local validation before touching any cloud resources:
  ```sh
  ./scripts/validate_infra.sh
  ```

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

Create the DNS record now so the certificate can be issued while you do the rest:
`A  $HOST  →  $(terraform output -raw ingress_ip)`.

## 3. Secret values (never echoed)

Terraform created seven empty secrets. Add one version to each; the helper reads the value
without echo and pipes it straight into `gcloud`. The `db-url-*` values contain no password
(IAM authentication through the proxy sidecars) but are stored as secrets by policy.

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

printf '%s' "postgresql+psycopg://db-commerce-app%40$PROJECT_ID.iam@127.0.0.1:5432/commerce"    | gcloud secrets versions add db-url-app    --data-file=-
printf '%s' "postgresql+psycopg://db-commerce-kernel%40$PROJECT_ID.iam@127.0.0.1:5433/commerce" | gcloud secrets versions add db-url-kernel --data-file=-
printf '%s' "postgresql+psycopg://db-commerce-worker%40$PROJECT_ID.iam@127.0.0.1:5432/commerce" | gcloud secrets versions add db-url-worker --data-file=-
```

`%40` is `@` inside the user name (`db-commerce-app@PROJECT_ID.iam` is the Cloud SQL IAM
user). Port 5432 is a process's primary role proxy, 5433 the kernel-role proxy.

## 4. Database bootstrap (one time)

The built-in `postgres` user has no password yet; set one interactively and use Cloud SQL
Studio (console → SQL → instance → Cloud SQL Studio; user `postgres`, database `commerce`):

```sh
gcloud sql users set-password postgres --instance="commerce-demo-pg-01" --prompt-for-password
```

Run `infra/sql/01-grant-migration-identity.sql` with `PROJECT_ID` replaced. It gives the
migration Job's identity `cloudsqlsuperuser` (needed to create the NOLOGIN group roles).

## 5. Images

```sh
cd "$(git rev-parse --show-toplevel)"
gcloud builds submit --config infra/docker/cloudbuild.yaml \
  --substitutions=_TAG=demo,_REGISTRY=$REGION-docker.pkg.dev/$PROJECT_ID/commerce .
```

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
kubectl -n commerce rollout status deployment/commerce-api deployment/durable-worker deployment/buyer-web
kubectl -n commerce get pods,svc,ingress
kubectl -n commerce get managedcertificate commerce   # Provisioning -> Active (up to ~60 min after DNS resolves)
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
```

Worker: `kubectl -n commerce logs deploy/durable-worker -c worker --tail=50`. Proxies:
`-c cloud-sql-proxy-app`, `-c cloud-sql-proxy-kernel`, `-c cloud-sql-proxy-worker`.

## Redeploying a change

1. Build with a new `_TAG` (step 5).
2. Set `newTag` in `infra/kubernetes/overlays/demo/images/kustomization.yaml`.
3. Steps 7 and 8. The API and the worker use `Recreate`, so expect a few seconds of API
   downtime per rollout; the web front end rolls without downtime.

## Deployment Ordering: Build, Migration and Rollout

Strict ordering is required to ensure database schemas and credentials exist before workloads start:

1. **Packaging & Image Build**:
   Build the three container images (`commerce-api`, `durable-worker`, `buyer-web`) with matching tags (e.g. `_TAG=demo`).
2. **Platform & Networking Infrastructure**:
   Execute Terraform applies (cluster, VPC, Cloud SQL, Memorystore, Artifact Registry, IAM, Secret Manager).
3. **Secret Values Population**:
   Add versions for all 7 Secret Manager secrets before pod creation so CSI driver mounts do not hang.
4. **Pre-Migration Database Bootstrap**:
   Run `infra/sql/01-grant-migration-identity.sql` in Cloud SQL Studio to grant `cloudsqlsuperuser` to `db-migration@PROJECT_ID.iam` so it can create the NOLOGIN PostgreSQL group roles.
5. **Database Migration Job**:
   Apply `infra/kubernetes/overlays/<env>/db-migrate`. Wait for Job completion (`kubectl wait --for=condition=complete job/db-migrate --timeout=15m`).
6. **Post-Migration Role Grants**:
   Run `infra/sql/02-grant-app-identities.sql` in Cloud SQL Studio to grant `commerce_app`, `commerce_kernel`, and `commerce_worker` roles to the IAM identities.
7. **Workload Rollout**:
   Apply `infra/kubernetes/overlays/<env>` to roll out `commerce-api`, `durable-worker`, and `buyer-web`.

## Application Entrypoint Status & Pre-Deployment Blockers

Distinguish configuration validity, image build success, application startup, and actual deployment:

| Workload | Executable Entrypoint | Image Build Status | Startup Smoke Status | Deployment Status & Blocker |
| --- | --- | --- | --- | --- |
| `buyer-web` | `node server.js` (Next.js 16 standalone) | **BUILT** (`buyer-web:dev`) | **PASSED** (HTTP 200 on `/`) | **Ready for deployment** once cluster infrastructure is live. |
| `commerce-api` | `uvicorn commerce_api.app:app` | **BUILT** (`commerce-api:dev`) | **FAILED** (`ModuleNotFoundError: No module named 'commerce_api.app'`) | **BLOCKED**: Application module `commerce_api.app` in `packages/commerce-api` is not yet implemented. |
| `durable-worker` | `python -m durable_worker.main` | **BUILT** (`durable-worker:dev`) | **FAILED** (`No module named durable_worker.main`) | **BLOCKED**: Worker entrypoint module `durable_worker.main` in `packages/durable-worker` is not yet implemented. |

> [!IMPORTANT]
> Do not attempt to deploy `commerce-api` or `durable-worker` to a live GKE cluster until their underlying application entrypoints and routers/handlers are implemented. Dummy implementations must never be introduced to mask deployment failures.

## Configuration Contracts

Documenting the configuration each workload requires:

### 1. `commerce-api`

| Variable Name | Purpose | Required? | Source | Implemented in Code? |
| --- | --- | --- | --- | --- |
| `APP_MODULE` | ASGI app module path (`commerce_api.app:app`) | Required | ConfigMap (`platform-config`) | Yes (Docker entrypoint expands) |
| `PORT` | HTTP server port (`8000`) | Required | ConfigMap (`platform-config`) | Yes (Docker entrypoint expands) |
| `WEB_CONCURRENCY` | Worker concurrency (`1` per ADR 0003 D14) | Required | ConfigMap (`platform-config`) | Yes (Docker entrypoint expands) |
| `DATABASE_URL_APP` | Cloud SQL async connection string for `commerce_app` role | Required | Secret Manager (`db-url-app`) via CSI mount | Yes (`platform_db.engine`) |
| `DATABASE_URL_KERNEL` | Cloud SQL async connection string for `commerce_kernel` role | Required | Secret Manager (`db-url-kernel`) via CSI mount | Yes (`platform_db.engine`) |
| `RAZORPAY_KEY_ID` | Razorpay API Key ID (`rzp_test_...`) | Required | Secret Manager (`razorpay-key-id`) via CSI mount | Yes (`payment_adapters.razorpay.env`) |
| `RAZORPAY_KEY_SECRET` | Razorpay API Secret | Required | Secret Manager (`razorpay-key-secret`) via CSI mount | Yes (`payment_adapters.razorpay.env`) |
| `RAZORPAY_WEBHOOK_SECRET` | Webhook raw-body HMAC secret | Required | Secret Manager (`razorpay-webhook-secret`) via CSI mount | Yes (`payment_adapters.razorpay.env`) |
| `RAZORPAY_PROFILE` | Profile constraint (`DEMO` or `DEVELOPMENT`) | Optional (default: `DEVELOPMENT`) | ConfigMap (`platform-config`) | Yes (`payment_adapters.razorpay.env`) |
| `GEMINI_API_KEY` | Developer API key for Gemini models | Optional (if Vertex AI used) | Secret Manager (`gemini-api-key`) via CSI mount | Yes (Google GenAI SDK) |
| `GOOGLE_GENAI_USE_VERTEXAI` | Boolean flag to toggle Vertex AI ADC | Optional (default: `false`) | ConfigMap (`platform-config`) | Pending API integration |
| `GEMINI_MODEL_ID` | Model identifier (`gemini-3.8-flash`) | Optional (default: `gemini-3.8-flash`) | ConfigMap (`platform-config`) | Pending API integration |
| `REDIS_URL` | Non-authoritative Redis cache URL | Required | ConfigMap (`platform-config`) | Pending API integration |
| `PUBLIC_BASE_URL` | Canonical public URL (`https://...`) | Required | ConfigMap (`platform-config`) | Pending API integration |
| `LOG_LEVEL` | Logging level (`INFO` or `DEBUG`) | Optional (default: `INFO`) | ConfigMap (`platform-config`) | Yes (standard Python logging) |

### 2. `durable-worker`

| Variable Name | Purpose | Required? | Source | Implemented in Code? |
| --- | --- | --- | --- | --- |
| `WORKER_MODULE` | Module execution path (`durable_worker.main`) | Required | ConfigMap (`platform-config`) | Yes (Docker entrypoint expands) |
| `WORKER_HEALTH_PORT` | Worker health server port (`8001`) | Required | ConfigMap (`platform-config`) | Pending worker implementation |
| `DATABASE_URL_WORKER` | Cloud SQL connection string for `commerce_worker` role | Required | Secret Manager (`db-url-worker`) via CSI mount | Yes (`platform_db.engine`) |
| `DATABASE_URL_KERNEL` | Cloud SQL connection string for `commerce_kernel` role | Required | Secret Manager (`db-url-kernel`) via CSI mount | Yes (`platform_db.engine`) |
| `RAZORPAY_KEY_ID` | Razorpay API Key ID (`rzp_test_...`) | Required | Secret Manager (`razorpay-key-id`) via CSI mount | Yes (`payment_adapters.razorpay.env`) |
| `RAZORPAY_KEY_SECRET` | Razorpay API Secret | Required | Secret Manager (`razorpay-key-secret`) via CSI mount | Yes (`payment_adapters.razorpay.env`) |
| `RAZORPAY_PROFILE` | Profile constraint (`DEMO` or `DEVELOPMENT`) | Optional | ConfigMap (`platform-config`) | Yes (`payment_adapters.razorpay.env`) |

> [!IMPORTANT]
> **Worker Credential Contract vs Adapter Loader Decision**:
> The existing `payment_adapters.razorpay.env.load_config_from_env()` requires `RAZORPAY_WEBHOOK_SECRET` alongside `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET`. However, per ADR 0003 (D3, D7), only `commerce-api` serves the webhook route (`POST /webhooks/razorpay/{tenant_slug}`) and verifies incoming HMAC signatures. The `durable-worker` only executes outbound HTTP calls (orders, captures, refunds) using HTTP Basic Auth.
>
> In accordance with the principle of least privilege, `razorpay-webhook-secret` is intentionally **withheld** from `durable-worker` in Terraform and Kubernetes CSI SecretProviderClass. When implementing `durable_worker.main`, the worker must either:
> 1. Use a client-only configuration loader that does not demand `RAZORPAY_WEBHOOK_SECRET`, or
> 2. Initialize its HTTP transport directly from `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET`.
>
> Blindly granting `razorpay-webhook-secret` to `durable-worker` to satisfy `load_config_from_env()` is prohibited as it broadens secret access unnecessarily.

### 3. `buyer-web`

| Variable Name | Purpose | Required? | Source | Implemented in Code? |
| --- | --- | --- | --- | --- |
| `PORT` | Next.js HTTP server port (`3000`) | Required | Container env | Yes (Next.js server) |
| `HOSTNAME` | Listening address (`0.0.0.0`) | Required | Container env | Yes (Next.js server) |
| `NODE_ENV` | Runtime environment (`production`) | Required | Container env | Yes (Node.js runtime) |
| `NEXT_PUBLIC_API_MODE` | Frontend API mode (`live` vs `mock`) | Required | Container env / build arg | Yes (`apps/buyer-web/src/lib/server/env.ts`) |
| `API_BASE` | In-cluster URL for `commerce-api` | Required | Container env | Yes (`apps/buyer-web/src/lib/server/env.ts`) |

### 4. `db-migrate` (Job)

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
| API restarts with `WEB_CONCURRENCY` error | the ConfigMap value was changed away from `1` |
